"""Browser workflows against an isolated database and synthetic provider responses."""
from io import BytesIO
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import expect, sync_playwright
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8002"
PASSWORD = "Browser-Controls-Only-123!"
KEY = "synthetic-shared-key-for-browser-test"
SERVER = r"""
from io import BytesIO
from PIL import Image
import uvicorn
from server import integrations, telegram_link
from server.main import create_app
from server.brand_images import prepare_brand, apply_brand, LogoPlacement

async def get_me(token, method, *args, **kwargs):
    assert method == 'getMe'
    return {'id': 42, 'username': 'editor_test_bot'}

async def image(company, sources, instruction, credentials):
    assert sources == []
    assert instruction == 'Бирюзовая стеклянная пластина'
    stream = BytesIO()
    Image.new('RGB', (160, 200), '#087f85').save(stream, format='PNG')
    original = stream.getvalue()
    placement = LogoPlacement(variant='primary', corner='bottom_right', width=0.35)
    return {'image_bytes': apply_brand(original, prepare_brand(company), placement), 'original_bytes': original,
            'logo_placement': placement.model_dump(), 'input_tokens': 5, 'output_tokens': 1,
            'layout_input_tokens': 10, 'layout_output_tokens': 2}

telegram_link.telegram_call = get_me
integrations.generate_photo = image
uvicorn.run(create_app(), host='127.0.0.1', port=8002, log_level='warning')
"""


def button(page, name):
    return page.get_by_role("button", name=name, exact=True)


def request(page, path, method="GET", payload=None):
    response = page.request.fetch(ORIGIN + "/api" + path, method=method, data=payload)
    assert response.ok, f"{method} {path}: {response.status} {response.text()}"
    return response.json() if response.status != 204 else None


