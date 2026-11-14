from datetime import datetime, timedelta, timezone
import asyncio

from fastapi.testclient import TestClient
import pytest

from server import integrations, photo_workflow
from server.domain import DomainError
from test_backend import ADMIN, SUPER, action, api, sign_in


def test_shared_and_salavat_credentials_are_scoped_and_quota_is_hard(api, monkeypatch):
    admin, service = api
    owner = sign_in(admin.app, SUPER)
    assert admin.get("/api/shared-api").status_code == 403
    assert admin.patch("/api/shared-api", json={"openai_key": "admin-override"}).status_code == 403
    shared = owner.patch("/api/shared-api", json={"openai_key": "shared-ai-key", "yandex_key": "shared-yandex-key",
                                                "yandex_folder_id": "shared-folder"})
    assert shared.json() == {"openai_configured": True, "yandex_configured": True, "yandex_folder_id": "shared-folder"}
    assert owner.patch("/api/companies/1", json={"openai_key": "sponsor-key"}).status_code == 200
    assert admin.patch("/api/companies/2", json={"openai_key": "own-company-two-key"}).status_code == 200
    assert admin.patch("/api/companies/2", json={"funding_mode": "shared"}).status_code == 403
    assert owner.patch("/api/companies/2", json={"funding_mode": "shared"}).status_code == 422
    assert owner.patch("/api/companies/2", json={"funding_mode": "shared", "requests_limit": 1}).status_code == 200
    assert owner.patch("/api/companies/3", json={"funding_mode": "salavat", "requests_limit": 2}).status_code == 200
    assert owner.patch("/api/companies/2", json={"requests_limit": 0}).status_code == 422
    for changes in ({"requests_limit": 100}, {"openai_key": "override"}, {"yandex_folder_id": "override"}, {"funding_mode": "own"}):
        assert admin.patch("/api/companies/2", json=changes).status_code == 403
    assert admin.patch("/api/companies/1", json={"openai_key": "sponsor-override"}).status_code == 403
    assert admin.get("/api/companies/1/settings").json()["provider_credentials_editable"] is False
    assert owner.get("/api/companies/1/settings").json()["provider_credentials_editable"] is True
    calls = []

    async def models(company, credentials):
        calls.append((company["id"], company["provider"], company["yandex_folder_id"],
                      credentials["openai_key"], company["allow_openai_fallback"]))
        return ["example-model"]

    async def generate(company, sources, instruction, credentials):
        calls.append((company["id"], company["provider"], company["yandex_folder_id"],
                      credentials["openai_key"], company["allow_openai_fallback"]))
        assert "Салаватстекло" not in instruction
        return {"title": "Черновик", "body": "", "questions": ["Укажите дату"], "facts": [], "events": [],
                "input_tokens": 15, "output_tokens": 5}

    monkeypatch.setattr(integrations, "list_models", models)
    monkeypatch.setattr(integrations, "generate_post", generate)
    assert admin.get("/api/companies/3/models?provider=openai").json() == ["example-model"]
    assert admin.get("/api/companies/2/models?provider=yandex").json() == ["example-model"]
    path = "/api/companies/2"
    source = admin.post(path + "/sources", json={"title": "Задача", "text": "Нужен анонс."}).json()
    post = admin.post(path + "/posts", json={"source_ids": [source["id"]]}).json()
    post_path = f"{path}/posts/{post['id']}"
    post = action(admin, post_path, "generate", post)
    assert admin.patch(path, json={"timezone": "Pacific/Kiritimati", "model": "example-model"}).status_code == 200
    assert admin.post(post_path + "/generate", json={"version": post["version"]}).status_code == 429
    assert calls == [(3, "openai", "", "sponsor-key", True), (2, "yandex", "shared-folder", "shared-ai-key", False),
                     (2, "openai", "shared-folder", "shared-ai-key", False)]
    assert service.usage(2)["requests"] == 1 and service.usage(2)["input_tokens"] == 15
    assert service.usage(1)["requests"] == service.usage(3)["requests"] == 0
    assert "shared-ai-key" not in admin.get(path + "/settings").text
    assert "shared-folder" not in admin.get(path + "/settings").text
    owner.close()


