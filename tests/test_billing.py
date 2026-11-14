import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

import httpx
import pytest

from server import billing, integrations
from server.db import Database
from server.domain import DomainError, Service


@pytest.fixture
def service(tmp_path):
    database = Database(tmp_path / "budget.sqlite3")
    database.initialize()
    service = Service(database)
    service.create_company("Первый")
    service.create_company("Второй")
    return service


def _text_tariff(**changes):
    return billing.Tariff.model_validate({
        "provider": "openai", "kind": "text", "model": "gpt-4.1-mini", "currency": "USD",
        "rub_per_unit": "100", "input_per_million": "1", "output_per_million": "1", "max_input_tokens": 5500,
        **changes,
    })


def _catalog(service, *tariffs, version="fixture"):
    with service.db.transaction() as connection:
        connection.execute("INSERT INTO billing_tariff_catalogs(version,tariffs) VALUES(?,?)",
                           (version, json.dumps([tariff.model_dump(mode="json") for tariff in tariffs])))


def _enable(service, *, cid=None, rubles=1):
    with service.db.transaction() as connection:
        if cid is None:
            connection.execute("INSERT INTO billing_global_limit(id,enabled,monthly_limit) VALUES(1,1,?)", (rubles,))
        else:
            connection.execute("INSERT INTO billing_limits(company_id,enabled) VALUES(?,1)", (cid,))
            connection.execute("UPDATE companies SET monthly_limit=? WHERE id=?", (rubles, cid))


def _reserve(service, cid=1, *, kind="text"):
    with service.db.transaction() as connection:
        row = service._company_row(connection, cid)
        return service._reserve_generation(connection, dict(row), cid, kind=kind)


def _totals(service, cid):
    with service.db.read() as connection:
        return billing._amounts(connection, cid, billing.month_now())


def _send(service, usage_id, send, *, cap=4500, kind="text", payload=None):
    payload = payload or {"model": "gpt-4.1-mini", "input": "Тест", "store": False, "max_output_tokens": cap}

    async def request():
        with billing.billing_scope(service.db, **{kind: usage_id}):
            return await billing.metered_generation(send, integrations._OPENAI_RESPONSES, {}, payload, 5, kind=kind)

    return asyncio.run(request())


def _count_fixture(monkeypatch, count=100):
    async def count_request(*args):
        return count

    monkeypatch.setattr(billing, "_count_openai_input", count_request)


def test_disabled_migration_reports_unpriced_history(service):
    with service.db.transaction() as connection:
        connection.execute("INSERT INTO usage(company_id,month,input_tokens,output_tokens) VALUES(1,?,15,5)",
                           (billing.month_now(),))
    service.db.initialize()
    usage_id = _reserve(service)

    async def transmit(*args):
        return httpx.Response(200, json={"usage": {"input_tokens": 100, "output_tokens": 100}})

    _send(service, usage_id, transmit)
    assert service.usage(1)["requests"] == 2
    assert _totals(service, 1)["unpriced_requests"] == 2
    assert _totals(service, 2)["unpriced_requests"] == 0


def test_intermediate_billing_schema_keeps_existing_amounts_after_repeated_initialize(service):
    tariff = _text_tariff()
    _catalog(service, tariff)
    unstarted_id = _reserve(service)
    usage_id = _reserve(service)
    with service.db.read() as connection:
        charge_id = connection.execute("SELECT id FROM billing_charges WHERE usage_id=?", (usage_id,)).fetchone()[0]
    billing.start_charge(service.db, charge_id)
    with service.db.transaction() as connection:
        connection.execute("ALTER TABLE billing_charges DROP COLUMN started_at_ns")
        connection.execute("ALTER TABLE billing_charges DROP COLUMN expires_at")
        connection.execute("ALTER TABLE billing_opening_balances DROP COLUMN booked_at_ns")
        connection.execute("ALTER TABLE billing_opening_balances DROP COLUMN legacy_usage_id")
        connection.execute("ALTER TABLE billing_opening_balances ADD COLUMN covered_requests INTEGER NOT NULL")
        connection.execute("INSERT INTO billing_opening_balances VALUES(1,?,100,1)", (billing.month_now(),))
    service.db.initialize()
    service.db.initialize()

    totals = _totals(service, 1)
    assert totals["opening_kopecks"] == 100
    assert totals["covered_requests"] == 0
    assert totals["reserved_kopecks"] == 100
    with service.db.read() as connection:
        expired_id = connection.execute("SELECT id FROM billing_charges WHERE usage_id=?", (unstarted_id,)).fetchone()[0]
    with pytest.raises(DomainError, match="нет действующего резерва"):
        billing.start_charge(service.db, expired_id)
    with service.db.transaction() as connection:
        billing._update_opening(connection, 1, billing.BudgetSettings(enabled=True, monthly_limit=2, opening_kopecks=150))
    assert _totals(service, 1)["opening_kopecks"] == 150


