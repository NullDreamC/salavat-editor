import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server import photo_workflow, worker as worker_module
from server.logo_workflow import apply_logo
from server.worker import Worker
from test_photo_workflow import png, setup


COMPANY = {'id': 2, 'chat_id': '-10042', 'bot_id': 12, 'bot_username': 'example_bot'}


@pytest.fixture
def bot_setup(setup, monkeypatch):
    service, post, _, generation = setup
    with service.db.transaction() as conn:
        conn.execute("UPDATE memberships SET telegram_id = CAST(100 + user_id AS TEXT) WHERE company_id=2")
    sent = []
    edits = []

    async def call(token, method, payload):
        if method == 'sendMessage':
            sent.append(payload)
            return {'message_id': len(sent) + 1000}
        if method == 'editMessageText':
            edits.append(payload)
        if method == 'getFile':
            return {'file_path': 'photos/own.png'}
        return True

    telegram = AsyncMock(side_effect=call)
    monkeypatch.setattr(worker_module, 'telegram_call', telegram)
    delivery = AsyncMock()
    monkeypatch.setattr(worker_module, 'publish_telegram', delivery)
    return Worker(service), post, generation, telegram, sent, edits, delivery


def buttons(payload):
    return [button for row in payload['reply_markup']['inline_keyboard'] for button in row]


async def press(bot, action, message_id, *, sender=101, chat=None, company=COMPANY):
    chat = chat or {'id': sender, 'type': 'private'}
    await bot.handle(company, 'test-token', {'callback_query': {
        'id': f'cb:{message_id}:{action}', 'from': {'id': sender}, 'data': action,
        'message': {'message_id': message_id, 'chat': chat},
    }})


async def choose(bot, sent, post, category, action, *, sender=101, chat=None):
    await press(bot, f'edit:{post["id"]}:{post["version"]}', 5, sender=sender, chat=chat)
    menu_id = 1000 + len(sent)
    assert [button['text'] for button in buttons(sent[-1])][:2] == ['Текст поста', 'Фото поста']
    await press(bot, f'pedit:{category}', menu_id, sender=sender, chat=chat)
    await press(bot, f'pedit:{action}', menu_id, sender=sender, chat=chat)
    assert sent[-1]['reply_markup']['force_reply']
    return 1000 + len(sent)


def reply(prompt_id, *, sender=101, chat=None, **fields):
    return {'message_id': 77, 'chat': chat or {'id': sender, 'type': 'private'},
            'from': {'id': sender}, 'reply_to_message': {'message_id': prompt_id}, **fields}


def test_exact_replacement_preserves_media_and_revokes_approval(bot_setup, monkeypatch):
    bot, post, generate, _, sent, edits, _ = bot_setup
    model = AsyncMock()
    monkeypatch.setattr(photo_workflow.integrations, 'generate_post', model)

    async def run():
        service = bot.service
        review = service.submit(2, post['id'], post['version'], actor=1)
        approved = service.approve(2, post['id'], review['version'], actor=2)
        prompt_id = await choose(bot, sent, approved, 'text', 'replace')
        assert [b['text'] for b in buttons(edits[-2])] == ['Описать правки', 'Заменить текст', 'Назад']
        too_long = reply(prompt_id, text='А' * 1025)
        await bot.handle(COMPANY, 'test-token', {'message': too_long})
        assert service.post(2, post['id']) == approved
        assert '1024' in sent[-1]['text']
        update = {'message': reply(prompt_id, text='Точный новый текст.\nСсылка на @example_bot.')}
        await bot.handle(COMPANY, 'test-token', update)
        current = service.post(2, post['id'])
        assert current['body'] == update['message']['text']
        assert current['status'] == 'review'
        assert current['source_ids'] == approved['source_ids']
        assert any(b['text'] == 'Изменить' for b in buttons(sent[-1]))
        assert not any(b['text'] == 'Одобрить' for b in buttons(sent[-1]))
        await bot.handle(COMPANY, 'test-token', update)
        assert service.posts(2) == [current]

    asyncio.run(run())
    generate.assert_not_awaited()
    model.assert_not_awaited()


def test_maximum_length_text_preview_keeps_the_complete_new_body(bot_setup):
    bot, _, generate, _, sent, _, _ = bot_setup
    source = bot.service.create_source(2, 'Заметка', 'Материал о событии.', actor=1)
    post = bot.service.create_post(2, [source['id']], actor=1)
    body = 'А' * 4095 + '.'

    async def run():
        prompt_id = await choose(bot, sent, post, 'text', 'replace')
        await bot.handle(COMPANY, 'test-token', {'message': reply(prompt_id, text=body)})

    asyncio.run(run())
    assert bot.service.post(2, post['id'])['body'] == body
    assert sent[-2]['text'] == body
    assert any(b['text'] == 'Изменить' for b in buttons(sent[-1]))
    generate.assert_not_awaited()


