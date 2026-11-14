from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from typing import Literal
from zoneinfo import ZoneInfo
import asyncio

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .analytics import COUNT_FIELDS, MEMBER_ROLES, _rollup, read_observations
from .domain import EDITOR_ROLES, DomainError, audit_row
from .integrations import TelegramError


SCHEMA = """
CREATE TABLE IF NOT EXISTS report_schedules (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    recipient_id INTEGER NOT NULL REFERENCES users(id),
    frequency TEXT NOT NULL CHECK(frequency IN ('daily','weekly')),
    time TEXT NOT NULL,
    weekday INTEGER,
    notify INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    request_key TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(company_id,actor_id,request_key)
);
CREATE INDEX IF NOT EXISTS report_schedules_due ON report_schedules(company_id,enabled);
CREATE TABLE IF NOT EXISTS company_reports (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    run_key TEXT NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES users(id),
    schedule_id INTEGER REFERENCES report_schedules(id),
    schedule_version INTEGER,
    recipient_id INTEGER REFERENCES users(id),
    telegram_chat_id TEXT,
    bot_fingerprint TEXT,
    start_on TEXT NOT NULL,
    end_on TEXT NOT NULL,
    timezone TEXT NOT NULL,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    delivery_status TEXT NOT NULL,
    delivery_error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    claimed_at REAL,
    delivered_at TEXT,
    UNIQUE(company_id,run_key)
);
CREATE INDEX IF NOT EXISTS company_reports_delivery ON company_reports(company_id,delivery_status,next_attempt,id);
"""

PUBLIC_SCHEDULE = "id,frequency,time,weekday,notify,enabled,version,recipient_id"
PUBLIC_REPORT = "id,start_on,end_on,timezone,created_at,text,delivery_status,delivery_error,delivered_at,schedule_id"
METRIC_LABELS = dict(zip(COUNT_FIELDS, ("Просмотры", "Охват", "Реакции", "Комментарии", "Репосты")))
REPORT_DAYS = {"daily": 1, "weekly": 7}
DELIVERY_ERRORS = {
    "access_changed": "Проверьте доступ автора расписания и привязку Telegram получателя.",
    "bot_changed": "Подключение бота изменилось с момента постановки отчёта в очередь.",
    "schedule_changed": "Расписание изменилось или было отключено.",
    "rate_limited": "Telegram ограничил частоту отправки. Повторим отправку позже.",
    "connection": "Сбой подключения к Telegram.",
    "rejected": "Telegram отклонил сообщение. Проверьте, что получатель начал чат с ботом.",
    "uncertain": "Telegram мог доставить отчёт. Проверьте личный чат с ботом.",
}


class ScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    frequency: Literal["daily", "weekly"]
    time: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    weekday: int | None = Field(default=None, strict=True, ge=0, le=6)
    recipient_id: int | None = Field(default=None, strict=True, gt=0)
    notify: bool = True
    enabled: bool = True

    @model_validator(mode="after")
    def validate_weekday(self):
        if (self.frequency == "weekly") != (self.weekday is not None):
            raise ValueError("Укажите день недели для еженедельного расписания.")
        return self


class ScheduleUpdate(ScheduleCreate):
    version: int = Field(strict=True, gt=0)


class ReportPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    days: Literal[1, 7] = 7


def initialize_reports(service):
    with service.db.transaction() as connection:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                connection.execute(statement)


