"""Read counters and receive comments for the company's configured Telegram channel."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.util import find_spec
import os
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from . import integrations
from .analytics import COUNT_FIELDS, MEMBER_ROLES, MetricInput, write_observation
from .domain import DomainError, audit_row, utc_now
from .inbox import Intake, _insert, _publication_format


INTERVAL = timedelta(minutes=15)
VERIFICATION_TTL = timedelta(minutes=20)
CHANNEL_ID = re.compile(r"-[0-9]{13}")
ADMIN_STATUSES = {"administrator", "creator"}


class ObservationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    analytics_enabled: bool = Field(strict=True)
    comments_enabled: bool = Field(strict=True)


def initialize_telegram_observation(service):
    with service.db.transaction() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS telegram_observation (
            company_id INTEGER PRIMARY KEY REFERENCES companies(id),
            analytics_enabled INTEGER NOT NULL DEFAULT 0,
            comments_enabled INTEGER NOT NULL DEFAULT 0,
            managed_by INTEGER NOT NULL REFERENCES users(id),
            target TEXT NOT NULL,
            bot_hash TEXT,
            bot_id INTEGER,
            channel_id TEXT,
            discussion_chat_id TEXT,
            checked_at TEXT,
            last_attempt TEXT,
            attempt_id TEXT,
            last_reaction_at TEXT,
            last_stats_sync TEXT,
            last_comment_at TEXT,
            analytics_status TEXT NOT NULL DEFAULT 'pending',
            comments_status TEXT NOT NULL DEFAULT 'pending',
            last_error TEXT
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS telegram_discussion_roots (
            company_id INTEGER NOT NULL REFERENCES companies(id),
            discussion_chat_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            channel_id TEXT NOT NULL,
            channel_message_id INTEGER NOT NULL,
            channel_message_date INTEGER,
            PRIMARY KEY(company_id, discussion_chat_id, message_id)
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS telegram_comment_events (
            company_id INTEGER NOT NULL REFERENCES companies(id),
            discussion_chat_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            post_id INTEGER NOT NULL REFERENCES posts(id),
            channel_id TEXT NOT NULL,
            channel_message_date INTEGER,
            inbox_item_id INTEGER REFERENCES inbox_items(id),
            changed_at INTEGER NOT NULL,
            update_id INTEGER NOT NULL,
            PRIMARY KEY(company_id, discussion_chat_id, message_id)
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS telegram_reaction_events (
            company_id INTEGER NOT NULL REFERENCES companies(id),
            channel_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            changed_at INTEGER NOT NULL,
            update_id INTEGER NOT NULL,
            PRIMARY KEY(company_id, channel_id, message_id)
        )""")


def _time(now=None):
    return (now or datetime.now(timezone.utc)).isoformat(timespec="microseconds")


def _event_time(value):
    if type(value) is int and 0 < value < 4_102_444_800:
        return datetime.fromtimestamp(value, timezone.utc)
    return None


def _positive_id(value):
    return type(value) is int and value > 0


def _bot_hash(token):
    return hashlib.sha256(token.encode()).hexdigest() if token else None


def _channel_id(chat):
    value = str(chat.get("id") or "")
    if CHANNEL_ID.fullmatch(value) and 1 <= _peer_id(value) <= 997_852_516_352:
        return value
    return None


def _peer_id(chat_id):
    return -int(chat_id) - 1_000_000_000_000


def _settings(connection, cid):
    return connection.execute("SELECT * FROM telegram_observation WHERE company_id=?", (cid,)).fetchone()


def _current_manager(connection, settings):
    return connection.execute("""SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id AND u.is_admin=0
        WHERE m.company_id=? AND m.user_id=? AND m.active=1 AND m.role='admin'""",
        (settings["company_id"], settings["managed_by"])).fetchone() is not None


def _configured_identity(company, settings, token):
    return company["channel_id"] == settings["target"] and settings["bot_hash"] == _bot_hash(token)


def _connection_is_current(service, connection, company, settings, token):
    return (_configured_identity(company, settings, token) and _current_manager(connection, settings)
            and service._credentials(company, {"telegram_token"})["telegram_token"] == token)


