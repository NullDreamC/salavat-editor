"""Bounded collection of untrusted public observations, separate from editorial sources."""

import asyncio
import base64
import binascii
import ipaddress
import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .db import reveal_secret, store_secret
from .domain import EDITOR_ROLES, DomainError, audit_row, normalize_text
from .knowledge import _import_url, _public_ip, _read_public, _read_public_body
from .network import http_options


MEMBER_ROLES = EDITOR_ROLES | {"approver"}
SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
MAX_COMPETITORS = 10
MAX_COMPETITOR_RECORDS = 50
MAX_PUBLICATIONS = 30
MAX_CANDIDATES = 10
MAX_SEARCHES_PER_DAY = 3
MAX_NEWS_LINKS = 3
LEASE = timedelta(minutes=5)
BACKOFF = timedelta(hours=1)
PAGE_TYPES = {"text/html", "application/xhtml+xml", "application/rss+xml", "application/atom+xml",
              "application/xml", "text/xml"}
TELEGRAM_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}\Z")
XML_CONTROL = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.I)
NEWS_SEGMENTS = {"news", "novosti", "новости", "press", "press-center", "presscentre", "presscenter", "blog"}
VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                 "param", "source", "track", "wbr"}


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, str_strip_whitespace=True)


class SettingsPatch(Body):
    version: int = Field(ge=0)
    query: str | None = Field(default=None, max_length=400)
    folder_id: str | None = Field(default=None, max_length=50, pattern=r"^[a-z0-9]*$")
    api_key: SecretStr | None = Field(default=None, max_length=1000)
    enabled: bool | None = None

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value):
        if value is not None and any(char.isspace() or not char.isascii() or ord(char) < 33
                                     for char in value.get_secret_value()):
            raise ValueError("Проверьте формат ключа Yandex Search API.")
        return value


class Discovery(Body):
    query: str | None = Field(default=None, min_length=1, max_length=400)


class CompetitorCreate(Body):
    name: str = Field(min_length=1, max_length=150)
    url: str = Field(min_length=1, max_length=2000)
    feed_url: str | None = Field(default=None, min_length=1, max_length=2000)
    candidate_id: int | None = Field(default=None, gt=0)


class CompetitorPatch(Body):
    version: int = Field(gt=0)
    name: str | None = Field(default=None, min_length=1, max_length=150)
    active: bool | None = None
    feed_url: str | None = Field(default=None, min_length=1, max_length=2000)


SCHEMA = """
CREATE TABLE IF NOT EXISTS research_settings (
    company_id INTEGER PRIMARY KEY REFERENCES companies(id),
    version INTEGER NOT NULL,
    query TEXT NOT NULL,
    folder_id TEXT NOT NULL,
    api_key BLOB,
    enabled INTEGER NOT NULL DEFAULT 0,
    actor_id INTEGER REFERENCES users(id),
    last_discovery_at TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS research_searches (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    local_day TEXT NOT NULL,
    automatic INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS research_searches_day ON research_searches(company_id,local_day);
CREATE UNIQUE INDEX IF NOT EXISTS research_searches_daily ON research_searches(company_id,local_day)
    WHERE automatic=1;
CREATE TABLE IF NOT EXISTS research_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    snippet TEXT NOT NULL,
    found_at TEXT NOT NULL,
    UNIQUE(company_id,url)
);
CREATE TABLE IF NOT EXISTS research_competitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    identity TEXT NOT NULL,
    feed_url TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    retry_at TEXT,
    claim TEXT,
    claim_until TEXT,
    UNIQUE(company_id,identity)
);
CREATE TABLE IF NOT EXISTS research_publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER NOT NULL REFERENCES research_competitors(id),
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    platform TEXT NOT NULL,
    format TEXT NOT NULL,
    published_on TEXT,
    observed_at TEXT NOT NULL,
    views INTEGER,
    UNIQUE(competitor_id,url)
);
CREATE INDEX IF NOT EXISTS research_publications_window ON research_publications(competitor_id,published_on);
"""


def initialize_research(service):
    with service.db.transaction() as connection:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                connection.execute(statement)


def _stamp(now):
    return now.isoformat(timespec="microseconds")