def _now(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _timestamp(instant):
    return instant.isoformat(timespec="microseconds")


def _require_actor(service, connection, cid, actor, roles):
    if actor is None:
        raise DomainError("Войдите в учётную запись компании.", 403)
    service.require_actor(connection, cid, actor, roles)
    actor_id = actor["id"] if isinstance(actor, dict) else actor
    return connection.execute(
        "SELECT user_id,role FROM memberships WHERE company_id=? AND user_id=? AND active=1", (cid, actor_id),
    ).fetchone()


def _recipient(connection, cid, recipient_id):
    return connection.execute(
        "SELECT u.id,u.name,m.telegram_id FROM memberships m JOIN users u ON u.id=m.user_id "
        "WHERE m.company_id=? AND m.user_id=? AND m.active=1 AND u.is_admin=0",
        (cid, recipient_id),
    ).fetchone()


def _chat_id(row):
    value = row["telegram_id"] if row else None
    if value and value.isascii() and value.isdigit() and 0 < int(value) < 2**63:
        return value
    return None


def _bot_fingerprint(service, company):
    token = service._credentials(company, {"telegram_token"})["telegram_token"]
    return _fingerprint(token) if token else None


def _fingerprint(token):
    return sha256(token.encode()).hexdigest()


def _target(service, connection, cid, recipient_id, company):
    chat = _chat_id(_recipient(connection, cid, recipient_id))
    if not chat:
        raise DomainError("Привяжите Telegram получателя к действующей учётной записи компании.", 422)
    bot = _bot_fingerprint(service, company)
    if not bot:
        raise DomainError("Подключите бота Telegram в настройках компании.", 422)
    return chat, bot


def _schedule_data(row):
    return {**dict(row), "notify": bool(row["notify"]), "enabled": bool(row["enabled"])}


def _schedule_list(connection, cid, member):
    return [_schedule_data(row) for row in connection.execute(
        f"SELECT {PUBLIC_SCHEDULE} FROM report_schedules "
        "WHERE company_id=? AND (actor_id=? OR ?='admin') ORDER BY id DESC",
        (cid, member["user_id"], member["role"]),
    )]


def list_report_schedules(service, cid, actor):
    with service.db.read() as connection:
        member = _require_actor(service, connection, cid, actor, EDITOR_ROLES)
        return _schedule_list(connection, cid, member)


def _schedule_row(connection, cid, schedule_id, member):
    row = connection.execute(
        f"SELECT {PUBLIC_SCHEDULE} FROM report_schedules WHERE company_id=? AND id=? AND (actor_id=? OR ?='admin')",
        (cid, schedule_id, member["user_id"], member["role"]),
    ).fetchone()
    if row is None:
        raise DomainError("Расписание отчёта не найдено.", 404)
    return row


def _schedule_values(service, connection, cid, member, payload):
    recipient = payload.recipient_id or member["user_id"]
    if recipient != member["user_id"] and member["role"] != "admin":
        raise DomainError("Другого получателя может назначить администратор компании.", 403)
    if not _recipient(connection, cid, recipient):
        raise DomainError("Получатель должен быть действующим сотрудником компании.", 422)
    if payload.enabled and payload.notify:
        _target(service, connection, cid, recipient, service._company_row(connection, cid))
    return recipient, payload.frequency, payload.time, payload.weekday, payload.notify, payload.enabled


def create_report_schedule(service, cid, actor, payload, *, request_key=None):
    with service.db.transaction() as connection:
        member = _require_actor(service, connection, cid, actor, EDITOR_ROLES)
        if request_key is not None:
            existing = connection.execute(
                f"SELECT {PUBLIC_SCHEDULE} FROM report_schedules WHERE company_id=? AND actor_id=? AND request_key=?",
                (cid, member["user_id"], request_key),
            ).fetchone()
            if existing:
                return _schedule_data(existing)
        values = _schedule_values(service, connection, cid, member, payload)
        schedule_id = connection.execute(
            "INSERT INTO report_schedules(company_id,actor_id,recipient_id,frequency,time,weekday,notify,enabled,"
            "request_key,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, member["user_id"], *values, request_key, _timestamp(_now())),
        ).lastrowid
        audit_row(connection, cid, "reports.schedule_created", f"Создано расписание отчётов №{schedule_id}.", actor)
        return _schedule_data(_schedule_row(connection, cid, schedule_id, member))


def _invalidate_queued(connection, cid, schedule_id):
    connection.execute(
        "UPDATE company_reports SET delivery_status='cancelled',delivery_error='schedule_changed' "
        "WHERE company_id=? AND schedule_id=? AND delivery_status='pending'",
        (cid, schedule_id),
    )


def update_report_schedule(service, cid, actor, schedule_id, payload):
    with service.db.transaction() as connection:
        member = _require_actor(service, connection, cid, actor, EDITOR_ROLES)
        current = _schedule_row(connection, cid, schedule_id, member)
        if current["version"] != payload.version:
            raise DomainError("Расписание изменилось. Обновите страницу.", 409)
        values = _schedule_values(service, connection, cid, member, payload)
        fields = ("recipient_id", "frequency", "time", "weekday", "notify", "enabled")
        if tuple(current[field] for field in fields) != values:
            connection.execute(
                "UPDATE report_schedules SET recipient_id=?,frequency=?,time=?,weekday=?,notify=?,enabled=?,"
                "version=version+1,updated_at=? WHERE company_id=? AND id=?",
                (*values, _timestamp(_now()), cid, schedule_id),
            )
            _invalidate_queued(connection, cid, schedule_id)
            audit_row(connection, cid, "reports.schedule_updated", f"Изменено расписание отчётов №{schedule_id}.", actor)
        return _schedule_data(_schedule_row(connection, cid, schedule_id, member))


def disable_report_schedule(service, cid, actor, schedule_id, *, expected_version=None):
    with service.db.transaction() as connection:
        member = _require_actor(service, connection, cid, actor, EDITOR_ROLES)
        current = _schedule_row(connection, cid, schedule_id, member)
        version = current["version"]
        if not current["enabled"] and expected_version in (None, version, version - 1):
            return _schedule_data(current)
        if expected_version is not None and expected_version != version:
            raise DomainError("Расписание изменилось. Запросите список заново.", 409)
        connection.execute(
            "UPDATE report_schedules SET enabled=0,version=version+1,updated_at=? WHERE company_id=? AND id=?",
            (_timestamp(_now()), cid, schedule_id),
        )
        _invalidate_queued(connection, cid, schedule_id)
        audit_row(connection, cid, "reports.schedule_disabled", f"Отключено расписание отчётов №{schedule_id}.", actor)
        return _schedule_data(_schedule_row(connection, cid, schedule_id, member))


def _telegram_metrics(connection, cid, start, end, local_today, now):
    rows = read_observations(connection, cid, start, end, platform="telegram",
                             observed_through=local_today.isoformat(), recorded_through=_timestamp(now))
    return rows, _rollup(rows)


def _report_data(connection, company, end_exclusive, days, now):
    cid = company["id"]
    zone = ZoneInfo(company["timezone"])
    local_now = now.astimezone(zone)
    start = end_exclusive - timedelta(days=days)
    end = end_exclusive - timedelta(days=1)
    first = datetime.combine(start, time.min, zone).astimezone(timezone.utc)
    last = datetime.combine(end_exclusive, time.min, zone).astimezone(timezone.utc)
    bounds = cid, _timestamp(first), _timestamp(last)
    publications = connection.execute(
        "SELECT count(*) FROM activity WHERE company_id=? AND action='post.published' "
        "AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)",
        bounds,
    ).fetchone()[0]
    tasks = connection.execute(
        "SELECT count(*) AS started, count(CASE WHEN status='completed' THEN 1 END) AS completed "
        "FROM assistant_tasks WHERE company_id=? "
        "AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)",
        bounds,
    ).fetchone()
    post_queue = dict(connection.execute(
        "SELECT status,count(*) FROM posts WHERE company_id=? AND status IN ('review','needs_info','failed') GROUP BY status",
        (cid,),
    ))
    review, needs_info, failed = (post_queue.get(status, 0) for status in ("review", "needs_info", "failed"))
    rows, metrics = _telegram_metrics(connection, cid, start, end, local_now.date(), now)
    report = [
        f"Редакционная сводка · {company['name']}",
        f"Период {start:%d.%m.%Y} — {end:%d.%m.%Y} ({zone.key})",
        f"Публикаций через кабинет за период — {publications}.",
        f"Задач помощнику создано — {tasks['started']}. Из них выполнено к моменту отчёта — {tasks['completed']}.",
        "",
        f"Статусы постов в кабинете на {local_now:%d.%m.%Y %H:%M}:",
        f"На согласовании — {review}. Требуют уточнения — {needs_info}. С ошибкой отправки — {failed}.",
        "",
        "Telegram · публикации за период",
    ]
    if rows:
        observation_dates = [entry["observed_on"] for row in rows for entry in row["metric_observations"].values()]
        first_observation = date.fromisoformat(min(observation_dates))
        last_observation = date.fromisoformat(max(observation_dates))
        observed = f"{first_observation:%d.%m.%Y}"
        if first_observation != last_observation:
            observed += f" — {last_observation:%d.%m.%Y}"
        report.append(f"Публикаций с замерами — {len(rows)}. Даты последних замеров — {observed}.")
        for metric, label in METRIC_LABELS.items():
            value = metrics["totals"][metric]
            coverage = metrics["coverage"][metric]
            report.append(f"{label} — {value} (по {coverage} из {len(rows)})" if value is not None else f"{label} — нет данных")
        er = metrics["er"]
        if er["percent"] is not None:
            report.append(f"ER по охвату — {er['percent']:.2f}% (по {er['publications']} из {len(rows)}).")
    else:
        report.append("Замеров нет. Просмотры, охваты, реакции, комментарии и репосты неизвестны.")
    recommendations = []
    if failed:
        recommendations.append("Откройте посты с ошибкой и сверьте их с каналом перед повторной отправкой.")
    if review:
        recommendations.append("Откройте очередь согласования и начните с ближайших запланированных публикаций.")
    if needs_info:
        recommendations.append("Пополните материалы к постам, у которых остались вопросы.")
    if not publications:
        recommendations.append("Проверьте план публикаций на ближайшие дни.")
    if not rows or metrics["coverage"]["views"] < len(rows):
        recommendations.append("Добавьте доступные замеры Telegram из статистики канала в раздел «Аналитика».")
    if recommendations:
        report.extend(["", "Следующие шаги", *(f"• {item}" for item in recommendations)])
    return {
        "start_on": start.isoformat(), "end_on": end.isoformat(), "timezone": zone.key,
        "created_at": _timestamp(now), "text": "\n".join(report),
    }


def preview_report(service, cid, actor, days=7, *, now=None):
    if days not in REPORT_DAYS.values():
        raise DomainError("Выберите предыдущий день или последние семь дней.", 422)
    now = _now(now)
    with service.db.read() as connection:
        connection.execute("BEGIN")
        _require_actor(service, connection, cid, actor, MEMBER_ROLES)
        company = service._company_row(connection, cid)
        return _report_data(connection, company, now.astimezone(ZoneInfo(company["timezone"])).date(), days, now)


def _public_report(row):
    return {**dict(row), "delivery_error": DELIVERY_ERRORS.get(row["delivery_error"])}


def _fetch_report(connection, cid, report_id):
    row = connection.execute("SELECT * FROM company_reports WHERE company_id=? AND id=?", (cid, report_id)).fetchone()
    if row is None:
        raise DomainError("Отчёт не найден.", 404)
    return row


def _report_by_key(connection, cid, key):
    return connection.execute(
        f"SELECT {PUBLIC_REPORT} FROM company_reports WHERE company_id=? AND run_key=?", (cid, key),
    ).fetchone()


def _save_report(connection, cid, key, actor_id, report, *, schedule=None, target=(None, None), error=None):
    chat, bot = target
    status = "pending" if chat else "cancelled" if error else "ready"
    connection.execute(
        "INSERT INTO company_reports(company_id,run_key,actor_id,schedule_id,schedule_version,recipient_id,"
        "telegram_chat_id,bot_fingerprint,start_on,end_on,timezone,created_at,text,delivery_status,delivery_error) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, key, actor_id, schedule["id"] if schedule else None, schedule["version"] if schedule else None,
         schedule["recipient_id"] if schedule else None, chat, bot, report["start_on"], report["end_on"],
         report["timezone"], report["created_at"], report["text"], status, error),
    )
    return _public_report(_report_by_key(connection, cid, key))


