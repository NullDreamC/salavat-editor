import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from server import integrations, telegram_observation as observation
from server.worker import Worker
from test_backend import EDITOR, SUPER, add_person, api, configure, new_post, sign_in


TOKEN = "synthetic-observation-bot"
BOT_ID = 489
CHANNEL_ID = -1001234567890
GROUP_ID = -1002222222222
OTHER_CHANNEL_ID = -1001234567891
TARGET = "@salavat_test"
PATH = "/api/companies/1/telegram-observation"
ENABLE = {"analytics_enabled": True, "comments_enabled": True}
CHANNEL_MESSAGE = 71


@pytest.fixture
def observed_channel(api, monkeypatch):
    client, service = api
    monkeypatch.delenv("SALAVAT_TELEGRAM_APP_ID", raising=False)
    monkeypatch.delenv("SALAVAT_TELEGRAM_APP_HASH", raising=False)
    configure(client, telegram_token=TOKEN, channel_id=TARGET, chat_id=str(GROUP_ID))
    _, post = new_post(client)
    now = datetime.now(timezone.utc)
    published = now - timedelta(minutes=5)
    permissions = {CHANNEL_ID: "administrator", GROUP_ID: "administrator"}
    with service.db.transaction() as connection:
        connection.execute("""UPDATE posts SET status='published',message_id=?,telegram_chat_id=?,approved_snapshot=?
            WHERE id=?""", (str(CHANNEL_MESSAGE), str(CHANNEL_ID), '{"media":[]}', post["id"]))
        connection.execute("""INSERT INTO activity (company_id,action,detail,created_at) VALUES(1,'post.published',?,?)""",
                           (f"Опубликован пост №{post['id']}.", published.isoformat()))

    async def call(token, method, payload=None):
        assert token == TOKEN
        if method == "getMe":
            return {"id": BOT_ID, "is_bot": True}
        if method == "getChat":
            chat_id = payload["chat_id"]
            if chat_id == TARGET:
                return {"id": CHANNEL_ID, "type": "channel", "linked_chat_id": GROUP_ID}
            assert chat_id == str(GROUP_ID)
            return {"id": GROUP_ID, "type": "supergroup", "linked_chat_id": CHANNEL_ID}
        if method == "getChatMember":
            assert payload["user_id"] == BOT_ID
            return {"status": permissions[int(payload["chat_id"])]}
        raise AssertionError(f"Unexpected external call {method}")

    monkeypatch.setattr(integrations, "telegram_call", call)
    return client, service, now, published, permissions


def _origin(published, *, channel_id=CHANNEL_ID):
    return {"type": "channel", "message_id": CHANNEL_MESSAGE, "chat": {"id": channel_id},
            "date": int(published.timestamp())}


def _root(published, *, channel_id=CHANNEL_ID):
    return {"chat": {"id": GROUP_ID, "type": "supergroup"}, "message_id": 400,
            "is_automatic_forward": True, "forward_origin": _origin(published, channel_id=channel_id)}


def _comment(now, *, parent=None, **fields):
    return {"chat": {"id": GROUP_ID, "type": "supergroup"}, "message_id": 401,
            "date": int(now.timestamp()), "from": {"id": 1113, "first_name": "Читатель"},
            "reply_to_message": parent or {}, "text": "Где можно посмотреть?", **fields}


def _reaction(now, *, count=6, channel_id=CHANNEL_ID, update_id=200):
    return {"update_id": update_id, "message_reaction_count": {
        "chat": {"id": channel_id, "type": "channel"}, "message_id": CHANNEL_MESSAGE,
        "date": int(now.timestamp()), "reactions": [{"total_count": count}] if count else [],
    }}


def _update(service, payload, *, cid=1, token=TOKEN):
    return asyncio.run(observation.handle_telegram_observation_update(service, cid, token, payload))


def _inbox(client, cid=1):
    return client.get(f"/api/companies/{cid}/inbox").json()["items"]


def _analytics(client, cid=1):
    return client.get(f"/api/companies/{cid}/analytics").json()


