import asyncio
import ipaddress
import socket
from html.parser import HTMLParser
from typing import Annotated, Literal
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .domain import EDITOR_ROLES, MAX_SOURCE_TEXT, DomainError, audit_row, utc_now


Category = Literal["business", "style", "faq", "decision"]
Status = Literal["draft", "active"]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
KnowledgeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SOURCE_TEXT)]
MAX_URL_LENGTH = 2048
MAX_IMPORT_BYTES = 1024 * 1024
MAX_REDIRECTS = 3
IMPORT_TIMEOUT = 12
MAX_CONTEXT_SOURCES = 48
REDIRECT_CODES = {301, 302, 303, 307, 308}
IMPORT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
PUBLIC_IPV6 = ipaddress.IPv6Network("2000::/3")
AZURE_PLATFORM_IP = ipaddress.IPv4Address("168.63.129.16")


class KnowledgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class KnowledgeCreate(KnowledgeInput):
    title: Title
    text: KnowledgeText | None = None
    category: Category
    status: Status = "draft"
    source_id: int | None = Field(default=None, gt=0, strict=True)


class KnowledgePatch(KnowledgeInput):
    title: Title | None = None
    text: KnowledgeText | None = None
    category: Category | None = None
    status: Status | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self):
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Поля записи базы знаний не могут принимать null.")
        return self


class WebsiteImport(KnowledgeInput):
    url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_URL_LENGTH)]


def initialize_knowledge(service):
    with service.db.transaction() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS knowledge_entries ("
            "id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), "
            "source_id INTEGER NOT NULL REFERENCES sources(id), "
            "category TEXT NOT NULL CHECK(category IN ('business','style','faq','decision')), "
            "status TEXT NOT NULL CHECK(status IN ('draft','active','archived')), "
            "origin_url TEXT, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS knowledge_company_status ON "
            "knowledge_entries(company_id,status,updated_at DESC,id DESC)"
        )


ENTRY_SELECT = (
    "SELECT k.id,s.title,s.text,k.category,k.status,k.source_id,k.origin_url,k.updated_at "
    "FROM knowledge_entries k JOIN sources s ON s.id=k.source_id AND s.company_id=k.company_id "
)


def _entry(connection, company_id, entry_id):
    row = connection.execute(ENTRY_SELECT + "WHERE k.company_id=? AND k.id=?", (company_id, entry_id)).fetchone()
    if row is None:
        raise DomainError("Запись базы знаний не найдена.", 404)
    return dict(row)


def knowledge_source_rows(connection, cid, max_chars=24000):
    if max_chars <= 0 or not connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='knowledge_entries'"
    ).fetchone():
        return []
    rows = connection.execute(
        "SELECT s.id,s.title,substr(s.text,1,?) AS text,s.kind,s.created_at,s.mime_type,k.category,k.origin_url "
        "FROM knowledge_entries k JOIN sources s ON s.id=k.source_id AND s.company_id=k.company_id "
        "JOIN companies c ON c.id=k.company_id AND c.archived_at IS NULL "
        "WHERE k.company_id=? AND k.status='active' "
        "ORDER BY k.updated_at DESC,k.id DESC LIMIT ?",
        (max_chars, cid, MAX_CONTEXT_SOURCES),
    )
    result = []
    for row in rows:
        source = dict(row)
        source["text"] = source["text"][:max_chars]
        max_chars -= len(source["text"])
        result.append(source)
        if not max_chars:
            break
    return result


def _snapshot(service, connection, company_id, title, text):
    if not text or not text.strip() or "\x00" in text:
        raise DomainError("Добавьте текст или выберите материал с извлечённым текстом.", 422)
    return service._insert_source(connection, company_id, title, text, "knowledge")