def _now(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _clean(value, limit):
    return normalize_text((value or "").replace("\x00", ""))[:limit]


def _public_url(value):
    url = _import_url(value)
    host = url.raw_host.decode("ascii").rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        valid = "." in host and not host.endswith((".local", ".localhost", ".internal", ".home.arpa"))
        valid = valid and not re.fullmatch(r"[0-9.]+", host)
    else:
        valid = _public_ip(address)
    if not valid:
        raise DomainError("Укажите публичный адрес HTTPS.", 422)
    return url


def _telegram_channel(url):
    if url.host not in {"t.me", "telegram.me"}:
        return None
    segment = url.path.strip("/").removeprefix("s/").split("/")[0]
    return segment.lower() if TELEGRAM_NAME.fullmatch(segment) else None


def _host(url):
    return url.host.rstrip(".").lower().removeprefix("www.")


def _identity(url):
    channel = _telegram_channel(url)
    if channel:
        return "telegram:" + channel
    return "website:" + _host(url)


def _safe_url(value):
    try:
        return _public_url(value)
    except DomainError:
        return None


def _linked_url(url, value):
    if not value:
        return None
    try:
        return _safe_url(urljoin(str(url), value))
    except ValueError:
        return None


def _plain_markup(value, limit):
    parser = _PublicPage()
    parser.feed(value or "")
    return _clean(" ".join(parser.text), limit)


def _safe_xml(body):
    # The response length is checked by the caller. DTDs and entity declarations have no place in a feed.
    if b"\0" in body or XML_CONTROL.search(body):
        raise DomainError("Источник вернул неподдерживаемый XML.", 502)
    try:
        return ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise DomainError("Источник вернул некорректный XML.", 502) from error


def _xml_field(node, names):
    for name in names:
        value = node.findtext(f"{{*}}{name}")
        if value and value.strip():
            return value.strip()
    return ""


def _published_day(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    return parsed.date().isoformat()


def _format(mime):
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "photo"
    if mime.startswith("audio/"):
        return "audio"
    return "article"


def _publication(url, title, excerpt, platform, format_, published_on, views=None):
    return {"url": str(url), "title": _clean(title, 240), "excerpt": _clean(excerpt, 1000),
            "platform": platform, "format": format_, "published_on": published_on, "views": views}


def _feed_publications(url, body):
    root = _safe_xml(body)
    if root.tag.rsplit("}", 1)[-1] not in {"rss", "feed", "RDF"}:
        raise DomainError("Укажите RSS- или Atom-ленту с публикациями.", 422)
    items = root.findall(".//{*}item") or root.findall("./{*}entry")
    publications = []
    for entry in items[:MAX_PUBLICATIONS]:
        links = [link.get("href") or link.text for link in entry.findall("{*}link")
                 if link.get("rel", "alternate") == "alternate"]
        link = next((link.strip() for link in links if link and link.strip()), "")
        source = _linked_url(url, link)
        if source is None:
            continue
        title = _plain_markup(_xml_field(entry, ("title",)), 240)
        excerpt = _plain_markup(_xml_field(entry, ("description", "summary", "content", "encoded")), 1000)
        if not title and not excerpt:
            continue
        enclosed = entry.find("{*}enclosure")
        mime = enclosed.get("type", "") if enclosed is not None else ""
        day = _published_day(_xml_field(entry, ("pubDate", "published", "date")))
        publications.append(_publication(source, title or excerpt[:100], excerpt, "website", _format(mime), day))
    return publications


class _PublicPage(HTMLParser):
    def __init__(self, channel=None):
        super().__init__()
        self.channel = channel
        self.stack = []
        self.text = []
        self.title = []
        self.metadata = {}
        self.feed_link = None
        self.links = []
        self.message = None
        self.publications = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag not in VOID_ELEMENTS:
            if len(self.stack) >= 128:
                raise DomainError("Страница содержит слишком много вложенных элементов.", 422)
            self.stack.append((tag, frozenset(classes)))
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key in {"article:published_time", "og:title", "og:description", "description", "og:type"}:
                self.metadata.setdefault(key, attrs.get("content"))
        if attrs.get("itemprop") == "datePublished" and tag in {"time", "meta"}:
            self.metadata.setdefault("datePublished", attrs.get("datetime") or attrs.get("content"))
        elif tag == "link" and self.feed_link is None:
            if attrs.get("rel") == "alternate" and attrs.get("type") in {"application/rss+xml", "application/atom+xml"}:
                self.feed_link = attrs.get("href")
        elif tag == "a" and not self.channel and attrs.get("href") and len(self.links) < 300:
            self.links.append(attrs["href"])
        if not self.channel:
            return
        if "tgme_widget_message" in classes:
            identity = (attrs.get("data-post") or "").lower()
            if re.fullmatch(rf"{re.escape(self.channel)}/[1-9][0-9]{{0,18}}", identity):
                self.message = {"identity": identity, "depth": len(self.stack), "parts": [], "views": [],
                                "date": None, "format": "text"}
        if self.message is None:
            return
        if tag == "time" and self._within("tgme_widget_message_date"):
            self.message["date"] = _published_day(attrs.get("datetime"))
        elif "tgme_widget_message_video_player" in classes or "tgme_widget_message_roundvideo_player" in classes:
            self.message["format"] = "video"
        elif "tgme_widget_message_photo_wrap" in classes and self.message["format"] == "text":
            self.message["format"] = "photo"
        elif "tgme_widget_message_voice_player" in classes and self.message["format"] == "text":
            self.message["format"] = "audio"
        elif "tgme_widget_message_document_wrap" in classes and self.message["format"] == "text":
            self.message["format"] = "document"

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def _within(self, css):
        return any(css in classes for _, classes in self.stack)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if self.message is not None and len(self.stack) < self.message["depth"]:
            message = self.message
            self.message = None
            excerpt = _clean(" ".join(message["parts"]), 1000)
            count = "".join(message["views"]).strip().replace("\u202f", "").replace("\xa0", "")
            views = int(count) if re.fullmatch(r"[0-9]{1,12}", count) else None
            self.publications.append(_publication(
                f"https://t.me/{message['identity']}", excerpt[:100] or f"Публикация {message['identity']}",
                excerpt, "telegram", message["format"], message["date"], views,
            ))

    def handle_data(self, value):
        if any(tag in {"script", "style", "template", "noscript"} for tag, _ in self.stack):
            return
        if self.message is not None:
            if self._within("tgme_widget_message_views"):
                self.message["views"].append(value)
            elif self._within("tgme_widget_message_text"):
                self.message["parts"].append(value)
        (self.title if any(tag == "title" for tag, _ in self.stack) else self.text).append(value)


async def _extract_publications(url, response):
    media_type, body = await _read_public_body(response, PAGE_TYPES)
    if media_type.endswith("xml"):
        return url, _feed_publications(url, body), None, "feed", []
    encoding = response.charset_encoding or "utf-8"
    try:
        html = body.decode(encoding, errors="replace")
    except (LookupError, UnicodeError):
        html = body.decode("utf-8", errors="replace")
    channel = _telegram_channel(url)
    page = _PublicPage(channel)
    page.feed(html)
    page.close()
    if channel:
        if not page.publications:
            raise DomainError("В общедоступном предпросмотре Telegram не найдено публикаций.", 422)
        return url, page.publications[-MAX_PUBLICATIONS:], None, "telegram", []
    title = page.metadata.get("og:title") or " ".join(page.title)
    excerpt = page.metadata.get("og:description") or page.metadata.get("description") or ""
    published = _published_day(page.metadata.get("article:published_time") or page.metadata.get("datePublished"))
    items = [_publication(url, title, excerpt, "website", "article", published)] if title and (
        published or page.metadata.get("og:type") == "article"
    ) else []
    feed = _linked_url(url, page.feed_link)
    return url, items, feed if feed and _host(feed) == _host(url) else None, "website", _news_links(url, page.links)


def _news_links(url, links):
    articles = {}
    indexes = {}
    for href in links:
        link = _linked_url(url, href)
        if link is None or _host(link) != _host(url) or link.query or link.path.rstrip("/") == url.path.rstrip("/"):
            continue
        parts = link.path.casefold().strip("/").split("/")
        if parts[-1] in NEWS_SEGMENTS:
            indexes.setdefault(link.path.rstrip("/"), link)
        elif any(segment in NEWS_SEGMENTS for segment in parts[:-1]):
            articles.setdefault(link.path.rstrip("/"), link)
    return list(articles.values())[:MAX_NEWS_LINKS] or list(indexes.values())[:1]


async def _load_publications(url):
    return await _read_public(str(url), _extract_publications, ", ".join(sorted(PAGE_TYPES)))


async def _collect_news(links):
    pending = list(links)
    visited = set()
    publications = []
    for _ in range(MAX_NEWS_LINKS):
        next_url = next((link for link in pending if str(link) not in visited), None)
        if next_url is None:
            break
        visited.add(str(next_url))
        try:
            final, entries, _, kind, more = await _load_publications(next_url)
        except DomainError:
            continue
        if _host(final) != _host(next_url) or kind != "website":
            continue
        publications.extend(item for item in entries if item["published_on"])
        pending.extend(more)
    return publications


async def _collect(competitor):
    url = _public_url(competitor["feed_url"] or competitor["url"])
    channel = _telegram_channel(url)
    start = f"https://t.me/s/{channel}" if channel else str(url)
    final, items, feed, kind, links = await _load_publications(start)
    if (channel and _telegram_channel(final) != channel) or (not channel and _host(url) != _host(final)):
        raise DomainError("Источник перенаправляет на другой сайт или канал. Укажите его адрес явно.", 422)
    if feed:
        try:
            final_feed, entries, _, feed_kind, _ = await _load_publications(feed)
        except DomainError:
            if not items and not links:
                raise
        else:
            if _host(feed) == _host(final_feed) and feed_kind == "feed":
                return entries[:MAX_PUBLICATIONS]
    if kind == "website" and not items:
        items = await _collect_news(links)
        if not items:
            raise DomainError("На сайте не найдены лента или датированные новости. Укажите публичную RSS- или Atom-ленту.", 422)
    return items[:MAX_PUBLICATIONS]


def _parse_search_result(raw_data):
    if not isinstance(raw_data, str) or len(raw_data) > 1_000_000:
        raise DomainError("Yandex Search API вернул неподдерживаемый ответ.", 502)
    try:
        xml = _safe_xml(base64.b64decode(raw_data, validate=True))
    except (ValueError, binascii.Error) as error:
        raise DomainError("Yandex Search API вернул неподдерживаемый ответ.", 502) from error
    if xml.tag != "yandexsearch" or xml.find("response") is None:
        raise DomainError("Yandex Search API вернул неподдерживаемый ответ.", 502)
    error = xml.find("response/error")
    if error is not None:
        if error.get("code") == "15":
            return []
        raise DomainError("Yandex Search API отклонил запрос. Проверьте настройки поиска.", 502)
    candidates = {}
    for doc in xml.findall(".//doc")[:MAX_CANDIDATES]:
        value = doc.findtext("url", "").strip()
        if not value:
            continue
        url = _safe_url(value)
        if url is None:
            continue
        channel = _telegram_channel(url)
        if channel:
            url = httpx.URL("https://t.me/" + channel)
        title = _clean("".join(doc.find("title").itertext()), 240) if doc.find("title") is not None else url.host
        snippets = " ".join("".join(node.itertext()) for node in doc.findall("passages/passage"))
        candidates.setdefault(_identity(url), (str(url), title or url.host, _clean(snippets, 500)))
    return list(candidates.values())


async def _call_search(query, folder_id, api_key):
    payload = {
        "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query, "familyMode": "FAMILY_MODE_MODERATE"},
        "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": str(MAX_CANDIDATES), "docsInGroup": "1"},
        "maxPassages": "2",
        "folderId": folder_id,
        "responseFormat": "FORMAT_XML",
    }
    try:
        async with asyncio.timeout(25), httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=8), follow_redirects=False, trust_env=False, **http_options()
        ) as client:
            async with client.stream("POST", SEARCH_URL, json=payload, headers={
                "Authorization": f"Api-Key {api_key}", "Accept-Encoding": "identity",
            }) as response:
                response.raise_for_status()
                _, body = await _read_public_body(response, {"application/json"}, byte_limit=1_100_000)
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise ValueError
                return result.get("rawData")
    except (TimeoutError, httpx.TimeoutException):
        raise DomainError("Yandex Search API не ответил вовремя.", 504) from None
    except (httpx.HTTPError, OSError, ValueError, RecursionError):
        raise DomainError("Запрос к Yandex Search API не выполнен. Проверьте доступ и настройки.", 502) from None


