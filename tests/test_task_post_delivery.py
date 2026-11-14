import asyncio
from unittest.mock import AsyncMock

import pytest

from server import assistant_tasks, domain, worker as worker_module
from server.integrations import TelegramError
from server.worker import Worker
from test_worker_tasks import COMPANY, bot, queued_tasks


TOKEN = '10:fixture'
REQUEST = 'Создай пост по теме 5 и сгенерируй фото'
BODY = 'Готовый пост о мероприятии'


def prepare_task_post(worker, monkeypatch, *, with_photo=True, status='completed'):
    monkeypatch.setattr(domain, 'reveal_secret', lambda value: value.decode() if value else '')
    with worker.db.transaction() as conn:
        conn.execute('UPDATE companies SET telegram_token=? WHERE id=1', (TOKEN.encode(),))
        conn.execute("UPDATE memberships SET role='admin' WHERE company_id=1 AND user_id=1")
    actor = worker.service.telegram_members(1, 101)
    task = assistant_tasks.enqueue_task(
        worker.service, 1, actor, 'chat', REQUEST, telegram_bot_id='10',
        telegram_chat_id=101, telegram_update_id=15,
    )
    source = worker.service.create_source(1, 'Материалы встречи', BODY, actor=actor)
    post = worker.service.create_post(1, [source['id']], actor=actor)
    photo_id = None
    if with_photo:
        file_path = worker.db.uploads_dir / '1' / 'brand-image.png'
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b'generated branded image fixture')
        photo = worker.service.create_source(1, 'Фото с логотипом', '', actor=actor,
                                             kind='image', file_path=str(file_path), mime_type='image/png')
        photo_id = photo['id']
    with worker.db.transaction() as conn:
        conn.execute(
            'UPDATE posts SET status=?,body=?,photo_id=?,photo_base_id=?,logo_placement=? WHERE id=?',
            ('review' if status == 'completed' else 'draft', BODY, photo_id, photo_id,
             '{"variant":"primary","corner":"top_left","width":0.35}' if with_photo else None, post['id']),
        )
        conn.execute(
            'UPDATE assistant_post_requests SET post_id=?,post_version=? WHERE request_id='
            '(SELECT post_request_id FROM assistant_tasks WHERE id=?)',
            (post['id'], post['version'], task['id']),
        )
        conn.execute('UPDATE assistant_tasks SET status=?,error=? WHERE id=?',
                     (status, None if status == 'completed' else 'Генератор фото недоступен.', task['id']))
    return worker.service.post(1, post['id']), photo_id


def test_image_and_review_controls_reach_author_and_reviewer_once(bot, monkeypatch):
    worker, sent = bot
    post, photo_id = prepare_task_post(worker, monkeypatch)
    send_media = AsyncMock(return_value={'message_id': 55})
    channel = AsyncMock()
    monkeypatch.setattr(worker_module, 'publish_telegram', send_media)
    monkeypatch.setattr(worker.service, 'publish', channel)

    async def scenario():
        await worker.notifications(COMPANY, TOKEN)
        await worker.assistant_notifications(COMPANY, TOKEN)
        restarted = Worker(worker.service)
        restarted.send = AsyncMock()
        await restarted.assistant_notifications(COMPANY, TOKEN)
        await restarted.notifications(COMPANY, TOKEN)
        restarted.send.assert_not_awaited()

    asyncio.run(scenario())
    assert [payload.args[1] for payload in send_media.await_args_list] == ['102', '101']
    for payload in send_media.await_args_list:
        assert payload.args[2] == BODY
        assert [media['id'] for media in payload.args[3]] == [photo_id]
    for chat in ('102', 101):
        controls, = [message for message in sent if message[0] == chat and message[2].get('reply_markup')]
        assert f'Пост №{post["id"]}' in controls[1]
        actions = [button['callback_data'] for row in controls[2]['reply_markup']['inline_keyboard'] for button in row]
        assert actions == [f'{action}:{post["id"]}:{post["version"]}' for action in ('approve', 'reject', 'edit', 'logo')]
    channel.assert_not_awaited()


@pytest.mark.parametrize('revoked', ['inactive', 'role', 'token'])
def test_post_media_respects_current_requester_access(bot, monkeypatch, revoked):
    worker, sent = bot
    prepare_task_post(worker, monkeypatch)
    send_media = AsyncMock()
    monkeypatch.setattr(worker_module, 'publish_telegram', send_media)
    with worker.db.transaction() as conn:
        if revoked == 'inactive':
            conn.execute('UPDATE memberships SET active=0 WHERE company_id=1 AND user_id=1')
        elif revoked == 'role':
            conn.execute("UPDATE memberships SET role='approver' WHERE company_id=1 AND user_id=1")
        else:
            conn.execute('UPDATE companies SET telegram_token=? WHERE id=1', (b'10:changed',))
    asyncio.run(worker.assistant_notifications(COMPANY, TOKEN))
    send_media.assert_not_awaited()
    assert all('Пост №' not in message[1] for message in sent)