def test_archive_preserves_data_and_disables_tenant_and_worker_access(api):
    admin, service = api
    owner = sign_in(admin.app, SUPER)
    assert owner.patch("/api/companies/2", json={"funding_mode": "salavat", "requests_limit": 2}).status_code == 200
    assert owner.delete("/api/companies/1").status_code == 409
    assert admin.delete("/api/companies/2").status_code == 403
    assert admin.patch("/api/companies/2", json={"channel_id": "@company2"}).status_code == 200
    path = "/api/companies/2"
    asset = admin.post(path + "/uploads", files={"file": ("internal.txt", b"private", "text/plain")}).json()
    assert admin.get(asset["file_url"]).content == b"private"
    post = admin.post(path + "/posts", json={"source_ids": [asset["id"]]}).json()
    post_path = f"{path}/posts/{post['id']}"
    schedule = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    post = admin.patch(post_path, json={"version": post["version"], "body": "Подтверждённый текст.",
                                       "scheduled_at": schedule}).json()
    post = action(admin, post_path, "submit", post)
    post = action(admin, post_path, "approve", post)
    invitation = owner.post(path + "/invitations", json={"role": "editor"}).json()
    assert service.due_posts() == [{"id": post["id"], "company_id": 2, "version": post["version"]}]
    with service.db.transaction() as conn:
        conn.execute("UPDATE posts SET status='publishing' WHERE id=?", (post["id"],))
    assert owner.delete(path).status_code == 409
    with service.db.transaction() as conn:
        conn.execute("UPDATE posts SET status='approved' WHERE id=?", (post["id"],))
    assert owner.delete(path).status_code == 204
    assert owner.delete(path).status_code == 404
    assert owner.get(path + "/access").status_code == 404
    assert admin.get(post_path).status_code == 404
    assert admin.get(asset["file_url"]).status_code == 404
    assert TestClient(owner.app).get(f"/api/invitations/{invitation['token']}").status_code == 410
    assert [item["id"] for item in admin.get("/api/companies").json()] == [1, 3]
    assert [item["id"] for item in owner.get("/api/companies").json()] == [1, 3]
    assert [item["id"] for item in service.companies()] == [1, 3]
    assert service.due_posts() == []
    with service.db.read() as conn:
        assert conn.execute("SELECT body FROM posts WHERE id=?", (post["id"],)).fetchone()[0] == "Подтверждённый текст."
        assert conn.execute("SELECT text FROM sources WHERE id=?", (asset["id"],)).fetchone()[0] == "private"
    assert owner.delete("/api/companies/1").status_code == 204
    assert owner.patch("/api/companies/3", json={"funding_mode": "salavat", "requests_limit": 1}).status_code == 404
    owner.close()


def test_image_and_text_share_an_atomic_monthly_reservation(api, monkeypatch):
    admin, service = api
    owner = sign_in(admin.app, SUPER)
    assert owner.patch("/api/shared-api", json={"openai_key": "shared-key"}).status_code == 200
    assert owner.patch("/api/companies/2", json={"funding_mode": "shared", "requests_limit": 1}).status_code == 200
    post = service.create_post(2, [service.create_source(2, "Тема", "Материал")['id']])
    actor = admin.get("/api/auth/me").json()
    assert actor["email"] == ADMIN["email"]
    monkeypatch.setattr(photo_workflow, "prepare_brand", lambda company: None)

    async def no_image_request(*args):
        pytest.fail("image provider must not be called after text reserves the last request")

    monkeypatch.setattr(integrations, "generate_photo", no_image_request)

    async def check_quota():
        provider_started = asyncio.Event()
        release = asyncio.Event()

        async def generate(company, sources, instruction, credentials):
            provider_started.set()
            await release.wait()
            return {"title": "Черновик", "body": "", "questions": ["Укажите дату"], "facts": [], "events": [],
                    "input_tokens": 1, "output_tokens": 1}

        monkeypatch.setattr(integrations, "generate_post", generate)
        drafting = asyncio.create_task(service.generate(2, post["id"], post["version"], actor=actor))
        await asyncio.wait_for(provider_started.wait(), timeout=5)
        try:
            with pytest.raises(DomainError) as error:
                await photo_workflow.apply_photo(service, 2, post["id"], post["version"], "generate",
                                                 "Иллюстрация к посту", actor=actor)
            assert error.value.status_code == 429
        finally:
            release.set()
            await drafting

    asyncio.run(check_quota())
    assert service.usage(2)["requests"] == 1
    owner.close()
