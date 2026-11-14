import asyncio
import json

from server import inbox, social_platforms
from server.domain import DomainError, audit_row, utc_now
from test_backend import EDITOR, REVIEWER, SUPER, add_person, api, sign_in


PATH = "/api/companies/1/inbox"


def new_item(client):
    response = client.post(PATH, json={"platform": "vk", "kind": "question", "body": "Когда встреча?"})
    assert response.status_code == 201, response.text
    item = response.json()
    return f"{PATH}/{item['id']}", item


def test_role_checks_tenant_access_and_revision_invalidate_approval(api):
    admin, _ = api
    add_person(admin, EDITOR)
    add_person(admin, REVIEWER)
    path, item = new_item(admin)
    assert item["status"] == "new"
    assert admin.get(PATH).json()["settings"] == {"auto_replies_enabled": False}
    assert admin.get(PATH.replace("/1/", "/2/")).json()["items"] == []
    assert admin.post(path.replace("/1/", "/2/") + "/close", json={"version": item["version"]}).status_code == 404
    with sign_in(admin.app, SUPER) as owner, sign_in(admin.app, EDITOR) as editor, sign_in(admin.app, REVIEWER) as reviewer:
        assert owner.get(PATH).status_code == 403
        assert owner.put(PATH + "/settings", json={"auto_replies_enabled": True}).status_code == 403
        assert editor.put(PATH + "/settings", json={"auto_replies_enabled": True}).status_code == 403
        assert admin.put(PATH + "/settings", json={"auto_replies_enabled": True}).status_code == 422
        assert admin.get(PATH.replace("/1/", "/2/")).json()["settings"] == {"auto_replies_enabled": False}
        assert editor.get(PATH.replace("/1/", "/2/")).status_code == 404
        draft = editor.put(path + "/draft", json={"version": item["version"], "text": "Встреча завтра."}).json()
        assert draft["status"] == "draft"
        assert editor.post(path + "/approve", json={"version": draft["version"]}).status_code == 403
        assert editor.post(path + "/send", json={"version": draft["version"]}).status_code == 409
        approved = reviewer.post(path + "/approve", json={"version": draft["version"]}).json()
        assert approved["status"] == "approved"
        assert editor.post(path + "/send", json={"version": approved["version"]}).status_code == 422
        edited = editor.put(path + "/draft", json={"version": approved["version"], "text": "Встреча в пятницу."}).json()
        assert edited["status"] == "draft" and edited["approved_at"] is None
        assert admin.post(path + "/send", json={"version": approved["version"]}).status_code == 409


def test_generation_uses_active_tenant_knowledge_and_unresolved_items_cannot_be_approved(api, monkeypatch):
    admin, service = api
    path, item = new_item(admin)
    empty = admin.post(path + "/generate", json={"version": item["version"]}).json()
    assert empty["questions"] and not empty["reply"] and service.usage(1)["requests"] == 0
    assert admin.post(path + "/approve", json={"version": empty["version"]}).status_code == 422
    assert admin.patch("/api/companies/1", json={"openai_key": "synthetic-key"}).status_code == 200
    seed = {"title": "Встреча", "category": "faq", "status": "active", "text": "Встреча назначена на пятницу."}
    source = admin.post("/api/companies/1/knowledge", json=seed).json()
    assert admin.post("/api/companies/2/knowledge", json={**seed, "text": "Только другая компания."}).status_code == 201

    async def draft(endpoint, model, headers, body, facts):
        assert body == "Когда встреча?"
        assert facts == {source["source_id"]: {"title": "Встреча", "text": seed["text"]}}
        return {"status": "completed", "usage": {"input_tokens": 20, "output_tokens": 13},
                "output_text": json.dumps({"text": seed["text"], "questions": [], "evidence": [
                    {"source_id": source["source_id"], "quote": seed["text"]}]})}

    monkeypatch.setattr(inbox, "_request_draft", draft)
    generated = admin.post(path + "/generate", json={"version": empty["version"]}).json()
    assert generated["reply"] == seed["text"]
    assert generated["evidence"][0]["title"] == "Встреча"
    assert service.usage(1)["input_tokens"] == 20
    assert admin.delete(f"/api/companies/1/knowledge/{source['id']}").status_code == 204
    assert admin.post(path + "/approve", json={"version": generated["version"]}).status_code == 409