def _settings(connection, cid):
    row = connection.execute("SELECT * FROM research_settings WHERE company_id=?", (cid,)).fetchone()
    if row is not None:
        return dict(row)
    return {"version": 0, "query": "", "folder_id": "", "api_key": None, "enabled": False, "actor_id": None,
            "last_discovery_at": None, "last_error": None}


def _configured(settings):
    return bool(settings["query"] and settings["folder_id"] and settings["api_key"])


def _patch_settings(service, cid, actor, payload):
    with service.db.transaction() as connection:
        service.require_actor(connection, cid, actor, {"admin"})
        settings = _settings(connection, cid)
        if settings["version"] != payload.version:
            raise DomainError("Настройки изменились. Обновите страницу.", 409)
        for field in ("query", "folder_id", "enabled"):
            value = getattr(payload, field)
            if field in payload.model_fields_set and value is None:
                raise DomainError("Настройки содержат пустое обязательное поле.", 422)
            if value is not None:
                settings[field] = value
        if "api_key" in payload.model_fields_set:
            key = payload.api_key.get_secret_value() if payload.api_key else ""
            try:
                settings["api_key"] = store_secret(key) if key else None
            except OSError:
                raise DomainError("Хранилище секретов недоступно для текущего пользователя.", 503) from None
        if settings["enabled"] and not _configured(settings):
            raise DomainError("Введите поисковый запрос, ID каталога и отдельный ключ Yandex Search API.", 422)
        actor_id = actor["id"] if isinstance(actor, dict) else actor
        connection.execute(
            """INSERT INTO research_settings(company_id,version,query,folder_id,api_key,enabled,actor_id)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET version=excluded.version,
               query=excluded.query,folder_id=excluded.folder_id,api_key=excluded.api_key,
               enabled=excluded.enabled,actor_id=excluded.actor_id,last_error=NULL""",
            (cid, settings["version"] + 1, settings["query"], settings["folder_id"], settings["api_key"],
             settings["enabled"], actor_id),
        )
        audit_row(connection, cid, "research.settings", "Обновлены настройки исследования конкурентов.", actor)


