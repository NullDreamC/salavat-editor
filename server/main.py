from contextlib import asynccontextmanager, suppress
from collections import deque
from datetime import date, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
import asyncio
import hashlib
import re
import secrets
import sqlite3
import time

from fastapi import APIRouter, Depends, FastAPI, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from starlette.middleware.gzip import GZipMiddleware

from .db import Database, ROOT, hash_password, verify_password
from .domain import EDITOR_ROLES, MAX_UPLOAD_SIZE, DomainError, Service, audit_row, can_review
from .webapp import public_url, WebAppHostMiddleware
from .knowledge import initialize_knowledge, register_knowledge_routes
from .social_platforms import initialize_social, register_social_routes
from .analytics import initialize_analytics, register_analytics_routes
from .inbox import initialize_inbox, register_inbox_routes, recover_inbox_sends
from .assistant_tasks import initialize_tasks, register_task_routes
from .review_policy import initialize_review_policy, register_review_policy_routes
from .reports import initialize_reports, register_report_routes
from .research import initialize_research, register_research_routes
from .strategy import initialize_strategy, register_strategy_routes
from .telegram_observation import initialize_telegram_observation, register_telegram_observation_routes
from .attribution import initialize_attribution, register_attribution_routes, is_attribution_webhook
from .billing import register_billing_routes
from . import crm


COOKIE = "salavat_session"
SESSION_TTL = 60 * 60 * 24 * 7
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
Email = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=3, max_length=254,
                                         pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")]
Password = Annotated[str, StringConstraints(min_length=10, max_length=256)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
Material = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120000)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=240)]
Secret = Annotated[str, StringConstraints(strip_whitespace=True, max_length=512)]
TelegramID = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[1-9][0-9]{0,18}$")]
Role = Literal["admin", "editor", "approver"]
Template = Literal["announcement", "person", "results"]
USER_ROLE = (
    "CASE WHEN u.is_admin=1 THEN 'super_admin' ELSE (SELECT m.role FROM memberships m "
    "JOIN companies c ON c.id=m.company_id WHERE m.user_id=u.id AND m.active=1 AND c.archived_at IS NULL "
    "ORDER BY CASE m.role WHEN 'admin' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END LIMIT 1) END AS role"
)


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Patch(Body):
    @model_validator(mode="after")
    def check_explicit_nulls(self):
        nullable = {"scheduled_at", "source_id", "telegram_id", "openai_key", "yandex_key", "telegram_token"}
        if any(getattr(self, field) is None and field not in nullable for field in self.model_fields_set):
            raise ValueError("Это поле нельзя оставить пустым.")
        return self


class Login(Body):
    email: Email
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class Setup(Login):
    name: Name
    password: Password


class PasswordChange(Body):
    current_password: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    new_password: Password


class PasswordReset(Body):
    new_password: Password


class CompanyCreate(Body):
    name: Name


class CompanyPatch(Patch):
    name: Name | None = None
    editorial_rules: Annotated[str, StringConstraints(strip_whitespace=True, max_length=12000)] | None = None
    timezone: Short | None = None
    calendar_enabled: bool | None = None
    provider: Literal["openai", "yandex"] | None = None
    model: Short | None = None
    channel_id: Short | None = None
    chat_id: Short | None = None
    bot_username: Short | None = None
    yandex_folder_id: Short | None = None
    monthly_limit: int | None = Field(None, ge=0, le=1_000_000_000)
    requests_limit: int | None = Field(None, ge=0, le=1_000_000_000)
    openai_key: Secret | None = None
    yandex_key: Secret | None = None
    telegram_token: Secret | None = None
    funding_mode: Literal["own", "salavat", "shared"] | None = None


class SharedApiPatch(Patch):
    openai_key: Secret | None = None
    yandex_key: Secret | None = None
    yandex_folder_id: Short | None = None


class UserCreate(Body):
    name: Name
    email: Email
    password: Password | None = None
    role: Role
    can_approve: bool = Field(default=False, strict=True)
    telegram_id: TelegramID | Literal[""] | int | None = None


class UserPatch(Patch):
    role: Role | None = None
    telegram_id: TelegramID | Literal[""] | int | None = None
    active: bool | None = None
    can_approve: Annotated[bool, Field(strict=True)] | None = None


class AccessPatch(Patch):
    role: Role | None = None
    active: bool | None = None


class InviteCreate(Body):
    role: Role


class SourceCreate(Body):
    title: Title
    text: Material


class PostCreate(Body):
    platform: Literal['telegram', 'vk', 'max'] = 'telegram'
    source_ids: list[int] = Field(max_length=50)
    title: Title | None = None
    kind: Short | None = None