def observation_status(service, cid, actor):
    with service.db.read() as connection:
        service.require_actor(connection, cid, actor, MEMBER_ROLES)
        company = service._company_row(connection, cid)
        row = _settings(connection, cid)
        result = {key: row[key] if row else None for key in (
            "channel_id", "discussion_chat_id", "last_stats_sync", "last_reaction_at", "last_comment_at", "last_error",
        )}
        for kind in ("analytics", "comments"):
            enabled = bool(row and row[f"{kind}_enabled"])
            status = row[f"{kind}_status"] if enabled else "disabled"
            result[f"{kind}_enabled"] = enabled
            result[f"{kind}_status"] = status
        if row and any(result[f"{kind}_enabled"] for kind in ("analytics", "comments")):
            token = service._credentials(company, {"telegram_token"})["telegram_token"]
            manager_active = _current_manager(connection, row)
            configuration_matches = _configured_identity(company, row, token)
            if not configuration_matches or not manager_active:
                for kind in ("analytics", "comments"):
                    if result[f"{kind}_enabled"]:
                        result[f"{kind}_status"] = "pending"
                result["last_error"] = "Пересохраните настройки мониторинга после изменения подключения Telegram."
            if not manager_active:
                result["last_error"] = "Попросите действующего администратора сохранить настройки мониторинга."
            elif configuration_matches and row["checked_at"] and (
                    datetime.fromisoformat(row["checked_at"]) <= datetime.now(timezone.utc) - VERIFICATION_TTL):
                for kind in ("analytics", "comments"):
                    if result[f"{kind}_status"] in {"active", "reactions_only"}:
                        result[f"{kind}_status"] = "pending"
        return result


async def change_observation_settings(service, cid, payload, actor):
    with service.db.transaction() as connection:
        service.require_actor(connection, cid, actor, {"admin"})
        company = service._company_row(connection, cid)
        token = service._credentials(company, {"telegram_token"})["telegram_token"]
        connection.execute("""INSERT INTO telegram_observation
            (company_id,analytics_enabled,comments_enabled,managed_by,target,bot_hash)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(company_id) DO UPDATE SET analytics_enabled=excluded.analytics_enabled,
                comments_enabled=excluded.comments_enabled,managed_by=excluded.managed_by,target=excluded.target,
                bot_hash=excluded.bot_hash,last_attempt=NULL,attempt_id=NULL,
                channel_id=NULL,checked_at=NULL,analytics_status='pending',comments_status='pending',last_error=NULL""",
            (cid, payload.analytics_enabled, payload.comments_enabled, actor["id"], company["channel_id"], _bot_hash(token)))
        audit_row(connection, cid, "telegram.observation_settings",
                  "Изменены настройки сбора метрик и комментариев Telegram.", actor)
    await sync_telegram_observation(service, cid, actor=actor)
    return observation_status(service, cid, actor)


def _claim(service, cid, actor, now):
    with service.db.transaction() as connection:
        if actor is not None:
            service.require_actor(connection, cid, actor, {"admin"})
        settings = _settings(connection, cid)
        if (not settings or not (settings["analytics_enabled"] or settings["comments_enabled"])
                or not _current_manager(connection, settings)):
            return None
        company = service._company_row(connection, cid)
        token = service._credentials(company, {"telegram_token"})["telegram_token"]
        if not _configured_identity(company, settings, token):
            return None
        interval = timedelta(seconds=30) if actor else INTERVAL
        if settings["last_attempt"] and datetime.fromisoformat(settings["last_attempt"]) > now - interval:
            return None
        attempt_id = uuid4().hex
        connection.execute("UPDATE telegram_observation SET last_attempt=?,attempt_id=? WHERE company_id=?",
                           (_time(now), attempt_id, cid))
        return dict(_settings(connection, cid)), token


