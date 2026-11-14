import asyncio
from io import BytesIO
from unittest.mock import AsyncMock

from PIL import Image, ImageDraw, ImageChops
import pytest

from server import domain, photo_workflow, logo_workflow
from server.brand_images import BrandImageError
from server.db import Database


def png(colour):
    image = BytesIO()
    Image.new("RGB", (160, 200), colour).save(image, "PNG")
    return image.getvalue()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db = Database(tmp_path / "photo.sqlite3")
    db.initialize()
    monkeypatch.setattr(domain, "reveal_secret", lambda value: value.decode() if value else "")
    monkeypatch.setattr(photo_workflow, "prepare_brand", lambda company: None)
    with db.transaction() as connection:
        connection.execute("INSERT INTO companies(id,name,openai_key,telegram_token,channel_id) VALUES(2,'Завод',?,?,'@destination')",
                           (b"openai-key", b"2:bot"))
        connection.executemany("INSERT INTO users(id,name,email,password_hash) VALUES(?,?,?,?)",
                               [(1, "Редактор", "editor@example.test", "set"), (2, "Согласующий", "approver@example.test", "set")])
        connection.executemany("INSERT INTO memberships(company_id,user_id,role) VALUES(2,?,?)", [(1, "editor"), (2, "approver")])
    service = domain.Service(db)
    brand_dir = db.path.parent / 'brands' / '2'
    brand_dir.mkdir(parents=True)
    for variant, colour in [('light', 'white'), ('dark', 'black'), ('primary', '#156E80')]:
        mark = Image.new('RGBA', (48, 16))
        ImageDraw.Draw(mark).rectangle((3, 2, 42, 12), fill=colour)
        mark.save(brand_dir / (variant + '.png'))
    photos = [service.store_upload(2, f"{name}.png", png(name), "image/png", actor=1) for name in ("white", "red")]
    post = service.create_post(2, [photo["id"] for photo in photos], title="Фотографии", actor=1)
    post = service.patch_post(2, post["id"], post["version"], {"body": "Фотографии с мероприятия."}, actor=1)
    generated = AsyncMock(return_value={"image_bytes": png("blue"), "input_tokens": 42, "output_tokens": 3})
    monkeypatch.setattr(domain.integrations, "generate_photo", generated)
    return service, post, photos, generated


def test_exact_photo_directive_keeps_facts_separate():
    parse = photo_workflow.photo_directive
    assert parse("Обычный тег #фото") == (None, "Обычный тег #фото", "")
    assert parse("#фото**") == (None, "#фото**", "")
    assert parse("Встреча 1 июля. #фото*") == ("original", "Встреча 1 июля.", "")
    assert parse("Встреча 1 июля. #изменить* Убрать блики") == ("edit", "Встреча 1 июля.", "Убрать блики")
    with pytest.raises(domain.DomainError):
        parse("#создать* #изменить*")


def test_generated_photo_replaces_album_only_for_publication_and_original_mode_restores_it(setup, monkeypatch):
    service, post, photos, generate = setup
    review = service.submit(2, post["id"], post["version"], actor=1)
    approved = service.approve(2, post["id"], review["version"], actor=2)

    def apply(state, mode, instruction=""):
        return asyncio.run(photo_workflow.apply_photo(service, 2, post["id"], state["version"], mode, instruction, actor=1))

    edited = apply(approved, "edit", "Убрать блики")
    assert edited["status"] == "draft" and edited["version"] > approved["version"]
    assert edited["source_ids"] == [photo["id"] for photo in photos]
    assert [source["id"] for source in generate.await_args.args[1]] == edited["source_ids"]
    assert len(service.publication_media(2, edited)) == 1
    assert edited["photo_url"] == service.publication_media(2, edited)[0]["file_url"]
    restored = apply(edited, "original")
    assert restored["photo_url"] is None
    assert [item["id"] for item in service.publication_media(2, restored)] == restored["source_ids"]
    generate.assert_awaited_once()

    created = apply(restored, "create", "Иллюстрация стекольного производства")
    assert generate.await_args.args[1] == []
    assert created['logo_pending'] and created['logo_editable']
    with pytest.raises(domain.DomainError, match='логотипа'):
        service.submit(2, post['id'], created['version'], actor=1)
    created = logo_workflow.apply_logo(service, 2, post['id'], created['version'],
                                      {'variant': 'primary', 'corner': 'top_left', 'width': 0.35}, actor=1)
    publish = AsyncMock(return_value={"message_id": 71})
    monkeypatch.setattr(domain.integrations, "publish_telegram", publish)
    review = service.submit(2, post["id"], created["version"], actor=1)
    approved = service.approve(2, post["id"], review["version"], actor=2)
    published = asyncio.run(service.publish(2, post["id"], approved["version"], actor=2))

    assert published["status"] == "published"
    media = publish.await_args.args[3]
    assert [item["file_url"] for item in media] == [created["photo_url"]]
    assert service.usage(2)["requests"] == 4
    assert service.usage(2)["input_tokens"] == 84


