"""Route private Telegram requests and bind schedule confirmations to their sender."""

from datetime import date, datetime, time, timedelta, timezone
import json
import re
import secrets
from zoneinfo import ZoneInfo

from . import integrations
from .assistant_tasks import enqueue_task
from .domain import DomainError, EDITOR_ROLES, pack
from .photo_workflow import DIRECTIVE


PREFIX = 'intent:'
EXPIRED = 'Этот запрос уже обработан или устарел. Отправьте новый запрос.'
DENIED = 'Для этого действия нужны права редактора.'
WEEKDAYS = ('понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье')
WEEKDAY_FORMS = (
    r'понедельник(?:ам|у)?', r'вторник(?:ам|у)?', r'сред(?:ам|а|у|е)', r'четверг(?:ам|у)?',
    r'пятниц(?:ам|а|у|е)', r'суббот(?:ам|а|у|е)', r'воскресень(?:ям|е|ю)',
)
POST_ID = re.compile(r'\b(?:пост(?:а|у|е)?|публикаци[яиюе])\s*(?:№|#|номер\s*)?\s*([1-9][0-9]{0,17})\b(?![./:-]\d)')
MARKED_ID = re.compile(r'(?:№|#|\bномер\s*)\s*([1-9][0-9]{0,17})\b')
NAMED_REPORT_ID = re.compile(r'\b(?:отчет(?:а|у|е)?|сводк[аиуе]|дайджест(?:а|у|е)?)\s+([1-9][0-9]{0,17})\b(?![./:-]\d)')
BARE_ID = re.compile(r'^([1-9][0-9]{0,17})(?=\s|$)')
CLOCK = re.compile(r'(?<![\d:])([01]?[0-9]|2[0-3]):([0-5][0-9])(?![\d:])')
CLOCK_LIKE = re.compile(r'(?<![\d:])\d{1,2}:\d{1,2}(?![\d:])')
DATE = re.compile(r'\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}(?:[./]\d{4})?)\b')
ZONE_HINT = re.compile(r'\b(?:utc|gmt|мск|по\s+москв\w+|по\s+\w+\s+времени|часов\w*\s+пояс\w*|[a-z]+/[a-z_]+)\b')
REPORT = re.compile(r'\b(?:отчет\w*|сводк\w*|дайджест\w*)\b')
MATERIAL = re.compile(r'^(?:материал\s+для\s+поста|новый\s+материал)(?:\s*[:\n]|\s*$)', re.IGNORECASE)
REQUEST_VERB = re.compile(
    r'^(?:(?:пожалуйста|можешь|можете|можно|помоги|помогите|давай)[, ]+)*'
    r'(?:предложи|предложить|подбери|подобрать|придумай|придумать|подготовь|подготовить|'
    r'составь|составить|сделай|сделать|напиши|написать|разработай|разработать|'
    r'нужен|нужна|нужны|нужно|хочу|дай|расскажи|рассказать|подскажи|подсказать|объясни|объяснить)(?:те)?\b'
)
QUESTION = re.compile(r'^(?:как|что|кто|куда|где|когда|зачем|почему|како\w+|каки\w+|сколько|можно ли)\b')
CANCEL = re.compile(r'^(?:отмена|отмени|отменить)[.!]?$')
DAILY = re.compile(r'\b(?:ежеднев\w*|каждый\s+день|раз\s+в\s+день)\b')
WEEKLY = re.compile(r'\b(?:еженедел\w*|каждую\s+неделю|раз\s+в\s+неделю)\b')
RECURRENCE = re.compile(r'\b(?:регулярн\w+|подпиш\w+|подпис\w+|расписани\w*)\b')
RECURRING_WEEKDAY = re.compile(rf"\bпо\s+(?:{'|'.join(WEEKDAY_FORMS)})\b")


