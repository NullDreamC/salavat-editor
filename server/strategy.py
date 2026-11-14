"""Versioned editorial experiments grounded in comparable company observations."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from .assistant_tasks import PlanCreate, _save_plan
from .domain import EDITOR_ROLES, DomainError, audit_row, pack
from .inbox import _publication_format


MEMBER_ROLES = EDITOR_ROLES | {"approver"}
WINDOW_DAYS = 60
AGE_MIN = 6
AGE_MAX = 8
TARGET_AGE = 7
MIN_PUBLICATIONS = 3
MIN_COVERAGE = 0.75
MIN_LIFT = 0.20
RECENT_DAYS = 30
MAX_POSTS = 500
MAX_GROUP_OBSERVATIONS = 30
MAX_RECOMMENDATIONS = 3
FORMATS = {"Текст": "post", "Фото": "post", "Видео": "video", "Фото и видео": "post"}
INSUFFICIENT = (
    "Для эксперимента нужны два формата на одном канале. Для каждого нужны минимум три публикации, "
    "просмотры через 6–8 дней после выхода у 75% публикаций и хотя бы один замер за последние 30 дней. "
    "Разница медиан должна составлять не меньше 20%."
)


class ApplyVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    version: int = Field(gt=0)


def initialize_strategy(service):
    with service.db.transaction() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS strategy_assessments (
            company_id INTEGER PRIMARY KEY REFERENCES companies(id),
            local_day TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS strategy_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(id),
            version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL CHECK(status IN ('proposed','superseded','applied')),
            platform TEXT NOT NULL,
            destination TEXT NOT NULL,
            preferred_format TEXT NOT NULL,
            comparison_format TEXT NOT NULL,
            evidence_key TEXT NOT NULL,
            evidence TEXT NOT NULL,
            planned_on TEXT NOT NULL,
            title TEXT NOT NULL,
            brief TEXT NOT NULL,
            plan_id INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(company_id, evidence_key)
        )""")
        connection.execute("CREATE INDEX IF NOT EXISTS strategy_company_status "
                           "ON strategy_recommendations(company_id,status,id)")


def _available(connection):
    return connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('analytics_metrics','content_plan')"
    ).fetchone()[0] == 2


def _clock(company, now=None):
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    zone = ZoneInfo(company["timezone"])
    today = moment.astimezone(zone).date()
    return moment, zone, today


def _utc_start(day, zone):
    return datetime.combine(day, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc).isoformat(timespec="seconds")


def _cohort(connection, cid, start, end, today, zone):
    posts = connection.execute(
        """SELECT p.id,p.platform,p.channel_id,p.message_id,p.approved_snapshot,a.created_at AS published_at
           FROM activity a JOIN posts p ON a.detail='Опубликован пост №'||p.id||'.'
           WHERE a.company_id=? AND p.company_id=? AND a.action='post.published' AND p.status='published'
           AND a.created_at>=? AND a.created_at<?
           AND p.message_id IS NOT NULL AND p.channel_id IS NOT NULL
           ORDER BY a.created_at DESC,p.id DESC LIMIT ?""",
        (cid, cid, _utc_start(start, zone), _utc_start(end + timedelta(days=1), zone), MAX_POSTS + 1),
    ).fetchall()
    if len(posts) > MAX_POSTS:
        return None
    published = {}
    for row in posts:
        format_ = _publication_format(row["approved_snapshot"])
        if format_ in FORMATS:
            local_date = datetime.fromisoformat(row["published_at"]).astimezone(zone).date()
            published.setdefault(row["id"], (row["platform"], row["channel_id"], format_, local_date.isoformat(),
                                            row["message_id"]))
    if not published:
        return {}
    measured = connection.execute(
        """WITH ranked AS (
               SELECT id,post_id,platform,publication_id,published_on,observed_on,views,source,
               ROW_NUMBER() OVER (PARTITION BY post_id ORDER BY
                 ABS(JULIANDAY(observed_on)-JULIANDAY(published_on)-?),observed_on DESC,recorded_at DESC,id DESC) n
               FROM analytics_metrics
               WHERE company_id=? AND published_on BETWEEN ? AND ? AND observed_on<=?
                 AND views IS NOT NULL AND JULIANDAY(observed_on)-JULIANDAY(published_on) BETWEEN ? AND ?
                 AND post_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
           ) SELECT id,post_id,platform,publication_id,published_on,observed_on,views,source FROM ranked WHERE n=1""",
        (TARGET_AGE, cid, start.isoformat(), end.isoformat(), today.isoformat(), AGE_MIN, AGE_MAX,
         pack(list(published))),
    ).fetchall()
    samples = {row["post_id"]: row for row in measured}
    groups = {}
    for post_id, (platform, destination, format_, day, external_id) in published.items():
        sample = samples.get(post_id)
        group = groups.setdefault((platform, destination), {}).setdefault(format_, {"count": 0, "samples": []})
        group["count"] += 1
        if sample is None:
            continue
        identity = f"{destination}:{external_id}" if platform == "telegram" else external_id
        if sample["platform"] == platform and sample["published_on"] == day and sample["publication_id"] == identity:
            group["samples"].append({"metric_id": sample["id"], "publication_id": sample["publication_id"],
                                     "published_on": day, "observed_on": sample["observed_on"], "views": sample["views"],
                                     "source": sample["source"]})
    return groups


