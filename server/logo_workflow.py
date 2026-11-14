"""Reapply the original logo to the saved clean image, without a provider request."""
import json
from pathlib import Path
import secrets

from fastapi import Depends
from pydantic import Field

from .brand_images import BrandImageError, LogoPlacement, apply_brand, load_brand_config, prepare_brand
from .domain import APPROVER_ROLES, EDITOR_ROLES, DomainError, audit_row, pack


COLOURS = {'primary': 'Основной цвет', 'light': 'Белый', 'dark': 'Чёрный'}
CORNERS = {'top_left': '↖ Слева сверху', 'top_right': '↗ Справа сверху',
           'bottom_left': '↙ Слева снизу', 'bottom_right': '↘ Справа снизу'}


class LogoRequest(LogoPlacement):
    version: int = Field(ge=1)
    width: float = Field(default=0.35, ge=0.32, le=0.38)


def _state(service, connection, cid, pid, version, actor):
    company = service._company_row(connection, cid)
    if actor is None:
        raise DomainError('Нет доступа.', 403)
    service.require_actor(connection, cid, actor, EDITOR_ROLES | APPROVER_ROLES)
    member = connection.execute('SELECT role FROM memberships WHERE company_id=? AND user_id=?',
                                (cid, actor['id'] if isinstance(actor, dict) else actor)).fetchone()
    row = service._post_row(connection, cid, pid)
    service._editable(row, version)
    if member['role'] == 'approver' and row['status'] not in {'review', 'approved'}:
        raise DomainError('Изменение логотипа доступно согласующему на проверке.', 403)
    if not row['photo_base_id']:
        raise DomainError('Для этой фотографии не сохранён вариант без логотипа. Подготовьте изображение заново.', 422)
    return company, row


def logo_options(service, cid, pid, version, *, actor):
    with service.db.read() as connection:
        _, row = _state(service, connection, cid, pid, version, actor)
        try:
            config = load_brand_config(service.db.path.parent / 'brands', cid)
        except BrandImageError as error:
            raise DomainError(str(error), 422) from None
        variants = [key for key in COLOURS if config.get(f'{key}_logo_path')
                    and Path(config[f'{key}_logo_path']).is_file()]
        return {'variants': variants, 'placement': json.loads(row['logo_placement']) if row['logo_placement'] else None}


def apply_logo(service, cid, pid, version, placement, *, actor):
    # No API credentials or quota needed. Recheck version/role when saving the image.
    placement = LogoPlacement.model_validate(placement)
    with service.db.read() as connection:
        company, row = _state(service, connection, cid, pid, version, actor)
        base = service._source_row(connection, cid, row['photo_base_id'])
        photo_base_id = base['id']
        company = {**dict(company), 'brand_root': service.db.path.parent / 'brands'}
        try:
            content = apply_brand(Path(base['file_path']).read_bytes(), prepare_brand(company), placement)
        except (BrandImageError, OSError) as error:
            message = str(error) if isinstance(error, BrandImageError) else 'Не удалось открыть фотографию без логотипа.'
            raise DomainError(message, 422) from None
    folder = service.db.uploads_dir / str(cid)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / f'{secrets.token_hex(20)}.png'
    try:
        with service.db.transaction() as connection:
            _, row = _state(service, connection, cid, pid, version, actor)
            if row['photo_base_id'] != photo_base_id:
                raise DomainError('Исходное изображение изменилось. Откройте публикацию заново.', 409)
            with path.open('xb') as stream:
                stream.write(content)
            photo_id = service._insert_source(connection, cid, f'Изображение поста №{pid}', '', 'image', str(path), 'image/png')
            status = 'review' if row['status'] in {'review', 'approved'} else 'needs_info' if json.loads(row['questions']) else 'draft'
            audit_row(connection, cid, 'post.logo_changed', f'Изменён логотип поста №{pid}.', actor)
            return service._update_post(connection, cid, pid, photo_id=photo_id,
                                        logo_placement=pack(placement.model_dump()), status=status,
                                        approved_snapshot=None, feedback=None, publication_uncertain=0)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def logo_keyboard(post, variants, variant=None, corner=None):
    pid, version = post['id'], post['version']
    if variant not in variants:
        variant = None
    if corner not in CORNERS:
        corner = None
    def pick(colour, position):
        return f'logopick:{pid}:{version}:{colour or "-"}:{position or "-"}'
    rows = [[{'text': ('✓ ' if colour == variant else '') + COLOURS[colour],
              'callback_data': pick(colour, corner)} for colour in variants]]
    positions = list(CORNERS)
    for group in (positions[:2], positions[2:]):
        rows.append([{'text': ('✓ ' if position == corner else '') + CORNERS[position],
                      'callback_data': pick(variant, position)} for position in group])
    if variant and corner:
        rows.append([{'text': 'Применить', 'callback_data': f'logoapply:{pid}:{version}:{variant}:{corner}'}])
    return {'inline_keyboard': [row for row in rows if row]}


def post_buttons(post, role=None):
    pid, version = post['id'], post['version']
    rows = []
    if not post.get('logo_pending'):
        if post.get('status') == 'review' and role != 'editor':
            rows.append([{'text': 'Одобрить', 'callback_data': f'approve:{pid}:{version}'},
                         {'text': 'Отклонить', 'callback_data': f'reject:{pid}:{version}'}])
        elif post.get('status') not in {'review', 'approved', 'published', 'publishing'} and role != 'approver':
            rows.append([{'text': 'На согласование', 'callback_data': f'submit:{pid}:{version}'}])
    if post.get('status') not in {'published', 'publishing'} and not post.get('publication_uncertain'):
        edit_button = {'text': 'Изменить', 'callback_data': f'edit:{pid}:{version}'}
        if rows:
            rows[0].append(edit_button)
        else:
            rows.append([edit_button])
    if post.get('logo_editable') and post.get('status') not in {'published', 'publishing'}:
        rows.append([{'text': 'Добавить логотип' if post.get('logo_pending') else 'Изменить логотип',
                      'callback_data': f'logo:{pid}:{version}'}])
    return rows


def register_logo_routes(api, service, member):
    @api.get('/companies/{cid}/posts/{post_id}/logo')
    def get_logo(cid: int, post_id: int, user=Depends(member)):
        post = service.post(cid, post_id)
        return logo_options(service, cid, post_id, post['version'], actor=user)

    @api.post('/companies/{cid}/posts/{post_id}/logo')
    def post_logo(cid: int, post_id: int, payload: LogoRequest, user=Depends(member)):
        return apply_logo(service, cid, post_id, payload.version,
                          payload.model_dump(exclude={'version'}), actor=user)
