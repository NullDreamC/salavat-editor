from PIL import Image, ImageDraw
import pytest

from server.cards import render_card


COMPANY = {"id": 2, "name": "Завод стекла"}


@pytest.mark.parametrize("template", ["announcement", "person", "results"])
def test_template_renders_cyrillic_title_at_readable_size(tmp_path, monkeypatch, template):
    title = "Итоги смены № 7_А"
    drawn = []
    draw_text = ImageDraw.ImageDraw.text

    def record_text(canvas, xy, text, *args, **kwargs):
        drawn.append((text, kwargs["font"].size))
        return draw_text(canvas, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_text)
    path = render_card(COMPANY, {"template": template, "title": title}, tmp_path / "card.png")

    assert drawn == [(COMPANY["name"], 30), (title, 88)]
    with Image.open(path) as image:
        assert image.size == (1254, 1254)
        assert image.format == "PNG"
        title_pixels = image.crop((84, 240, 720, 450)).getcolors(maxcolors=100_000)
        assert title_pixels and len(title_pixels) > 2


def test_card_retries_font_size_for_wide_word(tmp_path):
    path = render_card(COMPANY, {"title": "Ш" * 15}, tmp_path / "wide-word.png")

    assert path.is_file()


@pytest.mark.parametrize("title", ["Ш" * 60, "Длинный заголовок " * 20])
def test_card_rejects_overflow_before_saving(tmp_path, title):
    output = tmp_path / "overflow.png"

    with pytest.raises(ValueError, match="[Сс]ократите"):
        render_card(COMPANY, {"title": title}, output)

    assert not output.exists()


def test_publication_and_creation_dates_do_not_appear_on_card(tmp_path):
    post = {"title": "Встреча с сотрудниками"}
    dated_post = dict(post, created_at="2026-01-01T11:00:00Z", scheduled_at="2026-01-02T11:00:00Z")

    plain = render_card(COMPANY, post, tmp_path / "plain.png")
    dated = render_card(COMPANY, dated_post, tmp_path / "dated.png")

    assert plain.read_bytes() == dated.read_bytes()
