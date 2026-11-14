import asyncio
from unittest.mock import AsyncMock

from server import integrations, worker as worker_module
from test_backend import api, new_post, write, action
from test_worker import worker, COMPANY, POST, APPROVER


def test_approval_without_channel_cannot_publish_or_run_schedule(api, monkeypatch):
    client, service = api
    publish = AsyncMock()
    monkeypatch.setattr(integrations, 'publish_telegram', publish)
    path, post = new_post(client)
    post = write(client, path, post, title='Тест', body='Тестовый пост.', channel_id='')
    post = action(client, path, 'submit', post)
    post = action(client, path, 'approve', post)
    assert post['status'] == 'approved'
    with service.db.transaction() as connection:
        connection.execute('UPDATE posts SET scheduled_at=? WHERE id=?', ('2020-01-01T00:00:00Z', post['id']))
    assert not service.due_posts()
    response = client.post(path + '/publish', json={'version': post['version']})
    assert response.status_code == 422
    publish.assert_not_awaited()
    assert client.get(path).json()['status'] == 'approved'
    post = write(client, path, post, channel_id='@test_channel', scheduled_at=None)
    assert post['status'] == 'draft'
    assert client.post(path + '/publish', json={'version': post['version']}).status_code == 409


def test_draft_returns_photo_and_text_to_sender(worker, monkeypatch):
    bot, call = worker
    media = [{'file_path': 'test.png', 'mime_type': 'image/png'}]
    bot.service.publication_media.return_value = media
    preview = AsyncMock()
    monkeypatch.setattr(worker_module, 'publish_telegram', preview)
    asyncio.run(bot.respond(COMPANY, 'test-token', 101, POST))
    preview.assert_awaited_once_with('test-token', '101', POST['body'], media)
    assert call.await_args.args[2]['reply_markup']['inline_keyboard'][0][0]['callback_data'] == 'submit:20:3'


def test_approved_preview_has_no_publish_button(worker):
    bot, call = worker
    bot.service.posts.return_value = [dict(POST, status='approved', channel_id='', scheduled_at='2020-01-01T00:00:00Z')]
    asyncio.run(bot.notifications(COMPANY, 'test-token'))
    payload = call.await_args.args[2]
    assert payload['chat_id'] == APPROVER['telegram_id']
    buttons = [button for row in payload['reply_markup']['inline_keyboard'] for button in row]
    assert not any(button.get('callback_data', '').startswith('publish:') for button in buttons)
    assert any(button['text'] == 'Изменить' for button in buttons)
    assert 'Без публикации' in payload['text']
