"""Persistent Telegram inbox, approval notifications and due publication worker.

One worker per SQLite database. Uncertain sends are recorded, never blindly retried.
"""
import asyncio
from datetime import datetime
import hashlib
import json
import logging
import re
import time
from zoneinfo import ZoneInfo
import httpx

from .integrations import telegram_call, publish_telegram, message_is_addressed, IntegrationError, TelegramError
from .webapp import configure_bot, menu_markup, public_url
from .network import http_options
from .domain import DomainError, can_review
from .photo_workflow import apply_photo, photo_directive
from .logo_workflow import apply_logo, logo_options, logo_keyboard, post_buttons, CORNERS
from .telegram_link import claim_link, protect_link_update
from .bot_progress import Progress, current_progress, progress, stop_heartbeat
from .assistant_tasks import (
    append_task_message, due_routines, enqueue_task, list_task_notifications, list_telegram_tasks, process_tasks,
)
from .inbox import auto_process_inbox, sync_social_activity
from .reports import deliver_reports, due_reports, initialize_reports
from .review_policy import configured_reviewer_ids, reviewer_ids
from .telegram_intents import handle_free_request, handle_intent_callback, initialize_intents

LOG = logging.getLogger(__name__)
TASK_COMMANDS = {
    '/ask': ('chat', 'Задать вопрос'),
    '/topics': ('topics', 'Подобрать темы'),
    '/plan': ('plan', 'Составить контент-план'),
    '/strategy': ('strategy', 'Подготовить контент-стратегию'),
    '/video': ('video', 'Подготовить сценарий видео'),
}


