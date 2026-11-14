"""Telegram editing menus and single-use replies bound to a post version."""
import asyncio
import re

from .bot_progress import Progress, current_progress, stop_heartbeat
from .domain import DomainError, EDITOR_ROLES
from .photo_workflow import PHOTO_MIMES, apply_photo, current_photo, replace_photo
from .review_policy import require_reviewer


MENUS = {
    'menu': ('Что изменить?', [('Текст поста', 'text'), ('Фото поста', 'photo'), ('Закрыть', 'close')]),
    'text': ('Как изменить текст?', [('Описать правки', 'revise'), ('Заменить текст', 'replace'), ('Назад', 'menu')]),
    'photo': ('Что сделать с фото?', [('Изменить текущее фото', 'edit'), ('Создать новое фото', 'create'),
                                     ('Загрузить фото', 'upload'), ('Назад', 'menu')]),
}
PROMPTS = {
    'revise': 'Опишите, что изменить в тексте. Бот подготовит новую версию.',
    'replace': 'Пришлите готовый текст целиком. Он заменит текущий текст.',
    'edit': 'Опишите, что изменить на текущем фото.',
    'create': 'Опишите, какое новое изображение нужно создать.',
    'upload': 'Пришлите одно фото JPEG, PNG или WebP. Подпись к фото не заменит текст поста.',
    'feedback_text': 'Опишите, что нужно исправить в тексте. Передам правки редактору.',
    'feedback_photo': 'Опишите, что нужно исправить на фото. Передам правки редактору.',
}
PHOTO_ACTIONS = {'edit', 'create', 'upload'}
FEEDBACK_ACTIONS = {'feedback_text', 'feedback_photo'}


