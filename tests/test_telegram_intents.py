import asyncio
from unittest.mock import AsyncMock

import pytest

from server import analytics, assistant_tasks, telegram_intents
from server.db import Database
from server.domain import Service
from server.worker import Worker


COMPANY = {'id': 1, 'bot_id': 10, 'bot_username': 'editor_bot'}


@pytest.fixture
def bot(tmp_path, monkeypatch):
    db = Database(tmp_path / 'bot.sqlite3')
    db.initialize()
    service = Service(db)
    assistant_tasks.initialize_tasks(service)
    analytics.initialize_analytics(service)
    instance = Worker(service)
    with db.transaction() as conn:
        conn.execute("INSERT INTO companies(id,name) VALUES (1,'Север')")
        conn.execute("INSERT INTO users(id,name,email) VALUES (1,'Редактор','editor@example.test')")
        conn.execute("INSERT INTO memberships(company_id,user_id,role,telegram_id) VALUES (1,1,'editor','101')")
    sent = []

    async def send(_, chat, text, **kwargs):
        message_id = 1000 + len(sent)
        sent.append({'message_id': message_id, 'chat': chat, 'text': text, **kwargs})
        return {'message_id': message_id}

    instance.send = send
    monkeypatch.setattr(telegram_intents.integrations, 'telegram_call', AsyncMock())
    return instance, sent


def message(text, update_id=14, *, reply=None):
    result = {'message_id': update_id, 'text': text, 'from': {'id': 101}, 'chat': {'id': 101, 'type': 'private'}}
    if reply is not None:
        result['reply_to_message'] = {'message_id': reply['message_id']}
    return result


async def send_free(worker, text, update_id=14, *, reply=None, company=COMPANY, **fields):
    return await telegram_intents.handle_free_request(
        worker, company, 'token', {**message(text, update_id, reply=reply), **fields},
        worker.service.telegram_members(company['id'], 101), update_id,
    )


async def callback(worker, sent, index=0, *, company=COMPANY, chat=101, origin=None):
    return await telegram_intents.handle_intent_callback(
        worker, company, 'token', {
            'id': 'callback', 'from': {'id': chat},
            'message': {'chat': {'id': chat, 'type': 'private'}, 'message_id': origin or sent['message_id']},
            'data': sent['reply_markup']['inline_keyboard'][0][index]['callback_data'],
        }, worker.service.telegram_members(company['id'], chat),
    )


@pytest.mark.parametrize(('text', 'kind'), [
    ('Подбери темы для публикаций на декабрь', 'topics'),
    ('Составь план на следующую неделю', 'plan'),
    ('Разработай стратегию внутренних коммуникаций', 'strategy'),
    ('Напиши сценарий короткого видеоролика о заводе', 'video'),
    ('Сколько смен на предприятии?', 'chat'),
    ('Создай пост про Over Size и сгенерируй фото для поста', 'chat'),
    ('Подготовь пост с темой номер 5 и изображением', 'chat'),
    ('Напиши пост без изображения', 'chat'),
    ('Не создавай пост, только предложи темы', 'choice'),
    ('Предлагаю завтра встретиться с мастерами', 'choice'),
    ('Предлагаю две темы для публикаций', 'choice'),
    ('11 ноября участники подготовили программу смены', 'choice'),
    ('Не перенеси пост №10 на завтра', 'choice'),
    ('Пришли сводку за неделю', 'report_period'),
    ('Присылай отчёт по пятницам в 10:00', 'report_schedule'),
    ('Отключи отчёты', 'report_disable'),
    ('Покажи расписание отчётов', 'report_list'),
    ('Перенеси пост №12 на завтра в 15:00', 'post_schedule'),
])
def test_classifies_examples(text, kind):
    assert telegram_intents.classify_request(text) == kind


def test_natural_task_deduplicates_and_preserves_prompt(bot):
    worker, sent = bot

    async def scenario():
        request = 'Составь план на следующую неделю'
        assert await send_free(worker, '@editor_bot ' + request)
        assert await send_free(worker, '@editor_bot ' + request)
        task, = assistant_tasks.list_telegram_tasks(worker.service, 1, '10')
        assert task['kind'] == 'plan'
        assert task['prompt'] == request
        assert len(sent) == 1

    asyncio.run(scenario())