def _create_entry(service, company_id, payload, actor, *, origin_url=None):
    with service.db.transaction() as connection:
        service.require_actor(connection, company_id, actor, EDITOR_ROLES)
        text = payload.text
        if payload.source_id is not None:
            source = service._source_row(connection, company_id, payload.source_id)
            if source["kind"] == "knowledge":
                raise DomainError("Выберите источник из раздела «Материалы».", 422)
            text = text if text is not None else source["text"]
        source_id = _snapshot(service, connection, company_id, payload.title, text)
        entry_id = connection.execute(
            "INSERT INTO knowledge_entries(company_id,source_id,category,status,origin_url,updated_at) VALUES(?,?,?,?,?,?)",
            (company_id, source_id, payload.category, payload.status, origin_url, utc_now()),
        ).lastrowid
        audit_row(connection, company_id, "knowledge.created", f"Добавлена запись базы знаний №{entry_id}.", actor)
        return _entry(connection, company_id, entry_id)


def _import_url(value):
    invalid = DomainError("Введите публичный адрес страницы с HTTPS.", 422)
    if len(value) > MAX_URL_LENGTH or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise invalid
    try:
        parsed = urlsplit(value)
        url = httpx.URL(value)
        if (url.scheme != "https" or not url.raw_host or url.port is not None or "@" in parsed.netloc
                or b"%" in url.raw_host):
            raise invalid
    except (ValueError, httpx.InvalidURL) as error:
        raise invalid from error
    return url.copy_with(fragment=None)


def _public_ip(address):
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if address.version == 6:
        return address in PUBLIC_IPV6 and address.sixtofour is None and address.teredo is None
    return address != AZURE_PLATFORM_IP


async def _resolve_public_ip(host):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        hostname = host.rstrip(".")
        if "." not in hostname or hostname.endswith((".local", ".localhost", ".internal", ".home.arpa")):
            raise DomainError("Разрешены только публичные адреса сайтов.", 422)
        answers = await asyncio.get_running_loop().getaddrinfo(hostname + ".", 443, type=socket.SOCK_STREAM)
        addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
    else:
        addresses = [address]
    if not addresses or any(not _public_ip(address) for address in addresses):
        raise DomainError("Разрешены только публичные адреса сайтов.", 422)
    return str(min(addresses, key=lambda address: address.version))


class _PageText(HTMLParser):
    blocks = {"article", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "section", "tr"}

    def __init__(self):
        super().__init__()
        self.title = []
        self.text = []
        self.hidden = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template", "noscript"}:
            self.hidden.append(tag)
        elif not self.hidden:
            if tag == "title":
                self.in_title = True
            elif tag in self.blocks:
                self.text.append("\n")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag == "title":
            self.in_title = False
        elif tag in self.blocks:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            target = self.title if self.in_title else self.text
            target.append(data)


async def _read_website(value):
    url = _import_url(value)
    try:
        async with asyncio.timeout(IMPORT_TIMEOUT):
            for hop in range(MAX_REDIRECTS + 1):
                hostname = url.raw_host.decode("ascii")
                address = await _resolve_public_ip(hostname)
                authority = f"[{hostname}]" if ":" in hostname else hostname
                # The connection targets the validated IP, while verification and HTTP Host use the requested hostname.
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=IMPORT_TIMEOUT) as client:
                    async with client.stream(
                        "GET", url.copy_with(host=address),
                        headers={"Host": authority, "Accept": "text/html, application/xhtml+xml, text/plain",
                                 "Accept-Encoding": "identity"},
                        extensions={"sni_hostname": hostname.rstrip(".")},
                    ) as response:
                        if response.status_code in REDIRECT_CODES:
                            location = response.headers.get("location")
                            if not location or hop == MAX_REDIRECTS:
                                raise DomainError("Сайт вернул слишком много или некорректное перенаправление.", 422)
                            url = _import_url(urljoin(str(url), location))
                            continue
                        response.raise_for_status()
                        return await _extract_page(url, response)
    except (TimeoutError, httpx.TimeoutException) as error:
        raise DomainError("Сайт не ответил вовремя.", 504) from error
    except (OSError, ValueError, httpx.HTTPError) as error:
        raise DomainError("Не удалось загрузить страницу сайта.", 502) from error