async def _verify_channel(token, target, *, comments_enabled):
    result = {"channel_id": None, "discussion_chat_id": None, "bot_id": None,
              "channel_admin": False, "group_admin": False, "errors": []}
    if not token or not target:
        result["errors"].append("Укажите канал и подключите бота Telegram.")
        return result
    try:
        identity = await integrations.telegram_call(token, "getMe")
        chat = await integrations.telegram_call(token, "getChat", {"chat_id": target})
    except integrations.IntegrationError:
        result["errors"].append("Не удалось проверить доступ бота к каналу Telegram.")
        return result
    if not identity.get("is_bot") or not _positive_id(identity.get("id")) or chat.get("type") != "channel":
        result["errors"].append("Для мониторинга нужен канал Telegram и действующий бот.")
        return result
    channel_id = _channel_id(chat)
    if channel_id is None:
        result["errors"].append("Telegram не вернул идентификатор канала.")
        return result
    bot_id = identity["id"]
    group_id = _channel_id({"id": chat.get("linked_chat_id")})
    result.update(channel_id=channel_id, discussion_chat_id=group_id, bot_id=bot_id)
    try:
        membership = await integrations.telegram_call(token, "getChatMember", {"chat_id": channel_id, "user_id": bot_id})
        result["channel_admin"] = membership.get("status") in ADMIN_STATUSES
    except integrations.IntegrationError:
        pass
    if not result["channel_admin"]:
        result["errors"].append("Назначьте бота администратором канала Telegram.")
    if comments_enabled:
        if group_id is None:
            result["errors"].append("Привяжите группу обсуждений к каналу Telegram.")
        else:
            try:
                group = await integrations.telegram_call(token, "getChat", {"chat_id": group_id})
                member = await integrations.telegram_call(token, "getChatMember", {"chat_id": group_id, "user_id": bot_id})
                result["group_admin"] = (_channel_id(group) == group_id and group.get("type") == "supergroup"
                                         and str(group.get("linked_chat_id")) == channel_id
                                         and member.get("status") in ADMIN_STATUSES)
            except integrations.IntegrationError:
                pass
            if not result["group_admin"]:
                result["errors"].append("Добавьте бота администратором связанной группы обсуждений.")
    return result


def _api_credentials():
    raw_id = os.environ.get("SALAVAT_TELEGRAM_APP_ID", "")
    api_hash = os.environ.get("SALAVAT_TELEGRAM_APP_HASH", "")
    if re.fullmatch(r"[0-9]{1,10}", raw_id) and 0 < int(raw_id) < 2**31 and re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
        return int(raw_id), api_hash
    return None


def _mtproto_count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _total_reactions(counts):
    if counts is None or any(_mtproto_count(count) is None for count in counts):
        return None
    return _mtproto_count(sum(counts))


def _message_counters(message):
    reactions = getattr(getattr(message, "reactions", None), "results", None)
    replies = getattr(message, "replies", None)
    return {
        "views": _mtproto_count(getattr(message, "views", None)),
        "shares": _mtproto_count(getattr(message, "forwards", None)),
        "reactions": _total_reactions([entry.count for entry in reactions] if reactions is not None else None),
        "comments": _mtproto_count(replies.replies) if replies and replies.comments else None,
    }


async def fetch_mtproto_metrics(service, cid, token, bot_id, channel_id, message_ids):
    """Use an in-memory bot session and only read channel messages, without recording views."""
    from telethon import TelegramClient, functions, sessions, types

    api_id, api_hash = _api_credentials()
    sessions_by_company = service._telegram_metric_sessions
    session_key = _bot_hash(token), api_id, api_hash
    previous = sessions_by_company.get(cid)
    session = previous[1] if previous and previous[0] == session_key else sessions.MemorySession()
    # Persist only the in-memory authorization and reconnect without another bot sign-in next cycle.
    client = TelegramClient(session, api_id, api_hash, receive_updates=False, timeout=8,
                            request_retries=0, connection_retries=0, auto_reconnect=False, flood_sleep_threshold=0)
    try:
        await client.start(bot_token=token)
        identity = await client.get_me()
        if not identity or not identity.bot or identity.id != bot_id:
            sessions_by_company.pop(cid, None)
            raise DomainError("Идентификатор бота Telegram изменился.", 409)
        sessions_by_company[cid] = session_key, session
        peer_id = _peer_id(channel_id)
        response = await client(functions.channels.GetMessagesRequest(
            types.InputChannel(peer_id, 0), [types.InputMessageID(message_id) for message_id in message_ids]))
        return {message.id: (message.date, _message_counters(message)) for message in response.messages
                if isinstance(message, types.Message) and message.post and getattr(message.peer_id, "channel_id", None) == peer_id}
    finally:
        await client.disconnect()