def test_ambiguous_text_has_no_side_effect_before_choice(bot):
    worker, sent = bot
    worker.handle_material = AsyncMock()

    async def scenario():
        text = 'Коллеги обсуждали план ремонта.'
        assert await send_free(worker, text)
        assert await send_free(worker, text)
        assert len(sent) == 1
        worker.handle_material.assert_not_awaited()
        assert not assistant_tasks.list_telegram_tasks(worker.service, 1, '10')
        assert await callback(worker, sent[0], origin=5555)
        worker.handle_material.assert_not_awaited()
        assert await callback(worker, sent[0])
        assert await callback(worker, sent[0])
        worker.handle_material.assert_awaited_once()
        assert worker.handle_material.await_args.args[-1][0]['text'] == text

    asyncio.run(scenario())


def test_task_choice_runs_when_callback_acknowledgment_fails(bot):
    worker, sent = bot

    async def scenario():
        assert await send_free(worker, 'Подготовь аргументы к обсуждению')
        telegram_intents.integrations.telegram_call.side_effect = telegram_intents.integrations.TelegramError('Ошибка Telegram')
        assert await callback(worker, sent[0], 1)
        task, = assistant_tasks.list_telegram_tasks(worker.service, 1, '10')
        assert task['prompt'] == 'Подготовь аргументы к обсуждению'

    asyncio.run(scenario())


def test_material_attachments_and_reply_bindings_stay_in_existing_pipeline(bot):
    worker, sent = bot
    with worker.db.transaction() as conn:
        conn.execute("INSERT INTO bot_replies VALUES(1,'101',505,1,1)")
        conn.execute("INSERT INTO bot_photo_replies VALUES(1,'101',506,1,1,'create','101','')")

    async def scenario():
        assert not await send_free(worker, '@editor_bot Новый материал')
        assert not await send_free(worker, 'Материал для поста: 11 ноября состоялась встреча')
        assert not await send_free(worker, 'Для публикации #создать* иллюстрацию')
        assert not await send_free(worker, 'Детали визита', photo=[{'file_id': 'abc'}])
        assert not await send_free(worker, 'Кто участвовал?', reply={'message_id': 505})
        assert not await send_free(worker, 'Сделай сценарий видео', reply={'message_id': 506})
        assert not sent

    asyncio.run(scenario())


def test_reschedule_clarifies_and_resets_approval_only_on_confirmation(bot):
    worker, sent = bot
    post = worker.service.create_post(1, [], title='Новость')
    pid = post['id']
    with worker.db.transaction() as conn:
        conn.execute("UPDATE posts SET status='approved',approved_snapshot='{}' WHERE id=?", (pid,))

    async def scenario():
        assert await send_free(worker, 'Перенеси пост на завтра')
        question = sent[-1]
        assert 'номер поста' in question['text'] and 'ЧЧ:ММ' in question['text']
        assert question['reply_markup']['force_reply']
        assert await send_free(worker, f'№{pid} в 15:00', 15, reply=question)
        confirm = sent[-1]
        assert '15:00' in confirm['text'] and 'Europe/Moscow' in confirm['text']
        assert worker.service.post(1, pid)['status'] == 'approved'
        assert await callback(worker, confirm)
        updated = worker.service.post(1, pid)
        assert updated['status'] == 'draft'
        assert updated['scheduled_at'].endswith('T12:00:00+00:00')
        assert updated['version'] == post['version'] + 1
        assert await callback(worker, confirm)
        assert worker.service.post(1, pid)['version'] == updated['version']

    asyncio.run(scenario())