def test_live_comments_are_tenant_scoped_deduplicated_and_edits_invalidate_drafts(observed_channel):
    client, service, now, published, _ = observed_channel
    assert client.get(PATH).json()["comments_status"] == "disabled"
    assert client.patch(PATH, json=ENABLE).json()["comments_status"] == "active"
    assert _update(service, {"update_id": 1, "message": _root(published)})
    assert not _inbox(client)
    unrelated = _comment(now, parent={"message_id": 399}, message_id=399)
    assert _update(service, {"update_id": 2, "message": unrelated})
    foreign = _comment(now, parent=_root(published, channel_id=OTHER_CHANNEL_ID), message_id=402)
    assert _update(service, {"update_id": 3, "message": foreign})
    main = _comment(now, parent=_root(published))
    direct = {"update_id": 4, "message": main}
    assert _update(service, direct, token="wrong-synthetic-token")
    assert not _inbox(client)
    assert not _update(service, direct, cid=2)
    worker = Worker(service)
    asyncio.run(worker.handle({"id": 1}, TOKEN, direct))
    asyncio.run(worker.handle({"id": 1}, TOKEN, direct))
    first, = _inbox(client)
    assert first["body"] == main["text"]
    assert first["publication_id"] == f"{TARGET}:{CHANNEL_MESSAGE}"
    assert first["reference"] == "https://t.me/c/2222222222/401"
    assert not first["can_send"]
    assert _inbox(client, 2) == []
    assert _analytics(client)["items"] == []

    nested = _comment(now, parent={"message_id": 401, "chat": main["chat"]}, message_id=410, text="Ответ читателя")
    assert _update(service, {"update_id": 5, "message": nested})
    draft = client.put(f"/api/companies/1/inbox/{first['id']}/draft", json={"version": 1, "text": "Ответ"})
    assert draft.status_code == 200, draft.text
    edit = {**main, "text": "Когда можно посмотреть?", "edit_date": int(now.timestamp()) + 1}
    assert _update(service, {"update_id": 6, "edited_message": edit})
    assert _update(service, direct)
    second, first = _inbox(client)
    assert second["body"] == nested["text"]
    assert (first["body"], first["status"], first["reply"], first["version"]) == (edit["text"], "new", "", 3)
    assert client.get(PATH).json()["last_comment_at"]


def test_shared_discussion_group_keeps_authorized_employee_material_flow(observed_channel, monkeypatch):
    client, service, now, published, _ = observed_channel
    add_person(client, {**EDITOR, "telegram_id": "1113"})
    assert client.patch(PATH, json=ENABLE).json()["comments_status"] == "active"
    worker = Worker(service)
    handled = []

    async def material(company, token, user, messages):
        handled.extend(messages)

    monkeypatch.setattr(worker, "handle_material", material)
    company = {"id": 1, "chat_id": str(GROUP_ID), "bot_id": BOT_ID, "bot_username": "testbot"}
    employee = _comment(now, message_id=430, text="@testbot Материал для публикации",
                        entities=[{"type": "mention", "offset": 0, "length": 8}])
    asyncio.run(worker.handle(company, TOKEN, {"update_id": 20, "message": employee}))
    prompt_reply = {**employee, "message_id": 431, "reply_to_message": {"message_id": 429, "from": {"id": BOT_ID}}}
    asyncio.run(worker.handle(company, TOKEN, {"update_id": 21, "message": prompt_reply}))
    comment = {**employee, "message_id": 432, "reply_to_message": _root(published)}
    asyncio.run(worker.handle(company, TOKEN, {"update_id": 22, "message": comment}))
    assert [message["message_id"] for message in handled] == [430, 431]
    assert [item["external_id"] for item in _inbox(client)] == ["432"]


