from contextlib import contextmanager
from pathlib import Path
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import os
import secrets
import sqlite3

from .billing import SCHEMA as BILLING_SCHEMA, migrate_schema as migrate_billing_schema


ROOT = Path(__file__).resolve().parent.parent
PASSWORD_ITERATIONS = 600_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    editorial_rules TEXT NOT NULL DEFAULT '',
    archived_at TEXT,
    funding_mode TEXT NOT NULL DEFAULT 'own' CHECK(funding_mode IN ('own','salavat','shared')),
    timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
    calendar_enabled INTEGER NOT NULL DEFAULT 1,
    provider TEXT NOT NULL DEFAULT 'openai',
    model TEXT NOT NULL DEFAULT 'gpt-4.1-mini',
    channel_id TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    bot_username TEXT NOT NULL DEFAULT '',
    yandex_folder_id TEXT NOT NULL DEFAULT '',
    monthly_limit INTEGER NOT NULL DEFAULT 40000,
    requests_limit INTEGER NOT NULL DEFAULT 0,
    allow_openai_fallback INTEGER NOT NULL DEFAULT 0,
    openai_key BLOB,
    yandex_key BLOB,
    telegram_token BLOB
);
CREATE TABLE IF NOT EXISTS shared_api_settings (
    id INTEGER PRIMARY KEY CHECK(id=1),
    openai_key BLOB,
    yandex_key BLOB,
    yandex_folder_id TEXT NOT NULL DEFAULT ''
);
INSERT OR IGNORE INTO shared_api_settings(id) VALUES(1);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memberships (
    company_id INTEGER NOT NULL REFERENCES companies(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK(role IN ('admin','editor','approver')),
    can_approve INTEGER NOT NULL DEFAULT 0 CHECK(can_approve=0 OR (can_approve=1 AND role='editor')),
    telegram_id TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(company_id,user_id),
    UNIQUE(company_id,telegram_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invitations (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    token_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK(role IN ('admin','editor','approver')),
    created_by INTEGER NOT NULL REFERENCES users(id),
    expires_at INTEGER NOT NULL,
    used_at INTEGER,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    file_path TEXT,
    mime_type TEXT
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    version INTEGER NOT NULL DEFAULT 1,
    source_ids TEXT NOT NULL,
    questions TEXT NOT NULL DEFAULT '[]',
    facts TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    scheduled_at TEXT,
    channel_id TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'telegram',
    created_by INTEGER REFERENCES users(id),
    template TEXT NOT NULL,
    card_id INTEGER REFERENCES sources(id),
    photo_id INTEGER REFERENCES sources(id),
    photo_base_id INTEGER REFERENCES sources(id),
    logo_placement TEXT,
    publication_url TEXT,
    message_id TEXT,
    telegram_chat_id TEXT,
    feedback TEXT,
    publication_uncertain INTEGER NOT NULL DEFAULT 0,
    approved_snapshot TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    title TEXT NOT NULL,
    event_date TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source_id INTEGER REFERENCES sources(id),
    reminded_at TEXT
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    actor_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    month TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS post_generation_claims (
    company_id INTEGER NOT NULL REFERENCES companies(id),
    post_id INTEGER NOT NULL REFERENCES posts(id),
    version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    PRIMARY KEY(company_id,post_id,version)
);
CREATE INDEX IF NOT EXISTS sources_company ON sources(company_id,id);
CREATE INDEX IF NOT EXISTS posts_company ON posts(company_id,id);
CREATE INDEX IF NOT EXISTS posts_scheduled ON posts(status,scheduled_at);
CREATE INDEX IF NOT EXISTS events_company ON events(company_id,event_date);
CREATE INDEX IF NOT EXISTS activity_company ON activity(company_id,id);
CREATE INDEX IF NOT EXISTS usage_company ON usage(company_id,month);
"""


class Database:
    def __init__(self, path=None, uploads_dir=None):
        data_dir = Path(os.environ.get("SALAVAT_DATA_DIR", ROOT / ".runtime"))
        self.path = Path(path or data_dir / "salavat.sqlite3").resolve()
        self.uploads_dir = Path(uploads_dir or self.path.parent / "uploads").resolve()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.uploads_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.read() as connection:
            connection.executescript(SCHEMA + BILLING_SCHEMA)
        with self.transaction() as connection:
            migrate_billing_schema(connection)
            columns = {column["name"] for column in connection.execute("PRAGMA table_info(companies)")}
            if "editorial_rules" not in columns:
                connection.execute("ALTER TABLE companies ADD COLUMN editorial_rules TEXT NOT NULL DEFAULT ''")
            if "archived_at" not in columns:
                connection.execute("ALTER TABLE companies ADD COLUMN archived_at TEXT")
            if "funding_mode" not in columns:
                connection.execute("ALTER TABLE companies ADD COLUMN funding_mode TEXT NOT NULL DEFAULT 'own' "
                                   "CHECK(funding_mode IN ('own','salavat','shared'))")
            membership_columns = {column["name"] for column in connection.execute("PRAGMA table_info(memberships)")}
            if "can_approve" not in membership_columns:
                connection.execute("ALTER TABLE memberships ADD COLUMN can_approve INTEGER NOT NULL DEFAULT 0 "
                                   "CHECK(can_approve=0 OR (can_approve=1 AND role='editor'))")
            post_columns = {column["name"] for column in connection.execute("PRAGMA table_info(posts)")}
            if "telegram_chat_id" not in post_columns:
                connection.execute("ALTER TABLE posts ADD COLUMN telegram_chat_id TEXT")
            if "platform" not in post_columns:
                connection.execute("ALTER TABLE posts ADD COLUMN platform TEXT NOT NULL DEFAULT 'telegram'")
            if "created_by" not in post_columns:
                connection.execute("ALTER TABLE posts ADD COLUMN created_by INTEGER REFERENCES users(id)")
                connection.execute(
                    "UPDATE posts SET created_by=(SELECT actor_id FROM activity a WHERE a.company_id=posts.company_id "
                    "AND a.action='post.created' AND a.detail='Создан пост №'||posts.id||'.' ORDER BY a.id LIMIT 1)"
                )
            if "photo_id" not in post_columns:
                connection.execute("ALTER TABLE posts ADD COLUMN photo_id INTEGER REFERENCES sources(id)")
            if "photo_base_id" not in post_columns:
                connection.execute("ALTER TABLE posts ADD COLUMN photo_base_id INTEGER REFERENCES sources(id)")
            if "logo_placement" not in post_columns:
                connection.execute("ALTER TABLE posts ADD COLUMN logo_placement TEXT")
            if not connection.execute("SELECT 1 FROM app_migrations WHERE name='separate_global_admin'").fetchone():
                connection.execute("DELETE FROM memberships WHERE user_id IN (SELECT id FROM users WHERE is_admin=1)")
                connection.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE is_admin=1)")
                connection.execute("INSERT INTO app_migrations(name) VALUES('separate_global_admin')")
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def read(self):
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.read() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"{PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, encoded):
    if not encoded:
        # Spend the same work on unknown accounts as on failed password attempts.
        hashlib.pbkdf2_hmac("sha256", password.encode(), bytes(16), PASSWORD_ITERATIONS)
        return False
    rounds, salt, expected = encoded.split("$")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
    return hmac.compare_digest(digest.hex(), expected)


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data, protect):
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _Blob(len(data), buffer)
    result = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise OSError("Не удалось открыть хранилище секретов Windows.")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        release = ctypes.WinDLL("kernel32").LocalFree
        release.argtypes = [ctypes.c_void_p]
        release.restype = ctypes.c_void_p
        release(result.data)


def store_secret(value):
    if not value:
        return None
    data = value.encode()
    return _dpapi(data, True) if os.name == "nt" else data


def reveal_secret(value):
    if not value:
        return ""
    data = _dpapi(value, False) if os.name == "nt" else value
    return data.decode()