def generate_report(service, cid, actor, days, *, now=None):
    if days not in REPORT_DAYS.values():
        raise DomainError("Выберите предыдущий день или последние семь дней.", 422)
    now = _now(now)
    with service.db.transaction() as connection:
        member = _require_actor(service, connection, cid, actor, EDITOR_ROLES)
        company = service._company_row(connection, cid)
        end = now.astimezone(ZoneInfo(company["timezone"])).date()
        key = f"manual:{member['user_id']}:{company['timezone']}:{end}:{days}"
        existing = _report_by_key(connection, cid, key)
        if existing:
            return _public_report(existing)
        return _save_report(connection, cid, key, member["user_id"], _report_data(connection, company, end, days, now))


def _latest_slot(schedule, local_now):
    today = local_now.date()
    slot_time = time.fromisoformat(schedule["time"])
    weekday = schedule["weekday"]
    weekly = schedule["frequency"] == "weekly"
    days_ago = (today.weekday() - weekday) % 7 if weekly else 0
    slot = datetime.combine(today - timedelta(days=days_ago), slot_time, local_now.tzinfo)
    if slot.astimezone(timezone.utc) > local_now.astimezone(timezone.utc):
        slot -= timedelta(days=7 if weekly else 1)
    return slot


def due_reports(service, cid, *, now=None):
    now = _now(now)
    with service.db.transaction() as connection:
        company = service._company_row(connection, cid)
        connection.execute(
            "UPDATE company_reports SET delivery_status='uncertain',delivery_error='uncertain' "
            "WHERE company_id=? AND delivery_status='sending' AND claimed_at<?",
            (cid, now.timestamp() - 600),
        )
        if not company["telegram_token"]:
            connection.execute(
                "UPDATE company_reports SET delivery_status='cancelled',delivery_error='bot_changed' "
                "WHERE company_id=? AND delivery_status='pending'",
                (cid,),
            )
        local = now.astimezone(ZoneInfo(company["timezone"]))
        schedules = connection.execute(
            "SELECT * FROM report_schedules WHERE company_id=? AND enabled=1 ORDER BY time,id", (cid,),
        ).fetchall()
        result = []
        for schedule in schedules:
            slot = _latest_slot(schedule, local)
            if slot < datetime.fromisoformat(schedule["updated_at"]):
                continue
            key = f"schedule:{schedule['id']}:{slot.date()}"
            if _report_by_key(connection, cid, key):
                continue
            try:
                _require_actor(service, connection, cid, schedule["actor_id"], EDITOR_ROLES)
            except DomainError:
                continue
            error = None
            target = (None, None)
            if schedule["notify"]:
                try:
                    target = _target(service, connection, cid, schedule["recipient_id"], company)
                except DomainError:
                    error = "access_changed" if not _chat_id(_recipient(connection, cid, schedule["recipient_id"])) else "bot_changed"
            report = _report_data(connection, company, slot.date(), REPORT_DAYS[schedule["frequency"]], now)
            result.append(_save_report(connection, cid, key, schedule["actor_id"], report, schedule=schedule,
                                       target=target, error=error))
        return result