async def close_telegram_observation(service):
    if hasattr(service, "_telegram_metric_sessions"):
        service._telegram_metric_sessions.clear()


def _publication_rows(connection, cid, target, channel_id, *, since=None, message_id=None):
    timestamp = _time(since) if since else None
    return connection.execute("""SELECT p.*, MAX(a.created_at) AS published_at FROM posts p
        LEFT JOIN activity a ON a.company_id=p.company_id AND a.action='post.published'
            AND a.detail='Опубликован пост №'||p.id||'.'
        WHERE p.company_id=? AND p.status='published' AND p.platform='telegram'
            AND (p.telegram_chat_id=? OR (p.telegram_chat_id IS NULL AND p.channel_id COLLATE NOCASE IN (?,?)))
            AND p.message_id IS NOT NULL
            AND (? IS NULL OR p.message_id=?)
        GROUP BY p.id HAVING (? IS NULL OR julianday(MAX(a.created_at))>=julianday(?))
        ORDER BY published_at DESC,p.id DESC LIMIT 100""",
        (cid, channel_id, target, channel_id, message_id, str(message_id), timestamp, timestamp)).fetchall()


def _has_identity(post, channel_id, event_date=None):
    canonical = post["telegram_chat_id"]
    if canonical:
        return str(canonical) == channel_id
    if _channel_id({"id": post["channel_id"]}):
        return post["channel_id"] == channel_id
    # A public username is mutable. For legacy rows require Telegram's original message date
    # against the previously recorded publication, unless the stored publication URL has an immutable ID.
    if post["publication_url"] == f"https://t.me/c/{_peer_id(channel_id)}/{post['message_id']}":
        return True
    if not event_date or not post["published_at"]:
        return False
    return abs(datetime.fromisoformat(post["published_at"]) - event_date) <= timedelta(minutes=2)


def _write_metric(service, connection, cid, company, post, now, source, origin, values, *, published_at=None):
    if not post["published_at"] and published_at is None:
        return 0
    zone = ZoneInfo(company["timezone"])
    published = (published_at or datetime.fromisoformat(post["published_at"])).astimezone(zone).date()
    observed = now.astimezone(zone).date()
    if published > observed or all(values.get(key) is None for key in COUNT_FIELDS):
        return 0
    sample = MetricInput(platform="telegram", publication_id=f"{post['channel_id']}:{post['message_id']}",
                         title=post["title"][:200] or "Публикация Telegram", post_id=post["id"],
                         format=_publication_format(post["approved_snapshot"]), published_on=published,
                         observed_on=observed, source=source, **values)
    return write_observation(service, connection, cid, sample, origin, _time(now))


def _store_connection_check(service, cid, claim, token, verification, checked_at):
    with service.db.transaction() as connection:
        row = _settings(connection, cid)
        company = service._company_row(connection, cid)
        if row["attempt_id"] != claim["attempt_id"] or not _connection_is_current(service, connection, company, row, token):
            return False
        connection.execute("""UPDATE telegram_observation SET
            channel_id=COALESCE(?,channel_id),discussion_chat_id=COALESCE(?,discussion_chat_id),
            bot_id=COALESCE(?,bot_id),checked_at=?,comments_status=?,analytics_status=?,
            last_error=? WHERE company_id=?""",
            (verification["channel_id"], verification["discussion_chat_id"], verification["bot_id"], _time(checked_at),
             "active" if verification["channel_admin"] and verification["group_admin"] else "disconnected",
             "reactions_only" if verification["channel_admin"] else "disconnected",
             " ".join(verification["errors"]) or None, cid))
        return True


