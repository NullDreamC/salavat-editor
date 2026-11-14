import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from server import worker as worker_module
from server.assistant_tasks import initialize_tasks
from server.db import Database
from server.worker import Worker


COMPANY = {"id": 1, "chat_id": "-100123", "bot_username": "editor_bot", "bot_id": 10}
EDITOR = {"id": 1, "role": "editor", "telegram_id": "101"}
APPROVER = {"id": 2, "role": "approver", "telegram_id": "102"}
POST = {"id": 20, "version": 3, "body": "Готовый текст", "questions": []}


@pytest.fixture
def worker(tmp_path, monkeypatch):
    service = Mock()
    service.db = Database(tmp_path / "worker.sqlite3")
    service.db.initialize()
    initialize_tasks(service)
    memberships = {(1, 101): EDITOR, (1, 102): APPROVER, (2, 101): EDITOR}
    service.telegram_members.side_effect = lambda company_id, user_id: memberships.get((company_id, user_id))
    service.telegram_users.return_value = [EDITOR, APPROVER]
    service.create_source.return_value = {"id": 30}
    service.create_post.return_value = POST
    service.generate = AsyncMock(return_value=POST)
    service.answer = AsyncMock(return_value=POST)
    service.publish = AsyncMock()
    service.publication_media.return_value = []
    call = AsyncMock(return_value={"message_id": 42})
    monkeypatch.setattr(worker_module, "telegram_call", call)
    return Worker(service), call


def message(user_id, *, chat_id=-100123, chat_type="supergroup", mention=True):
    body = {
        "message_id": 12,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": user_id},
        "text": "@editor_bot Новый материал",
    }
    if mention:
        body["entities"] = [{"type": "mention", "offset": 0, "length": len("@editor_bot")}]
    return {"message": body}


@pytest.mark.parametrize(
    "update",
    [
        message(999),
        message(101, mention=False),
        message(101, chat_id=-100456),
        message(999, chat_id=999, chat_type="private", mention=False),
    ],
    ids=["unknown-in-group", "no-mention-entity", "different-group", "unknown-in-private"],
)
def test_ignored_messages_have_no_side_effects(worker, update):
    bot, call = worker

    asyncio.run(bot.handle(COMPANY, "test-token", update))

    bot.service.create_source.assert_not_called()
    bot.service.create_post.assert_not_called()
    bot.service.generate.assert_not_awaited()
    call.assert_not_awaited()


@pytest.mark.parametrize("update", [message(101), message(101, chat_id=101, chat_type="private", mention=False)])
def test_member_can_start_draft_in_allowed_chat(worker, update):
    bot, call = worker

    asyncio.run(bot.handle(COMPANY, "test-token", update))

    bot.service.create_post.assert_called_once_with(1, [30], actor=EDITOR)
    bot.service.generate.assert_awaited_once_with(1, 20, 3, actor=EDITOR)
    assert any(entry.args[1] == 'sendMessage' and entry.args[2].get('reply_markup') for entry in call.await_args_list)
    assert call.await_args.args[2]["chat_id"] == update["message"]["chat"]["id"]


@pytest.mark.parametrize(
    ("sender", "chat_id", "chat_type", "action", "allowed"),
    [
        (999, 999, "private", "submit", False),
        (101, 101, "private", "submit", True),
        (101, 101, "private", "approve", False),
        (101, 101, "private", "reject", False),
        (101, 101, "private", "publish", False),
        (102, 102, "private", "approve", True),
        (102, -100456, "supergroup", "approve", False),
    ],
)
def test_callback_checks_member_role_and_chat(worker, sender, chat_id, chat_type, action, allowed):
    bot, call = worker
    update = {
        "callback_query": {
            "id": "callback-id",
            "from": {"id": sender},
            "message": {"message_id": 42, "chat": {"id": chat_id, "type": chat_type}},
            "data": f"{action}:20:3",
        },
    }

    asyncio.run(bot.handle(COMPANY, "test-token", update))

    for name in ("submit", "approve", "reject", "publish"):
        method = getattr(bot.service, name)
        if allowed and action == name:
            actor = EDITOR if sender == 101 else APPROVER
            method.assert_called_once_with(1, 20, 3, actor=actor)
        else:
            method.assert_not_called()
    assert call.await_args.args[1] == "answerCallbackQuery"
    assert call.await_args.args[2]["callback_query_id"] == "callback-id"


def test_clarification_reply_stays_with_own_company(worker):
    bot, _ = worker
    post = dict(POST, questions=["Где состоится встреча?"])
    update = message(101, chat_id=101, chat_type="private", mention=False)
    update["message"].update(text="Встреча в цехе", reply_to_message={"message_id": 42})

    asyncio.run(bot.respond(COMPANY, "test-token", 101, post))
    asyncio.run(bot.handle(dict(COMPANY, id=2), "test-token", update))

    bot.service.answer.assert_not_awaited()

    asyncio.run(bot.handle(COMPANY, "test-token", update))

    bot.service.answer.assert_awaited_once_with(1, 20, 3, "Встреча в цехе", actor=EDITOR)


