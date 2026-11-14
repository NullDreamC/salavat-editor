import asyncio
from unittest.mock import AsyncMock

import pytest

from server import assistant_tasks, domain, worker as worker_module
from server.db import Database
from server.domain import Service
from server.integrations import TelegramError
from server.worker import Worker


COMPANY = {'id': 1, 'bot_id': 10, 'bot_username': 'editor_bot', 'chat_id': '-100123'}


@pytest.fixture
def bot(tmp_path, monkeypatch):
    db = Database(tmp_path / 'worker.sqlite3')
    db.initialize()
    service = Service(db)
    assistant_tasks.initialize_tasks(service)
    with db.transaction() as conn:
        conn.executemany('INSERT INTO companies (id,name,channel_id) VALUES (?,?,?)', [
            (1, 'Север', '@channel'), (2, 'Юг', '@other_channel'),
        ])
        conn.executemany('INSERT INTO users (id,name,email) VALUES (?,?,?)', [
            (1, 'Анна', 'anna@example.test'), (2, 'Борис', 'boris@example.test'),
        ])
        conn.executemany('INSERT INTO memberships (company_id,user_id,role,telegram_id) VALUES (?,?,?,?)', [
            (1, 1, 'editor', '101'), (1, 2, 'approver', '102'), (2, 1, 'editor', '101'),
        ])
    instance = Worker(service)
    sent = []

    async def send(_, chat, text, **kwargs):
        message_id = 40 + len(sent)
        sent.append((chat, text, kwargs, message_id))
        return {'message_id': message_id}

    monkeypatch.setattr(instance, 'send', send)
    return instance, sent


def update(text, *, update_id=14, sender=101, chat=101, chat_type='private', reply=None):
    message = {
        'message_id': update_id,
        'from': {'id': sender},
        'chat': {'id': chat, 'type': chat_type},
        'text': text,
    }
    if reply is not None:
        message['reply_to_message'] = {'message_id': reply}
    return {'update_id': update_id, 'message': message}


def queued_tasks(bot, company=COMPANY):
    return assistant_tasks.list_telegram_tasks(bot.service, company['id'], str(company['bot_id']))


def test_commands_and_bound_replies_are_durable_and_private(bot, monkeypatch):
    worker, sent = bot
    monkeypatch.setattr(assistant_tasks, '_prepare', lambda *_: ({'name': 'Север', 'editorial_rules': ''}, {}, {}, []))
    monkeypatch.setattr(assistant_tasks, '_provider_settings', lambda *_: ('mock', 'mock', {}))
    monkeypatch.setattr(assistant_tasks, '_reserve', lambda *_: True)
    generate = AsyncMock(side_effect=[
        (assistant_tasks.Generated(answer_type='clarification', reply='', questions=['Кто выступает?'], facts=[], proposals=[]), (1, 1)),
        (assistant_tasks.Generated(answer_type='proposal', reply='Готовый ответ', questions=[], facts=[], proposals=[]), (1, 1)),
    ])
    monkeypatch.setattr(assistant_tasks, '_generate', generate)

    async def scenario():
        command = update('/ask@editor_bot Какие темы подойдут?')
        await worker.handle(COMPANY, 'token', command)
        await worker.handle(COMPANY, 'token', command)
        assert len(queued_tasks(worker)) == len(sent) == 1
        assert generate.await_count == 0
        await worker.assistant_tick(COMPANY)
        await worker.assistant_notifications(COMPANY, 'token')
        assert len(sent) == 3
        assert 'принята' in sent[0][1]
        assert 'Выполняю' in sent[1][1]
        assert 'Кто выступает?' in sent[-1][1]
        assert sent[-1][2]['reply_markup']['force_reply']
        reply = update('Директор завода', update_id=15, reply=sent[-1][3])
        await worker.handle(COMPANY, 'token', reply)
        await worker.handle(COMPANY, 'token', reply)
        stale = update('Запоздалое уточнение', update_id=16, reply=sent[2][3])
        await worker.handle(COMPANY, 'token', stale)
        assert 'более новый запрос' in sent[-1][1]
        task, = queued_tasks(worker)
        assert task['status'] == 'queued'
        assert len(assistant_tasks.task_for_delivery(worker.service, 1, task['id'])['messages']) == 3
        await worker.assistant_tick(COMPANY)
        await worker.assistant_notifications(COMPANY, 'token')
        assert 'Готовый ответ' in sent[-1][1]
        assert all(chat == 101 for chat, *_ in sent)
        restarted = Worker(worker.service)
        restarted.send = AsyncMock()
        await restarted.assistant_notifications(COMPANY, 'token')
        restarted.send.assert_not_awaited()

    asyncio.run(scenario())