@pytest.mark.parametrize('action', ['edit', 'create'])
def test_photo_generation_uses_expected_reference_and_preserves_text(bot_setup, action):
    bot, post, generate, _, sent, edits, _ = bot_setup
    generate.return_value.update(image_bytes=png('blue'), original_bytes=png('red'),
                                 logo_placement={'variant': 'light', 'corner': 'top_left', 'width': 0.35})

    async def run():
        service = bot.service
        branded = await photo_workflow.apply_photo(service, 2, post['id'], post['version'], 'create', 'Начальное фото', actor=1)
        with bot.db.read() as conn:
            before = conn.execute('SELECT photo_base_id, photo_id FROM posts WHERE id=?', (post['id'],)).fetchone()
        assert before['photo_base_id'] != before['photo_id']
        generate.reset_mock()
        prompt_id = await choose(bot, sent, branded, 'photo', action)
        assert [b['text'] for b in buttons(edits[-2])] == [
            'Изменить текущее фото', 'Создать новое фото', 'Загрузить фото', 'Назад']
        update = {'message': reply(prompt_id, text='Светлее, нейтральный фон.')}
        await bot.handle(COMPANY, 'test-token', update)
        await bot.handle(COMPANY, 'test-token', update)
        result = service.post(2, post['id'])
        assert result['body'] == branded['body'] and result['source_ids'] == branded['source_ids']
        assert result['photo_url'] != branded['photo_url']
        assert result['status'] == 'review'
        generate.assert_awaited_once()
        refs = generate.await_args.args[1]
        assert [reference['id'] for reference in refs] == ([before['photo_base_id']] if action == 'edit' else [])

    asyncio.run(run())


def test_photo_upload_binds_group_album_and_caption_to_existing_post(bot_setup, monkeypatch):
    bot, post, generate, telegram, sent, _, delivery = bot_setup
    photo = png('green')
    monkeypatch.setattr(bot, 'download_bytes', AsyncMock(return_value=photo))
    chat = {'id': -10042, 'type': 'supergroup'}

    async def run():
        prompt_id = await choose(bot, sent, post, 'photo', 'upload', chat=chat)
        update = reply(prompt_id, chat=chat, photo=[{'file_id': 'own-photo'}], caption='@example_bot Эта подпись не попадёт в пост',
                       caption_entities=[{'type': 'mention', 'offset': 0, 'length': 12}])
        await bot.handle(COMPANY, 'test-token', {'message': dict(update, caption_entities=[])})
        assert bot.service.posts(2) == [post]
        await bot.handle_album(COMPANY, 'test-token', [update, dict(update, message_id=88, reply_to_message={})])
        assert bot.service.posts(2) == [post]
        await bot.handle(COMPANY, 'test-token', {'message': update})
        await bot.handle(COMPANY, 'test-token', {'message': update})
        result = bot.service.post(2, post['id'])
        assert bot.service.posts(2) == [result]
        assert result['body'] == post['body'] and result['source_ids'] == post['source_ids']
        assert result['logo_pending'] and result['status'] == 'draft'
        media = bot.service.publication_media(2, result)
        assert len(media) == 1 and Path(media[0]['file_path']).read_bytes() == photo
        assert any(b['text'] == 'Белый' for b in buttons(sent[-1]))
        assert any(b['text'] == 'Изменить' for b in buttons(sent[-1]))

    asyncio.run(run())
    assert len([c for c in telegram.await_args_list if c.args[1] == 'getFile']) == 1
    delivery.assert_awaited_once()
    generate.assert_not_awaited()


def test_stale_cross_actor_and_downgraded_photo_prompts_do_not_generate(bot_setup):
    bot, post, generate, telegram, sent, _, _ = bot_setup
    chat = {'id': -10042, 'type': 'supergroup'}
    mention = {'text': '@example_bot Нарисуйте новое фото.',
               'entities': [{'type': 'mention', 'offset': 0, 'length': 12}]}

    async def run():
        service = bot.service
        prompt_id = await choose(bot, sent, post, 'photo', 'create', chat=chat)
        await bot.handle(COMPANY, 'test-token', {'message': reply(prompt_id, sender=102, chat=chat, **mention)})
        with bot.db.transaction() as conn:
            conn.execute("UPDATE memberships SET role='approver' WHERE company_id=2 AND user_id=1")
        update = {'message': reply(prompt_id, chat=chat, **mention)}
        await bot.handle(COMPANY, 'test-token', update)
        assert service.posts(2) == [post]
        with bot.db.transaction() as conn:
            conn.execute("UPDATE memberships SET role='editor' WHERE company_id=2 AND user_id=1")
        changed = service.patch_post(2, post['id'], post['version'], {'body': 'Уже исправлено.'}, actor=1)
        await bot.handle(COMPANY, 'test-token', update)
        assert service.posts(2) == [changed]
        assert 'Версия публикации изменилась' in sent[-1]['text']

    asyncio.run(run())
    generate.assert_not_awaited()
    assert not any(c.args[1] == 'getFile' for c in telegram.await_args_list)


def test_approver_photo_menu_only_forwards_feedback(bot_setup):
    bot, post, generate, telegram, sent, edits, _ = bot_setup

    async def run():
        reviewed = bot.service.submit(2, post['id'], post['version'], actor=1)
        await press(bot, f'edit:{post["id"]}:{reviewed["version"]}', 5, sender=102)
        menu_id = 1000 + len(sent)
        await press(bot, 'pedit:photo', menu_id, sender=102)
        assert [b['text'] for b in buttons(edits[-1])] == ['Описать правки', 'Назад']
        await press(bot, 'pedit:create', menu_id, sender=102)
        assert telegram.await_args.args[2]['show_alert'] is True
        await press(bot, 'pedit:feedback_photo', menu_id, sender=102)
        await bot.handle(COMPANY, 'test-token', {'message': reply(1000 + len(sent), sender=102, text='Уберите отражение.')})
        current = bot.service.post(2, post['id'])
        assert current['status'] == 'rejected' and current['feedback'] == 'Уберите отражение.'
        assert current['body'] == reviewed['body'] and current['photo_url'] == reviewed['photo_url']

    asyncio.run(run())
    generate.assert_not_awaited()