def test_reschedule_rejects_a_stale_version_and_expired_or_revoked_callback(bot):
    worker, sent = bot
    post = worker.service.create_post(1, [], title='Новость')
    pid = post['id']
    request = f'Перенеси пост №{pid} на завтра в 15:00'

    async def scenario():
        assert await send_free(worker, request)
        worker.service.patch_post(1, pid, post['version'], {'title': 'Новый заголовок'})
        assert await callback(worker, sent[-1])
        assert 'Версия' in telegram_intents.integrations.telegram_call.await_args.args[-1]['text']
        assert await send_free(worker, request, 15)
        with worker.db.transaction() as conn:
            conn.execute('UPDATE bot_intents SET expires_at=0 WHERE update_key=?', ('u15',))
        assert await callback(worker, sent[-1])
        assert 'устарел' in telegram_intents.integrations.telegram_call.await_args.args[-1]['text']
        assert await send_free(worker, request, 16)
        with worker.db.transaction() as conn:
            conn.execute('UPDATE memberships SET active=0 WHERE company_id=1 AND user_id=1')
        assert await callback(worker, sent[-1])
        assert 'права' in telegram_intents.integrations.telegram_call.await_args.args[-1]['text']
        assert worker.service.post(1, pid)['scheduled_at'] is None

    asyncio.run(scenario())


def test_report_schedule_waits_for_time_and_confirmation(bot, monkeypatch):
    from server import reports

    worker, sent = bot
    reports.initialize_reports(worker.service)
    monkeypatch.setattr(reports, '_bot_fingerprint', lambda *_: 'test-bot')
    actor = worker.service.telegram_members(1, 101)

    async def scenario():
        assert await send_free(worker, 'Присылай отчёт по пятницам')
        assert 'ЧЧ:ММ' in sent[-1]['text']
        assert await send_free(worker, '10:00', 15, reply=sent[-1])
        confirm = sent[-1]
        assert 'пятница' in confirm['text'] and '10:00' in confirm['text']
        assert not reports.list_report_schedules(worker.service, 1, actor)
        assert await callback(worker, confirm)
        assert await callback(worker, confirm)
        schedule, = reports.list_report_schedules(worker.service, 1, actor)
        assert schedule['frequency'] == 'weekly' and schedule['weekday'] == 4 and schedule['time'] == '10:00'
        assert await send_free(worker, 'Отключи отчёты', 16)
        assert await callback(worker, sent[-1], 1)
        assert reports.list_report_schedules(worker.service, 1, actor)[0]['enabled']
        assert await send_free(worker, 'Отключи отчёты', 17)
        assert await callback(worker, sent[-1])
        assert not reports.list_report_schedules(worker.service, 1, actor)[0]['enabled']

    asyncio.run(scenario())


def test_requests_one_time_report_and_does_not_infer_today(bot):
    from server import reports

    worker, sent = bot
    reports.initialize_reports(worker.service)

    async def scenario():
        await send_free(worker, 'Пришли сводку за сегодня')
        assert sent[-1]['reply_markup']['force_reply']
        await send_free(worker, 'за неделю', 15, reply=sent[-1])
        assert 'Telegram' in sent[-1]['text']
        assert not reports.list_report_schedules(worker.service, 1, worker.service.telegram_members(1, 101))

    asyncio.run(scenario())


def test_alternative_and_different_zone_times_require_clarification(bot):
    worker, sent = bot
    pid = worker.service.create_post(1, [], title='Новость')['id']

    async def scenario():
        await send_free(worker, f'Перенеси пост №{pid} на завтра в 15:00 UTC')
        assert sent[-1]['reply_markup']['force_reply']
        assert 'Europe/Moscow' in sent[-1]['text']
        await send_free(worker, '15:00 или 28:00', 15, reply=sent[-1])
        assert sent[-1]['reply_markup']['force_reply']
        await send_free(worker, '15:00', 16, reply=sent[-1])
        assert sent[-1]['reply_markup']['inline_keyboard']

    asyncio.run(scenario())


def test_date_without_post_id_does_not_take_the_year_as_id(bot):
    worker, sent = bot

    async def scenario():
        await send_free(worker, 'Перенеси пост 2027-01-15 в 15:00')
        assert sent[-1]['reply_markup']['force_reply']
        assert 'номер поста' in sent[-1]['text']

    asyncio.run(scenario())
