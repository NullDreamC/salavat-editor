"""Tenant brand assets and deterministic artwork composition."""

from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, File, HTTPException, UploadFile
from PIL import Image, ImageColor, ImageDraw, ImageOps, ImageStat
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .cards import BRAND


MAX_LOGO_BYTES = 4 * 1024 * 1024
MAX_PHOTO_PIXELS = 32_000_000
_VARIANTS = ("light", "dark", "primary")
_NEUTRAL = {"light_background": "#FFFFFF", "dark_background": "#202020", "accents": [], "theme": "auto"}
_SALAVAT = {
    "light_background": "#DCE0E0", "dark_background": "#265054",
    "accents": ["#156E80", "#C0CFD2"], "theme": "auto",
}
_SALAVAT_LOGOS = {
    "light": BRAND / "ЛОГОТИП" / "БЕЛЫЙ" / "RUS" / ".PNG" / "ЛОГОТИП.png",
    "dark": BRAND / "ЛОГОТИП" / "ЧЕРНЫЙ" / "RUS" / ".PNG" / "ЛОГОТИП.png",
    "primary": BRAND / "ЛОГОТИП" / "ОСНОВНОЙ ЦВЕТ" / "RUS" / ".PNG" / "Логотип.png",
}
_COLOUR = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]
_THEME = Literal["auto", "light", "dark"]


class BrandImageError(ValueError):
    pass


class LogoPlacement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant: Literal["light", "dark", "primary"]
    corner: Literal["top_left", "top_right", "bottom_left", "bottom_right"]
    width: float = Field(ge=0.32, le=0.38)


class _BrandSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    light_background: _COLOUR
    dark_background: _COLOUR
    accents: list[_COLOUR] = Field(max_length=6)
    theme: _THEME


class _BrandPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    light_background: _COLOUR | None = None
    dark_background: _COLOUR | None = None
    accents: list[_COLOUR] | None = Field(None, max_length=6)
    theme: _THEME | None = None


def _tenant_directory(root: Path, company_id: int) -> Path:
    return root / str(int(company_id))


def load_brand_config(root: Path | None, company_id: int) -> dict:
    settings = dict(_SALAVAT if company_id == 1 else _NEUTRAL)
    logo_paths = _SALAVAT_LOGOS if company_id == 1 else {}
    if root is not None:
        folder = _tenant_directory(root, company_id)
        try:
            settings = _BrandSettings.model_validate_json((folder / "settings.json").read_bytes()).model_dump()
        except FileNotFoundError:
            pass
        except (ValueError, OSError):
            raise BrandImageError("Не удалось прочитать фирменные настройки компании.") from None
        logo_paths = {
            variant: folder / f"{variant}.png" if (folder / f"{variant}.png").is_file() else logo_paths.get(variant)
            for variant in _VARIANTS
        }
    return {**settings, **{f"{variant}_logo_path": logo_paths.get(variant) for variant in _VARIANTS}}


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _public_brand(brand: dict) -> dict:
    configured = {}
    for variant in _VARIANTS:
        path = brand.get(f"{variant}_logo_path")
        configured[f"{variant}_logo_configured"] = bool(path and Path(path).is_file())
    return {
        **_public_brand_settings(brand),
        **configured,
    }


def _load_logo(source) -> Image.Image:
    try:
        with Image.open(source, formats=["PNG"]) as image:
            if image.width * image.height > MAX_PHOTO_PIXELS or image.is_animated:
                raise ValueError
            rgba = image.convert("RGBA")
    except (OSError, ValueError, Image.DecompressionBombError):
        raise BrandImageError("Загрузите статичный PNG-логотип с прозрачным фоном.") from None
    alpha = rgba.getchannel("A")
    if alpha.getextrema() != (0, 255):
        raise BrandImageError("PNG-логотип должен иметь прозрачный фон и видимый знак.")
    rgba.info.clear()
    return rgba


def _luminance(rgb: tuple[int, int, int]) -> float:
    values = [channel / 255 for channel in rgb]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in values]
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def _logo_colour(logo: Image.Image) -> float:
    opaque = logo.getchannel("A").point(lambda value: 255 if value >= 224 else 0)
    return _luminance(ImageStat.Stat(logo.convert("RGB"), opaque).mean)


def _check_contrast(logo: Image.Image, background: str) -> None:
    high, low = sorted((_logo_colour(logo), _luminance(ImageColor.getrgb(background))), reverse=True)
    if (high + 0.05) / (low + 0.05) < 4.5:
        raise BrandImageError("Логотип сливается с фоном. Выберите контрастный цвет или загрузите другой вариант.")


@dataclass
class Brand:
    settings: _BrandSettings
    light_logo: Image.Image
    dark_logo: Image.Image
    primary_logo: Image.Image | None = None

    def logo(self, variant):
        image = getattr(self, f'{variant}_logo', None)
        if image is None:
            raise BrandImageError('Выбранный вариант логотипа не загружен.')
        return image

    @property
    def palette(self) -> list[str]:
        return [self.settings.light_background, self.settings.dark_background, *self.settings.accents]


