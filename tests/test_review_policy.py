from unittest.mock import AsyncMock

import pytest

from server import integrations, social_platforms
from server.review_policy import configured_reviewer_ids, reviewer_ids

from test_backend import EDITOR, REVIEWER, SUPER, action, add_person, api, new_post, sign_in, write


POLICY_PATH = "/api/companies/1/review-policy"


def test_assignment_permissions_follow_current_company_roles(api):
    admin, service = api
    reviewer = add_person(admin, REVIEWER)
    editor = add_person(admin, EDITOR)
    other = add_person(admin, {**REVIEWER, "email": "other@example.test"}, cid=2)
    admin_id = admin.get("/api/auth/me").json()["id"]

    with sign_in(admin.app, SUPER) as owner, sign_in(admin.app, REVIEWER) as approver:
        owner_id = owner.get("/api/auth/me").json()["id"]
        assert owner.get(POLICY_PATH).json() == {
            "reviewer_ids": [],
            "options": [
                {"id": admin_id, "role": "admin", "active": True},
                {"id": reviewer["id"], "role": "approver", "active": True},
            ],
        }
        assert approver.patch(POLICY_PATH, json={"reviewer_ids": []}).status_code == 403
        for invalid in (editor["id"], other["id"], owner_id, 999999):
            assert admin.patch(POLICY_PATH, json={"reviewer_ids": [invalid]}).status_code == 422

        with service.db.read() as connection:
            assert reviewer_ids(connection, 1) == [admin_id, reviewer["id"]]

        reviewer_id = reviewer["id"]
        updated = owner.patch(POLICY_PATH, json={"reviewer_ids": [reviewer_id, reviewer_id]})
        assert updated.status_code == 200
        assert updated.json()["reviewer_ids"] == [reviewer_id]
        path, post = new_post(admin)
        post = write(admin, path, post, body="Текст на согласование.")
        post = action(admin, path, "submit", post)
        assert admin.post(path + "/approve", json={"version": post["version"]}).status_code == 403
        assert admin.post(path + "/reject", json={"version": post["version"], "comment": "Правка."}).status_code == 403
        approved = action(approver, path, "approve", post)
        assert approved["status"] == "approved"

        assert admin.patch(f"/api/companies/1/users/{reviewer_id}", json={"role": "editor"}).status_code == 200
        with service.db.read() as connection:
            assert configured_reviewer_ids(connection, 1) == [reviewer_id]
            assert reviewer_ids(connection, 1) == []
        assert approver.post(path + "/reject", json={"version": approved["version"], "comment": "Правка."}).status_code == 403
        assert admin.patch(POLICY_PATH, json={"reviewer_ids": []}).json()["reviewer_ids"] == []
        rejected = action(admin, path, "reject", approved, comment="Правка.")
        assert rejected["status"] == "rejected"


def uncertain_post(client, service, platform, destination):
    path, post = new_post(client)
    with service.db.transaction() as connection:
        return path, service._update_post(connection, 1, post["id"], platform=platform, channel_id=destination,
                                          status="failed", publication_uncertain=1, approved_snapshot="{}",
                                          feedback="Проверьте канал.")


@pytest.mark.parametrize(("platform", "destination", "invalid_id", "message_id"), [
    ("telegram", "@example", "0", "12"),
    ("vk", "-20", "-21_12", "-20_12"),
    ("max", "-30", "mid.invalid/id", "mid.valid_id"),
])
def test_reconcile_as_published_is_versioned_and_sends_nothing(api, monkeypatch, platform, destination,
                                                               invalid_id, message_id):
    admin, service = api
    send = AsyncMock()
    monkeypatch.setattr(integrations, "publish_telegram", send)
    monkeypatch.setattr(social_platforms, "publish_social", send)
    path, post = uncertain_post(admin, service, platform, destination)
    url = "https://example.test/posts/12"
    version = post["version"]
    payload = {"version": version, "outcome": "published", "publication_url": url}
    endpoint = path + "/reconcile"
    assert admin.post(endpoint, json={**payload, "version": version - 1, "message_id": message_id}).status_code == 409
    assert admin.post(endpoint, json=payload).status_code == 422
    assert admin.post(endpoint, json={**payload, "message_id": invalid_id}).status_code == 422
    assert admin.post(endpoint, json={**payload, "message_id": message_id,
                                     "publication_url": "javascript:alert(1)"}).status_code == 422
    assert admin.post(endpoint, json={**payload, "message_id": message_id,
                                     "publication_url": "https://user:password@example.test/"}).status_code == 422

    reconciled = action(admin, path, "reconcile", post, outcome="published", message_id=message_id,
                        publication_url=url, note="Проверено вручную.")
    assert reconciled["status"] == "published"
    assert reconciled["version"] == version + 1
    assert reconciled["publication_uncertain"] is False
    assert reconciled["publication_url"] == url
    assert reconciled["feedback"] is None
    assert admin.post(endpoint, json={**payload, "version": reconciled["version"], "message_id": message_id}).status_code == 409
    with service.db.read() as connection:
        assert connection.execute("SELECT message_id FROM posts WHERE id=?", (post["id"],)).fetchone()[0] == message_id
        audit = connection.execute("SELECT actor_id,detail FROM activity WHERE action='post.reconciled_published'").fetchone()
        assert audit["actor_id"] == admin.get("/api/auth/me").json()["id"]
        assert "Проверено вручную." in audit["detail"]
    send.assert_not_called()


def test_reconcile_missing_post_requires_assigned_approver_and_clears_approval(api, monkeypatch):
    admin, service = api
    send = AsyncMock()
    monkeypatch.setattr(integrations, "publish_telegram", send)
    reviewer_id = add_person(admin, REVIEWER)["id"]
    path, post = uncertain_post(admin, service, "telegram", "@example")
    endpoint = path + "/reconcile"
    payload = {"version": post["version"], "outcome": "not_published"}
    assert admin.patch(POLICY_PATH, json={"reviewer_ids": [reviewer_id]}).status_code == 200
    assert admin.post(endpoint, json=payload).status_code == 403
    with sign_in(admin.app, SUPER) as owner, sign_in(admin.app, REVIEWER) as approver:
        assert owner.post(endpoint, json=payload).status_code == 403
        draft = action(approver, path, "reconcile", post, outcome="not_published")
    assert draft["status"] == "draft"
    assert draft["version"] == post["version"] + 1
    assert draft["publication_uncertain"] is False
    assert draft["feedback"] is None
    with service.db.read() as connection:
        assert tuple(connection.execute("SELECT approved_snapshot,message_id,publication_url FROM posts WHERE id=?",
                                        (post["id"],)).fetchone()) == (None, None, None)
    assert service.due_posts() == []
    send.assert_not_called()