async def sync_telegram_observation(service, cid, *, actor=None):
    now = datetime.now(timezone.utc)
    claimed = _claim(service, cid, actor, now)
    if claimed is None:
        return
    claim, token = claimed
    verification = await _verify_channel(token, claim["target"], comments_enabled=bool(claim["comments_enabled"]))
    if claim["channel_id"] and verification["channel_id"] and claim["channel_id"] != verification["channel_id"]:
        verification.update(channel_id=None, discussion_chat_id=None, channel_admin=False, group_admin=False,
                            errors=["Адрес Telegram теперь ведёт в другой канал. Проверьте и пересохраните настройки."])
    if not _store_connection_check(service, cid, claim, token, verification, datetime.now(timezone.utc)):
        return
    if not claim["analytics_enabled"] or not verification["channel_admin"]:
        return
    if not _api_credentials() or find_spec("telethon") is None:
        if hasattr(service, "_telegram_metric_sessions"):
            service._telegram_metric_sessions.pop(cid, None)
        return
    with service.db.read() as connection:
        channel_id = verification["channel_id"]
        posts = _publication_rows(connection, cid, claim["target"], channel_id, since=now - timedelta(days=30))
        message_ids = list({int(post["message_id"]) for post in posts if post["message_id"].isdecimal()})
    raw = {}
    if not hasattr(service, "_telegram_metric_sessions"):
        service._telegram_metric_sessions = {}
    error = None
    if message_ids:
        try:
            async with asyncio.timeout(25):
                raw = await fetch_mtproto_metrics(service, cid, token, verification["bot_id"], channel_id, message_ids)
            if not raw:
                error = "Telegram не вернул данные по недавним публикациям."
        except Exception:
            error = "Не удалось получить счётчики Telegram MTProto. Проверьте подключение и доступ бота к каналу."
    observed = datetime.now(timezone.utc)
    with service.db.transaction() as connection:
        row = _settings(connection, cid)
        company = service._company_row(connection, cid)
        if (row["attempt_id"] != claim["attempt_id"] or not row["analytics_enabled"]
                or row["analytics_status"] == "disconnected"
                or not _connection_is_current(service, connection, company, row, token)):
            return
        written = 0
        for post in _publication_rows(connection, cid, claim["target"], channel_id, since=now - timedelta(days=30)):
            snapshot = raw.get(int(post["message_id"])) if post["message_id"].isdecimal() else None
            if snapshot and _has_identity(post, channel_id, snapshot[0]):
                written += _write_metric(service, connection, cid, company, post, observed,
                                         f"Telegram MTProto · {channel_id}", "telegram_mtproto", snapshot[1],
                                         published_at=snapshot[0])
        if message_ids and not written and error is None:
            error = "Telegram не вернул счётчики для подтверждённых публикаций."
        status = "reactions_only" if error else "active"
        connection.execute("""UPDATE telegram_observation SET analytics_status=?,last_stats_sync=COALESCE(?,last_stats_sync),
            last_error=? WHERE company_id=?""",
            (status, _time(observed) if written else None, " ".join([*verification["errors"], error or ""]).strip() or None, cid))


def _origin(message):
    if message.get("is_automatic_forward") is not True:
        return None
    origin = message.get("forward_origin") or {}
    channel_id = _channel_id(origin.get("chat") or {})
    if origin.get("type") == "channel" and channel_id and _positive_id(origin.get("message_id")):
        return channel_id, origin["message_id"], _event_time(origin.get("date"))
    return None


def _matching_post(connection, cid, settings, channel_id, message_id, event_date=None):
    if channel_id != settings["channel_id"]:
        return None
    posts = _publication_rows(connection, cid, settings["target"], channel_id, message_id=message_id)
    matches = [post for post in posts if _has_identity(post, channel_id, event_date)]
    return matches[0] if len(matches) == 1 else None