async def _extract_page(url, response):
    media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type not in IMPORT_TYPES or response.headers.get("content-encoding", "identity").lower() != "identity":
        raise DomainError("Сайт должен вернуть HTML или обычный текст без сжатия.", 415)
    if int(response.headers.get("content-length", "0")) > MAX_IMPORT_BYTES:
        raise DomainError("Страница превышает 1 МБ.", 413)
    body = bytearray()
    async for chunk in response.aiter_raw():
        if len(body) + len(chunk) > MAX_IMPORT_BYTES:
            raise DomainError("Страница превышает 1 МБ.", 413)
        body.extend(chunk)

    encoding = response.charset_encoding or "utf-8-sig"
    try:
        text = body.decode(encoding, errors="replace")
    except (LookupError, UnicodeError):
        text = body.decode("utf-8-sig", errors="replace")
    text = text.replace("\x00", "")
    title = url.host
    if media_type != "text/plain":
        page = _PageText()
        page.feed(text)
        page.close()
        text = "\n".join(line for part in "".join(page.text).splitlines() if (line := " ".join(part.split())))
        title = " ".join("".join(page.title).split()) or title
    if not text.strip():
        raise DomainError("На странице не найден текст для импорта.", 422)
    return title[:240], text.strip()[:MAX_SOURCE_TEXT], str(url)


def register_knowledge_routes(api, service, member, editor):
    base = "/companies/{cid}/knowledge"

    @api.get(base)
    def list_entries(cid: int, user=Depends(member)):
        with service.db.read() as connection:
            service.require_actor(connection, cid, user, EDITOR_ROLES | {"approver"})
            return [dict(row) for row in connection.execute(
                ENTRY_SELECT + "WHERE k.company_id=? ORDER BY k.updated_at DESC,k.id DESC", (cid,)
            )]

    @api.post(base, status_code=201)
    def create_entry(cid: int, payload: KnowledgeCreate, user=Depends(editor)):
        return _create_entry(service, cid, payload, user)

    @api.post(base + "/import-url", status_code=201)
    async def import_website(cid: int, payload: WebsiteImport, user=Depends(editor)):
        with service.db.read() as connection:
            service.require_actor(connection, cid, user, EDITOR_ROLES)
        title, text, origin_url = await _read_website(payload.url)
        entry = KnowledgeCreate(title=title, text=text, category="business")
        return _create_entry(service, cid, entry, user, origin_url=origin_url)

    @api.patch(base + "/{entry_id}")
    def patch_entry(cid: int, entry_id: int, payload: KnowledgePatch, user=Depends(editor)):
        changes = payload.model_dump(exclude_unset=True)
        with service.db.transaction() as connection:
            service.require_actor(connection, cid, user, EDITOR_ROLES)
            current = _entry(connection, cid, entry_id)
            if not any(current[key] != value for key, value in changes.items()):
                return current
            revised = {**current, **changes}
            source_id = _snapshot(service, connection, cid, revised["title"], revised["text"])
            connection.execute(
                "UPDATE knowledge_entries SET source_id=?,category=?,status=?,updated_at=? WHERE company_id=? AND id=?",
                (source_id, revised["category"], revised["status"], utc_now(), cid, entry_id),
            )
            audit_row(connection, cid, "knowledge.edited", f"Изменена запись базы знаний №{entry_id}.", user)
            return _entry(connection, cid, entry_id)

    @api.delete(base + "/{entry_id}", status_code=204)
    def archive_entry(cid: int, entry_id: int, user=Depends(editor)):
        with service.db.transaction() as connection:
            service.require_actor(connection, cid, user, EDITOR_ROLES)
            if _entry(connection, cid, entry_id)["status"] == "archived":
                return
            connection.execute("UPDATE knowledge_entries SET status='archived',updated_at=? WHERE company_id=? AND id=?",
                               (utc_now(), cid, entry_id))
            audit_row(connection, cid, "knowledge.archived", f"Запись базы знаний №{entry_id} перемещена в архив.", user)
