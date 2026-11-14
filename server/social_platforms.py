"""Tenant settings and API adapters for VK community walls and MAX."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import ssl
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends
import httpx
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from .integrations import IntegrationError
from .network import http_options


SocialPlatform = Literal["vk", "max"]
_NAMES = {"vk": "ВКонтакте", "max": "MAX"}
_VK_API = "https://api.vk.com/method"
_VK_VERSION = "5.199"
_MAX_API = "https://platform-api2.max.ru"
_TIMEOUT = httpx.Timeout(120, connect=15)
_MAX_ID = re.compile(r"(?:mid\.)?[A-Za-z0-9_-]{1,250}")
_VK_POST_ID = re.compile(r"(-[1-9][0-9]{0,18})_([1-9][0-9]{0,18})")
_DESTINATION_ID = re.compile(r"-?[1-9][0-9]{0,18}")
_TOKEN = re.compile(r"[!-~]{1,2048}")
_UPLOAD_HOSTS = {
    "vk": ("vk.com", "vk.ru", "vkuserphoto.ru", "vkuser.net", "vkuseraudio.net", "vk-cdn.net", "vkcdn.ru",
           "userapi.com", "okcdn.ru", "mycdn.me", "vkvideo.ru"),
    "max": ("oneme.ru", "okcdn.ru"),
}
_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/tiff": ".tiff", "image/bmp": ".bmp", "image/heic": ".heic",
}
_VIDEO_EXTENSIONS = {
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/x-matroska": ".mkv",
}
_VK_IMAGES = {"image/jpeg", "image/png", "image/gif"}
_MB = 1_000_000


class SocialPublicationError(IntegrationError):
    def __init__(self, message: str, *, status_code: int | None = None, uncertain: bool = False):
        super().__init__(message, status_code=status_code)
        self.uncertain = uncertain


class SocialCapabilityError(SocialPublicationError):
    """The target does not support the requested operation."""


class _MaxAttachmentNotReady(SocialPublicationError):
    pass


class _SocialPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, hide_input_in_errors=True)

    destination_id: Annotated[str, StringConstraints(max_length=20)] | None = None
    label: Annotated[str, StringConstraints(max_length=150)] | None = None
    token: SecretStr | None = Field(default=None, max_length=2048)
    enabled: bool | None = None


def initialize_social(service):
    with service.db.transaction() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS social_connections (
            company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            platform TEXT NOT NULL CHECK(platform IN ('vk', 'max')),
            destination_id TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            token BLOB,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
            PRIMARY KEY(company_id, platform)
        )""")


def _connection_row(connection, company_id, platform):
    # The caller also checks membership when this is accessed on behalf of a user.
    return connection.execute(
        "SELECT s.* FROM social_connections s JOIN companies c ON c.id=s.company_id "
        "WHERE s.company_id=? AND s.platform=? AND c.archived_at IS NULL",
        (company_id, platform),
    ).fetchone()


def get_social_connection(connection, cid, platform, *, include_secret=False):
    """Read the active company's connection. Only trusted integration code requests its token."""
    row = _connection_row(connection, cid, platform)
    token = row["token"] if row else None
    destination = row["destination_id"] if row else ""
    result = {
        "platform": platform, "destination_id": destination, "label": row["label"] if row else "",
        "configured": bool(destination and token), "token_configured": bool(token),
        "enabled": bool(row and row["enabled"]),
    }
    if include_secret:
        from .domain import DomainError, reveal_secret

        try:
            result["token"] = reveal_secret(token)
        except (OSError, UnicodeError):
            raise DomainError("Хранилище секретов недоступно.", 503) from None
    return result


def _destination(platform, value):
    if not isinstance(value, str) or not _DESTINATION_ID.fullmatch(value):
        raise ValueError
    number = int(value)
    if not -(2**63) <= number < 2**63:
        raise ValueError
    return str(-abs(number)) if platform == "vk" else value