def test_generation_claim_serializes_provider_calls(api, monkeypatch):
    admin, service = api
    _, item = new_item(admin)
    assert admin.patch("/api/companies/1", json={"openai_key": "synthetic-key"}).status_code == 200
    seed = {"title": "Встреча", "category": "faq", "status": "active", "text": "Встреча в пятницу."}
    assert admin.post("/api/companies/1/knowledge", json=seed).status_code == 201
    actor = admin.get("/api/auth/me").json()

    async def run_concurrent():
        started, release = asyncio.Event(), asyncio.Event()

        async def generate(*args):
            started.set()
            await release.wait()
            return {"status": "completed", "output_text": json.dumps({"text": "", "questions": ["Укажите время."], "evidence": []})}

        monkeypatch.setattr(inbox, "_request_draft", generate)
        payload = inbox.Version(version=item["version"])
        first = asyncio.create_task(inbox.generate_draft(service, 1, item["id"], payload, actor))
        await started.wait()
        try:
            await asyncio.wait_for(inbox.generate_draft(service, 1, item["id"], payload, actor), timeout=1)
        except DomainError as error:
            assert error.status_code == 409
        finally:
            release.set()
        return await first

    draft = asyncio.run(run_concurrent())
    assert draft["questions"] and not draft["generating"]
    assert service.usage(1)["requests"] == 1


def test_sync_deduplicates_and_delivery_requires_version_approval(api, monkeypatch):
    admin, service = api
    social = {"enabled": True, "configured": True, "destination_id": "-123", "token": "synthetic"}
    monkeypatch.setattr(social_platforms, "get_social_connection", lambda *args, **kw: social)

    provider_comments = [
        {"text": "Как попасть на встречу?", "created_at": "2026-01-01T10:00:00+00:00", "id": "17", "author_id": "42"},
        {"text": "Наша публикация", "id": "16", "author_id": "-123"},
    ]

    async def comments(*args):
        return provider_comments

    monkeypatch.setattr(social_platforms, "list_social_comments", comments)
    request = {"platform": "vk", "publication_id": "-123_1"}
    assert admin.post(PATH + "/sync", json=request).json() == {"imported": 1}
    assert admin.post(PATH + "/sync", json=request).json() == {"imported": 0}
    synced, = admin.get(PATH).json()["items"]
    path = f"{PATH}/{synced['id']}"
    draft = admin.put(path + "/draft", json={"version": synced["version"], "text": "Через главную проходную."}).json()
    approved = admin.post(path + "/approve", json={"version": draft["version"]}).json()
    calls = []

    async def send(platform, connection, publication_id, comment_id, text):
        calls.append((publication_id, comment_id, text))
        with service.db.read() as connection:
            claimed = connection.execute("SELECT send_claim_at,version FROM inbox_items WHERE id=?", (synced["id"],)).fetchone()
        assert claimed["send_claim_at"] and claimed["version"] == approved["version"] + 1
        return {"message_id": "18"}

    monkeypatch.setattr(social_platforms, "send_social_reply", send)
    assert admin.post(path + "/send", json={"version": draft["version"]}).status_code == 409
    response = admin.post(path + "/send", json={"version": approved["version"]})
    assert response.status_code == 200, response.text
    sent = response.json()
    assert sent["status"] == "sent" and sent["reply_external_id"] == "18"
    assert admin.post(path + "/send", json={"version": sent["version"]}).status_code == 409
    assert calls == [("-123_1", "17", "Через главную проходную.")]
    provider_comments.extend([{"text": "Через главную проходную.", "id": "18"},
                              {"text": "От бота", "id": "19", "is_own": True}])
    assert admin.post(PATH + "/sync", json=request).json() == {"imported": 0}


def test_restart_marks_interrupted_sends_uncertain(api):
    admin, service = api
    path, item = new_item(admin)
    with service.db.transaction() as connection:
        connection.execute("UPDATE inbox_items SET status='approved', send_claim_at='2026-01-01' WHERE id=?", (item["id"],))
    inbox.recover_inbox_sends(service)
    pending, = admin.get(PATH).json()["items"]
    assert pending["send_uncertain"] and not pending["sending"]
    assert admin.post(path + "/send", json={"version": pending["version"]}).status_code == 409
    closed = admin.post(path + "/close", json={"version": pending["version"]}).json()
    assert closed["status"] == "closed" and closed["sent_at"] is None