def test_no_repeat_after_uncertain_result_and_revoked_membership(bot, monkeypatch):
    worker, sent = bot

    async def scenario():
        await worker.handle(COMPANY, 'token', update('/topics Темы о заводе'))
        with worker.db.transaction() as conn:
            conn.execute("UPDATE assistant_tasks SET status='completed',reply='Ответ' WHERE company_id=1")
            conn.execute('UPDATE memberships SET active=0 WHERE company_id=1 AND user_id=1')
        await worker.assistant_notifications(COMPANY, 'token')
        assert len(sent) == 1
        with worker.db.transaction() as conn:
            conn.execute('UPDATE memberships SET active=1 WHERE company_id=1 AND user_id=1')
        await worker.assistant_notifications(dict(COMPANY, bot_id=20), 'token')
        await worker.assistant_notifications(dict(COMPANY, id=2), 'token')
        assert len(sent) == 1
        uncertain_send = AsyncMock(side_effect=TimeoutError())
        monkeypatch.setattr(worker, 'send', uncertain_send)
        await worker.assistant_notifications(COMPANY, 'token')
        restarted = Worker(worker.service)
        restarted.send = uncertain_send
        await restarted.assistant_notifications(COMPANY, 'token')
        uncertain_send.assert_awaited_once()

    asyncio.run(scenario())


def test_rate_limit_defers_task_delivery_across_restarts(bot, monkeypatch):
    worker, sent = bot
    current = 1000
    monkeypatch.setattr(worker_module.time, 'time', lambda: current)

    async def scenario():
        nonlocal current
        await worker.handle(COMPANY, 'token', update('/topics Темы о заводе'))
        with worker.db.transaction() as conn:
            conn.execute("UPDATE assistant_tasks SET status='completed',reply='Ответ' WHERE company_id=1")
        send = AsyncMock(side_effect=[
            TelegramError('Rate limited', status_code=429, retry_after=20), {'message_id': 99},
        ])
        monkeypatch.setattr(worker, 'send', send)
        await worker.assistant_notifications(COMPANY, 'token')
        restarted = Worker(worker.service)
        restarted.send = send
        current += 19
        await restarted.assistant_notifications(COMPANY, 'token')
        send.assert_awaited_once()
        current += 1
        await restarted.assistant_notifications(COMPANY, 'token')
        await restarted.assistant_notifications(COMPANY, 'token')
        assert send.await_count == 2
        assert 'Ответ' in send.await_args.args[2]
        assert len(sent) == 1

    asyncio.run(scenario())