class Worker:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        with self.db.transaction() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS worker_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bot_inbox(
                    company_id INTEGER NOT NULL, update_id INTEGER NOT NULL,
                    body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', received_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(company_id,update_id));
                CREATE TABLE IF NOT EXISTS bot_replies(
                    company_id INTEGER, chat_id TEXT, message_id INTEGER,
                    post_id INTEGER, version INTEGER,
                    PRIMARY KEY(company_id,chat_id,message_id));
                CREATE TABLE IF NOT EXISTS bot_edit_replies(
                    company_id INTEGER, chat_id TEXT, message_id INTEGER,
                    post_id INTEGER, version INTEGER, telegram_id TEXT NOT NULL,
                    PRIMARY KEY(company_id,chat_id,message_id));
                CREATE TABLE IF NOT EXISTS bot_photo_replies(
                    company_id INTEGER, chat_id TEXT, message_id INTEGER,
                    post_id INTEGER, version INTEGER, mode TEXT NOT NULL, telegram_id TEXT NOT NULL,
                    instruction TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(company_id,chat_id,message_id));
                CREATE TABLE IF NOT EXISTS bot_task_replies(
                    company_id INTEGER NOT NULL, bot_id INTEGER NOT NULL, chat_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
                    task_id INTEGER NOT NULL, request_id INTEGER NOT NULL,
                    PRIMARY KEY(company_id,bot_id,chat_id,message_id));
            ''')
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(bot_inbox)')}
            if 'received_at' not in columns:
                conn.execute('ALTER TABLE bot_inbox ADD COLUMN received_at REAL NOT NULL DEFAULT 0')
            photo_columns = {row['name'] for row in conn.execute('PRAGMA table_info(bot_photo_replies)')}
            if 'instruction' not in photo_columns:
                conn.execute("ALTER TABLE bot_photo_replies ADD COLUMN instruction TEXT NOT NULL DEFAULT ''")
            # An interrupted operation may have reached Telegram. Human inspection required.
            conn.execute("UPDATE bot_inbox SET status='interrupted' WHERE status='processing'")
            conn.execute(
                "UPDATE worker_state SET value='uncertain' WHERE value='claimed' AND key LIKE 'task:%:post:%:%:media'"
            )
        initialize_intents(service)
        initialize_reports(service)

    def state(self, key, value=None):
        with self.db.transaction() as conn:
            if value is not None:
                conn.execute('INSERT OR REPLACE INTO worker_state VALUES (?,?)', (key, str(value)))
            row = conn.execute('SELECT value FROM worker_state WHERE key=?', (key,)).fetchone()
            return row['value'] if row else None

    def claim(self, key):
        with self.db.transaction() as conn:
            return conn.execute('INSERT OR IGNORE INTO worker_state VALUES (?,?)', (key, 'claimed')).rowcount == 1

    def audit_error(self, cid, action):
        self.service.audit(cid, 'bot_error', action)

    async def run(self):
        cycles = {}
        try:
            while True:
                try:
                    for key, task in list(cycles.items()):
                        if task.done():
                            del cycles[key]
                            if not task.cancelled() and task.exception() is not None:
                                LOG.error('Background worker cycle failed; credentials omitted')
                    for company in self.service.companies():
                        cid = company['id']
                        for name, cycle in (
                            ('telegram', self.tick), ('assistant', self.assistant_tick),
                            ('routines', self.routines_tick), ('inbox', self.inbox_tick),
                            ('reports', self.reports_tick),
                        ):
                            key = cid, name
                            if key not in cycles:
                                cycles[key] = asyncio.create_task(cycle(company))
                except Exception:
                    LOG.error('Background worker cycle failed; credentials omitted')
                await asyncio.sleep(5)
        finally:
            for task in cycles.values():
                task.cancel()
            await asyncio.gather(*cycles.values(), return_exceptions=True)

    async def assistant_tick(self, company):
        cid = company['id']

        async def on_started(task):
            if task.get('telegram_bot_id'):
                self.state(self.assistant_key(task, 'started'), 'done')

        async def on_post_image(task):
            if task.get('telegram_bot_id'):
                self.state(self.assistant_key(task, 'image-started'), 'done')

        await process_tasks(self.service, cid, on_started=on_started, on_post_image=on_post_image)

    async def routines_tick(self, company):
        due_routines(self.service, company['id'])

    async def reports_tick(self, company):
        cid = company['id']
        due_reports(self.service, cid)
        token = self.service.credentials(cid).get('telegram_token')
        if not token:
            return
        identity_key = f'identity:{cid}:{hashlib.sha256(token.encode()).hexdigest()[:16]}'
        identity = self.state(identity_key)
        if identity:
            await deliver_reports(self.service, cid, json.loads(identity)['id'],
                                  lambda chat, text: self.send(token, chat, text), token=token)

    async def inbox_tick(self, company):
        cid = company['id']
        try:
            await sync_social_activity(self.service, cid)
        finally:
            await auto_process_inbox(self.service, cid)

    async def tick(self, company):
        cid = company['id']
        for post in self.service.due_posts():
            if post['company_id'] == cid:
                try:
                    await self.service.publish(cid, post['id'], post['version'], scheduled=True)
                except Exception:
                    if self.claim(f'publish-error:{cid}:{post["id"]}:{post["version"]}'):
                        self.audit_error(cid, f'Не удалось опубликовать пост №{post["id"]}. Проверьте его статус.')
        try:
            token = self.service.credentials(cid).get('telegram_token')
            if not token:
                return
            identity_key = f'identity:{cid}:{hashlib.sha256(token.encode()).hexdigest()[:16]}'
            identity = self.state(identity_key)
            if not identity:
                identity = json.dumps(await telegram_call(token, 'getMe'))
                self.state(identity_key, identity)
            identity = json.loads(identity)
            company = dict(company, bot_username=identity['username'], bot_id=identity['id'])
            url = public_url(self.db)
            menu_key = f'menu-v2:{cid}:{identity["id"]}'
            if self.state(menu_key) != url:
                await configure_bot(telegram_call, token, url)
                self.state(menu_key, url)
            if self.state(f'active-bot:{cid}') != str(identity['id']):
                with self.db.transaction() as conn:
                    conn.execute('DELETE FROM bot_inbox WHERE company_id=?', (cid,))
                    conn.execute('DELETE FROM bot_replies WHERE company_id=?', (cid,))
                    conn.execute('DELETE FROM bot_edit_replies WHERE company_id=?', (cid,))
                    conn.execute('DELETE FROM bot_photo_replies WHERE company_id=?', (cid,))
                self.state(f'active-bot:{cid}', identity['id'])
            # Offset scoped to bot identity, changing token does not lose new bot updates.
            offset_key = f'offset:{cid}:{identity["id"]}'
            updates = await telegram_call(token, 'getUpdates', {
                'offset': int(self.state(offset_key) or 0), 'timeout': 0,
                'allowed_updates': ['message', 'callback_query'], 'limit': 30,
            })
            with self.db.transaction() as conn:
                for update in updates:
                    conn.execute('INSERT OR IGNORE INTO bot_inbox(company_id,update_id,body,received_at) VALUES (?,?,?,?)',
                                 (cid, update['update_id'], json.dumps(protect_link_update(update), ensure_ascii=False), time.time()))
            if updates:
                self.state(offset_key, max(u['update_id'] for u in updates) + 1)
            with self.db.read() as conn:
                pending = conn.execute("SELECT * FROM bot_inbox WHERE company_id=? AND status='pending' ORDER BY update_id LIMIT 20", (cid,)).fetchall()
            for row in pending:
                batch = self.claim_updates(company, row, len(updates) >= 30)
                if not batch:
                    continue
                try:
                    update = json.loads(batch[0]['body'])
                    if update.get('message', {}).get('media_group_id'):
                        await self.handle_album(company, token, [json.loads(item['body'])['message'] for item in batch])
                    else:
                        await self.handle(company, token, update)
                    status = 'done'
                except asyncio.CancelledError:
                    raise
                except Exception:
                    status = 'failed'
                    self.audit_error(cid, f'Не обработано сообщение Telegram {row["update_id"]}. Повторите задание в кабинете.')
                with self.db.transaction() as conn:
                    conn.executemany('UPDATE bot_inbox SET status=?,body=? WHERE company_id=? AND update_id=?',
                                     [(status, '{}', cid, item['update_id']) for item in batch])
            await self.assistant_notifications(company, token)
            await self.notifications(company, token)
            await self.calendar(company, token)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One log entry per failure period, no raw HTTP exceptions containing tokens.
            key = f'connection-error:{cid}:{datetime.now().strftime("%Y-%m-%d-%H")}'
            if self.claim(key):
                self.audit_error(cid, 'Telegram недоступен. Проверьте подключение и отсутствие другого запущенного бота.')

    def claim_updates(self, company, row, full_poll):
        cid = company['id']
        message = json.loads(row['body']).get('message') or {}
        album = message.get('media_group_id')
        with self.db.transaction() as conn:
            current = conn.execute("SELECT status FROM bot_inbox WHERE company_id=? AND update_id=?", (cid, row['update_id'])).fetchone()
            if current is None or current['status'] != 'pending':
                return []
            if album:
                if full_poll:
                    return []
                chat = message.get('chat', {}).get('id')
                sender = message.get('from', {}).get('id')
                rows = conn.execute(
                    "SELECT * FROM bot_inbox WHERE company_id=? AND status='pending' "
                    "AND json_extract(body,'$.message.media_group_id')=? "
                    "AND json_extract(body,'$.message.chat.id') IS ? AND json_extract(body,'$.message.from.id') IS ? "
                    "ORDER BY update_id", (cid, album, chat, sender),
                ).fetchall()
                if time.time() - max(item['received_at'] for item in rows) < 2:
                    return []
                album_key = f'album:{cid}:{company["bot_id"]}:{chat}:{sender}:{album}'
                if not conn.execute('INSERT OR IGNORE INTO worker_state VALUES (?,?)', (album_key, 'claimed')).rowcount:
                    conn.executemany("UPDATE bot_inbox SET status='done',body='{}' WHERE company_id=? AND update_id=?",
                                     [(cid, item['update_id']) for item in rows])
                    return []
            else:
                rows = [row]
            conn.executemany("UPDATE bot_inbox SET status='processing' WHERE company_id=? AND update_id=?",
                             [(cid, item['update_id']) for item in rows])
            return rows

    async def send(self, token, chat, text, **kwargs):
        return await telegram_call(token, 'sendMessage', {'chat_id': chat, 'text': text, **kwargs})

    async def handle(self, company, token, update):
        cid = company['id']
        callback = update.get('callback_query')
        if callback:
            return await self.callback(company, token, callback)
        message = update.get('message') or {}
        if message.get('from', {}).get('is_bot') or message.get('sender_chat'):
            return
        chat = message.get('chat', {})
        private = chat.get('type') == 'private'
        sender = message.get('from', {}).get('id')
        if private and chat.get('id') != sender:
            return
        user = self.service.telegram_members(cid, sender)
        text = message.get('text') or message.get('caption') or ''
        command = text.split()[0].split('@')[0] if text.strip() else ''
        parts = text.split()
        raw_code = parts[1] if command == '/start' and len(parts) == 2 and parts[1].startswith('link_') else ''
        code_hash = update.get('_link_hash')
        if private and (raw_code or code_hash):
            if claim_link(self.service, cid, company['bot_id'], raw_code, sender, code_hash=code_hash, bot_token=token):
                await self.send(token, chat['id'], 'Telegram привязан к вашей учётной записи компании.')
                await self.menu(company, token, chat['id'], 'start', self.service.telegram_members(cid, sender))
            else:
                await self.send(token, chat['id'], 'Ссылка недействительна. Получите новую ссылку в кабинете компании.')
            return
        if private and command in ('/start', '/help', '/id', '/material', '/posts'):
            await self.menu(company, token, chat['id'], command[1:], user)
            return
        if not user:
            return
        if not private:
            if str(chat.get('id')) != str(company.get('chat_id')):
                return
            if not message_is_addressed(message, company['bot_username'], company['bot_id']):
                return
        chat_id = chat['id']
        text_only = message.get('text') and not self.attachment(message)
        if text_only and command in TASK_COMMANDS:
            suffix = parts[0].partition('@')[2]
            if suffix and suffix.casefold() != company['bot_username'].casefold():
                return
            if private:
                prompt = message['text'].strip()[len(parts[0]):].strip()
                await self.assistant_command(company, token, message, user, command, prompt, update.get('update_id'))
            else:
                await self.send(token, chat_id, 'Отправьте команду в личный чат с ботом.')
            return
        if command in ('/start', '/help'):
            await self.send(token, chat_id, 'Отправьте материал для поста. Для привязки откройте настройки Telegram в кабинете компании.')
            return
        if command in ('/allow', '/remove', '/list'):
            if user['role'] != 'admin' or not private:
                await self.send(token, chat_id, 'Доступно администратору в личном чате.')
            elif command == '/list':
                people = self.service.telegram_users(cid)
                await self.send(token, chat_id, '\n'.join(f'{p["name"]}: {p.get("telegram_id")} ({p["role"]})' for p in people) or 'Список пуст.')
            elif len(parts) == 2 and parts[1].isdigit():
                self.service.set_telegram_member(cid, int(parts[1]), active=command == '/allow', actor=user)
                await self.send(token, chat_id, 'Доступ обновлён.')
            else:
                await self.send(token, chat_id, 'Укажите числовой Telegram ID.')
            return
        if text.startswith('/reject '):
            parts = text.split(maxsplit=2)
            if not can_review(user) or len(parts) < 3 or not parts[1].isdigit():
                await self.send(token, chat_id, 'Укажите номер поста и причину. Нужны права согласующего.')
                return
            post = self.service.post(cid, int(parts[1]))
            self.service.reject(cid, post['id'], post['version'], parts[2], actor=user)
            await self.send(token, chat_id, 'Пост возвращён на доработку.')
            return
        if private and text_only and await self.assistant_reply(company, token, message, user, update.get('update_id')):
            return
        if await self.handle_edit_reply(company, token, message, user):
            return
        if private and text_only and await handle_free_request(self, company, token, message, user, update.get('update_id')):
            return
        if user['role'] not in ('admin', 'editor'):
            await self.send(token, chat_id, 'Ваша роль — согласование. Для передачи материалов нужны права редактора.')
            return
        await self.handle_material(company, token, user, [message])

    @staticmethod
    def assistant_key(task, stage):
        return f'task:{task["company_id"]}:{task["telegram_bot_id"]}:{task["id"]}:{task["request_id"]}:{stage}'

    async def assistant_command(self, company, token, message, user, command, prompt, update_id):
        chat_id = message['chat']['id']
        kind, _ = TASK_COMMANDS[command]
        if not prompt:
            await self.send(token, chat_id, f'Напишите запрос после {command}.')
            return
        try:
            task = enqueue_task(
                self.service, company['id'], user, kind, prompt, telegram_chat_id=chat_id,
                telegram_bot_id=str(company['bot_id']), telegram_update_id=update_id if update_id is not None else message['message_id'],
            )
        except DomainError as error:
            await self.send(token, chat_id, error.detail)
            return
        await self.deliver_assistant_task(company, token, task)

    async def assistant_reply(self, company, token, message, user, update_id):
        chat_id = message['chat']['id']
        reply_to = message.get('reply_to_message') or {}
        if reply_to.get('message_id') is None:
            return False
        cid, bot_id = company['id'], company['bot_id']
        with self.db.read() as conn:
            reply = conn.execute(
                'SELECT * FROM bot_task_replies WHERE company_id=? AND bot_id=? AND chat_id=? AND message_id=?',
                (cid, bot_id, str(chat_id), reply_to['message_id']),
            ).fetchone()
        if reply is None:
            return False
        if reply['actor_id'] != user['id']:
            return True
        try:
            task = append_task_message(
                self.service, cid, reply['task_id'], user, message['text'].strip(),
                telegram_bot_id=str(bot_id), telegram_update_id=update_id if update_id is not None else message['message_id'],
                telegram_chat_id=chat_id, expected_request_id=reply['request_id'],
            )
        except DomainError as error:
            await self.send(token, chat_id, error.detail)
            return True
        await self.deliver_assistant_task(company, token, task)
        return True

    async def assistant_notifications(self, company, token):
        for task in list_telegram_tasks(self.service, company['id'], str(company['bot_id'])):
            await self.deliver_assistant_task(company, token, task)
        for task in list_task_notifications(self.service, company['id'], company['bot_id']):
            await self.deliver_task_notification(token, task)

    async def deliver_task_notification(self, token, task):
        if task.get('post_id'):
            await self.deliver_task_post(token, task)
            return
        heading = f'Задача №{task["id"]}'
        if task['routine_id'] is not None:
            heading = f'Рутина №{task["routine_id"]} · {heading}'
        status = task['status']
        if status == 'completed':
            title, details = 'выполнена', task['reply']
        elif status == 'needs_info':
            title, details = 'требует уточнений', '\n'.join(task['questions'])
        elif status == 'interrupted':
            title, details = 'прервана', ''
        else:
            title, details = 'не выполнена', task['error'] or ''
        if len(details) > 1850:
            details = details[:1849] + '…'
        text = '\n\n'.join(part for part in (f'{heading} {title}.', details, 'Откройте задачу в кабинете.') if part)
        attempt = task['notification_attempt'] or 'initial'
        url = public_url(self.db)
        markup = {'inline_keyboard': [[{'text': 'Открыть кабинет', 'web_app': {'url': url}}]]} if url else None
        await self.send_assistant_stage(token, task, f'notification:{status}:{attempt}', text, reply_markup=markup)

    async def deliver_assistant_task(self, company, token, task):
        cid, bot_id = company['id'], company['bot_id']
        chat = str(task.get('telegram_chat_id') or '')
        if (task['company_id'] != cid or str(task.get('telegram_bot_id')) != str(bot_id)
                or not chat.isascii() or not chat.isdigit() or int(chat) < 1):
            return
        if str(task.get('telegram_actor_id')) != chat:
            return
        status = task['status']
        heading = f'Задача №{task["id"]}'
        result_post = task.get('post_id') and status not in ('queued', 'running')
        progress_sent = await self.send_assistant_stage(token, task, 'accepted', f'{heading} принята.')
        if progress_sent and (status == 'running' or self.state(self.assistant_key(task, 'started'))):
            progress_sent = await self.send_assistant_stage(token, task, 'running', f'Выполняю задачу №{task["id"]}.')
        if status == 'running' and progress_sent and self.state(self.assistant_key(task, 'image-started')):
            await self.send_assistant_stage(token, task, 'generating-image', f'Готовлю изображение для задачи №{task["id"]}.')
        if status in ('queued', 'running') or (not progress_sent and not result_post):
            return
        if result_post:
            await self.deliver_task_post(token, task)
            if status != 'needs_info' or not task.get('questions'):
                return
        if status == 'failed':
            text = f'{heading} не выполнена. {task.get("error") or "Подробности в кабинете."}'
        elif status == 'interrupted':
            text = f'Работа над задачей №{task["id"]} прервалась. Проверьте её в кабинете.'
        elif status in ('completed', 'needs_info'):
            heading = f'{heading} выполнена.' if status == 'completed' else f'Нужны уточнения по задаче №{task["id"]}.'
            sections = [heading, task.get('reply') or '', '\n'.join(task.get('questions') or [])]
            sections.append('Чтобы продолжить, ответьте на это сообщение.')
            text = '\n\n'.join(section for section in sections if section)
        else:
            return
        # Telegram counts UTF-16 units. The chunks also leave room for emoji-heavy responses.
        chunks = [text[start:start + 2000] for start in range(0, len(text), 2000)]
        for index, chunk in enumerate(chunks):
            bind = status in ('completed', 'needs_info')
            force_reply = status == 'needs_info' and index == len(chunks) - 1
            if not await self.send_assistant_stage(token, task, f'{status}:{index}', chunk, bind=bind, force_reply=force_reply):
                break

    def assistant_recipient(self, task):
        chat = str(task['telegram_chat_id'])
        user = self.service.telegram_members(task['company_id'], int(chat))
        if (user and user['role'] in ('admin', 'editor')
                and task.get('recipient_id', task['actor_id']) == user['id']
                and str(user.get('telegram_id')) == chat):
            return user

    async def deliver_task_post(self, token, task):
        user = self.assistant_recipient(task)
        if not user:
            return
        cid, pid, version = task['company_id'], task['post_id'], task.get('post_version')
        stage = f'post:{pid}:{version}'
        complete_key = self.assistant_key(task, f'{stage}:delivered')
        if self.state(complete_key):
            return
        heading = f'Задача №{task["id"]} · Пост №{pid}'
        status = task['status']
        if status == 'failed':
            heading += '\n' + (task.get('error') or 'Работу над постом не удалось завершить.')
        elif status == 'interrupted':
            heading += '\nПодготовка поста прервалась. Пост сохранён.'
        try:
            post = self.service.post(cid, pid)
        except DomainError:
            post = None
        if post is None or post['version'] != version:
            url = public_url(self.db)
            markup = {'inline_keyboard': [[{'text': 'Открыть кабинет', 'web_app': {'url': url}}]]} if url else None
            if await self.send_assistant_stage(token, task, f'{stage}:changed',
                                               heading + '\nПроверьте текущий статус поста в кабинете.',
                                               reply_markup=markup):
                self.state(complete_key, 'done')
            return
        if task['status'] == 'needs_info' and not post['body'] and not post.get('logo_pending'):
            return

        buttons = self.post_controls(cid, post, user)
        markup = {'inline_keyboard': buttons} if buttons else None
        if post.get('logo_pending'):
            try:
                options = logo_options(self.service, cid, pid, version, actor=user)
                markup = logo_keyboard(post, options['variants'])
                heading += '\nВыберите цвет и угол логотипа, затем нажмите «Применить».'
            except DomainError as error:
                heading += '\n' + error.detail
        elif post['status'] == 'review':
            heading += '\nПост на согласовании.'

        try:
            media = self.service.publication_media(cid, post)
        except DomainError:
            media = []
            heading += '\nВложения проверьте в кабинете.'
        body = post['body']
        if media:
            media_stage = f'{stage}:media'
            caption = body if len(body.encode('utf-16-le')) <= 2048 else ''
            delivered = await self.send_assistant_stage(token, task, media_stage, caption, media=media, post=post)
            if delivered and caption:
                body = ''
            elif not delivered:
                state = self.state(self.assistant_key(task, media_stage))
                if state not in ('failed', 'uncertain'):
                    return
                heading += ('\nИзображение сохранено в кабинете. Не удалось отправить его в Telegram.'
                            if state == 'failed' else '\nРезультат отправки изображения в Telegram неизвестен.')

        preview = '\n\n'.join(part for part in (heading, body) if part)
        chunks = [preview[start:start + 2000] for start in range(0, len(preview), 2000)]
        for index, chunk in enumerate(chunks):
            final_chunk = index == len(chunks) - 1
            bind = final_chunk and status == 'completed' and 'recipient_id' not in task
            if not await self.send_assistant_stage(token, task, f'{stage}:controls:{index}', chunk,
                                                   bind=bind, reply_markup=markup if final_chunk else None, post=post):
                return
        self.state(complete_key, 'done')

    async def send_assistant_stage(self, token, task, stage, text, *, bind=False, force_reply=False,
                                   reply_markup=None, media=None, post=None):
        key = self.assistant_key(task, stage)
        chat = str(task['telegram_chat_id'])
        user = self.assistant_recipient(task)
        recipient = task.get('recipient_id', task['actor_id'])
        post_stage = stage.startswith('post:')
        if not user:
            return False
        backoff_key = f'task-bot-backoff:{task["company_id"]}:{task["telegram_bot_id"]}'
        with self.db.transaction() as conn:
            entry = conn.execute('SELECT value FROM worker_state WHERE key=?', (key,)).fetchone()
            attempts = 1
            now = time.time()
            if entry:
                if not entry['value'].startswith('retry:'):
                    return entry['value'] == 'done'
                _, attempt, due_at = entry['value'].split(':')
                if now < float(due_at):
                    return False
                attempts = int(attempt) + 1
            backoff = conn.execute('SELECT value FROM worker_state WHERE key=?', (backoff_key,)).fetchone()
            if backoff and now < float(backoff['value']):
                return False
            if post_stage or 'recipient_id' in task:
                company = self.service._company_row(conn, task['company_id'])
                if (not token.strip().startswith(f'{task["telegram_bot_id"]}:')
                        or self.service._credentials(company, {'telegram_token'})['telegram_token'] != token):
                    return False
                try:
                    self.service.require_actor(conn, task['company_id'], user, {'admin', 'editor'})
                except DomainError:
                    return False
            if 'recipient_id' in task:
                if not conn.execute(
                    'SELECT 1 FROM assistant_tasks WHERE company_id=? AND id=? AND request_id=? AND status=? '
                    'AND attempt_id IS ? AND notify_telegram=1 AND notify_user_id=? AND notify_chat_id=? AND notify_bot_id=?',
                    (task['company_id'], task['id'], task['request_id'], task['status'], task['notification_attempt'],
                     recipient, chat, task['telegram_bot_id']),
                ).fetchone():
                    return False
            elif post_stage:
                if not conn.execute(
                    'SELECT 1 FROM assistant_tasks WHERE company_id=? AND id=? AND request_id=? AND status=? '
                    'AND actor_id=? AND telegram_actor_id=? AND telegram_chat_id=? AND telegram_bot_id=?',
                    (task['company_id'], task['id'], task['request_id'], task['status'], recipient, chat, chat,
                     task['telegram_bot_id']),
                ).fetchone():
                    return False
            if post:
                try:
                    row = self.service._post_row(conn, task['company_id'], post['id'])
                    self.service._check_version(row, post['version'])
                except DomainError:
                    return False
                if post['status'] == 'review':
                    conn.execute('INSERT OR IGNORE INTO worker_state VALUES (?,?)', (
                        f'notify:{task["company_id"]}:{post["id"]}:{post["version"]}:review:{chat}', 'task',
                    ))
            conn.execute('INSERT OR REPLACE INTO worker_state VALUES (?,?)', (key, 'claimed'))
        try:
            if media:
                sent = await publish_telegram(token, chat, text, media)
            else:
                if force_reply:
                    reply_markup = {'force_reply': True}
                options = {'reply_markup': reply_markup} if reply_markup else {}
                sent = await self.send(token, int(chat), text, **options)
            if post_stage and (not isinstance(sent, dict) or type(sent.get('message_id')) is not int
                               or sent['message_id'] < 1):
                raise TelegramError('Telegram не подтвердил отправку.', uncertain=True)
            with self.db.transaction() as conn:
                if bind:
                    conn.execute('INSERT OR IGNORE INTO bot_task_replies VALUES(?,?,?,?,?,?,?)', (
                        task['company_id'], task['telegram_bot_id'], chat, sent['message_id'],
                        task['actor_id'], task['id'], task['request_id'],
                    ))
                conn.execute('UPDATE worker_state SET value=? WHERE key=?', ('done', key))
        except Exception as error:
            if isinstance(error, TelegramError) and not error.uncertain:
                retryable = error.status_code in (None, 429)
                if retryable and (attempts < 3 or (error.status_code == 429 and not post_stage)):
                    retry_at = time.time() + max(5, error.retry_after or 15 * attempts)
                    with self.db.transaction() as conn:
                        conn.execute('UPDATE worker_state SET value=? WHERE key=?', (f'retry:{attempts}:{retry_at}', key))
                        if error.status_code == 429:
                            conn.execute('INSERT OR REPLACE INTO worker_state VALUES (?,?)', (backoff_key, str(retry_at)))
                    return False
                state = 'failed'
            else:
                state = 'uncertain'
            self.state(key, state)
            self.audit_error(task['company_id'], f'Не доставлено сообщение о задаче №{task["id"]}.')
            return False
        return True

    async def handle_album(self, company, token, messages):
        if any(message.get('from', {}).get('is_bot') or message.get('sender_chat') for message in messages):
            return
        first = messages[0]
        sender = first.get('from', {}).get('id')
        chat = first.get('chat', {})
        private = chat.get('type') == 'private'
        if private and chat.get('id') != sender:
            return
        user = self.service.telegram_members(company['id'], sender)
        if not user or user['role'] not in ('admin', 'editor'):
            return
        if not private and (str(chat.get('id')) != str(company.get('chat_id')) or not any(
            message_is_addressed(item, company['bot_username'], company['bot_id']) for item in messages
        )):
            return
        await self.handle_material(company, token, user, messages)

    async def handle_material(self, company, token, user, messages):
        attachments = sum(bool(self.attachment(item)) for item in messages)
        has_text = any(item.get('text') or item.get('caption') for item in messages)
        received = 'Получено: ' + ', '.join(
            (['текст'] if has_text else []) + ([f'вложений — {attachments}'] if attachments else [])
        ) + '.'
        status = Progress(telegram_call, token, messages[0]['chat']['id'], received)
        context = current_progress.set(status)
        await status.update('Сохраняю материалы.')
        heartbeat = asyncio.create_task(status.heartbeat())
        try:
            await self._handle_material(company, token, user, messages)
        except asyncio.CancelledError:
            await status.update('Подготовка прервана. Проверьте черновик в кабинете.')
            raise
        except Exception as error:
            detail = error.detail if isinstance(error, DomainError) else (
                str(error) if isinstance(error, IntegrationError) else
                'Не удалось обработать материал. Проверьте черновик в кабинете и повторите попытку.'
            )
            await status.update('Ошибка. ' + detail)
            # Never log raw network errors: their URLs may include a bot token.
            self.audit_error(company['id'], f'Сбой обработки материала: {type(error).__name__}.')
            raise
        finally:
            await stop_heartbeat(heartbeat)
            current_progress.reset(context)

    async def _handle_material(self, company, token, user, messages):
        cid = company['id']
        message = messages[0]
        chat_id = message['chat']['id']
        sender = message['from']['id']
        text = '\n'.join(dict.fromkeys(item.get('text') or item.get('caption') or '' for item in messages)).strip()
        # Routing mentions are not evidence or image instructions.
        text = re.sub(rf'(?<!\w)@{re.escape(company["bot_username"])}(?!\w)', '', text, flags=re.IGNORECASE).strip()
        try:
            mode, material, instruction = photo_directive(text)
        except DomainError as error:
            await progress('Нужно уточнение: ' + error.detail)
            await self.send(token, chat_id, error.detail)
            return
        reply_id = message.get('reply_to_message', {}).get('message_id')
        with self.db.read() as conn:
            reply = conn.execute('SELECT * FROM bot_replies WHERE company_id=? AND chat_id=? AND message_id=?', (cid, str(chat_id), reply_id)).fetchone()
            photo_reply = conn.execute('SELECT * FROM bot_photo_replies WHERE company_id=? AND chat_id=? AND message_id=? AND telegram_id=?',
                                       (cid, str(chat_id), reply_id, str(sender))).fetchone()
        if photo_reply:
            return await self.handle_photo_reply(company, token, chat_id, sender, user, photo_reply, messages, mode, instruction, material)
        if reply and material and not mode and not any(self.attachment(item) for item in messages):
            await progress('Готовлю текст с учётом уточнения.', reply['post_id'])
            post = await self.service.answer(cid, reply['post_id'], reply['version'], material, actor=user)
            return await self.respond(company, token, chat_id, post, user=user)
        source_ids = []
        attachments = []
        try:
            for item in messages:
                if self.attachment(item):
                    attachments.append(await self.download(company, token, item, user))
        except DomainError as error:
            await progress('Ошибка. ' + error.detail)
            await self.send(token, chat_id, error.detail)
            return
        if material:
            source_ids.append(self.service.create_source(cid, material[:90], material, actor=user)['id'])
        source_ids.extend(source['id'] for source in attachments)
        if not source_ids and not mode:
            await progress('Нужен текст, фото или документ для поста.')
            return
        post = self.service.create_post(cid, source_ids, actor=user)
        await progress('Материалы сохранены.', post['id'])
        has_photo = any(source.get('kind') == 'image' for source in attachments)
        if mode or has_photo:
            return await self.prepare_photo(company, token, chat_id, sender, user, post, mode, instruction, has_photo)
        await self.generate_material(company, token, chat_id, user, post)

    @staticmethod
    def attachment(message):
        return message.get('document') or message.get('video') or (message.get('photo') or [None])[-1]

    async def download(self, company, token, message, user):
        await progress('Загружаю вложения из Telegram.')
        attachment = self.attachment(message)
        if attachment.get('file_size', 0) > 20 * 1024 * 1024:
            raise DomainError('Файл больше 20 МБ. Загрузите его через кабинет.', 413)
        for attempt in range(3):
            try:
                meta = await telegram_call(token, 'getFile', {'file_id': attachment['file_id']})
                break
            except IntegrationError as error:
                if error.status_code in (400, 401, 403, 404):
                    raise DomainError('Telegram не предоставил вложение. Отправьте файл заново.', 422) from None
                if attempt == 2:
                    raise DomainError('Не удалось получить вложение из Telegram после трёх попыток. До модели оно не дошло.', 502) from None
                await progress(f'Telegram временно недоступен. Повторяю загрузку ({attempt + 2}/3).')
                await asyncio.sleep(attempt + 1)
        path = meta['file_path']
        if '..' in path or '://' in path:
            raise ValueError('Invalid Telegram path')
        data = await self.download_bytes(token, path)
        filename = attachment.get('file_name') or path.rsplit('/', 1)[-1]
        mime = attachment.get('mime_type') or ('image/jpeg' if message.get('photo') else 'application/octet-stream')
        return self.service.store_upload(company['id'], filename, data, mime, actor=user)

    async def download_bytes(self, token, path):
        configured = http_options()
        # Only a read-only download is retried. Sending messages or generating
        # content must not inherit this policy: it could duplicate paid work.
        for attempt in range(3):
            options = {} if attempt == 1 else configured
            try:
                async with asyncio.timeout(60):
                    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10),
                                                 trust_env=False, follow_redirects=False, **options) as client:
                        async with client.stream('GET', f'https://api.telegram.org/file/bot{token}/{path}') as response:
                            if response.status_code == 429 or response.status_code >= 500:
                                response.raise_for_status()
                            if response.status_code != 200:
                                raise DomainError('Telegram не отдал файл. Отправьте вложение заново.', 422)
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                data.extend(chunk)
                                if len(data) > 20 * 1024 * 1024:
                                    raise DomainError('Файл больше 20 МБ. Загрузите его через кабинет.', 413)
                            return bytes(data)
            except (httpx.HTTPError, TimeoutError):
                if attempt == 2:
                    raise DomainError('Не удалось скачать вложение из Telegram после трёх попыток. До модели оно не дошло. Отправьте файл повторно или загрузите через кабинет.', 502) from None
                await progress(f'Соединение прервалось. Повторяю скачивание ({attempt + 2}/3).')
                await asyncio.sleep(attempt + 1)

    async def generate_material(self, company, token, chat_id, user, post):
        cid = company['id']
        await progress('Готовлю текст. Ожидаю ответ модели.', post['id'])
        try:
            post = await self.service.generate(cid, post['id'], post['version'], actor=user)
        except Exception as error:
            detail = error.detail if isinstance(error, DomainError) else 'Проверьте подключение в кабинете.'
            await progress('Не удалось подготовить текст. ' + detail)
            await self.send(token, chat_id, f'Материал сохранён. Пост №{post["id"]}. Не удалось подготовить текст — проверьте подключение в кабинете.')
            return
        await self.respond(company, token, chat_id, post, user=user)

    async def ask_photo(self, company, token, chat_id, sender, post, mode, question, instruction=''):
        await progress('Нужно уточнение — ответьте на сообщение ниже.', post['id'])
        if str(chat_id).startswith('-'):
            question += f'\nОтветьте на это сообщение с упоминанием @{company["bot_username"]}.'
        else:
            question += '\nОтветьте на это сообщение.'
        sent = await self.send(token, chat_id, f'Пост №{post["id"]}\n\n{question}')
        with self.db.transaction() as conn:
            conn.execute('INSERT OR REPLACE INTO bot_photo_replies VALUES (?,?,?,?,?,?,?,?)',
                         (company['id'], str(chat_id), sent['message_id'], post['id'], post['version'], mode or '', str(sender), instruction))

    async def prepare_photo(self, company, token, chat_id, sender, user, post, mode, instruction, has_photo):
        if not mode:
            return await self.ask_photo(company, token, chat_id, sender, post, mode,
                                        'Что сделать с фотографиями? #фото* — использовать оригиналы, #создать* — создать изображение, #изменить* — изменить оригиналы. Напишите факты о событии до команды, задание для изображения — после команды.')
        if mode != 'original' and not instruction:
            return await self.ask_photo(company, token, chat_id, sender, post, mode,
                                        'Какое изображение нужно создать или что нужно изменить? Опишите задание.')
        if mode in ('original', 'edit') and not has_photo:
            return await self.ask_photo(company, token, chat_id, sender, post, mode,
                                        'Добавьте исходную фотографию.', instruction)
        try:
            await progress('Сохраняю исходные фотографии.' if mode == 'original' else 'Готовлю изображение. Ожидаю ответ модели.', post['id'])
            post = await apply_photo(self.service, company['id'], post['id'], post['version'], mode, instruction, actor=user)
        except DomainError as error:
            return await self.ask_photo(company, token, chat_id, sender, post, mode, error.detail, instruction)
        if post.get('source_ids'):
            return await self.generate_material(company, token, chat_id, user, post)
        await progress('Изображение готово. Нужны факты для текста.', post['id'])
        await self.send(token, chat_id, f'Изображение сохранено в посте №{post["id"]}. Добавьте факты для текста в кабинете.')

    async def handle_photo_reply(self, company, token, chat_id, sender, user, reply, messages, mode, instruction, material):
        cid = company['id']
        post = self.service.post(cid, reply['post_id'])
        if post['version'] != reply['version']:
            await progress('Пост изменился. Продолжите работу в кабинете.', post['id'])
            return await self.send(token, chat_id, 'Пост изменился. Продолжите работу с актуальной версией в кабинете.')
        if mode is None and reply['mode']:
            mode = reply['mode']
            if mode != 'original':
                instruction = material or reply['instruction']
                material = ''
        source_ids = list(post.get('source_ids', []))
        for item in messages:
            if self.attachment(item):
                source_ids.append((await self.download(company, token, item, user))['id'])
        if material:
            source_ids.append(self.service.create_source(cid, material[:90], material, actor=user)['id'])
        if source_ids != post.get('source_ids', []):
            post = self.service.patch_post(cid, post['id'], post['version'], {'source_ids': source_ids}, actor=user)
        has_photo = any(self.service.source(cid, sid)['kind'] == 'image' for sid in source_ids)
        return await self.prepare_photo(company, token, chat_id, sender, user, post, mode, instruction, has_photo)

    async def menu(self, company, token, chat_id, action, user):
        role = user['role'] if user else None
        if action == 'id':
            text = f'Ваш Telegram ID: {chat_id}'
        elif not user:
            text = 'Войдите в кабинет компании и получите одноразовую ссылку в разделе привязки Telegram.'
        elif action == 'material':
            text = ('Пришлите текст, фото или документ. Текст начните со слов «Материал для поста:». '
                    'Укажите событие, дату, место и участников.'
                    if role in ('admin', 'editor') else 'Для передачи материалов нужны права редактора.')
        elif action == 'posts':
            labels = {'draft': 'Черновик', 'needs_info': 'Нужно уточнение', 'review': 'На согласовании',
                      'approved': 'Согласовано', 'published': 'Опубликовано', 'rejected': 'На доработку',
                      'failed': 'Ошибка отправки', 'publishing': 'Отправляется'}
            posts = self.service.posts(company['id'])[:10]
            text = '\n\n'.join(f'№{p["id"]} · {labels.get(p["status"], p["status"])}\n{p["title"]}' for p in posts) or 'Публикаций пока нет.'
        elif action == 'help':
            text = 'Пришлите материал боту. Ответы на уточнения отправляйте ответом на его сообщение. Согласование — кнопками под публикацией.\n\nДля исходных фотографий добавьте #фото*. Для нового изображения используйте #создать*, для редактирования — #изменить*. Факты о событии напишите до команды, задание для изображения — после команды.'
            text += '\n\n' + '\n'.join(f'{command} — {label.lower()}' for command, (_, label) in TASK_COMMANDS.items())
            text += '\n\nВ личном чате можно написать поручение обычными словами, например «Составь контент-план» или «Присылай отчёт каждый понедельник в 09:00».'
            if role == 'admin':
                text += '\n\n/allow ID — дать доступ\n/remove ID — убрать доступ\n/list — сотрудники'
        else:
            text = 'Выберите действие.'
        await self.send(token, chat_id, text, reply_markup=menu_markup(public_url(self.db), role))

    def post_controls(self, cid, post, user):
        allowed = can_review(user) if user else None
        if user and allowed and post.get('status') == 'review':
            with self.db.read() as conn:
                if configured_reviewer_ids(conn, cid) and user['id'] not in reviewer_ids(conn, cid):
                    allowed = False
        return post_buttons(post, user['role'] if user else None, can_approve=allowed)

    async def respond(self, company, token, chat_id, post, user=None):
        cid = company['id']
        if user is None and not str(chat_id).startswith('-'):
            user = self.service.telegram_members(cid, int(chat_id))
        if (user and user['role'] in ('admin', 'editor') and post.get('status') in ('draft', 'rejected', 'failed')
                and not post.get('questions') and not post.get('logo_pending') and post.get('source_ids') and post.get('body')):
            post = self.service.submit(cid, post['id'], post['version'], actor=user)
        if post.get('questions'):
            await progress('Нужно уточнение — ответьте на сообщение ниже.', post['id'])
            reply_hint = f'Ответьте на это сообщение с упоминанием @{company["bot_username"]}.' if str(chat_id).startswith('-') else 'Ответьте на это сообщение.'
            sent = await self.send(token, chat_id, f'Пост №{post["id"]}\n\n' + '\n'.join(post['questions']) + '\n\n' + reply_hint)
            with self.db.transaction() as conn:
                conn.execute('INSERT OR REPLACE INTO bot_replies VALUES (?,?,?,?,?)', (cid, str(chat_id), sent['message_id'], post['id'], post['version']))
        else:
            media = self.service.publication_media(cid, post)
            if media:
                await publish_telegram(token, str(chat_id), post['body'], media)
            preview = f'Пост №{post["id"]}' if media else f'Пост №{post["id"]}\n\n{post["body"]}'
            if post.get('logo_pending'):
                if user:
                    options = logo_options(self.service, cid, post['id'], post['version'], actor=user)
                    markup = logo_keyboard(post, options['variants'])
                    preview += '\nВыберите цвет и угол логотипа, затем нажмите «Применить».'
                else:
                    markup = {'inline_keyboard': self.post_controls(cid, post, user)}
            else:
                markup = {'inline_keyboard': self.post_controls(cid, post, user)}
            await self.send(token, chat_id, preview[:4096], reply_markup=markup)
            if post.get('status') == 'review' and user and str(user.get('telegram_id')) == str(chat_id):
                self.state(f'notify:{cid}:{post["id"]}:{post["version"]}:review:{chat_id}', 'done')
            await progress('Готово. Текст и вложения отправлены на проверку.', post['id'])

    async def callback(self, company, token, cb):
        cid = company['id']
        user = self.service.telegram_members(cid, cb.get('from', {}).get('id'))
        if await handle_intent_callback(self, company, token, cb, user):
            return
        try:
            origin = cb.get('message', {}).get('chat', {})
            private_origin = origin.get('type') == 'private' and str(origin.get('id')) == str(cb.get('from', {}).get('id'))
            group_origin = origin.get('type') in ('group', 'supergroup') and str(origin.get('id')) == str(company.get('chat_id'))
            if cb.get('from', {}).get('is_bot') or not (private_origin or group_origin):
                raise ValueError('Недопустимый чат.')
            if cb.get('data', '').startswith('menu:'):
                action = cb['data'].split(':', 1)[1]
                if not private_origin or action not in ('material', 'posts', 'id', 'help'):
                    raise ValueError('Недопустимое действие меню.')
                await self.menu(company, token, origin['id'], action, user)
                await telegram_call(token, 'answerCallbackQuery', {'callback_query_id': cb['id']})
                return
            parts = cb.get('data', '').split(':')
            if parts[0] in ('logo', 'logopick', 'logoapply'):
                if not user:
                    raise ValueError('Нет доступа.')
                await self.logo_callback(company, token, cb, parts, user)
                return
            action, raw_id, raw_version = parts
            pid, version = int(raw_id), int(raw_version)
            if not user:
                raise ValueError('Нет доступа.')
            if action in ('approve', 'reject', 'publish') and not can_review(user):
                raise ValueError('Нужны права согласующего.')
            if action == 'submit':
                self.service.submit(cid, pid, version, actor=user)
            elif action == 'approve':
                self.service.approve(cid, pid, version, actor=user)
            elif action == 'reject':
                self.service.reject(cid, pid, version, 'Отклонено в Telegram', actor=user)
            elif action == 'publish':
                await self.service.publish(cid, pid, version, actor=user)
            elif action == 'edit':
                with self.db.read() as conn:
                    self.service.require_actor(conn, cid, user, {'admin', 'editor', 'approver'})
                    post = self.service._post_row(conn, cid, pid)
                    self.service._editable(post, version)
                    if user['role'] == 'approver' and post['status'] not in ('review', 'approved'):
                        raise DomainError('Правки доступны согласующему на проверке.', 403)
                hint = 'Напишите, что нужно исправить. Передам правки редактору.' if user['role'] == 'approver' else 'Напишите, что изменить в тексте поста.'
                if group_origin:
                    hint += f' Добавьте упоминание @{company["bot_username"]}.'
                sent = await self.send(token, origin['id'], f'Пост №{pid}\n{hint}', reply_markup={'force_reply': True, 'selective': True})
                with self.db.transaction() as conn:
                    conn.execute('INSERT OR REPLACE INTO bot_edit_replies VALUES(?,?,?,?,?,?)',
                                 (cid, str(origin['id']), sent['message_id'], pid, version, str(cb['from']['id'])))
            else:
                raise ValueError('Неизвестное действие.')
            answer = 'Сохранено.'
        except Exception:
            answer = 'Действие не выполнено. Проверьте права, канал и текущую версию в кабинете.'
        await telegram_call(token, 'answerCallbackQuery', {'callback_query_id': cb['id'], 'text': answer, 'show_alert': True})

    async def handle_edit_reply(self, company, token, message, user):
        cid, chat_id = company['id'], message['chat']['id']
        reply_id = message.get('reply_to_message', {}).get('message_id')
        if reply_id is None:
            return False
        with self.db.read() as conn:
            reply = conn.execute('SELECT * FROM bot_edit_replies WHERE company_id=? AND chat_id=? AND message_id=?',
                                 (cid, str(chat_id), reply_id)).fetchone()
        if not reply:
            return False
        if str(message['from']['id']) != reply['telegram_id']:
            return True
        text = re.sub(rf'(?<!\w)@{re.escape(company["bot_username"])}(?!\w)', '', message.get('text', ''), flags=re.IGNORECASE).strip()
        if not text or len(text) > 4000:
            await self.send(token, chat_id, 'Пришлите правки текстом, до 4000 символов.')
            return True
        status = heartbeat = context = None
        try:
            if user['role'] == 'approver':
                self.service.reject(cid, reply['post_id'], reply['version'], text, actor=user)
                await self.send(token, chat_id, 'Правки сохранены. Пост возвращён редактору.')
            else:
                status = Progress(telegram_call, token, chat_id, 'Правки получены.')
                context = current_progress.set(status)
                await status.update('Обновляю текст.', reply['post_id'])
                heartbeat = asyncio.create_task(status.heartbeat())
                post = await self.service.revise(cid, reply['post_id'], reply['version'], text, actor=user)
                await self.respond(company, token, chat_id, post, user=user)
        except DomainError as error:
            if status:
                await status.update('Не удалось обновить текст. ' + error.detail)
            await self.send(token, chat_id, error.detail)
        finally:
            if heartbeat:
                await stop_heartbeat(heartbeat)
            if context is not None:
                current_progress.reset(context)
        return True

    async def logo_callback(self, company, token, cb, parts, user):
        action = parts[0]
        if len(parts) != (3 if action == 'logo' else 5):
            raise ValueError('Неверная кнопка логотипа.')
        pid, version = int(parts[1]), int(parts[2])
        cid = company['id']
        options = logo_options(self.service, cid, pid, version, actor=user)
        post = self.service.post(cid, pid)
        self.service._check_version(post, version)
        if action == 'logo':
            selected = options['placement'] or {}
            variant, corner = selected.get('variant'), selected.get('corner')
        else:
            variant, corner = (None if item == '-' else item for item in parts[3:])
            if (variant and variant not in options['variants']) or (corner and corner not in CORNERS):
                raise ValueError('Такого варианта логотипа нет.')
        chat_id = cb['message']['chat']['id']
        if action == 'logoapply':
            if not variant or not corner:
                raise ValueError('Выберите цвет и угол.')
            width = (options['placement'] or {}).get('width', 0.35)
            post = await asyncio.to_thread(apply_logo, self.service, cid, pid, version,
                                           {'variant': variant, 'corner': corner, 'width': width}, actor=user)
            await telegram_call(token, 'answerCallbackQuery', {'callback_query_id': cb['id'], 'text': 'Логотип обновлён.'})
            await self.respond(company, token, chat_id, post, user=user)
        else:
            await telegram_call(token, 'answerCallbackQuery', {'callback_query_id': cb['id']})
            text = f'Пост №{pid}\nВыберите цвет и угол логотипа, затем нажмите «Применить».'
            markup = logo_keyboard(post, options['variants'], variant, corner)
            if cb['message'].get('text') == text and cb['message'].get('reply_markup') == markup:
                return
            await telegram_call(token, 'editMessageText', {
                'chat_id': chat_id, 'message_id': cb['message']['message_id'],
                'text': text, 'reply_markup': markup,
            })

    async def notifications(self, company, token):
        cid = company['id']
        people = [user for user in self.service.telegram_users(cid) if user.get('telegram_id') and user.get('active', True)]
        with self.db.read() as conn:
            assigned = set(reviewer_ids(conn, cid)) if configured_reviewer_ids(conn, cid) else None
            # The task result already supplies this exact review version to its original recipient.
            task_recipients = set()
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_post_requests'"
            ).fetchone():
                task_recipients = {tuple(row) for row in conn.execute(
                    'SELECT p.post_id,p.post_version,t.actor_id,t.telegram_chat_id FROM assistant_tasks t '
                    'JOIN assistant_post_requests p ON p.request_id=t.post_request_id '
                    'WHERE t.company_id=? AND t.telegram_bot_id=? AND t.telegram_actor_id=t.telegram_chat_id '
                    'AND p.post_id IS NOT NULL UNION '
                    'SELECT p.post_id,p.post_version,t.notify_user_id,t.notify_chat_id FROM assistant_tasks t '
                    'JOIN assistant_post_requests p ON p.request_id=t.post_request_id '
                    'WHERE t.company_id=? AND t.notify_telegram=1 AND t.notify_bot_id=? AND p.post_id IS NOT NULL',
                    (cid, str(company.get('bot_id')), cid, str(company.get('bot_id'))),
                )}
        approvers = {
            str(user['telegram_id']): user for user in people
            if can_review(user) and (assigned is None or user['id'] in assigned)
        }
        authors = {user['id']: user for user in people}
        for post in self.service.posts(cid):
            status = post['status']
            if status not in ('review', 'rejected', 'approved', 'published', 'failed'):
                continue
            recipients = {} if status == 'rejected' else approvers.copy()
            author = authors.get(post.get('created_by'))
            if author and status != 'review':
                recipients.setdefault(str(author['telegram_id']), author)
            for user in recipients.values():
                if status == 'review' and can_review(user) and post.get('platform', 'telegram') == 'telegram':
                    target = (user['id'], str(user['telegram_id']))
                    if any((post['id'], version, *target) in task_recipients for version in (None, post['version'])):
                        continue
                key = f'notify:{cid}:{post["id"]}:{post["version"]}:{post["status"]}:{user["telegram_id"]}'
                if not self.claim(key):
                    continue
                try:
                    can_approve = str(user['telegram_id']) in approvers
                    if status == 'review':
                        platform = {'vk': 'ВКонтакте', 'max': 'MAX'}.get(post.get('platform'), 'Telegram')
                        buttons = self.post_controls(cid, post, user)
                        text = f'Пост №{post["id"]}\nПлощадка: {platform}\nКанал: {post.get("channel_id") or "не выбран"}\nВремя: {post.get("scheduled_at") or "после ручной отправки"}'
                        if platform == 'Telegram':
                            media = self.service.publication_media(cid, post)
                            await publish_telegram(token, str(user['telegram_id']), post['body'], media)
                        else:
                            excerpt = post['body'][:1200].strip()
                            if len(excerpt) < len(post['body'].strip()):
                                excerpt += '…'
                            text += f'\n\n{excerpt}\n\nПолный пост в кабинете.'
                            url = public_url(self.db)
                            if url:
                                buttons.append([{'text': 'Открыть кабинет', 'web_app': {'url': url}}])
                        await self.send(token, user['telegram_id'], text, reply_markup={'inline_keyboard': buttons})
                    elif status == 'approved':
                        has_channel = bool((post.get('channel_id') or '').strip())
                        buttons = []
                        if can_approve and has_channel and not post.get('scheduled_at'):
                            buttons = [[{'text': 'Опубликовать', 'callback_data': f'publish:{post["id"]}:{post["version"]}'}]]
                        buttons += self.post_controls(cid, post, user)
                        note = ' Без публикации: канал не выбран.' if not has_channel else (' Отправка по расписанию.' if post.get('scheduled_at') else '')
                        await self.send(token, user['telegram_id'], f'Пост №{post["id"]} согласован.' + note, reply_markup={'inline_keyboard': buttons})
                    elif status == 'rejected':
                        feedback = post.get('feedback')
                        details = f'\n\n{feedback[:1900]}' if feedback else ''
                        await self.send(token, user['telegram_id'], f'Пост №{post["id"]} возвращён на доработку.' + details)
                    elif status == 'published':
                        await self.send(token, user['telegram_id'], f'Пост №{post["id"]} опубликован.\n{post.get("publication_url") or ""}')
                    else:
                        await self.send(token, user['telegram_id'], f'Пост №{post["id"]} не отправлен или результат отправки неизвестен. Проверьте канал и кабинет.')
                    self.state(key, 'done')
                except Exception:
                    self.state(key, 'failed')
                    self.audit_error(cid, f'Не доставлено уведомление о посте №{post["id"]}.')

    async def calendar(self, company, token):
        if not company.get('calendar_enabled'):
            return
        cid = company['id']
        today = datetime.now(ZoneInfo(company.get('timezone') or 'Europe/Moscow')).date().isoformat()
        people = [p for p in self.service.telegram_users(cid) if p.get('telegram_id') and p.get('active', True)]
        for event in self.service.events(cid):
            if event['event_date'][:10] != today or event.get('reminded_at') or not people:
                continue
            delivered = False
            for user in people:
                key = f'event:{cid}:{event["id"]}:{today}:{user["telegram_id"]}'
                if self.claim(key):
                    try:
                        await self.send(token, user['telegram_id'], f'Сегодня: {event["title"]}\nПришлите факты и фотографии для поста.')
                        delivered = True
                        self.state(key, 'done')
                    except Exception:
                        self.state(key, 'failed')
                        self.audit_error(cid, f'Не доставлено напоминание о событии №{event["id"]}.')
            if delivered:
                self.service.mark_reminded(cid, event['id'])
