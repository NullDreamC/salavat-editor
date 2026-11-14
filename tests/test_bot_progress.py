import asyncio
from unittest.mock import AsyncMock
import pytest

from server.domain import DomainError
from server.bot_progress import Progress, current_progress

from test_worker import worker, message, COMPANY, POST


def test_receipt_and_progress_arrive_before_model_reply(worker):
    bot, call = worker

    async def generate(*args, **kwargs):
        texts = [entry.args[2].get('text', '') for entry in call.await_args_list]
        assert any('Получено' in text for text in texts)
        assert any('Готовлю текст' in text for text in texts)
        return POST

    bot.service.generate.side_effect = generate
    asyncio.run(bot.handle(COMPANY, 'test-token', message(101, chat_id=101, chat_type='private', mention=False)))
    # generate_material catches failures, so also check the final visible outcome.
    assert any('Готово' in entry.args[2].get('text', '') for entry in call.await_args_list)


def test_download_failure_is_reported_before_any_model_call(worker):
    bot, call = worker
    bot.download = AsyncMock(side_effect=RuntimeError('secret must not appear'))
    update = message(101, chat_id=101, chat_type='private', mention=False)
    update['message']['photo'] = [{'file_id': 'test-photo'}]
    with pytest.raises(RuntimeError):
        asyncio.run(bot.handle(COMPANY, 'test-token', update))
    texts = [entry.args[2].get('text', '') for entry in call.await_args_list]
    assert 'Получено' in texts[0] and 'вложений — 1' in texts[0]
    assert 'Ошибка' in texts[-1]
    assert all('secret' not in text for text in texts)
    bot.service.generate.assert_not_awaited()
    assert current_progress.get() is None


def test_provider_failure_replaces_waiting_status(worker):
    bot, call = worker
    bot.service.generate.side_effect = DomainError('Месячный лимит исчерпан.', 429)
    asyncio.run(bot.handle(COMPANY, 'test-token', message(101, chat_id=101, chat_type='private', mention=False)))
    texts = [entry.args[2].get('text', '') for entry in call.await_args_list]
    assert any('Не удалось подготовить текст' in text and 'лимит' in text for text in texts)
    assert not any('Готово' in text for text in texts)


def test_status_delivery_failure_does_not_discard_material(worker):
    bot, call = worker
    calls = 0
    async def telegram(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError('network')
        return {'message_id': 42}
    call.side_effect = telegram
    asyncio.run(bot.handle(COMPANY, 'test-token', message(101, chat_id=101, chat_type='private', mention=False)))
    bot.service.generate.assert_awaited_once()
    assert any('Готово' in entry.args[2].get('text', '') for entry in call.await_args_list)


def test_heartbeat_shows_elapsed_time_and_edits_same_message():
    call = AsyncMock(return_value={'message_id': 123})
    async def run():
        status = Progress(call, 'test-token', 101, 'Получено: фото.')
        await status.update('Готовлю изображение.', 3)
        status.started -= 65
        await status.update()
    asyncio.run(run())
    assert call.await_args.args[1] == 'editMessageText'
    payload = call.await_args.args[2]
    assert payload['message_id'] == 123
    assert 'Прошло 1 мин 5 с' in payload['text']
    assert '%' not in payload['text']