def _search_claim(service, cid, actor, query, automatic, now):
    from . import billing

    with service.db.transaction() as connection:
        company = service._company_row(connection, cid)
        settings = _settings(connection, cid)
        if automatic:
            if not settings["enabled"] or not settings["actor_id"]:
                return None
            actor = settings["actor_id"]
        service.require_actor(connection, cid, actor, {"admin"})
        local_day = now.astimezone(ZoneInfo(company["timezone"])).date().isoformat()
        stats = connection.execute(
            "SELECT COUNT(*) AS attempts, COALESCE(MAX(automatic),0) AS automatic FROM research_searches "
            "WHERE company_id=? AND local_day=?", (cid, local_day),
        ).fetchone()
        if automatic and (stats["automatic"] or stats["attempts"] >= MAX_SEARCHES_PER_DAY):
            return None
        if not automatic and stats["attempts"] >= MAX_SEARCHES_PER_DAY:
            raise DomainError("Достигнут дневной предел поисковых запросов.", 429)
        recent = connection.execute(
            "SELECT 1 FROM research_searches WHERE company_id=? AND completed_at IS NULL AND started_at>?",
            (cid, _stamp(now - LEASE)),
        ).fetchone()
        if recent:
            if automatic:
                return None
            raise DomainError("Поиск уже выполняется.", 409)
        text = query or settings["query"]
        if not (text and settings["folder_id"] and settings["api_key"]):
            raise DomainError("Укажите поисковый запрос, ID каталога и ключ Yandex Search API.", 422)
        try:
            api_key = reveal_secret(settings["api_key"])
        except (OSError, UnicodeError):
            raise DomainError("Ключ Yandex Search API недоступен. Сохраните его заново.", 503) from None
        if not api_key:
            raise DomainError("Ключ Yandex Search API недоступен. Сохраните его заново.", 422)
        try:
            charge = billing.reserve_search(connection, company, billing.SEARCH_MODEL, require_tariff=True)
        except DomainError as error:
            if not automatic:
                raise
            # One outcome per company-local day, including a disabled tariff or exhausted budget.
            connection.execute(
                "INSERT INTO research_searches(company_id,local_day,automatic,started_at,completed_at) VALUES(?,?,?,?,?)",
                (cid, local_day, 1, _stamp(now), _stamp(now)),
            )
            connection.execute("UPDATE research_settings SET last_error=? WHERE company_id=?", (error.detail, cid))
            return None
        row_id = connection.execute(
            "INSERT INTO research_searches(company_id,local_day,automatic,started_at) VALUES(?,?,?,?)",
            (cid, local_day, automatic, _stamp(now)),
        ).lastrowid
        return row_id, charge, actor, settings, text, api_key