def _proposals(connection, cid, start, end, today, zone):
    cohort = _cohort(connection, cid, start, end, today, zone)
    if cohort is None:
        return [], "Период содержит более 500 публикаций. Для этой выборки автоматический эксперимент недоступен."
    recent = (today - timedelta(days=RECENT_DAYS)).isoformat()
    results = []
    for (platform, destination), formats in sorted(cohort.items()):
        ranked = []
        for format_, group in sorted(formats.items()):
            samples = group["samples"]
            count = group["count"]
            if len(samples) < MIN_PUBLICATIONS or len(samples) < MIN_COVERAGE * count:
                continue
            # Compare equally bounded recent samples and preserve their exact observation references.
            ordered = sorted(samples, key=lambda item: (item["published_on"], item["metric_id"]), reverse=True)
            comparison = ordered[:MAX_GROUP_OBSERVATIONS]
            if not any(item["observed_on"] >= recent for item in comparison):
                continue
            ranked.append({"format": format_, "publications": count, "views_coverage": len(samples),
                           "median_views": median(item["views"] for item in comparison), "observations": comparison})
        ranked.sort(key=lambda item: (-item["median_views"], item["format"]))
        if len(ranked) < 2:
            continue
        first, second = ranked[:2]
        difference = first["median_views"] - second["median_views"]
        if difference <= 0 or difference < second["median_views"] * MIN_LIFT:
            continue
        evidence = {"metric": "views", "window_start": start.isoformat(), "window_end": end.isoformat(),
                    "age_days": {"min": AGE_MIN, "max": AGE_MAX}, "groups": [first, second]}
        evidence_key = hashlib.sha256(pack([platform, destination, first, second]).encode()).hexdigest()
        platform_name = {"telegram": "Telegram", "vk": "VK", "max": "MAX"}.get(platform, platform)
        title = f"Эксперимент формата «{first['format']}» в {platform_name}"
        brief = (f"Для канала {destination} проверьте формат «{first['format']}» на сопоставимой теме и дне недели. "
                 f"В выборке медиана просмотров через 6–8 дней: {first['median_views']:g} по "
                 f"{len(first['observations'])} публикациям этого формата и {second['median_views']:g} по "
                 f"{len(second['observations'])} публикациям формата «{second['format']}». "
                 "Зафиксируйте просмотры через семь дней после выхода и сравните замеры. "
                 "Тему и материалы согласуйте с редактором.")
        results.append((platform, destination, first["format"], second["format"], evidence_key, pack(evidence),
                        (today + timedelta(days=7)).isoformat(), title, brief))
    return results[:MAX_RECOMMENDATIONS], INSUFFICIENT


def _dates(today):
    return today - timedelta(days=WINDOW_DAYS - 1), today - timedelta(days=AGE_MAX)


