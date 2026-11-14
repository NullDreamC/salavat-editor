"""Company reviewer assignments and manual publication reconciliation."""

import re
from typing import Annotated, Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

from .domain import APPROVER_ROLES, DomainError, Service, audit_row
from .social_platforms import SocialPublicationError, _max_message_id, _vk_post_id


class ReviewPolicyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    reviewer_ids: list[Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]]


class Reconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    version: int = Field(ge=1)
    outcome: Literal["published", "not_published"]
    message_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)] | None = None
    publication_url: HttpUrl | None = None
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] = ""


def initialize_review_policy(service):
    with service.db.transaction() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS company_reviewers (
                company_id INTEGER NOT NULL REFERENCES companies(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                PRIMARY KEY(company_id,user_id)
            )
        """)


def configured_reviewer_ids(connection, company_id):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_reviewers'"
    ).fetchone():
        return []
    return [row["user_id"] for row in connection.execute(
        "SELECT user_id FROM company_reviewers WHERE company_id=? ORDER BY user_id", (company_id,),
    )]


def _eligible_reviewers(connection, company_id):
    return [dict(row, active=True) for row in connection.execute(
        "SELECT m.user_id AS id,m.role FROM memberships m JOIN users u ON u.id=m.user_id "
        "JOIN companies c ON c.id=m.company_id WHERE m.company_id=? AND c.archived_at IS NULL "
        "AND m.active=1 AND u.is_admin=0 AND m.role IN ('admin','approver') ORDER BY m.user_id",
        (company_id,),
    )]


def reviewer_ids(connection, company_id):
    assigned = set(configured_reviewer_ids(connection, company_id))
    return [row["id"] for row in _eligible_reviewers(connection, company_id)
            if not assigned or row["id"] in assigned]


def require_reviewer(connection, company_id, actor):
    Service.require_actor(connection, company_id, actor, APPROVER_ROLES)
    if actor is None:
        return
    assigned = configured_reviewer_ids(connection, company_id)
    actor_id = actor["id"] if isinstance(actor, dict) else actor
    if assigned and actor_id not in assigned:
        raise DomainError("Это действие доступно назначенным согласующим компании.", 403)


def _publication_identity(row, payload):
    message_id = payload.message_id
    if not message_id:
        raise DomainError("Укажите ID опубликованного сообщения.", 422)
    try:
        if row["platform"] == "vk":
            _vk_post_id(message_id, row["channel_id"])
        elif row["platform"] == "max":
            _max_message_id(message_id)
        elif not re.fullmatch(r"[1-9][0-9]{0,18}", message_id):
            raise DomainError("Укажите числовой ID сообщения Telegram.", 422)
    except SocialPublicationError as error:
        raise DomainError(str(error), 422) from error
    url = payload.publication_url
    if url and (url.username is not None or url.password is not None):
        raise DomainError("Укажите ссылку на публикацию без данных для входа.", 422)
    return message_id, str(url) if url else None


def reconcile_publication(service, company_id, post_id, payload, actor):
    with service.db.transaction() as connection:
        require_reviewer(connection, company_id, actor)
        row = service._post_row(connection, company_id, post_id)
        service._check_version(row, payload.version)
        if row["status"] != "failed" or not row["publication_uncertain"]:
            raise DomainError("Ручная проверка доступна при неизвестном результате публикации.", 409)

        if payload.outcome == "published":
            message_id, publication_url = _publication_identity(row, payload)
            changes = dict(status="published", message_id=message_id, publication_url=publication_url)
            detail = f"Публикация поста №{post_id} подтверждена вручную."
        else:
            changes = dict(status="draft", message_id=None, publication_url=None, approved_snapshot=None)
            detail = f"Пост №{post_id} не найден на площадке и возвращён в черновики."
        if payload.note:
            detail += f" {payload.note}"
        audit_row(connection, company_id, f"post.reconciled_{payload.outcome}", detail, actor)
        return service._update_post(connection, company_id, post_id, **changes,
                                    publication_uncertain=0, feedback=None)


def register_review_policy_routes(api, service, member, company_manager, approver):
    @api.get("/companies/{cid}/review-policy")
    def policy(cid: int, user=Depends(company_manager)):
        with service.db.read() as connection:
            service.require_actor(connection, cid, user, {"admin", "super_admin"})
            return {"reviewer_ids": configured_reviewer_ids(connection, cid),
                    "options": _eligible_reviewers(connection, cid)}

    @api.patch("/companies/{cid}/review-policy")
    def update_policy(cid: int, payload: ReviewPolicyPatch, user=Depends(company_manager)):
        with service.db.transaction() as connection:
            service.require_actor(connection, cid, user, {"admin", "super_admin"})
            options = _eligible_reviewers(connection, cid)
            assigned = sorted(set(payload.reviewer_ids))
            if not set(assigned).issubset(row["id"] for row in options):
                raise DomainError("Можно назначить только действующих администраторов и согласующих компании.", 422)
            if assigned != configured_reviewer_ids(connection, cid):
                connection.execute("DELETE FROM company_reviewers WHERE company_id=?", (cid,))
                connection.executemany("INSERT INTO company_reviewers(company_id,user_id) VALUES(?,?)",
                                       ((cid, reviewer_id) for reviewer_id in assigned))
                detail = ("Обновлён список назначенных согласующих." if assigned
                          else "Согласование открыто всем администраторам и согласующим компании.")
                audit_row(connection, cid, "company.review_policy_updated", detail, user)
            return {"reviewer_ids": assigned, "options": options}

    @api.post("/companies/{cid}/posts/{post_id}/reconcile")
    def reconcile(cid: int, post_id: int, payload: Reconciliation, user=Depends(approver)):
        return reconcile_publication(service, cid, post_id, payload, user)
