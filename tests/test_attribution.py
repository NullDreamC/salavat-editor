from datetime import datetime, time, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from server.attribution import EventInput, record_crm_event
from server.domain import audit_row
from test_backend import EDITOR, REVIEWER, SUPER, add_person, api, new_post, sign_in


PATH = "/api/companies/1/attribution"
TARGET = "https://example.test/application?ref=web&utm_campaign=obsolete#form"


def enable(client, path=PATH):
    response = client.patch(path + "/settings", json={"enabled": True, "target_url": TARGET})
    assert response.status_code == 200, response.text
    response = client.post(path + "/key")
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["token"]}


def make_campaign(client, post):
    response = client.post(PATH + "/campaigns", json={"post_id": post["id"]})
    assert response.status_code == 201, response.text
    return response.json()


def publish(service, post):
    with service.db.transaction() as connection:
        connection.execute("UPDATE posts SET status='published',message_id='1' WHERE id=?", (post["id"],))
        audit_row(connection, 1, "post.published", f"Опубликован пост №{post['id']}.")


def event(campaign, **overrides):
    return {"event_id": "event-01", "reference": "opaque-lead-01", "campaign_code": campaign["code"],
            "kind": "lead", "occurred_at": datetime.now(timezone.utc).isoformat(), **overrides}


def test_default_opt_in_roles_cross_company_and_safe_link(api):
    client, service = api
    post_path, draft = new_post(client)
    assert client.get(PATH).json()["enabled"] is False
    assert client.post(PATH + "/campaigns", json={"post_id": draft["id"]}).status_code == 409
    for unsafe in ("http://example.test/", "https://user:pass@example.test/", "https://localhost/form",
                   "https://127.0.0.1/", "https://example.test/?token=private"):
        assert client.patch(PATH + "/settings", json={"enabled": True, "target_url": unsafe}).status_code == 422
    headers = enable(client)
    assert client.get(PATH + "/settings").json()["key_configured"] is True
    assert headers["Authorization"].split()[1] not in client.get(PATH + "/settings").text
    assert headers["Authorization"].split()[1] not in client.get(PATH).text
    with service.db.read() as connection:
        assert connection.execute("SELECT key_hash FROM attribution_settings").fetchone()[0] != headers["Authorization"].split()[1]

    add_person(client, EDITOR)
    add_person(client, REVIEWER)
    with sign_in(client.app, EDITOR) as editor:
        campaign = make_campaign(editor, draft)
        assert make_campaign(editor, draft)["code"] == campaign["code"]
        assert editor.get(PATH.replace("/1/", "/2/")).status_code == 404
        assert editor.get(PATH + "/settings").status_code == 403
        assert editor.post(PATH + "/key").status_code == 403
    with sign_in(client.app, REVIEWER) as reviewer:
        assert reviewer.get(PATH).json()["prepared"] == []
        assert reviewer.post(PATH + "/campaigns", json={"post_id": draft["id"]}).status_code == 403
        assert reviewer.patch(PATH + "/settings", json={"enabled": False, "target_url": ""}).status_code == 403
    with sign_in(client.app, SUPER) as owner:
        assert owner.get(PATH).status_code == 403
        assert owner.post(PATH + "/key").status_code == 403
    assert client.get(post_path).json() == draft
    assert client.get(PATH).json()["prepared"][0]["url"] == campaign["url"]
    parsed = urlsplit(campaign["url"])
    assert parsed.netloc == "example.test" and parsed.fragment == "form"
    assert parse_qs(parsed.query) == {"ref": ["web"], "utm_source": ["editorial"], "utm_medium": ["social"],
                                     "utm_campaign": [campaign["code"]]}
    other = PATH.replace("/1/", "/2/")
    other_headers = enable(client, other)
    assert client.post(other + "/campaigns", json={"post_id": draft["id"]}).status_code == 404
    with TestClient(client.app) as webhook:
        assert webhook.post(other + "/events", json=event(campaign), headers=headers).status_code == 401
        assert webhook.post(other + "/events", json=event(campaign), headers=other_headers).status_code == 404
        assert webhook.post(PATH + "/events", json=event(campaign), headers=headers).status_code == 404
    publish(service, draft)
    data = client.get(PATH).json()
    assert data["prepared"] == [] and data["items"][0]["post_id"] == draft["id"]


