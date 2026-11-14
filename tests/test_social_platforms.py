import asyncio
import json
import logging
from urllib.parse import parse_qs

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest

from server import domain, social_platforms as social
from test_backend import EDITOR, SUPER, api, join_company, sign_in


VK = {"destination_id": "-123", "token": "mock-vk-secret", "enabled": True}
MAX = {"destination_id": "-456", "token": "mock-max-secret", "enabled": True}


@pytest.fixture
def remote(monkeypatch):
    def start(handler):
        monkeypatch.setattr(social, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    return start


@pytest.fixture
def media(tmp_path):
    image = tmp_path / "internal-photo.png"
    Image.new("RGB", (4, 4)).save(image)
    video = tmp_path / "internal-video.mp4"
    video.write_bytes(b"mock mp4 payload")
    return [
        {"kind": "image", "mime_type": "image/png", "file_path": str(image)},
        {"kind": "video", "mime_type": "video/mp4", "file_path": str(video)},
    ]


def test_connections_permissions_rotation_and_encryption(api, monkeypatch):
    admin, service = api
    path = "/api/companies/1/social/vk"
    other = "/api/companies/2/social/vk"
    # The production route uses the same vault as the existing integrations.
    def protect(value):
        return b"sealed:" + value[::-1].encode()

    monkeypatch.setattr(service, "_store_secret", protect)
    monkeypatch.setattr(domain, "reveal_secret", lambda value: value[7:].decode()[::-1] if value else "")
    with TestClient(admin.app) as anonymous:
        assert anonymous.get(path).status_code == 401
    join_company(admin, EDITOR, 1, "editor")
    with sign_in(admin.app, EDITOR) as editor, sign_in(admin.app, SUPER) as owner:
        assert editor.get(path).json()["configured"] is False
        assert editor.get(other).status_code == 404
        assert editor.patch(path, json={"token": "editor-secret"}).status_code == 403
        assert admin.patch(path, json={"destination_id": "https://vk.com/club123"}).status_code == 422
        assert admin.patch(path, json={"enabled": True}).status_code == 422
        changed = admin.patch(path, json={"destination_id": "123", "label": "Рабочая группа",
                                         "token": "first-secret", "enabled": True})
        assert changed.status_code == 200, changed.text
        assert changed.json() == {"platform": "vk", "destination_id": "-123", "label": "Рабочая группа",
                                  "configured": True, "token_configured": True, "enabled": True}
        assert owner.get(path).json() == editor.get(path).json() == changed.json()
        assert admin.get(other).json()["configured"] is False
        assert owner.patch(path, json={"label": "Новое название", "token": ""}).status_code == 200
        with service.db.read() as connection:
            stored = connection.execute("SELECT token FROM social_connections WHERE company_id=1 AND platform='vk'").fetchone()[0]
            assert stored != b"first-secret"
            assert social.get_social_connection(connection, 1, "vk", include_secret=True)["token"] == "first-secret"
        changed = owner.patch(path, json={"token": "second-secret"})
        assert changed.status_code == 200 and "second-secret" not in changed.text
        rejected = owner.patch(path, json={"token": ["malformed-token"]})
        assert rejected.status_code == 422 and "malformed-token" not in rejected.text
        with service.db.read() as connection:
            assert social.get_social_connection(connection, 1, "vk", include_secret=True)["token"] == "second-secret"
        cleared = owner.patch(path, json={"token": None}).json()
        assert not cleared["configured"] and not cleared["enabled"]
        assert owner.patch(other, json={"destination_id": "99", "token": "tenant-two-secret", "enabled": True}).status_code == 200
        assert owner.delete("/api/companies/2").status_code == 204
        assert owner.patch(other, json={"token": "archive-change-secret"}).status_code == 404
        with service.db.read() as connection:
            assert not social.get_social_connection(connection, 2, "vk", include_secret=True)["token"]


def test_vk_publication_uploads_photo_and_video_before_wall_post(remote, media, caplog):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.host != "api.vk.com":
            assert "authorization" not in request.headers
            assert b"internal-" not in request.content
            if request.url.path == "/upload-photo":
                assert b'name="photo"' in request.content
                return httpx.Response(200, json={"server": 201, "photo": "[photo]", "hash": "photo-upload-hash"})
            assert b'name="video_file"' in request.content
            return httpx.Response(200, json={"video_id": 99, "size": 16})

        fields = parse_qs(request.content.decode())
        assert not request.url.query and fields["access_token"] == [VK["token"]] and fields["v"] == ["5.199"]
        match request.url.path:
            case "/method/photos.getWallUploadServer":
                assert fields["group_id"] == ["123"]
                result = {"upload_url": "https://cs1.userapi.com/upload-photo?token=mock-upload-secret"}
            case "/method/photos.saveWallPhoto":
                assert fields["group_id"] == ["123"] and fields["server"] == ["201"]
                result = [{"owner_id": -123, "id": 98}]
            case "/method/video.save":
                assert fields["group_id"] == ["123"] and fields["wallpost"] == fields["auto_publish"] == ["0"]
                result = {"upload_url": "https://omub.okcdn.ru/upload-video?sig=mock-upload-secret", "owner_id": -123,
                          "video_id": 99}
            case "/method/wall.post":
                assert fields["owner_id"] == ["-123"] and fields["from_group"] == ["1"]
                assert fields["attachments"] == ["photo-123_98,video-123_99"] and fields["message"] == ["Новости"]
                result = {"post_id": 42}
            case _:
                pytest.fail(request.url.path)
        return httpx.Response(200, json={"response": result})

    remote(respond)
    with caplog.at_level(logging.INFO, logger="httpx"):
        assert asyncio.run(social.publish_social("vk", VK, "Новости", media)) == {
            "message_id": "-123_42", "publication_url": "https://vk.com/wall-123_42",
        }
    assert calls == ["/method/photos.getWallUploadServer", "/upload-photo", "/method/photos.saveWallPhoto",
                     "/method/video.save", "/upload-video", "/method/wall.post"]
    assert "mock-upload-secret" not in caplog.text


def test_max_publication_retries_only_explicit_attachment_processing_error(remote, media, monkeypatch):
    posted = []
    delays = []

    async def sleep(delay):
        delays.append(delay)

    def respond(request):
        if request.url.host == "iu.oneme.ru":
            assert request.headers["authorization"] == MAX["token"] and b'name="data"' in request.content
            return httpx.Response(200, json={"photos": {"789": {"token": "photo-token"}}})
        if request.url.host == "omub.okcdn.ru":
            assert "authorization" not in request.headers and b'name="data"' in request.content
            return httpx.Response(200, text="<retval>1</retval>")
        assert request.url.host == "platform-api2.max.ru" and request.headers["authorization"] == MAX["token"]
        if request.url.path == "/uploads":
            if request.url.params["type"] == "image":
                return httpx.Response(200, json={"url": "https://iu.oneme.ru/uploadImage?r=mock-signature"})
            return httpx.Response(200, json={"url": "https://omub.okcdn.ru/upload.do?sig=mock-signature",
                                             "token": "video-token"})
        assert request.url.path == "/messages" and request.url.params["chat_id"] == "-456"
        posted.append(json.loads(request.content))
        if len(posted) == 1:
            return httpx.Response(400, json={"code": "attachment.not.ready"})
        return httpx.Response(200, json={"message": {"body": {"mid": "mid.test"}, "url": "https://max.ru/news/test"}})

    monkeypatch.setattr(social.asyncio, "sleep", sleep)
    remote(respond)
    assert asyncio.run(social.publish_social("max", MAX, "Новости", media)) == {
        "message_id": "mid.test", "publication_url": "https://max.ru/news/test",
    }
    payload = {"text": "Новости", "attachments": [
        {"type": "image", "payload": {"token": "photo-token"}},
        {"type": "video", "payload": {"token": "video-token"}},
    ]}
    assert posted == [payload, payload] and delays == [1]


@pytest.mark.parametrize("platform,connection", [("vk", VK), ("max", MAX)])
@pytest.mark.parametrize("failure,uncertain", [("read", True), ("connect", False), ("invalid", True),
                                               ("server", True), ("permission", False)])
def test_send_error_does_not_leak_response_or_repeat_final_request(remote, platform, connection, failure, uncertain):
    sent = []

    def respond(request):
        sent.append(request)
        if failure == "read":
            raise httpx.ReadTimeout("private-body-and-token")
        if failure == "connect":
            raise httpx.ConnectTimeout("private-body-and-token")
        if failure == "invalid":
            return httpx.Response(200, text="private-body-and-token")
        if failure == "server":
            return httpx.Response(502, text="private-body-and-token")
        return httpx.Response(403, json={"message": "private-body-and-token"})

    remote(respond)
    with pytest.raises(social.SocialPublicationError) as error:
        asyncio.run(social.publish_social(platform, connection, "Новости", []))
    assert error.value.uncertain is uncertain and len(sent) == 1
    assert "private-body-and-token" not in str(error.value)


@pytest.mark.parametrize("url", ["https://127.0.0.1/upload", "https://iu.oneme.ru.bad.test/upload",
                                 "http://iu.oneme.ru/upload", "https://internal@iu.oneme.ru/upload"])
def test_unsafe_upload_destination_never_receives_file(remote, media, url):
    def respond(request):
        assert request.url.host == "platform-api2.max.ru" and request.url.path == "/uploads"
        return httpx.Response(200, json={"url": url})

    remote(respond)
    with pytest.raises(social.SocialPublicationError, match="адрес загрузки") as failure:
        asyncio.run(social.publish_social("max", MAX, "Новости", media[:1]))
    assert not failure.value.uncertain


def test_upload_redirect_does_not_forward_credentials_or_file(remote, media):
    def respond(request):
        if request.url.host == "platform-api2.max.ru":
            return httpx.Response(200, json={"url": "https://iu.oneme.ru/uploadImage?r=mock-signature"})
        assert request.url.host == "iu.oneme.ru"
        return httpx.Response(307, headers={"location": "https://example.test/private"})

    remote(respond)
    with pytest.raises(social.SocialPublicationError) as error:
        asyncio.run(social.publish_social("max", MAX, "Новости", media[:1]))
    assert error.value.status_code == 307 and not error.value.uncertain


def test_max_certificate_error_reports_tls_without_retry_or_sensitive_details(remote):
    def respond(request):
        raise httpx.ConnectError("CERTIFICATE_VERIFY_FAILED: private-network-path")

    remote(respond)
    with pytest.raises(social.SocialPublicationError, match="TLS-сертификат MAX") as error:
        asyncio.run(social.publish_social("max", MAX, "Новости", []))
    assert not error.value.uncertain and "private-network-path" not in str(error.value)


def test_media_validation(tmp_path, media):
    with pytest.raises(social.SocialPublicationError, match="4000"):
        social.validate_social_publication("max", "а" * 4001, [])
    with pytest.raises(social.SocialPublicationError, match="10"):
        social.validate_social_publication("vk", "Новости", media[:1] * 11)
    with pytest.raises(social.SocialPublicationError, match="открыть"):
        social.validate_social_publication("max", "Новости", [{**media[0], "file_path": "https://example.test/file.png"}])
    oversized = tmp_path / "wide.png"
    Image.new("RGB", (7681, 1)).save(oversized)
    with pytest.raises(social.SocialPublicationError, match="7680"):
        social.validate_social_publication("max", "Новости", [{**media[0], "file_path": str(oversized)}])


def test_vk_reads_real_counters_and_comments_and_replies_as_group(remote):
    def respond(request):
        fields = parse_qs(request.content.decode())
        match request.url.path:
            case "/method/wall.getById":
                assert fields["posts"] == ["-123_42"]
                result = {"items": [{"owner_id": -123, "id": 42, "views": {"count": 90}, "likes": {"count": 0}}]}
            case "/method/wall.getComments":
                assert fields["owner_id"] == ["-123"] and fields["post_id"] == ["42"]
                assert fields["count"] == ["20"] and fields["preview_length"] == ["0"]
                result = {"items": [{"id": 5, "text": "Вопрос", "from_id": 45, "date": 1_700_000_000},
                                    {"id": 6, "text": "Ответ", "from_id": -123}]}
            case "/method/wall.createComment":
                assert fields["from_group"] == ["123"] and fields["reply_to_comment"] == ["5"]
                assert fields["message"] == ["Ответ"]
                result = {"comment_id": 6}
            case _:
                pytest.fail(request.url.path)
        return httpx.Response(200, json={"response": result})

    remote(respond)
    assert asyncio.run(social.fetch_social_metrics("vk", VK, "-123_42")) == {"views": 90, "likes": 0}
    assert asyncio.run(social.list_social_comments("vk", VK, "-123_42", limit=20)) == [
        {"id": "5", "text": "Вопрос", "author_id": "45", "created_at": "2023-11-14T22:13:20+00:00", "is_own": False},
        {"id": "6", "text": "Ответ", "author_id": "-123", "created_at": None, "is_own": True},
    ]
    assert asyncio.run(social.send_social_reply("vk", VK, "-123_42", "5", "Ответ")) == {
        "message_id": "6", "publication_url": "https://vk.com/wall-123_42?reply=6",
    }
    with pytest.raises(social.SocialPublicationError, match="не относится"):
        asyncio.run(social.fetch_social_metrics("vk", VK, "-124_42"))


def test_max_reads_views_comments_and_replies_only_for_connected_channel(remote):
    def respond(request):
        if request.url.path == "/me":
            assert request.method == "GET" and request.headers["authorization"] == MAX["token"]
            return httpx.Response(200, json={"user_id": 123, "is_bot": True})
        if request.url.path == "/messages/mid.post/comments":
            if request.method == "POST":
                assert json.loads(request.content) == {"text": "Ответ", "link": {"type": "reply", "mid": "mid.comment"}}
                return httpx.Response(200, json={"message": {"body": {"mid": "mid.reply"}}})
            assert request.method == "GET" and request.url.params["count"] == "50"
            return httpx.Response(200, json={"messages": [
                {"recipient": {"chat_id": -456, "post_id": "mid.post"},
                 "body": {"mid": "mid.comment", "text": "Вопрос"}, "timestamp": 1_700_000_000_000},
                {"sender": {"user_id": 123, "is_bot": True}, "body": {"mid": "mid.reply", "text": "Ответ"}},
                {"sender": {"user_id": 124, "is_bot": True}, "body": {"mid": "mid.bystander", "text": "Вопрос другого бота"}},
            ]})
        message_id = request.url.path.rsplit("/", 1)[-1]
        chat_id = -457 if message_id == "mid.foreign" else -456
        chat_type = "chat" if message_id == "mid.group" else "channel"
        message = {"recipient": {"chat_id": chat_id, "chat_type": chat_type}, "body": {"mid": message_id}}
        if chat_type == "channel":
            message["stat"] = {"views": 50}
        return httpx.Response(200, json=message)

    remote(respond)
    assert asyncio.run(social.fetch_social_metrics("max", MAX, "mid.post")) == {"views": 50}
    assert asyncio.run(social.fetch_social_metrics("max", MAX, "mid.group")) == {}
    assert asyncio.run(social.list_social_comments("max", MAX, "mid.post")) == [
        {"id": "mid.comment", "text": "Вопрос", "author_id": None, "created_at": "2023-11-14T22:13:20+00:00", "is_own": False},
        {"id": "mid.reply", "text": "Ответ", "author_id": "123", "created_at": None, "is_own": True},
        {"id": "mid.bystander", "text": "Вопрос другого бота", "author_id": "124", "created_at": None, "is_own": False},
    ]
    assert asyncio.run(social.send_social_reply("max", MAX, "mid.post", "mid.comment", "Ответ")) == {
        "message_id": "mid.reply",
    }
    with pytest.raises(social.SocialCapabilityError):
        asyncio.run(social.list_social_comments("max", MAX, "mid.group"))
    with pytest.raises(social.SocialPublicationError, match="не относится"):
        asyncio.run(social.send_social_reply("max", MAX, "mid.foreign", "mid.comment", "Ответ"))