def run_workflows(browser):
    owner = browser.new_page()
    owner.set_default_timeout(7500)
    errors = []
    owner.on("pageerror", lambda error: errors.append(str(error)))
    owner.route("**/api/companies/*/models?*", lambda route: route.fulfill(json=["gpt-4.1-mini"]))
    request(owner, "/auth/setup", "POST", {"name": "Владелец", "email": "owner@example.test", "password": PASSWORD})
    owner.goto(ORIGIN)

    button(owner, "Общие ключи").click()
    owner.get_by_label("Ключ OpenAI", exact=True).fill(KEY)
    button(owner, "Сохранить").click()
    expect(owner.get_by_text("Ключи сохранены", exact=True)).to_be_visible()
    expect(owner.get_by_label("Ключ OpenAI", exact=True)).to_have_value("")
    shared = request(owner, "/shared-api")
    assert shared["openai_configured"] and KEY not in str(shared)

    button(owner, "Настройки").click()
    owner.get_by_label("Источник ключей").select_option("shared")
    owner.get_by_label("Запросы", exact=True).fill("35")
    button(owner, "Сохранить настройки").click()
    expect(owner.get_by_text("Настройки сохранены", exact=True)).to_be_visible()
    settings = request(owner, "/companies/1/settings")
    assert settings["funding_mode"] == "shared" and settings["requests_limit"] == 35
    assert settings["openai_configured"] and KEY not in str(settings)

    owner.get_by_label("Компания", exact=True).select_option("new")
    owner.get_by_role("dialog").get_by_label("Название").fill("Проверка архива")
    button(owner.get_by_role("dialog"), "Создать").click()
    expect(owner.get_by_label("Компания", exact=True).locator("option:checked")).to_have_text("Проверка архива")
    button(owner, "Убрать компанию").click()
    dialog = owner.get_by_role("dialog")
    expect(dialog.get_by_text("Материалы и публикации сохранятся", exact=False)).to_be_visible()
    button(dialog, "Убрать компанию").click()
    expect(owner.get_by_label("Компания", exact=True).locator("option").filter(has_text="Проверка архива")).to_have_count(0)

    request(owner, "/companies/1", "PATCH", {"telegram_token": "123456:synthetic-for-browser"})
    invitation = request(owner, "/companies/1/invitations", "POST", {"role": "admin"})
    member = browser.new_page()
    member.set_default_timeout(7500)
    member.on("pageerror", lambda error: errors.append(str(error)))
    member.route("**/api/companies/*/models?*", lambda route: route.fulfill(json=["gpt-4.1-mini"]))
    request(member, f"/invitations/{invitation['token']}/accept", "POST", {
        "name": "Администратор компании", "email": "admin@example.test", "password": PASSWORD,
    })
    member.goto(ORIGIN)
    button(member, "Привязать Telegram").click()
    button(member, "Создать ссылку").click()
    open_bot = member.get_by_role("link", name="Открыть бота")
    expect(open_bot).to_be_visible()
    command = member.get_by_label("Одноразовая команда", exact=True).input_value()
    assert command.startswith("/start link_")
    assert open_bot.get_attribute("href") == "https://t.me/editor_test_bot?start=" + command.split()[1]
    button(member.get_by_role("dialog"), "Закрыть").click()

    button(member, "Настройки").click()
    expect(member.get_by_label("Источник ключей")).to_have_value("shared")
    expect(member.get_by_label("Запросы", exact=True)).to_be_disabled()
    expect(member.get_by_label("Ключ OpenAI", exact=True)).to_have_count(0)
    button(member, "Сохранить настройки").click()
    expect(member.get_by_text("Настройки сохранены", exact=True)).to_be_visible()

    button(member, "Публикации").click()
    button(member, "Новая публикация").click()
    member.get_by_label("Заголовок", exact=True).fill("Проверка изображения")
    button(member.get_by_role("dialog"), "Создать черновик").click()
    member.locator("summary").filter(has_text="Фотографии").click()
    expect(button(member, "Использовать оригиналы")).to_be_disabled()
    member.get_by_label("Режим", exact=True).select_option("edit")
    expect(button(member, "Оформить фотографии")).to_be_disabled()
    member.get_by_label("Режим", exact=True).select_option("create")
    member.get_by_label("Что должно быть на изображении", exact=True).fill("Бирюзовая стеклянная пластина")
    button(member, "Создать изображение").click()
    generated = member.get_by_alt_text("Оформленное изображение", exact=True)
    expect(generated).to_be_visible()
    expect(generated).to_have_js_property("complete", True)
    assert generated.evaluate("(img) => img.naturalWidth") == 160
    expect(member.get_by_label('Цвет логотипа', exact=True)).to_have_value('primary')
    expect(member.get_by_label('Угол фотографии', exact=True)).to_have_value('bottom_right')
    member.get_by_label('Цвет логотипа', exact=True).select_option('light')
    member.get_by_label('Угол фотографии', exact=True).select_option('top_left')
    before = generated.get_attribute('src')
    button(member, 'Применить логотип').click()
    expect(generated).not_to_have_attribute('src', before)
    expect(member.get_by_label('Цвет логотипа', exact=True)).to_have_value('light')
    member.get_by_label('Цвет логотипа', exact=True).select_option('primary')
    member.get_by_label('Угол фотографии', exact=True).select_option('bottom_right')
    before = generated.get_attribute('src')
    button(member, 'Применить логотип').click()
    expect(generated).not_to_have_attribute('src', before)
    assert request(member, '/companies/1/usage')['requests'] == 2

    button(member, "Материалы").click()
    button(member, "Добавить материал").click()
    button(member, "Файл").click()
    photo = BytesIO()
    Image.new("RGB", (16, 16), "#2679a7").save(photo, format="PNG")
    member.locator('input[name="file"]').set_input_files({
        "name": "original.png", "mimeType": "image/png", "buffer": photo.getvalue(),
    })
    button(member.get_by_role("dialog"), "Добавить").click()
    expect(member.get_by_role("dialog")).not_to_be_visible()
    button(member, "Публикации").click()
    member.get_by_role("button", name="Проверка изображения", exact=False).click()
    member.locator("#post-editor summary").filter(has_text="Материалы").click()
    member.get_by_label("original.png", exact=False).check()
    member.locator("#post-editor summary").filter(has_text="Фотографии").click()
    button(member, "Использовать оригиналы").click()
    expect(generated).to_have_count(0)
    button(member, "Предпросмотр").click()
    expect(member.get_by_alt_text("original.png", exact=True)).to_be_visible()

    button(member, 'База знаний').click()
    button(member, 'Добавить запись').click()
    dialog = member.get_by_role('dialog')
    dialog.get_by_label('Название', exact=True).fill('История предприятия')
    dialog.get_by_label('Текст', exact=True).fill('День рождения завода — 30 июня 1962 года.')
    dialog.get_by_label('Статус', exact=True).select_option('active')
    button(dialog, 'Сохранить').click()
    expect(member.get_by_text('История предприятия', exact=True)).to_be_visible()
    assert all(source['kind'] != 'knowledge' for source in request(member, '/companies/1/sources'))

    button(member, 'Задачи').click()
    button(member, 'Новая задача').click()
    dialog = member.get_by_role('dialog')
    dialog.get_by_label('Тип задачи').select_option('topics')
    dialog.get_by_label('Что нужно сделать?').fill('Предложи темы о стекле для сотрудников.')
    button(dialog, 'Отправить задачу').click()
    expect(member.get_by_role('dialog').get_by_text('В очереди', exact=True)).to_be_visible()
    member.reload()
    button(member, 'Задачи').click()
    expect(member.get_by_text('Предложи темы о стекле для сотрудников.', exact=True)).to_be_visible()

    button(member, 'Контент-план').click()
    button(member, 'Добавить в план').click()
    dialog = member.get_by_role('dialog')
    dialog.get_by_label('Название', exact=True).fill('История стекла')
    dialog.get_by_label('Что подготовить', exact=True).fill('Подготовить материал об истории предприятия.')
    button(dialog, 'Сохранить').click()
    expect(member.get_by_text('История стекла', exact=True)).to_be_visible()
    button(member, 'Регулярные задачи').click()
    expect(button(member, 'Добавить регулярную задачу')).to_be_visible()

    button(member, 'Обращения').click()
    expect(member.get_by_text('Обращений пока нет', exact=True)).to_be_visible()
    button(member, 'Статистика').click()
    expect(member.get_by_text('За выбранный период нет замеров', exact=True)).to_be_visible()
    button(member, 'Отчёты').click()
    button(member, 'Предпросмотр').click()
    expect(member.get_by_text('Замеров нет.', exact=False)).to_be_visible()
    button(member, 'Сохранить за период').click()
    dialog = member.get_by_role('dialog')
    expect(dialog.get_by_text('Редакционная сводка', exact=False)).to_be_visible()
    button(dialog, 'Закрыть').click()
    button(member, 'Добавить расписание').click()
    dialog = member.get_by_role('dialog')
    dialog.get_by_label('Повтор').select_option('daily')
    dialog.get_by_label('Время компании').fill('10:00')
    button(dialog, 'Сохранить расписание').click()
    expect(member.get_by_text('Ежедневно · 10:00', exact=True)).to_be_visible()
    button(member, 'Отключить').click()
    expect(member.get_by_text('Отключено', exact=True)).to_be_visible()
    assert request(member, '/companies/1/reports')['schedules'][0]['enabled'] is False
    member.set_viewport_size({'width': 390, 'height': 844})
    button(member, 'База знаний').click()
    expect(member.get_by_text('История предприятия', exact=True)).to_be_visible()
    assert member.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile overflow'
    artifacts = ROOT / 'test-results'
    artifacts.mkdir(exist_ok=True)
    member.screenshot(path=str(artifacts / 'features-mobile.png'), full_page=True)
    member.set_viewport_size({'width': 1440, 'height': 900})
    member.screenshot(path=str(artifacts / 'features-desktop.png'), full_page=True)
    assert not errors, errors


def main():
    with tempfile.TemporaryDirectory(prefix="salavat-controls-") as directory:
        environment = dict(os.environ, SALAVAT_DATA_DIR=directory)
        log_path = Path(directory) / "server.log"
        with log_path.open("w", encoding="utf-8") as log:
            server = subprocess.Popen([sys.executable, "-c", SERVER], cwd=ROOT, env=environment, stdout=log, stderr=log,
                                      creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        raise RuntimeError(log_path.read_text(encoding="utf-8"))
                    try:
                        urllib.request.urlopen(ORIGIN + "/api/auth/setup", timeout=1).close()
                        break
                    except OSError:
                        time.sleep(0.2)
                else:
                    raise RuntimeError("Browser test server did not start")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel="msedge", headless=True)
                    try:
                        run_workflows(browser)
                    finally:
                        browser.close()
            finally:
                if server.poll() is None:
                    server.terminate()
                    server.wait(timeout=15)
    print("Browser controls passed. No external services called.")


if __name__ == "__main__":
    main()
