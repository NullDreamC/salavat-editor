"""Deterministic brand cards: exact text, no generated faces or event evidence."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / 'Основные и дополнительные визуальные элементы бренда'


def font(size, bold=False):
    name = 'avenirnextcyr-bold.ttf' if bold else 'avenirnextcyr-regular.ttf'
    candidates = list(BRAND.rglob(name))
    if candidates:
        return ImageFont.truetype(str(candidates[0]), size)
    return ImageFont.truetype('C:/Windows/Fonts/arial.ttf', size)


def clean(text):
    return str(text or '').strip()


def wrap(draw, text, face, width):
    lines, current = [], ''
    for word in text.split():
        if draw.textlength(word, font=face) > width:
            raise ValueError('Слишком длинное слово для карточки. Сократите заголовок.')
        candidate = f'{current} {word}'.strip()
        if draw.textlength(candidate, font=face) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def render_card(company: dict, post: dict, output_path: Path) -> Path:
    """Only use supplied title/body. Refuse overflow instead of silently truncating."""
    template = post.get('template') or 'announcement'
    if template not in ('announcement', 'person', 'results'):
        raise ValueError('Выберите шаблон карточки.')
    title = clean(post.get('title'))
    if not title:
        raise ValueError('Добавьте заголовок для карточки.')
    if len(title) > 190:
        raise ValueError('Сократите заголовок карточки до 190 символов.')
    dark, teal, pale = '#083849', '#008d9a', '#e9f2f4'
    base = dark if template == 'announcement' else pale
    canvas = Image.new('RGB', (1254, 1254), base)
    draw = ImageDraw.Draw(canvas)
    # Fixed geometry echoes the brand's diagonal glass surfaces.
    draw.polygon([(720, 0), (1254, 0), (1254, 870), (1170, 1050)], fill=teal)
    draw.polygon([(0, 1070), (950, 1254), (0, 1254)], fill=teal)
    foreground = 'white' if template == 'announcement' else dark
    company_name = clean(company.get('name', ''))
    if len(company_name) > 70:
        raise ValueError('Сократите название компании для карточки.')
    nameface = font(30, True)
    for index, line in enumerate(wrap(draw, company_name, nameface, 900)):
        draw.text((84, 80 + index * 38), line, font=nameface, fill=foreground)
    for size in (88, 80, 72, 64, 56):
        face = font(size, True)
        try:
            lines = wrap(draw, title, face, 970)
        except ValueError:
            continue
        if len(lines) * (size + 16) <= 660:
            break
    else:
        raise ValueError('Заголовок не помещается. Сократите его.')
    y = 320 if template != 'person' else 240
    for line in lines:
        draw.text((84, y), line, font=face, fill=foreground)
        y += size + 16
    # No extracted date: choosing a publication date as the event date would be unsafe.
    draw.rectangle((84, 1030, 230, 1040), fill='#f2cf55' if template == 'announcement' else teal)
    # Company's own logo is used only for its own tenant, never generic companies.
    if company.get('id') == 1 and 'салават' in company_name.casefold():
        color = 'БЕЛЫЙ' if template == 'announcement' else 'ОСНОВНОЙ ЦВЕТ'
        candidates = list((BRAND / 'ЛОГОТИП' / color / 'RUS' / '.PNG').glob('*'))
        logos = [p for p in candidates if p.suffix.lower() == '.png' and '_' not in p.stem]
        if logos:
            with Image.open(logos[0]) as logo:
                logo = logo.convert('RGBA')
                logo.thumbnail((310, 110))
                canvas.paste(logo, (860, 1100), logo)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, 'PNG')
    return output_path