def initialize_intents(service):
    with service.db.transaction() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS bot_intents (
            id TEXT PRIMARY KEY, company_id INTEGER NOT NULL, bot_id TEXT NOT NULL,
            chat_id INTEGER NOT NULL, actor_id INTEGER NOT NULL, update_key TEXT NOT NULL,
            kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
            expires_at REAL NOT NULL, message_id INTEGER,
            UNIQUE(company_id, bot_id, chat_id, update_key)
        )''')
        conn.execute('''CREATE INDEX IF NOT EXISTS bot_intent_replies
                        ON bot_intents(company_id, bot_id, chat_id, message_id)''')
        conn.execute('''CREATE INDEX IF NOT EXISTS bot_intent_expiry
                        ON bot_intents(expires_at) WHERE state='pending' ''')


def _recurring(text):
    return DAILY.search(text) or WEEKLY.search(text) or RECURRENCE.search(text) or RECURRING_WEEKDAY.search(text)


def classify_request(prompt):
    text = _normalize(prompt)
    if re.search(r'\bне\s+(?:перенес\w+|перенос\w+|передвиг\w+|передвин\w+|перепланир\w+|сдвиг\w+|сдвин\w+)\b', text):
        return 'choice'
    if (re.search(r'\b(?:перенеси(?:те)?|передвинь(?:те)?|перепланируй(?:те)?|сдвинь(?:те)?|(?:можно|надо|нужно)\s+перенести)\b', text)
            and re.search(r'\b(?:пост\w*|публикаци\w*)\b', text)):
        return 'post_schedule'
    if REPORT.search(text):
        if re.search(r'\b(?:отключ\w+|останов\w+|отмен\w+|прекрат\w+)\b|\bне\s+присылай\w*\b', text):
            return 'report_disable'
        if re.search(r'\b(?:список|расписани\w*|подписк\w*|настроенн\w*|какие\s+отчеты)\b', text) and not re.search(r'\b(?:настрой\w+|подпиш\w+|установ\w+)\b', text):
            return 'report_list'
        if _recurring(text) or re.search(r'\bкажд\w+\b', text):
            return 'report_schedule'
        if re.search(r'\b(?:пришл\w+|пришли|присыла\w+|сдела\w+|подготов\w+|покаж\w+|сформиру\w+|дай|нуж(?:ен|на|ны|но))\b', text) or text.startswith(('отчет', 'сводка', 'дайджест')):
            return 'report_period'
    from .task_posts import get_post_intent
    if get_post_intent(prompt) is not None:
        return 'chat'
    if REQUEST_VERB.search(text) or QUESTION.match(text):
        for kind, pattern in (
            ('video', r'\b(?:сценари\w*|раскадров\w*)\b[^\n]*\b(?:виде\w*|ролик\w*)\b'),
            ('strategy', r'\bстратеги\w*\b'),
            ('plan', r'\b(?:контент[\s-]+план\w*|план\s+(?:публикац\w+|пост\w*|контент\w*|на\s+\w+))\b'),
            ('topics', r'\b(?:тем[уы]|тем[ауе]ми?|тематики|идеи)\b'),
        ):
            if re.search(pattern, text):
                return kind
    if (QUESTION.match(text) and text.endswith('?')) or re.match(r'^(?:расскажи|объясни|подскажи)(?:те)?\b', text):
        return 'chat'
    return 'choice'


def _normalize(text):
    return text.strip().casefold().replace('ё', 'е')


def _editor(worker, cid, chat, user):
    current = worker.service.telegram_members(cid, chat)
    if not user or not current or current['id'] != user['id'] or current['role'] not in EDITOR_ROLES:
        raise DomainError(DENIED, 403)
    return current


def _scope(company, chat):
    return company['id'], str(company['bot_id']), chat


def _select_message(worker, scope, message_id):
    with worker.db.read() as conn:
        return conn.execute(
            'SELECT * FROM bot_intents WHERE company_id=? AND bot_id=? AND chat_id=? AND message_id=?',
            (*scope, message_id),
        ).fetchone()


def _old_material_reply(worker, scope, message_id):
    if message_id is None:
        return False
    cid, _, chat = scope
    with worker.db.read() as conn:
        return conn.execute(
            'SELECT 1 FROM bot_replies WHERE company_id=? AND chat_id=? AND message_id=? '
            'UNION SELECT 1 FROM bot_photo_replies WHERE company_id=? AND chat_id=? AND message_id=? AND telegram_id=?',
            (cid, str(chat), message_id, cid, str(chat), message_id, str(chat)),
        ).fetchone() is not None


def _reserve(worker, scope, key, actor_id, kind, payload, *, state='pending', previous=None):
    """Reserve a Telegram update before sending its response or changing state."""
    intent_id = secrets.token_urlsafe(12)
    now = datetime.now(timezone.utc).timestamp()
    with worker.db.transaction() as conn:
        # Keep the update and reply bindings while removing expired drafts from their payloads.
        conn.execute("UPDATE bot_intents SET state='done',payload='{}' WHERE state='pending' AND expires_at<=?", (now,))
        if conn.execute('SELECT 1 FROM bot_intents WHERE company_id=? AND bot_id=? AND chat_id=? AND update_key=?', (*scope, key)).fetchone():
            return None
        if previous is not None and not conn.execute(
            "UPDATE bot_intents SET state='done',payload='{}' WHERE id=? AND state='pending' AND expires_at>?",
            (previous, now),
        ).rowcount:
            return None
        conn.execute(
            'INSERT INTO bot_intents (id,company_id,bot_id,chat_id,actor_id,update_key,kind,payload,state,expires_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (intent_id, *scope, actor_id, key, kind, pack(payload), state, now + 1800),
        )
    return intent_id


def _buttons(intent_id, choices):
    return {'inline_keyboard': [[{'text': label, 'callback_data': f'{PREFIX}{intent_id}:{action}'}
                                  for action, label in choices]]}


async def _request(worker, token, scope, key, user, kind, payload, text, *, previous=None, choices=()):
    intent_id = _reserve(worker, scope, key, user['id'], kind, payload, previous=previous)
    if not intent_id:
        return
    markup = _buttons(intent_id, choices) if choices else {'force_reply': True, 'selective': True}
    sent = await worker.send(token, scope[2], text, reply_markup=markup)
    with worker.db.transaction() as conn:
        conn.execute('UPDATE bot_intents SET message_id=? WHERE id=?', (sent['message_id'], intent_id))


async def _notice(worker, token, scope, key, user, text, *, previous=None):
    if _reserve(worker, scope, key, user['id'], 'notice', {}, state='done', previous=previous):
        await worker.send(token, scope[2], text)


def _local_datetime(day, clock, zone):
    naive = datetime.combine(day, time.fromisoformat(clock))
    first, second = naive.replace(tzinfo=zone), naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise DomainError('Это местное время попадает на переход часового пояса. Укажите другое время.', 422)
    return first


def _selection(text, previous, pattern, transform):
    found = set(pattern.findall(text))
    if len(found) > 1:
        return None
    if found:
        return transform(found.pop())
    return previous


def _date_selection(text, previous, today):
    values = set(DATE.findall(text))
    relative = set(re.findall(r'\b(?:сегодня|завтра|послезавтра)\b', text))
    if len(values) + len(relative) > 1:
        return None
    if relative:
        return (today + timedelta(days={'сегодня': 0, 'завтра': 1, 'послезавтра': 2}[relative.pop()])).isoformat()
    if values:
        value = values.pop()
        try:
            if '-' in value:
                return date.fromisoformat(value).isoformat()
            day, month, year = map(int, re.split(r'[./]', value))
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return previous


def _clock_selection(text, previous):
    candidate = _selection(text, previous, CLOCK_LIKE, str)
    if candidate is None:
        return None
    match = CLOCK.fullmatch(candidate)
    return f'{int(match[1]):02d}:{match[2]}' if match else None


def _report_period(text):
    day = bool(re.search(r'\b(?:вчера|за\s+(?:прошедший\s+)?день)\b', text))
    week = bool(re.search(r'\b(?:за\s+(?:прошлую\s+)?неделю|(?:за\s+)?7\s+дней)\b', text))
    if day == week:
        return None
    return 1 if day else 7


def _report_label(schedule):
    period = 'ежедневно' if schedule['frequency'] == 'daily' else f"раз в неделю ({WEEKDAYS[schedule['weekday']]})"
    return f"{period} в {schedule['time']}"


def _needs_company_time(text, zone_name):
    aliases = {zone_name.casefold(), 'по местному времени'}
    if zone_name == 'Europe/Moscow':
        aliases.update(('мск', 'по москве', 'по московскому времени'))
    return any(item not in aliases for item in ZONE_HINT.findall(text)) or bool(re.search(r'\b(?:utc|gmt)\s*[+-]', text))


async def _resolve(worker, token, scope, key, user, kind, text, payload, previous=None):
    """Read concrete parameters, then send either a reply prompt or a confirmation."""
    cid, _, chat = scope
    text = _normalize(text)
    data = dict(payload)
    zone_name = worker.service.company(cid)['timezone']
    zone = ZoneInfo(zone_name)
    now = datetime.now(timezone.utc).astimezone(zone)
    missing = []
    prompt = None
    wrong_zone = kind in {'post_schedule', 'report_schedule'} and _needs_company_time(text, zone_name)
    if kind == 'post_schedule':
        ids = set(POST_ID.findall(text)) | set(MARKED_ID.findall(text))
        if not ids and previous is not None and not data.get('post_id'):
            ids.update(BARE_ID.findall(text))
        if ids:
            data['post_id'] = int(ids.pop()) if len(ids) == 1 else None
        data['date'] = _date_selection(text, data.get('date'), now.date())
        data['time'] = None if wrong_zone else _clock_selection(text, data.get('time'))
        if not data.get('post_id'):
            missing.append('номер поста (например, №12)')
        if not data.get('date'):
            missing.append('дату (например, завтра или 2027-01-15)')
        if not data.get('time'):
            missing.append('время в формате ЧЧ:ММ')
        if not missing:
            try:
                when = _local_datetime(date.fromisoformat(data['date']), data['time'], zone)
                if when <= now:
                    raise DomainError('Укажите будущую дату и время.', 422)
            except DomainError as error:
                data.pop('time', None)
                if date.fromisoformat(data['date']) < now.date():
                    data.pop('date', None)
                prompt = error.detail
            else:
                with worker.db.read() as conn:
                    row = worker.service._post_row(conn, cid, data['post_id'])
                    worker.service._editable(row, row['version'])
                    if row['platform'] != 'telegram':
                        raise DomainError('Укажите номер публикации для Telegram.', 422)
                data.update(version=row['version'], scheduled_at=when.isoformat(), timezone=zone_name)
                confirm = f"Перенести пост №{row['id']} на {when:%d.%m.%Y в %H:%M} ({zone_name})? Пост потребуется согласовать заново."
    elif kind == 'report_schedule':
        days = {day for day, pattern in enumerate(WEEKDAY_FORMS) if re.search(rf'\b{pattern}\b', text)}
        daily = bool(DAILY.search(text))
        weekly = bool(days) or bool(WEEKLY.search(text))
        if (daily and weekly) or len(days) > 1:
            data.update(frequency=None, weekday=None)
        elif daily:
            data.update(frequency='daily', weekday=None)
        elif weekly:
            data.update(frequency='weekly', weekday=next(iter(days), data.get('weekday')))
        data['time'] = None if wrong_zone else _clock_selection(text, data.get('time'))
        if not data.get('frequency'):
            missing.append('периодичность (ежедневно или раз в неделю)')
        elif data['frequency'] == 'weekly' and data['weekday'] is None:
            missing.append('день недели')
        if not data.get('time'):
            missing.append('время в формате ЧЧ:ММ')
        if not missing:
            data['timezone'] = zone_name
            confirm = f'Присылать вам сводку {_report_label(data)} ({zone_name})?'
    elif kind in {'report_list', 'report_disable'}:
        from .reports import list_report_schedules

        schedules = [item for item in list_report_schedules(worker.service, cid, user)
                     if item['enabled'] and item['recipient_id'] == user['id']]
        labels = '\n'.join(f"№{item['id']} — {_report_label(item)}" for item in schedules[:25])
        if not schedules or kind == 'report_list':
            label = f'Ваши отчёты ({zone_name})\n{labels}' if schedules else 'У вас нет активных регулярных отчётов.'
            return await _notice(worker, token, scope, key, user, label, previous=previous)
        schedule_id = _selection(text, None, MARKED_ID, int)
        if not MARKED_ID.search(text):
            schedule_id = _selection(text, None, NAMED_REPORT_ID, int)
        if schedule_id is None and previous and re.fullmatch(r'[1-9][0-9]{0,17}', text):
            schedule_id = int(text)
        if schedule_id is None and len(schedules) == 1 and not (MARKED_ID.search(text) or NAMED_REPORT_ID.search(text)):
            schedule_id = schedules[0]['id']
        selected = next((item for item in schedules if item['id'] == schedule_id), None)
        if selected:
            data.update(schedule_id=selected['id'], version=selected['version'], timezone=zone_name)
            confirm = f"Отключить отчёт №{selected['id']} ({_report_label(selected)}, {zone_name})?"
        else:
            prompt = f'Ответьте номером отчёта, который хотите отключить.\n{labels}'
    else:
        from .reports import preview_report

        if _recurring(text):
            return await _resolve(worker, token, scope, key, user, 'report_schedule', text, {}, previous)
        period = _report_period(text)
        if period:
            result = preview_report(worker.service, cid, user, days=period)
            return await _notice(worker, token, scope, key, user, result['text'], previous=previous)
        prompt = 'За какой период нужна сводка? Ответьте «за день» или «за неделю». Для регулярной отправки укажите периодичность и время.'
    if missing or prompt:
        text = prompt or f'Ответьте на это сообщение и укажите {", ".join(missing)}. Часовой пояс компании — {zone_name}.'
        await _request(worker, token, scope, key, user, kind, data, text, previous=previous)
    else:
        await _request(worker, token, scope, key, user, f'{kind}:confirm', data, confirm, previous=previous,
                       choices=(('yes', 'Подтвердить'), ('no', 'Отмена')))


async def _enqueue(worker, company, token, user, chat, kind, text, update_id):
    task = enqueue_task(worker.service, company['id'], user, kind, text, telegram_chat_id=chat,
                        telegram_bot_id=str(company['bot_id']), telegram_update_id=update_id)
    await worker.deliver_assistant_task(company, token, task)


async def handle_free_request(worker, company, token, message, user, update_id):
    sender = message.get('from') or {}
    chat = message.get('chat') or {}
    text = (message.get('text') or '').strip()
    if (chat.get('type') != 'private' or chat.get('id') != sender.get('id') or sender.get('is_bot')
            or message.get('sender_chat') or not user or not text or text.startswith('/') or worker.attachment(message)):
        return False
    text = re.sub(rf'(?<!\w)@{re.escape(company["bot_username"])}(?!\w)', '', text, flags=re.IGNORECASE).strip()
    if not text:
        return False
    chat_id = chat['id']
    scope = _scope(company, chat_id)
    reply_id = (message.get('reply_to_message') or {}).get('message_id')
    if _old_material_reply(worker, scope, reply_id):
        return False
    previous = _select_message(worker, scope, reply_id) if reply_id else None
    if previous is None:
        if DIRECTIVE.search(text) or MATERIAL.match(text):
            return False
        if re.match(r'^(?:создай|напиши|подготовь|сделай)\s+(?:новый\s+)?пост\s*:', text, re.IGNORECASE):
            return False
    key = f'u{update_id}' if update_id is not None else f'm{message["message_id"]}'
    previous_id = None
    try:
        user = _editor(worker, scope[0], chat_id, user)
        if previous:
            if (previous['actor_id'] != user['id'] or previous['state'] != 'pending'
                    or previous['expires_at'] <= datetime.now(timezone.utc).timestamp()):
                await _notice(worker, token, scope, key, user, EXPIRED)
                return True
            previous_id = previous['id']
            if CANCEL.match(_normalize(text)):
                await _notice(worker, token, scope, key, user, 'Запрос отменён.', previous=previous_id)
                return True
            if previous['kind'] == 'choice' or previous['kind'].endswith(':confirm'):
                await _notice(worker, token, scope, key, user, 'Выберите действие кнопкой под сообщением.')
                return True
            await _resolve(worker, token, scope, key, user, previous['kind'], text,
                           json.loads(previous['payload']), previous_id)
        else:
            kind = classify_request(text)
            if kind == 'choice':
                await _request(worker, token, scope, key, user, kind, {
                    'text': text, 'message_id': message['message_id'],
                    'update_id': update_id if update_id is not None else message['message_id'],
                },
                               'Что сделать с сообщением?', choices=(('material', 'Материал для поста'),
                                                                    ('task', 'Поручение'), ('no', 'Отмена')))
            elif kind.startswith(('post_', 'report_')):
                await _resolve(worker, token, scope, key, user, kind, text, {})
            else:
                await _enqueue(worker, company, token, user, chat_id, kind, text, update_id if update_id is not None else message['message_id'])
    except DomainError as error:
        await _notice(worker, token, scope, key, user, error.detail, previous=previous_id)
    return True


def _confirm(worker, cid, bot_id, user, row, payload):
    zone_name = payload.get('timezone')
    if zone_name and zone_name != worker.service.company(cid)['timezone']:
        raise DomainError('Часовой пояс компании изменился. Отправьте запрос заново.', 409)
    if row['kind'] == 'post_schedule:confirm':
        when = datetime.fromisoformat(payload['scheduled_at'])
        if when <= datetime.now(timezone.utc):
            raise DomainError('Выбранное время уже прошло. Укажите новую дату и время.', 409)
        post = worker.service.patch_post(cid, payload['post_id'], payload['version'], {'scheduled_at': when}, actor=user)
        return f'Время поста №{post["id"]} обновлено.' if post['version'] != payload['version'] else 'У поста уже установлено это время.'
    from .reports import ScheduleCreate, create_report_schedule, disable_report_schedule

    if row['kind'] == 'report_disable:confirm':
        disable_report_schedule(worker.service, cid, user, payload['schedule_id'], expected_version=payload['version'])
        return f'Отчёт №{payload["schedule_id"]} отключён.'
    schedule = create_report_schedule(
        worker.service, cid, user,
        ScheduleCreate(frequency=payload['frequency'], time=payload['time'], weekday=payload.get('weekday')),
        request_key=f'tg:{bot_id}:{row["id"]}',
    )
    return f'Отчёт №{schedule["id"]} настроен.'


async def handle_intent_callback(worker, company, token, cb, user):
    data = cb.get('data') or ''
    if not data.startswith(PREFIX):
        return False
    message = cb.get('message') or {}
    sender = cb.get('from') or {}
    chat = message.get('chat') or {}
    chat_id = chat.get('id')

    async def answer(text):
        try:
            await integrations.telegram_call(token, 'answerCallbackQuery', {'callback_query_id': cb['id'], 'text': text, 'show_alert': True})
        except integrations.IntegrationError:
            pass

    try:
        if chat.get('type') != 'private' or chat_id != sender.get('id') or sender.get('is_bot'):
            raise DomainError('Действие доступно в личном чате с ботом.', 403)
        cid, bot_id, _ = _scope(company, chat_id)
        user = _editor(worker, cid, chat_id, user)
        parts = data.split(':')
        if len(parts) != 3:
            raise DomainError(EXPIRED, 409)
        _, intent_id, action = parts
        with worker.db.transaction() as conn:
            worker.service.require_actor(conn, cid, user, EDITOR_ROLES)
            row = conn.execute('SELECT * FROM bot_intents WHERE id=? AND company_id=? AND bot_id=? '
                               'AND chat_id=? AND actor_id=? AND message_id=?',
                               (intent_id, cid, bot_id, chat_id, user['id'], message.get('message_id'))).fetchone()
            if row is None or row['state'] != 'pending' or row['expires_at'] <= datetime.now(timezone.utc).timestamp():
                raise DomainError(EXPIRED, 409)
            choice = row['kind'] == 'choice'
            if action not in (('task', 'material', 'no') if choice else ('yes', 'no')) or not (choice or row['kind'].endswith(':confirm')):
                raise DomainError(EXPIRED, 409)
            conn.execute("UPDATE bot_intents SET state='done',payload='{}' WHERE id=?", (intent_id,))
        if action == 'no':
            await answer('Запрос отменён.')
            return True
        payload = json.loads(row['payload'])
        if action == 'task':
            task = enqueue_task(worker.service, cid, user, 'chat', payload['text'], telegram_chat_id=chat_id,
                                telegram_bot_id=bot_id, telegram_update_id=payload['update_id'])
            await answer('Задача принята.')
            await worker.deliver_assistant_task(company, token, task)
        elif action == 'material':
            await answer('Материал принят.')
            await worker.handle_material(company, token, user, [{
                'chat': chat, 'from': sender, 'text': payload['text'], 'message_id': payload['message_id'],
            }])
        else:
            await answer(_confirm(worker, cid, bot_id, user, row, payload))
    except DomainError as error:
        await answer(error.detail)
    return True