def test_opted_in_web_tasks_deliver_terminal_updates_once_to_the_original_recipient(bot, monkeypatch):
    worker, sent = bot
    token = '10:synthetic'
    monkeypatch.setattr(domain, 'store_secret', lambda value: value.encode() if value else None)
    monkeypatch.setattr(domain, 'reveal_secret', lambda value: value.decode() if value else '')
    worker.service.update_company(1, {'telegram_token': token})
    create = assistant_tasks.enqueue_task
    done = create(worker.service, 1, 1, 'chat', 'Ответь', notify_telegram=True)
    clarify = create(worker.service, 1, 1, 'chat', 'Уточни', notify_telegram=True)
    failed = create(worker.service, 1, 1, 'chat', 'Проверь', notify_telegram=True)
    revoked = create(worker.service, 1, 1, 'chat', 'Сбой', notify_telegram=True)
    ordinary = create(worker.service, 1, 1, 'chat', 'Без уведомления')
    assert done['notify_telegram'] is True
    assert all(field not in done for field in ('notify_user_id', 'notify_chat_id', 'notify_bot_id'))

    async def scenario():
        await worker.assistant_notifications(COMPANY, token)
        assert sent == []
        with worker.db.transaction() as conn:
            conn.execute("UPDATE assistant_tasks SET status='completed',reply='Готово' WHERE id IN (?, ?)",
                         (done['id'], ordinary['id']))
            conn.execute("UPDATE assistant_tasks SET status='needs_info',questions=? WHERE id=?",
                         ('["Кто выступает?"]', clarify['id']))
            conn.execute("UPDATE assistant_tasks SET status='failed',error='Нет материалов' WHERE id=?", (failed['id'],))
        await worker.assistant_notifications(dict(COMPANY, bot_id=20), token)
        await worker.assistant_notifications(dict(COMPANY, id=2), token)
        await worker.assistant_notifications(COMPANY, '10:old-token')
        assert sent == []
        await worker.assistant_notifications(COMPANY, token)
        assert len(sent) == 3
        assert all(chat == 101 for chat, *_ in sent)
        assert 'Готово' in sent[0][1]
        assert 'Кто выступает?' in sent[1][1]
        assert 'Нет материалов' in sent[2][1]
        restarted = Worker(worker.service)
        restarted.send = AsyncMock()
        await restarted.assistant_notifications(COMPANY, token)
        restarted.send.assert_not_awaited()
        with worker.db.transaction() as conn:
            conn.execute("UPDATE memberships SET telegram_id='105' WHERE company_id=1 AND user_id=1")
            conn.execute("UPDATE assistant_tasks SET status='failed',error='Сбой' WHERE id=?", (revoked['id'],))
        await worker.assistant_notifications(COMPANY, token)
        assert len(sent) == 3

    asyncio.run(scenario())


def test_uncertain_assistant_stage_stops_subsequent_delivery_chunks(bot, monkeypatch):
    worker, _ = bot

    async def scenario():
        await worker.handle(COMPANY, 'token', update('/topics Темы о заводе'))
        with worker.db.transaction() as conn:
            conn.execute("UPDATE assistant_tasks SET status='completed',reply=?", ('Текст ' * 500,))
        send = AsyncMock(side_effect=TimeoutError())
        monkeypatch.setattr(worker, 'send', send)
        await worker.assistant_notifications(COMPANY, 'token')
        await worker.assistant_notifications(COMPANY, 'token')
        send.assert_awaited_once()

    asyncio.run(scenario())


def test_task_command_does_not_dispatch_from_group_or_wrong_user(bot):
    worker, sent = bot

    async def scenario():
        group = update('/plan Материалы @editor_bot', chat=-100123, chat_type='supergroup')
        await worker.handle(COMPANY, 'token', group)
        assert sent == []
        group['message']['entities'] = [{'type': 'mention', 'offset': 16, 'length': 11}]
        await worker.handle(COMPANY, 'token', group)
        assert 'личный чат' in sent[-1][1]
        await worker.handle(COMPANY, 'token', update('/plan Материалы', sender=102, chat=102))
        assert 'Недостаточно прав' in sent[-1][1]
        await worker.handle(COMPANY, 'token', update('/plan Материалы', sender=102))
        assert len(sent) == 2
        assert queued_tasks(worker) == []

    asyncio.run(scenario())


