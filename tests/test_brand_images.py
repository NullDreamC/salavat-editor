import asyncio
import base64
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import httpx
from PIL import Image, ImageDraw
import pytest

from server import brand_images, integrations


def png(image):
    result = BytesIO()
    image.save(result, "PNG")
    return result.getvalue()


def logo(color):
    image = Image.new("RGBA", (48, 16))
    ImageDraw.Draw(image).rectangle((3, 2, 42, 12), fill=color)
    return png(image)


@pytest.fixture
def brand_root(tmp_path):
    folder = tmp_path / "7"
    folder.mkdir()
    (folder / "light.png").write_bytes(logo("white"))
    (folder / "dark.png").write_bytes(logo("black"))
    return tmp_path


@pytest.fixture
def mock_http(monkeypatch):
    actual_client = httpx.AsyncClient
    monkeypatch.setattr(integrations, "http_options", lambda: {})

    def configure(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: actual_client(transport=transport, **kw))

    return configure


def test_bundled_logo_variants_and_palette():
    brand = brand_images.prepare_brand({"id": 1})
    assert brand.palette == ["#DCE0E0", "#265054", "#156E80", "#C0CFD2"]
    assert brand.light_logo.getchannel("A").getextrema() == (0, 255)
    assert brand.dark_logo.getchannel("A").getextrema() == (0, 255)
    assert brand.primary_logo is not None


def test_primary_logo_is_optional_and_tenant_scoped(brand_root):
    assert brand_images.prepare_brand({'id': 7, 'brand_root': brand_root}).primary_logo is None
    (brand_root / '7' / 'primary.png').write_bytes(logo('#265054'))
    brand = brand_images.prepare_brand({'id': 7, 'brand_root': brand_root})
    placement = brand_images.LogoPlacement(variant='primary', corner='top_right', width=0.35)
    result = Image.open(BytesIO(brand_images.apply_brand(png(Image.new('RGB', (400, 400), 'white')), brand, placement)))
    assert result.getpixel((320, 20)) == (38, 80, 84)
    assert brand_images.load_brand_config(brand_root, 8)['primary_logo_path'] is None


@pytest.mark.parametrize('format,mime', [('JPEG', 'image/jpeg'), ('PNG', 'image/png'), ('WEBP', 'image/webp')])
def test_reference_photo_formats_are_supported(tmp_path, format, mime):
    path = tmp_path / ('photo.' + format.lower())
    Image.new('RGB', (80, 100), 'gray').save(path, format)
    content = integrations._photo_content([{'kind': 'image', 'mime_type': mime, 'file_path': str(path)}])
    assert len(content) == 1
    assert content[0]['image_url'].startswith('data:image/jpeg;base64,')


@pytest.mark.parametrize("color,mark", [("white", (0, 0, 0)), ("black", (255, 255, 255))])
def test_corner_logo_uses_contrast_without_strip_or_resizing(brand_root, color, mark):
    photo = Image.new("RGB", (400, 400), color)
    brand = brand_images.prepare_brand({"id": 7, "brand_root": brand_root})
    output = Image.open(BytesIO(brand_images.apply_brand(png(photo), brand)))
    assert output.size == photo.size
    assert output.crop((0, 60, 400, 400)).tobytes() == photo.crop((0, 60, 400, 400)).tobytes()
    assert output.crop((0, 0, 240, 400)).tobytes() == photo.crop((0, 0, 240, 400)).tobytes()
    assert output.getpixel((365, 18)) == mark


def test_company_cannot_inherit_another_company_logo(brand_root):
    with pytest.raises(brand_images.BrandImageError, match="оба варианта"):
        brand_images.prepare_brand({"id": 8, "brand_root": brand_root})


@pytest.mark.parametrize('corner', ['top_left', 'top_right', 'bottom_left', 'bottom_right'])
@pytest.mark.parametrize('size', [(400, 600), (600, 400)])
def test_logo_is_large_and_anchored_to_both_corner_edges(brand_root, corner, size):
    from PIL import ImageChops

    photo = Image.new('RGB', size, 'gray')
    brand = brand_images.prepare_brand({'id': 7, 'brand_root': brand_root})
    placement = brand_images.LogoPlacement(variant='dark', corner=corner, width=0.35)
    output = Image.open(BytesIO(brand_images.apply_brand(png(photo), brand, placement)))
    box = ImageChops.difference(photo, output).getbbox()
    margin = round(size[0] * 0.035)
    assert box[2] - box[0] == round(size[0] * 0.35)
    assert box[0] == margin if corner.endswith('left') else size[0] - box[2] == margin
    assert box[1] == margin if corner.startswith('top') else size[1] - box[3] == margin
    assert output.getpixel((box[0] + 4, box[1] + 4)) == (0, 0, 0)
    assert output.size == photo.size


def test_logo_schema_rejects_free_coordinates_and_small_size():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        brand_images.LogoPlacement(variant='dark', corner='top_right', width=0.26)
    with pytest.raises(ValidationError):
        brand_images.LogoPlacement(variant='dark', edge='right', x=0.70, y=0.20, width=0.35)


