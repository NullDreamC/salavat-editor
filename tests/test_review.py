import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from server import domain
from server.db import Database
from server.domain import DomainError, Service
from server.main import create_app


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, "store_secret", lambda value: value.encode() if value else None)
    monkeypatch.setattr(domain, "reveal_secret", lambda value: value.decode() if value else "")
    db = Database(tmp_path / "review.sqlite3")
    db.initialize()
    with db.transaction() as connection:
        connection.executemany("INSERT INTO companies(id,name) VALUES (?,?)", [(1, "Север"), (2, "Юг")])
        connection.execute("INSERT INTO users(id,name,email,password_hash) VALUES (10,'Редактор','editor@example.test','set')")
        connection.execute("INSERT INTO memberships(company_id,user_id,role,telegram_id) VALUES (1,10,'editor','12345')")
    return Service(db)


def test_actor_dict_is_tenant_scoped_and_audited(service):
    telegram_actor = service.telegram_members(1, 12345)

    source = service.create_source(1, "Заголовок", "Текст", actor=telegram_actor)

    with service.db.read() as connection:
        row = connection.execute("SELECT company_id,actor_id FROM activity WHERE action='source.created'").fetchone()
    assert tuple(row) == (1, 10)
    with pytest.raises(DomainError) as cross_tenant:
        service.create_source(2, "Заголовок", "Текст", actor=telegram_actor)
    assert cross_tenant.value.status_code == 403
    assert service.source(1, source["id"])["text"] == "Текст"
    assert service.sources(2) == []


def test_revoked_telegram_actor_cannot_write_with_active_web_membership(service):
    telegram_actor = service.telegram_members(1, 12345)
    service.set_telegram_member(1, 12345, active=False)

    with pytest.raises(DomainError) as error:
        service.create_source(1, "Материал", "Текст из Telegram", actor=telegram_actor)

    assert error.value.status_code == 403
    assert service.telegram_members(1, 12345) is None
    assert service.sources(1) == []
    assert service.create_source(1, "Материал", "Текст из кабинета", actor=10)["text"] == "Текст из кабинета"


def test_duplicate_bot_token_is_rejected_without_exposing_value(service):
    token = "12345:synthetic-token"
    assert service.update_company(1, {"telegram_token": token})["telegram_configured"]

    with pytest.raises(DomainError) as error:
        service.update_company(2, {"telegram_token": token})

    assert error.value.status_code == 409
    assert token not in error.value.detail
    assert service.credentials(2)["telegram_token"] == ""


def test_request_limit_reserves_before_generation_returns(service, monkeypatch):
    service.update_company(2, {"openai_key": "synthetic-test-key", "requests_limit": 1, "monthly_limit": 1})
    source = service.create_source(2, "Материал", "Завод открыл участок.")
    first = service.create_post(2, [source["id"]])
    second = service.create_post(2, [source["id"]])

    async def check_usage():
        started = asyncio.Event()
        release = asyncio.Event()

        async def complete_generation(*args):
            started.set()
            await release.wait()
            return {
                "title": "Открытие участка", "body": "Завод открыл участок.",
                "facts": [{"text": "Завод открыл участок.", "source_ids": [source["id"]],
                           "evidence": [{"source_id": source["id"], "quote": "Завод открыл участок."}]}],
                "questions": [], "events": [], "input_tokens": 20, "output_tokens": 10,
            }

        generate = AsyncMock(side_effect=complete_generation)
        monkeypatch.setattr(domain.integrations, "generate_post", generate)
        pending = asyncio.create_task(service.generate(2, first["id"], first["version"]))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            with pytest.raises(DomainError) as error:
                await service.generate(2, second["id"], second["version"])
            assert error.value.status_code == 429
        finally:
            release.set()
            await pending
        generate.assert_awaited_once()

    asyncio.run(check_usage())

    assert service.usage(2) == {
        "requests": 1, "input_tokens": 20, "output_tokens": 10, "requests_limit": 1, "monthly_limit": 1,
    }


def test_rejected_provider_response_still_records_token_usage(service, monkeypatch):
    service.update_company(2, {"openai_key": "synthetic-test-key", "monthly_limit": 0, "requests_limit": 0})
    source = service.create_source(2, "Материал", "Завод открыл участок.")
    post = service.create_post(2, [source["id"]])
    rejection = domain.integrations.GenerationError("Не найдено подтверждение факта.")
    rejection.input_tokens = 25
    rejection.output_tokens = 12
    monkeypatch.setattr(domain.integrations, "generate_post", AsyncMock(side_effect=rejection))

    with pytest.raises(DomainError) as error:
        asyncio.run(service.generate(2, post["id"], post["version"]))

    assert error.value.status_code == 502
    assert service.usage(2) == {
        "requests": 1, "input_tokens": 25, "output_tokens": 12, "requests_limit": 0, "monthly_limit": 0,
    }
    assert service.post(2, post["id"]) == post


def test_remote_browser_origin_cannot_claim_first_admin(tmp_path):
    app = create_app(Database(tmp_path / "first-run.sqlite3"))
    admin = {"name": "Администратор", "email": "admin@example.test", "password": "synthetic-test-password"}

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        rejected = client.post("/api/auth/setup", json=admin, headers={"Origin": "https://example.test"})
        assert rejected.status_code == 403
        assert client.get("/api/auth/setup").json() == {"required": True}

        created = client.post("/api/auth/setup", json=admin, headers={"Origin": "http://127.0.0.1:8000"})
        assert created.status_code == 200
        assert client.get("/api/auth/me").json()["email"] == admin["email"]