def _store_reactions(service, connection, cid, company, settings, update, update_id):
    if not settings["analytics_enabled"] or settings["analytics_status"] not in {"active", "reactions_only"}:
        return
    chat = update.get("chat") or {}
    channel_id = _channel_id(chat)
    message_id, timestamp = update.get("message_id"), update.get("date")
    observed = _event_time(timestamp)
    counts = update.get("reactions")
    if (channel_id != settings["channel_id"] or chat.get("type") != "channel" or not _positive_id(message_id)
            or observed is None or not isinstance(counts, list) or not all(isinstance(entry, dict) for entry in counts)):
        return
    total = _total_reactions([entry.get("total_count") for entry in counts])
    if total is None:
        return
    post = _matching_post(connection, cid, settings, channel_id, message_id)
    if post is None:
        return
    accepted = connection.execute("""INSERT INTO telegram_reaction_events
        (company_id,channel_id,message_id,changed_at,update_id) VALUES(?,?,?,?,?)
        ON CONFLICT(company_id,channel_id,message_id) DO UPDATE SET changed_at=excluded.changed_at,update_id=excluded.update_id
        WHERE (excluded.changed_at,excluded.update_id)>(telegram_reaction_events.changed_at,telegram_reaction_events.update_id)""",
        (cid, channel_id, message_id, timestamp, update_id)).rowcount
    if not accepted:
        return
    written = _write_metric(service, connection, cid, company, post, observed,
                            f"Telegram Bot API · {channel_id}", "telegram_bot", {"reactions": total})
    if written:
        connection.execute("""UPDATE telegram_observation SET last_reaction_at=MAX(COALESCE(last_reaction_at,''),?)
            WHERE company_id=?""", (_time(observed), cid))


def _record_root(connection, cid, group_id, message, origin):
    if origin is None or not _positive_id(message.get("message_id")):
        return
    connection.execute("""INSERT OR IGNORE INTO telegram_discussion_roots
        (company_id,discussion_chat_id,message_id,channel_id,channel_message_id,channel_message_date) VALUES(?,?,?,?,?,?)""",
        (cid, group_id, message["message_id"], origin[0], origin[1], int(origin[2].timestamp()) if origin[2] else None))


def _comment_post(connection, cid, settings, group_id, message, reply):
    origin = _origin(reply)
    if origin:
        if origin[0] == settings["channel_id"] and str((reply.get("chat") or {}).get("id")) == group_id:
            _record_root(connection, cid, group_id, reply, origin)
            return _matching_post(connection, cid, settings, *origin)
        return None
    for parent_id in (message.get("message_thread_id"), reply.get("message_id")):
        if not _positive_id(parent_id):
            continue
        if parent_id == reply.get("message_id") and (reply.get("chat") or {}).get("id") not in {None, int(group_id)}:
            continue
        root = connection.execute("""SELECT channel_id,channel_message_id,channel_message_date FROM telegram_discussion_roots
            WHERE company_id=? AND discussion_chat_id=? AND message_id=?""", (cid, group_id, parent_id)).fetchone()
        if root:
            return _matching_post(connection, cid, settings, root["channel_id"], root["channel_message_id"],
                                  _event_time(root["channel_message_date"]))
        parent = connection.execute("""SELECT post_id,channel_id,channel_message_date
            FROM telegram_comment_events WHERE company_id=? AND discussion_chat_id=? AND message_id=?""",
            (cid, group_id, parent_id)).fetchone()
        match = _previous_comment_post(connection, cid, settings, parent)
        if match:
            return match
    return None


def _previous_comment_post(connection, cid, settings, previous):
    if previous is None or previous["channel_id"] != settings["channel_id"]:
        return None
    post = connection.execute("SELECT message_id FROM posts WHERE company_id=? AND id=?",
                              (cid, previous["post_id"])).fetchone()
    if not post or not (post["message_id"] or "").isdecimal():
        return None
    match = _matching_post(connection, cid, settings, previous["channel_id"], int(post["message_id"]),
                           _event_time(previous["channel_message_date"]))
    return match if match and match["id"] == previous["post_id"] else None


