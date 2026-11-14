import asyncio
from unittest.mock import AsyncMock

import pytest

from server import integrations
from server.db import Database, SCHEMA
from server.domain import DomainError
from server.review_policy import reviewer_ids

from test_backend import EDITOR, REVIEWER, SUPER, action, add_person, api, configure, join_company, new_post, sign_in, write
from test_review_policy import uncertain_post


COMPANY = "/api/companies/1"


def test_editor_can_only_review_in_company_that_grants_permission(api, monkeypatch):
    admin, service = api
    member = add_person(admin, EDITOR)
    user_path = f"{COMPANY}/users/{member['id']}"
    policy_path = f"{COMPANY}/review-policy"
    assert member["can_approve"] is False
    join_company(admin, EDITOR, 2, "editor")

    configure(admin, channel_id="@example", telegram_token="synthetic-bot-token")
    send = AsyncMock(return_value={"message_id": 123})
    monkeypatch.setattr(integrations, "publish_telegram", send)
    path, post = new_post(admin)
    post = action(admin, path, "submit", write(admin, path, post, body="Текст на согласование."))
    with sign_in(admin.app, EDITOR) as editor:
        assert editor.post(f"{path}/approve", json={"version": post["version"]}).status_code == 403
        assert editor.patch(user_path, json={"can_approve": True}).status_code == 403
        grant = admin.patch(user_path, json={"can_approve": True})
        assert grant.status_code == 200
        assert grant.json()["can_approve"] is True
        assert [(c["id"], c["role"], c["can_approve"]) for c in editor.get("/api/companies").json()] == [
            (1, "editor", True), (2, "editor", False),
        ]
        assert editor.get(f"{COMPANY}/settings").status_code == 403
        assert editor.get(f"{COMPANY}/users").status_code == 403
        assert editor.patch(policy_path, json={"reviewer_ids": []}).status_code == 403
        assert editor.post(f"{path.replace('/companies/1/', '/companies/2/')}/approve",
                           json={"version": post["version"]}).status_code == 403

        admin_id = admin.get("/api/auth/me").json()["id"]
        assert admin.patch(policy_path, json={"reviewer_ids": [admin_id]}).status_code == 200
        assert editor.post(f"{path}/approve", json={"version": post["version"]}).status_code == 403
        response = admin.patch(policy_path, json={"reviewer_ids": [member["id"]]})
        assert response.status_code == 200
        assert {"id": member["id"], "role": "editor", "active": True} in response.json()["options"]
        with service.db.read() as connection:
            assert reviewer_ids(connection, 1) == [member["id"]]
        rejected = action(editor, path, "reject", post, comment="Нужна правка.")
        assert rejected["status"] == "rejected"
        post = action(admin, path, "submit", write(admin, path, rejected, body="Исправленный текст."))
        approved = action(editor, path, "approve", post)
        assert action(editor, path, "publish", approved)["status"] == "published"
        send.assert_awaited_once()

        path, failed = uncertain_post(admin, service, "telegram", "@example")
        assert action(editor, path, "reconcile", failed, outcome="not_published")["status"] == "draft"


def test_revocation_rechecks_stale_actor_before_publication_claim(api, monkeypatch):
    admin, service = api
    member = add_person(admin, {**EDITOR, "can_approve": True, "telegram_id": "123"})
    actor = service.telegram_members(1, 123)
    assert actor["can_approve"] is True
    configure(admin, channel_id="@example", telegram_token="synthetic-bot-token")
    send = AsyncMock()
    monkeypatch.setattr(integrations, "publish_telegram", send)
    path, post = new_post(admin)
    post = action(admin, path, "submit", write(admin, path, post, body="Текст на согласование."))
    approved = service.approve(1, post["id"], post["version"], actor=actor)

    assert admin.patch(f"{COMPANY}/users/{member['id']}", json={"can_approve": False}).json()["can_approve"] is False
    with pytest.raises(DomainError) as rejected:
        asyncio.run(service.publish(1, post["id"], approved["version"], actor=actor))
    assert rejected.value.status_code == 403
    assert admin.get(path).json()["status"] == "approved"
    send.assert_not_called()
    with service.db.read() as connection:
        assert member["id"] not in reviewer_ids(connection, 1)


@pytest.mark.parametrize("change_route", ["employee", "technical", "invitation", "telegram"])
def test_role_change_clears_grant(api, change_route):
    admin, service = api
    member = add_person(admin, {**EDITOR, "can_approve": True, "telegram_id": "123"})
    user_path = f"{COMPANY}/users/{member['id']}"
    if change_route == "employee":
        assert admin.patch(user_path, json={"role": "approver"}).json()["can_approve"] is False
    elif change_route == "technical":
        with sign_in(admin.app, SUPER) as owner:
            access_path = f"{COMPANY}/access/{member['id']}"
            assert owner.patch(access_path, json={"can_approve": True}).status_code == 422
            result = owner.patch(access_path, json={"role": "approver"})
            assert result.status_code == 200
            assert set(result.json()) == {"id", "company_id", "role", "active"}
    elif change_route == "invitation":
        join_company(admin, EDITOR, 1, "approver")
    else:
        assert service.set_telegram_member(1, 123, role="approver")["can_approve"] is False
    result = admin.patch(user_path, json={"role": "editor"})
    assert result.status_code == 200
    assert result.json()["can_approve"] is False


def test_company_admin_can_only_grant_permission_to_editor(api):
    admin, _ = api
    assert admin.post(f"{COMPANY}/users", json={**REVIEWER, "can_approve": True}).status_code == 422
    member = add_person(admin, REVIEWER)
    path = f"{COMPANY}/users/{member['id']}"
    for payload in ({"can_approve": True}, {"role": "editor", "can_approve": "true"}):
        assert admin.patch(path, json=payload).status_code == 422
    assert admin.patch(path, json={"role": "editor", "can_approve": True}).json()["can_approve"] is True
    assert next(row for row in admin.get(f"{COMPANY}/users").json() if row["id"] == member["id"])["can_approve"] is True


def test_membership_migration_defaults_existing_editors_to_no_permission(tmp_path):
    db = Database(tmp_path / "legacy.sqlite3")
    new_column = "    can_approve INTEGER NOT NULL DEFAULT 0 CHECK(can_approve=0 OR (can_approve=1 AND role='editor')),\n"
    with db.read() as connection:
        connection.executescript(SCHEMA.replace(new_column, ""))
        assert "can_approve" not in {column["name"] for column in connection.execute("PRAGMA table_info(memberships)")}
        connection.execute("INSERT INTO companies(id,name) VALUES(1,'Компания')")
        connection.execute("INSERT INTO users(id,name,email) VALUES(1,'Редактор','editor@example.test')")
        connection.execute("INSERT INTO memberships(company_id,user_id,role) VALUES(1,1,'editor')")
    db.initialize()
    db.initialize()
    with db.read() as connection:
        assert tuple(connection.execute("SELECT role,can_approve FROM memberships").fetchone()) == ("editor", 0)
