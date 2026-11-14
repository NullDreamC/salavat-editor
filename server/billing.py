"""Company-local ruble limits over app-originated provider calls."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import json
import time
from typing import Literal

from fastapi import Depends
import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


SEARCH_MODEL = "yandex-web-sync"
OUTPUT_LIMITS = {"text": 4500, "image": 4500, "layout": 1000, "search": 0}
IMAGE_SIZE = "1024x1024"
IMAGE_QUALITY = "medium"
IMAGE_COMPLEXITY = "low"
_RESERVATION_TTL = 15 * 60
_YANDEX_CONTEXT_LIMITS = {"qwen3.6-35b-a3b": 262_144}
_SCOPE = ContextVar("billing_scope", default=None)

SCHEMA = """
CREATE TABLE IF NOT EXISTS billing_limits (
    company_id INTEGER PRIMARY KEY REFERENCES companies(id),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1))
);
CREATE TABLE IF NOT EXISTS billing_global_limit (
    id INTEGER PRIMARY KEY CHECK(id=1),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    monthly_limit INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS billing_opening_balances (
    scope_id INTEGER NOT NULL,
    month TEXT NOT NULL,
    amount_kopecks INTEGER NOT NULL,
    booked_at_ns INTEGER NOT NULL,
    legacy_usage_id INTEGER NOT NULL,
    PRIMARY KEY(scope_id,month)
);
CREATE TABLE IF NOT EXISTS billing_tariff_catalogs (
    id INTEGER PRIMARY KEY,
    version TEXT NOT NULL UNIQUE,
    tariffs TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS billing_charges (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    usage_id INTEGER UNIQUE REFERENCES usage(id),
    month TEXT NOT NULL,
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    model TEXT NOT NULL,
    tariff_version TEXT REFERENCES billing_tariff_catalogs(version),
    tariff TEXT,
    reserved_kopecks INTEGER NOT NULL,
    metered_kopecks INTEGER,
    bound_exceeded INTEGER NOT NULL DEFAULT 0 CHECK(bound_exceeded IN (0,1)),
    observed_tokens TEXT,
    expires_at INTEGER NOT NULL,
    started_at_ns INTEGER,
    status TEXT NOT NULL CHECK(status IN ('reserved','started','settled','released'))
);
CREATE INDEX IF NOT EXISTS billing_charges_company_month ON billing_charges(company_id,month);
CREATE INDEX IF NOT EXISTS billing_charges_month ON billing_charges(month);
"""


def migrate_schema(connection):
    charges = {column["name"] for column in connection.execute("PRAGMA table_info(billing_charges)")}
    if "bound_exceeded" not in charges:
        connection.execute("ALTER TABLE billing_charges ADD COLUMN bound_exceeded INTEGER NOT NULL DEFAULT 0")
    if "observed_tokens" not in charges:
        connection.execute("ALTER TABLE billing_charges ADD COLUMN observed_tokens TEXT")
    if "expires_at" not in charges:
        # Only unsubmitted reservations expire. Started charges stay reserved.
        connection.execute("ALTER TABLE billing_charges ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0")
    if "started_at_ns" not in charges:
        connection.execute("ALTER TABLE billing_charges ADD COLUMN started_at_ns INTEGER")
        connection.execute("UPDATE billing_charges SET started_at_ns=? WHERE status IN ('started','settled')", (time.time_ns(),))

    balances = {column["name"] for column in connection.execute("PRAGMA table_info(billing_opening_balances)")}
    if "booked_at_ns" not in balances:
        connection.execute("ALTER TABLE billing_opening_balances ADD COLUMN booked_at_ns INTEGER NOT NULL DEFAULT 0")
    if "legacy_usage_id" not in balances:
        connection.execute("ALTER TABLE billing_opening_balances ADD COLUMN legacy_usage_id INTEGER NOT NULL DEFAULT 0")
    if "covered_requests" in balances:
        # Keep the amount. An old count cannot establish which requests it covered.
        connection.execute("ALTER TABLE billing_opening_balances DROP COLUMN covered_requests")


class Tariff(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["openai", "yandex"]
    kind: Literal["text", "image", "layout", "search"]
    model: str = Field(min_length=1, max_length=300, pattern=r"^[A-Za-z0-9._:/-]+$")
    currency: Literal["RUB", "USD", "EUR"]
    rub_per_unit: Decimal = Field(gt=0, le=10_000, decimal_places=8)
    input_per_million: Decimal = Field(default=Decimal(0), ge=0, le=100_000, decimal_places=8)
    output_per_million: Decimal = Field(default=Decimal(0), ge=0, le=100_000, decimal_places=8)
    max_input_tokens: int = Field(default=0, ge=0, le=10_000_000)
    image_model: str | None = Field(default=None, min_length=1, max_length=300, pattern=r"^[A-Za-z0-9._:-]+$")
    image_bounds_source: HttpUrl | None = None
    image_input_per_million: Decimal = Field(default=Decimal(0), ge=0, le=100_000, decimal_places=8)
    image_output_per_million: Decimal = Field(default=Decimal(0), ge=0, le=100_000, decimal_places=8)
    max_image_input_tokens: int = Field(default=0, ge=0, le=10_000_000)
    max_image_output_tokens: int = Field(default=0, ge=0, le=10_000_000)
    per_request: Decimal = Field(default=Decimal(0), ge=0, le=100_000, decimal_places=8)

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.currency == "RUB" and self.rub_per_unit != 1:
            raise ValueError("Для тарифа в RUB курс должен быть равен 1.")
        if self.kind == "search":
            if self.provider != "yandex" or self.model != SEARCH_MODEL or not self.per_request:
                raise ValueError("Для поиска нужен тариф Yandex yandex-web-sync с ценой запроса.")
        elif not self.max_input_tokens or not (self.input_per_million or self.output_per_million or self.per_request):
            raise ValueError("Укажите верхнюю границу входных токенов модели и цену.")
        if self.kind == "image":
            if self.provider != "openai" or not all((self.image_model, self.max_image_input_tokens,
                    self.max_image_output_tokens, self.image_input_per_million, self.image_output_per_million)):
                raise ValueError("Укажите модель, цены и подтверждённые провайдером границы токенов изображения.")
            if self.image_bounds_source and self.image_bounds_source.scheme != "https":
                raise ValueError("Ссылка на подтверждение границ должна использовать HTTPS.")
        if self.kind == "layout" and self.provider != "openai":
            raise ValueError("Выбор логотипа использует OpenAI.")
        return self

    def cost(self, inputs=0, outputs=0, image_inputs=0, image_outputs=0):
        tokens = (inputs * self.input_per_million + outputs * self.output_per_million
                  + image_inputs * self.image_input_per_million + image_outputs * self.image_output_per_million)
        rubles = self.rub_per_unit * (self.per_request + tokens / Decimal(1_000_000))
        return int((rubles * 100).to_integral_value(rounding=ROUND_CEILING))

    def maximum(self):
        return self.cost(self.max_input_tokens, OUTPUT_LIMITS[self.kind], self.max_image_input_tokens,
                         self.max_image_output_tokens)


class TariffCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version: str = Field(min_length=1, max_length=100)
    expected_version: str | None = Field(default=None, max_length=100)
    tariffs: list[Tariff] = Field(max_length=200)

    @model_validator(mode="after")
    def unique_keys(self):
        keys = {(tariff.provider, tariff.kind, tariff.model) for tariff in self.tariffs}
        if len(keys) != len(self.tariffs):
            raise ValueError("Для провайдера, модели и типа вызова укажите один тариф.")
        if any(tariff.kind == "image" and not tariff.image_bounds_source for tariff in self.tariffs):
            raise ValueError("Укажите ссылку на подтверждение верхних границ генерации изображения провайдером.")
        return self


class BudgetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(strict=True)
    monthly_limit: int = Field(strict=True, ge=0, le=1_000_000_000)
    opening_kopecks: int | None = Field(default=None, strict=True, ge=0, le=100_000_000_000)
    expected_month: str | None = Field(default=None, pattern=r"^[0-9]{4}-[0-9]{2}$")

    @model_validator(mode="after")
    def positive_limit(self):
        if self.enabled and not self.monthly_limit:
            raise ValueError("Укажите положительный месячный лимит в рублях.")
        return self


def month_now():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _catalog(connection):
    row = connection.execute("SELECT version,tariffs FROM billing_tariff_catalogs ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return "", {}
    tariffs = (Tariff.model_validate(item) for item in json.loads(row["tariffs"]))
    return row["version"], {(t.provider, t.kind, t.model): t for t in tariffs}


def _active(connection, company_id):
    row = connection.execute("SELECT enabled FROM billing_limits WHERE company_id=?", (company_id,)).fetchone()
    return bool(row and row["enabled"])


def _global_budget(connection):
    row = connection.execute("SELECT enabled,monthly_limit FROM billing_global_limit WHERE id=1").fetchone()
    return (bool(row["enabled"]), row["monthly_limit"]) if row else (False, 40_000)


def _amounts(connection, company_id, month, *, pending_usage_id=None):
    where = "month=?" if company_id is None else "month=? AND company_id=?"
    fields = (month,) if company_id is None else (month, company_id)
    opening = connection.execute("SELECT * FROM billing_opening_balances WHERE scope_id=? AND month=?",
                                 (company_id or 0, month)).fetchone()
    booked_at = opening["booked_at_ns"] if opening else 0
    covered_legacy = opening["legacy_usage_id"] if opening else 0
    row = connection.execute("""SELECT
        COALESCE(SUM(metered_kopecks),0) AS metered_kopecks,
        COALESCE(SUM(CASE WHEN status='started' OR (status='reserved' AND expires_at>?)
            THEN reserved_kopecks ELSE 0 END),0) AS reserved_kopecks,
        COUNT(CASE WHEN tariff IS NULL AND status IN ('started','settled') THEN 1 END) AS unpriced_requests,
        COUNT(CASE WHEN tariff IS NULL AND status IN ('started','settled') AND started_at_ns<=? THEN 1 END) AS covered_requests
        FROM billing_charges WHERE """ + where, (int(time.time()), booked_at, *fields)).fetchone()
    usage_filter = " AND u.company_id=?" if company_id is not None else ""
    legacy, legacy_covered, last_legacy = connection.execute("""SELECT COUNT(*),
        COUNT(CASE WHEN u.id<=? THEN 1 END), COALESCE(MAX(u.id),0) FROM usage u WHERE u.month=? AND u.id<>?
        AND NOT EXISTS (SELECT 1 FROM billing_charges c WHERE c.usage_id=u.id)""" + usage_filter,
                                               (covered_legacy, month, pending_usage_id or 0, *fields[1:])).fetchone()
    return {**dict(row), "unpriced_requests": row["unpriced_requests"] + legacy,
            "opening_kopecks": opening["amount_kopecks"] if opening else None,
            "covered_requests": row["covered_requests"] + legacy_covered, "last_legacy_usage": last_legacy}


def _model_keys(company):
    from .integrations import _PHOTO_MODEL, _provider_model

    company = dict(company)
    provider = company["provider"]
    model = _provider_model(company)
    if provider == "yandex" and model.startswith("gpt://"):
        model = model.removeprefix("gpt://").partition("/")[2]
    return {
        "text": (provider, "text", model),
        "image": ("openai", "image", company.get("photo_model") or _PHOTO_MODEL),
        "layout": ("openai", "layout", company.get("photo_model") or _PHOTO_MODEL),
    }


def _available_tariff(tariffs, key):
    from .integrations import _PHOTO_TOOL_MODEL

    tariff = tariffs.get(key)
    if tariff and tariff.kind == "image" and (tariff.image_model != _PHOTO_TOOL_MODEL or not tariff.image_bounds_source):
        return None
    return tariff


def _verified_context(tariff):
    if tariff.provider != "yandex" or tariff.kind != "text":
        return True
    context = _YANDEX_CONTEXT_LIMITS.get(tariff.model)
    return context is not None and tariff.max_input_tokens >= context


def _require_room(connection, company_id, limit, month, amount, *, pending_usage_id):
    from .domain import DomainError

    totals = _amounts(connection, company_id, month, pending_usage_id=pending_usage_id)
    if totals["unpriced_requests"] > totals["covered_requests"]:
        raise DomainError("Укажите уже понесённые расходы за вызовы без тарифа в этом месяце.", 409)
    used = totals["metered_kopecks"] + totals["reserved_kopecks"] + (totals["opening_kopecks"] or 0)
    if used + amount > limit * 100:
        raise DomainError("Месячного рублёвого лимита не хватает на этот запрос.", 429)


def _check_budget(connection, company, month, amount, *, tariff, pending_usage_id=None):
    from .domain import DomainError

    company_active = _active(connection, company["id"])
    global_active, global_limit = _global_budget(connection)
    if (company_active or global_active) and not tariff:
        raise DomainError("Для запроса нужен подтверждённый рублёвый тариф.", 422)
    if (company_active or global_active) and not _verified_context(tariff):
        raise DomainError("Для защиты Yandex нужен тариф модели с подтверждённым размером контекста.", 422)
    if company_active:
        _require_room(connection, company["id"], company["monthly_limit"], month, amount, pending_usage_id=pending_usage_id)
    if global_active:
        _require_room(connection, None, global_limit, month, amount, pending_usage_id=pending_usage_id)


def _require_reviewed_tariff(connection, tariff, key):
    from .domain import DomainError

    rows = connection.execute("""SELECT DISTINCT observed_tokens FROM billing_charges
        WHERE provider=? AND kind=? AND model=? AND bound_exceeded=1""", key)
    bounds = (tariff.max_input_tokens, OUTPUT_LIMITS[tariff.kind], tariff.max_image_input_tokens,
              tariff.max_image_output_tokens)
    for row in rows:
        if row["observed_tokens"] is None or any(
            observed > bound for observed, bound in zip(json.loads(row["observed_tokens"]), bounds, strict=True)
        ):
            raise DomainError("Фактический расход превысил границы тарифа. Проверьте границы и сохраните новую версию.", 409)


def _reserve(connection, company, month, key, *, usage_id=None, require_tariff=False):
    from .domain import DomainError

    version, tariffs = _catalog(connection)
    tariff = _available_tariff(tariffs, key)
    active = _active(connection, company["id"]) or _global_budget(connection)[0]
    if not tariff and (active or require_tariff):
        raise DomainError(f"Для тарифа {key[0]} / {key[1]} / {key[2]} нужна подтверждённая цена и границы расхода.", 422)
    amount = tariff.maximum() if tariff else 0
    if tariff and active:
        _require_reviewed_tariff(connection, tariff, key)
    _check_budget(connection, company, month, amount, tariff=tariff, pending_usage_id=usage_id)
    return connection.execute("""INSERT INTO billing_charges
        (company_id,usage_id,month,provider,kind,model,tariff_version,tariff,reserved_kopecks,expires_at,status)
        VALUES(?,?,?,?,?,?,?,?,?,?,'reserved')""", (company["id"], usage_id, month, *key,
        version if tariff else None, tariff.model_dump_json() if tariff else None, amount,
        int(time.time()) + _RESERVATION_TTL)).lastrowid


def reserve_generation(connection, company, month, provider_company, usage_id, kind):
    return _reserve(connection, company, month, _model_keys(provider_company)[kind], usage_id=usage_id)


def reserve_search(connection, company, model=SEARCH_MODEL, *, require_tariff=False):
    return _reserve(connection, company, month_now(), ("yandex", "search", model), require_tariff=require_tariff)


def _guarded(connection, company_id):
    return _active(connection, company_id) or _global_budget(connection)[0]


def start_charge(db, charge_id, *, validate=None, preflight_performed=None):
    from .domain import DomainError, Service

    with db.transaction() as connection:
        charge = connection.execute("SELECT * FROM billing_charges WHERE id=?", (charge_id,)).fetchone()
        if not charge or charge["status"] != "reserved" or charge["expires_at"] <= int(time.time()):
            raise DomainError("У запроса нет действующего резерва расходов.", 409)
        company = Service._company_row(connection, charge["company_id"])
        tariff = Tariff.model_validate_json(charge["tariff"]) if charge["tariff"] else None
        month = month_now()
        guarded = _guarded(connection, company["id"])
        if preflight_performed is False and guarded:
            raise DomainError("Рублёвый лимит изменился перед запросом. Повторите действие.", 409)
        if guarded and tariff:
            version, _ = _catalog(connection)
            if charge["tariff_version"] != version:
                raise DomainError("Тариф изменился. Повторите действие.", 409)
            _require_reviewed_tariff(connection, tariff, (charge["provider"], charge["kind"], charge["model"]))
        if validate is not None:
            validate(charge, tariff)
        # An unguarded Responses payload can exceed any catalogue envelope.
        # Until complete usage arrives, it remains unpriced for opening reconciliation.
        if preflight_performed is False:
            connection.execute("""UPDATE billing_charges SET month=?,status='started',started_at_ns=?,
                tariff=NULL,reserved_kopecks=0 WHERE id=?""", (month, time.time_ns(), charge_id))
        else:
            connection.execute("UPDATE billing_charges SET month=?,status='started',started_at_ns=? WHERE id=?",
                               (month, time.time_ns(), charge_id))
        _check_budget(connection, company, month, 0, tariff=tariff)
        return tariff


def release_charge(db, charge_id):
    with db.transaction() as connection:
        connection.execute("UPDATE billing_charges SET status='released' WHERE id=? AND status='reserved'", (charge_id,))


def _settle(db, charge_id, amount, exceeded, observed=None, *, tariff=None):
    with db.transaction() as connection:
        connection.execute("""UPDATE billing_charges SET status='settled',metered_kopecks=?,bound_exceeded=?,observed_tokens=?,
            tariff=COALESCE(?,tariff) WHERE id=? AND status='started'""",
            (amount, exceeded, json.dumps(observed) if observed else None, tariff.model_dump_json() if tariff else None, charge_id))


def settle_search(db, charge_id):
    with db.transaction() as connection:
        connection.execute("""UPDATE billing_charges SET status='settled',
            metered_kopecks=CASE WHEN tariff IS NOT NULL THEN reserved_kopecks END
            WHERE id=? AND kind='search' AND status='started'""", (charge_id,))


@contextmanager
def billing_scope(db, **usage_ids):
    with db.read() as connection:
        charge_ids = {
            kind: connection.execute("SELECT id FROM billing_charges WHERE usage_id=? AND kind=?", (usage_id, kind)).fetchone()
            for kind, usage_id in usage_ids.items()
        }
    charge_ids = {kind: row["id"] for kind, row in charge_ids.items() if row is not None}
    token = _SCOPE.set((db, charge_ids))
    try:
        yield
    finally:
        _SCOPE.reset(token)
        for charge_id in charge_ids.values():
            release_charge(db, charge_id)


def _request_images(payload):
    from .domain import DomainError

    messages = payload.get("input", "")
    if isinstance(messages, str):
        return []
    images = []
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            continue
        for part in content:
            if part.get("type") == "input_image" and str(part.get("image_url", "")).startswith("data:image/"):
                images.append(part)
            elif part.get("type") != "input_text":
                raise DomainError("Этот тип вложения не поддерживается тарифом.", 422)
    return images


def _validate_request(charge, tariff, endpoint, payload, kind):
    from .domain import DomainError
    from .integrations import _OPENAI_RESPONSES, _YANDEX_RESPONSES

    provider = {_OPENAI_RESPONSES: "openai", _YANDEX_RESPONSES: "yandex"}.get(endpoint)
    model = payload.get("model", "")
    if provider == "yandex":
        model = model.removeprefix("gpt://").partition("/")[2]
    if (provider, kind, model) != (charge["provider"], charge["kind"], charge["model"]):
        raise DomainError("Модель запроса изменилась. Повторите действие.", 409)
    if tariff is None:
        return
    allowed = {"model", "instructions", "input", "text", "store", "max_output_tokens", "truncation"}
    if kind == "image":
        allowed.update(("tools", "tool_choice", "parallel_tool_calls", "max_tool_calls"))
        expected = {"type": "image_generation", "model": tariff.image_model, "output_format": "png",
                    "quality": IMAGE_QUALITY, "size": IMAGE_SIZE, "complexity": IMAGE_COMPLEXITY}
        tools = payload.get("tools", [])
        if (len(tools) != 1 or {k: v for k, v in tools[0].items() if k != "action"} != expected
                or tools[0].get("action") not in {"generate", "edit"}
                or payload.get("max_tool_calls") != 1 or payload.get("parallel_tool_calls") is not False
                or payload.get("tool_choice") != {"type": "image_generation"}):
            raise DomainError("Настройки генерации изображения превышают границы тарифа.", 422)
    limit = payload.get("max_output_tokens")
    if set(payload) - allowed or type(limit) is not int or not 16 <= limit <= OUTPUT_LIMITS[kind] or payload.get("store") is not False:
        raise DomainError("Настройки запроса превышают границы тарифа.", 422)


def _inspect_charge(db, charge_id):
    from .domain import DomainError

    with db.read() as connection:
        charge = connection.execute("SELECT * FROM billing_charges WHERE id=?", (charge_id,)).fetchone()
        if not charge or charge["status"] != "reserved" or charge["expires_at"] <= int(time.time()):
            raise DomainError("У запроса нет действующего резерва расходов.", 409)
        tariff = Tariff.model_validate_json(charge["tariff"]) if charge["tariff"] else None
        return charge, tariff, _guarded(connection, charge["company_id"])


async def _count_openai_input(endpoint, headers, payload):
    from .domain import DomainError
    from .network import http_options

    count_fields = {"model", "instructions", "input", "text", "tools", "tool_choice", "parallel_tool_calls", "truncation"}
    count_payload = {key: value for key, value in payload.items() if key in count_fields}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25, connect=10), trust_env=False, **http_options()) as client:
            response = await client.post(endpoint + "/input_tokens", headers=headers, json=count_payload)
        response.raise_for_status()
        data = response.json()
        count = data.get("input_tokens") if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError, UnicodeError):
        count = None
    if not _integer(count) or not count:
        raise DomainError("Провайдер не подтвердил объём входа. Запрос генерации не отправлен.", 502)
    return count


async def _check_input_bounds(tariff, endpoint, headers, payload):
    from .domain import DomainError

    images = _request_images(payload)
    if tariff.kind == "image" and len(images) > 6:
        raise DomainError("Материалы превышают границы тарифа генерации изображения.", 422)
    if tariff.provider == "yandex":
        if images:
            raise DomainError("Рублёвый тариф Yandex поддерживает только текстовый вход.", 422)
        # Reserve the complete provider context for guarded text calls. Unlike
        # the OpenAI counter, Yandex tokenization does not count a Responses payload.
        return
    count = await _count_openai_input(endpoint, headers, payload)
    if count > tariff.max_input_tokens:
        raise DomainError("Вход запроса превышает границы рублёвого тарифа. Уменьшите объём материалов.", 422)


def _integer(value):
    return type(value) is int and value >= 0


def _measured_cost(tariff, data):
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    inputs = usage.get("input_tokens", usage.get("prompt_tokens"))
    outputs = usage.get("output_tokens", usage.get("completion_tokens"))
    if not (_integer(inputs) and _integer(outputs)) or not inputs:
        return None
    tool_inputs = tool_outputs = 0
    if tariff.kind == "image":
        tool_usage = data.get("tool_usage")
        counted = tool_usage.get("image_gen") if isinstance(tool_usage, dict) else None
        if not isinstance(counted, dict):
            return None
        tool_inputs, tool_outputs = counted.get("input_tokens"), counted.get("output_tokens")
        if not (_integer(tool_inputs) and _integer(tool_outputs)) or not tool_outputs:
            return None
    exceeded = (inputs > tariff.max_input_tokens or outputs > OUTPUT_LIMITS[tariff.kind]
                or tool_inputs > tariff.max_image_input_tokens or tool_outputs > tariff.max_image_output_tokens)
    return tariff.cost(inputs, outputs, tool_inputs, tool_outputs), exceeded, (inputs, outputs, tool_inputs, tool_outputs)


def _runtime_payload(charge, tariff, endpoint, payload, kind):
    if tariff and kind == "image":
        tools = [{**tool, "size": IMAGE_SIZE, "complexity": IMAGE_COMPLEXITY} for tool in payload.get("tools", [])]
        payload.update(tools=tools, max_output_tokens=OUTPUT_LIMITS["image"])
    if tariff and tariff.provider == "yandex":
        payload["truncation"] = "disabled"
    _validate_request(charge, tariff, endpoint, payload, kind)


async def metered_generation(transport, endpoint, headers, payload, timeout, *, kind="text"):
    from .domain import DomainError

    scope = _SCOPE.get()
    if scope is None:
        return await transport(endpoint, headers, payload, timeout)
    db, charge_ids = scope
    charge_id = charge_ids.get(kind)
    if charge_id is None:
        raise DomainError("Для запроса не найден резерв расходов.", 409)
    payload = dict(payload)
    charge, tariff, guarded = _inspect_charge(db, charge_id)
    _runtime_payload(charge, tariff if guarded else None, endpoint, payload, kind)
    if guarded and tariff:
        await _check_input_bounds(tariff, endpoint, headers, payload)
    tariff = start_charge(db, charge_id, preflight_performed=guarded and tariff is not None)
    response = await transport(endpoint, headers, payload, timeout)
    if not tariff:
        _settle(db, charge_id, None, False)
    if tariff and response.is_success:
        try:
            measured = _measured_cost(tariff, response.json())
        except (ValueError, UnicodeError):
            measured = None
        if measured is not None:
            amount, exceeded, observed = measured
            _settle(db, charge_id, amount, exceeded and guarded, observed, tariff=tariff)
        elif not guarded:
            _settle(db, charge_id, None, False)
    return response


def _budget_summary(connection, company_id, enabled, limit):
    month = month_now()
    totals = _amounts(connection, company_id, month)
    totals.pop("last_legacy_usage")
    remaining = limit * 100 - totals["metered_kopecks"] - totals["reserved_kopecks"] - (totals["opening_kopecks"] or 0)
    return {"enabled": enabled, "month": month, "monthly_limit": limit,
            "remaining_kopecks": max(0, remaining) if enabled and totals["unpriced_requests"] <= totals["covered_requests"] else None,
            **totals}


def _limit_editable(service, connection, company, user):
    pooled = company["funding_mode"] != "own" or (company["id"] == 1 and service._has_salavat_dependents(connection))
    return bool(user["is_admin"] or not pooled)


def _summary(connection, company, editable):
    _, tariffs = _catalog(connection)
    keys = {**_model_keys(company), "search": ("yandex", "search", SEARCH_MODEL)}
    current = {kind: _available_tariff(tariffs, key) for kind, key in keys.items()}
    return {
        **_budget_summary(connection, company["id"], _active(connection, company["id"]), company["monthly_limit"]),
        "limit_editable": editable,
        "models": {kind: key[2] for kind, key in keys.items()},
        "missing_tariffs": [" / ".join(key) for kind, key in keys.items()
                            if current[kind] is None or not _verified_context(current[kind])],
        "quotes_kopecks": {kind: tariff.maximum() if tariff and _verified_context(tariff) else None for kind, tariff in current.items()},
    }


def _update_opening(connection, company_id, payload):
    from .domain import DomainError

    month = month_now()
    if payload.expected_month is not None and payload.expected_month != month:
        raise DomainError("Начался новый расчётный месяц. Загрузите текущие расходы.", 409)
    totals = _amounts(connection, company_id, month)
    if payload.opening_kopecks is not None:
        connection.execute("""INSERT INTO billing_opening_balances(scope_id,month,amount_kopecks,booked_at_ns,legacy_usage_id)
            VALUES(?,?,?,?,?) ON CONFLICT(scope_id,month) DO UPDATE SET amount_kopecks=excluded.amount_kopecks,
            booked_at_ns=excluded.booked_at_ns,legacy_usage_id=excluded.legacy_usage_id""",
            (company_id or 0, month, payload.opening_kopecks, time.time_ns(), totals["last_legacy_usage"]))
    elif payload.enabled and totals["unpriced_requests"] > totals["covered_requests"]:
        raise DomainError("Для включения лимита укажите расходы за неучтённые вызовы текущего месяца.", 409)


def register_billing_routes(api, service, company_manager, global_admin):
    from .domain import DomainError, audit_row

    @api.get("/companies/{cid}/billing")
    def billing_summary(cid: int, user=Depends(company_manager)):
        with service.db.read() as connection:
            service.require_actor(connection, cid, user, {"admin", "super_admin"})
            company = service._company_row(connection, cid)
            return _summary(connection, company, _limit_editable(service, connection, company, user))

    @api.put("/companies/{cid}/billing")
    def update_budget(cid: int, payload: BudgetSettings, user=Depends(company_manager)):
        with service.db.transaction() as connection:
            service.require_actor(connection, cid, user, {"admin", "super_admin"})
            company = service._company_row(connection, cid)
            if not _limit_editable(service, connection, company, user):
                raise DomainError("Рублёвый лимит общей оплаты меняет главный администратор.", 403)
            _update_opening(connection, cid, payload)
            connection.execute("UPDATE companies SET monthly_limit=? WHERE id=?", (payload.monthly_limit, cid))
            connection.execute("INSERT INTO billing_limits(company_id,enabled) VALUES(?,?) "
                               "ON CONFLICT(company_id) DO UPDATE SET enabled=excluded.enabled", (cid, payload.enabled))
            audit_row(connection, cid, "billing.limit_updated", "Рублёвый лимит расходов обновлён.", user)
            return _summary(connection, service._company_row(connection, cid), True)

    @api.get("/billing/settings")
    def global_summary(user=Depends(global_admin)):
        with service.db.read() as connection:
            return _budget_summary(connection, None, *_global_budget(connection))

    @api.put("/billing/settings")
    def update_global_budget(payload: BudgetSettings, user=Depends(global_admin)):
        with service.db.transaction() as connection:
            _update_opening(connection, None, payload)
            connection.execute("INSERT INTO billing_global_limit(id,enabled,monthly_limit) VALUES(1,?,?) "
                               "ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,monthly_limit=excluded.monthly_limit",
                               (payload.enabled, payload.monthly_limit))
            return _budget_summary(connection, None, payload.enabled, payload.monthly_limit)

    @api.get("/billing/tariffs")
    def tariffs(user=Depends(global_admin)):
        with service.db.read() as connection:
            version, entries = _catalog(connection)
            return {"version": version, "tariffs": [tariff.model_dump(mode="json") for tariff in entries.values()]}

    @api.put("/billing/tariffs")
    def update_tariffs(payload: TariffCatalog, user=Depends(global_admin)):
        with service.db.transaction() as connection:
            version, _ = _catalog(connection)
            if payload.expected_version is not None and version != payload.expected_version:
                raise DomainError("Тарифы уже изменились. Загрузите текущую версию.", 409)
            if connection.execute("SELECT 1 FROM billing_tariff_catalogs WHERE version=?", (payload.version,)).fetchone():
                raise DomainError("Эта версия тарифа уже существует. Укажите новый номер версии.", 409)
            entries = [tariff.model_dump(mode="json") for tariff in payload.tariffs]
            connection.execute("INSERT INTO billing_tariff_catalogs(version,tariffs) VALUES(?,?)",
                               (payload.version, json.dumps(entries, ensure_ascii=False)))
            return {"version": payload.version, "tariffs": entries}