def _store_comment(connection, cid, settings, group_id, message, update_id):
    origin = _origin(message)
    if origin:
        if origin[0] == settings["channel_id"]:
            _record_root(connection, cid, group_id, message, origin)
        return
    if message.get("is_automatic_forward") or message.get("business_connection_id"):
        return
    sender = message.get("from") or {}
    if sender.get("is_bot") and not message.get("sender_chat"):
        return
    message_id = message.get("message_id")
    timestamp = message.get("edit_date") or message.get("date")
    original = _event_time(message.get("date"))
    if not _positive_id(message_id) or not _event_time(timestamp) or original is None:
        return
    reply = message.get("reply_to_message") or {}
    previous = connection.execute("""SELECT * FROM telegram_comment_events
        WHERE company_id=? AND discussion_chat_id=? AND message_id=?""", (cid, group_id, message_id)).fetchone()
    if previous and (timestamp, update_id) <= (previous["changed_at"], previous["update_id"]):
        return
    post = _comment_post(connection, cid, settings, group_id, message, reply)
    if post is None and not reply and not message.get("message_thread_id"):
        post = _previous_comment_post(connection, cid, settings, previous)
    if post is None or (previous and previous["post_id"] != post["id"]):
        return
    body = (message.get("text") or message.get("caption") or "").strip()
    if not body and any(message.get(key) for key in (
            "photo", "video", "video_note", "voice", "animation", "audio", "document", "sticker",
            "contact", "location", "venue", "poll")):
        body = "Вложение в комментарии Telegram."
    author = (message.get("sender_chat") or {}).get("title") or " ".join(
        value for value in (sender.get("first_name"), sender.get("last_name")) if isinstance(value, str)
    )
    publication_id = f"{post['channel_id']}:{post['message_id']}"
    reference = f"https://t.me/c/{_peer_id(group_id)}/{message_id}"
    item_id = previous["inbox_item_id"] if previous else None
    if body and item_id is None:
        payload = Intake(platform="telegram", kind="comment", body=body[:4000], author=author[:200], reference=reference)
        inserted = _insert(connection, cid, payload, _time(original), publication_id=publication_id,
                           external_id=str(message_id), source_account=f"telegram:{group_id}")
        if inserted.rowcount:
            item_id = inserted.lastrowid
    elif body and item_id is not None:
        connection.execute("""UPDATE inbox_items SET body=?,author=?,updated_at=?,version=version+1,
            status=CASE WHEN status IN ('draft','approved') THEN 'new' ELSE status END,
            reply=CASE WHEN status IN ('draft','approved') THEN '' ELSE reply END,
            questions=CASE WHEN status IN ('draft','approved') THEN '[]' ELSE questions END,
            evidence=CASE WHEN status IN ('draft','approved') THEN '[]' ELSE evidence END,
            approved_at=NULL,approved_by=NULL,generation_claim=NULL
            WHERE company_id=? AND id=? AND body<>?""", (body[:4000], author[:200], utc_now(), cid, item_id, body[:4000]))
    connection.execute("""INSERT INTO telegram_comment_events
        (company_id,discussion_chat_id,message_id,post_id,inbox_item_id,changed_at,update_id,channel_id,channel_message_date)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(company_id,discussion_chat_id,message_id) DO UPDATE SET
            inbox_item_id=excluded.inbox_item_id,changed_at=excluded.changed_at,update_id=excluded.update_id""",
        (cid, group_id, message_id, post["id"], item_id, timestamp, update_id, settings["channel_id"],
         int(datetime.fromisoformat(post["published_at"]).timestamp()) if post["published_at"] else None))
    if item_id is not None:
        connection.execute("""UPDATE telegram_observation SET last_comment_at=MAX(COALESCE(last_comment_at,''),?)
            WHERE company_id=?""", (_time(_event_time(timestamp)), cid))