class Version(Body):
    version: int = Field(ge=1)


class PostPatch(Patch, Version):
    platform: Literal['telegram', 'vk', 'max'] | None = None
    title: Title | None = None
    body: Annotated[str, StringConstraints(max_length=20000)] | None = None
    channel_id: Short | None = None
    scheduled_at: datetime | None = None
    template: Template | None = None
    source_ids: list[int] | None = Field(None, max_length=50)


class Answer(Version):
    text: Material


class Reject(Version):
    comment: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class EventCreate(Body):
    title: Title
    event_date: date
    description: Annotated[str, StringConstraints(max_length=8000)] = ""
    source_id: int | None = None


class EventPatch(Patch):
    title: Title | None = None
    event_date: date | None = None
    description: Annotated[str, StringConstraints(max_length=8000)] | None = None
    source_id: int | None = None


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def telegram_id(value):
    if value is None or value == "":
        return None
    value = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) >= 2**63:
        raise DomainError("Некорректный ID пользователя Telegram.", 422)
    return value


def webapp_origin(request):
    # The explicitly configured HTTPS origin is authoritative for this host.
    # localhost.run can terminate TLS without sending X-Forwarded-Proto.
    configured = request.state.webapp_url
    target = urlsplit(configured)
    if target.hostname and request.url.hostname == target.hostname and request.url.port in (None, 443):
        return configured
    return ''


def session_cookie_options(request):
    embedded = bool(webapp_origin(request))
    return {'httponly': True, 'samesite': 'none' if embedded else 'strict',
            'secure': embedded or request.url.scheme == 'https', 'path': '/api'}


def session_cookie(response, request, token):
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL, **session_cookie_options(request))


def same_origin(request):
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    actual = urlsplit(origin)
    expected = urlsplit(webapp_origin(request) or str(request.url))
    if actual.scheme == expected.scheme and actual.netloc == expected.netloc:
        return True
    local = {"127.0.0.1", "localhost"}
    return (actual.scheme == expected.scheme == "http" and actual.hostname in local
            and actual.port == 5173 and expected.hostname in local and expected.port == 8000)


