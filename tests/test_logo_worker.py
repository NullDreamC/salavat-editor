import asyncio
from unittest.mock import AsyncMock

import pytest

from server import photo_workflow, worker as worker_module
from server.worker import Worker
from server.logo_workflow import apply_logo, post_buttons
from test_photo_workflow import setup


def callback(data, sender=101):
    return {'id': 'logo-choice', 'from': {'id': sender}, 'data': data,
            'message': {'message_id': 50, 'chat': {'id': sender, 'type': 'private'}}}


def test_telegram_colour_corner_apply_changes_only_logo_and_stale_buttons_do_nothing(setup, monkeypatch):
    service, post, _, generation = setup
    with service.db.transaction() as c:
        c.execute("UPDATE memberships SET telegram_id='101' WHERE company_id=2 AND user_id=1")
    raw = asyncio.run(photo_workflow.apply_photo(service, 2, post['id'], post['version'], 'create', 'Фото', actor=1))
    bot = Worker(service)
    calls = AsyncMock(return_value={'message_id': 50})
    delivery = AsyncMock()
    monkeypatch.setattr(worker_module, 'telegram_call', calls)
    monkeypatch.setattr(worker_module, 'publish_telegram', delivery)
    company = {'id': 2, 'bot_username': 'example_bot'}
    asyncio.run(bot.respond(company, 'fixture-token', 101, raw))
    markup = calls.await_args.args[2]['reply_markup']['inline_keyboard']
    assert any(b['text'] == 'Белый' for row in markup for b in row)
    assert not any(b['text'] == 'На согласование' for row in markup for b in row)
    selected = f"logopick:{post['id']}:{raw['version']}:light:top_left"
    asyncio.run(bot.callback(company, 'fixture-token', callback(selected)))
    assert service.post(2, post['id']) == raw
    markup = calls.await_args.args[2]['reply_markup']['inline_keyboard']
    apply = markup[-1][0]['callback_data']
    assert apply.startswith('logoapply:')
    asyncio.run(bot.callback(company, 'fixture-token', callback(apply)))
    result = service.post(2, post['id'])
    assert result['logo'] == {'variant': 'light', 'corner': 'top_left', 'width': 0.35}
    assert result['body'] == raw['body'] and not result['logo_pending']
    assert result['version'] == raw['version'] + 2  # logo update, then automatic submission for review
    assert result['status'] == 'review'
    generation.assert_awaited_once()
    assert delivery.await_count == 2
    markup = calls.await_args.args[2]['reply_markup']['inline_keyboard']
    assert any(b['text'] == 'Изменить логотип' for row in markup for b in row)
    asyncio.run(bot.callback(company, 'fixture-token', callback(apply)))
    assert service.post(2, post['id']) == result
    assert delivery.await_count == 2


@pytest.mark.parametrize('choice', ['primary:centre', 'blue:top_left', 'light:-'])
def test_telegram_rejects_invalid_or_incomplete_logo_selection(setup, monkeypatch, choice):
    service, post, _, _ = setup
    with service.db.transaction() as c:
        c.execute("UPDATE memberships SET telegram_id='101' WHERE company_id=2 AND user_id=1")
    raw = asyncio.run(photo_workflow.apply_photo(service, 2, post['id'], post['version'], 'create', 'Фото', actor=1))
    bot = Worker(service)
    calls = AsyncMock()
    monkeypatch.setattr(worker_module, 'telegram_call', calls)
    asyncio.run(bot.callback({'id': 2}, 'fixture-token', callback(f"logoapply:{post['id']}:{raw['version']}:{choice}")))
    assert service.post(2, post['id']) == raw
    assert calls.await_args.args[1] == 'answerCallbackQuery'
    assert calls.await_args.args[2]['show_alert'] is True


@pytest.mark.parametrize('role', ['admin', 'approver'])
def test_three_review_actions_and_logo_button_edit_reply_respects_role_and_version(setup, monkeypatch, role):
    service, post, _, _ = setup
    with service.db.transaction() as c:
        c.execute('UPDATE memberships SET telegram_id=?,role=? WHERE company_id=2 AND user_id=1', ('101', role))
    # Prepare as the editor before switching their actual role.
    with service.db.transaction() as c:
        c.execute("UPDATE memberships SET role='admin' WHERE company_id=2 AND user_id=1")
    raw = asyncio.run(photo_workflow.apply_photo(service, 2, post['id'], post['version'], 'create', 'Фото', actor=1))
    branded = apply_logo(service, 2, post['id'], raw['version'], {'variant': 'light', 'corner': 'top_left', 'width': 0.35}, actor=1)
    review = service.submit(2, post['id'], branded['version'], actor=1)
    with service.db.transaction() as c:
        c.execute('UPDATE memberships SET role=? WHERE company_id=2 AND user_id=1', (role,))
    assert [b['text'] for row in post_buttons(review, role) for b in row] == ['Одобрить', 'Отклонить', 'Изменить', 'Изменить логотип']
    bot = Worker(service)
    calls = AsyncMock(return_value={'message_id': 50})
    monkeypatch.setattr(worker_module, 'telegram_call', calls)
    monkeypatch.setattr(worker_module, 'publish_telegram', AsyncMock())
    fact = {'text': 'Фотография', 'source_ids': [post['source_ids'][0]],
            'evidence': [{'source_id': post['source_ids'][0], 'quote': 'Фотография'}]}
    regenerate = AsyncMock(return_value={'title': 'Фото', 'body': 'Новый короткий текст.', 'questions': [], 'facts': [fact], 'events': [],
                                        'input_tokens': 10, 'output_tokens': 5})
    monkeypatch.setattr(photo_workflow.integrations, 'generate_post', regenerate)
    company = {'id': 2, 'bot_username': 'example_bot', 'bot_id': 12}
    asyncio.run(bot.callback(company, 'fixture-token', callback(f"edit:{post['id']}:{review['version']}")))
    update = {'message': {'message_id': 55, 'from': {'id': 101}, 'chat': {'id': 101, 'type': 'private'},
                          'text': 'Сделать текст короче.', 'reply_to_message': {'message_id': 50}}}
    asyncio.run(bot.handle(company, 'fixture-token', update))
    result = service.post(2, post['id'])
    assert result['photo_url'] == review['photo_url'] and result['logo'] == review['logo']
    if role == 'admin':
        assert result['body'] == 'Новый короткий текст.' and result['status'] == 'review'
        regenerate.assert_awaited_once()
    else:
        assert result['body'] == review['body'] and result['status'] == 'rejected'
        assert result['feedback'] == 'Сделать текст короче.'
        regenerate.assert_not_awaited()
    asyncio.run(bot.handle(company, 'fixture-token', update))
    assert service.post(2, post['id']) == result