@pytest.mark.parametrize("edit", [False, True])
def test_openai_automatically_adds_logo_and_retains_clean_photo(mock_http, tmp_path, brand_root, edit):
    source_file = tmp_path / "source.png"
    source_file.write_bytes(png(Image.new("RGB", (150, 150), "gray")))
    references = [{"kind": "image", "mime_type": "image/png", "file_path": source_file}] if edit else []
    reply_image = png(Image.new("RGB", (400, 400), "black"))
    requests = []

    def handle(request):
        requests.append(request)
        if json.loads(request.content).get('text', {}).get('format', {}).get('name') == 'logo_placement':
            return httpx.Response(200, json={'status': 'completed', 'output_text': json.dumps(
                {'variant': 'light', 'corner': 'top_right', 'width': 0.35}),
                'usage': {'input_tokens': 30, 'output_tokens': 10}})
        return httpx.Response(200, json={
            "status": "completed",
            "usage": {"input_tokens": 80, "output_tokens": 50},
            "output": [{"type": "image_generation_call", "status": "completed",
                        "result": base64.b64encode(reply_image).decode()}],
        })

    mock_http(handle)
    result = asyncio.run(integrations.generate_photo(
        {"id": 7, "brand_root": brand_root, "provider": "yandex"}, references, "Сделай фото светлее.",
        {"openai_key": "sk-photo-fixture", "yandex_key": "yandex-photo-fixture"},
    ))
    assert len(requests) == 2
    request = requests[0]
    assert request.url.host == "api.openai.com"
    assert request.headers["Authorization"] == "Bearer sk-photo-fixture"
    payload = json.loads(request.content)
    assert payload["tools"][0]["action"] == ("edit" if edit else "generate")
    content = payload["input"][0]["content"]
    assert len(content) == (2 if edit else 1)
    if edit:
        assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
    assert payload["store"] is False
    assert result["input_tokens"] == 80 and result["output_tokens"] == 50
    assert result['original_bytes'] == reply_image
    assert result['layout_input_tokens'] == 30 and result['layout_output_tokens'] == 10
    assert result['logo_placement'] == {'variant': 'light', 'corner': 'top_right', 'width': 0.35}
    image = Image.open(BytesIO(result["image_bytes"]))
    assert image.size == (400, 400)
    assert image.getpixel((365, 18)) == (255, 255, 255)
    assert 'точно воспроизведи' in payload['instructions']


def test_photo_failure_keeps_known_usage_and_hides_provider_data(mock_http, brand_root):
    mock_http(lambda request: httpx.Response(200, json={
        "status": "completed",
        "usage": {"input_tokens": 65, "output_tokens": 8},
        "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "sensitive provider data"}]}],
    }))
    with pytest.raises(integrations.GenerationError) as error:
        asyncio.run(integrations.generate_photo(
            {"id": 7, "brand_root": brand_root}, [], "Нарисуй обложку.", {"openai_key": "sk-photo-fixture"},
        ))
    assert "sensitive" not in str(error.value)
    assert (error.value.input_tokens, error.value.output_tokens) == (65, 8)


def test_failed_automatic_logo_keeps_paid_clean_artwork_for_manual_controls(mock_http, brand_root):
    raw = png(Image.new('RGB', (400, 400), 'gray'))
    requests = []
    def handle(request):
        requests.append(request)
        if len(requests) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={'status': 'completed', 'usage': {'input_tokens': 80, 'output_tokens': 50},
            'output': [{'type': 'image_generation_call', 'status': 'completed', 'result': base64.b64encode(raw).decode()}]})
    mock_http(handle)
    result = asyncio.run(integrations.generate_photo({'id': 7, 'brand_root': brand_root}, [], 'Фото', {'openai_key': 'fixture'}))
    assert len(requests) == 2
    assert result['image_bytes'] == result['original_bytes'] == raw
    assert result['logo_placement'] is None and result['logo_error'] is True
    assert result['input_tokens'] == 80


def test_brand_routes_hide_paths_reject_wrong_variant_and_isolate_tenants(tmp_path):
    from contextlib import nullcontext

    service = SimpleNamespace(
        db=SimpleNamespace(path=tmp_path / "db.sqlite3", transaction=lambda: nullcontext(None)),
        require_actor=lambda *_: None,
    )
    app = FastAPI()

    def manager(cid: int):
        if cid == 9:
            raise HTTPException(403)
        return {"id": 123}

    brand_images.register_brand_routes(app, service, manager)
    client = TestClient(app)
    endpoint = "/companies/7/brand"
    assert client.get(endpoint).json()["dark_logo_configured"] is False
    assert client.patch(endpoint, json={"dark_logo_path": str(Path("outside.png"))}).status_code == 422
    update = client.patch(endpoint, json={"light_background": "#FFFF00", "theme": "light"})
    assert update.json()["light_background"] == "#FFFF00"
    wrong = client.post(endpoint + "/logos/dark", files={"file": ("logo.png", logo("white"), "image/png")})
    assert wrong.status_code == 422
    upload = client.post(endpoint + "/logos/dark", files={"file": ("../logo.png", logo("black"), "image/png")})
    assert upload.status_code == 200
    assert upload.json()["dark_logo_configured"] is True
    assert "dark_logo_path" not in upload.json()
    assert client.get("/companies/8/brand").json()["dark_logo_configured"] is False
    assert client.get("/companies/9/brand").status_code == 403
