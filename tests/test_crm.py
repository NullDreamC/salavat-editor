import asyncio
from datetime import datetime, timezone
from functools import lru_cache
import socket

import pytest

from server import crm
from test_attribution import enable, make_campaign, publish
from test_backend import EDITOR, SUPER, add_person, api, new_post, sign_in


ROOT = "/api/companies/1/crm"
BITRIX = ROOT + "/bitrix24"
AMO = ROOT + "/amocrm"
WEBHOOK = "https://example.bitrix24.ru/rest/7/test_webhook_secret/"
TOKEN = "test.amo.long_lived_token"


@lru_cache(maxsize=1)
def dpapi_available():
    if crm.os.name != "nt":
        return False
    try:
        return crm.reveal_secret(crm.store_secret("disposable-test-value")) == "disposable-test-value"
    except OSError:
        return False


@pytest.fixture(autouse=True)
def no_live_requests(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Test attempted an external CRM request")

    monkeypatch.setattr(crm, "_request_json", blocked)
    if not dpapi_available():
        from base64 import b64decode, b64encode

        monkeypatch.setattr(crm, "_secret", lambda value: b64encode(value.encode()))
        monkeypatch.setattr(crm, "reveal_secret", lambda value: b64decode(value).decode())


def put_bitrix(client, **changes):
    return client.put(BITRIX, json={"version": 0, "enabled": True, "webhook_url": WEBHOOK, **changes})


def put_amo(client, **changes):
    return client.put(AMO, json={"version": 0, "enabled": True, "domain": "example.amocrm.ru",
                                 "token": TOKEN, **changes})


def now():
    return datetime.now(timezone.utc).isoformat()


def campaign_for(client, service):
    enable(client)
    _, draft = new_post(client)
    campaign = make_campaign(client, draft)
    publish(service, draft)
    return campaign["code"]


def test_admin_only_settings_write_only_credentials_and_revoke(api):
    client, service = api
    default = client.get(ROOT).json()
    assert [row["provider"] for row in default] == ["bitrix24", "amocrm"]
    assert all(not row["enabled"] and row["interval_minutes"] == 0 for row in default)
    for invalid in ("http://example.bitrix24.ru/rest/7/test_webhook_secret",
                    "https://example.bitrix24.ru.evil.test/rest/7/test_webhook_secret",
                    "https://example.bitrix24.ru@evil.test/rest/7/test_webhook_secret",
                    "https://example.bitrix24.ru:444/rest/7/test_webhook_secret",
                    WEBHOOK + "crm.deal.add", WEBHOOK + "?secret=token"):
        reply = put_bitrix(client, webhook_url=invalid)
        assert reply.status_code == 422 and "test_webhook_secret" not in reply.text
    assert put_amo(client, domain="example.amocrm.ru.evil.test").status_code == 422
    invalid = client.put(BITRIX, json={"version": 0, "enabled": True, WEBHOOK: WEBHOOK})
    assert invalid.status_code == 422 and "test_webhook_secret" not in invalid.text
    result = put_bitrix(client)
    assert result.status_code == 200, result.text
    assert result.json()["domain"] == "example.bitrix24.ru" and result.json()["configured"]
    assert put_amo(client).status_code == 200
    assert "test_webhook_secret" not in result.text + client.get(ROOT).text
    assert TOKEN not in client.get(ROOT).text + client.get("/api/companies/1/activity").text
    with service.db.read() as connection:
        stored = connection.execute("SELECT credential FROM crm_connections WHERE provider='bitrix24'").fetchone()[0]
    assert b"test_webhook_secret" not in stored
    assert put_bitrix(client).status_code == 409
    assert client.post(BITRIX + "/sync").status_code == 409
    add_person(client, EDITOR)
    with sign_in(client.app, EDITOR) as editor:
        assert editor.get(ROOT).status_code == 403
        assert put_bitrix(editor).status_code == 403
        assert editor.post(BITRIX + "/sync").status_code == 403
        assert editor.get(ROOT.replace("/1/", "/2/")).status_code == 404
    with sign_in(client.app, SUPER) as owner:
        assert owner.get(ROOT).status_code == 403
    state = client.put(BITRIX, json={"version": 1, "enabled": False, "remove_credential": True}).json()
    assert not state["configured"] and not state["enabled"] and not state["domain"]
    with service.db.read() as connection:
        assert connection.execute("SELECT credential FROM crm_connections WHERE provider='bitrix24'").fetchone()[0] is None


def test_bitrix_checkpoint_converted_lead_and_won_deal(api, monkeypatch):
    client, service = api
    campaign = campaign_for(client, service)
    assert put_bitrix(client, lead_field="UF_CRM_101").status_code == 200
    calls = []
    closed = now()
    monkeypatch.setattr(crm, "PAGE_BUDGET", 1)

    def get_page(host, provider, method, *, credential, data):
        assert (host, provider, credential) == ("example.bitrix24.ru", "bitrix24", "/rest/7/test_webhook_secret")
        cursor = data["filter"][">ID"]
        calls.append((method, cursor))
        assert data["start"] == -1 and data["order"] == {"ID": "ASC"}
        assert set(data["select"]).isdisjoint({"TITLE", "NAME", "PHONE", "EMAIL"})
        if method == "crm.lead.list":
            assert "UF_CRM_101" in data["select"]
            return {"result": [{"ID": str(i), "DATE_CREATE": closed, "UF_CRM_101": campaign,
                                 "STATUS_SEMANTIC_ID": "S", "NAME": "test private data"}
                                for i in range(1, 52) if i > cursor][:50]}
        return {"result": [
            {"ID": "1", "UTM_CAMPAIGN": campaign, "STAGE_SEMANTIC_ID": "S",
             "MOVED_TIME": closed, "CURRENCY_ID": "RUB", "OPPORTUNITY": "840.15"},
            {"ID": "2", "UTM_CAMPAIGN": campaign, "STAGE_SEMANTIC_ID": "S",
             "MOVED_TIME": closed, "CURRENCY_ID": "USD", "OPPORTUNITY": "100"},
            {"ID": "3", "UTM_CAMPAIGN": campaign, "STAGE_SEMANTIC_ID": "unexpected"},
            {"ID": "4", "UTM_CAMPAIGN": campaign, "STAGE_SEMANTIC_ID": "F"},
            {"ID": "5", "UTM_CAMPAIGN": "p_0000000000000000", "STAGE_SEMANTIC_ID": "S",
             "MOVED_TIME": closed, "CURRENCY_ID": "RUB", "OPPORTUNITY": "100"},
        ]}

    monkeypatch.setattr(crm, "_request_json", get_page)
    first = client.post(BITRIX + "/sync")
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["last_state"] == "partial"
    counts = data["last_counts"]
    assert counts["lead"] == 50 and counts["sale"] == 1 and counts["unmatched"] == 1
    assert counts["unsupported_currency"] == 1 and counts["unknown_stage"] == 1
    data = client.post(BITRIX + "/sync").json()
    assert data["last_state"] == "completed" and data["last_counts"]["lead"] == 1
    assert calls == [("crm.lead.list", 0), ("crm.deal.list", 0), ("crm.lead.list", 50)]
    replay = client.post(BITRIX + "/sync").json()
    assert replay["last_counts"]["duplicates"] == 51
    with service.db.read() as connection:
        rows = connection.execute("SELECT kind,amount_kopecks,source,event_hash FROM attribution_events").fetchall()
    assert len(rows) == 52
    assert [(row[1], row[2]) for row in rows if row[0] == "sale"] == [(84015, "bitrix24")]
    assert all(len(row[3]) == 64 for row in rows)


def test_amo_uses_own_pages_and_only_confirmed_won_rub_sales(api, monkeypatch):
    client, service = api
    campaign = campaign_for(client, service)
    assert put_amo(client, amo_field_id=74).status_code == 200
    observed = int(datetime.now(timezone.utc).timestamp()) + 1
    seen = []

    def get_page(host, provider, path, *, credential, params):
        assert (host, provider, path, credential) == ("example.amocrm.ru", "amocrm", "/api/v4/leads", TOKEN)
        page = params["page"]
        seen.append(page)
        if page == 1:
            return {"_page": 1, "_links": {"next": {"href": "https://127.0.0.1/would_leak_secret"}},
                    "_embedded": {"leads": [{"id": 12, "status_id": 142, "price": 1000,
                       "created_at": observed, "closed_at": observed, "custom_fields_values": [
                         {"field_id": 74, "values": [{"value": campaign}]}]}]}}
        return {"_page": 2, "_links": {}, "_embedded": {"leads": []}}

    monkeypatch.setattr(crm, "_request_json", get_page)
    first = client.post(AMO + "/sync").json()
    assert first["last_state"] == "completed"
    assert first["last_counts"]["lead"] == 1 and first["last_counts"]["sale"] == 0
    assert first["last_counts"]["unsupported_currency"] == 1
    assert seen == [1, 2]
    assert client.put(AMO, json={"version": 1, "enabled": True, "amo_field_id": 74, "amo_rub": True}).status_code == 200
    next_sync = client.post(AMO + "/sync").json()
    assert next_sync["last_counts"]["duplicates"] == 1 and next_sync["last_counts"]["sale"] == 1
    with service.db.read() as connection:
        rows = [tuple(row) for row in connection.execute(
            "SELECT kind,amount_kopecks,source FROM attribution_events ORDER BY kind"
        )]
    assert rows == [("lead", None, "amocrm"), ("sale", 100000, "amocrm")]


def test_lease_rejects_parallel_sync_and_rotation_during_read(api, monkeypatch):
    client, service = api
    campaign = campaign_for(client, service)
    assert put_bitrix(client).status_code == 200

    def read_then_revoke(host, provider, method, *, credential, data):
        assert client.post(BITRIX + "/sync").status_code == 409
        result = client.put(BITRIX, json={"version": 1, "enabled": False, "remove_credential": True})
        assert result.status_code == 200
        return {"result": [{"ID": "1", "UTM_CAMPAIGN": campaign, "DATE_CREATE": now()}]}

    monkeypatch.setattr(crm, "_request_json", read_then_revoke)
    assert client.post(BITRIX + "/sync").status_code == 409
    with service.db.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM attribution_events").fetchone()[0] == 0
        assert connection.execute("SELECT lease FROM crm_connections").fetchone()[0] is None


def test_public_dns_rejects_any_non_public_answer(monkeypatch):
    def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in ("8.8.8.8", "127.0.0.1")]

    monkeypatch.setattr(crm.socket, "getaddrinfo", resolve)
    with pytest.raises(crm.CrmError):
        crm._public_ip("example.amocrm.ru")


def test_schedule_requires_explicit_interval_and_current_admin(api, monkeypatch):
    client, service = api
    enable(client)
    assert put_bitrix(client).status_code == 200
    assert put_amo(client, interval_minutes=15).status_code == 200
    called = []

    def fake_request(host, provider, path, *, credential, params):
        called.append(provider)
        return {"_embedded": {"leads": []}}

    monkeypatch.setattr(crm, "_request_json", fake_request)
    asyncio.run(crm.sync_due(service, 2))
    asyncio.run(crm.sync_due(service, 1))
    asyncio.run(crm.sync_due(service, 1))
    assert called == ["amocrm"]
    with service.db.transaction() as connection:
        connection.execute("UPDATE crm_connections SET next_due=0")
        connection.execute(
            "UPDATE memberships SET active=0 WHERE company_id=1 AND user_id IN "
            "(SELECT configured_by FROM crm_connections WHERE company_id=1)"
        )
    asyncio.run(crm.sync_due(service, 1))
    assert called == ["amocrm"]