def create_app(db=None, *, start_worker=False):
    db = db or Database()
    service = Service(db)

    @asynccontextmanager
    async def lifespan(app):
        db.initialize()
        initialize_knowledge(service)
        initialize_social(service)
        initialize_analytics(service)
        initialize_inbox(service)
        initialize_tasks(service)
        initialize_review_policy(service)
        initialize_reports(service)
        initialize_research(service)
        initialize_strategy(service)
        initialize_telegram_observation(service)
        initialize_attribution(service)
        crm.initialize(db)
        if start_worker:
            recover_inbox_sends(service)
        worker_task = None
        if start_worker:
            from .worker import Worker

            service.recover_publications()
            worker_task = asyncio.create_task(Worker(service).run())
        try:
            yield
        finally:
            if worker_task is not None:
                worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.webapp_url = public_url(db)
    app.add_middleware(WebAppHostMiddleware, db=db, allow_test=not start_worker)
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)
    api = APIRouter(prefix="/api")
    login_attempts = {}

    @app.middleware("http")
    async def protect_requests(request, call_next):
        request.state.webapp_url = public_url(db)
        app.state.webapp_url = request.state.webapp_url
        if app.state.webapp_url and request.url.path == '/api/auth/login' and request.method == 'POST':
            now = time.monotonic()
            address = request.client.host if request.client else 'unknown'
            for key in list(login_attempts):
                if not login_attempts[key] or login_attempts[key][-1] <= now - 60:
                    del login_attempts[key]
            attempts = login_attempts.setdefault(address, deque())
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            if len(attempts) >= 15 or len(login_attempts) > 4096:
                return JSONResponse({'detail': 'Слишком много попыток входа. Подождите минуту.'}, status_code=429,
                                    headers={'Retry-After': '60', 'Cache-Control': 'no-store'})
            attempts.append(now)
        if (request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}
                and not is_attribution_webhook(request)):
            try:
                valid_origin = same_origin(request)
            except ValueError:
                valid_origin = False
            if not valid_origin:
                return JSONResponse({"detail": "Запрос с другого сайта запрещён."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if app.state.webapp_url and request.url.hostname == urlsplit(app.state.webapp_url).hostname:
            response.headers["Content-Security-Policy"] = "frame-ancestors 'self' https://web.telegram.org https://*.web.telegram.org https://telegram.org"
        else:
            response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse({"detail": error.detail}, status_code=error.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        locations = [str(item["loc"][-1]) for item in error.errors() if item.get("loc") and item["loc"][-1] != "body"]
        suffix = f" Проверьте поля: {', '.join(dict.fromkeys(locations))}." if locations else ""
        return JSONResponse({"detail": f"Некорректные данные запроса.{suffix}"}, status_code=422)

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request, error):
        return JSONResponse({"detail": "Эти данные уже используются или связанная запись недоступна."}, status_code=409)

    def current_user(request: Request):
        token = request.cookies.get(COOKIE, "")
        if not token or len(token) > 128:
            raise DomainError("Войдите в кабинет.", 401)
        with db.read() as connection:
            row = connection.execute(
                "SELECT u.id,u.name,u.email,u.is_admin," + USER_ROLE + " "
                "FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
                (token_hash(token), int(time.time())),
            ).fetchone()
        if row is None or row["role"] is None:
            raise DomainError("Сеанс истёк или доступ отключён. Войдите снова.", 401)
        return dict(row)

    def public_user(user):
        return {key: user[key] for key in ("id", "name", "email", "role")}

    def require_global_admin(user=Depends(current_user)):
        if not user["is_admin"]:
            raise DomainError("Это действие доступно только главному администратору.", 403)
        return user

    def tenant(roles=None, *, allow_global=False, require_review=False):
        def verify(cid: int, user=Depends(current_user)):
            if user["is_admin"]:
                if not allow_global:
                    raise DomainError("Главному администратору недоступны личные данные и материалы сотрудников.", 403)
                service.company(cid)
                return user
            with db.read() as connection:
                member = connection.execute(
                    "SELECT m.role,m.can_approve FROM memberships m JOIN companies c ON c.id=m.company_id "
                    "WHERE m.company_id=? AND m.user_id=? AND m.active=1 AND c.archived_at IS NULL", (cid, user["id"]),
                ).fetchone()
            if member is None:
                raise DomainError("Компания не найдена.", 404)
            if roles is not None and member["role"] not in roles:
                raise DomainError("Недостаточно прав для этого действия.", 403)
            if require_review and not can_review(member):
                raise DomainError("Недостаточно прав для этого действия.", 403)
            return {**user, "role": member["role"], "can_approve": bool(member["can_approve"])}

        return verify

    member = tenant()
    editor = tenant(EDITOR_ROLES)
    approver = tenant(require_review=True)
    admin = tenant({"admin"})
    company_manager = tenant({"admin"}, allow_global=True)
    company_reader = tenant(allow_global=True)

    def start_session(connection, user_id):
        now = int(time.time())
        token = secrets.token_urlsafe(32)
        connection.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        connection.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
                           (token_hash(token), user_id, now + SESSION_TTL))
        return token

    def recheck_session(connection, request, user_id):
        # Password changes and revocations may occur after dependency evaluation.
        row = connection.execute(
            "SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id "
            "WHERE u.id=? AND s.token_hash=? AND s.expires_at>? AND (u.is_admin=1 OR EXISTS "
            "(SELECT 1 FROM memberships m JOIN companies c ON c.id=m.company_id "
            "WHERE m.user_id=u.id AND m.active=1 AND c.archived_at IS NULL))",
            (user_id, token_hash(request.cookies.get(COOKIE, "")), int(time.time())),
        ).fetchone()
        if row is None:
            raise DomainError("Сеанс завершён. Войдите снова.", 401)
        return row

    def replace_password(connection, user_id, encoded, action, actor):
        connection.execute("UPDATE users SET password_hash=? WHERE id=?", (encoded, user_id))
        connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        for membership in connection.execute("SELECT company_id FROM memberships WHERE user_id=?", (user_id,)):
            audit_row(connection, membership["company_id"], action,
                      f"Изменён пароль сотрудника №{user_id}. Сеансы завершены.", actor)

    def ensure_admin_remains(connection, cid, row, changes):
        if row["role"] == "admin" and row["active"] and (changes.get("active") is False or changes.get("role", "admin") != "admin"):
            other = connection.execute("SELECT 1 FROM memberships WHERE company_id=? AND user_id<>? AND role='admin' AND active=1",
                                       (cid, row["user_id"])).fetchone()
            if not other:
                raise DomainError("Сначала назначьте другого администратора компании.", 409)

    def access_record(row):
        return {"id": row["user_id"], "company_id": row["company_id"], "role": row["role"], "active": bool(row["active"])}

    def valid_invitation(connection, token):
        if len(token) > 128:
            raise DomainError("Приглашение недействительно.", 410)
        row = connection.execute("SELECT i.*,c.name AS company_name FROM invitations i JOIN companies c ON c.id=i.company_id "
                                 "WHERE i.token_hash=? AND c.archived_at IS NULL", (token_hash(token),)).fetchone()
        if not row or row["used_at"] or row["revoked"] or row["expires_at"] <= int(time.time()):
            raise DomainError("Приглашение истекло или уже использовано.", 410)
        issuer = connection.execute("SELECT 1 FROM users u WHERE u.id=? AND (u.is_admin=1 OR EXISTS "
                                    "(SELECT 1 FROM memberships m WHERE m.user_id=u.id AND m.company_id=? AND m.role='admin' AND m.active=1))",
                                    (row["created_by"], row["company_id"])).fetchone()
        if not issuer:
            raise DomainError("Приглашение отозвано.", 410)
        return row

    @api.post("/companies/{cid}/invitations", status_code=201)
    def invite(cid: int, payload: InviteCreate, request: Request, user=Depends(company_manager)):
        token = secrets.token_urlsafe(32)
        expires = int(time.time()) + 7 * 24 * 60 * 60
        with db.transaction() as connection:
            recheck_session(connection, request, user["id"])
            service.require_actor(connection, cid, user, {"admin", "super_admin"})
            iid = connection.execute("INSERT INTO invitations(company_id,token_hash,role,created_by,expires_at) VALUES(?,?,?,?,?)",
                                     (cid, token_hash(token), payload.role, user["id"], expires)).lastrowid
            audit_row(connection, cid, "invitation.created", "Создано приглашение.", user)
        return {"id": iid, "token": token, "role": payload.role, "expires_at": expires}

    @api.get("/companies/{cid}/invitations")
    def invitations(cid: int, user=Depends(company_manager)):
        with db.read() as connection:
            rows = connection.execute("SELECT id,role,expires_at,used_at,revoked FROM invitations WHERE company_id=? ORDER BY id DESC", (cid,))
            return [{"id": row["id"], "role": row["role"], "expires_at": row["expires_at"],
                     "status": "used" if row["used_at"] else "revoked" if row["revoked"] else "expired" if row["expires_at"] <= int(time.time()) else "active"} for row in rows]

    @api.delete("/companies/{cid}/invitations/{invite_id}", status_code=204)
    def revoke_invitation(cid: int, invite_id: int, request: Request, user=Depends(company_manager)):
        with db.transaction() as connection:
            recheck_session(connection, request, user["id"])
            service.require_actor(connection, cid, user, {"admin", "super_admin"})
            if not connection.execute("UPDATE invitations SET revoked=1 WHERE company_id=? AND id=?", (cid, invite_id)).rowcount:
                raise DomainError("Приглашение не найдено.", 404)
        return Response(status_code=204)

    @api.get("/invitations/{token}")
    def invitation_info(token: str):
        with db.read() as connection:
            row = valid_invitation(connection, token)
            return {"company_name": row["company_name"], "role": row["role"]}

    @api.post("/invitations/{token}/accept")
    def accept_invitation(token: str, payload: Setup, request: Request, response: Response):
        encoded = hash_password(payload.password)
        with db.transaction() as connection:
            row = valid_invitation(connection, token)
            session = connection.execute("SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token_hash=? AND s.expires_at>?",
                                         (token_hash(request.cookies.get(COOKIE, "")), int(time.time()))).fetchone()
            if session and (session["is_admin"] or session["email"].lower() != payload.email.lower()):
                raise DomainError("Откройте приглашение в отдельном браузере получателя.", 403)
            existing = connection.execute("SELECT * FROM users WHERE email=?", (payload.email,)).fetchone()
            if existing:
                if existing["is_admin"] or not verify_password(payload.password, existing["password_hash"]):
                    raise DomainError("Не удалось принять приглашение. Проверьте данные входа.", 400)
                uid, name = existing["id"], existing["name"]
                previous = connection.execute("SELECT * FROM memberships WHERE company_id=? AND user_id=?", (row["company_id"], uid)).fetchone()
                if previous:
                    ensure_admin_remains(connection, row["company_id"], previous, {"role": row["role"], "active": True})
            else:
                name = payload.name
                uid = connection.execute("INSERT INTO users(name,email,password_hash) VALUES(?,?,?)", (name, payload.email, encoded)).lastrowid
            connection.execute("INSERT INTO memberships(company_id,user_id,role) VALUES(?,?,?) ON CONFLICT(company_id,user_id) "
                               "DO UPDATE SET role=excluded.role,active=1,can_approve=CASE "
                               "WHEN memberships.role=excluded.role THEN memberships.can_approve ELSE 0 END",
                               (row["company_id"], uid, row["role"]))
            connection.execute("UPDATE invitations SET used_at=? WHERE id=?", (int(time.time()), row["id"]))
            audit_row(connection, row["company_id"], "invitation.accepted", f"Приглашение принято профилем №{uid}.", uid)
            session_token = start_session(connection, uid)
        session_cookie(response, request, session_token)
        return {"id": uid, "name": name, "email": payload.email, "role": row["role"]}

    @api.get("/companies/{cid}/access")
    def access_list(cid: int, user=Depends(require_global_admin)):
        with db.read() as connection:
            service._company_row(connection, cid)
            rows = connection.execute("SELECT m.company_id,m.user_id,m.role,m.active FROM memberships m JOIN users u ON u.id=m.user_id "
                                      "WHERE m.company_id=? AND u.is_admin=0 ORDER BY m.user_id", (cid,))
            return [access_record(row) for row in rows]

    @api.patch("/companies/{cid}/access/{user_id}")
    def access_change(cid: int, user_id: int, payload: AccessPatch, request: Request, user=Depends(require_global_admin)):
        changes = payload.model_dump(exclude_unset=True)
        with db.transaction() as connection:
            actor = recheck_session(connection, request, user["id"])
            if not actor["is_admin"]:
                raise DomainError("Недостаточно прав.", 403)
            service._company_row(connection, cid)
            row = connection.execute("SELECT m.* FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.company_id=? AND m.user_id=? AND u.is_admin=0",
                                     (cid, user_id)).fetchone()
            if row is None:
                raise DomainError("Профиль не найден.", 404)
            if changes.get("role", row["role"]) != row["role"]:
                changes["can_approve"] = False
            if changes:
                assignments = ",".join(f"{key}=?" for key in changes)
                connection.execute(f"UPDATE memberships SET {assignments} WHERE company_id=? AND user_id=?",
                                   (*changes.values(), cid, user_id))
                connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                audit_row(connection, cid, "member.access", f"Изменён доступ профиля №{user_id}.", user)
            return access_record(connection.execute("SELECT * FROM memberships WHERE company_id=? AND user_id=?", (cid, user_id)).fetchone())

    @api.get("/auth/setup")
    def setup_status():
        with db.read() as connection:
            return {"required": connection.execute("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1").fetchone() is None}

    @api.post("/auth/setup")
    def setup(payload: Setup, request: Request, response: Response):
        if not setup_status()["required"]:
            raise DomainError("Администратор уже создан.", 409)
        encoded = hash_password(payload.password)
        with db.transaction() as connection:
            if connection.execute("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1").fetchone():
                raise DomainError("Администратор уже создан.", 409)
            user_id = connection.execute("INSERT INTO users(name,email,password_hash,is_admin) VALUES(?,?,?,1)",
                                         (payload.name, payload.email, encoded)).lastrowid
            for index, name in enumerate(("Салаватстекло", "Компания 2", "Компания 3")):
                cid = connection.execute("INSERT INTO companies(name,allow_openai_fallback) VALUES(?,?)",
                                         (name, int(index == 0))).lastrowid
                audit_row(connection, cid, "company.created", "Компания создана при настройке приложения.", user_id)
            token = start_session(connection, user_id)
        session_cookie(response, request, token)
        return {"id": user_id, "name": payload.name, "email": payload.email, "role": "super_admin"}

    @api.post("/auth/login")
    def login(payload: Login, request: Request, response: Response):
        with db.read() as connection:
            user = connection.execute(
                "SELECT u.*, " + USER_ROLE + " "
                "FROM users u WHERE email=?", (payload.email,),
            ).fetchone()
        if not verify_password(payload.password, user["password_hash"] if user else None) or not user["role"]:
            raise DomainError("Неверная почта или пароль.", 401)
        with db.transaction() as connection:
            fresh = connection.execute("SELECT password_hash FROM users WHERE id=? AND (is_admin=1 OR EXISTS "
                                       "(SELECT 1 FROM memberships m JOIN companies c ON c.id=m.company_id "
                                       "WHERE m.user_id=? AND m.active=1 AND c.archived_at IS NULL))",
                                       (user["id"], user["id"])).fetchone()
            if fresh is None or fresh["password_hash"] != user["password_hash"]:
                raise DomainError("Пароль или доступ изменился. Войдите снова.", 401)
            old_token = request.cookies.get(COOKIE)
            if old_token:
                connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(old_token),))
            token = start_session(connection, user["id"])
        session_cookie(response, request, token)
        return public_user(user)

    @api.get("/auth/me")
    def me(user=Depends(current_user)):
        return public_user(user)

    @api.post("/auth/password", status_code=204)
    def change_password(payload: PasswordChange, request: Request, user=Depends(current_user)):
        if payload.current_password == payload.new_password:
            raise DomainError("Новый пароль должен отличаться от текущего.", 400)
        encoded = hash_password(payload.new_password)
        with db.transaction() as connection:
            fresh = recheck_session(connection, request, user["id"])
            if not verify_password(payload.current_password, fresh["password_hash"]):
                raise DomainError("Текущий пароль указан неверно.", 400)
            replace_password(connection, user["id"], encoded, "account.password_changed", user["id"])
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, **session_cookie_options(request))
        return response

    @api.post("/auth/logout", status_code=204)
    def logout(request: Request):
        token = request.cookies.get(COOKIE)
        if token:
            with db.transaction() as connection:
                connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, **session_cookie_options(request))
        return response

    @api.get("/companies")
    def companies(user=Depends(current_user)):
        if user["is_admin"]:
            return [{**company, "role": "super_admin"} for company in service.companies()]
        with db.read() as connection:
            rows = connection.execute(
                "SELECT c.*,m.role,m.can_approve FROM companies c JOIN memberships m ON c.id=m.company_id "
                "WHERE m.user_id=? AND m.active=1 AND c.archived_at IS NULL ORDER BY c.id", (user["id"],),
            )
            return [{**service._company_data(row), "role": row["role"], "can_approve": bool(row["can_approve"])}
                    for row in rows]

    @api.post("/companies", status_code=201)
    def add_company(payload: CompanyCreate, user=Depends(require_global_admin)):
        return {**service.create_company(payload.name, actor=user), "role": "super_admin"}

    @api.delete("/companies/{cid}", status_code=204)
    def delete_company(cid: int, user=Depends(require_global_admin)):
        service.archive_company(cid, actor=user)
        return Response(status_code=204)

    @api.get("/shared-api")
    def shared_api_settings(user=Depends(require_global_admin)):
        return service.shared_api_settings()

    @api.patch("/shared-api")
    def update_shared_api_settings(payload: SharedApiPatch, user=Depends(require_global_admin)):
        return service.update_shared_api_settings(payload.model_dump(exclude_unset=True))

    def settings_response(company, user):
        return {**company, "role": user["role"], "webapp_url": app.state.webapp_url,
                "provider_credentials_editable": service.provider_credentials_editable(company, user["role"])}

    @api.get("/companies/{cid}/settings")
    def settings(cid: int, user=Depends(company_manager)):
        return settings_response(service.company(cid, settings=True), user)

    @api.get("/companies/{cid}/models")
    async def models(cid: int, provider: Literal["openai", "yandex"], user=Depends(company_manager)):
        return await service.models(cid, provider, actor=user)

    @api.patch("/companies/{cid}")
    def update_company(cid: int, payload: CompanyPatch, user=Depends(company_manager)):
        return settings_response(service.update_company(cid, payload.model_dump(exclude_unset=True), actor=user), user)

    @api.get("/companies/{cid}/users")
    def company_users(cid: int, user=Depends(admin)):
        return service.telegram_users(cid)

    @api.post("/companies/{cid}/users", status_code=201)
    def add_user(cid: int, payload: UserCreate, user=Depends(admin)):
        tg_id = telegram_id(payload.telegram_id)
        with db.transaction() as connection:
            service.require_actor(connection, cid, user, {"admin"})
            if payload.can_approve and payload.role != "editor":
                raise DomainError("Это право можно выдать только редактору.", 422)
            existing = connection.execute("SELECT * FROM users WHERE email=?", (payload.email,)).fetchone()
            if existing:
                raise DomainError("Этот адрес уже используется. Добавьте сотрудника по приглашению.", 409)
            else:
                if payload.password is None:
                    raise DomainError("Для нового сотрудника задайте пароль из 10 символов или длиннее.", 422)
                user_id = connection.execute("INSERT INTO users(name,email,password_hash) VALUES(?,?,?)",
                                             (payload.name, payload.email, hash_password(payload.password))).lastrowid
            connection.execute("INSERT INTO memberships(company_id,user_id,role,telegram_id,can_approve) VALUES(?,?,?,?,?)",
                               (cid, user_id, payload.role, tg_id, payload.can_approve))
            audit_row(connection, cid, "member.created", f"Предоставлен доступ сотруднику №{user_id}.", user)
        return next(item for item in service.telegram_users(cid) if item["id"] == user_id)

    @api.patch("/companies/{cid}/users/{user_id}")
    def change_user(cid: int, user_id: int, payload: UserPatch, user=Depends(admin)):
        changes = payload.model_dump(exclude_unset=True)
        if "telegram_id" in changes:
            changes["telegram_id"] = telegram_id(changes["telegram_id"])
        with db.transaction() as connection:
            row = connection.execute("SELECT m.* FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.company_id=? AND m.user_id=? AND u.is_admin=0", (cid, user_id)).fetchone()
            if row is None:
                raise DomainError("Участник компании не найден.", 404)
            service.require_actor(connection, cid, user, {"admin"})
            role = changes.get("role", row["role"])
            if changes.get("can_approve") and role != "editor":
                raise DomainError("Это право можно выдать только редактору.", 422)
            if role != row["role"]:
                changes.setdefault("can_approve", False)
            ensure_admin_remains(connection, cid, row, changes)
            if changes:
                assignments = ",".join(f"{key}=?" for key in changes)
                connection.execute(f"UPDATE memberships SET {assignments} WHERE company_id=? AND user_id=?",
                                   (*changes.values(), cid, user_id))
            if changes.get("active") is False:
                connection.execute("DELETE FROM sessions WHERE user_id=? AND NOT EXISTS "
                                   "(SELECT 1 FROM memberships m JOIN companies c ON c.id=m.company_id "
                                   "WHERE m.user_id=? AND m.active=1 AND c.archived_at IS NULL)", (user_id, user_id))
            if changes:
                audit_row(connection, cid, "member.edited", f"Изменён доступ сотрудника №{user_id}.", user)
        return next(item for item in service.telegram_users(cid) if item["id"] == user_id)

    @api.post("/companies/{cid}/users/{user_id}/password", status_code=204)
    def reset_password(cid: int, user_id: int, payload: PasswordReset, request: Request, user=Depends(admin)):
        encoded = hash_password(payload.new_password)
        with db.transaction() as connection:
            actor = recheck_session(connection, request, user["id"])
            service.require_actor(connection, cid, actor["id"], {"admin"})
            target = connection.execute("SELECT u.* FROM users u JOIN memberships m ON m.user_id=u.id "
                                        "WHERE m.company_id=? AND u.id=?", (cid, user_id)).fetchone()
            if target is None:
                raise DomainError("Участник компании не найден.", 404)
            if target["is_admin"] or user_id == user["id"]:
                raise DomainError("Пароль администратора меняется через «Сменить пароль».", 403)
            if not target["password_hash"]:
                raise DomainError("У сотрудника нет входа по паролю.", 409)
            unmanaged = connection.execute(
                "SELECT 1 FROM memberships target WHERE target.user_id=? AND NOT EXISTS "
                "(SELECT 1 FROM memberships own WHERE own.user_id=? AND own.company_id=target.company_id AND own.role='admin' AND own.active=1)",
                (user_id, user["id"]),
            ).fetchone()
            if unmanaged:
                raise DomainError("У сотрудника есть доступ к другой компании. Он должен сменить пароль самостоятельно.", 409)
            replace_password(connection, user_id, encoded, "account.password_reset", user["id"])
        return Response(status_code=204)

    @api.get("/companies/{cid}/sources")
    def sources(cid: int, user=Depends(member)):
        return service.sources(cid)

    @api.post("/companies/{cid}/sources", status_code=201)
    def add_source(cid: int, payload: SourceCreate, user=Depends(editor)):
        return service.create_source(cid, payload.title, payload.text, actor=user)

    @api.post("/companies/{cid}/uploads", status_code=201)
    def upload(cid: int, file: UploadFile, user=Depends(editor)):
        try:
            content = file.file.read(MAX_UPLOAD_SIZE + 1)
            return service.store_upload(cid, file.filename or "material", content, file.content_type, actor=user)
        finally:
            file.file.close()

    @api.get("/companies/{cid}/sources/{source_id}/file")
    def source_file(cid: int, source_id: int, user=Depends(member)):
        source = service.source(cid, source_id, internal=True)
        if "file_path" not in source:
            raise DomainError("У материала нет вложения.", 404)
        preview = source["kind"] in {"image", "video"}
        return FileResponse(source["file_path"], media_type=source["mime_type"], filename=source["title"],
                            content_disposition_type="inline" if preview else "attachment")

    @api.get("/companies/{cid}/posts")
    def posts(cid: int, user=Depends(member)):
        return service.posts(cid)

    @api.post("/companies/{cid}/posts", status_code=201)
    def add_post(cid: int, payload: PostCreate, user=Depends(editor)):
        return service.create_post(cid, payload.source_ids, title=payload.title, kind=payload.kind, actor=user, platform=payload.platform)

    @api.get("/companies/{cid}/posts/{post_id}")
    def post(cid: int, post_id: int, user=Depends(member)):
        return service.post(cid, post_id)

    @api.patch("/companies/{cid}/posts/{post_id}")
    def patch_post(cid: int, post_id: int, payload: PostPatch, user=Depends(editor)):
        return service.patch_post(cid, post_id, payload.version,
                                  payload.model_dump(exclude_unset=True, exclude={"version"}), actor=user)

    @api.post("/companies/{cid}/posts/{post_id}/generate")
    async def generate(cid: int, post_id: int, payload: Version, user=Depends(editor)):
        return await service.generate(cid, post_id, payload.version, actor=user)

    @api.post("/companies/{cid}/posts/{post_id}/answer")
    async def answer(cid: int, post_id: int, payload: Answer, user=Depends(editor)):
        return await service.answer(cid, post_id, payload.version, payload.text, actor=user)

    @api.post("/companies/{cid}/posts/{post_id}/submit")
    def submit(cid: int, post_id: int, payload: Version, user=Depends(editor)):
        return service.submit(cid, post_id, payload.version, actor=user)

    @api.post("/companies/{cid}/posts/{post_id}/approve")
    def approve(cid: int, post_id: int, payload: Version, user=Depends(approver)):
        return service.approve(cid, post_id, payload.version, actor=user)

    @api.post("/companies/{cid}/posts/{post_id}/reject")
    def reject(cid: int, post_id: int, payload: Reject, user=Depends(approver)):
        return service.reject(cid, post_id, payload.version, payload.comment, actor=user)

    @api.post("/companies/{cid}/posts/{post_id}/publish")
    async def publish(cid: int, post_id: int, payload: Version, user=Depends(approver)):
        return await service.publish(cid, post_id, payload.version, actor=user)

    @api.post("/companies/{cid}/posts/{post_id}/card")
    def card(cid: int, post_id: int, payload: Version, user=Depends(editor)):
        return service.card(cid, post_id, payload.version, actor=user)

    @api.get("/companies/{cid}/events")
    def events(cid: int, user=Depends(member)):
        return service.events(cid)

    @api.post("/companies/{cid}/events", status_code=201)
    def create_event(cid: int, payload: EventCreate, user=Depends(editor)):
        return service.change_event(cid, None, payload.model_dump(), actor=user)

    @api.patch("/companies/{cid}/events/{event_id}")
    def patch_event(cid: int, event_id: int, payload: EventPatch, user=Depends(editor)):
        return service.change_event(cid, event_id, payload.model_dump(exclude_unset=True), actor=user)

    @api.delete("/companies/{cid}/events/{event_id}", status_code=204)
    def delete_event(cid: int, event_id: int, user=Depends(editor)):
        service.delete_event(cid, event_id, actor=user)
        return Response(status_code=204)

    @api.get("/companies/{cid}/activity")
    def activity(cid: int, user=Depends(member)):
        return service.activity(cid)

    @api.get("/companies/{cid}/usage")
    def usage(cid: int, user=Depends(company_reader)):
        return service.usage(cid)

    from .brand_images import register_brand_routes
    from .photo_workflow import register_photo_routes
    from .logo_workflow import register_logo_routes
    from .telegram_link import register_telegram_link_routes

    register_brand_routes(api, service, company_manager)
    register_photo_routes(api, service, editor)
    register_logo_routes(api, service, member)
    register_telegram_link_routes(api, service, member)
    register_knowledge_routes(api, service, member, editor)
    register_social_routes(api, service, company_reader, company_manager)
    register_analytics_routes(api, service, member, editor)
    register_inbox_routes(api, service, member, editor)
    register_task_routes(api, service, member, editor)
    register_review_policy_routes(api, service, company_manager, approver)
    register_report_routes(api, service, member, editor)
    register_research_routes(api, service, member, editor)
    register_strategy_routes(api, service, member, editor)
    register_telegram_observation_routes(api, service, member, editor)
    register_attribution_routes(api, service, member, editor, admin)
    register_billing_routes(api, service, company_manager, require_global_admin)
    crm.register_crm_routes(api, service, admin)
    app.include_router(api)

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise DomainError("Маршрут API не найден.", 404)
        dist = (ROOT / "web" / "dist").resolve()
        if path.startswith(("assets/", "fonts/")) or path == "favicon.svg":
            asset = (dist / path).resolve()
            if dist in asset.parents and asset.is_file():
                response = FileResponse(asset)
                # Keep small bundles in one body so gzip can include Content-Length.
                # This also avoids stalled chunked transfers on the test tunnel.
                response.chunk_size = 1024 * 1024
                return response
            raise DomainError("Файл не найден.", 404)
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise DomainError("Соберите веб-кабинет перед запуском приложения.", 503)

    return app


app = create_app(start_worker=True)