@pytest.mark.parametrize(('state', 'edit_active', 'recipients'), [
    ('rejected', True, {'101'}), ('approved', True, {'101', '102'}),
    ('published', True, {'101', '102'}), ('failed', False, {'102'}),
])
def test_notifications_include_active_author(bot, state, edit_active, recipients):
    worker, sent = bot
    user = worker.service.telegram_members(1, 101)
    post = worker.service.create_post(1, [], actor=user)
    with worker.db.transaction() as conn:
        conn.execute("UPDATE posts SET status=?,feedback='Причина' WHERE id=?", (state, post['id']))
        conn.execute('UPDATE memberships SET active=? WHERE company_id=1 AND user_id=1', (edit_active,))
    asyncio.run(worker.notifications(COMPANY, 'token'))
    delivered = {chat for chat, *_ in sent}
    assert delivered == recipients
    if state == 'approved':
        for chat, _, payload, _ in sent:
            actions = [button['callback_data'] for row in payload['reply_markup']['inline_keyboard'] for button in row]
            assert any(action.startswith('publish:') for action in actions) == (chat == '102')
    if state == 'rejected':
        assert 'Причина' in sent[0][1]


def test_scheduled_publication_runs_without_telegram(bot, monkeypatch):
    worker, _ = bot
    monkeypatch.setattr(worker.service, 'due_posts', lambda: [{'id': 25, 'company_id': 1, 'version': 2}])
    monkeypatch.setattr(worker.service, 'credentials', lambda _: {'telegram_token': None})
    publish = AsyncMock()
    telegram = AsyncMock()
    monkeypatch.setattr(worker.service, 'publish', publish)
    monkeypatch.setattr(worker_module, 'telegram_call', telegram)
    asyncio.run(worker.tick(COMPANY))
    publish.assert_awaited_once_with(1, 25, 2, scheduled=True)
    telegram.assert_not_awaited()


def test_polling_and_routines_continue_during_paid_task(bot, monkeypatch):
    worker, _ = bot

    async def scenario():
        started = asyncio.Event()
        blocked = asyncio.Event()
        second_polled = asyncio.Event()
        polls = []
        routines = []

        async def pause_work(_, cid, **_kwargs):
            if cid == 1:
                started.set()
                await blocked.wait()

        async def tick(company):
            cid = company['id']
            polls.append(cid)
            if cid == 1 and polls.count(1) == 2:
                second_polled.set()

        async def wait_for_cycle(_):
            await sleep(0)

        sleep = asyncio.sleep
        monkeypatch.setattr(worker_module.asyncio, 'sleep', wait_for_cycle)
        monkeypatch.setattr(worker_module, 'process_tasks', pause_work)
        monkeypatch.setattr(worker_module, 'due_routines', lambda _, cid: routines.append(cid))
        monkeypatch.setattr(worker, 'tick', tick)
        monkeypatch.setattr(worker, 'inbox_tick', AsyncMock())
        runner = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.wait_for(second_polled.wait(), 2)
            assert polls.count(2) >= 2
            assert routines.count(1) >= 2
            assert not blocked.is_set()
        finally:
            runner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await runner

    asyncio.run(scenario())


@pytest.mark.parametrize(('platform', 'label'), [('vk', 'ВКонтакте'), ('max', 'MAX')])
def test_long_social_approval_has_cabinet_link_and_actions(bot, monkeypatch, platform, label):
    worker, sent = bot
    user = worker.service.telegram_members(1, 101)
    post = worker.service.create_post(1, [], actor=user)
    with worker.db.transaction() as conn:
        conn.execute(
            "UPDATE posts SET status='review',body=?,platform=? WHERE id=?", ('😀' * 5000, platform, post['id']),
        )
    monkeypatch.setattr(worker_module, 'public_url', lambda _: 'https://cabinet.example.test')
    telegram_publication = AsyncMock(side_effect=AssertionError('Never send social post through Telegram'))
    monkeypatch.setattr(worker_module, 'publish_telegram', telegram_publication)
    asyncio.run(worker.notifications(COMPANY, 'token'))
    telegram_publication.assert_not_awaited()
    (chat, text, payload, _), = sent
    assert chat == '102'
    assert label in text
    assert len(text.encode('utf-16-le')) // 2 <= 4096
    assert 'Полный пост в кабинете' in text
    rows = payload['reply_markup']['inline_keyboard']
    assert rows[0][0]['callback_data'].startswith('approve:')
    assert rows[-1][0]['web_app']['url'] == 'https://cabinet.example.test'