def _access_changed(connection, cid, report):
    if report["schedule_id"] is not None:
        schedule = connection.execute(
            "SELECT enabled,notify,version FROM report_schedules WHERE company_id=? AND id=?",
            (cid, report["schedule_id"]),
        ).fetchone()
        if (not schedule or not schedule["enabled"] or not schedule["notify"]
                or schedule["version"] != report["schedule_version"]):
            return "schedule_changed"
    author = connection.execute(
        "SELECT m.role FROM memberships m JOIN users u ON u.id=m.user_id "
        "WHERE m.company_id=? AND m.user_id=? AND m.active=1 AND u.is_admin=0",
        (cid, report["actor_id"]),
    ).fetchone()
    chat = _chat_id(_recipient(connection, cid, report["recipient_id"]))
    if not author or author["role"] not in EDITOR_ROLES or not chat or chat != report["telegram_chat_id"]:
        return "access_changed"
    if report["actor_id"] != report["recipient_id"] and author["role"] != "admin":
        return "access_changed"
    return None


def queue_report(service, cid, actor, report_id):
    with service.db.transaction() as connection:
        member = _require_actor(service, connection, cid, actor, EDITOR_ROLES)
        row = _fetch_report(connection, cid, report_id)
        if row["actor_id"] != member["user_id"] and member["role"] != "admin":
            raise DomainError("Действие доступно автору отчёта и администратору компании.", 403)
        status = row["delivery_status"]
        if status in {"pending", "sending", "sent"}:
            return _public_report(_report_by_key(connection, cid, row["run_key"]))
        if status not in {"ready", "failed"} or (status == "ready" and row["schedule_id"] is not None):
            raise DomainError("Этот отчёт нельзя поставить в очередь отправки.", 409)
        company = service._company_row(connection, cid)
        if status == "ready":
            if row["actor_id"] != member["user_id"]:
                raise DomainError("Первую отправку запускает автор отчёта.", 403)
            recipient = member["user_id"]
            chat, bot = _target(service, connection, cid, recipient, company)
        else:
            recipient, chat, bot = row["recipient_id"], row["telegram_chat_id"], row["bot_fingerprint"]
            problem = _access_changed(connection, cid, row)
            if not problem and bot != _bot_fingerprint(service, company):
                problem = "bot_changed"
            if problem:
                raise DomainError(DELIVERY_ERRORS[problem], 409)
        connection.execute(
            "UPDATE company_reports SET recipient_id=?,telegram_chat_id=?,bot_fingerprint=?,delivery_status='pending',"
            "delivery_error=NULL,attempts=0,next_attempt=0,claimed_at=NULL WHERE company_id=? AND id=?",
            (recipient, chat, bot, cid, report_id),
        )
        return _public_report(_report_by_key(connection, cid, row["run_key"]))