def test_photo_requires_instruction_and_brand_before_reserving_quota(setup, monkeypatch):
    service, post, _, generate = setup

    def fail_brand(_):
        raise BrandImageError("Сначала добавьте логотипы.")

    monkeypatch.setattr(photo_workflow, "prepare_brand", fail_brand)
    for mode, instruction, message in (("edit", "", "задание"), ("create", "Описание", "логотипы")):
        with pytest.raises(domain.DomainError, match=message):
            asyncio.run(photo_workflow.apply_photo(service, 2, post["id"], post["version"], mode, instruction, actor=1))

    generate.assert_not_awaited()
    assert service.usage(2)["requests"] == 0
    assert service.post(2, post["id"]) == post


def test_logo_replacement_preserves_clean_image_text_and_quota_and_invalidates_approval(setup):
    service, post, _, generate = setup
    raw = asyncio.run(photo_workflow.apply_photo(service, 2, post['id'], post['version'], 'create', 'Фото', actor=1))
    original = service.publication_media(2, raw)[0]
    with open(original['file_path'], 'rb') as stream:
        original_bytes = stream.read()
    first = logo_workflow.apply_logo(service, 2, post['id'], raw['version'],
                                    {'variant': 'light', 'corner': 'top_left', 'width': 0.35}, actor=1)
    review = service.submit(2, post['id'], first['version'], actor=1)
    approved = service.approve(2, post['id'], review['version'], actor=2)
    second = logo_workflow.apply_logo(service, 2, post['id'], approved['version'],
                                     {'variant': 'dark', 'corner': 'bottom_right', 'width': 0.35}, actor=2)
    assert second['status'] == 'review' and not second['logo_pending']
    assert second['body'] == post['body'] and second['source_ids'] == post['source_ids']
    assert second['logo']['variant'] == 'dark'
    assert service.usage(2)['requests'] == 2
    generate.assert_awaited_once()
    with service.db.read() as c:
        row = c.execute('SELECT * FROM posts WHERE id=?', (post['id'],)).fetchone()
        assert row['approved_snapshot'] is None and row['photo_base_id'] == original['id']
    rendered = Image.open(service.publication_media(2, second)[0]['file_path'])
    clean = Image.open(BytesIO(original_bytes))
    # The old corner is restored exactly; only the new corner differs.
    assert rendered.crop((0, 0, 160, 160)).tobytes() == clean.crop((0, 0, 160, 160)).tobytes()
    assert ImageChops.difference(rendered, clean).getbbox() == (98, 179, 154, 194)
    with open(original['file_path'], 'rb') as stream:
        assert stream.read() == original_bytes
    with pytest.raises(domain.DomainError, match='Версия'):
        service.approve(2, post['id'], approved['version'], actor=2)


