"""Explicit public origin for the optional Telegram WebApp tunnel."""
import json
from urllib.parse import urlsplit
from starlette.middleware.trustedhost import TrustedHostMiddleware


def validate_webapp_url(value):
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ('', '/') or url.port not in (None, 443)):
        raise ValueError('Укажите HTTPS-адрес кабинета без пути и параметров.')
    return f'https://{url.hostname}'


def public_url(db):
    try:
        value = json.loads((db.path.parent / 'webapp.json').read_text(encoding='utf-8'))['url']
        return validate_webapp_url(value)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ''


class WebAppHostMiddleware:
    def __init__(self, app, db, allow_test=False):
        self.app, self.db, self.allow_test = app, db, allow_test

    async def __call__(self, scope, receive, send):
        hosts = ['localhost', '127.0.0.1', '[::1]']
        url = public_url(self.db)
        if url:
            hosts.append(urlsplit(url).hostname)
        if self.allow_test:
            hosts.append('testserver')
        await TrustedHostMiddleware(self.app, allowed_hosts=hosts)(scope, receive, send)


BOT_COMMANDS = [
    {'command': 'start', 'description': 'Открыть меню'},
    {'command': 'material', 'description': 'Передать материал'},
    {'command': 'posts', 'description': 'Публикации'},
    {'command': 'ask', 'description': 'Задать вопрос'},
    {'command': 'topics', 'description': 'Подобрать темы'},
    {'command': 'plan', 'description': 'Составить контент-план'},
    {'command': 'strategy', 'description': 'Подготовить контент-стратегию'},
    {'command': 'video', 'description': 'Подготовить сценарий видео'},
    {'command': 'id', 'description': 'Мой Telegram ID'},
    {'command': 'help', 'description': 'Помощь'},
]


async def configure_bot(call, token, url):
    await call(token, 'setMyCommands', {'commands': BOT_COMMANDS, 'scope': {'type': 'all_private_chats'}})
    menu = {'type': 'web_app', 'text': 'Кабинет', 'web_app': {'url': url}} if url else {'type': 'commands'}
    await call(token, 'setChatMenuButton', {'menu_button': menu})


def menu_markup(url, role=None):
    buttons = []
    if url:
        buttons.append([{'text': 'Открыть кабинет', 'web_app': {'url': url}}])
    if role in ('admin', 'editor'):
        buttons.append([{'text': 'Передать материал', 'callback_data': 'menu:material'}])
    if role:
        buttons.append([{'text': 'Публикации', 'callback_data': 'menu:posts'}])
    buttons.append([{'text': 'Мой ID', 'callback_data': 'menu:id'}, {'text': 'Помощь', 'callback_data': 'menu:help'}])
    return {'inline_keyboard': buttons}