def _claim_delivery(service, cid, fingerprint, now):
    with service.db.transaction() as connection:
        company = service._company_row(connection, cid)
        if _bot_fingerprint(service, company) != fingerprint:
            return None
        rows = connection.execute(
            "SELECT * FROM company_reports WHERE company_id=? AND delivery_status='pending' "
            "AND next_attempt<=? ORDER BY id LIMIT 20",
            (cid, now),
        ).fetchall()
        for row in rows:
            problem = _access_changed(connection, cid, row)
            if not problem and row["bot_fingerprint"] != fingerprint:
                problem = "bot_changed"
            if problem:
                connection.execute(
                    "UPDATE company_reports SET delivery_status='cancelled',delivery_error=? WHERE id=?",
                    (problem, row["id"]),
                )
                continue
            connection.execute(
                "UPDATE company_reports SET delivery_status='sending',attempts=attempts+1,claimed_at=? WHERE id=?",
                (now, row["id"]),
            )
            return {**dict(row), "attempts": row["attempts"] + 1}
        return None


def _finish_delivery(service, cid, report, status, error=None, retry_at=0):
    with service.db.transaction() as connection:
        connection.execute(
            "UPDATE company_reports SET delivery_status=?,delivery_error=?,next_attempt=?,delivered_at=? "
            "WHERE company_id=? AND id=? AND delivery_status='sending'",
            (status, error, retry_at, _timestamp(_now()) if status == "sent" else None, cid, report["id"]),
        )
        if error == "rate_limited":
            connection.execute(
                "UPDATE company_reports SET next_attempt=max(next_attempt,?) "
                "WHERE company_id=? AND delivery_status='pending' AND bot_fingerprint=?",
                (retry_at, cid, report["bot_fingerprint"]),
            )