def _revoke_access(connection, cid, settings, event):
    chat_id = _channel_id(event.get("chat") or {})
    member = event.get("new_chat_member") or {}
    if (member.get("user") or {}).get("id") != settings["bot_id"] or member.get("status") in ADMIN_STATUSES:
        return
    if chat_id == settings["channel_id"]:
        connection.execute("""UPDATE telegram_observation SET analytics_status='disconnected',comments_status='disconnected',
            last_attempt=NULL,attempt_id=NULL,last_error=? WHERE company_id=?""",
            ("У бота отозваны права администратора канала Telegram.", cid))
    elif chat_id == settings["discussion_chat_id"]:
        connection.execute("""UPDATE telegram_observation SET comments_status='disconnected',
            last_attempt=NULL,attempt_id=NULL,last_error=? WHERE company_id=?""",
            ("У бота отозваны права администратора группы обсуждений.", cid))


def _is_discussion_message(connection, cid, settings, group_id, message):
    if group_id is None:
        return False
    reply = message.get("reply_to_message") or {}
    origin = _origin(message) or _origin(reply)
    if origin and origin[0] == settings["channel_id"]:
        return True
    if group_id != settings["discussion_chat_id"]:
        return False
    if message.get("is_automatic_forward") or reply.get("is_automatic_forward"):
        return True
    known_ids = (message.get("message_id"), message.get("message_thread_id"), reply.get("message_id"))
    if connection.execute("""SELECT 1 FROM telegram_comment_events WHERE company_id=? AND discussion_chat_id=?
        AND message_id IN (?,?,?)
        UNION ALL
        SELECT 1 FROM telegram_discussion_roots WHERE company_id=? AND discussion_chat_id=? AND message_id IN (?,?,?)
        LIMIT 1""", (cid, group_id, *known_ids, cid, group_id, *known_ids)).fetchone():
        return True
    if message.get("is_topic_message"):
        return False
    if reply and (reply.get("from") or {}).get("id") == settings["bot_id"]:
        return False
    return bool(reply or message.get("message_thread_id"))


async def handle_telegram_observation_update(service, cid, token, update):
    if any(key in update for key in ("channel_post", "edited_channel_post")):
        return True
    message = update.get("message") or update.get("edited_message") or {}
    reaction = update.get("message_reaction_count")
    membership = update.get("my_chat_member")
    chat = message.get("chat") or {}
    group_id = _channel_id(chat) if chat.get("type") == "supergroup" else None
    if group_id is None and reaction is None and membership is None:
        return False
    update_id = update.get("update_id")
    if type(update_id) is not int:
        update_id = 0
    with service.db.transaction() as connection:
        settings = _settings(connection, cid)
        if settings is None:
            return reaction is not None or membership is not None
        discussion_message = _is_discussion_message(connection, cid, settings, group_id, message)
        consumed = discussion_message or reaction is not None or membership is not None
        if not consumed or not (settings["analytics_enabled"] or settings["comments_enabled"]):
            return consumed
        company = connection.execute("SELECT * FROM companies WHERE id=? AND archived_at IS NULL", (cid,)).fetchone()
        if company is None or not _connection_is_current(service, connection, company, settings, token):
            return True
        if membership is not None:
            _revoke_access(connection, cid, settings, membership)
            return True
        checked_at = settings["checked_at"]
        if not checked_at or datetime.fromisoformat(checked_at) <= datetime.now(timezone.utc) - VERIFICATION_TTL:
            return True
        if reaction is not None:
            _store_reactions(service, connection, cid, company, settings, reaction, update_id)
        elif (settings["comments_enabled"] and settings["comments_status"] == "active"
              and group_id == settings["discussion_chat_id"]):
            _store_comment(connection, cid, settings, group_id, message, update_id)
        return True


def register_telegram_observation_routes(api, service, member, editor):
    @api.get("/companies/{cid}/telegram-observation")
    def status(cid: int, user=Depends(member)):
        return observation_status(service, cid, user)

    @api.patch("/companies/{cid}/telegram-observation")
    async def configure(cid: int, payload: ObservationSettings, user=Depends(editor)):
        return await change_observation_settings(service, cid, payload, user)

    @api.post("/companies/{cid}/telegram-observation/sync")
    async def sync(cid: int, user=Depends(editor)):
        await sync_telegram_observation(service, cid, actor=user)
        return observation_status(service, cid, user)
