import json
import re
import secrets
from typing import Annotated, Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import integrations
from .brand_images import BrandImageError, prepare_brand
from .domain import EDITOR_ROLES, DomainError, audit_row, pack, upload_content


MODES = {"фото": "original", "создать": "create", "изменить": "edit"}
DIRECTIVE = re.compile(r"(?<!\w)#(фото|создать|изменить)\*(?![\w*])", re.IGNORECASE)
PHOTO_MIMES = {"image/jpeg", "image/png", "image/webp"}


def photo_directive(text):
    matches = list(DIRECTIVE.finditer(text))
    if not matches:
        return None, text, ""
    modes = {MODES[match[1].lower()] for match in matches}
    if len(modes) != 1:
        raise DomainError("Выберите один режим: #фото*, #создать* или #изменить*.", 422)
    marker = matches[0]
    mode = modes.pop()
    if mode == "original":
        return mode, DIRECTIVE.sub("", text).strip(), ""
    # Facts before the directive are separate from the image brief after it.
    return mode, text[:marker.start()].strip(), DIRECTIVE.sub("", text[marker.end():]).strip()


class PhotoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    version: int = Field(ge=1)
    mode: Literal["original", "create", "edit"]
    instruction: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] = ""
    source_id: int | None = Field(default=None, gt=0)


def _reset(service, connection, company_id, row, actor, photo_id, action, *, photo_base_id=None, logo_placement=None):
    audit_row(connection, company_id, action, f"Обновлены фотографии поста №{row['id']}.", actor)
    return service._update_post(
        connection, company_id, row["id"], photo_id=photo_id, card_id=None,
        photo_base_id=photo_base_id, logo_placement=pack(logo_placement) if logo_placement else None,
        status="needs_info" if json.loads(row["questions"]) else "draft",
        approved_snapshot=None, feedback=None, publication_uncertain=0,
    )


def _photo_state(service, connection, company_id, post_id, version, actor):
    company = service._company_row(connection, company_id)
    service.require_actor(connection, company_id, actor, EDITOR_ROLES)
    row = service._post_row(connection, company_id, post_id)
    service._editable(row, version)
    return company, row


async def apply_photo(service, company_id, post_id, version, mode, instruction="", source_id=None, *, actor, check=None):
    instruction = instruction.strip()
    if mode != "original" and not instruction:
        raise DomainError("Какое изображение нужно создать или что нужно изменить? Добавьте задание для изображения.", 422)
    with service.db.transaction() as connection:
        if check is not None:
            check(connection)
        company_row, row = _photo_state(service, connection, company_id, post_id, version, actor)
        ids = json.loads(row["source_ids"])
        photos = [source for sid in ids if (source := service._source_row(connection, company_id, sid))["kind"] == "image"]
        if mode == "original":
            if not photos:
                raise DomainError("Добавьте исходную фотографию к посту.", 422)
            if not row["photo_id"] and not row["card_id"]:
                return service._post_data(row)
            return _reset(service, connection, company_id, row, actor, None, "post.photo_original")
        if source_id is not None:
            photos = [source for source in photos if source["id"] == source_id]
            if not photos:
                raise DomainError("Выберите исходную фотографию этого поста.", 422)
        if mode == "edit" and not photos:
            raise DomainError("Какую фотографию нужно изменить? Добавьте оригинал к посту.", 422)
        references = photos if mode == "edit" or source_id is not None else []
        if len(references) > 6 or any(photo["mime_type"] not in PHOTO_MIMES for photo in references):
            raise DomainError("Выберите до шести фотографий в формате JPEG, PNG или WebP.", 422)
        sources = [service._source_data(photo, internal=True) for photo in references]
        company, credentials = service.provider_context(connection, company_row, provider="openai")
        if not credentials.get("openai_key") and not (
            company.get("allow_openai_fallback") and integrations.openai_fallback_available()
        ):
            raise DomainError("Подключите OpenAI для работы с изображениями.", 422)
        company["brand_root"] = service.db.path.parent / "brands"
        try:
            prepare_brand(company)
        except BrandImageError as error:
            raise DomainError(str(error), 422) from error
        usage_id = service._reserve_generation(connection, company, company_id)
        layout_usage_id = service._reserve_generation(connection, company, company_id)
    try:
        result = await integrations.generate_photo(company, sources, instruction, credentials)
    except integrations.GenerationError as error:
        with service.db.transaction() as connection:
            connection.execute("UPDATE usage SET input_tokens=?,output_tokens=? WHERE id=?",
                               (error.input_tokens, error.output_tokens, usage_id))
            audit_row(connection, company_id, "post.photo_failed", f"Не удалось подготовить фото поста №{post_id}.", actor)
        raise DomainError(str(error), 429 if error.status_code == 429 else 502) from error
    with service.db.transaction() as connection:
        connection.execute("UPDATE usage SET input_tokens=?,output_tokens=? WHERE id=?",
                           (result["input_tokens"], result["output_tokens"], usage_id))
        connection.execute("UPDATE usage SET input_tokens=?,output_tokens=? WHERE id=?",
                           (result.get('layout_input_tokens', 0), result.get('layout_output_tokens', 0), layout_usage_id))
    content = result["image_bytes"]
    original = result.get('original_bytes', content)
    kind, mime, _ = upload_content("photo.png", content, "image/png")
    if mime != "image/png":
        raise DomainError("Сервис вернул изображение в неожиданном формате.", 502)
    if upload_content('original.png', original, 'image/png')[1] != 'image/png':
        raise DomainError("Сервис вернул исходное изображение в неожиданном формате.", 502)
    folder = service.db.uploads_dir / str(company_id)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / f"{secrets.token_hex(20)}.png"
    base_path = folder / f"{secrets.token_hex(20)}.png" if result.get('logo_placement') else path
    try:
        with service.db.transaction() as connection:
            if check is not None:
                check(connection)
            _, row = _photo_state(service, connection, company_id, post_id, version, actor)
            with path.open("xb") as stream:
                stream.write(content)
            if base_path != path:
                with base_path.open('xb') as stream:
                    stream.write(original)
                base_id = service._insert_source(connection, company_id, f"Изображение поста №{post_id} без логотипа", "", kind,
                                                  str(base_path), mime)
            photo_id = service._insert_source(connection, company_id, f"Изображение поста №{post_id}", "", kind,
                                              str(path), mime)
            if result.get('logo_error'):
                audit_row(connection, company_id, 'post.logo_auto_failed', f'Выберите логотип поста №{post_id} кнопками.', actor)
            return _reset(service, connection, company_id, row, actor, photo_id, "post.photo_generated",
                          photo_base_id=base_id if base_path != path else photo_id, logo_placement=result.get('logo_placement'))
    except BaseException:
        path.unlink(missing_ok=True)
        if base_path != path:
            base_path.unlink(missing_ok=True)
        raise


def register_photo_routes(api, service, editor):
    @api.post("/companies/{cid}/posts/{post_id}/photo")
    async def post_photo(cid: int, post_id: int, payload: PhotoRequest, user=Depends(editor)):
        return await apply_photo(service, cid, post_id, **payload.model_dump(), actor=user)