async def deliver_reports(service, cid, bot_id, send_callable, *, token):
    if not token or token.partition(":")[0] != str(bot_id):
        return
    fingerprint = _fingerprint(token)
    for _ in range(10):
        report = _claim_delivery(service, cid, fingerprint, _now().timestamp())
        if not report:
            return
        try:
            sent = await send_callable(int(report["telegram_chat_id"]), report["text"])
        except asyncio.CancelledError:
            _finish_delivery(service, cid, report, "uncertain", "uncertain")
            raise
        except TelegramError as error:
            if error.uncertain:
                _finish_delivery(service, cid, report, "uncertain", "uncertain")
            elif error.status_code == 429:
                retry_at = _now().timestamp() + max(5, error.retry_after or 15)
                _finish_delivery(service, cid, report, "pending", "rate_limited", retry_at)
                return
            elif error.status_code is None and report["attempts"] < 3:
                retry_at = _now().timestamp() + 15 * report["attempts"]
                _finish_delivery(service, cid, report, "pending", "connection", retry_at)
            else:
                reason = "connection" if error.status_code is None else "rejected"
                _finish_delivery(service, cid, report, "failed", reason)
        except Exception:
            _finish_delivery(service, cid, report, "uncertain", "uncertain")
        else:
            if isinstance(sent, dict) and type(sent.get("message_id")) is int and sent["message_id"] > 0:
                _finish_delivery(service, cid, report, "sent")
            else:
                _finish_delivery(service, cid, report, "uncertain", "uncertain")