def _verify_search(service, connection, cid, actor, settings):
    service.require_actor(connection, cid, actor, {"admin"})
    if _settings(connection, cid)["version"] != settings["version"]:
        raise DomainError("Настройки поиска изменились во время выполнения.", 409)


def _finish_search(service, cid, row_id, actor, settings, now, *, rows=None, error=None):
    with service.db.transaction() as connection:
        connection.execute("UPDATE research_searches SET completed_at=? WHERE company_id=? AND id=?",
                           (_stamp(now), cid, row_id))
        try:
            _verify_search(service, connection, cid, actor, settings)
        except DomainError as failure:
            return failure
        connection.execute(
            "UPDATE research_settings SET last_discovery_at=CASE WHEN ? IS NULL THEN ? ELSE last_discovery_at END, "
            "last_error=? WHERE company_id=?", (error, _stamp(now), error, cid),
        )
        if rows is not None:
            for url, title, snippet in rows:
                connection.execute(
                    """INSERT INTO research_candidates(company_id,url,title,snippet,found_at) VALUES(?,?,?,?,?)
                       ON CONFLICT(company_id,url) DO UPDATE SET title=excluded.title,snippet=excluded.snippet,
                       found_at=excluded.found_at""", (cid, url, title, snippet, _stamp(now)),
                )
            connection.execute(
                """DELETE FROM research_candidates WHERE company_id=? AND id NOT IN
                   (SELECT id FROM research_candidates WHERE company_id=? ORDER BY found_at DESC,id DESC LIMIT 100)""",
                (cid, cid),
            )
        audit_row(connection, cid, "research.search", f"Непроверенных результатов: {len(rows)}." if rows is not None
                  else "Поисковый запрос завершился ошибкой.", actor)
    return None