def refresh_strategy(service, cid, actor=None, *, now=None):
    if actor is None:
        with service.db.read() as connection:
            company = service._company_row(connection, cid)
            _, _, today = _clock(company, now)
            done = connection.execute("SELECT 1 FROM strategy_assessments WHERE company_id=? AND local_day=?",
                                      (cid, today.isoformat())).fetchone()
            if done or not _available(connection):
                return
    with service.db.transaction() as connection:
        service.require_actor(connection, cid, actor, EDITOR_ROLES)
        company = service._company_row(connection, cid)
        moment, zone, today = _clock(company, now)
        prior = connection.execute("SELECT local_day FROM strategy_assessments WHERE company_id=?", (cid,)).fetchone()
        if actor is None and prior and prior["local_day"] == today.isoformat():
            return
        if not _available(connection):
            return
        start, end = _dates(today)
        proposals, insufficient = _proposals(connection, cid, start, end, today, zone)
        evidence_keys = [proposal[4] for proposal in proposals]
        pending = connection.execute(
            "SELECT id,evidence_key,planned_on FROM strategy_recommendations WHERE company_id=? AND status='proposed'",
            (cid,),
        ).fetchall()
        for row in pending:
            if row["evidence_key"] not in evidence_keys or row["planned_on"] <= today.isoformat():
                connection.execute(
                    "UPDATE strategy_recommendations SET status='superseded',version=version+1 WHERE id=?", (row["id"],),
                )
        for proposal in proposals:
            connection.execute(
                """INSERT INTO strategy_recommendations(company_id,status,platform,destination,preferred_format,
                   comparison_format,evidence_key,evidence,planned_on,title,brief,created_at)
                   VALUES(?,'proposed',?,?,?,?,?,?,?,?,?,?) ON CONFLICT(company_id,evidence_key) DO NOTHING""",
                (cid, *proposal, moment.isoformat()),
            )
        pending_count = connection.execute(
            "SELECT COUNT(*) FROM strategy_recommendations WHERE company_id=? AND status='proposed'", (cid,),
        ).fetchone()[0]
        status = "recommendations" if pending_count or proposals else "insufficient"
        reason = insufficient
        if status == "recommendations":
            reason = ("Доступны эксперименты по сопоставимым замерам просмотров." if pending_count else
                      "Эта выборка уже использована. Новые предложения появятся после обновления замеров.")
        connection.execute(
            """INSERT INTO strategy_assessments(company_id,local_day,status,reason,checked_at,window_start,window_end)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET local_day=excluded.local_day,
               status=excluded.status,reason=excluded.reason,checked_at=excluded.checked_at,
               window_start=excluded.window_start,window_end=excluded.window_end""",
            (cid, today.isoformat(), status, reason, moment.isoformat(), start.isoformat(), end.isoformat()),
        )


def apply_strategy(service, cid, recommendation_id, version, actor, *, now=None):
    with service.db.transaction() as connection:
        service.require_actor(connection, cid, actor, EDITOR_ROLES)
        company = service._company_row(connection, cid)
        _, zone, today = _clock(company, now)
        row = connection.execute(
            "SELECT * FROM strategy_recommendations WHERE company_id=? AND id=?", (cid, recommendation_id),
        ).fetchone()
        if row is None:
            raise DomainError("Рекомендация не найдена.", 404)
        if row["status"] != "proposed" or row["version"] != version or row["planned_on"] <= today.isoformat():
            raise DomainError("Рекомендация изменилась или устарела. Обновите стратегию.", 409)
        if not _available(connection):
            raise DomainError("Контент-план ещё недоступен.", 503)
        start, end = _dates(today)
        proposals, _ = _proposals(connection, cid, start, end, today, zone)
        if row["evidence_key"] not in {proposal[4] for proposal in proposals}:
            raise DomainError("Замеры для рекомендации изменились. Обновите стратегию.", 409)
        payload = PlanCreate(title=row["title"], planned_on=row["planned_on"], brief=row["brief"],
                             format=FORMATS[row["preferred_format"]])
        plan = _save_plan(connection, cid, payload)
        connection.execute(
            "UPDATE strategy_recommendations SET status='applied',plan_id=?,version=version+1 WHERE id=?",
            (plan["id"], recommendation_id),
        )
        audit_row(connection, cid, "strategy.applied",
                  f"По рекомендации №{recommendation_id} добавлена позиция плана №{plan['id']}.", actor)


def strategy_overview(service, cid, actor):
    with service.db.read() as connection:
        service.require_actor(connection, cid, actor, MEMBER_ROLES)
        assessment = connection.execute(
            "SELECT status,reason,checked_at,window_start,window_end FROM strategy_assessments WHERE company_id=?",
            (cid,),
        ).fetchone()
        rows = connection.execute(
            """SELECT id,version,status,platform,preferred_format,comparison_format,planned_on,title,brief,
               evidence,plan_id,created_at FROM strategy_recommendations WHERE company_id=?
               ORDER BY CASE status WHEN 'proposed' THEN 0 WHEN 'applied' THEN 1 ELSE 2 END,id DESC LIMIT 30""",
            (cid,),
        )
        recommendations = [{**dict(row), "evidence": json.loads(row["evidence"])} for row in rows]
    empty = {"status": "insufficient", "reason": "Стратегия ещё не рассчитана.", "checked_at": None,
             "window_start": None, "window_end": None}
    return {"assessment": dict(assessment) if assessment else empty, "recommendations": recommendations}


def register_strategy_routes(api, service, member, editor):
    base = "/companies/{cid}/strategy"

    @api.get(base)
    def overview(cid: int, user=Depends(member)):
        return strategy_overview(service, cid, user)

    @api.post(base + "/refresh")
    def refresh(cid: int, user=Depends(editor)):
        refresh_strategy(service, cid, user)
        return strategy_overview(service, cid, user)

    @api.post(base + "/{recommendation_id}/apply")
    def apply(cid: int, recommendation_id: int, payload: ApplyVersion, user=Depends(editor)):
        apply_strategy(service, cid, recommendation_id, payload.version, user)
        return strategy_overview(service, cid, user)