def register_social_routes(api, service, member, company_manager):
    from .domain import DomainError, audit_row

    @api.get("/companies/{cid}/social/{platform}")
    def social_settings(cid: int, platform: SocialPlatform, user=Depends(member)):
        with service.db.read() as connection:
            service.require_actor(connection, cid, user, {"admin", "editor", "approver", "super_admin"})
            return get_social_connection(connection, cid, platform)

    @api.patch("/companies/{cid}/social/{platform}")
    def patch_social_settings(cid: int, platform: SocialPlatform, payload: _SocialPatch,
                              user=Depends(company_manager)):
        with service.db.transaction() as connection:
            service.require_actor(connection, cid, user, {"admin", "super_admin"})
            row = _connection_row(connection, cid, platform)
            destination = row["destination_id"] if row else ""
            label = row["label"] if row else ""
            token = row["token"] if row else None
            enabled = bool(row and row["enabled"])
            if "destination_id" in payload.model_fields_set:
                try:
                    destination = _destination(platform, payload.destination_id) if payload.destination_id else ""
                except ValueError:
                    raise DomainError("Укажите числовой идентификатор сообщества или канала.", 422) from None
            if "label" in payload.model_fields_set:
                label = payload.label or ""
            if "token" in payload.model_fields_set:
                if payload.token is None:
                    token = None
                elif value := payload.token.get_secret_value().strip():
                    if not _TOKEN.fullmatch(value):
                        raise DomainError("Проверьте формат токена подключения.", 422)
                    token = service._store_secret(value)
            if payload.enabled is True and not (destination and token):
                raise DomainError("Для включения подключения укажите идентификатор и токен.", 422)
            if payload.enabled is not None:
                enabled = payload.enabled
            if not (destination and token):
                enabled = False
            connection.execute(
                "INSERT INTO social_connections(company_id,platform,destination_id,label,token,enabled) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(company_id,platform) DO UPDATE SET "
                "destination_id=excluded.destination_id,label=excluded.label,token=excluded.token,enabled=excluded.enabled",
                (cid, platform, destination, label, token, int(enabled)),
            )
            audit_row(connection, cid, "social.updated", f"Обновлено подключение {_NAMES[platform]}.", user)
            return get_social_connection(connection, cid, platform)


def _https_url(value):
    if not isinstance(value, str) or not value or any(ord(ch) < 33 or ord(ch) == 127 for ch in value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme == "https" and url.hostname and url.port in (None, 443) and not (
            url.username is not None or url.password is not None or url.fragment or "\\" in value
        ):
            return url
    except ValueError:
        pass
    return None


def _upload_url(platform, value):
    url = _https_url(value)
    if url and any(url.hostname == host or url.hostname.endswith(f".{host}") for host in _UPLOAD_HOSTS[platform]):
        return value
    raise SocialPublicationError(f"{_NAMES[platform]} вернул недопустимый адрес загрузки.")


_SOCIAL_REQUEST = ContextVar("social_request", default=False)


class _SocialRequestLogFilter(logging.Filter):
    def filter(self, record):
        # Upload URLs and wire headers may contain credentials.
        return not _SOCIAL_REQUEST.get()


for _logger_name in ("httpx", "httpcore.http11", "httpcore.http2", "httpcore.connection", "httpcore.proxy"):
    logging.getLogger(_logger_name).addFilter(_SocialRequestLogFilter())


def _client():
    return httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False, **http_options())


def _token_and_destination(platform, connection, *, publishing=False):
    if platform not in _NAMES:
        raise SocialPublicationError("Площадка публикации не поддерживается.")
    name = _NAMES[platform]
    if publishing and connection.get("enabled") is False:
        raise SocialPublicationError(f"Подключение {name} выключено.")
    token = connection.get("token")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise SocialPublicationError(f"Укажите токен подключения {name}.")
    try:
        destination = _destination(platform, connection.get("destination_id"))
    except ValueError:
        raise SocialPublicationError(f"Укажите идентификатор назначения {name}.") from None
    return token, destination


def _bad_response(platform, *, sending=False):
    return SocialPublicationError(f"{_NAMES[platform]} не подтвердил результат запроса.", uncertain=sending)


def _remote_error(platform, status, *, uncertain=False):
    name = _NAMES[platform]
    message = {
        400: f"{name} отклонил запрос. Проверьте содержимое и настройки подключения.",
        401: f"{name} отклонил токен подключения.",
        403: f"Проверьте права доступа подключения {name}.",
        404: f"{name} не нашёл указанный ресурс.",
        429: f"{name} ограничил частоту запросов.",
    }.get(status, f"{name} не подтвердил результат запроса.")
    return SocialPublicationError(message, status_code=status, uncertain=uncertain)