async def discover(service, cid, actor=None, *, query=None, automatic=False, now=None):
    from . import billing

    claim = _search_claim(service, cid, actor, query, automatic, _now(now))
    if claim is None:
        return
    row_id, charge, actor, settings, text, key = claim

    def validate(_charge, _tariff):
        with service.db.read() as connection:
            _verify_search(service, connection, cid, actor, settings)

    try:
        billing.start_charge(service.db, charge, validate=validate)
    except DomainError as error:
        billing.release_charge(service.db, charge)
        _finish_search(service, cid, row_id, actor, settings, _now(now), error=error.detail)
        raise
    try:
        raw = await _call_search(text, settings["folder_id"], key)
        billing.settle_search(service.db, charge)
        rows = _parse_search_result(raw)
    except DomainError as error:
        _finish_search(service, cid, row_id, actor, settings, _now(now), error=error.detail)
        raise
    failure = _finish_search(service, cid, row_id, actor, settings, _now(now), rows=rows)
    if failure:
        raise failure


def _competitor(connection, cid, competitor_id):
    row = connection.execute("SELECT * FROM research_competitors WHERE company_id=? AND id=?",
                             (cid, competitor_id)).fetchone()
    if row is None:
        raise DomainError("Источник конкурента не найден.", 404)
    return row


def _edit_competitor(service, cid, actor, payload, competitor_id=None):
    feed = str(_public_url(payload.feed_url)) if payload.feed_url else None
    with service.db.transaction() as connection:
        service.require_actor(connection, cid, actor, EDITOR_ROLES)
        if competitor_id is not None:
            row = _competitor(connection, cid, competitor_id)
            if row["version"] != payload.version:
                raise DomainError("Источник изменился. Обновите страницу.", 409)
            active = row["active"] if payload.active is None else payload.active
            if active and not row["active"]:
                _check_competitor_limit(connection, cid)
            connection.execute(
                """UPDATE research_competitors SET name=?,feed_url=?,active=?,version=version+1,
                   claim=NULL,claim_until=NULL WHERE company_id=? AND id=?""",
                (payload.name or row["name"], feed if "feed_url" in payload.model_fields_set else row["feed_url"],
                 active, cid, competitor_id),
            )
        else:
            url = _public_url(payload.url)
            if payload.candidate_id:
                candidate = connection.execute("SELECT url FROM research_candidates WHERE company_id=? AND id=?",
                                               (cid, payload.candidate_id)).fetchone()
                if candidate is None or candidate["url"] != str(url):
                    raise DomainError("Результат поиска недоступен или его адрес изменился.", 409)
            identity = _identity(url)
            existing = connection.execute(
                "SELECT id FROM research_competitors WHERE company_id=? AND identity=?", (cid, identity),
            ).fetchone()
            if existing:
                raise DomainError("Этот сайт или канал уже добавлен. Откройте его карточку.", 409)
            _check_competitor_limit(connection, cid)
            if connection.execute("SELECT COUNT(*) FROM research_competitors WHERE company_id=?",
                                  (cid,)).fetchone()[0] >= MAX_COMPETITOR_RECORDS:
                raise DomainError(f"Достигнут предел сохранённых источников ({MAX_COMPETITOR_RECORDS}).", 422)
            competitor_id = connection.execute(
                "INSERT INTO research_competitors(company_id,name,url,identity,feed_url) VALUES(?,?,?,?,?)",
                (cid, payload.name, str(url), identity, feed),
            ).lastrowid
        audit_row(connection, cid, "research.competitor", f"Сохранён источник конкурента №{competitor_id}.", actor)


def _check_competitor_limit(connection, cid):
    if connection.execute("SELECT COUNT(*) FROM research_competitors WHERE company_id=? AND active=1",
                          (cid,)).fetchone()[0] >= MAX_COMPETITORS:
        raise DomainError(f"Можно отслеживать до {MAX_COMPETITORS} активных источников.", 422)