def test_logo_options_and_changes_recheck_versions_roles_and_tenant(setup):
    service, post, _, _ = setup
    raw = asyncio.run(photo_workflow.apply_photo(service, 2, post['id'], post['version'], 'create', 'Фото', actor=1))
    placement = {'variant': 'primary', 'corner': 'top_right', 'width': 0.35}
    assert logo_workflow.logo_options(service, 2, post['id'], raw['version'], actor=1)['variants'] == ['primary', 'light', 'dark']
    with pytest.raises(domain.DomainError, match='Версия'):
        logo_workflow.apply_logo(service, 2, post['id'], post['version'], placement, actor=1)
    with pytest.raises(domain.DomainError, match='согласующему'):
        logo_workflow.apply_logo(service, 2, post['id'], raw['version'], placement, actor=2)
    with service.db.transaction() as c:
        c.execute("INSERT INTO companies(id,name) VALUES(3,'Другая')")
        c.execute("INSERT INTO memberships(company_id,user_id,role) VALUES(3,1,'admin')")
    with pytest.raises(domain.DomainError) as error:
        logo_workflow.apply_logo(service, 3, post['id'], raw['version'], placement, actor=1)
    assert error.value.status_code == 404
    with service.db.transaction() as c:
        c.execute('UPDATE memberships SET active=0 WHERE company_id=2 AND user_id=1')
    with pytest.raises(domain.DomainError) as error:
        logo_workflow.apply_logo(service, 2, post['id'], raw['version'], placement, actor=1)
    assert error.value.status_code == 403
    assert service.post(2, post['id']) == raw


def test_logo_keyboard_requires_both_choices_and_limits_callback_size():
    post = {'id': 12345, 'version': 999, 'logo_pending': True, 'logo_editable': True}
    def buttons(variant=None, corner=None):
        return [button for row in logo_workflow.logo_keyboard(post, ['primary', 'light', 'dark'], variant, corner)['inline_keyboard'] for button in row]
    assert not any(item['text'] == 'Применить' for item in buttons('light'))
    assert not any(item['text'] == 'Применить' for item in buttons(corner='top_left'))
    selected = buttons('light', 'top_left')
    assert sum(item['text'].startswith('✓ ') for item in selected) == 2
    assert selected[-1]['callback_data'] == 'logoapply:12345:999:light:top_left'
    assert all(len(item['callback_data'].encode()) <= 64 for item in selected)
    assert all(not b['callback_data'].startswith('submit:') for row in logo_workflow.post_buttons(post) for b in row)


def test_automatic_logo_stores_clean_base_and_still_allows_manual_change(setup):
    service, post, _, generated = setup
    placement = {'variant': 'light', 'corner': 'top_left', 'width': 0.35}
    from server.brand_images import prepare_brand, apply_brand, LogoPlacement
    brand = prepare_brand({'id': 2, 'brand_root': service.db.path.parent / 'brands'})
    raw = png('blue')
    generated.return_value = {'image_bytes': apply_brand(raw, brand, LogoPlacement(**placement)),
                             'original_bytes': raw, 'logo_placement': placement,
                             'input_tokens': 42, 'output_tokens': 3, 'layout_input_tokens': 12, 'layout_output_tokens': 5}
    ready = asyncio.run(photo_workflow.apply_photo(service, 2, post['id'], post['version'], 'create', 'Фото', actor=1))
    assert not ready['logo_pending'] and ready['logo'] == placement
    with service.db.read() as c:
        row = c.execute('SELECT * FROM posts WHERE id=?', (post['id'],)).fetchone()
        assert row['photo_base_id'] != row['photo_id']
        from pathlib import Path
        base = service._source_row(c, 2, row['photo_base_id'])
        assert Path(base['file_path']).read_bytes() == raw
    review = service.submit(2, post['id'], ready['version'], actor=1)
    changed = logo_workflow.apply_logo(service, 2, post['id'], review['version'],
                                      {'variant': 'primary', 'corner': 'bottom_right', 'width': 0.35}, actor=2)
    assert changed['status'] == 'review' and changed['body'] == post['body']
    generated.assert_awaited_once()
    assert service.usage(2)['requests'] == 2
    assert service.usage(2)['input_tokens'] == 54