def _overview(service, cid, actor):
    with service.db.read() as connection:
        member = _require_actor(service, connection, cid, actor, MEMBER_ROLES)
        company = service._company_row(connection, cid)
        writable = member["role"] in EDITOR_ROLES
        recipients = []
        if writable:
            recipients = [dict(row) for row in connection.execute(
                "SELECT u.id,u.name FROM memberships m JOIN users u ON u.id=m.user_id "
                "WHERE m.company_id=? AND (m.user_id=? OR ?='admin') "
                "AND m.active=1 AND u.is_admin=0 AND m.telegram_id IS NOT NULL ORDER BY u.name,u.id",
                (cid, member["user_id"], member["role"]),
            )]
        reports = []
        for row in connection.execute(
            f"SELECT {PUBLIC_REPORT},actor_id FROM company_reports WHERE company_id=? ORDER BY id DESC LIMIT 30", (cid,),
        ):
            report = _public_report(row)
            owner = report.pop("actor_id") == member["user_id"]
            status = report["delivery_status"]
            report["can_send"] = writable and (
                (status == "ready" and report["schedule_id"] is None and owner)
                or (status == "failed" and (owner or member["role"] == "admin"))
            )
            reports.append(report)
        return {
            "actor_id": member["user_id"], "telegram_linked": bool(_chat_id(_recipient(connection, cid, member["user_id"]))),
            "telegram_configured": bool(company["telegram_token"]), "recipients": recipients,
            "schedules": _schedule_list(connection, cid, member) if writable else [],
            "reports": reports,
        }


def register_report_routes(api, service, member, editor):
    path = "/companies/{cid}/reports"

    @api.get(path)
    def report_overview(cid: int, user=Depends(member)):
        return _overview(service, cid, user)

    @api.get(path + "/preview")
    def report_preview(cid: int, days: int = Query(7), user=Depends(member)):
        return preview_report(service, cid, user, days)

    @api.post(path, status_code=201)
    def report_create(cid: int, payload: ReportPeriod, user=Depends(editor)):
        return generate_report(service, cid, user, payload.days)

    @api.post(path + "/{report_id}/send", status_code=202)
    def report_send(cid: int, report_id: int, user=Depends(editor)):
        return queue_report(service, cid, user, report_id)

    @api.post(path + "/schedules", status_code=201)
    def report_schedule_create(cid: int, payload: ScheduleCreate, user=Depends(editor)):
        return create_report_schedule(service, cid, user, payload)

    @api.put(path + "/schedules/{schedule_id}")
    def report_schedule_update(cid: int, schedule_id: int, payload: ScheduleUpdate, user=Depends(editor)):
        return update_report_schedule(service, cid, user, schedule_id, payload)

    @api.post(path + "/schedules/{schedule_id}/disable")
    def report_schedule_disable(cid: int, schedule_id: int, version: int = Query(..., gt=0), user=Depends(editor)):
        return disable_report_schedule(service, cid, user, schedule_id, expected_version=version)
