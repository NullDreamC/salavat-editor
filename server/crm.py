"""Read-only sync of attributed leads and won deal amounts."""

import asyncio
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from starlette.concurrency import run_in_threadpool

from . import attribution
from .db import reveal_secret, store_secret
from .domain import DomainError, audit_row, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_connections (
    company_id INTEGER NOT NULL REFERENCES companies(id),
    provider TEXT NOT NULL CHECK(provider IN ('bitrix24','amocrm')),
    version INTEGER NOT NULL,
    host TEXT NOT NULL DEFAULT '',
    credential BLOB,
    enabled INTEGER NOT NULL,
    interval_minutes INTEGER NOT NULL CHECK(interval_minutes IN (0,15,60)),
    configured_by INTEGER NOT NULL REFERENCES users(id),
    lead_field TEXT NOT NULL DEFAULT '',
    deal_field TEXT NOT NULL DEFAULT '',
    amo_field_id INTEGER,
    amo_rub INTEGER NOT NULL DEFAULT 0,
    cursor_lead INTEGER NOT NULL DEFAULT 0,
    cursor_deal INTEGER NOT NULL DEFAULT 0,
    window_start INTEGER NOT NULL DEFAULT 0,
    window_end INTEGER NOT NULL DEFAULT 0,
    lead_done INTEGER NOT NULL DEFAULT 0,
    deal_done INTEGER NOT NULL DEFAULT 0,
    next_due INTEGER NOT NULL DEFAULT 0,
    lease TEXT,
    lease_until INTEGER NOT NULL DEFAULT 0,
    last_state TEXT,
    last_error TEXT,
    last_finished_at TEXT,
    last_counts TEXT,
    PRIMARY KEY(company_id,provider)
);
"""
Provider = Literal["bitrix24", "amocrm"]
PROVIDERS = ("bitrix24", "amocrm")
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST_PATTERNS = {
    "bitrix24": re.compile(rf"{LABEL}\.bitrix24\.(?:ru|com)", re.ASCII),
    "amocrm": re.compile(rf"{LABEL}\.amocrm\.ru", re.ASCII),
}
BITRIX_PATH = re.compile(r"/rest/([1-9][0-9]{0,18})/([A-Za-z0-9_-]{8,128})/?", re.ASCII)
CUSTOM_FIELD = re.compile(r"UF_CRM_[A-Z0-9_]{1,70}", re.ASCII)
CAMPAIGN = re.compile(r"p_[A-Za-z0-9_-]{16}", re.ASCII)
AMO_TOKEN = re.compile(r"[A-Za-z0-9._~-]{16,8192}", re.ASCII)
MAX_BYTES = 2 * 1024 * 1024
BITRIX_PAGE_SIZE = 50
AMO_PAGE_SIZE = 50
PAGE_BUDGET = 3
LEASE_SECONDS = 120
SYNC_SECONDS = 75
NETWORK_SECONDS = 8
RETRY_SECONDS = 300
ADMIN = {"admin"}


class ConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=0, le=2**31 - 1)
    enabled: bool
    interval_minutes: Literal[0, 15, 60] = 0
    webhook_url: SecretStr | None = Field(default=None, max_length=300)
    domain: str | None = Field(default=None, max_length=100)
    token: SecretStr | None = Field(default=None, max_length=8192)
    remove_credential: bool = False
    lead_field: str = Field(default="", max_length=80)
    deal_field: str = Field(default="", max_length=80)
    amo_field_id: int | None = Field(default=None, gt=0, le=2**31 - 1)
    amo_rub: bool = False


class CrmError(DomainError):
    def __init__(self, message):
        super().__init__(message, 502)


class StaleSync(DomainError):
    def __init__(self):
        super().__init__("Настройки или права синхронизации CRM изменились.", 409)


def initialize(db):
    with db.read() as connection:
        connection.executescript(SCHEMA)


def _get(connection, cid, provider):
    return connection.execute(
        "SELECT * FROM crm_connections WHERE company_id=? AND provider=?", (cid, provider)
    ).fetchone()


def _public(provider, row):
    return {
        "provider": provider,
        "version": row["version"] if row else 0,
        "domain": row["host"] if row else "",
        "configured": bool(row and row["credential"]),
        "enabled": bool(row and row["enabled"]),
        "interval_minutes": row["interval_minutes"] if row else 0,
        "lead_field": row["lead_field"] if row else "",
        "deal_field": row["deal_field"] if row else "",
        "amo_field_id": row["amo_field_id"] if row else None,
        "amo_rub": bool(row and row["amo_rub"]),
        "running": bool(row and row["enabled"] and row["lease_until"] > time.time()),
        "last_state": row["last_state"] if row else None,
        "last_error": row["last_error"] if row else None,
        "last_finished_at": row["last_finished_at"] if row else None,
        "last_counts": json.loads(row["last_counts"]) if row and row["last_counts"] else None,
    }


def _webhook(value):
    error = DomainError("Укажите HTTPS-адрес входящего вебхука портала Bitrix24.", 422)
    if re.search(r"[^!-~]|[\\?#]", value):
        raise error
    try:
        parts = urlsplit(value)
    except ValueError:
        raise error from None
    host = parts.netloc.lower()
    match = BITRIX_PATH.fullmatch(parts.path)
    if parts.scheme != "https" or not HOST_PATTERNS["bitrix24"].fullmatch(host) or match is None:
        raise error
    return host, f"/rest/{match[1]}/{match[2]}"


def _secret(value):
    if os.name != "nt":
        raise DomainError("Для сохранения ключа CRM требуется Windows DPAPI.", 503)
    try:
        return store_secret(value)
    except (OSError, ValueError):
        raise DomainError("Не удалось защитить ключ CRM.", 503) from None


def configure(service, cid, actor, provider, payload):
    with service.db.transaction() as connection:
        service.require_actor(connection, cid, actor, ADMIN)
        row = _get(connection, cid, provider)
        existing = _public(provider, row)
        if payload.version != existing["version"]:
            raise DomainError("Настройки CRM уже изменились. Обновите страницу.", 409)
        host = existing["domain"]
        credential = row["credential"] if row else None
        if payload.remove_credential and (payload.enabled or payload.token or payload.webhook_url):
            raise DomainError("Для удаления ключа отключите подключение CRM.", 422)
        if provider == "bitrix24":
            if payload.domain is not None or payload.token or payload.amo_field_id or payload.amo_rub:
                raise DomainError("Проверьте настройки Bitrix24.", 422)
            if any(field and not CUSTOM_FIELD.fullmatch(field) for field in (payload.lead_field, payload.deal_field)):
                raise DomainError("Укажите код поля Bitrix24 вида UF_CRM_123.", 422)
            if payload.webhook_url:
                host, path = _webhook(payload.webhook_url.get_secret_value())
                credential = _secret(path)
        else:
            if payload.webhook_url or payload.lead_field or payload.deal_field:
                raise DomainError("Проверьте настройки amoCRM.", 422)
            if payload.domain is not None:
                host = payload.domain.lower()
                if not HOST_PATTERNS[provider].fullmatch(host):
                    raise DomainError("Укажите поддомен аккаунта вида компания.amocrm.ru.", 422)
                if host != existing["domain"] and not payload.token:
                    raise DomainError("Для другого домена нужен новый токен amoCRM.", 422)
            if payload.token:
                token = payload.token.get_secret_value()
                if not host or not AMO_TOKEN.fullmatch(token):
                    raise DomainError("Укажите домен и долгосрочный токен amoCRM.", 422)
                credential = _secret(token)
        if payload.remove_credential:
            host, credential = "", None
        if payload.enabled and not credential:
            raise DomainError("Сначала укажите ключ подключения CRM.", 422)
        reset = (
            host != existing["domain"] or payload.lead_field != existing["lead_field"]
            or payload.deal_field != existing["deal_field"] or payload.amo_field_id != existing["amo_field_id"]
            or payload.amo_rub != existing["amo_rub"] or bool(payload.token or payload.webhook_url)
            or payload.enabled != existing["enabled"]
        )
        connection.execute(
            """INSERT INTO crm_connections(company_id,provider,version,host,credential,enabled,interval_minutes,
                                           configured_by,lead_field,deal_field,amo_field_id,amo_rub)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(company_id,provider) DO UPDATE SET
                   version=excluded.version,host=excluded.host,credential=excluded.credential,
                   enabled=excluded.enabled,interval_minutes=excluded.interval_minutes,
                   configured_by=excluded.configured_by,lead_field=excluded.lead_field,deal_field=excluded.deal_field,
                   amo_field_id=excluded.amo_field_id,amo_rub=excluded.amo_rub,next_due=0""",
            (cid, provider, payload.version + 1, host, credential, payload.enabled, payload.interval_minutes,
             actor["id"], payload.lead_field, payload.deal_field, payload.amo_field_id, payload.amo_rub),
        )
        if reset:
            connection.execute(
                """UPDATE crm_connections SET cursor_lead=0,cursor_deal=0,window_start=0,window_end=0,
                          lead_done=0,deal_done=0,last_state=NULL,last_error=NULL,last_counts=NULL,last_finished_at=NULL
                   WHERE company_id=? AND provider=?""", (cid, provider)
            )
        audit_row(connection, cid, "crm.settings", f"Обновлены настройки CRM ({provider}).", actor)
        return _public(provider, _get(connection, cid, provider))


def _public_ip(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
    if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
        raise CrmError("Адрес CRM недоступен для публичного HTTPS-подключения.")
    return str(next((ip for ip in ips if ip.version == 4), ips[0]))


def _request_json(host, provider, path, *, credential, data=None, params=None):
    # Connect to the resolved public address directly. Verify TLS against the configured CRM host.
    if not HOST_PATTERNS[provider].fullmatch(host):
        raise CrmError("Недопустимый домен CRM.")
    if provider == "bitrix24":
        if BITRIX_PATH.fullmatch(credential) is None or path not in {"crm.lead.list", "crm.deal.list"}:
            raise CrmError("Проверьте ключ и разрешения Bitrix24.")
        endpoint = f"{credential}/{path}.json"
        method = "POST"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
    else:
        if not AMO_TOKEN.fullmatch(credential) or path != "/api/v4/leads":
            raise CrmError("Проверьте токен amoCRM.")
        endpoint = f"{path}?{urlencode(params)}"
        method = "GET"
        headers = {"Authorization": f"Bearer {credential}", "Accept": "application/hal+json"}
    context = ssl.create_default_context()
    client = http.client.HTTPSConnection(host, timeout=NETWORK_SECONDS, context=context)
    try:
        with socket.create_connection((_public_ip(host), 443), NETWORK_SECONDS) as raw:
            client.sock = context.wrap_socket(raw, server_hostname=host)
            client.request(method, endpoint, body=json.dumps(data) if data is not None else None, headers=headers)
            with client.getresponse() as response:
                if response.status in (401, 403):
                    raise CrmError("CRM отклонила доступ. Проверьте ключ и права на чтение.")
                if response.status == 429:
                    raise CrmError("CRM ограничила частоту запросов. Повторите позже.")
                if response.status == 204 and provider == "amocrm":
                    return {"_embedded": {"leads": []}}
                if response.status != 200:
                    raise CrmError("CRM отклонила запрос. Проверьте подключение и доступ к сделкам.")
                deadline = time.monotonic() + NETWORK_SECONDS
                body = bytearray()
                while chunk := response.read1(65536):
                    body.extend(chunk)
                    if len(body) > MAX_BYTES or time.monotonic() > deadline:
                        raise CrmError("CRM не вернула ограниченный ответ вовремя.")
                result = json.loads(body, parse_float=Decimal)
    except (OSError, ValueError, http.client.HTTPException, RecursionError):
        raise CrmError("Не удалось получить данные CRM. Проверьте домен и подключение.") from None
    finally:
        client.close()
    if not isinstance(result, dict):
        raise CrmError("CRM вернула неожиданный формат ответа.")
    if "error" in result or "error_description" in result:
        raise CrmError("CRM отклонила запрос. Проверьте ключ, доступ и коды полей.")
    return result


def _epoch(instant):
    return datetime.fromtimestamp(instant, timezone.utc).isoformat(timespec="seconds")


def _timestamp(value, numeric):
    try:
        if numeric and type(value) is int and value > 0:
            return _epoch(value)
        if not numeric and isinstance(value, str) and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})",
            value,
        ):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _id(value):
    if type(value) is int and 0 < value < 2**63:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value) and int(value) < 2**63:
        return int(value)
    return None


def _amo_code(row, field_id):
    fields = row.get("custom_fields_values")
    if not isinstance(fields, list):
        return None
    codes = set()
    for field in fields:
        if not isinstance(field, dict):
            continue
        selected = (
            field.get("field_id") == field_id if field_id is not None
            else field.get("field_code") == "UTM_CAMPAIGN"
        )
        values = field.get("values")
        if selected and isinstance(values, list):
            for item in values:
                value = item.get("value") if isinstance(item, dict) else None
                if isinstance(value, str) and CAMPAIGN.fullmatch(value):
                    codes.add(value)
    return next(iter(codes)) if len(codes) == 1 else None


def _kopecks(value):
    if type(value) is bool or not isinstance(value, (Decimal, int, str)):
        return None
    if isinstance(value, str) and not re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,2})?", value):
        return None
    try:
        amount = Decimal(value)
        if not amount.is_finite() or not 0 < amount <= Decimal(attribution.MAX_KOPECKS) / 100:
            return None
        kopecks = amount * 100
        return int(kopecks) if kopecks == kopecks.to_integral_value() else None
    except InvalidOperation:
        return None


def _make_event(config, entity, row_id, campaign, kind, timestamp, amount=None):
    reference = hashlib.sha256(f'{config["host"]}:{entity}:{row_id}'.encode("ascii")).hexdigest()
    return attribution.EventInput(
        event_id=f"{kind}_{reference}", reference=reference, campaign_code=campaign,
        kind=kind, occurred_at=timestamp, amount_kopecks=amount, currency="RUB" if amount is not None else None,
    )


def _events(config, entity, row, counts):
    amo = config["provider"] == "amocrm"
    row_id = _id(row.get("id" if amo else "ID"))
    if amo and row.get("is_deleted") is True:
        counts["skipped"] += 1
        return
    field = config["lead_field" if entity == "lead" else "deal_field"] or "UTM_CAMPAIGN"
    code = _amo_code(row, config["amo_field_id"]) if amo else row.get(field)
    if not isinstance(code, str) or not CAMPAIGN.fullmatch(code):
        counts["unmatched"] += 1
        return
    if entity == "lead" or amo:
        created_at = _timestamp(row.get("created_at" if amo else "DATE_CREATE"), amo)
        if created_at:
            yield _make_event(config, entity, row_id, code, "lead", created_at)
        else:
            counts["invalid_data"] += 1
    if entity == "lead":
        return
    stage = _id(row.get("status_id")) if amo else row.get("STAGE_SEMANTIC_ID")
    if (amo and stage is None) or (not amo and stage not in ("P", "S", "F")):
        counts["unknown_stage"] += 1
        return
    if stage != (142 if amo else "S"):
        return
    if (amo and not config["amo_rub"]) or (not amo and row.get("CURRENCY_ID") != "RUB"):
        counts["unsupported_currency"] += 1
        return
    amount = _kopecks(row.get("price" if amo else "OPPORTUNITY"))
    closed_at = _timestamp(row.get("closed_at" if amo else "MOVED_TIME"), amo)
    if not amount or not closed_at:
        counts["invalid_data"] += 1
        return
    yield _make_event(config, entity, row_id, code, "sale", closed_at, amount)


def _claim(service, cid, provider, actor):
    now = int(time.time())
    with service.db.transaction() as connection:
        if actor:
            service.require_actor(connection, cid, actor, ADMIN)
        row = _get(connection, cid, provider)
        if not actor and (not row or not row["interval_minutes"] or row["next_due"] > now):
            return None
        if not row or not row["enabled"] or not row["credential"]:
            if actor:
                raise DomainError("Подключите CRM перед синхронизацией.", 409)
            return None
        if row["lease_until"] > now:
            if actor:
                raise DomainError("Синхронизация CRM уже выполняется.", 409)
            return None
        if not connection.execute(
            "SELECT 1 FROM attribution_settings WHERE company_id=? AND enabled=1", (cid,)
        ).fetchone():
            if actor:
                raise DomainError("Сначала включите учёт заявок и продаж.", 409)
            return None
        service.require_actor(connection, cid, row["configured_by"], ADMIN)
        claim = dict(row)
        claim.update(lease=secrets.token_urlsafe(24), lease_until=now + LEASE_SECONDS)
        if provider == "bitrix24" and not claim["window_end"]:
            # A small overlap handles timestamp precision and delayed provider visibility.
            claim["window_end"] = max(claim["window_start"] + 1, now - 5)
        connection.execute(
            "UPDATE crm_connections SET lease=?,lease_until=?,window_end=? WHERE company_id=? AND provider=?",
            (claim["lease"], claim["lease_until"], claim["window_end"], cid, provider),
        )
        return claim


def _check(service, connection, claim, actor):
    cid, provider = claim["company_id"], claim["provider"]
    row = _get(connection, cid, provider)
    if (not row or not row["enabled"] or not row["credential"] or row["version"] != claim["version"]
            or row["lease"] != claim["lease"] or row["lease_until"] <= time.time()):
        raise StaleSync()
    service.require_actor(connection, cid, row["configured_by"], ADMIN)
    if actor:
        service.require_actor(connection, cid, actor, ADMIN)
    if not connection.execute(
        "SELECT 1 FROM attribution_settings WHERE company_id=? AND enabled=1", (cid,)
    ).fetchone():
        raise StaleSync()


def _bitrix_query(claim, entity):
    upper = _epoch(claim["window_end"])
    filters = {">ID": claim[f"cursor_{entity}"], "<=DATE_MODIFY": upper}
    if claim["window_start"]:
        filters[">=DATE_MODIFY"] = _epoch(claim["window_start"])
    fields = ["ID", "DATE_CREATE", claim[f"{entity}_field"] or "UTM_CAMPAIGN"]
    if entity == "deal":
        fields.extend(["STAGE_SEMANTIC_ID", "MOVED_TIME", "OPPORTUNITY", "CURRENCY_ID"])
    return {"select": fields, "order": {"ID": "ASC"}, "filter": filters, "start": -1}


async def _page(claim, credential, entity):
    provider = claim["provider"]
    if provider == "bitrix24":
        response = await asyncio.to_thread(
            _request_json, claim["host"], provider, f"crm.{entity}.list", credential=credential,
            data=_bitrix_query(claim, entity),
        )
        rows = response.get("result")
        page_size = BITRIX_PAGE_SIZE
    else:
        page = claim["cursor_deal"] + 1
        response = await asyncio.to_thread(
            _request_json, claim["host"], provider, "/api/v4/leads", credential=credential,
            params={"limit": AMO_PAGE_SIZE, "page": page, "order[id]": "asc"},
        )
        embedded = response.get("_embedded")
        rows = embedded.get("leads") if isinstance(embedded, dict) else None
        page_size = AMO_PAGE_SIZE
        if "_page" in response and response["_page"] != page:
            raise CrmError("CRM вернула другую страницу. Продолжить синхронизацию не удалось.")
    if not isinstance(rows, list) or len(rows) > page_size or any(not isinstance(row, dict) for row in rows):
        raise CrmError("CRM вернула неожиданный формат списка.")
    ids = [_id(row.get("ID" if provider == "bitrix24" else "id")) for row in rows]
    if any(value is None for value in ids) or any(a >= b for a, b in zip(ids, ids[1:])):
        raise CrmError("CRM вернула записи без устойчивого порядка.")
    if provider == "bitrix24":
        cursor = claim[f"cursor_{entity}"]
        if ids and ids[0] <= cursor:
            raise CrmError("Bitrix24 вернул уже обработанную страницу.")
        return rows, ids[-1] if ids else cursor, len(rows) == page_size
    links = response.get("_links", {})
    if not isinstance(links, dict) or (links.get("next") and not rows):
        raise CrmError("amoCRM вернула некорректное продолжение списка.")
    # Provider links signal pagination only. Requests always use the configured host and a fixed API path.
    return rows, page, bool(links.get("next")) or len(rows) == page_size


def _checkpoint(service, claim, entity, cursor, more, actor):
    claim[f"cursor_{entity}"] = cursor
    claim[f"{entity}_done"] = int(not more)
    with service.db.transaction() as connection:
        _check(service, connection, claim, actor)
        connection.execute(
            """UPDATE crm_connections SET cursor_lead=?,cursor_deal=?,lead_done=?,deal_done=?
               WHERE company_id=? AND provider=?""",
            (claim["cursor_lead"], claim["cursor_deal"], claim["lead_done"], claim["deal_done"],
             claim["company_id"], claim["provider"]),
        )


def _finish(service, claim, actor, state, counts, error):
    cid, provider = claim["company_id"], claim["provider"]
    with service.db.transaction() as connection:
        try:
            _check(service, connection, claim, actor)
        except DomainError:
            connection.execute(
                "UPDATE crm_connections SET lease=NULL,lease_until=0 WHERE company_id=? AND provider=? AND lease=?",
                (cid, provider, claim["lease"]),
            )
            return False
        if state == "completed":
            connection.execute(
                """UPDATE crm_connections SET cursor_lead=0,cursor_deal=0,lead_done=0,deal_done=0,
                          window_start=?,window_end=0 WHERE company_id=? AND provider=?""",
                (max(0, claim["window_end"] - 120), cid, provider),
            )
        delay = RETRY_SECONDS if error else claim["interval_minutes"] * 60
        connection.execute(
            """UPDATE crm_connections SET lease=NULL,lease_until=0,next_due=?,last_state=?,last_error=?,
                      last_finished_at=?,last_counts=? WHERE company_id=? AND provider=?""",
            (int(time.time()) + delay, state, error, utc_now(), json.dumps(counts), cid, provider),
        )
        return True


def _run_active(service, claim, actor):
    with service.db.read() as connection:
        _check(service, connection, claim, actor)


def _record_page(service, claim, entity, rows, counts, actor):
    def check(connection):
        _check(service, connection, claim, actor)

    for row in rows:
        counts["records"] += 1
        for event in _events(claim, entity, row, counts):
            try:
                result = attribution.record_crm_event(
                    service, claim["company_id"], event, claim["provider"], access_check=check,
                )
            except attribution.UnattributableCRMEvent:
                counts["unmatched"] += 1
            else:
                counts[event.kind if result["recorded"] else "duplicates"] += 1


async def sync(service, cid, provider, actor=None):
    claim = _claim(service, cid, provider, actor)
    if not claim:
        return None
    counts = dict.fromkeys(("records", "lead", "sale", "duplicates", "unmatched", "unsupported_currency",
                            "unknown_stage", "invalid_data", "skipped"), 0)
    state = "failed"
    error = "Синхронизация CRM прервана. Повторите позже."
    try:
        try:
            credential = reveal_secret(claim["credential"])
        except (OSError, ValueError):
            raise CrmError("Не удалось открыть ключ CRM. Введите его заново.") from None
        entities = ("lead", "deal") if provider == "bitrix24" else ("deal",)
        async with asyncio.timeout(SYNC_SECONDS):
            for entity in entities:
                for _ in range(PAGE_BUDGET):
                    if claim[f"{entity}_done"]:
                        break
                    _run_active(service, claim, actor)
                    rows, cursor, more = await _page(claim, credential, entity)
                    await asyncio.to_thread(_record_page, service, claim, entity, rows, counts, actor)
                    _checkpoint(service, claim, entity, cursor, more, actor)
                    if more:
                        await asyncio.sleep(0.55 if provider == "bitrix24" else 0.2)
        state = "completed" if all(claim[f"{entity}_done"] for entity in entities) else "partial"
        error = None
    except CrmError as failure:
        error = failure.detail
    except TimeoutError:
        error = "CRM не ответила вовремя. Синхронизация продолжится с сохранённой страницы."
    finally:
        finished = _finish(service, claim, actor, state, counts, error)
    if not finished:
        raise StaleSync()
    with service.db.read() as connection:
        return _public(provider, _get(connection, cid, provider))


async def sync_due(service, cid):
    async def due(provider):
        try:
            await sync(service, cid, provider)
        except DomainError:
            # Revocation of the scheduling admin or a concurrent settings edit stops this run.
            pass

    await asyncio.gather(*(due(provider) for provider in PROVIDERS))


def register_crm_routes(api, service, admin):
    prefix = "/companies/{cid}/crm"

    @api.get(prefix)
    def settings(cid: int, user=Depends(admin)):
        with service.db.read() as connection:
            service.require_actor(connection, cid, user, ADMIN)
            return [_public(provider, _get(connection, cid, provider)) for provider in PROVIDERS]

    @api.put(prefix + "/{provider}")
    async def save(cid: int, provider: Provider, request: Request, user=Depends(admin)):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 12_288:
                raise DomainError("Слишком большой запрос настроек CRM.", 413)
        try:
            payload = ConfigInput.model_validate_json(bytes(body))
        except ValidationError:
            raise DomainError("Проверьте поля настроек CRM.", 422) from None
        return await run_in_threadpool(configure, service, cid, user, provider, payload)

    @api.post(prefix + "/{provider}/sync")
    async def run(cid: int, provider: Provider, user=Depends(admin)):
        return await sync(service, cid, provider, user)