def test_global_cap_admits_one_concurrent_company_and_retains_unknown_cost(service, monkeypatch):
    tariff = _text_tariff()
    assert tariff.maximum() == 100
    _catalog(service, tariff)
    _enable(service)
    _count_fixture(monkeypatch)
    barrier = Barrier(2)
    transmitted = []

    def request_company(cid):
        async def transmit(*args):
            transmitted.append(cid)
            raise httpx.ReadTimeout("synthetic ambiguous timeout")

        barrier.wait(timeout=5)
        try:
            _send(service, _reserve(service, cid), transmit)
        except DomainError as error:
            return error.status_code
        except httpx.ReadTimeout:
            return "ambiguous"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(request_company, [1, 2]))
    assert results.count("ambiguous") == results.count(429) == 1
    assert len(transmitted) == 1
    admitted = transmitted[0]
    assert _totals(service, admitted)["reserved_kopecks"] == 100
    assert _totals(service, 3 - admitted)["reserved_kopecks"] == 0
    assert _totals(service, None)["metered_kopecks"] == 0


def test_exact_preflight_blocks_oversized_paid_call_and_releases_unsent_reserve(service, monkeypatch):
    _catalog(service, _text_tariff())
    _enable(service, cid=1)
    _count_fixture(monkeypatch, 5501)

    async def transmit(*args):
        pytest.fail("paid request sent past verified input limit")

    with pytest.raises(DomainError, match="превышает границы рублёвого тарифа"):
        _send(service, _reserve(service), transmit)
    assert _totals(service, 1)["reserved_kopecks"] == 0


def test_atomic_cost_reconciliation_and_tariff_revision_freeze(service, monkeypatch):
    _catalog(service, _text_tariff())
    _enable(service, rubles=10)
    _count_fixture(monkeypatch)

    async def known(*args):
        return httpx.Response(200, json={"usage": {"input_tokens": 100, "output_tokens": 100}})

    _send(service, _reserve(service), known)
    assert _totals(service, 1)["metered_kopecks"] == 2
    assert _totals(service, 1)["reserved_kopecks"] == 0

    async def over_bound(*args):
        return httpx.Response(200, json={"usage": {"input_tokens": 6000, "output_tokens": 4400}})

    _send(service, _reserve(service), over_bound)
    assert _totals(service, 1)["metered_kopecks"] == 106
    with pytest.raises(DomainError, match="новую версию"):
        _reserve(service, 2)
    _catalog(service, _text_tariff(), _text_tariff(model="unrelated"), version="fixture-2")
    with pytest.raises(DomainError, match="новую версию"):
        _reserve(service, 2)
    _catalog(service, _text_tariff(max_input_tokens=6000), version="fixture-3")
    _send(service, _reserve(service, 2), known)
    assert _totals(service, 2)["metered_kopecks"] == 2


def test_unknown_current_model_and_tariff_scope_fail_closed(service):
    _catalog(service, _text_tariff())
    _enable(service, cid=1, rubles=10)
    service.update_company(1, {"model": "custom-generation"})
    with pytest.raises(DomainError, match="подтверждённая цена"):
        _reserve(service, 1)
    assert service.usage(1)["requests"] == 0
    _reserve(service, 2)


def test_separate_opening_balance_covers_legacy_month_without_inventing_cost(service):
    with service.db.transaction() as connection:
        connection.execute("INSERT INTO usage(company_id,month) VALUES(1,?)", (billing.month_now(),))
    _catalog(service, _text_tariff())
    _enable(service, cid=1, rubles=1)
    with pytest.raises(DomainError, match="вызовы без тарифа"):
        _reserve(service)
    with service.db.transaction() as connection:
        billing._update_opening(connection, 1, billing.BudgetSettings(enabled=True, monthly_limit=2, opening_kopecks=100))
        connection.execute("UPDATE companies SET monthly_limit=2 WHERE id=1")
    _reserve(service)
    assert _totals(service, 1)["opening_kopecks"] == 100
    assert _totals(service, 2)["opening_kopecks"] is None
    with pytest.raises(DomainError, match="не хватает"):
        _reserve(service)


def test_image_tool_configuration_and_incomplete_usage_stay_reserved(service, monkeypatch):
    image_tariff = _text_tariff(kind="image", model=integrations._PHOTO_MODEL,
        image_model=integrations._PHOTO_TOOL_MODEL, image_bounds_source="https://provider.example/contract",
        image_input_per_million=8, image_output_per_million=30, max_image_input_tokens=2000, max_image_output_tokens=3000)
    _catalog(service, image_tariff)
    _enable(service, cid=1, rubles=500)
    _count_fixture(monkeypatch)
    payload = {"model": integrations._PHOTO_MODEL, "input": "Фото", "store": False,
               "tools": [{"type": "image_generation", "model": integrations._PHOTO_TOOL_MODEL, "action": "generate",
                          "output_format": "png", "quality": "medium", "size": "auto"}],
               "tool_choice": {"type": "image_generation"}, "max_tool_calls": 1, "parallel_tool_calls": False}

    async def transmit(endpoint, headers, request, timeout):
        assert request["tools"][0]["size"] == "1024x1024"
        assert request["max_output_tokens"] == 4500
        return httpx.Response(200, json={"usage": {"input_tokens": 100, "output_tokens": 100}})

    _send(service, _reserve(service, kind="image"), transmit, kind="image", payload=payload)
    assert _totals(service, 1)["reserved_kopecks"] == image_tariff.maximum()


