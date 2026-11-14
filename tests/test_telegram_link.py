import asyncio
from unittest.mock import AsyncMock

import pytest

from server import domain, telegram_link
from server.db import Database
from server.worker import Worker


@pytest.fixture
def service(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.sqlite3")
    db.initialize()
    monkeypatch.setattr(domain, "reveal_secret", lambda value: value.decode() if value else "")
    with db.transaction() as connection:
        connection.executemany("INSERT INTO companies(id,name,telegram_token) VALUES(?,?,?)",
                               [(1, "Компания", b"1:bot"), (2, "Другая", b"2:bot")])
        connection.executemany("INSERT INTO users(id,name,email,password_hash,is_admin) VALUES(?,?,?,?,?)",
                               [(1, "Редактор", "one@example.test", "set", 0),
                                (2, "Администратор", "two@example.test", "set", 0),
                                (3, "Суперадминистратор", "three@example.test", "set", 1)])
        connection.executemany("INSERT INTO memberships(company_id,user_id,role,telegram_id) VALUES(?,?,?,?)",
                               [(1, 1, "editor", None), (1, 2, "admin", "222"), (2, 1, "editor", None)])
    monkeypatch.setattr(telegram_link, "telegram_call", AsyncMock(return_value={"id": 10, "username": "office_bot"}))
    return domain.Service(db)


def issue(service):
    return asyncio.run(telegram_link.issue_link(service, 1, {"id": 1, "is_admin": 0}))


def test_link_is_one_time_and_scoped_to_bot_tenant_and_membership(service):
    link = issue(service)
    code = link["command"].split()[1]
    assert link["url"] == f"https://t.me/office_bot?start={code}"
    assert not telegram_link.claim_link(service, 2, 10, code, 111)
    assert not telegram_link.claim_link(service, 1, 11, code, 111)
    assert not telegram_link.claim_link(service, 1, 10, code, 222)
    assert telegram_link.claim_link(service, 1, 10, code, 111)
    assert not telegram_link.claim_link(service, 1, 10, code, 333)
    assert service.telegram_members(1, 111)["id"] == 1
    assert service.telegram_members(1, 222)["id"] == 2
    assert service.telegram_members(2, 111) is None


def test_expiry_reissue_global_exclusion_and_revocation(service, monkeypatch):
    with pytest.raises(domain.DomainError) as error:
        asyncio.run(telegram_link.issue_link(service, 1, {"id": 3, "is_admin": 1}))
    assert error.value.status_code == 403
    first = issue(service)["command"].split()[1]
    second = issue(service)["command"].split()[1]
    assert not telegram_link.claim_link(service, 1, 10, first, 111)
    with service.db.transaction() as connection:
        connection.execute("UPDATE memberships SET active=0 WHERE company_id=1 AND user_id=1")
    assert not telegram_link.claim_link(service, 1, 10, second, 111)
    with service.db.transaction() as connection:
        connection.execute("UPDATE memberships SET active=1 WHERE company_id=1 AND user_id=1")
        connection.execute("UPDATE telegram_links SET expires_at=0")
    assert not telegram_link.claim_link(service, 1, 10, second, 111)


def test_worker_handles_private_deep_link_without_storing_raw_code(service, monkeypatch):
    link = issue(service)
    update = {"message": {"chat": {"id": 111, "type": "private"}, "from": {"id": 111}, "text": link["command"]}}
    protected = telegram_link.protect_link_update(update)
    assert link["command"].split()[1] not in str(protected)
    worker = Worker(service)
    worker.send = AsyncMock()

    asyncio.run(worker.handle({"id": 1, "bot_id": 10}, "1:bot", protected))

    assert service.telegram_members(1, 111)["id"] == 1
    assert "Telegram привязан" in worker.send.await_args_list[0].args[2]