def test_reaction_events_report_zero_only_when_returned_and_preserve_previous_observations(observed_channel):
    client, service, now, published, _ = observed_channel
    status = client.patch(PATH, json=ENABLE).json()
    assert status["analytics_status"] == "reactions_only"
    assert _update(service, _reaction(now, count=999, channel_id=OTHER_CHANNEL_ID))
    assert not _analytics(client)["items"]
    prior = published.date().isoformat()
    payload = {"platform": "telegram", "publication_id": f"{TARGET}:{CHANNEL_MESSAGE}",
               "title": "Публикация", "format": "Текст", "source": "Статистика канала",
               "published_on": published.date().isoformat(), "observed_on": prior, "views": 981, "shares": 3}
    assert client.post("/api/companies/1/analytics/metrics", json=payload).status_code == 201
    with service.db.transaction() as connection:
        connection.execute("UPDATE analytics_metrics SET recorded_at=? WHERE company_id=1",
                           ((now - timedelta(minutes=1)).isoformat(),))
    assert _update(service, _reaction(now, count=6))
    assert _update(service, _reaction(now, count=0, update_id=201))
    assert _update(service, _reaction(now - timedelta(seconds=1), count=9, update_id=202))
    item, = _analytics(client)["items"]
    assert item["reactions"] == 0
    assert item["views"] == 981 and item["shares"] == 3
    assert item["reach"] is None and item["comments"] is None
    assert item["metric_observations"]["views"] == {"source": "Статистика канала", "observed_on": prior}
    assert item["metric_observations"]["reactions"]["source"].startswith("Telegram Bot API")
    assert _analytics(client, 2)["items"] == []
    assert client.get(PATH).json()["last_reaction_at"]


def test_settings_access_and_revocation_fail_closed(observed_channel):
    client, service, now, published, permissions = observed_channel
    add_person(client, EDITOR)
    with sign_in(client.app, EDITOR) as editor:
        assert editor.get(PATH).status_code == 200
        assert editor.patch(PATH, json=ENABLE).status_code == 403
        assert editor.post(PATH + "/sync").status_code == 403
    with sign_in(client.app, SUPER) as owner:
        assert owner.get(PATH).status_code == 403
    permissions[GROUP_ID] = "member"
    assert client.patch(PATH, json=ENABLE).json()["comments_status"] == "disconnected"
    direct = {"update_id": 10, "message": _comment(now, parent=_root(published))}
    assert _update(service, direct)
    assert not _inbox(client)
    permissions[GROUP_ID] = "administrator"
    assert client.patch(PATH, json=ENABLE).json()["comments_status"] == "active"
    assert _update(service, {"my_chat_member": {"chat": {"id": GROUP_ID},
                                              "new_chat_member": {"user": {"id": BOT_ID}, "status": "member"}}})
    assert client.get(PATH).json()["comments_status"] == "disconnected"
    assert _update(service, direct)
    assert not _inbox(client)
    assert client.patch(PATH, json={"analytics_enabled": False, "comments_enabled": False}).json()["comments_status"] == "disabled"
    assert _update(service, direct)
    assert not _inbox(client)


def test_mtproto_snapshots_use_actual_counters_and_keep_reach_unknown(observed_channel, monkeypatch):
    client, _, now, published, _ = observed_channel
    monkeypatch.setenv("SALAVAT_TELEGRAM_APP_ID", "203")
    monkeypatch.setenv("SALAVAT_TELEGRAM_APP_HASH", "a" * 32)
    monkeypatch.setattr(observation, "find_spec", lambda _: object())

    async def fetch(service, cid, token, bot_id, channel, message_ids):
        assert (cid, token, bot_id, channel, message_ids) == (1, TOKEN, BOT_ID, str(CHANNEL_ID), [CHANNEL_MESSAGE])
        message = SimpleNamespace(views=151, forwards=0, replies=SimpleNamespace(comments=True, replies=8))
        return {CHANNEL_MESSAGE: (published, observation._message_counters(message))}

    monkeypatch.setattr(observation, "fetch_mtproto_metrics", fetch)
    status = client.patch(PATH, json=ENABLE).json()
    assert status["analytics_status"] == "active" and status["last_stats_sync"]
    item, = _analytics(client)["items"]
    assert (item["views"], item["shares"], item["comments"], item["reach"], item["reactions"]) == (151, 0, 8, None, None)
    assert item["origin"] == "telegram_mtproto"
    assert status["last_comment_at"] is None
    assert TOKEN not in str(status)