def prepare_brand(company: dict) -> Brand:
    raw_root = company.get("brand_root")
    config = company.get("brand") or load_brand_config(
        Path(raw_root) if raw_root is not None else None, company["id"],
    )
    try:
        settings = _BrandSettings.model_validate({
            **{field: config[field] for field in _BrandSettings.model_fields if field in config},
            "theme": company.get("brand_theme") or config.get("theme", "auto"),
        })
        light_path, dark_path = (Path(config[f"{variant}_logo_path"]) for variant in ("light", "dark"))
        if not light_path.is_file() or not dark_path.is_file():
            raise FileNotFoundError
    except (KeyError, TypeError, OSError, ValueError):
        raise BrandImageError("В настройках компании укажите фирменную палитру и загрузите оба варианта логотипа.") from None
    light_logo, dark_logo = _load_logo(light_path), _load_logo(dark_path)
    _check_contrast(light_logo, settings.dark_background)
    _check_contrast(dark_logo, settings.light_background)
    primary = _load_logo(config['primary_logo_path']) if config.get('primary_logo_path') else None
    return Brand(
        settings,
        light_logo.crop(light_logo.getchannel("A").getbbox()),
        dark_logo.crop(dark_logo.getchannel("A").getbbox()),
        primary.crop(primary.getchannel('A').getbbox()) if primary is not None else None,
    )


def apply_brand(photo: bytes, brand: Brand, placement: LogoPlacement | None = None) -> bytes:
    """Anchor the original logo to both edges of the chosen corner, without a strip."""
    try:
        with Image.open(BytesIO(photo), formats=["PNG", "JPEG", "WEBP"]) as image:
            if image.width * image.height > MAX_PHOTO_PIXELS or min(image.size) < 64:
                raise ValueError
            canvas = ImageOps.exif_transpose(image).convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError):
        raise BrandImageError("Сервис вернул изображение в неожиданном формате.") from None
    width, height = canvas.size
    margin = max(4, round(width * 0.035))
    if placement is None:
        bounds_width = round(width * 0.35)
        bounds_height = round(bounds_width * brand.dark_logo.height / brand.dark_logo.width)
        region = canvas.crop((width - margin - bounds_width, margin, width - margin, min(height, margin + bounds_height)))
        background = ImageStat.Stat(region).mean
        theme = brand.settings.theme
        dark = theme == "dark" or (theme == "auto" and _luminance(background) < 0.18)
        white = min(background) >= 240 and max(background) - min(background) < 15
        variant = 'light' if dark else 'dark' if white or brand.primary_logo is None else 'primary'
        placement = LogoPlacement(variant=variant, corner='top_right', width=0.35)
    logo = brand.logo(placement.variant).copy()
    logo_width = round(width * placement.width)
    logo_height = round(logo_width * logo.height / logo.width)
    if logo_width + 2 * margin > width or logo_height + 2 * margin > height:
        raise BrandImageError('Для крупного логотипа недостаточно места. Выберите менее вытянутое изображение.')
    x = margin if placement.corner.endswith('left') else width - margin - logo_width
    y = margin if placement.corner.startswith('top') else height - margin - logo_height
    logo = logo.resize((logo_width, logo_height), Image.Resampling.LANCZOS)
    canvas.paste(logo, (x, y), logo)
    output = BytesIO()
    canvas.save(output, "PNG", optimize=True)
    return output.getvalue()


def register_brand_routes(api, service, company_manager):
    root = service.db.path.parent / "brands"

    def read_brand(cid):
        try:
            return load_brand_config(root, cid)
        except BrandImageError as error:
            raise HTTPException(422, str(error)) from None

    @api.get("/companies/{cid}/brand")
    def brand_settings(cid: int, user=Depends(company_manager)):
        return _public_brand(read_brand(cid))

    @api.patch("/companies/{cid}/brand")
    def patch_brand_settings(cid: int, payload: _BrandPatch, user=Depends(company_manager)):
        try:
            with service.db.transaction() as connection:
                service.require_actor(connection, cid, user, {"admin", "super_admin"})
                brand = read_brand(cid)
                updates = payload.model_dump(exclude_none=True)
                settings = _BrandSettings.model_validate({**_public_brand_settings(brand), **updates})
                _atomic_write(_tenant_directory(root, cid) / "settings.json", settings.model_dump_json().encode())
                return _public_brand({**brand, **settings.model_dump()})
        except OSError:
            raise HTTPException(500, "Не удалось сохранить фирменные настройки компании.") from None

    @api.post("/companies/{cid}/brand/logos/{variant}")
    def upload_brand_logo(
        cid: int, variant: Literal["light", "dark", "primary"], file: UploadFile = File(...),
        user=Depends(company_manager),
    ):
        raw = file.file.read(MAX_LOGO_BYTES + 1)
        if len(raw) > MAX_LOGO_BYTES:
            raise HTTPException(413, "Размер логотипа превышает 4 МБ.")
        try:
            logo = _load_logo(BytesIO(raw))
            with service.db.transaction() as connection:
                service.require_actor(connection, cid, user, {"admin", "super_admin"})
                brand = read_brand(cid)
                background = brand["dark_background" if variant == "light" else "light_background"]
                _check_contrast(logo, background)
                path = _tenant_directory(root, cid) / f"{variant}.png"
                # Store sanitized PNG pixels at a fixed tenant-local path.
                stream = BytesIO()
                logo.save(stream, "PNG")
                _atomic_write(path, stream.getvalue())
                return _public_brand({**brand, f"{variant}_logo_path": path})
        except BrandImageError as error:
            raise HTTPException(422, str(error)) from None
        except OSError:
            raise HTTPException(500, "Не удалось сохранить логотип компании.") from None


def _public_brand_settings(brand: dict) -> dict:
    return {field: brand[field] for field in _BrandSettings.model_fields}
