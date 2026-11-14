from datetime import datetime, timedelta, timezone
import sqlite3

from fastapi.testclient import TestClient
import pytest

from server import domain, integrations
from server.db import Database, SCHEMA
from server.main import COOKIE, create_app


ADMIN = {"name": "Администратор", "email": "admin@example.test", "password": "admin-pass-123"}
SUPER = {"name": "Главный администратор", "email": "owner@example.test", "password": "owner-pass-123"}
EDITOR = {"name": "Редактор", "email": "editor@example.test", "password": "editor-pass-123", "role": "editor"}
REVIEWER = {"name": "Согласующий", "email": "reviewer@example.test", "password": "review-pass-123", "role": "approver"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    # The test vault holds synthetic credentials; no provider or real key is read.
    monkeypatch.setattr(domain, "store_secret", lambda value: value.encode() if value else None)
    monkeypatch.setattr(domain, "reveal_secret", lambda value: value.decode() if value else "")
    monkeypatch.setattr(integrations, "openai_fallback_available", lambda: False)
    application = create_app(Database(tmp_path / "app.sqlite3"))
    with TestClient(application) as client:
        response = client.post("/api/auth/setup", json=SUPER)
        assert response.status_code == 200, response.text
        employee = TestClient(application)
        for cid in (1, 2, 3):
            invite = client.post(f"/api/companies/{cid}/invitations", json={"role": "admin"}).json()
            accepted = employee.post(f"/api/invitations/{invite['token']}/accept", json=ADMIN)
            assert accepted.status_code == 200, accepted.text
        yield employee, application.state.service
        employee.close()


def join_company(client, account, cid, role):
    invite = client.post(f"/api/companies/{cid}/invitations", json={"role": role})
    assert invite.status_code == 201, invite.text
    with TestClient(client.app) as recipient:
        result = recipient.post(f"/api/invitations/{invite.json()['token']}/accept", json={key: account[key] for key in ('name', 'email', 'password')})
        assert result.status_code == 200, result.text
        return result.json()


def add_person(client, payload, cid=1):
    response = client.post(f"/api/companies/{cid}/users", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def sign_in(application, payload):
    client = TestClient(application)
    response = client.post("/api/auth/login", json={field: payload[field] for field in ("email", "password")})
    assert response.status_code == 200, response.text
    return client


def configure(client, **fields):
    response = client.patch("/api/companies/1", json=fields)
    assert response.status_code == 200, response.text
    return response.json()


def new_post(client, *, text="Собрание 2027-06-14 в 10:00 в актовом зале."):
    response = client.post("/api/companies/1/sources", json={"title": "Собрание", "text": text})
    assert response.status_code == 201, response.text
    source_id = response.json()["id"]
    response = client.post("/api/companies/1/posts", json={"source_ids": [source_id], "kind": "announcement"})
    assert response.status_code == 201, response.text
    post = response.json()
    return f"/api/companies/1/posts/{post['id']}", post


def action(client, path, operation, post, **payload):
    response = client.post(f"{path}/{operation}", json={"version": post["version"], **payload})
    assert response.status_code == 200, response.text
    return response.json()


def write(client, path, post, **changes):
    response = client.patch(path, json={"version": post["version"], **changes})
    assert response.status_code == 200, response.text
    return response.json()


def test_setup_session_and_cross_origin(api):
    client, service = api
    assert client.get("/api/auth/setup").json() == {"required": False}
    assert client.post("/api/auth/setup", json=ADMIN).status_code == 409
    assert [item["name"] for item in client.get("/api/companies").json()] == ["Салаватстекло", "Компания 2", "Компания 3"]
    assert client.post("/api/companies", headers={"Origin": "https://elsewhere.invalid"},
                       json={"name": "Несанкционированный запрос"}).status_code == 403
    anonymous = TestClient(client.app)
    assert anonymous.get("/api/companies").status_code == 401
    response = anonymous.post("/api/auth/login", json={"email": ADMIN["email"], "password": ADMIN["password"]})
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
    raw_cookie = anonymous.cookies.get(COOKIE)
    with service.db.read() as connection:
        assert connection.execute("SELECT token_hash FROM sessions WHERE token_hash=?", (raw_cookie,)).fetchone() is None
        assert ADMIN["password"] not in connection.execute("SELECT password_hash FROM users").fetchone()[0]
    assert anonymous.post("/api/auth/logout").status_code == 204
    assert anonymous.get("/api/auth/me", headers={"Cookie": f"{COOKIE}={raw_cookie}"}).status_code == 401


def test_tenant_assets_members_and_secret_fields(api):
    client, _ = api
    created = add_person(client, EDITOR, cid=2)
    outsider = sign_in(client.app, EDITOR)
    settings = configure(client, telegram_token="synthetic-token", openai_key="synthetic-key")
    assert settings["telegram_configured"] and settings["openai_configured"]
    assert all(key not in settings for key in ("telegram_token", "openai_key", "yandex_key"))
    assert [item["id"] for item in outsider.get("/api/companies").json()] == [2]
    assert outsider.get("/api/companies/2/settings").status_code == 403
    upload = client.post("/api/companies/1/uploads", files={"file": ("material.txt", b"private", "text/plain")})
    assert upload.status_code == 201, upload.text
    item = upload.json()
    assert client.get(item["file_url"]).content == b"private"
    assert outsider.get(item["file_url"]).status_code == 404
    assert outsider.get(item["file_url"].replace("/1/", "/2/")).status_code == 404
    assert outsider.post("/api/companies/2/posts", json={"source_ids": [item["id"]]}).status_code == 404
    assert "file_path" not in item
    assert client.patch(f"/api/companies/2/users/{created['id']}", json={"active": False}).status_code == 200
    assert outsider.get("/api/auth/me").status_code == 401


def test_shared_account_has_distinct_company_roles(api):
    client, _ = api
    editor = add_person(client, EDITOR)
    approver = join_company(client, EDITOR, 2, "approver")
    assert editor["id"] == approver["id"]
    employee = sign_in(client.app, EDITOR)
    assert [(item["id"], item["role"]) for item in employee.get("/api/companies").json()] == [(1, "editor"), (2, "approver")]
    assert employee.post("/api/companies/2/sources", json={"title": "Материал", "text": "Текст"}).status_code == 403
    assert employee.post("/api/companies/1/sources", json={"title": "Материал", "text": "Текст"}).status_code == 201


def test_editorial_rules_migrate_and_stay_scoped_to_company(tmp_path, monkeypatch):
    db = Database(tmp_path / "previous.sqlite3")
    with sqlite3.connect(db.path) as connection:
        connection.executescript(SCHEMA.replace("    editorial_rules TEXT NOT NULL DEFAULT '',\n", ""))
    monkeypatch.setattr(domain, "store_secret", lambda value: value.encode() if value else None)
    monkeypatch.setattr(domain, "reveal_secret", lambda value: value.decode() if value else "")
    seen = {}

    async def generate(company, sources, instruction, credentials):
        seen[company["id"]] = instruction
        return {"title": "Черновик", "body": "", "questions": ["Нужна дата?"], "facts": [], "events": [],
                "input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(integrations, "generate_post", generate)
    app = create_app(db)
    with TestClient(app) as client:
        assert client.post("/api/auth/setup", json=SUPER).status_code == 200
        invites = [client.post(f"/api/companies/{cid}/invitations", json={"role": "admin"}).json() for cid in (1, 2, 3)]
        client.post('/api/auth/logout')
        for invite in invites:
            assert client.post(f"/api/invitations/{invite['token']}/accept", json=ADMIN).status_code == 200
        assert all(company["editorial_rules"] == "" for company in client.get("/api/companies").json())
        rules = {1: "Пишите от первого лица.", 2: "Используйте официальное обращение."}
        for cid, rule in rules.items():
            path = f"/api/companies/{cid}"
            assert client.patch(path, json={"openai_key": "synthetic-key", "editorial_rules": rule}).status_code == 200
            assert client.get(path + "/settings").json()["editorial_rules"] == rule
            source = client.post(path + "/sources", json={"title": "Собрание", "text": "Планируем собрание."}).json()
            post = client.post(path + "/posts", json={"source_ids": [source["id"]]}).json()
            action(client, f"{path}/posts/{post['id']}", "generate", post)
        assert client.patch("/api/companies/2", json={"editorial_rules": "а" * 12001}).status_code == 422
    assert rules[1] in seen[1] and rules[1] not in seen[2]
    assert rules[2] in seen[2] and rules[2] not in seen[1]
    assert "Салаватстекло" in seen[1] and "Салаватстекло" not in seen[2]
    assert "Начало инструкции" not in seen[1] and "Конец инструкции" not in seen[1]
    db.initialize()
    assert app.state.service.company(2)["editorial_rules"] == rules[2]


def test_approval_rechecks_version_role_and_exact_media(api, monkeypatch):
    client, _ = api
    configure(client, channel_id="@test_channel", telegram_token="synthetic-token")
    add_person(client, EDITOR)
    add_person(client, REVIEWER)
    editor = sign_in(client.app, EDITOR)
    reviewer = sign_in(client.app, REVIEWER)
    path, post = new_post(editor)
    post = write(editor, path, post, body="Пройдёт собрание.")
    post = action(editor, path, "submit", post)
    assert editor.post(path + "/approve", json={"version": post["version"]}).status_code == 403
    assert reviewer.patch(path, json={"version": post["version"], "body": "Чужая правка"}).status_code == 403
    approved = action(reviewer, path, "approve", post)
    post = action(editor, path, "card", approved)
    assert post["status"] == "draft" and post["card_url"] and post["version"] > approved["version"]
    assert editor.get(post["card_url"]).headers["content-type"] == "image/png"
    assert reviewer.post(path + "/publish", json={"version": approved["version"]}).status_code == 409
    post = action(editor, path, "submit", post)
    post = action(reviewer, path, "approve", post)
    calls = []

    async def publish(token, channel, body, media):
        calls.append((token, channel, body, media))
        return {"message_id": 42, "publication_url": "https://t.me/test_channel/42"}

    monkeypatch.setattr(integrations, "publish_telegram", publish)
    finished = action(reviewer, path, "publish", post)
    assert finished["status"] == "published"
    assert calls[0][0:3] == ("synthetic-token", "@test_channel", "Пройдёт собрание.")
    assert len(calls[0][3]) == 1 and calls[0][3][0]["mime_type"] == "image/png"
    assert reviewer.post(path + "/publish", json={"version": finished["version"]}).status_code == 409
    assert len(calls) == 1


def test_missing_facts_block_review_and_confirmation_extracts_calendar(api, monkeypatch):
    client, _ = api
    configure(client, channel_id="@test_channel", openai_key="synthetic-key", requests_limit=2)
    path, post = new_post(client, text="Запланировано собрание.")

    async def generate(company, sources, instruction, credentials):
        if len(sources) == 1:
            return {"title": "Собрание", "body": "", "questions": ["Когда состоится собрание?"], "facts": [],
                    "events": [{"source_id": sources[0]["id"], "title": "Собрание", "event_date": "2027-06-14",
                                "evidence": "Запланировано собрание.", "description": ""}],
                    "input_tokens": 20, "output_tokens": 5}
        source = sources[-1]
        return {"title": "Собрание", "body": source["text"], "questions": [],
                "facts": [{"text": source["text"], "source_ids": [source["id"]],
                           "evidence": [{"source_id": source["id"], "quote": source["text"]}]}],
                "events": [{"source_id": source["id"], "title": "Собрание", "event_date": "2027-06-14",
                            "evidence": source["text"], "description": ""}],
                "input_tokens": 24, "output_tokens": 7}

    monkeypatch.setattr(integrations, "generate_post", generate)
    post = action(client, path, "generate", post)
    assert post["status"] == "needs_info" and post["questions"]
    assert client.get("/api/companies/1/events").json() == []
    assert client.post(path + "/submit", json={"version": post["version"]}).status_code == 409
    post = action(client, path, "answer", post, text="Собрание состоится 2027-06-14.")
    assert post["status"] == "draft" and not post["questions"] and len(post["source_ids"]) == 2
    event = client.get("/api/companies/1/events").json()[0]
    assert event["event_date"] == "2027-06-14" and event["source_id"] == post["source_ids"][-1]
    configure(client, calendar_enabled=False)
    assert client.get("/api/companies/1/events").json() == [event]
    assert client.post("/api/companies/1/events", json={"title": "Новое", "event_date": "2027-06-15"}).status_code == 409
    assert client.post(path + "/generate", json={"version": post["version"]}).status_code == 429
    usage = client.get("/api/companies/1/usage").json()
    assert usage["requests"] == 2 and usage["input_tokens"] == 44 and usage["output_tokens"] == 12


def test_uncertain_delivery_stays_locked_and_schedule_is_independent(api, monkeypatch):
    client, service = api
    configure(client, channel_id="@test_channel", telegram_token="synthetic-token", calendar_enabled=False)
    path, post = new_post(client)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    post = write(client, path, post, body="Пройдёт собрание.", scheduled_at=past)
    post = action(client, path, "submit", post)
    post = action(client, path, "approve", post)
    assert service.due_posts() == [{"id": post["id"], "company_id": 1, "version": post["version"]}]

    async def interrupted(token, channel, body, media):
        raise integrations.TelegramError("Истекло время ожидания.", uncertain=True)

    monkeypatch.setattr(integrations, "publish_telegram", interrupted)
    result = client.post(path + "/publish", json={"version": post["version"]})
    assert result.status_code == 502, result.text
    failed = client.get(path).json()
    assert failed["status"] == "failed" and failed["publication_uncertain"]
    assert not service.due_posts()
    assert client.patch(path, json={"version": failed["version"], "body": "Правка"}).status_code == 409
    assert client.post(path + "/publish", json={"version": failed["version"]}).status_code == 409