def test_automatic_delivery_requires_selected_active_faq_and_exact_external_comment(api, monkeypatch):
    admin, service = api
    _, manual = new_item(admin)
    social = {"enabled": True, "configured": True, "destination_id": "-123", "token": "synthetic"}
    monkeypatch.setattr(social_platforms, "get_social_connection", lambda *args, **kw: social)
    comments = [{"id": "11", "text": "Когда встреча?"}, {"id": "12", "text": "Когда встреча? Нужна запись?"}]

    async def read(*args):
        return comments

    sent = []

    async def send(platform, connection, publication_id, comment_id, text):
        sent.append((comment_id, text))
        return {"message_id": "13"}

    monkeypatch.setattr(social_platforms, "list_social_comments", read)
    monkeypatch.setattr(social_platforms, "send_social_reply", send)
    request = {"platform": "vk", "publication_id": "-123_1"}
    assert admin.post(PATH + "/sync", json=request).status_code == 201
    knowledge = "/api/companies/1/knowledge"
    seed = {"title": "Когда встреча?", "text": "Встреча в пятницу.", "category": "faq", "status": "active"}
    faq = admin.post(knowledge, json=seed).json()
    asyncio.run(inbox.auto_process_inbox(service, 1))
    assert sent == []
    settings = {"auto_replies_enabled": True, "faq_source_ids": [faq["source_id"]]}
    assert admin.put(PATH + "/settings", json=settings).status_code == 200
    asyncio.run(inbox.auto_process_inbox(service, 1))
    asyncio.run(inbox.auto_process_inbox(service, 1))
    assert sent == [("11", seed["text"])]
    matching, = [item for item in admin.get(PATH).json()["items"] if item["external_id"] == "11"]
    assert matching["status"] == "sent" and matching["evidence"][0]["source_id"] == faq["source_id"]
    assert matching["id"] != manual["id"]

    revised = admin.patch(knowledge + f"/{faq['id']}", json={"text": "Новый ответ."}).json()
    assert revised["source_id"] != faq["source_id"]
    comments.append({"id": "14", "text": "Когда встреча?"})
    assert admin.post(PATH + "/sync", json=request).status_code == 201
    asyncio.run(inbox.auto_process_inbox(service, 1))
    assert sent == [("11", seed["text"])]


def test_monitor_only_updates_recent_published_targets_when_enabled(api, monkeypatch):
    admin, service = api
    social = {"enabled": True, "configured": True, "destination_id": "-123", "token": "synthetic"}
    monkeypatch.setattr(social_platforms, "get_social_connection", lambda *args, **kw: social)
    calls = []

    async def metrics(platform, connection, message_id):
        calls.append((platform, message_id))
        return {"views": 7}

    async def comments(*args):
        return [{"id": "24", "text": "Комментарий к публикации"}]

    monkeypatch.setattr(social_platforms, "fetch_social_metrics", metrics)
    monkeypatch.setattr(social_platforms, "list_social_comments", comments)
    with service.db.transaction() as connection:
        for message_id, status, confirmed in (("-123_1", "published", True), ("-123_2", "published", False),
                                              ("-123_3", "approved", True)):
            post_id = connection.execute("""INSERT INTO posts(company_id,title,kind,status,source_ids,created_at,
                channel_id,template,platform,message_id) VALUES (1,'Пост','general',?,'[]',?,'-123','','vk',?)""",
                                         (status, utc_now(), message_id)).lastrowid
            if confirmed:
                audit_row(connection, 1, "post.published", f"Опубликован пост №{post_id}.")
    asyncio.run(inbox.sync_social_activity(service, 1))
    assert calls == []
    assert admin.put(PATH + "/monitor", json={"enabled": True}).status_code == 200
    asyncio.run(inbox.sync_social_activity(service, 1))
    asyncio.run(inbox.sync_social_activity(service, 1))
    assert calls == [("vk", "-123_1")]
    report = admin.get("/api/companies/1/analytics").json()
    assert report["summary"]["totals"]["views"] == 7
    data = admin.get(PATH).json()
    item, = data["items"]
    assert item["status"] == "new" and item["external_id"] == "24"
    assert data["monitor"]["last_sync"] and not data["monitor"]["last_error"]


def test_anonymous_max_comments_are_not_automatically_answered(api, monkeypatch):
    admin, service = api
    social = {'enabled': True, 'configured': True, 'destination_id': '-123', 'token': 'synthetic'}
    monkeypatch.setattr(social_platforms, 'get_social_connection', lambda *args, **kw: social)

    async def comments(*args):
        return [{'id': 'unknown-author-comment', 'text': 'Когда встреча?', 'author_id': '', 'is_own': False}]

    async def send(*args):
        raise AssertionError('An unidentified MAX author must not trigger an automatic reply.')

    monkeypatch.setattr(social_platforms, 'list_social_comments', comments)
    monkeypatch.setattr(social_platforms, 'send_social_reply', send)
    assert admin.post(PATH + '/sync', json={'platform': 'max', 'publication_id': 'post-1'}).status_code == 201
    faq = admin.post('/api/companies/1/knowledge', json={
        'title': 'Когда встреча?', 'text': 'В пятницу.', 'category': 'faq', 'status': 'active',
    }).json()
    assert admin.put(PATH + '/settings', json={
        'auto_replies_enabled': True, 'faq_source_ids': [faq['source_id']],
    }).status_code == 200
    asyncio.run(inbox.auto_process_inbox(service, 1))
    assert admin.get(PATH).json()['items'][0]['status'] == 'new'