def initialize(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS bot_post_edits(
        company_id INTEGER NOT NULL, bot_id INTEGER NOT NULL, chat_id TEXT NOT NULL,
        message_id INTEGER NOT NULL, actor TEXT NOT NULL,
        post_id INTEGER NOT NULL, version INTEGER NOT NULL, stage TEXT NOT NULL,
        consumed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(company_id,bot_id,chat_id,message_id))''')


def _scope(company, chat, message):
    return company['id'], company['bot_id'], str(chat), message


def _save(worker, company, chat, message, actor, pid, version, stage):
    with worker.db.transaction() as conn:
        conn.execute('INSERT INTO bot_post_edits VALUES(?,?,?,?,?,?,?,?,0)',
                     (*_scope(company, chat, message), str(actor), pid, version, stage))


def _find(connection, scope):
    return connection.execute('SELECT * FROM bot_post_edits WHERE company_id=? AND bot_id=? AND chat_id=? AND message_id=?',
                              scope).fetchone()


def _check(worker, connection, cid, pid, version, user, stage):
    service = worker.service
    if not user:
        raise DomainError('Нет доступа.', 403)
    post = service._post_row(connection, cid, pid)
    service._editable(post, version)
    if stage in FEEDBACK_ACTIONS or ((stage in MENUS or stage == 'close') and user['role'] == 'approver'):
        require_reviewer(connection, cid, user)
        if post['status'] not in ('review', 'approved'):
            raise DomainError('Правки доступны согласующему на проверке.', 403)
    else:
        service.require_actor(connection, cid, user, EDITOR_ROLES)
    if stage in PHOTO_ACTIONS:
        # Telegram previews with a photo use a caption, including for posts bound to another platform.
        if len(post['body']) > 1024:
            raise DomainError('Сначала сократите текст до 1024 символов, чтобы добавить фото.', 422)
        videos = sum(item['kind'] == 'video' for item in service._media(connection, cid, post))
        if videos >= 10:
            raise DomainError('В этом посте уже 10 видео. Уберите одно из них, чтобы добавить фото.', 422)
    if stage == 'edit':
        current_photo(service, connection, cid, post)
    return post


def _choices(stage, user):
    question, choices = MENUS[stage]
    if user['role'] == 'approver' and stage in ('text', 'photo'):
        return question, [('Описать правки', f'feedback_{stage}'), ('Назад', 'menu')]
    return question, choices


def _menu(pid, stage, user):
    question, choices = _choices(stage, user)
    text = f'Пост №{pid}\n{question}'
    buttons = [[{'text': label, 'callback_data': f'pedit:{action}'}] for label, action in choices]
    return text, {'inline_keyboard': buttons}


async def _prompt(worker, company, token, chat, actor, pid, version, stage):
    hint = PROMPTS[stage] + '\nОтветьте на это сообщение.'
    if str(chat).startswith('-'):
        hint += f' Добавьте упоминание @{company["bot_username"]}.'
    sent = await worker.send(token, chat, f'Пост №{pid}\n{hint}', reply_markup={'force_reply': True})
    _save(worker, company, chat, sent['message_id'], actor, pid, version, stage)


async def open_menu(worker, company, token, chat, actor, pid, version, user):
    stage = 'menu'
    with worker.db.read() as conn:
        _check(worker, conn, company['id'], pid, version, user, stage)
    text, markup = _menu(pid, stage, user)
    sent = await worker.send(token, chat, text, reply_markup=markup)
    _save(worker, company, chat, sent['message_id'], actor, pid, version, stage)


async def menu_callback(worker, company, token, cb, user, action, call):
    chat = cb['message']['chat']['id']
    message_id = cb['message']['message_id']
    scope = _scope(company, chat, message_id)
    actor = cb['from']['id']
    with worker.db.transaction() as conn:
        edit = _find(conn, scope)
        if not user or not edit or edit['actor'] != str(actor) or edit['consumed']:
            raise DomainError('Откройте меню «Изменить» под текущей версией поста.', 409)
        stage = edit['stage']
        if stage not in MENUS or action not in {value for _, value in _choices(stage, user)[1]}:
            raise DomainError('Этот выбор уже не действует. Используйте кнопки текущего меню.', 409)
        pid, version = edit['post_id'], edit['version']
        _check(worker, conn, company['id'], pid, version, user, action)
        conn.execute('UPDATE bot_post_edits SET stage=?,consumed=? WHERE company_id=? AND bot_id=? AND chat_id=? AND message_id=?',
                     (action, action not in MENUS, *scope))
    if action in MENUS:
        text, markup = _menu(pid, action, user)
    else:
        text = f'Пост №{pid}\n' + ('Меню закрыто.' if action == 'close' else 'Действие выбрано. Ответьте на сообщение ниже.')
        markup = {'inline_keyboard': []}
    await call(token, 'editMessageText', {'chat_id': chat, 'message_id': message_id, 'text': text, 'reply_markup': markup})
    if action in PROMPTS:
        await _prompt(worker, company, token, chat, actor, pid, version, action)


def _read_input(worker, company, messages, stage):
    if stage == 'upload':
        message = messages[0]
        document = message.get('document') or {}
        if len(messages) != 1 or not (message.get('photo') or document.get('mime_type') in PHOTO_MIMES):
            raise DomainError('Пришлите только одно фото JPEG, PNG или WebP ответом на запрос бота.', 422)
        return None
    text = messages[0].get('text', '')
    if str(messages[0]['chat']['id']).startswith('-'):
        # A mention in a group is for routing. A private replacement can contain the username as content.
        text = re.sub(rf'(?<!\w)@{re.escape(company["bot_username"])}(?!\w)', '', text, flags=re.IGNORECASE)
    text = text.strip()
    limit = 4096 if stage == 'replace' else 4000
    if len(messages) != 1 or worker.attachment(messages[0]) or not text or len(text) > limit:
        raise DomainError(f'Ответьте на запрос бота текстом до {limit} символов.', 422)
    return text


async def handle_reply(worker, company, token, messages, user, call):
    reply_ids = {message.get('reply_to_message', {}).get('message_id') for message in messages} - {None}
    if not reply_ids or 'bot_id' not in company:
        return False
    cid, chat = company['id'], messages[0]['chat']['id']
    actor = messages[0].get('from', {}).get('id')
    status = heartbeat = context = None
    consumed = False
    try:
        with worker.db.transaction() as conn:
            scopes = [_scope(company, chat, mid) for mid in reply_ids]
            matches = [(scope, row) for scope in scopes if (row := _find(conn, scope))]
            if not matches:
                return False
            scope, edit = matches[0]
            if edit['actor'] != str(actor) or edit['consumed']:
                return True
            stage, pid, version = edit['stage'], edit['post_id'], edit['version']
            if stage not in PROMPTS:
                raise DomainError('Выберите действие в меню поста.', 422)
            row = _check(worker, conn, cid, pid, version, user, stage)
            text = _read_input(worker, company, messages, stage)
            if stage == 'replace':
                media = worker.service._media(conn, cid, row)
                if len(text) > 1024 and any(item['id'] != row['card_id'] for item in media):
                    raise DomainError('Для поста с фото или видео пришлите текст до 1024 символов.', 422)
            conn.execute('UPDATE bot_post_edits SET consumed=1 WHERE company_id=? AND bot_id=? AND chat_id=? AND message_id=?', scope)
        consumed = True
        if stage in FEEDBACK_ACTIONS:
            worker.service.reject(cid, pid, version, text, actor=user)
            await worker.send(token, chat, 'Правки сохранены. Пост возвращён редактору.')
            return True
        if stage == 'replace':
            post = worker.service.patch_post(cid, pid, version, {'body': text}, actor=user)
        else:
            status = Progress(call, token, chat, 'Правки получены.')
            context = current_progress.set(status)
            await status.update({'revise': 'Обновляю текст.', 'upload': 'Сохраняю фото.'}.get(stage, 'Готовлю изображение.'), pid)
            heartbeat = asyncio.create_task(status.heartbeat())
            if stage == 'revise':
                post = await worker.service.revise(cid, pid, version, text, actor=user)
            elif stage == 'upload':
                with worker.db.read() as conn:
                    _check(worker, conn, cid, pid, version, user, stage)
                source = await worker.download(company, token, messages[0], user)
                post = replace_photo(worker.service, cid, pid, version, source['id'], actor=user)
            else:
                post = await apply_photo(worker.service, cid, pid, version, stage, text, actor=user,
                                         edit_current=stage == 'edit')
        await worker.respond(company, token, chat, post, user=user)
    except DomainError as error:
        detail = error.detail
        if consumed:
            detail += ' Чтобы повторить, откройте «Изменить» под актуальной версией поста.'
        if status:
            await status.update(detail)
        else:
            await worker.send(token, chat, detail)
    except asyncio.CancelledError:
        if status:
            await status.update('Подготовка прервана. Проверьте актуальную версию поста перед повтором.')
        raise
    finally:
        if heartbeat:
            await stop_heartbeat(heartbeat)
        if context is not None:
            current_progress.reset(context)
    return True