def test_replacement_bot_reuses_update_id_without_old_reply_context(worker):
    bot, call = worker
    bot.service.posts.return_value = []
    bot.service.due_posts.return_value = []
    bot.service.credentials.side_effect = [{"telegram_token": "old-token"}, {"telegram_token": "new-token"}]
    bot.service.generate.return_value = dict(POST, questions=["Где состоится встреча?"])
    first_update = dict(message(101, chat_id=101, chat_type="private", mention=False), update_id=14)
    second_update = dict(message(101, chat_id=101, chat_type="private", mention=False), update_id=14)
    second_update["message"].update(text="Встреча в цехе", reply_to_message={"message_id": 42})
    async def response(token, method, payload=None):
        old = token == 'old-token'
        if method == 'getMe':
            return {'id': 10 if old else 20, 'username': 'editor_bot' if old else 'replacement_bot'}
        if method == 'getUpdates':
            return [first_update if old else second_update]
        if method == 'sendMessage':
            return {'message_id': 42 if old else 43}
        return True
    call.side_effect = response

    asyncio.run(bot.tick(COMPANY))
    asyncio.run(bot.tick(COMPANY))

    bot.service.generate.assert_awaited_once()
    bot.service.answer.assert_not_awaited()
    bot.service.audit.assert_not_called()
    assert any(token == 'new-token' and method == 'sendMessage' and payload.get('text') == 'Что сделать с сообщением?'
               for token, method, payload in (item.args for item in call.await_args_list if len(item.args) == 3))
    polls = [c.args for c in call.await_args_list if c.args[1] == "getUpdates"]
    assert [(token, payload["offset"]) for token, _, payload in polls] == [("old-token", 0), ("new-token", 0)]


def test_disabled_calendar_never_reads_events_or_notifies(worker):
    bot, call = worker

    asyncio.run(bot.calendar(dict(COMPANY, calendar_enabled=False), "test-token"))

    bot.service.events.assert_not_called()
    bot.service.mark_reminded.assert_not_called()
    call.assert_not_awaited()


def test_unknown_start_can_open_webapp_but_cannot_see_posts(worker):
    bot, call = worker
    (bot.db.path.parent / 'webapp.json').write_text('{"url":"https://test.example"}')
    update = message(999, chat_id=999, chat_type='private', mention=False)
    update['message']['text'] = '/start'
    asyncio.run(bot.handle(COMPANY, 'test-token', update))
    payload = call.await_args.args[2]
    assert 'одноразовую ссылку' in payload['text']
    buttons = payload['reply_markup']['inline_keyboard']
    assert buttons[0][0]['web_app']['url'] == 'https://test.example'
    assert all(b.get('callback_data') != 'menu:posts' for row in buttons for b in row)
    bot.service.posts.assert_not_called()
    bot.service.generate.assert_not_awaited()
    asyncio.run(bot.callback(COMPANY, 'test-token', {
        'id': 'cb', 'from': {'id': 999}, 'message': {'chat': {'id': 999, 'type': 'private'}}, 'data': 'menu:posts',
    }))
    bot.service.posts.assert_not_called()


def test_menu_material_never_generates_a_placeholder_post(worker):
    bot, call = worker
    asyncio.run(bot.callback(COMPANY, 'test-token', {
        'id': 'cb', 'from': {'id': 101}, 'message': {'chat': {'id': 101, 'type': 'private'}}, 'data': 'menu:material',
    }))
    assert 'Пришлите текст' in call.await_args_list[0].args[2]['text']
    bot.service.create_source.assert_not_called()
    bot.service.generate.assert_not_awaited()


def test_menu_posts_cannot_be_opened_from_another_chat(worker):
    bot, call = worker
    asyncio.run(bot.callback(COMPANY, 'test-token', {
        'id': 'cb', 'from': {'id': 101}, 'message': {'chat': {'id': -100456, 'type': 'supergroup'}}, 'data': 'menu:posts',
    }))
    bot.service.posts.assert_not_called()
    assert call.await_args.args[1] == 'answerCallbackQuery'


def test_approval_preview_uses_publication_media(worker, monkeypatch):
    bot, call = worker
    post = dict(POST, status="review", channel_id="@company", scheduled_at="2026-11-19T12:00:00Z")
    media = [{"file_path": "card.png", "mime_type": "image/png"}]
    bot.service.posts.return_value = [post]
    bot.service.publication_media.return_value = media
    preview = AsyncMock()
    monkeypatch.setattr(worker_module, "publish_telegram", preview)

    asyncio.run(bot.notifications(COMPANY, "test-token"))

    bot.service.publication_media.assert_called_once_with(1, post)
    preview.assert_awaited_once_with("test-token", APPROVER["telegram_id"], post["body"], media)
    notification = call.await_args.args[2]
    assert post["channel_id"] in notification["text"]
    assert post["scheduled_at"] in notification["text"]
    buttons = notification["reply_markup"]["inline_keyboard"][0]
    assert [button["callback_data"] for button in buttons] == ["approve:20:3", "reject:20:3", "edit:20:3"]


