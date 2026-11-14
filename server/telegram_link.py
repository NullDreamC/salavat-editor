from datetime import datetime, timezone
import hashlib
import re
import secrets
import time

from fastapi import Depends

from .domain import APPROVER_ROLES, EDITOR_ROLES, DomainError, audit_row
from .integrations import TelegramError, telegram_call


TTL_SECONDS = 10 * 60
ROLES = EDITOR_ROLES | APPROVER_ROLES
CODE = re.compile(r"link_[A-Za-z0-9_-]{32}")


def _prepare(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS telegram_links (
        company_id INTEGER NOT NULL REFERENCES companies(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        token_hash TEXT NOT NULL UNIQUE,
        bot_id TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        PRIMARY KEY(company_id,user_id)
    )""")


def _hash(code):
    return hashlib.sha256(code.encode()).hexdigest()


def _membership(service, connection, company_id, actor):
    if actor.get("is_admin"):
        raise DomainError("Привязка доступна в учётной записи компании.", 403)
    service._company_row(connection, company_id)
    service.require_actor(connection, company_id, actor, ROLES)
    return connection.execute(
        "SELECT telegram_id FROM memberships WHERE company_id=? AND user_id=?",
        (company_id, actor["id"]),
    ).fetchone()


def _status(service, company_id, actor):
    with service.db.read() as connection:
        member = _membership(service, connection, company_id, actor)
        company = service._company_row(connection, company_id)
        return {"linked": bool(member["telegram_id"]), "configured": bool(company["telegram_token"])}


async def issue_link(service, company_id, actor):
    status = _status(service, company_id, actor)
    if not status["configured"]:
        raise DomainError("Сначала подключите бота в настройках компании.", 422)
    if status["linked"]:
        raise DomainError("Telegram уже привязан к этой учётной записи.", 409)
    token = service.credentials(company_id)["telegram_token"]
    try:
        identity = await telegram_call(token, "getMe")
        username = identity["username"]
        bot_id = identity["id"]
        if type(bot_id) is not int or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            raise ValueError
    except (TelegramError, TypeError, KeyError, ValueError) as error:
        raise DomainError("Не удалось проверить бота Telegram. Проверьте настройки компании.", 502) from error
    code = "link_" + secrets.token_urlsafe(24)
    expires = int(time.time()) + TTL_SECONDS
    with service.db.transaction() as connection:
        member = _membership(service, connection, company_id, actor)
        company = service._company_row(connection, company_id)
        if member["telegram_id"] or service._credentials(company)["telegram_token"] != token:
            raise DomainError("Привязка изменилась. Обновите страницу и попробуйте снова.", 409)
        _prepare(connection)
        connection.execute("DELETE FROM telegram_links WHERE expires_at<=?", (int(time.time()),))
        connection.execute(
            "INSERT OR REPLACE INTO telegram_links VALUES (?,?,?,?,?)",
            (company_id, actor["id"], _hash(code), str(bot_id), expires),
        )
    return {
        "command": f"/start {code}", "bot_username": username,
        "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
        "url": f"https://t.me/{username}?start={code}",
    }


def protect_link_update(update):
    message = update.get("message") or {}
    text = message.get("text") or ""
    parts = text.split()
    if len(parts) != 2 or parts[0].split("@")[0] != "/start" or not CODE.fullmatch(parts[1]):
        return update
    protected = {**update, "message": {**message, "text": parts[0]}}
    if message.get("chat", {}).get("type") == "private":
        protected["_link_hash"] = _hash(parts[1])
    return protected


def claim_link(service, company_id, bot_id, code, telegram_id, *, code_hash=None, bot_token=None):
    if type(telegram_id) is not int or telegram_id <= 0:
        return False
    if code_hash is None:
        if not CODE.fullmatch(code):
            return False
        code_hash = _hash(code)
    with service.db.transaction() as connection:
        company = service._company_row(connection, company_id)
        if bot_token is not None and service._credentials(company, {"telegram_token"})["telegram_token"] != bot_token:
            return False
        _prepare(connection)
        link = connection.execute(
            "SELECT l.user_id,m.telegram_id FROM telegram_links l "
            "JOIN memberships m ON m.user_id=l.user_id AND m.company_id=l.company_id "
            "JOIN users u ON u.id=l.user_id WHERE l.company_id=? AND l.bot_id=? AND l.token_hash=? "
            "AND l.expires_at>? AND m.active=1 AND u.is_admin=0 AND u.password_hash IS NOT NULL",
            (company_id, str(bot_id), code_hash, int(time.time())),
        ).fetchone()
        if not link or link["telegram_id"]:
            return False
        if connection.execute(
            "SELECT 1 FROM memberships WHERE company_id=? AND telegram_id=?", (company_id, str(telegram_id)),
        ).fetchone():
            return False
        connection.execute("UPDATE memberships SET telegram_id=? WHERE company_id=? AND user_id=?",
                           (str(telegram_id), company_id, link["user_id"]))
        connection.execute("DELETE FROM telegram_links WHERE company_id=? AND user_id=?", (company_id, link["user_id"]))
        audit_row(connection, company_id, "member.telegram_linked", "Сотрудник привязал свой Telegram.", link["user_id"])
    return True


def register_telegram_link_routes(api, service, member):
    @api.get("/companies/{cid}/telegram-link")
    def telegram_link_status(cid: int, user=Depends(member)):
        return _status(service, cid, user)

    @api.post("/companies/{cid}/telegram-link")
    async def telegram_link(cid: int, user=Depends(member)):
        return await issue_link(service, cid, user)
