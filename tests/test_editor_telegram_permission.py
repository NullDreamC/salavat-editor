import asyncio
from unittest.mock import AsyncMock

from server import integrations, worker as worker_module
from server.review_policy import initialize_review_policy
from test_task_post_delivery import TOKEN, prepare_task_post
from test_worker_tasks import COMPANY, bot  # noqa: F401


def set_permission(worker, enabled):
    with worker.db.transaction() as connection:
        connection.execute("UPDATE memberships SET role='editor',can_approve=? WHERE company_id=1 AND user_id=1",
                           (enabled,))


def callback(post, action):
    return {'id': 'editor-action', 'from': {'id': 101},
            'message': {'message_id': 50, 'chat': {'id': 101, 'type': 'private'}},
            'data': f'{action}:{post["id"]}:{post["version"]}'}


def test_editor_permission_controls_telegram_approval_and_publication(bot, monkeypatch):
    worker, sent = bot
    post, _ = prepare_task_post(worker, monkeypatch)
    call = AsyncMock(return_value={'message_id': 90})
    publish = AsyncMock(return_value={'message_id': 91})
    monkeypatch.setattr(worker_module, 'telegram_call', call)
    monkeypatch.setattr(worker_module, 'publish_telegram', AsyncMock(return_value={'message_id': 89}))
    monkeypatch.setattr(integrations, 'publish_telegram', publish)

    async def scenario():
        set_permission(worker, False)
        await worker.callback(COMPANY, TOKEN, callback(post, 'approve'))
        assert worker.service.post(1, post['id'])['status'] == 'review'
        set_permission(worker, True)
        await worker.notifications(COMPANY, TOKEN)
        await worker.assistant_notifications(COMPANY, TOKEN)
        controls = [message for message in sent if str(message[0]) == '101' and message[2].get('reply_markup')]
        actions = [button['callback_data'].split(':')[0] for row in controls[-1][2]['reply_markup']['inline_keyboard']
                   for button in row]
        assert actions == ['approve', 'reject', 'edit', 'logo']
        await worker.callback(COMPANY, TOKEN, callback(post, 'approve'))
        approved = worker.service.post(1, post['id'])
        assert approved['status'] == 'approved'
        await worker.notifications(COMPANY, TOKEN)
        controls = [message for message in sent if str(message[0]) == '101']
        assert any(button['callback_data'].startswith('publish:')
                   for row in controls[-1][2]['reply_markup']['inline_keyboard'] for button in row)
        set_permission(worker, False)
        await worker.callback(COMPANY, TOKEN, callback(approved, 'publish'))
        publish.assert_not_awaited()
        assert worker.service.post(1, post['id'])['status'] == 'approved'
        set_permission(worker, True)
        await worker.callback(COMPANY, TOKEN, callback(approved, 'publish'))
        assert worker.service.post(1, post['id'])['status'] == 'published'
        publish.assert_awaited_once()
    asyncio.run(scenario())


def test_assigned_reviewers_and_revocation_apply_to_editor_buttons(bot, monkeypatch):
    worker, _ = bot
    post, _ = prepare_task_post(worker, monkeypatch)
    initialize_review_policy(worker.service)
    set_permission(worker, True)
    user = worker.service.telegram_members(1, 101)
    assert 'approve' in [b['callback_data'].split(':')[0] for row in worker.post_controls(1, post, user) for b in row]
    with worker.db.transaction() as connection:
        connection.execute('INSERT INTO company_reviewers VALUES(1,2)')
    assert 'approve' not in [b['callback_data'].split(':')[0] for row in worker.post_controls(1, post, user) for b in row]
    with worker.db.transaction() as connection:
        connection.execute('INSERT INTO company_reviewers VALUES(1,1)')
    assert 'approve' in [b['callback_data'].split(':')[0] for row in worker.post_controls(1, post, user) for b in row]
    set_permission(worker, False)
    user = worker.service.telegram_members(1, 101)
    assert 'approve' not in [b['callback_data'].split(':')[0] for row in worker.post_controls(1, post, user) for b in row]