def test_album_from_separate_polls_creates_one_post_with_all_originals(worker, monkeypatch):
    bot, call = worker
    bot.service.credentials.return_value = {"telegram_token": "test-token"}
    bot.service.posts.return_value = []
    bot.service.due_posts.return_value = []
    bot.configure_bot = AsyncMock()
    monkeypatch.setattr(worker_module, "configure_bot", bot.configure_bot)
    clock = Mock(return_value=1000)
    monkeypatch.setattr(worker_module, "time", Mock(time=clock))
    apply = AsyncMock(return_value=dict(POST, source_ids=[30, 31, 32]))
    monkeypatch.setattr(worker_module, "apply_photo", apply)
    bot.download = AsyncMock(side_effect=[{"id": 31, "kind": "image"}, {"id": 32, "kind": "image"}])

    def album_update(update_id, caption=None):
        update = dict(message(101, mention=False), update_id=update_id)
        payload = update["message"]
        payload.pop("text")
        payload.update(media_group_id="album-1", photo=[{"file_id": str(update_id)}])
        if caption:
            payload.update(caption=caption, caption_entities=[{"type": "mention", "offset": 0, "length": len("@editor_bot")}])
        return update

    updates = [[album_update(1)], [album_update(2, "@editor_bot Встреча в цехе. #фото*")], [], [album_update(3)], []]

    async def telegram(_, method, payload=None):
        if method == "getMe":
            return {"id": 10, "username": "editor_bot"}
        if method == "getUpdates":
            return updates.pop(0)
        return {"message_id": 99}

    call.side_effect = telegram
    asyncio.run(bot.tick(COMPANY))
    clock.return_value = 1005
    asyncio.run(bot.tick(COMPANY))
    bot.service.create_post.assert_not_called()
    clock.return_value = 1010
    asyncio.run(bot.tick(COMPANY))
    clock.return_value = 1015
    asyncio.run(bot.tick(COMPANY))
    clock.return_value = 1020
    asyncio.run(bot.tick(COMPANY))

    bot.service.create_source.assert_called_once_with(1, "Встреча в цехе.", "Встреча в цехе.", actor=EDITOR)
    bot.service.create_post.assert_called_once_with(1, [30, 31, 32], actor=EDITOR)
    apply.assert_awaited_once_with(bot.service, 1, 20, 3, "original", "", actor=EDITOR)
    bot.service.generate.assert_awaited_once()
    assert bot.download.await_count == 2
    with bot.db.read() as connection:
        rows = connection.execute("SELECT status,body FROM bot_inbox ORDER BY update_id").fetchall()
    assert [(row["status"], json.loads(row["body"])) for row in rows] == [("done", {})] * 3


@pytest.mark.parametrize(("caption", "question"), [
    ("Снимок с мероприятия #фото", "Что сделать с фотографиями"),
    ("Снимок с мероприятия #изменить*", "Какое изображение"),
])
def test_photo_waits_for_exact_mode_and_instruction(worker, monkeypatch, caption, question):
    bot, call = worker
    apply = AsyncMock()
    monkeypatch.setattr(worker_module, "apply_photo", apply)
    bot.download = AsyncMock(return_value={"id": 31, "kind": "image"})
    update = message(101, chat_id=101, chat_type="private", mention=False)
    update["message"].pop("text")
    update["message"].update(caption=caption, photo=[{"file_id": "test"}])

    asyncio.run(bot.handle(COMPANY, "test-token", update))

    assert question in call.await_args.args[2]["text"]
    apply.assert_not_awaited()
    bot.service.generate.assert_not_awaited()


def test_photo_failure_preserves_edit_brief_for_retry(worker, monkeypatch):
    from server.domain import DomainError
    bot, call = worker
    monkeypatch.setattr(worker_module, 'apply_photo', AsyncMock(side_effect=DomainError('Временная ошибка', 502)))
    bot.download = AsyncMock(return_value={'id': 31, 'kind': 'image'})
    update = message(101, chat_id=101, chat_type='private', mention=False)
    update['message'].pop('text')
    update['message'].update(caption='Тестовый пост. #изменить* Сохранить детали и фирменные цвета.', photo=[{'file_id': 'test'}])
    asyncio.run(bot.handle(COMPANY, 'test-token', update))
    with bot.db.read() as connection:
        reply = connection.execute('SELECT instruction FROM bot_photo_replies').fetchone()
    assert reply['instruction'] == 'Сохранить детали и фирменные цвета.'


@pytest.mark.parametrize("delivery_fails", [False, True])
def test_notification_claim_persists_across_worker_restart(worker, delivery_fails):
    bot, call = worker
    bot.service.posts.return_value = [dict(POST, status="approved")]
    if delivery_fails:
        call.side_effect = TimeoutError("uncertain delivery")

    asyncio.run(bot.notifications(COMPANY, "test-token"))
    asyncio.run(Worker(bot.service).notifications(COMPANY, "test-token"))

    call.assert_awaited_once()
    assert call.await_args.args[1] == "sendMessage"
    assert call.await_args.args[2]["chat_id"] == APPROVER["telegram_id"]
    assert bot.service.audit.call_count == int(delivery_fails)
