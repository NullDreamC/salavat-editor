import asyncio
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import hmac
import ipaddress
import re
import secrets
from time import monotonic
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from .domain import DomainError, EDITABLE, EDITOR_ROLES, audit_row, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS attribution_settings (
    company_id INTEGER PRIMARY KEY REFERENCES companies(id),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    target_url TEXT NOT NULL,
    key_hash TEXT
);
CREATE TABLE IF NOT EXISTS attribution_campaigns (
    company_id INTEGER NOT NULL REFERENCES companies(id),
    post_id INTEGER NOT NULL REFERENCES posts(id),
    code TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    cost_kopecks INTEGER CHECK(cost_kopecks >= 0),
    PRIMARY KEY(company_id,post_id)
);
CREATE TABLE IF NOT EXISTS attribution_events (
    company_id INTEGER NOT NULL,
    post_id INTEGER NOT NULL,
    event_hash TEXT NOT NULL,
    reference_hash TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('crm_webhook','bitrix24','amocrm')),
    kind TEXT NOT NULL CHECK(kind IN ('lead','sale','refund')),
    amount_kopecks INTEGER,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    FOREIGN KEY(company_id,post_id) REFERENCES attribution_campaigns(company_id,post_id),
    PRIMARY KEY(company_id,event_hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS attribution_single_lead_or_order
    ON attribution_events(company_id,kind,reference_hash) WHERE kind IN ('lead','sale');
CREATE INDEX IF NOT EXISTS attribution_events_date ON attribution_events(company_id,occurred_at,post_id);
CREATE INDEX IF NOT EXISTS attribution_refunds ON attribution_events(company_id,reference_hash,kind);
"""
WEBHOOK_PATH = re.compile(r"/api/companies/[1-9][0-9]{0,18}/attribution/events")
BEARER = re.compile(r"Bearer (att_[A-Za-z0-9_-]{43})", re.IGNORECASE | re.ASCII)
MAX_BODY = 4096
MAX_KOPECKS = 10_000_000_000
STAT_ROLES = EDITOR_ROLES | {"approver"}
CAMPAIGN_STATUSES = EDITABLE | {"published"}
UTM_FIELDS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "utm_id"}
SENSITIVE_QUERY = {"access_token", "api_key", "apikey", "auth", "email", "password", "phone", "refresh_token", "secret", "token"}
EventSource = Literal["crm_webhook", "bitrix24", "amocrm"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingsInput(Input):
    enabled: Annotated[bool, Field(strict=True)]
    target_url: str = Field(max_length=1500)

    @field_validator("target_url")
    @classmethod
    def validate_url(cls, value):
        return target_url(value) if value else ""

    @model_validator(mode="after")
    def require_target(self):
        if self.enabled and not self.target_url:
            raise ValueError("Требуется ссылка на страницу заявки")
        return self


class CampaignInput(Input):
    post_id: Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class SpendInput(Input):
    cost_kopecks: Annotated[int, Field(strict=True, ge=0, le=MAX_KOPECKS)] | None


OpaqueId = Annotated[str, Field(min_length=1, max_length=96, pattern=r"^[a-zA-Z0-9_-]+$")]


class EventInput(Input):
    event_id: OpaqueId
    campaign_code: str = Field(pattern=r"^p_[a-zA-Z0-9_-]{16}$")
    kind: Literal["lead", "sale", "refund"]
    reference: OpaqueId
    occurred_at: AwareDatetime
    amount_kopecks: Annotated[int, Field(strict=True, gt=0, le=MAX_KOPECKS)] | None = None
    currency: Literal["RUB"] | None = None

    @field_validator("occurred_at", mode="before")
    @classmethod
    def validate_timestamp(cls, value):
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
        ):
            raise ValueError("Требуется дата со временем и часовым поясом")
        return value

    @model_validator(mode="after")
    def validate_amount(self):
        if self.kind == "lead" and (self.amount_kopecks is not None or self.currency is not None):
            raise ValueError("У заявки нет суммы")
        if self.kind != "lead" and (self.amount_kopecks is None or self.currency != "RUB"):
            raise ValueError("Укажите сумму в копейках и валюту RUB")
        return self


def initialize_attribution(service):
    with service.db.read() as connection:
        connection.executescript(SCHEMA)


class UnattributableCRMEvent(DomainError):
    pass


def is_attribution_webhook(request):
    return request.method == "POST" and WEBHOOK_PATH.fullmatch(request.url.path) is not None


def target_url(value):
    error = ValueError("Укажите публичную HTTPS-ссылку без учётных данных")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value) or "\\" in value:
        raise error
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
        fields = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=40)
        fragment_query = parsed.fragment.split("?", 1)[-1]
        fields.extend(parse_qsl(fragment_query, keep_blank_values=True, max_num_fields=40))
    except ValueError:
        raise error from None
    if parsed.scheme != "https" or not host or port not in (None, 443) or "@" in parsed.netloc or "%" in parsed.netloc:
        raise error
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", host):
            raise error from None
        if host.endswith((".local", ".localhost", ".internal")):
            raise error from None
    else:
        if not address.is_global or address.is_multicast:
            raise error
    if any(key.lower() in SENSITIVE_QUERY or any(ord(ch) < 32 or ord(ch) == 127 for ch in key + val) for key, val in fields):
        raise error
    netloc = f"[{host}]" if ":" in host else host
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, parsed.fragment))


def _campaign_url(url, code):
    parts = urlsplit(url)
    params = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True) if key.lower() not in UTM_FIELDS]
    params.extend((("utm_source", "editorial"), ("utm_medium", "social"), ("utm_campaign", code)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def _settings(connection, cid):
    return connection.execute(
        "SELECT enabled,target_url,key_hash FROM attribution_settings WHERE company_id=?", (cid,)
    ).fetchone()


def _public_settings(row):
    return {"enabled": bool(row and row["enabled"]), "target_url": row["target_url"] if row else "",
            "key_configured": bool(row and row["key_hash"])}


def _require_enabled(settings):
    if not settings or not settings["enabled"]:
        raise DomainError("Учёт заявок и продаж отключён.", 409)


def _campaign(connection, cid, post_id):
    return connection.execute(
        "SELECT c.post_id,c.code,c.url,c.created_at,c.cost_kopecks,p.title,p.platform,p.status "
        "FROM attribution_campaigns c JOIN posts p ON p.id=c.post_id AND p.company_id=c.company_id "
        "WHERE c.company_id=? AND c.post_id=?", (cid, post_id)
    ).fetchone()


def _event_time(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _period(company, start, end):
    zone = ZoneInfo(company["timezone"])
    today = datetime.now(zone).date()
    end = end or today
    if not date(2000, 1, 1) <= end <= today:
        raise DomainError("Проверьте даты отчёта.", 422)
    start = start or end - timedelta(days=29)
    if not date(2000, 1, 1) <= start <= end or (end - start).days > 365:
        raise DomainError("Выберите период отчёта не более 366 дней.", 422)
    first = _event_time(datetime.combine(start, time.min, zone))
    last = _event_time(datetime.combine(end + timedelta(days=1), time.min, zone))
    return start, end, first, last


def _report(service, cid, actor, start, end, source):
    with service.db.read() as connection:
        connection.execute("BEGIN")
        service.require_actor(connection, cid, actor, STAT_ROLES)
        role = connection.execute("SELECT role FROM memberships WHERE company_id=? AND user_id=?", (cid, actor["id"])).fetchone()[0]
        start, end, first, last = _period(service._company_row(connection, cid), start, end)
        settings = _public_settings(_settings(connection, cid))
        rows = connection.execute(
            """WITH period AS (
                SELECT post_id, count(*) AS events,
                       count(CASE WHEN kind='lead' THEN 1 END) AS leads,
                       count(CASE WHEN kind='sale' THEN 1 END) AS sales,
                       count(CASE WHEN kind='refund' THEN 1 END) AS refunds,
                       sum(CASE WHEN kind='sale' THEN amount_kopecks WHEN kind='refund' THEN -amount_kopecks END) AS net,
                       group_concat(DISTINCT source) AS sources,
                       max(occurred_at) AS last_occurred_at,
                       max(received_at) AS last_received_at
                FROM attribution_events WHERE company_id=? AND occurred_at>=? AND occurred_at<?
                    AND (? IS NULL OR source=?) GROUP BY post_id
            ), lifetime AS (
                SELECT post_id,
                       sum(CASE WHEN kind='sale' THEN amount_kopecks WHEN kind='refund' THEN -amount_kopecks END) AS net
                FROM attribution_events WHERE company_id=? AND (? IS NULL OR source=?) GROUP BY post_id
            )
            SELECT c.post_id,c.code,c.url,c.created_at,c.cost_kopecks,p.title,p.platform,
                   coalesce(period.events,0) AS events,coalesce(period.leads,0) AS leads,
                   coalesce(period.sales,0) AS sales,coalesce(period.refunds,0) AS refunds,
                   period.net AS net_kopecks,period.last_occurred_at,period.last_received_at,period.sources,
                   CASE WHEN c.cost_kopecks>0 THEN 1.0*lifetime.net/c.cost_kopecks END AS revenue_cost_ratio
            FROM attribution_campaigns c JOIN posts p ON p.id=c.post_id AND p.company_id=c.company_id
            LEFT JOIN period ON period.post_id=c.post_id LEFT JOIN lifetime ON lifetime.post_id=c.post_id
            WHERE c.company_id=? AND p.status='published' ORDER BY c.post_id DESC""",
            (cid, first, last, source, source, cid, source, source, cid),
        ).fetchall()
        posts, prepared = [], []
        if settings["enabled"] and role in EDITOR_ROLES:
            for row in connection.execute(
                "SELECT p.id,p.title,p.platform,p.status,c.url FROM posts p LEFT JOIN attribution_campaigns c "
                "ON c.company_id=p.company_id AND c.post_id=p.id WHERE p.company_id=? ORDER BY p.id DESC", (cid,),
            ):
                if row["status"] in CAMPAIGN_STATUSES and row["url"] is None:
                    posts.append(dict(row))
                elif row["status"] != "published" and row["url"]:
                    prepared.append(dict(row))
    items = [dict(row) for row in rows]
    for row in items:
        row["sources"] = sorted(row["sources"].split(",")) if row["sources"] else []
    net_values = [row["net_kopecks"] for row in items if row["net_kopecks"] is not None]
    summary = {field: sum(row[field] for row in items) for field in ("events", "leads", "sales", "refunds")}
    summary["net_kopecks"] = sum(net_values) if net_values else None
    return {"enabled": settings["enabled"], "start": start, "end": end,
            "items": items, "posts": posts, "prepared": prepared, "summary": summary, "source": source}


def _authenticate(connection, cid, key_hash):
    row = connection.execute(
        "SELECT s.enabled,s.key_hash FROM attribution_settings s JOIN companies c ON c.id=s.company_id "
        "WHERE s.company_id=? AND c.archived_at IS NULL", (cid,),
    ).fetchone()
    expected = row["key_hash"] if row and row["key_hash"] else "0" * 64
    if not hmac.compare_digest(expected, key_hash) or not row or not row["key_hash"]:
        raise DomainError("Ключ приёма событий недействителен.", 401)
    _require_enabled(row)


def _identifier_hash(cid, source, value):
    return hashlib.sha256(f"{cid}:{source}:{value}".encode("ascii")).hexdigest()


def _record_event(service, cid, event, source, access_check):
    now = datetime.now(timezone.utc)
    if not 2000 <= event.occurred_at.year < 9999 or event.occurred_at > now + timedelta(minutes=5):
        raise UnattributableCRMEvent("Проверьте дату события.", 422)
    occurred = _event_time(event.occurred_at)
    event_hash = _identifier_hash(cid, source, event.event_id)
    reference_hash = _identifier_hash(cid, source, event.reference)
    with service.db.transaction() as connection:
        access_check(connection)
        _require_enabled(_settings(connection, cid))
        existing = connection.execute(
            "SELECT post_id,reference_hash,kind,amount_kopecks,occurred_at FROM attribution_events "
            "WHERE company_id=? AND event_hash=?", (cid, event_hash),
        ).fetchone()
        if existing and source != "crm_webhook":
            return {"recorded": False}
        campaign = connection.execute(
            "SELECT c.post_id,c.created_at FROM attribution_campaigns c JOIN posts p "
            "ON p.id=c.post_id AND p.company_id=c.company_id "
            "WHERE c.company_id=? AND c.code=? AND p.status='published'", (cid, event.campaign_code),
        ).fetchone()
        if campaign is None:
            raise UnattributableCRMEvent("Ссылка публикации не найдена.", 404)
        post_id = campaign["post_id"]
        values = (post_id, reference_hash, event.kind, event.amount_kopecks, occurred)
        if existing:
            if tuple(existing) != values:
                raise DomainError("Событие с таким идентификатором уже записано с другими данными.", 409)
            return {"recorded": False}
        publication = connection.execute(
            "SELECT created_at FROM activity WHERE company_id=? AND "
            "((action='post.published' AND detail=?) OR (action='post.reconciled_published' AND detail LIKE ?)) "
            "ORDER BY id DESC LIMIT 1", (cid, f"Опубликован пост №{post_id}.", f"Публикация поста №{post_id} подтверждена вручную.%"),
        ).fetchone()
        if publication is None:
            raise UnattributableCRMEvent("Для приёма событий требуется запись о времени публикации.", 409)
        first_event_at = max(datetime.fromisoformat(campaign["created_at"]), datetime.fromisoformat(publication["created_at"]))
        if event.occurred_at < first_event_at:
            raise UnattributableCRMEvent("Событие датировано раньше публикации или создания ссылки.", 422)
        same_kind = connection.execute(
            "SELECT 1 FROM attribution_events WHERE company_id=? AND kind=? AND reference_hash=?",
            (cid, event.kind, reference_hash),
        ).fetchone() if event.kind in {"sale", "lead"} else None
        if same_kind:
            raise DomainError("Заявка или продажа с таким идентификатором уже записана.", 409)
        if event.kind == "refund":
            sale = connection.execute(
                "SELECT post_id,amount_kopecks,occurred_at FROM attribution_events "
                "WHERE company_id=? AND kind='sale' AND reference_hash=?", (cid, reference_hash),
            ).fetchone()
            if not sale or sale["post_id"] != post_id or sale["occurred_at"] > occurred:
                raise DomainError("Для возврата нужна ранее записанная продажа по той же ссылке.", 409)
            previous = connection.execute(
                "SELECT coalesce(sum(amount_kopecks),0) FROM attribution_events "
                "WHERE company_id=? AND kind='refund' AND reference_hash=?", (cid, reference_hash),
            ).fetchone()[0]
            if previous + event.amount_kopecks > sale["amount_kopecks"]:
                raise DomainError("Сумма возвратов превышает сумму продажи.", 409)
        connection.execute(
            "INSERT INTO attribution_events(company_id,event_hash,post_id,reference_hash,kind,amount_kopecks,occurred_at,received_at,source) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (cid, event_hash, *values, _event_time(now), source),
        )
        return {"recorded": True}


def record_crm_event(service, cid, event: EventInput, provider, *, access_check):
    if provider not in {"bitrix24", "amocrm"}:
        raise ValueError("Unknown CRM event source")
    return _record_event(service, cid, event, provider, access_check)


class _RateLimit:
    def __init__(self):
        self.window = -1
        self.requests = {}

    def count(self, key, limit):
        window = int(monotonic() // 60)
        if window != self.window:
            self.requests.clear()
            self.window = window
        count = self.requests.get(key, 0)
        if count >= limit or (not count and len(self.requests) >= 4096):
            raise DomainError("Слишком много событий. Повторите запрос через минуту.", 429)
        self.requests[key] = count + 1


async def _read_event(request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise DomainError("Ожидается JSON.", 415)
    length = request.headers.get("content-length")
    if length is not None and (not re.fullmatch(r"[0-9]{1,16}", length) or int(length) > MAX_BODY):
        raise DomainError("Событие превышает допустимый размер.", 413)
    body = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_BODY:
                    raise DomainError("Событие превышает допустимый размер.", 413)
                body.extend(chunk)
    except TimeoutError:
        raise DomainError("Время получения события истекло.", 408) from None
    try:
        return EventInput.model_validate_json(body)
    except ValidationError:
        raise DomainError("Некорректные поля события.", 422) from None


def register_attribution_routes(api, service, member, editor, admin):
    path = "/companies/{cid}/attribution"
    rate = _RateLimit()

    @api.get(path)
    def report(cid: int, start: date | None = None, end: date | None = None,
               source: EventSource | None = None, user=Depends(member)):
        return _report(service, cid, user, start, end, source)

    @api.get(path + "/settings")
    def get_settings(cid: int, request: Request, user=Depends(admin)):
        with service.db.read() as connection:
            service.require_actor(connection, cid, user, {"admin"})
            origin = request.state.webapp_url or str(request.base_url).rstrip("/")
            return {**_public_settings(_settings(connection, cid)), "webhook_url": f"{origin}/api/companies/{cid}/attribution/events"}

    @api.patch(path + "/settings")
    def save_settings(cid: int, payload: SettingsInput, user=Depends(admin)):
        with service.db.transaction() as connection:
            service.require_actor(connection, cid, user, {"admin"})
            connection.execute(
                "INSERT INTO attribution_settings(company_id,enabled,target_url) VALUES(?,?,?) "
                "ON CONFLICT(company_id) DO UPDATE SET enabled=excluded.enabled,target_url=excluded.target_url",
                (cid, payload.enabled, payload.target_url),
            )
            audit_row(connection, cid, "attribution.settings", "Обновлены настройки учёта заявок и продаж.", user)
            return _public_settings(_settings(connection, cid))

    @api.post(path + "/key")
    def rotate_key(cid: int, user=Depends(admin)):
        token = "att_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with service.db.transaction() as connection:
            service.require_actor(connection, cid, user, {"admin"})
            connection.execute(
                "INSERT INTO attribution_settings(company_id,enabled,target_url,key_hash) VALUES(?,0,'',?) "
                "ON CONFLICT(company_id) DO UPDATE SET key_hash=excluded.key_hash", (cid, digest),
            )
            audit_row(connection, cid, "attribution.key", "Выпущен новый ключ приёма событий.", user)
        return {"token": token}

    @api.post(path + "/campaigns", status_code=201)
    def create_campaign(cid: int, payload: CampaignInput, user=Depends(editor)):
        with service.db.transaction() as connection:
            service.require_actor(connection, cid, user, EDITOR_ROLES)
            settings = _settings(connection, cid)
            _require_enabled(settings)
            post = service._post_row(connection, cid, payload.post_id)
            if post["status"] not in CAMPAIGN_STATUSES:
                raise DomainError("Ссылку можно подготовить для черновика или опубликованного поста.", 409)
            existing = _campaign(connection, cid, post["id"])
            if existing:
                return dict(existing)
            code = "p_" + secrets.token_urlsafe(12)
            url = _campaign_url(settings["target_url"], code)
            connection.execute(
                "INSERT INTO attribution_campaigns(company_id,post_id,code,url,created_at) VALUES(?,?,?,?,?)",
                (cid, post["id"], code, url, utc_now()),
            )
            audit_row(connection, cid, "attribution.campaign", f"Создана ссылка для поста №{post['id']}.", user)
            return dict(_campaign(connection, cid, post["id"]))

    @api.patch(path + "/campaigns/{post_id}")
    def save_spend(cid: int, post_id: int, payload: SpendInput, user=Depends(editor)):
        with service.db.transaction() as connection:
            service.require_actor(connection, cid, user, EDITOR_ROLES)
            _require_enabled(_settings(connection, cid))
            campaign = _campaign(connection, cid, post_id)
            if not campaign or campaign["status"] != "published":
                raise DomainError("Ссылка публикации не найдена.", 404)
            connection.execute(
                "UPDATE attribution_campaigns SET cost_kopecks=? WHERE company_id=? AND post_id=?",
                (payload.cost_kopecks, cid, post_id),
            )
            audit_row(connection, cid, "attribution.spend", f"Обновлены расходы поста №{post_id}.", user)
            return dict(_campaign(connection, cid, post_id))

    @api.post(path + "/events")
    async def webhook(cid: Annotated[int, Field(gt=0, le=2**63 - 1)], request: Request):
        address = request.client.host if request.client else "unknown"
        rate.count(("peer", address), 180)
        match = BEARER.fullmatch(request.headers.get("Authorization", ""))
        if match is None:
            raise DomainError("Нужен ключ приёма событий.", 401)
        digest = hashlib.sha256(match[1].encode("ascii")).hexdigest()

        def check_access(connection):
            _authenticate(connection, cid, digest)

        def authenticate():
            with service.db.read() as connection:
                check_access(connection)

        await run_in_threadpool(authenticate)
        rate.count(("company", cid), 120)
        payload = await _read_event(request)
        return await run_in_threadpool(_record_event, service, cid, payload, "crm_webhook", check_access)