def test_webhook_authentication_validation_rotation_and_disable(api):
    client, service = api
    _, post = new_post(client)
    headers = enable(client)
    campaign = make_campaign(client, post)
    publish(service, post)
    payload = event(campaign)
    origin = {"Origin": "https://crm.example.test"}
    with TestClient(client.app) as webhook:
        assert webhook.post(PATH + "/events", json=payload).status_code == 401
        assert webhook.post(PATH + "/events", json=payload, headers={"Authorization": "Bearer att_" + "x" * 43}).status_code == 401
        assert webhook.post(PATH + "/events/", json=payload, headers={**headers, **origin}).status_code == 403
        assert webhook.post(PATH + "/key", headers={**headers, **origin}).status_code == 403
        assert webhook.post(PATH + "/events", content="no", headers=headers).status_code == 415
        assert webhook.post(PATH + "/events", content=b"x" * 4097, headers={**headers, "Content-Type": "application/json"}).status_code == 413
        assert webhook.post(PATH + "/events", content=iter([b"x" * 3000, b"x" * 3000]),
                            headers={**headers, "Content-Type": "application/json"}).status_code == 413
        for invalid in ({"email": "test@example.test"}, {"reference": "test@example.test"}, {"occurred_at": 0},
                        {"occurred_at": "2026-01-01T00:00:00"}, {"kind": "sale", "currency": "USD", "amount_kopecks": 1},
                        {"kind": "sale", "currency": "RUB", "amount_kopecks": "1"}, {"amount_kopecks": 5},
                        {"occurred_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}):
            assert webhook.post(PATH + "/events", json={**payload, **invalid}, headers=headers).status_code == 422
        assert webhook.post(PATH + "/events", json=payload, headers={**headers, **origin}).json() == {"recorded": True}
        assert webhook.post(PATH + "/events", json=payload, headers=headers).json() == {"recorded": False}
        assert webhook.post(PATH + "/events", json={**payload, "reference": "different"}, headers=headers).status_code == 409
        assert webhook.post(PATH + "/events", json={**payload, "event_id": "alternate"}, headers=headers).status_code == 409
        token = client.post(PATH + "/key").json()["token"]
        assert webhook.post(PATH + "/events", json=payload, headers=headers).status_code == 401
        assert client.patch(PATH + "/settings", json={"enabled": False, "target_url": TARGET}).status_code == 200
        assert webhook.post(PATH + "/events", json=payload, headers={"Authorization": "Bearer " + token}).status_code == 409
    assert client.get(PATH).json()["summary"]["leads"] == 1
    with service.db.read() as connection:
        record = connection.execute("SELECT event_hash,reference_hash FROM attribution_events").fetchone()
        assert len(record[0]) == len(record[1]) == 64
        assert payload["event_id"] not in tuple(record) and payload["reference"] not in tuple(record)


def test_sales_refunds_spend_and_source(api):
    client, service = api
    _, first = new_post(client)
    _, second = new_post(client)
    headers = enable(client)
    campaign = make_campaign(client, first)
    another_campaign = make_campaign(client, second)
    publish(service, first)
    publish(service, second)
    sale = event(campaign, kind="sale", reference="order-01", amount_kopecks=10_000, currency="RUB")
    refund = event(campaign, event_id="refund-01", kind="refund", reference="order-01", amount_kopecks=1_500, currency="RUB")
    with TestClient(client.app) as webhook:
        send = lambda data: webhook.post(PATH + "/events", json=data, headers=headers)
        assert send(refund).status_code == 409
        assert send(sale).json() == {"recorded": True}
        assert send({**sale, "event_id": "double-sale"}).status_code == 409
        assert send({**refund, "campaign_code": another_campaign["code"]}).status_code == 409
        assert send(refund).json() == {"recorded": True}
        assert send(refund).json() == {"recorded": False}
        assert send({**refund, "event_id": "refund-02", "amount_kopecks": 9_000}).status_code == 409
    spend_path = PATH + f"/campaigns/{first['id']}"
    assert client.patch(spend_path, json={"cost_kopecks": 2000}).status_code == 200
    report = client.get(PATH).json()
    assert report["summary"] == {"events": 2, "leads": 0, "sales": 1, "refunds": 1, "net_kopecks": 8500}
    saved = next(item for item in report["items"] if item["post_id"] == first["id"])
    assert saved["revenue_cost_ratio"] == 4.25 and saved["sources"] == ["crm_webhook"]
    assert saved["last_received_at"] is not None
    assert client.get(PATH + "?start=2020-01-01&end=2020-01-01").json()["summary"]["net_kopecks"] is None
    assert client.get(PATH + "?start=2020-01-02&end=2020-01-01").status_code == 422

    def has_access(connection):
        assert connection.in_transaction

    provider_sale = EventInput.model_validate({**sale, "campaign_code": another_campaign["code"]})
    assert record_crm_event(service, 1, provider_sale, "bitrix24", access_check=has_access) == {"recorded": True}
    assert record_crm_event(service, 1, provider_sale.model_copy(update={"amount_kopecks": 20_000}),
                            "bitrix24", access_check=has_access) == {"recorded": False}
    saved = next(item for item in client.get(PATH).json()["items"] if item["post_id"] == second["id"])
    assert saved["net_kopecks"] == 10_000 and saved["sources"] == ["bitrix24"]
    assert saved["revenue_cost_ratio"] is None
    filtered = client.get(PATH + "?source=bitrix24").json()
    assert filtered["source"] == "bitrix24"
    assert filtered["summary"] == {"events": 1, "leads": 0, "sales": 1, "refunds": 0, "net_kopecks": 10_000}


def test_refund_is_reported_on_its_local_event_date(api):
    client, service = api
    _, post = new_post(client)
    headers = enable(client)
    campaign = make_campaign(client, post)
    publish(service, post)
    zone = ZoneInfo("Europe/Moscow")
    yesterday = datetime.now(zone).date() - timedelta(days=1)
    local_midnight = datetime.combine(yesterday, time.min, zone)
    link_time = (local_midnight - timedelta(days=2)).astimezone(timezone.utc).isoformat()
    with service.db.transaction() as connection:
        connection.execute("UPDATE attribution_campaigns SET created_at=? WHERE post_id=?", (link_time, post["id"]))
        connection.execute("UPDATE activity SET created_at=? WHERE action='post.published'", (link_time,))
    sale = event(campaign, kind="sale", reference="order-01", amount_kopecks=10_000, currency="RUB",
                 occurred_at=(local_midnight - timedelta(seconds=1)).astimezone(timezone.utc).isoformat())
    refund = {**sale, "event_id": "refund-01", "kind": "refund", "amount_kopecks": 2_000,
              "occurred_at": local_midnight.astimezone(timezone.utc).isoformat()}
    with TestClient(client.app) as webhook:
        assert webhook.post(PATH + "/events", json=sale, headers=headers).json() == {"recorded": True}
        assert webhook.post(PATH + "/events", json=refund, headers=headers).json() == {"recorded": True}
    period = {"start": yesterday, "end": yesterday}
    report = client.get(PATH, params=period).json()
    assert report["summary"] == {"events": 1, "leads": 0, "sales": 0, "refunds": 1, "net_kopecks": -2_000}