def _claim_competitor(service, cid, competitor_id, actor, now, settings_version=None):
    with service.db.transaction() as connection:
        service.require_actor(connection, cid, actor, EDITOR_ROLES if settings_version is None else {"admin"})
        row = _competitor(connection, cid, competitor_id)
        if not row["active"]:
            raise DomainError("Источник отключён.", 409)
        if settings_version is not None:
            settings = _settings(connection, cid)
            if not settings["enabled"] or settings["version"] != settings_version:
                raise DomainError("Настройки сбора изменились.", 409)
        if row["claim_until"] and row["claim_until"] > _stamp(now):
            raise DomainError("Обновление источника уже выполняется.", 409)
        if row["retry_at"] and row["retry_at"] > _stamp(now):
            raise DomainError("Источник недавно проверялся. Повторите позже.", 429)
        claim = secrets.token_hex(16)
        connection.execute(
            "UPDATE research_competitors SET claim=?,claim_until=?,last_checked_at=? WHERE id=?",
            (claim, _stamp(now + LEASE), _stamp(now), competitor_id),
        )
        return row, claim


def _complete_competitor(service, cid, actor, row, claim, now, result, error, settings_version):
    with service.db.transaction() as connection:
        current = _competitor(connection, cid, row["id"])
        if current["claim"] != claim or current["version"] != row["version"]:
            return DomainError("Источник изменился во время обновления.", 409)
        try:
            service.require_actor(connection, cid, actor, EDITOR_ROLES if settings_version is None else {"admin"})
            if settings_version is not None:
                settings = _settings(connection, cid)
                if not settings["enabled"] or settings["version"] != settings_version:
                    raise DomainError("Настройки сбора изменились.", 409)
        except DomainError as failure:
            connection.execute("UPDATE research_competitors SET claim=NULL,claim_until=NULL WHERE id=?", (row["id"],))
            return failure
        connection.execute(
            """UPDATE research_competitors SET claim=NULL,claim_until=NULL,last_error=?,retry_at=?,
               last_success_at=CASE WHEN ? IS NULL THEN ? ELSE last_success_at END WHERE id=?""",
            (error, _stamp(now + (BACKOFF if error else timedelta(minutes=15))), error, _stamp(now), row["id"]),
        )
        if result is not None:
            for item in {entry["url"]: entry for entry in result}.values():
                connection.execute(
                    """INSERT INTO research_publications(competitor_id,url,title,excerpt,platform,format,published_on,
                       observed_at,views) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(competitor_id,url) DO UPDATE SET
                       title=excluded.title,excerpt=excluded.excerpt,format=excluded.format,
                       published_on=COALESCE(excluded.published_on,research_publications.published_on),
                       observed_at=excluded.observed_at,views=excluded.views""",
                    (row["id"], item["url"], item["title"], item["excerpt"], item["platform"], item["format"],
                     item["published_on"], _stamp(now), item["views"]),
                )
            connection.execute(
                """DELETE FROM research_publications WHERE competitor_id=? AND id NOT IN
                   (SELECT id FROM research_publications WHERE competitor_id=?
                    ORDER BY observed_at DESC,id DESC LIMIT 500)""", (row["id"], row["id"]),
            )
    return None


async def collect_competitor(service, cid, competitor_id, actor, *, now=None, settings_version=None):
    row, claim = _claim_competitor(service, cid, competitor_id, actor, _now(now), settings_version)
    try:
        publications = await _collect(row)
    except DomainError as error:
        _complete_competitor(service, cid, actor, row, claim, _now(now), None, error.detail, settings_version)
        raise
    failure = _complete_competitor(service, cid, actor, row, claim, _now(now), publications, None, settings_version)
    if failure:
        raise failure