def test_unpriced_reserved_call_rechecked_when_limit_is_enabled(service):
    usage_id = _reserve(service)
    _enable(service, cid=1)

    async def transmit(*args):
        pytest.fail("unguarded call bypassed the activated cap")

    with pytest.raises(DomainError, match="лимит изменился"):
        _send(service, usage_id, transmit)
    assert _totals(service, 1)["unpriced_requests"] == 0


def test_unsent_reserve_expires_but_submitted_unknown_cost_survives_restart(service, monkeypatch):
    _catalog(service, _text_tariff())
    _enable(service, rubles=2)
    now = billing.time.time()
    unsent = _reserve(service)
    started = _reserve(service)
    with service.db.read() as connection:
        started_charge = connection.execute("SELECT id FROM billing_charges WHERE usage_id=?", (started,)).fetchone()[0]
    billing.start_charge(service.db, started_charge)
    monkeypatch.setattr(billing.time, "time", lambda: now + billing._RESERVATION_TTL + 2)
    service.db.initialize()
    assert _totals(service, 1)["reserved_kopecks"] == 100

    async def transmit(*args):
        pytest.fail("expired reservation admitted")

    with pytest.raises(DomainError, match="нет действующего резерва"):
        _send(service, unsent, transmit)
    _reserve(service, 2)
    assert _totals(service, None)["reserved_kopecks"] == 200


def test_former_month_reservation_rechecks_current_month_at_dispatch(service, monkeypatch):
    _catalog(service, _text_tariff())
    _enable(service, rubles=1)
    previous = billing.month_now()
    usage_id = _reserve(service)
    with service.db.read() as connection:
        charge_id = connection.execute("SELECT id FROM billing_charges WHERE usage_id=?", (usage_id,)).fetchone()[0]
    monkeypatch.setattr(billing, "month_now", lambda: "2099-01")
    billing.start_charge(service.db, charge_id)
    with service.db.read() as connection:
        assert billing._amounts(connection, None, previous)["reserved_kopecks"] == 0
    assert _totals(service, None)["reserved_kopecks"] == 100


def test_priced_unguarded_request_keeps_original_options_and_settles(service, monkeypatch):
    _catalog(service, _text_tariff())

    async def unexpected_preflight(*args):
        pytest.fail("billing tariff setup changed unguarded generation")

    monkeypatch.setattr(billing, "_count_openai_input", unexpected_preflight)

    async def transmit(*args):
        assert _totals(service, 1)["unpriced_requests"] == 1
        return httpx.Response(200, json={"usage": {"input_tokens": 100, "output_tokens": 100}})

    _send(service, _reserve(service), transmit)
    assert _totals(service, 1)["unpriced_requests"] == 0
    assert _totals(service, 1)["metered_kopecks"] == 2


def test_yandex_guard_requires_documented_context_and_rejects_vision_before_send(service):
    yandex = {"provider": "yandex", "model": "qwen3.6-35b-a3b", "currency": "RUB", "rub_per_unit": 1}
    service.update_company(1, {"provider": "yandex", "model": yandex["model"]})
    _catalog(service, _text_tariff(**yandex, max_input_tokens=50_000))
    _enable(service, cid=1, rubles=500)
    with pytest.raises(DomainError, match="подтверждённым размером контекста"):
        _reserve(service)
    _catalog(service, _text_tariff(**yandex, max_input_tokens=262_144), version="complete-context")

    async def transmit(endpoint, headers, payload, timeout):
        assert payload["truncation"] == "disabled"
        return httpx.Response(200, json={"usage": {"input_tokens": 100, "output_tokens": 100}})

    async def request(input_data):
        with billing.billing_scope(service.db, text=_reserve(service)):
            await billing.metered_generation(transmit, integrations._YANDEX_RESPONSES, {},
                {"model": "gpt://synthetic-folder/qwen3.6-35b-a3b", "input": input_data,
                 "store": False, "max_output_tokens": 4500}, 5)

    with pytest.raises(DomainError, match="только текстовый вход"):
        asyncio.run(request([{"role": "user", "content": [
            {"type": "input_image", "image_url": "data:image/png;base64,aA=="}
        ]}]))
    asyncio.run(request("Текст"))
    assert _totals(service, 1)["metered_kopecks"] == 1