def test_uncertain_media_is_never_resent_and_author_gets_controls(bot, monkeypatch):
    worker, sent = bot
    prepare_task_post(worker, monkeypatch)
    send_media = AsyncMock(side_effect=TelegramError('Timed out', uncertain=True))
    monkeypatch.setattr(worker_module, 'publish_telegram', send_media)

    async def scenario():
        await worker.assistant_notifications(COMPANY, TOKEN)
        restarted = Worker(worker.service)
        restarted.send = AsyncMock()
        await restarted.assistant_notifications(COMPANY, TOKEN)
        restarted.send.assert_not_awaited()

    asyncio.run(scenario())
    send_media.assert_awaited_once()
    assert 'Результат отправки изображения в Telegram неизвестен.' in sent[-1][1]
    assert BODY in sent[-1][1]
    assert sent[-1][2]['reply_markup']


def test_restart_after_claim_preserves_media_at_most_once(bot, monkeypatch):
    worker, sent = bot
    post, _ = prepare_task_post(worker, monkeypatch)
    task, = queued_tasks(worker)
    media_key = worker.assistant_key(task, f'post:{post["id"]}:{post["version"]}:media')
    worker.claim(media_key)
    send_media = AsyncMock()
    monkeypatch.setattr(worker_module, 'publish_telegram', send_media)
    restarted = Worker(worker.service)
    restarted.send = worker.send
    asyncio.run(restarted.assistant_notifications(COMPANY, TOKEN))

    send_media.assert_not_awaited()
    assert 'Результат отправки изображения в Telegram неизвестен.' in sent[-1][1]
    assert sent[-1][2]['reply_markup']


def test_known_pre_send_failure_retries_only_the_unsent_stage(bot, monkeypatch):
    worker, sent = bot
    prepare_task_post(worker, monkeypatch)
    send_media = AsyncMock(return_value={'message_id': 55})
    monkeypatch.setattr(worker_module, 'publish_telegram', send_media)
    now = 1000
    monkeypatch.setattr(worker_module.time, 'time', lambda: now)
    send_controls = AsyncMock(side_effect=[
        {'message_id': 50}, TelegramError('No connection'), {'message_id': 51},
    ])
    monkeypatch.setattr(worker, 'send', send_controls)

    async def scenario():
        nonlocal now
        await worker.assistant_notifications(COMPANY, TOKEN)
        restarted = Worker(worker.service)
        restarted.send = send_controls
        now += 14
        await restarted.assistant_notifications(COMPANY, TOKEN)
        assert send_controls.await_count == 2
        now += 1
        await restarted.assistant_notifications(COMPANY, TOKEN)
        await restarted.assistant_notifications(COMPANY, TOKEN)
        assert send_controls.await_count == 3

    asyncio.run(scenario())
    send_media.assert_awaited_once()
    assert send_controls.await_args.kwargs['reply_markup']
    assert sent == []


def test_photo_failure_preserves_and_delivers_text_post(bot, monkeypatch):
    worker, sent = bot
    post, _ = prepare_task_post(worker, monkeypatch, with_photo=False, status='failed')
    send_media = AsyncMock()
    monkeypatch.setattr(worker_module, 'publish_telegram', send_media)
    asyncio.run(worker.assistant_notifications(COMPANY, TOKEN))
    send_media.assert_not_awaited()
    assert f'Пост №{post["id"]}' in sent[-1][1]
    assert BODY in sent[-1][1]
    assert 'Генератор фото недоступен.' in sent[-1][1]
    assert worker.service.post(1, post['id'])['status'] == 'draft'


def test_image_progress_is_sent_while_processing(bot, monkeypatch):
    worker, sent = bot
    prepare_task_post(worker, monkeypatch)
    with worker.db.transaction() as conn:
        conn.execute("UPDATE assistant_tasks SET status='running' WHERE company_id=1")

    async def process_tasks(service, cid, *, on_started, on_post_image):
        task, = queued_tasks(worker)
        await on_started(task)
        await on_post_image(task)
        await worker.assistant_notifications(COMPANY, TOKEN)
        await worker.assistant_notifications(COMPANY, TOKEN)

    monkeypatch.setattr(worker_module, 'process_tasks', process_tasks)
    asyncio.run(worker.assistant_tick(COMPANY))
    assert len(sent) == 3
    assert 'Готовлю изображение' in sent[-1][1]