async def refresh_research(service, cid, *, now=None):
    moment = _now(now)
    with service.db.read() as connection:
        settings = _settings(connection, cid)
        actor = settings["actor_id"]
        if not settings["enabled"] or not actor:
            return
        try:
            service.require_actor(connection, cid, actor, {"admin"})
        except DomainError:
            return
        zone = ZoneInfo(service._company_row(connection, cid)["timezone"])
        today = moment.astimezone(zone).date()
        midnight = datetime.combine(today, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
        search_due = connection.execute(
            """SELECT COUNT(*)<? AND COALESCE(MAX(automatic),0)=0 FROM research_searches
               WHERE company_id=? AND local_day=?""", (MAX_SEARCHES_PER_DAY, cid, today.isoformat()),
        ).fetchone()[0]
        rows = connection.execute(
            """SELECT id FROM research_competitors WHERE company_id=? AND active=1
               AND (last_checked_at IS NULL OR last_checked_at<?)
               AND (retry_at IS NULL OR retry_at<=?)
               ORDER BY last_checked_at,id LIMIT ?""", (cid, _stamp(midnight), _stamp(moment), MAX_COMPETITORS),
        ).fetchall()
    if search_due:
        try:
            await discover(service, cid, automatic=True, now=now)
        except DomainError:
            pass
    for row in rows:
        try:
            await collect_competitor(service, cid, row["id"], actor, now=now, settings_version=settings["version"])
        except DomainError:
            continue


def _comparison(competitors, rows, start, end, observed_after):
    groups = {entry["id"]: {"competitor_id": entry["id"], "observed_publications": 0, "undated_publications": 0,
                           "formats": {}, "views_total": None, "views_coverage": 0}
              for entry in competitors if entry["active"]}
    for item in rows:
        group = groups.get(item["competitor_id"])
        if group is None:
            continue
        published = item["published_on"]
        if published is None:
            if item["observed_at"] >= observed_after:
                group["undated_publications"] += 1
            continue
        if not start <= published <= end:
            continue
        group["observed_publications"] += 1
        format_ = item["format"]
        group["formats"][format_] = group["formats"].get(format_, 0) + 1
        if item["views"] is not None:
            group["views_coverage"] += 1
            group["views_total"] = (group["views_total"] or 0) + item["views"]
    return {"start": start, "end": end, "competitors": list(groups.values()), "coverage": "observed_samples"}


def research_overview(service, cid, actor, days=30, now=None):
    with service.db.read() as connection:
        service.require_actor(connection, cid, actor, MEMBER_ROLES)
        company = service._company_row(connection, cid)
        zone = ZoneInfo(company["timezone"])
        end = _now(now).astimezone(zone).date()
        start = end - timedelta(days=days - 1)
        threshold = datetime.combine(start, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
        settings = _settings(connection, cid)
        selected = connection.execute(
            """SELECT id,name,url,identity,feed_url,active,version,last_checked_at,last_success_at,last_error
               FROM research_competitors WHERE company_id=? ORDER BY active DESC,id DESC""", (cid,),
        ).fetchall()
        competitors = [dict(row) for row in selected]
        identities = {entry.pop("identity") for entry in competitors}
        candidates = [dict(row) for row in connection.execute(
            "SELECT id,url,title,snippet,found_at FROM research_candidates WHERE company_id=? "
            "ORDER BY found_at DESC,id DESC LIMIT 50", (cid,),
        )]
        for entry in candidates:
            entry["state"] = "selected" if _identity(_public_url(entry["url"])) in identities else "unverified"
        observations = [dict(row) for row in connection.execute(
            """SELECT p.* FROM research_publications p JOIN research_competitors c ON c.id=p.competitor_id
               WHERE c.company_id=? AND c.active=1 AND
               (p.published_on BETWEEN ? AND ? OR (p.published_on IS NULL AND p.observed_at>=?))
               ORDER BY p.published_on DESC,p.observed_at DESC,p.id DESC""",
            (cid, start.isoformat(), end.isoformat(), _stamp(threshold)),
        )]
    for entry in competitors:
        entry["active"] = bool(entry["active"])
    safe_settings = {key: settings[key] for key in ("version", "query", "folder_id", "last_discovery_at", "last_error")}
    safe_settings.update(configured=_configured(settings), enabled=bool(settings["enabled"]))
    return {"settings": safe_settings, "candidates": candidates, "competitors": competitors,
            "publications": observations[:100],
            "comparison": _comparison(competitors, observations, start.isoformat(), end.isoformat(), _stamp(threshold))}


def register_research_routes(api, service, member, editor):
    base = "/companies/{cid}/research"

    @api.get(base)
    def overview(cid: int, days: int = Query(30, ge=1, le=90), user=Depends(member)):
        return research_overview(service, cid, user, days)

    @api.patch(base + "/settings")
    def settings(cid: int, payload: SettingsPatch, user=Depends(editor)):
        _patch_settings(service, cid, user, payload)
        return research_overview(service, cid, user)

    @api.post(base + "/discover")
    async def search(cid: int, payload: Discovery, user=Depends(editor)):
        await discover(service, cid, user, query=payload.query)
        return research_overview(service, cid, user)

    @api.post(base + "/competitors", status_code=201)
    def create(cid: int, payload: CompetitorCreate, user=Depends(editor)):
        _edit_competitor(service, cid, user, payload)
        return research_overview(service, cid, user)

    @api.patch(base + "/competitors/{competitor_id}")
    def edit(cid: int, competitor_id: int, payload: CompetitorPatch, user=Depends(editor)):
        _edit_competitor(service, cid, user, payload, competitor_id)
        return research_overview(service, cid, user)

    @api.post(base + "/competitors/{competitor_id}/refresh")
    async def collect(cid: int, competitor_id: int, user=Depends(editor)):
        await collect_competitor(service, cid, competitor_id, user)
        return research_overview(service, cid, user)