def _tls_verification_failed(error):
    while error is not None:
        if isinstance(error, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(error):
            return True
        error = error.__cause__
    return False


async def _request(client, platform, method, url, *, sending=False, **kwargs):
    log_scope = _SOCIAL_REQUEST.set(True)
    try:
        return await client.request(method, url, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
        if platform == "max" and _tls_verification_failed(error):
            raise SocialPublicationError("Не удалось проверить TLS-сертификат MAX. Проверьте доверие к сертификату Минцифры.") from None
        raise SocialPublicationError(f"Не удалось установить соединение с {_NAMES[platform]}.") from None
    except (httpx.HTTPError, httpx.InvalidURL, OSError, UnicodeError):
        raise _bad_response(platform, sending=sending) from None
    finally:
        _SOCIAL_REQUEST.reset(log_scope)


def _json_object(response, platform, *, sending=False):
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        raise _bad_response(platform, sending=sending) from None
    if not isinstance(data, dict):
        raise _bad_response(platform, sending=sending)
    return data


def _vk_api_status(code):
    if code in (5, 27, 28, 1117):
        return 401
    if code in (7, 15, 18, 20, 203, 212, 213, 214, 703, 704):
        return 403
    if code in (6, 9, 29):
        return 429
    if code in (1, 10):
        return 502
    return 400


async def _vk_call(client, token, method, data, *, sending=False):
    response = await _request(client, "vk", "POST", f"{_VK_API}/{method}", sending=sending,
                              data={**data, "v": _VK_VERSION, "access_token": token})
    if not response.is_success:
        raise _remote_error("vk", response.status_code, uncertain=sending and _ambiguous_status(response.status_code))
    result = _json_object(response, "vk", sending=sending)
    error = result.get("error")
    if isinstance(error, dict) and type(error.get("error_code")) is int:
        status = _vk_api_status(error["error_code"])
        raise _remote_error("vk", status, uncertain=sending and status >= 500)
    if "response" not in result or error is not None:
        raise _bad_response("vk", sending=sending)
    return result["response"]


def _ambiguous_status(status):
    return not 400 <= status < 500 or status in (408, 499)


async def _max_call(client, token, method, path, *, sending=False, **kwargs):
    response = await _request(client, "max", method, f"{_MAX_API}{path}", sending=sending,
                              headers={"Authorization": token}, **kwargs)
    if not response.is_success and _ambiguous_status(response.status_code):
        raise _remote_error("max", response.status_code, uncertain=sending)
    if response.status_code == 400 or response.is_success:
        result = _json_object(response, "max", sending=sending and response.is_success)
        if result.get("code") == "attachment.not.ready":
            raise _MaxAttachmentNotReady("MAX ещё обрабатывает вложение.", status_code=400)
        if result.get("success") is False or result.get("code") is not None:
            raise _remote_error("max", 400)
        if response.is_success:
            return result
    raise _remote_error("max", response.status_code)


def validate_social_publication(platform, body, media):
    if platform not in _NAMES:
        raise SocialPublicationError("Площадка публикации не поддерживается.")
    name = _NAMES[platform]
    text_limit, media_limit = (16_384, 10) if platform == "vk" else (4000, 12)
    if len(body) > text_limit:
        raise SocialPublicationError(f"Сократите текст до {text_limit} символов для публикации в {name}.")
    if len(media) > media_limit:
        raise SocialPublicationError(f"Для {name} можно приложить не больше {media_limit} фото и видео.")
    if not body.strip() and not media:
        raise SocialPublicationError("Добавьте текст публикации.")
    for item in media:
        kind, mime = item.get("kind"), item.get("mime_type")
        if kind == "image" and mime in _IMAGE_EXTENSIONS and (platform == "max" or mime in _VK_IMAGES):
            limit = 50 * _MB if platform == "max" else 40 * 1024 * 1024
        elif kind == "video" and mime in _VIDEO_EXTENSIONS:
            limit = 250 * _MB if platform == "max" else 40 * 1024 * 1024
        else:
            raise SocialPublicationError(f"Формат вложения не поддерживается для публикации в {name}.")
        try:
            path = Path(item["file_path"])
            if not path.is_absolute() or not path.is_file() or not (size := path.stat().st_size):
                raise OSError
            if size > limit:
                raise SocialPublicationError(f"Вложение превышает ограничение размера для {name}.")
            if platform == "max" and kind == "image":
                with Image.open(path) as image:
                    if max(image.size) > 7680:
                        raise SocialPublicationError("Для MAX ширина и высота изображения должны быть не больше 7680 пикселей.")
        except (KeyError, TypeError, ValueError, OSError, Image.DecompressionBombError):
            raise SocialPublicationError("Не удалось открыть вложение публикации.") from None


def _object(value):
    return value if isinstance(value, dict) else {}


def _integer(value, *, min_value=1):
    return type(value) is int and value >= min_value


async def _upload(client, platform, url, field, item, *, headers=None):
    url = _upload_url(platform, url)
    mime = item["mime_type"]
    extension = (_IMAGE_EXTENSIONS if item["kind"] == "image" else _VIDEO_EXTENSIONS)[mime]
    try:
        with Path(item["file_path"]).open("rb") as stream:
            response = await _request(client, platform, "POST", url, headers=headers,
                                      files={field: (f"attachment{extension}", stream, mime)})
    except OSError:
        raise SocialPublicationError("Не удалось открыть вложение публикации.") from None
    if not response.is_success:
        raise _remote_error(platform, response.status_code)
    return response


async def _upload_vk(client, token, group_id, item):
    if item["kind"] == "image":
        server = await _vk_call(client, token, "photos.getWallUploadServer", {"group_id": group_id})
        response = await _upload(client, "vk", _object(server).get("upload_url"), "photo", item)
        info = _json_object(response, "vk")
        if not (_integer(info.get("server")) and isinstance(info.get("photo"), str) and info["photo"] not in {"", "[]"}
                and isinstance(info.get("hash"), str) and info["hash"]):
            raise _bad_response("vk")
        saved = await _vk_call(client, token, "photos.saveWallPhoto", {
            "group_id": group_id, "server": info["server"], "photo": info["photo"], "hash": info["hash"],
        })
        attachment = _object(saved[0]) if isinstance(saved, list) and saved else {}
        media_kind, media_id = "photo", attachment.get("id")
    else:
        attachment = _object(await _vk_call(client, token, "video.save", {
            "name": "Видео к публикации", "group_id": group_id, "wallpost": 0, "auto_publish": 0,
        }))
        media_kind, media_id = "video", attachment.get("video_id")
        if not _integer(media_id):
            raise _bad_response("vk")
        response = await _upload(client, "vk", attachment.get("upload_url"), "video_file", item)
        uploaded = _json_object(response, "vk")
        if ("error" in uploaded or uploaded.get("video_id", media_id) != media_id
                or not (_integer(uploaded.get("video_id")) or _integer(uploaded.get("size")))):
            raise _bad_response("vk")
    owner_id = attachment.get("owner_id")
    if type(owner_id) is not int or not owner_id or not _integer(media_id):
        raise _bad_response("vk")
    access_key = attachment.get("access_key")
    key_suffix = f"_{access_key}" if isinstance(access_key, str) and re.fullmatch(r"[A-Za-z0-9_-]+", access_key) else ""
    return f"{media_kind}{owner_id}_{media_id}{key_suffix}"


async def _upload_max(client, token, item):
    kind = item["kind"]
    ticket = await _max_call(client, token, "POST", "/uploads", params={"type": kind})
    url = _upload_url("max", ticket.get("url"))
    # MAX's image endpoint uses bot authorization. Never forward it to other upload hosts.
    headers = {"Authorization": token} if kind == "image" and urlsplit(url).hostname == "iu.oneme.ru" else None
    response = await _upload(client, "max", url, "data", item, headers=headers)
    if kind == "image":
        photos = _object(_json_object(response, "max").get("photos"))
        attachment_token = _object(next(iter(photos.values()))).get("token") if len(photos) == 1 else None
    else:
        if response.text.strip() != "<retval>1</retval>":
            raise _bad_response("max")
        attachment_token = ticket.get("token")
    if not isinstance(attachment_token, str) or not attachment_token:
        raise _bad_response("max")
    return {"type": kind, "payload": {"token": attachment_token}}


def _max_publication_url(value):
    url = _https_url(value)
    if url and url.hostname == "max.ru" and url.path and not url.query:
        return value
    return None


def _max_send_result(message, destination, *, publication_id=None):
    message = _object(message)
    message_id = _object(message.get("body")).get("mid")
    recipient = _object(message.get("recipient"))
    chat_id = recipient.get("chat_id")
    post_id = recipient.get("post_id")
    if not isinstance(message_id, str) or not _MAX_ID.fullmatch(message_id):
        raise _bad_response("max", sending=True)
    if (chat_id is not None and str(chat_id) != destination) or (post_id is not None and post_id != publication_id):
        raise _bad_response("max", sending=True)
    result = {"message_id": message_id}
    if publication_id is None and (url := _max_publication_url(message.get("url"))):
        result["publication_url"] = url
    return result


async def publish_social(platform, connection, body, media):
    """Publish tenant-resolved local files and return the platform's publication identity."""
    token, destination = _token_and_destination(platform, connection, publishing=True)
    validate_social_publication(platform, body, media)
    async with _client() as client:
        if platform == "vk":
            attachments = [await _upload_vk(client, token, -int(destination), item) for item in media]
            data = {"owner_id": destination, "from_group": 1}
            if body.strip():
                data["message"] = body
            if attachments:
                data["attachments"] = ",".join(attachments)
            post = _object(await _vk_call(client, token, "wall.post", data, sending=True))
            if not _integer(post.get("post_id")):
                raise _bad_response("vk", sending=True)
            message_id = f"{destination}_{post['post_id']}"
            return {"message_id": message_id, "publication_url": f"https://vk.com/wall{message_id}"}

        data = {}
        if body.strip():
            data["text"] = body
        if media:
            data["attachments"] = [await _upload_max(client, token, item) for item in media]
        # MAX's explicit attachment-processing rejection is safe to retry with the same tokens.
        for delay in (0, 1, 2, 4):
            if delay:
                await asyncio.sleep(delay)
            try:
                result = await _max_call(client, token, "POST", "/messages", sending=True,
                                         params={"chat_id": destination}, json=data)
                return _max_send_result(result.get("message"), destination)
            except _MaxAttachmentNotReady:
                if not media or delay == 4:
                    raise


def _vk_post_id(message_id, destination):
    match = _VK_POST_ID.fullmatch(message_id) if isinstance(message_id, str) else None
    if not match or match[1] != destination:
        raise SocialPublicationError("Публикация ВКонтакте не относится к подключённому сообществу.", status_code=404)
    return int(match[2])


def _max_message_id(value):
    if not isinstance(value, str) or not _MAX_ID.fullmatch(value):
        raise SocialPublicationError("Некорректный идентификатор сообщения MAX.", status_code=400)
    return value


async def _max_get_post(client, token, message_id, destination):
    post = await _max_call(client, token, "GET", f"/messages/{_max_message_id(message_id)}")
    if (_object(post.get("body")).get("mid") != message_id
            or str(_object(post.get("recipient")).get("chat_id")) != destination):
        raise SocialPublicationError("Публикация MAX не относится к подключённому каналу или чату.", status_code=404)
    return post


def _require_max_channel(post):
    if _object(post.get("recipient")).get("chat_type") != "channel":
        raise SocialCapabilityError("MAX поддерживает комментарии к постам в каналах.", status_code=422)


async def fetch_social_metrics(platform, connection, message_id):
    """Return only counters the platform supplied, including reported zero values."""
    token, destination = _token_and_destination(platform, connection)
    async with _client() as client:
        if platform == "vk":
            post_id = _vk_post_id(message_id, destination)
            result = await _vk_call(client, token, "wall.getById", {"posts": message_id})
            items = _object(result).get("items")
            if not isinstance(items, list):
                raise _bad_response("vk")
            post = _object(items[0]) if items else {}
            if post.get("owner_id") != int(destination) or post.get("id") != post_id:
                raise _remote_error("vk", 404)
            counters = {key: _object(post.get(key)).get("count") for key in ("views", "likes", "comments", "reposts")}
        else:
            post = await _max_get_post(client, token, message_id, destination)
            counters = {"views": _object(post.get("stat")).get("views")}
        return {key: value for key, value in counters.items() if _integer(value, min_value=0)}


def _comment_record(comment_id, text, author_id, timestamp, *, own_author_id, divisor=1):
    created_at = None
    if _integer(timestamp):
        try:
            created_at = datetime.fromtimestamp(timestamp / divisor, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            pass
    return {
        "id": str(comment_id), "text": text if isinstance(text, str) else "",
        "author_id": str(author_id) if type(author_id) is int else None, "created_at": created_at,
        "is_own": type(author_id) is int and author_id == own_author_id,
    }


async def list_social_comments(platform, connection, message_id, *, limit=50):
    """Read one page of the most recent comments for a publication."""
    token, destination = _token_and_destination(platform, connection)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise SocialPublicationError("Количество комментариев должно быть от 1 до 100.", status_code=400)
    async with _client() as client:
        if platform == "vk":
            post_id = _vk_post_id(message_id, destination)
            owner_id = int(destination)
            data = await _vk_call(client, token, "wall.getComments", {
                "owner_id": destination, "post_id": post_id, "count": limit, "sort": "desc", "preview_length": 0,
            })
            items = _object(data).get("items")
            if not isinstance(items, list):
                raise _bad_response("vk")
            comments = []
            for item in items[:limit]:
                comment = _object(item)
                if comment.get("owner_id", owner_id) != owner_id or comment.get("post_id", post_id) != post_id:
                    raise _bad_response("vk")
                if _integer(comment.get("id")) and not comment.get("deleted"):
                    comments.append(_comment_record(comment["id"], comment.get("text"), comment.get("from_id"),
                                                    comment.get("date"), own_author_id=owner_id))
            return comments

        post = await _max_get_post(client, token, message_id, destination)
        _require_max_channel(post)
        data = await _max_call(client, token, "GET", f"/messages/{message_id}/comments", params={"count": limit})
        items = data.get("messages")
        if not isinstance(items, list):
            raise _bad_response("max")
        if not items:
            return []
        identity = await _max_call(client, token, "GET", "/me")
        bot_id = identity.get("user_id")
        if type(bot_id) is not int:
            raise _bad_response("max")
        comments = []
        for item in items[:limit]:
            comment = _object(item)
            body = _object(comment.get("body"))
            recipient = _object(comment.get("recipient"))
            chat_id = recipient.get("chat_id")
            post_id = recipient.get("post_id")
            if (chat_id is not None and str(chat_id) != destination) or (post_id is not None and post_id != message_id):
                raise _bad_response("max")
            comment_id = _max_message_id(body.get("mid"))
            author_id = _object(comment.get("sender")).get("user_id")
            comments.append(_comment_record(comment_id, body.get("text"), author_id, comment.get("timestamp"),
                                            own_author_id=bot_id, divisor=1000))
        return comments


async def send_social_reply(platform, connection, publication_id, comment_id, text):
    """Send an explicitly approved reply once. The caller owns the approval and send claim."""
    token, destination = _token_and_destination(platform, connection, publishing=True)
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise SocialPublicationError("Введите текст ответа длиной до 4000 символов.", status_code=400)
    async with _client() as client:
        if platform == "vk":
            post_id = _vk_post_id(publication_id, destination)
            if not isinstance(comment_id, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", comment_id):
                raise SocialPublicationError("Некорректный идентификатор комментария ВКонтакте.", status_code=400)
            data = await _vk_call(client, token, "wall.createComment", {
                "owner_id": destination, "post_id": post_id, "from_group": -int(destination),
                "reply_to_comment": comment_id, "message": text,
            }, sending=True)
            reply_id = _object(data).get("comment_id")
            if not _integer(reply_id):
                raise _bad_response("vk", sending=True)
            return {"message_id": str(reply_id), "publication_url": f"https://vk.com/wall{publication_id}?reply={reply_id}"}

        post = await _max_get_post(client, token, publication_id, destination)
        _require_max_channel(post)
        result = await _max_call(client, token, "POST", f"/messages/{publication_id}/comments", json={
            "text": text, "link": {"type": "reply", "mid": _max_message_id(comment_id)},
        }, sending=True)
        return _max_send_result(result.get("message"), destination, publication_id=publication_id)
