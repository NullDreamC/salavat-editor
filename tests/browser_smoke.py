"""Real browser smoke against isolated data. No provider calls or publications."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'test-results'


def main():
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='salavat-browser-') as directory:
        env = dict(os.environ, SALAVAT_DATA_DIR=directory)
        log = (OUT / 'browser-server.log').open('w', encoding='utf-8')
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'server.main:app', '--host', '127.0.0.1', '--port', '8001', '--log-level', 'warning'],
                                   cwd=ROOT, env=env, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            for _ in range(100):
                try:
                    urllib.request.urlopen('http://127.0.0.1:8001/api/auth/setup', timeout=1)
                    break
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError('Server exited')
                    time.sleep(0.2)
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge', headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.set_default_timeout(10000)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto('http://127.0.0.1:8001')
                page.wait_for_load_state('networkidle')
                page.screenshot(path=str(OUT / 'setup.png'), full_page=True)
                page.get_by_label('Имя', exact=True).fill('Главный администратор')
                page.get_by_label('Почта', exact=True).fill('owner@example.test')
                page.get_by_label('Пароль', exact=True).fill('Browser-Local-Only-123!')
                page.get_by_role('button', name='Создать аккаунт', exact=True).click()
                expect(page.get_by_role('heading', name='Доступы', exact=True)).to_be_visible()
                owner_page = page
                owner_page.get_by_role('button', name='Пригласить', exact=True).click()
                owner_page.get_by_role('button', name='Создать ссылку', exact=True).click()
                invite_link = owner_page.get_by_label('Ссылка на 7 дней', exact=True)
                expect(invite_link).to_be_visible()
                url = invite_link.input_value()
                owner_page.get_by_role('button', name='Готово', exact=True).click()
                recipient = browser.new_context(viewport={'width': 1440, 'height': 1000})
                page = recipient.new_page()
                # Model discovery is covered with HTTP mocks separately; never call a paid account here.
                def models(route):
                    ids = ['aliceai-llm/latest', 'qwen3.6-35b-a3b'] if 'provider=yandex' in route.request.url else ['gpt-4.1-mini', 'live-text-model']
                    route.fulfill(json=ids)
                page.route('**/api/companies/*/models?*', models)
                owner_page.route('**/api/companies/*/models?*', models)
                page.set_default_timeout(10000)
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(url)
                page.get_by_label('Имя', exact=True).fill('Администратор компании')
                page.get_by_label('Почта', exact=True).fill('test@example.test')
                page.get_by_label('Пароль', exact=True).fill('Browser-Local-Only-123!')
                page.get_by_role('button', name='Принять приглашение', exact=True).click()
                expect(page.get_by_role('heading', name='Публикации', exact=True)).to_be_visible()
                page.get_by_role('button', name='Материалы', exact=True).click()
                page.get_by_role('button', name='Добавить материал', exact=True).click()
                page.get_by_label('Название', exact=True).fill('Учебный турнир')
                page.get_by_label('Текст', exact=True).fill('Учебный пример: 20 декабря 2026 года в 12:00 в спортивном зале состоится волейбольный турнир для сотрудников. Запись не требуется.')
                page.get_by_role('button', name='Добавить', exact=True).click()
                expect(page.get_by_role('dialog')).not_to_be_visible()
                page.get_by_label('Выбрать «Учебный турнир»', exact=True).check()
                page.get_by_role('button', name='Новая публикация (1)', exact=True).click()
                page.get_by_label('Заголовок', exact=True).fill('Волейбольный турнир')
                page.get_by_role('dialog').get_by_role('button', name='Создать черновик', exact=True).click()
                expect(page.get_by_role('dialog')).not_to_be_visible()
                page.get_by_label('Текст', exact=True).fill('Коллеги, приглашаем на волейбольный турнир.\n\n20 декабря 2026 года, 12:00. Спортивный зал. Запись не требуется.')
                page.locator('summary').filter(has_text='Канал и дата').click()
                page.get_by_label('Канал Telegram', exact=True).fill('@local_test_channel')
                page.get_by_role('button', name='Сохранить', exact=True).click()
                expect(page.get_by_role('button', name='Сохранить', exact=True)).not_to_be_visible()
                page.locator('summary').filter(has_text='Карточка').click()
                page.get_by_role('button', name='Создать карточку', exact=True).click()
                expect(page.get_by_alt_text('Карточка публикации')).to_be_visible()
                page.get_by_role('button', name='На согласование', exact=True).click()
                expect(page.get_by_role('button', name='Согласовать', exact=True)).to_be_visible()
                page.get_by_role('button', name='Согласовать', exact=True).click()
                expect(page.get_by_role('button', name='Опубликовать', exact=True)).to_be_visible()
                # The approval is invalidated by an edit; no real publication attempted.
                page.get_by_role('button', name='Редактор', exact=True).click()
                page.locator('#post-editor .editor-fields textarea').fill('Коллеги, учебный турнир состоится 20 декабря в 12:00. Спортивный зал. Запись не требуется.')
                page.get_by_role('button', name='Сохранить', exact=True).click()
                expect(page.get_by_role('button', name='Опубликовать', exact=True)).not_to_be_visible()
                expect(page.get_by_role('button', name='На согласование', exact=True)).to_be_visible()
                page.get_by_role('button', name='Предпросмотр', exact=True).click()
                page.screenshot(path=str(OUT / 'editor.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.screenshot(path=str(OUT / 'mobile.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile overflow'
                page.set_viewport_size({'width': 1440, 'height': 1000})
                page.get_by_role('button', name='Сотрудники', exact=True).click()
                for name, email, role in [('Редактор', 'editor@example.test', 'editor'), ('Согласующий', 'approver@example.test', 'approver')]:
                    page.get_by_role('button', name='Добавить сотрудника', exact=True).click()
                    page.get_by_label('Имя', exact=True).fill(name)
                    page.get_by_label('Почта', exact=True).fill(email)
                    page.get_by_label('Пароль', exact=True).fill('Local-Only-Password-123!')
                    page.get_by_role('dialog').locator('select[name="role"]').select_option(role)
                    page.get_by_role('dialog').get_by_role('button', name='Добавить', exact=True).click()
                    expect(page.get_by_role('dialog')).not_to_be_visible()
                page.get_by_role('button', name='Настройки', exact=True).click()
                expect(page.get_by_label('Модель', exact=True)).to_be_enabled()
                assert page.locator('input[name="model"]').count() == 0
                page.get_by_label('Модель', exact=True).select_option('live-text-model')
                page.locator('textarea[name="editorial_rules"]').fill('Короткие абзацы. Обращение: коллеги.')
                page.get_by_role('button', name='Сохранить настройки', exact=True).click()
                expect(page.get_by_text('Настройки сохранены', exact=True)).to_be_visible()
                assert page.request.get('http://127.0.0.1:8001/api/companies/1/settings').json()['editorial_rules'] == 'Короткие абзацы. Обращение: коллеги.'
                assert page.request.get('http://127.0.0.1:8001/api/companies/1/settings').json()['model'] == 'live-text-model'
                page.get_by_label('Сервис', exact=True).select_option('yandex')
                page.get_by_label('Модель', exact=True).select_option('aliceai-llm/latest')
                page.get_by_role('button', name='Сохранить настройки', exact=True).click()
                expect(page.get_by_text('Настройки сохранены', exact=True)).to_be_visible()
                assert page.request.get('http://127.0.0.1:8001/api/companies/1/settings').json()['model'] == 'aliceai-llm/latest'
                page.get_by_label('Сервис', exact=True).select_option('openai')
                page.get_by_label('Модель', exact=True).select_option('gpt-4.1-mini')
                page.get_by_role('button', name='Сохранить настройки', exact=True).click()
                expect(page.get_by_text('Настройки сохранены', exact=True)).to_be_visible()
                page.route('**/api/companies/*/models?*', lambda route: route.fulfill(status=502, json={'detail': 'Проверьте ключ модели.'}))
                page.get_by_role('button', name='Обновить модели', exact=True).click()
                expect(page.get_by_text('Проверьте ключ модели.', exact=True)).to_be_visible()
                expect(page.get_by_label('Модель', exact=True)).to_have_value('gpt-4.1-mini')
                page.unroute('**/api/companies/*/models?*')
                page.route('**/api/companies/*/models?*', models)
                page.get_by_role('button', name='Обновить модели', exact=True).click()
                expect(page.get_by_text('Проверьте ключ модели.', exact=True)).not_to_be_visible()
                page.screenshot(path=str(OUT / 'models.png'), full_page=True)
                assert page.request.get('http://127.0.0.1:8001/api/companies/2/users').status == 404
                assert [c['id'] for c in page.request.get('http://127.0.0.1:8001/api/companies').json()] == [1]
                page.reload()
                expect(page.get_by_role('heading', name='Публикации', exact=True)).to_be_visible()
                # Password changes run against temporary accounts only.
                page.get_by_role('button', name='Сменить пароль', exact=True).click()
                dialog = page.get_by_role('dialog')
                dialog.get_by_label('Текущий пароль', exact=True).fill('wrong-password')
                dialog.get_by_label('Новый пароль', exact=True).fill('Changed-Admin-Password-123!')
                dialog.get_by_label('Повторите пароль', exact=True).fill('Different-Password-123!')
                dialog.get_by_role('button', name='Сменить пароль', exact=True).click()
                expect(page.get_by_text('Пароли не совпадают.', exact=True)).to_be_visible()
                dialog.get_by_label('Повторите пароль', exact=True).fill('Changed-Admin-Password-123!')
                dialog.get_by_role('button', name='Сменить пароль', exact=True).click()
                expect(page.get_by_text('Текущий пароль указан неверно.', exact=True)).to_be_visible()
                dialog.get_by_label('Текущий пароль', exact=True).fill('Browser-Local-Only-123!')
                page.screenshot(path=str(OUT / 'password.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.screenshot(path=str(OUT / 'password-mobile.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                dialog.get_by_role('button', name='Сменить пароль', exact=True).click()
                expect(page.get_by_text('Пароль изменён. Войдите снова.', exact=True)).to_be_visible()
                page.get_by_label('Почта', exact=True).fill('test@example.test')
                page.get_by_label('Пароль', exact=True).fill('Browser-Local-Only-123!')
                page.get_by_role('button', name='Войти', exact=True).click()
                expect(page.get_by_text('Неверная почта или пароль.', exact=True)).to_be_visible()
                page.get_by_label('Пароль', exact=True).fill('Changed-Admin-Password-123!')
                page.get_by_role('button', name='Войти', exact=True).click()
                expect(page.get_by_role('heading', name='Публикации', exact=True)).to_be_visible()
                page.set_viewport_size({'width': 1440, 'height': 1000})
                page.get_by_role('button', name='Сотрудники', exact=True).click()
                employee_context = browser.new_context()
                assert employee_context.request.post('http://127.0.0.1:8001/api/auth/login', data={
                    'email': 'editor@example.test', 'password': 'Local-Only-Password-123!'}).status == 200
                page.locator('button.employee-row').filter(has_text='editor@example.test').click()
                page.get_by_role('button', name='Сбросить пароль', exact=True).click()
                page.get_by_label('Новый пароль', exact=True).fill('Reset-Employee-Password-123!')
                page.get_by_label('Повторите пароль', exact=True).fill('Reset-Employee-Password-123!')
                page.get_by_role('dialog').get_by_role('button', name='Сбросить пароль', exact=True).click()
                expect(page.get_by_text('Пароль изменён. Сеансы сотрудника завершены.', exact=True)).to_be_visible()
                assert employee_context.request.get('http://127.0.0.1:8001/api/auth/me').status == 401
                assert employee_context.request.post('http://127.0.0.1:8001/api/auth/login', data={
                    'email': 'editor@example.test', 'password': 'Reset-Employee-Password-123!'}).status == 200
                employee_context.close()
                page.get_by_role('button', name='Готово', exact=True).click()
                expect(page.get_by_role('dialog')).not_to_be_visible()
                owner_page.reload()
                expect(owner_page.get_by_text('Профиль #2', exact=True)).to_be_visible()
                for secret in ('test@example.test', 'editor@example.test', 'approver@example.test', 'Администратор компании'):
                    # Company role label is allowed, identities are not.
                    if '@' in secret:
                        assert secret not in owner_page.locator('body').inner_text()
                assert owner_page.get_by_role('button', name='Сотрудники', exact=True).count() == 0
                assert owner_page.get_by_role('button', name='Материалы', exact=True).count() == 0
                owner_page.locator('button.employee-row').filter(has_text='Профиль #3').click()
                owner_page.get_by_role('dialog').locator('select[name="role"]').select_option('approver')
                owner_page.get_by_role('button', name='Сохранить', exact=True).click()
                expect(owner_page.get_by_role('dialog')).not_to_be_visible()
                owner_page.screenshot(path=str(OUT / 'access.png'), full_page=True)
                owner_page.set_viewport_size({'width': 390, 'height': 844})
                owner_page.screenshot(path=str(OUT / 'access-mobile.png'), full_page=True)
                assert owner_page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                owner_page.set_viewport_size({'width': 1440, 'height': 1000})
                owner_page.get_by_label('Компания', exact=True).select_option('new')
                owner_page.get_by_role('dialog').get_by_label('Название', exact=True).fill('Новая компания')
                owner_page.get_by_role('dialog').get_by_role('button', name='Создать', exact=True).click()
                expect(owner_page.get_by_role('heading', name='Настройки', exact=True)).to_be_visible()
                assert not errors, errors
                print('Browser smoke passed: owner invites, private company admin signup, anonymous access management, company creation, editorial flow, company isolation, mobile, password change and reset. No external requests.')
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=15)
            log.close()


if __name__ == '__main__':
    main()
