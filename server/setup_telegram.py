"""Configure the explicitly selected company's bot without displaying its token."""
import argparse
import asyncio
import json
import re
from pathlib import Path

from .db import Database
from .domain import Service
from .integrations import telegram_call, IntegrationError
from .webapp import configure_bot, validate_webapp_url


async def configuration_call(token, method, payload=None):
    # These reads/setters are idempotent. Never use this retry policy for messages.
    for attempt in range(4):
        try:
            return await telegram_call(token, method, payload)
        except IntegrationError as error:
            if attempt == 3 or error.status_code in (400, 401, 403, 404):
                raise
            await asyncio.sleep(min(getattr(error, 'retry_after', None) or 1 + attempt, 10))


async def setup(company_id, token_file, url):
    content = Path(token_file).read_text(encoding='utf-8-sig').strip()
    match = re.fullmatch(r'(?:[A-Z_]+\s*=\s*)?[\"\']?(\d{5,20}:[A-Za-z0-9_-]{30,100})[\"\']?', content)
    if not match:
        raise ValueError('В файле должен быть только токен Telegram или TELEGRAM_BOT_TOKEN=токен.')
    token = match[1]
    url = validate_webapp_url(url) if url else ''
    db = Database()
    db.initialize()
    service = Service(db)
    service.company(company_id)
    identity = await configuration_call(token, 'getMe')
    if not identity.get('is_bot'):
        raise ValueError('Токен не принадлежит боту.')
    # Save only after Telegram accepts the credential. DPAPI protects the database copy.
    service.update_company(company_id, {'telegram_token': token, 'bot_username': identity['username']})
    webhook = await configuration_call(token, 'getWebhookInfo')
    if webhook.get('url'):
        await configuration_call(token, 'deleteWebhook', {'drop_pending_updates': False})
    await configure_bot(configuration_call, token, url)
    if url:
        (db.path.parent / 'webapp.json').write_text(json.dumps({'url': url}), encoding='utf-8')
    menu = await configuration_call(token, 'getChatMenuButton')
    print(json.dumps({'bot': identity['username'], 'company_id': company_id, 'menu': menu}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--company', type=int, required=True)
    parser.add_argument('--token-file', default='TGAPI.txt')
    parser.add_argument('--webapp-url', default='')
    args = parser.parse_args()
    try:
        asyncio.run(setup(args.company, args.token_file, args.webapp_url))
    except Exception as error:
        # Never emit raw transport errors or a traceback containing a token URL.
        from .integrations import IntegrationError
        print(str(error) if isinstance(error, (IntegrationError, ValueError)) else 'Не удалось настроить бота. Проверьте файл и подключение.')
        raise SystemExit(1) from None
