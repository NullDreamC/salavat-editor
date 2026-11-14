from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import asyncio
import hashlib
import json
import secrets
import unicodedata
import zipfile

from . import billing, integrations
from .db import ROOT, reveal_secret, store_secret


TEMPLATES = {"announcement", "person", "results"}
EDITOR_ROLES = {"admin", "editor"}
APPROVER_ROLES = {"admin", "approver"}
SECRET_FIELDS = {"openai_key", "yandex_key", "telegram_token"}
COMPANY_FIELDS = (
    "id", "name", "timezone", "calendar_enabled", "provider", "model", "channel_id",
    "chat_id", "bot_username", "yandex_folder_id", "monthly_limit", "requests_limit", "editorial_rules",
    "funding_mode",
)
SOURCE_FIELDS = ("id", "title", "text", "kind", "created_at", "mime_type")
POST_FIELDS = (
    "id", "title", "body", "kind", "status", "version", "created_at", "scheduled_at",
    "channel_id", "platform", "created_by", "template", "publication_url", "feedback",
)
EDITABLE = {"draft", "needs_info", "review", "approved", "rejected"}
MAX_UPLOAD_SIZE = 40 * 1024 * 1024
MAX_SOURCE_TEXT = 120_000


class DomainError(Exception):
    def __init__(self, detail, status_code=400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def can_review(member):
    member = dict(member)
    return bool(member.get("active", True)) and (
        member.get("role") in APPROVER_ROLES
        or (member.get("role") == "editor" and bool(member.get("can_approve")))
    )


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pack(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def audit_row(connection, company_id, action, detail, actor=None):
    actor_id = actor["id"] if isinstance(actor, dict) else actor
    connection.execute(
        "INSERT INTO activity(company_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
        (company_id, actor_id, action, detail, utc_now()),
    )


def file_url(company_id, source_id):
    return f"/api/companies/{company_id}/sources/{source_id}/file"


def normalize_text(value):
    return " ".join(unicodedata.normalize("NFC", value).split())


def editorial_instruction(company):
    sections = []
    if company["id"] == 1:
        instruction = (ROOT / "Инструкция_ИИ_редактору.md").read_text(encoding="utf-8-sig")
        _, start, rest = instruction.partition("## Начало инструкции\n")
        content, end, _ = rest.partition("\n## Конец инструкции")
        if start and end:
            sections.append(content.strip())
    if company["editorial_rules"]:
        sections.append("Редакционные правила компании применяй к языку, тону и структуре поста. "
                        "Проверяемые утверждения по-прежнему подтверждай материалами задания.\n\n"
                        + company["editorial_rules"])
    return "\n\n".join(sections)


def upload_content(filename, data, mime_type):
    extension = Path(filename).suffix.lower()
    if len(data) > MAX_UPLOAD_SIZE:
        raise DomainError("Файл превышает 40 МБ.", 413)
    if not data:
        raise DomainError("Файл пуст.")
    try:
        if extension in {".txt", ".md", ".csv"}:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("cp1251")
            if "\x00" in text:
                raise ValueError
            return "document", "text/plain", text
        if extension == ".pdf" and data.startswith(b"%PDF-"):
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                raise DomainError("Загрузите PDF без пароля.", 422)
            parts = []
            size = 0
            for page in reader.pages:
                page_text = page.extract_text() or ""
                parts.append(page_text)
                size += len(page_text)
                if size >= MAX_SOURCE_TEXT:
                    break
            return "document", "application/pdf", "\n\n".join(parts)
        if extension == ".docx":
            from docx import Document

            with zipfile.ZipFile(BytesIO(data)) as archive:
                if sum(info.file_size for info in archive.infolist()) > 60 * 1024 * 1024:
                    raise DomainError("Размер распакованного документа слишком велик.", 413)
            document = Document(BytesIO(data))
            blocks = [paragraph.text for paragraph in document.paragraphs]
            for table in document.tables:
                blocks.extend(" | ".join(cell.text for cell in row.cells) for row in table.rows)
            return "document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "\n".join(blocks)
        if extension in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            from PIL import Image

            with Image.open(BytesIO(data), formats=["JPEG", "PNG", "WEBP", "GIF"]) as image:
                if image.width * image.height > 30_000_000:
                    raise DomainError("Изображение содержит слишком много пикселей.", 413)
                image.verify()
                return "image", Image.MIME[image.format], ""
        if extension in {".mp4", ".mov"} and data[4:8] == b"ftyp":
            return "video", "video/mp4" if extension == ".mp4" else "video/quicktime", ""
        if extension == ".webm" and data.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in data[:4096]:
            return "video", "video/webm", ""
    except DomainError:
        raise
    except Exception as error:
        raise DomainError("Не удалось прочитать файл. Проверьте его формат и содержимое.", 422) from error
    raise DomainError("Поддерживаются TXT, MD, CSV, PDF, DOCX, JPEG, PNG, WEBP, GIF, MP4, MOV и WEBM.", 415)


class Service:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def require_actor(connection, company_id, actor, roles, *, allow_editor_approval=False):
        Service._company_row(connection, company_id)
        if actor is None:
            return
        actor_id = actor["id"] if isinstance(actor, dict) else actor
        account = connection.execute("SELECT is_admin FROM users WHERE id=?", (actor_id,)).fetchone()
        if account and account["is_admin"]:
            if "super_admin" in roles:
                return
            raise DomainError("Главный администратор не имеет доступа к данным сотрудников.", 403)
        membership = connection.execute(
            "SELECT role,telegram_id,can_approve FROM memberships WHERE company_id=? AND user_id=? AND active=1",
            (company_id, actor_id),
        ).fetchone()
        if membership is None or not (membership["role"] in roles or (allow_editor_approval and can_review(membership))):
            raise DomainError("Недостаточно прав для этого действия.", 403)
        if isinstance(actor, dict) and actor.get("telegram_id") and actor["telegram_id"] != membership["telegram_id"]:
            raise DomainError("Доступ через Telegram отозван.", 403)

    @staticmethod
    def require_approval_actor(connection, company_id, actor):
        Service.require_actor(connection, company_id, actor, APPROVER_ROLES, allow_editor_approval=True)

    @staticmethod
    def _company_row(connection, company_id):
        row = connection.execute("SELECT * FROM companies WHERE id=? AND archived_at IS NULL", (company_id,)).fetchone()
        if row is None:
            raise DomainError("Компания не найдена.", 404)
        return row

    @staticmethod
    def _company_data(row):
        result = {field: row[field] for field in COMPANY_FIELDS}
        result["calendar_enabled"] = bool(result["calendar_enabled"])
        return result

    def companies(self):
        with self.db.read() as connection:
            return [self._company_data(row) for row in connection.execute(
                "SELECT * FROM companies WHERE archived_at IS NULL ORDER BY id")]

    def company(self, company_id, settings=False):
        with self.db.read() as connection:
            row = self._company_row(connection, company_id)
            result = self._company_data(row)
            if settings:
                supplier, fallback = self._provider_supplier(connection, row)
                result.update(self._provider_status(supplier, fallback), telegram_configured=bool(row["telegram_token"]))
            return result

    @staticmethod
    def _credentials(row, fields=SECRET_FIELDS):
        try:
            return {field: reveal_secret(row[field]) for field in fields}
        except OSError as error:
            raise DomainError("Хранилище секретов недоступно для текущего пользователя.", 503) from error

    @staticmethod
    def _shared_row(connection):
        return connection.execute("SELECT * FROM shared_api_settings WHERE id=1").fetchone()

    def _provider_supplier(self, connection, company_row):
        mode = company_row["funding_mode"]
        if mode == "shared":
            return self._shared_row(connection), False
        supplier = self._company_row(connection, 1) if mode == "salavat" else company_row
        return supplier, bool(supplier["allow_openai_fallback"])

    @staticmethod
    def _provider_status(supplier, fallback=False):
        return {
            "openai_configured": bool(supplier["openai_key"] or (fallback and integrations.openai_fallback_available())),
            "yandex_configured": bool(supplier["yandex_key"]),
        }

    def provider_context(self, connection, company_row, provider=None):
        supplier, fallback = self._provider_supplier(connection, company_row)
        company = self._company_data(company_row)
        if provider is not None:
            company["provider"] = provider
        company.update(yandex_folder_id=supplier["yandex_folder_id"], allow_openai_fallback=fallback)
        key_field = f"{company['provider']}_key"
        if key_field not in {"openai_key", "yandex_key"}:
            raise DomainError("Выберите сервис ИИ в настройках компании.", 422)
        return company, self._credentials(supplier, {"openai_key", "yandex_key"})

    def shared_api_settings(self):
        with self.db.read() as connection:
            row = self._shared_row(connection)
            return {**self._provider_status(row), "yandex_folder_id": row["yandex_folder_id"]}

    def update_shared_api_settings(self, changes):
        with self.db.transaction() as connection:
            for field, value in changes.items():
                if field not in {"openai_key", "yandex_key", "yandex_folder_id"}:
                    raise DomainError("Недопустимое поле общего подключения.", 422)
                if field in SECRET_FIELDS:
                    value = self._store_secret(value)
                connection.execute(f"UPDATE shared_api_settings SET {field}=? WHERE id=1", (value,))
        return self.shared_api_settings()

    @staticmethod
    def _store_secret(value):
        try:
            return store_secret(value)
        except OSError as error:
            raise DomainError("Хранилище секретов недоступно.", 503) from error

    def credentials(self, company_id):
        with self.db.read() as connection:
            return self._credentials(self._company_row(connection, company_id))

    async def models(self, company_id, provider, *, actor):
        with self.db.read() as connection:
            self.require_actor(connection, company_id, actor, {"admin", "super_admin"})
            company, credentials = self.provider_context(connection, self._company_row(connection, company_id), provider)
        try:
            return await integrations.list_models(company, credentials)
        except integrations.IntegrationError as error:
            # A provider's 401 must not expire the user's web session.
            raise DomainError(str(error), 502) from None

    def create_company(self, name, *, actor=None):
        with self.db.transaction() as connection:
            company_id = connection.execute("INSERT INTO companies(name) VALUES(?)", (name,)).lastrowid
            audit_row(connection, company_id, "company.created", "Компания создана.", actor)
        return self.company(company_id)

    @staticmethod
    def _has_salavat_dependents(connection):
        return connection.execute("SELECT 1 FROM companies WHERE funding_mode='salavat' AND archived_at IS NULL LIMIT 1").fetchone() is not None

    def provider_credentials_editable(self, company, role):
        if company["id"] == 1 and role == "super_admin":
            return True
        if company["funding_mode"] != "own":
            return False
        with self.db.read() as connection:
            return company["id"] != 1 or not self._has_salavat_dependents(connection)

    def archive_company(self, company_id, *, actor):
        with self.db.transaction() as connection:
            self.require_actor(connection, company_id, actor, {"super_admin"})
            if company_id == 1 and self._has_salavat_dependents(connection):
                raise DomainError("Сначала измените источник оплаты у компаний, которые используют API Салаватстекло.", 409)
            if connection.execute("SELECT 1 FROM posts WHERE company_id=? AND status='publishing' LIMIT 1", (company_id,)).fetchone():
                raise DomainError("Дождитесь завершения отправки в Telegram.", 409)
            connection.execute("UPDATE companies SET archived_at=? WHERE id=?", (utc_now(), company_id))
            connection.execute("UPDATE invitations SET revoked=1 WHERE company_id=? AND used_at IS NULL", (company_id,))
            connection.execute(
                "DELETE FROM sessions WHERE user_id IN (SELECT user_id FROM memberships WHERE company_id=?) "
                "AND NOT EXISTS (SELECT 1 FROM memberships m JOIN companies c ON c.id=m.company_id "
                "WHERE m.user_id=sessions.user_id AND m.active=1 AND c.archived_at IS NULL)",
                (company_id,),
            )
            audit_row(connection, company_id, "company.archived", "Компания перемещена в архив.", actor)

    def update_company(self, company_id, changes, *, actor=None):
        with self.db.transaction() as connection:
            current = self._company_row(connection, company_id)
            self.require_actor(connection, company_id, actor, {"admin", "super_admin"})
            actor_id = actor["id"] if isinstance(actor, dict) else actor
            is_global = actor_id is None or connection.execute("SELECT is_admin FROM users WHERE id=?", (actor_id,)).fetchone()["is_admin"]
            funding = changes.get("funding_mode", current["funding_mode"])
            if funding not in {"own", "salavat", "shared"}:
                raise DomainError("Выберите источник оплаты API.", 422)
            if not is_global and ("funding_mode" in changes or (funding != "own" and "requests_limit" in changes)):
                raise DomainError("Источник оплаты и лимит общего API меняет главный администратор.", 403)
            if "monthly_limit" in changes:
                if billing._active(connection, company_id):
                    raise DomainError("Измените рублёвый лимит в разделе «Расходы».", 409)
                if not is_global and (funding != "own" or (company_id == 1 and self._has_salavat_dependents(connection))):
                    raise DomainError("Рублёвый лимит общей оплаты меняет главный администратор.", 403)
            provider_fields = {"openai_key", "yandex_key", "yandex_folder_id"}
            if not is_global and provider_fields.intersection(changes):
                if funding != "own" or (company_id == 1 and self._has_salavat_dependents(connection)):
                    raise DomainError("Ключи и каталог общего API меняет главный администратор.", 403)
            if funding != "own" and not changes.get("requests_limit", current["requests_limit"]):
                raise DomainError("Укажите положительный месячный лимит запросов к общему API.", 422)
            if funding == "salavat":
                if company_id == 1:
                    raise DomainError("Для собственного API Салаватстекло выберите «Свои ключи».", 422)
                self._company_row(connection, 1)
            if "timezone" in changes:
                try:
                    ZoneInfo(changes["timezone"])
                except (ZoneInfoNotFoundError, ValueError) as error:
                    raise DomainError("Укажите часовой пояс IANA, например Europe/Moscow.", 422) from error
            if "provider" in changes and changes["provider"] != current["provider"] and "model" not in changes:
                changes["model"] = "gpt-4.1-mini" if changes["provider"] == "openai" else "qwen3.6-35b-a3b"
            for field, value in changes.items():
                if field not in COMPANY_FIELDS[1:] and field not in SECRET_FIELDS:
                    raise DomainError("Недопустимое поле настроек.", 422)
                if field in SECRET_FIELDS:
                    try:
                        if field == "telegram_token" and value:
                            tokens = connection.execute(
                                "SELECT telegram_token FROM companies WHERE id<>? AND telegram_token IS NOT NULL "
                                "AND archived_at IS NULL",
                                (company_id,),
                            )
                            if any(reveal_secret(row["telegram_token"]) == value for row in tokens):
                                raise DomainError("Для каждой компании нужен отдельный бот Telegram.", 409)
                        value = self._store_secret(value)
                    except OSError as error:
                        raise DomainError("Хранилище секретов недоступно.", 503) from error
                connection.execute(f"UPDATE companies SET {field}=? WHERE id=?", (value, company_id))
            if changes:
                audit_row(connection, company_id, "company.settings", "Настройки компании обновлены.", actor)
        return self.company(company_id, settings=True)

    @staticmethod
    def _user_data(row):
        return {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"],
                "telegram_id": row["telegram_id"], "active": bool(row["active"]), "can_approve": bool(row["can_approve"])}

    def telegram_users(self, company_id):
        with self.db.read() as connection:
            self._company_row(connection, company_id)
            rows = connection.execute(
                "SELECT u.id,u.name,u.email,m.role,m.active,m.telegram_id,m.can_approve FROM memberships m "
                "JOIN users u ON u.id=m.user_id WHERE m.company_id=? AND u.is_admin=0 ORDER BY u.name,u.id", (company_id,),
            )
            return [self._user_data(row) for row in rows]

    def telegram_members(self, company_id, telegram_id):
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT u.id,u.name,u.email,m.role,m.active,m.telegram_id,m.can_approve FROM memberships m "
                "JOIN users u ON u.id=m.user_id JOIN companies c ON c.id=m.company_id "
                "WHERE m.company_id=? AND m.telegram_id=? AND m.active=1 AND u.is_admin=0 AND c.archived_at IS NULL",
                (company_id, str(telegram_id)),
            ).fetchone()
            return self._user_data(row) if row is not None else None

    def set_telegram_member(self, company_id, telegram_id, *, role="editor", active=True, actor=None):
        telegram_id = str(telegram_id)
        if not telegram_id.isascii() or not telegram_id.isdigit() or not 0 < int(telegram_id) < 2**63:
            raise DomainError("Некорректный ID пользователя Telegram.", 422)
        if role not in {"editor", "approver"}:
            raise DomainError("Доступны роли редактора и согласующего.", 422)
        with self.db.transaction() as connection:
            self._company_row(connection, company_id)
            self.require_actor(connection, company_id, actor, {"admin"})
            existing = connection.execute(
                "SELECT m.*,u.password_hash FROM memberships m JOIN users u ON u.id=m.user_id "
                "WHERE m.company_id=? AND m.telegram_id=?", (company_id, telegram_id),
            ).fetchone()
            if existing is not None:
                if existing["role"] == "admin":
                    raise DomainError("Измените Telegram ID администратора в веб-кабинете.", 409)
                if active and existing["role"] != role:
                    connection.execute(
                        "UPDATE memberships SET role=?,active=1,can_approve=0 WHERE company_id=? AND user_id=?",
                        (role, company_id, existing["user_id"]),
                    )
                elif active:
                    connection.execute(
                        "UPDATE memberships SET active=1 WHERE company_id=? AND user_id=?",
                        (company_id, existing["user_id"]),
                    )
                elif existing["password_hash"] is None:
                    connection.execute(
                        "UPDATE memberships SET active=0 WHERE company_id=? AND user_id=?",
                        (company_id, existing["user_id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE memberships SET telegram_id=NULL WHERE company_id=? AND user_id=?",
                        (company_id, existing["user_id"]),
                    )
            elif active:
                user_id = connection.execute(
                    "INSERT INTO users(name,email) VALUES(?,?)",
                    (f"Telegram {telegram_id}", f"telegram-{company_id}-{telegram_id}-{secrets.token_hex(4)}@local.invalid"),
                ).lastrowid
                connection.execute(
                    "INSERT INTO memberships(company_id,user_id,role,telegram_id) VALUES(?,?,?,?)",
                    (company_id, user_id, role, telegram_id),
                )
            else:
                raise DomainError("Пользователь Telegram не найден.", 404)
            audit_row(connection, company_id, "member.telegram", f"Обновлён доступ Telegram {telegram_id}.", actor)
        return self.telegram_members(company_id, telegram_id)

    def audit(self, company_id, action, detail, actor=None):
        with self.db.transaction() as connection:
            audit_row(connection, company_id, action, detail, actor)

    def activity(self, company_id):
        with self.db.read() as connection:
            self._company_row(connection, company_id)
            return [dict(row) for row in connection.execute(
                "SELECT id,action,detail,created_at FROM activity WHERE company_id=? ORDER BY id DESC LIMIT 300",
                (company_id,),
            )]

    @staticmethod
    def _source_row(connection, company_id, source_id):
        Service._company_row(connection, company_id)
        row = connection.execute("SELECT * FROM sources WHERE company_id=? AND id=?", (company_id, source_id)).fetchone()
        if row is None:
            raise DomainError("Материал не найден.", 404)
        return row

    def _source_data(self, row, internal=False):
        data = {key: row[key] for key in SOURCE_FIELDS}
        if row["file_path"]:
            data["file_url"] = file_url(row["company_id"], row["id"])
            if internal:
                data["file_path"] = str(self._file_path(row["company_id"], row["file_path"]))
        return data

    def _file_path(self, company_id, path):
        company_folder = self.db.uploads_dir / str(company_id)
        resolved = Path(path).resolve()
        if company_folder not in resolved.parents or not resolved.is_file():
            raise DomainError("Файл материала недоступен.", 404)
        return resolved

    def sources(self, company_id):
        with self.db.read() as connection:
            self._company_row(connection, company_id)
            return [self._source_data(row) for row in connection.execute(
                "SELECT * FROM sources WHERE company_id=? AND kind<>'knowledge' ORDER BY id DESC", (company_id,),
            )]

    def source(self, company_id, source_id, internal=False):
        with self.db.read() as connection:
            return self._source_data(self._source_row(connection, company_id, source_id), internal)

    def _insert_source(self, connection, company_id, title, text, kind, file_path=None, mime_type=None):
        return connection.execute(
            "INSERT INTO sources(company_id,title,text,kind,created_at,file_path,mime_type) VALUES(?,?,?,?,?,?,?)",
            (company_id, title, text[:MAX_SOURCE_TEXT], kind, utc_now(), file_path, mime_type),
        ).lastrowid

    def create_source(self, company_id, title, text, *, actor=None, kind="text", file_path=None, mime_type=None):
        if file_path is not None:
            file_path = str(self._file_path(company_id, file_path))
        with self.db.transaction() as connection:
            self._company_row(connection, company_id)
            self.require_actor(connection, company_id, actor, EDITOR_ROLES)
            source_id = self._insert_source(connection, company_id, title, text, kind, file_path, mime_type)
            audit_row(connection, company_id, "source.created", f"Добавлен материал №{source_id}.", actor)
        return self.source(company_id, source_id)

    def store_upload(self, company_id, filename, content, mime_type, *, actor=None):
        self.company(company_id)
        title = Path(filename.replace("\\", "/")).name[:240]
        kind, real_mime, text = upload_content(title, content, mime_type)
        folder = self.db.uploads_dir / str(company_id)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / f"{secrets.token_hex(20)}{Path(title).suffix.lower()}"
        with path.open("xb") as stream:
            stream.write(content)
        try:
            return self.create_source(company_id, title, text.strip(), actor=actor, kind=kind,
                                      file_path=path, mime_type=real_mime)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _post_row(connection, company_id, post_id):
        Service._company_row(connection, company_id)
        row = connection.execute("SELECT * FROM posts WHERE company_id=? AND id=?", (company_id, post_id)).fetchone()
        if row is None:
            raise DomainError("Публикация не найдена.", 404)
        return row

    def _post_data(self, row):
        data = {field: row[field] for field in POST_FIELDS}
        data["source_ids"] = json.loads(row["source_ids"])
        data["questions"] = json.loads(row["questions"])
        data["facts"] = json.loads(row["facts"])
        data["publication_uncertain"] = bool(row["publication_uncertain"])
        data["card_url"] = file_url(row["company_id"], row["card_id"]) if row["card_id"] else None
        data["photo_url"] = file_url(row["company_id"], row["photo_id"]) if row["photo_id"] else None
        data["logo_editable"] = bool(row["photo_base_id"])
        data["logo"] = json.loads(row["logo_placement"]) if row["logo_placement"] else None
        data["logo_pending"] = bool(row["photo_base_id"] and not row["logo_placement"])
        return data

    def posts(self, company_id):
        with self.db.read() as connection:
            self._company_row(connection, company_id)
            return [self._post_data(row) for row in connection.execute(
                "SELECT * FROM posts WHERE company_id=? ORDER BY id DESC", (company_id,),
            )]

    def post(self, company_id, post_id):
        with self.db.read() as connection:
            return self._post_data(self._post_row(connection, company_id, post_id))

    @staticmethod
    def _check_version(row, version):
        if row["version"] != version:
            raise DomainError("Версия публикации изменилась. Откройте её заново.", 409)

    def _editable(self, row, version):
        self._check_version(row, version)
        if row["status"] not in EDITABLE and not (row["status"] == "failed" and not row["publication_uncertain"]):
            raise DomainError("Публикацию в этом статусе нельзя менять.", 409)

    def _validate_sources(self, connection, company_id, source_ids):
        result = list(dict.fromkeys(source_ids))
        for source_id in result:
            source = self._source_row(connection, company_id, source_id)
            if source['kind'] == 'knowledge':
                active = connection.execute(
                    "SELECT 1 FROM knowledge_entries WHERE company_id=? AND source_id=? AND status='active'",
                    (company_id, source_id),
                ).fetchone()
                if not active:
                    raise DomainError('Сведения из базы знаний ещё не подтверждены или архивированы.', 422)
        return result

    def create_post(self, company_id, source_ids, *, title=None, kind=None, actor=None, platform="telegram"):
        with self.db.transaction() as connection:
            return self._create_post(connection, company_id, source_ids, title=title, kind=kind,
                                     actor=actor, platform=platform)

    def _create_post(self, connection, company_id, source_ids, *, title=None, kind=None, actor=None, platform="telegram"):
        kind = kind or "general"
        if platform not in {'telegram', 'vk', 'max'}:
            raise DomainError('Неизвестная площадка.', 422)
        company = self._company_row(connection, company_id)
        self.require_actor(connection, company_id, actor, EDITOR_ROLES)
        source_ids = self._validate_sources(connection, company_id, source_ids)
        default_title = self._source_row(connection, company_id, source_ids[0])["title"] if source_ids else "Новый пост"
        destination = company['channel_id'] if platform == 'telegram' else self._social_destination(connection, company_id, platform)
        post_id = connection.execute(
            "INSERT INTO posts(company_id,title,kind,source_ids,created_at,channel_id,template,platform,created_by) VALUES(?,?,?,?,?,?,?,?,?)",
            (company_id, title or default_title, kind, pack(source_ids), utc_now(), destination,
             kind if kind in TEMPLATES else "announcement", platform, actor.get('id') if isinstance(actor, dict) else actor),
        ).lastrowid
        audit_row(connection, company_id, "post.created", f"Создан пост №{post_id}.", actor)
        return self._post_data(self._post_row(connection, company_id, post_id))

    @staticmethod
    def _social_destination(connection, company_id, platform):
        from .social_platforms import get_social_connection
        configuration = get_social_connection(connection, company_id, platform)
        return configuration.get('destination_id', '') if configuration else ''

    def _update_post(self, connection, company_id, post_id, **changes):
        assignments = ",".join(f"{column}=?" for column in changes)
        connection.execute(
            f"UPDATE posts SET {assignments},version=version+1 WHERE company_id=? AND id=?",
            (*changes.values(), company_id, post_id),
        )
        return self._post_data(self._post_row(connection, company_id, post_id))

    def patch_post(self, company_id, post_id, version, changes, *, actor=None):
        with self.db.transaction() as connection:
            row = self._post_row(connection, company_id, post_id)
            self.require_actor(connection, company_id, actor, EDITOR_ROLES)
            self._editable(row, version)
            if 'platform' in changes:
                if changes['platform'] not in {'telegram', 'vk', 'max'}:
                    raise DomainError('Неизвестная площадка.', 422)
                if changes['platform'] != row['platform'] and 'channel_id' not in changes:
                    company = self._company_row(connection, company_id)
                    changes['channel_id'] = (company['channel_id'] if changes['platform'] == 'telegram' else
                                             self._social_destination(connection, company_id, changes['platform']))
            if "source_ids" in changes:
                changes["source_ids"] = pack(self._validate_sources(connection, company_id, changes["source_ids"]))
            selected_platform = changes.get('platform', row['platform'])
            if selected_platform != 'telegram' and 'channel_id' in changes:
                target = self._social_destination(connection, company_id, selected_platform)
                if changes['channel_id'] != target:
                    raise DomainError('Адрес площадки задаётся в настройках подключения.', 422)
            if changes.get("scheduled_at") is not None:
                schedule = changes["scheduled_at"]
                if schedule.tzinfo is None:
                    zone = ZoneInfo(self._company_row(connection, company_id)["timezone"])
                    schedule = schedule.replace(tzinfo=zone)
                changes["scheduled_at"] = schedule.astimezone(timezone.utc).isoformat(timespec="seconds")
            changes = {field: value for field, value in changes.items() if value != row[field]}
            if not changes:
                return self._post_data(row)
            if {"title", "body", "template", "source_ids"} & changes.keys():
                changes["card_id"] = None
            if "source_ids" in changes:
                changes["photo_id"] = None
                changes["photo_base_id"] = None
                changes["logo_placement"] = None
            if {"title", "body", "source_ids"} & changes.keys():
                changes["facts"] = "[]"
            audit_row(connection, company_id, "post.edited", f"Изменён пост №{post_id}.", actor)
            return self._update_post(connection, company_id, post_id, **changes,
                                     status="needs_info" if json.loads(row["questions"]) else "draft",
                                     approved_snapshot=None, feedback=None, publication_uncertain=0)

    @staticmethod
    def _usage(connection, company_id, company):
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        summary = connection.execute(
            "SELECT COUNT(*) AS requests, COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens FROM usage WHERE company_id=? AND month=?",
            (company_id, month),
        ).fetchone()
        return month, {**dict(summary), "monthly_limit": company["monthly_limit"], "requests_limit": company["requests_limit"]}

    def usage(self, company_id):
        with self.db.read() as connection:
            _, summary = self._usage(connection, company_id, self._company_row(connection, company_id))
            return summary

    def _reserve_generation(self, connection, company, company_id, *, kind="text"):
        current = self._company_row(connection, company_id)
        month, summary = self._usage(connection, company_id, current)
        if current["funding_mode"] != "own" and not summary["requests_limit"]:
            raise DomainError("Для общего API нужен месячный лимит запросов.", 422)
        if summary["requests_limit"] and summary["requests"] >= summary["requests_limit"]:
            raise DomainError("Месячный лимит запросов ИИ исчерпан.", 429)
        usage_id = connection.execute("INSERT INTO usage(company_id,month) VALUES(?,?)", (company_id, month)).lastrowid
        billing.reserve_generation(connection, current, month, company, usage_id, kind)
        return usage_id

    @staticmethod
    def _grounding(result, sources):
        sources_by_id = {source["id"]: source for source in sources}
        source_text = {source["id"]: normalize_text(source["text"]) for source in sources}
        facts = []
        questions = list(result["questions"])
        for fact in result["facts"]:
            ids = set(fact["source_ids"])
            evidence_ids = set()
            for item in fact["evidence"]:
                source_id = item["source_id"]
                quote = normalize_text(item["quote"])
                source = sources_by_id.get(source_id)
                if source and quote and (quote in source_text[source_id] or source["kind"] == "image"):
                    evidence_ids.add(source_id)
            if ids and ids <= evidence_ids:
                facts.append(fact)
            else:
                questions.append(f"Подтвердите факт источником: {fact['text']}")
        if result["body"].strip() and not facts:
            questions.append("Каким материалом подтверждаются ключевые сведения для поста?")
        return facts, list(dict.fromkeys(questions))

    @staticmethod
    def _save_extracted_events(connection, company_id, events, sources):
        source_text = {source["id"]: normalize_text(source["text"]) for source in sources}
        for event in events:
            source_id = event["source_id"]
            evidence = normalize_text(event["evidence"])
            event_date = event["event_date"]
            if not evidence or evidence not in source_text.get(source_id, ""):
                continue
            if not integrations.date_is_grounded(event_date, evidence):
                continue
            existing = connection.execute(
                "SELECT id FROM events WHERE company_id=? AND source_id=? AND title=? AND event_date=?",
                (company_id, source_id, event["title"], event_date),
            ).fetchone()
            if not existing:
                connection.execute("INSERT INTO events(company_id,title,event_date,description,source_id) VALUES(?,?,?,?,?)",
                                   (company_id, event["title"], event_date, event["description"], source_id))
                audit_row(connection, company_id, "event.extracted", "Добавлено событие с подтверждённой датой.")

    async def generate(self, company_id, post_id, version, *, actor=None, revision=""):
        with self.db.transaction() as connection:
            row = self._post_row(connection, company_id, post_id)
            self.require_actor(connection, company_id, actor, EDITOR_ROLES)
            self._editable(row, version)
            source_ids = json.loads(row["source_ids"])
            if not source_ids:
                raise DomainError("Добавьте исходные материалы для подготовки текста.", 422)
            self._validate_sources(connection, company_id, source_ids)
            sources = [self._source_data(self._source_row(connection, company_id, source_id), True)
                       for source_id in source_ids]
            from .knowledge import knowledge_source_rows
            sources.extend(source for source in knowledge_source_rows(connection, company_id)
                           if source['id'] not in source_ids)
            company_row = self._company_row(connection, company_id)
            company, credentials = self.provider_context(connection, company_row)
            company.update(post_title=row["title"], post_kind=row["kind"], scheduled_at=row["scheduled_at"],
                           channel_id=row["channel_id"], platform=row["platform"], template=row["template"])
            if revision:
                company.update(post_body=row['body'], revision=revision)
            if company["provider"] == "openai" and not credentials.get("openai_key"):
                if not (company["allow_openai_fallback"] and integrations.openai_fallback_available()):
                    raise DomainError("Добавьте ключ OpenAI в настройках компании.", 422)
            if company["provider"] == "yandex" and (not credentials.get("yandex_key") or not company["yandex_folder_id"]):
                raise DomainError("Укажите ключ и каталог Yandex AI Studio в настройках компании.", 422)
            claimed = connection.execute(
                "INSERT OR IGNORE INTO post_generation_claims(company_id,post_id,version) VALUES(?,?,?)",
                (company_id, post_id, version),
            ).rowcount
            if not claimed:
                claimed = connection.execute(
                    "UPDATE post_generation_claims SET status='running' WHERE company_id=? AND post_id=? AND version=? "
                    "AND status IN ('failed','interrupted')", (company_id, post_id, version),
                ).rowcount
            if not claimed:
                raise DomainError('Эта версия уже отправлена на подготовку. Дождитесь результата и обновите пост.', 409)
            usage_id = self._reserve_generation(connection, company, company_id)
        finish_claim = (
            "UPDATE post_generation_claims SET status=? WHERE company_id=? AND post_id=? AND version=? "
            "AND status='running'"
        )
        try:
            instruction = editorial_instruction(company)
            with billing.billing_scope(self.db, text=usage_id):
                result = await integrations.generate_post(company, sources, instruction, credentials)
            with self.db.transaction() as connection:
                connection.execute("UPDATE usage SET input_tokens=?,output_tokens=? WHERE id=?",
                                   (result["input_tokens"], result["output_tokens"], usage_id))
            facts, questions = self._grounding(result, sources)
            knowledge_ids = {source["id"] for source in sources if source["kind"] == "knowledge"}
            with self.db.transaction() as connection:
                row = self._post_row(connection, company_id, post_id)
                self.require_actor(connection, company_id, actor, EDITOR_ROLES)
                self._editable(row, version)
                # A snapshot can be revoked while the provider prepares its response.
                if knowledge_ids:
                    active_ids = {source["source_id"] for source in connection.execute(
                        "SELECT source_id FROM knowledge_entries WHERE company_id=? AND status='active'", (company_id,),
                    )}
                    revoked = knowledge_ids - active_ids
                    if revoked:
                        questions.append('База знаний изменилась во время подготовки. Проверьте сведения и обновите текст.')
                        facts = [fact for fact in facts if revoked.isdisjoint(fact["source_ids"])]
                connection.execute(finish_claim, ("completed", company_id, post_id, version))
                if not questions and self._company_row(connection, company_id)["calendar_enabled"]:
                    self._save_extracted_events(connection, company_id, result["events"], sources)
                audit_row(connection, company_id, "post.generated", f"Подготовлен текст поста №{post_id}.", actor)
                return self._update_post(connection, company_id, post_id, title=result["title"] or row["title"],
                                         body=result["body"], facts=pack(facts), questions=pack(questions),
                                         status="needs_info" if questions else "draft", approved_snapshot=None,
                                         card_id=None, feedback=None, publication_uncertain=0)
        except (Exception, asyncio.CancelledError) as error:
            provider_error = isinstance(error, integrations.IntegrationError)
            claim_status = "interrupted" if isinstance(error, asyncio.CancelledError) else "failed"
            with self.db.transaction() as connection:
                connection.execute(finish_claim, (claim_status, company_id, post_id, version))
                if provider_error:
                    connection.execute("UPDATE usage SET input_tokens=?,output_tokens=? WHERE id=?",
                                       (getattr(error, "input_tokens", 0), getattr(error, "output_tokens", 0), usage_id))
            if not provider_error:
                raise
            self.audit(company_id, "post.generation_failed", f"Не удалось подготовить пост №{post_id}.", actor)
            status = 429 if error.status_code == 429 else 502
            raise DomainError(str(error), status) from error

    async def revise(self, company_id, post_id, version, text, *, actor=None):
        text = text.strip()
        if not text or len(text) > 4000:
            raise DomainError('Напишите правки к тексту, до 4000 символов.', 422)
        with self.db.transaction() as connection:
            row = self._post_row(connection, company_id, post_id)
            self.require_actor(connection, company_id, actor, EDITOR_ROLES)
            self._editable(row, version)
            source_id = self._insert_source(connection, company_id, f'Правки редактора к посту №{post_id}', text, 'text')
            updated = self._update_post(connection, company_id, post_id,
                                        source_ids=pack([*json.loads(row['source_ids']), source_id]),
                                        status='draft', approved_snapshot=None, card_id=None, feedback=None)
            audit_row(connection, company_id, 'post.revision_requested', f'Сохранены правки поста №{post_id}.', actor)
        try:
            return await self.generate(company_id, post_id, updated['version'], actor=actor, revision=text)
        except DomainError as error:
            raise DomainError(f'Правки сохранены. {error.detail}', error.status_code) from error

    async def answer(self, company_id, post_id, version, text, *, actor=None):
        with self.db.transaction() as connection:
            row = self._post_row(connection, company_id, post_id)
            self.require_actor(connection, company_id, actor, EDITOR_ROLES)
            self._editable(row, version)
            if row["status"] != "needs_info":
                raise DomainError("У поста нет открытых вопросов для уточнения.", 409)
            source_id = self._insert_source(connection, company_id, f"Уточнение к посту №{post_id}", text, "text")
            updated = self._update_post(connection, company_id, post_id,
                                        source_ids=pack([*json.loads(row["source_ids"]), source_id]),
                                        approved_snapshot=None, card_id=None)
            audit_row(connection, company_id, "post.answered", f"Добавлено уточнение к посту №{post_id}.", actor)
        try:
            return await self.generate(company_id, post_id, updated["version"], actor=actor)
        except DomainError as error:
            raise DomainError(f"Уточнение сохранено. {error.detail}", error.status_code) from error

    @staticmethod
    def _validate_publication(row, media):
        if row["photo_base_id"] and not row["logo_placement"]:
            raise DomainError("Выберите цвет и угол логотипа перед согласованием.", 422)
        if row['platform'] != 'telegram':
            from .social_platforms import validate_social_publication
            try:
                validate_social_publication(row['platform'], row['body'], media)
            except integrations.IntegrationError as error:
                raise DomainError(str(error), 422) from error
            return
        try:
            integrations.validate_publication(row["body"], media)
        except integrations.TelegramError as error:
            raise DomainError(str(error), 422) from error

    def submit(self, company_id, post_id, version, *, actor=None):
        with self.db.transaction() as connection:
            return self._submit_post(connection, company_id, post_id, version, actor=actor)

    def _submit_post(self, connection, company_id, post_id, version, *, actor=None):
        row = self._post_row(connection, company_id, post_id)
        self.require_actor(connection, company_id, actor, EDITOR_ROLES)
        self._check_version(row, version)
        if json.loads(row["questions"]):
            raise DomainError("Ответьте на открытые вопросы перед согласованием.", 409)
        if row["status"] not in {"draft", "rejected", "failed"} or row["publication_uncertain"]:
            raise DomainError("Пост нельзя отправить на согласование в этом статусе.", 409)
        if not row["body"].strip() or not row["title"].strip() or not json.loads(row["source_ids"]):
            raise DomainError("Для согласования нужны заголовок, текст и исходные материалы.", 422)
        self._validate_publication(row, self._media(connection, company_id, row))
        audit_row(connection, company_id, "post.submitted", f"Пост №{post_id} передан на согласование.", actor)
        return self._update_post(connection, company_id, post_id, status="review", feedback=None,
                                 approved_snapshot=None)

    def _media(self, connection, company_id, row):
        ids = json.loads(row["source_ids"])
        decorations = [source_id for source_id in (row["card_id"], row["photo_id"]) if source_id]
        media = []
        for source_id in dict.fromkeys(decorations + ids):
            source = self._source_row(connection, company_id, source_id)
            if row["photo_id"] and source["kind"] == "image" and source_id not in decorations:
                continue
            if source["kind"] in {"image", "video"} and source["file_path"]:
                media.append(self._source_data(source, internal=True))
        return media

    def publication_media(self, company_id, post):
        with self.db.read() as connection:
            row = self._post_row(connection, company_id, post["id"])
            self._check_version(row, post["version"])
            return self._media(connection, company_id, row)

    @staticmethod
    def _snapshot(row, media):
        snapshot = {key: row[key] for key in ("title", "body", "source_ids", "channel_id", "scheduled_at", "template", "card_id")}
        if row['platform'] != 'telegram':
            snapshot['platform'] = row['platform']
        if row["photo_id"]:
            snapshot["photo_id"] = row["photo_id"]
        snapshot["media"] = []
        for item in media:
            with Path(item["file_path"]).open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            snapshot["media"].append({"id": item["id"], "sha256": digest, "mime_type": item["mime_type"]})
        return pack(snapshot)

    def approve(self, company_id, post_id, version, *, actor=None):
        from .review_policy import require_reviewer
        with self.db.transaction() as connection:
            row = self._post_row(connection, company_id, post_id)
            require_reviewer(connection, company_id, actor)
            self._check_version(row, version)
            if row["status"] != "review" or json.loads(row["questions"]):
                raise DomainError("Согласовать можно только готовый пост на проверке.", 409)
            media = self._media(connection, company_id, row)
            self._validate_publication(row, media)
            snapshot = self._snapshot(row, media)
            audit_row(connection, company_id, "post.approved", f"Пост №{post_id} согласован.", actor)
            return self._update_post(connection, company_id, post_id, status="approved", approved_snapshot=snapshot)

    def reject(self, company_id, post_id, version, comment, *, actor=None):
        from .review_policy import require_reviewer
        with self.db.transaction() as connection:
            row = self._post_row(connection, company_id, post_id)
            require_reviewer(connection, company_id, actor)
            self._check_version(row, version)
            if row["status"] not in {"review", "approved"}:
                raise DomainError("Отклонить можно пост на согласовании или согласованный пост.", 409)
            audit_row(connection, company_id, "post.rejected", f"Пост №{post_id} возвращён на доработку.", actor)
            return self._update_post(connection, company_id, post_id, status="rejected", feedback=comment,
                                     approved_snapshot=None)

    def card(self, company_id, post_id, version, *, actor=None):
        from .cards import render_card

        with self.db.read() as connection:
            row = self._post_row(connection, company_id, post_id)
            self.require_actor(connection, company_id, actor, EDITOR_ROLES)
            self._editable(row, version)
            company = self._company_data(self._company_row(connection, company_id))
            post = self._post_data(row)
        folder = self.db.uploads_dir / str(company_id)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / f"{secrets.token_hex(20)}.png"
        try:
            try:
                render_card(company, post, path)
            except ValueError as error:
                raise DomainError(str(error), 422) from error
            with self.db.transaction() as connection:
                row = self._post_row(connection, company_id, post_id)
                self.require_actor(connection, company_id, actor, EDITOR_ROLES)
                self._editable(row, version)
                card_id = self._insert_source(connection, company_id, f"Карточка поста №{post_id}", "", "image",
                                              str(self._file_path(company_id, path)), "image/png")
                audit_row(connection, company_id, "post.card", f"Обновлена карточка поста №{post_id}.", actor)
                return self._update_post(connection, company_id, post_id, card_id=card_id,
                                         status="needs_info" if json.loads(row["questions"]) else "draft",
                                         approved_snapshot=None, feedback=None, publication_uncertain=0)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def _failed_publication(self, company_id, post_id, claim_version, error_text, uncertain, actor):
        with self.db.transaction() as connection:
            row = self._post_row(connection, company_id, post_id)
            if row["status"] == "publishing" and row["version"] == claim_version:
                self._update_post(connection, company_id, post_id, status="failed", feedback=error_text,
                                  publication_uncertain=int(uncertain))
                audit_row(connection, company_id, "post.publish_failed", f"Ошибка публикации поста №{post_id}.", actor)

    async def publish(self, company_id, post_id, version, *, actor=None, scheduled=False):
        with self.db.transaction() as connection:
            row = self._post_row(connection, company_id, post_id)
            self.require_approval_actor(connection, company_id, actor)
            self._check_version(row, version)
            if row["status"] != "approved" or not row["approved_snapshot"]:
                raise DomainError("Публиковать можно только согласованную версию поста.", 409)
            if not row["channel_id"].strip():
                raise DomainError("Укажите канал и повторите согласование перед публикацией.", 422)
            if row["scheduled_at"] and row["scheduled_at"] > utc_now():
                raise DomainError("Запланированное время публикации ещё не наступило.", 409)
            platform = row['platform']
            token = None
            social = None
            if platform == 'telegram':
                token = self._credentials(self._company_row(connection, company_id))["telegram_token"]
                if not token:
                    raise DomainError("Подключите бота Telegram в настройках компании.", 422)
            else:
                from .social_platforms import get_social_connection
                social = get_social_connection(connection, company_id, platform, include_secret=True)
                if not social or not social.get('enabled') or not social.get('token'):
                    raise DomainError('Подключите выбранную площадку в настройках компании.', 422)
                if str(social['destination_id']) != row['channel_id']:
                    raise DomainError('Адрес площадки изменился. Обновите пост и повторите согласование.', 409)
            media = self._media(connection, company_id, row)
            self._validate_publication(row, media)
            if self._snapshot(row, media) != row["approved_snapshot"]:
                raise DomainError("Материалы изменились. Повторите согласование поста.", 409)
            approved = json.loads(row["approved_snapshot"])
            claim = self._update_post(connection, company_id, post_id, status="publishing")
            audit_row(connection, company_id, "post.publishing", f"Начата публикация поста №{post_id}.", actor)
        uncertain_text = "Площадка могла принять публикацию. Проверьте канал вручную. Повторная отправка заблокирована."
        try:
            if platform == 'telegram':
                result = await integrations.publish_telegram(token, approved["channel_id"], approved["body"], media)
            else:
                from .social_platforms import publish_social
                result = await publish_social(platform, social, approved['body'], media)
        except integrations.IntegrationError as error:
            uncertain = getattr(error, 'uncertain', True)
            detail = uncertain_text if uncertain else str(error)
            self._failed_publication(company_id, post_id, claim["version"], detail, uncertain, actor)
            status = 429 if error.status_code == 429 else 502
            raise DomainError(detail, status) from error
        except asyncio.CancelledError:
            self._failed_publication(company_id, post_id, claim["version"], uncertain_text, True, actor)
            raise
        except Exception as error:
            self._failed_publication(company_id, post_id, claim["version"], uncertain_text, True, actor)
            raise DomainError(uncertain_text, 502) from error
        with self.db.transaction() as connection:
            audit_row(connection, company_id, "post.published", f"Опубликован пост №{post_id}.", actor)
            return self._update_post(connection, company_id, post_id, status="published",
                                     message_id=str(result["message_id"]), publication_url=result.get("publication_url"),
                                     telegram_chat_id=result.get("telegram_chat_id") if platform == "telegram" else None)

    def due_posts(self):
        with self.db.read() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT p.id,p.company_id,p.version FROM posts p JOIN companies c ON c.id=p.company_id "
                "WHERE c.archived_at IS NULL AND p.status='approved' AND trim(p.channel_id)<>'' "
                "AND p.scheduled_at<=? ORDER BY p.scheduled_at,p.id",
                (utc_now(),),
            )]

    def recover_publications(self):
        # A process interruption can happen after Telegram accepted the message.
        with self.db.transaction() as connection:
            connection.execute("UPDATE post_generation_claims SET status='interrupted' WHERE status='running'")
            for row in connection.execute("SELECT id,company_id FROM posts WHERE status='publishing'").fetchall():
                self._update_post(connection, row["company_id"], row["id"], status="failed", publication_uncertain=1,
                                  feedback="Публикация прервалась. Проверьте канал вручную. Повторная отправка заблокирована.")
                audit_row(connection, row["company_id"], "post.publish_interrupted", f"Прервана публикация поста №{row['id']}.")

    @staticmethod
    def _event_row(connection, company_id, event_id):
        row = connection.execute("SELECT id,title,event_date,description,source_id,reminded_at FROM events "
                                 "WHERE company_id=? AND id=?", (company_id, event_id)).fetchone()
        if row is None:
            raise DomainError("Событие не найдено.", 404)
        return row

    def events(self, company_id):
        with self.db.read() as connection:
            self._company_row(connection, company_id)
            return [dict(row) for row in connection.execute(
                "SELECT id,title,event_date,description,source_id,reminded_at FROM events "
                "WHERE company_id=? ORDER BY event_date,id", (company_id,),
            )]

    def change_event(self, company_id, event_id, changes, *, actor=None):
        with self.db.transaction() as connection:
            self.require_actor(connection, company_id, actor, EDITOR_ROLES)
            if not self._company_row(connection, company_id)["calendar_enabled"]:
                raise DomainError("Календарь отключён в настройках компании.", 409)
            if changes.get("source_id") is not None:
                self._source_row(connection, company_id, changes["source_id"])
            if "event_date" in changes:
                changes["event_date"] = changes["event_date"].isoformat()
            if event_id is None:
                event_id = connection.execute(
                    "INSERT INTO events(company_id,title,event_date,description,source_id) VALUES(?,?,?,?,?)",
                    (company_id, changes["title"], changes["event_date"], changes.get("description", ""),
                     changes.get("source_id")),
                ).lastrowid
                action = "event.created"
            else:
                row = self._event_row(connection, company_id, event_id)
                changes = {field: value for field, value in changes.items() if value != row[field]}
                if not changes:
                    return dict(row)
                if "event_date" in changes:
                    changes["reminded_at"] = None
                assignments = ",".join(f"{field}=?" for field in changes)
                connection.execute(f"UPDATE events SET {assignments} WHERE company_id=? AND id=?",
                                   (*changes.values(), company_id, event_id))
                action = "event.edited"
            audit_row(connection, company_id, action, f"Обновлено событие №{event_id}.", actor)
            return dict(self._event_row(connection, company_id, event_id))

    def delete_event(self, company_id, event_id, *, actor=None):
        with self.db.transaction() as connection:
            self.require_actor(connection, company_id, actor, EDITOR_ROLES)
            if not self._company_row(connection, company_id)["calendar_enabled"]:
                raise DomainError("Календарь отключён в настройках компании.", 409)
            self._event_row(connection, company_id, event_id)
            connection.execute("DELETE FROM events WHERE company_id=? AND id=?", (company_id, event_id))
            audit_row(connection, company_id, "event.deleted", f"Удалено событие №{event_id}.", actor)

    def mark_reminded(self, company_id, event_id):
        with self.db.transaction() as connection:
            if not self._company_row(connection, company_id)["calendar_enabled"]:
                return
            connection.execute("UPDATE events SET reminded_at=? WHERE company_id=? AND id=? AND reminded_at IS NULL",
                               (utc_now(), company_id, event_id))
