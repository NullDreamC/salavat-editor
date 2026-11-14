import csv
import io
import json
from datetime import date, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from . import integrations
from .domain import APPROVER_ROLES, EDITOR_ROLES, DomainError, audit_row, utc_now


COUNT_FIELDS = ("views", "reach", "reactions", "comments", "shares")
MEMBER_ROLES = EDITOR_ROLES | APPROVER_ROLES
ER_FORMULA = "ER = (реакции + комментарии + репосты) / охват × 100%."
Platform = Literal["telegram", "vk", "max"]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Count = Annotated[int, Field(strict=True, ge=0, le=10**12)]


class Publication(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    platform: Platform
    publication_id: Identifier
    title: Identifier
    format: Identifier
    published_on: date
    post_id: int | None = Field(default=None, gt=0)


class MetricInput(Publication):
    observed_on: date
    source: Identifier
    views: Count | None = None
    reach: Count | None = None
    reactions: Count | None = None
    comments: Count | None = None
    shares: Count | None = None

    @model_validator(mode="after")
    def valid_snapshot(self):
        if self.observed_on < self.published_on:
            raise ValueError("Дата замера должна быть не раньше даты публикации.")
        if all(getattr(self, field) is None for field in COUNT_FIELDS):
            raise ValueError("Укажите хотя бы одну метрику.")
        return self


class MetricImport(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    format: Literal["csv", "json"]
    content: str = Field(min_length=1, max_length=1_000_000)


class MetricSync(Publication):
    platform: Literal["vk", "max"]


def initialize_analytics(service):
    with service.db.transaction() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS analytics_metrics (
            id INTEGER PRIMARY KEY,
            company_id INTEGER NOT NULL REFERENCES companies(id),
            platform TEXT NOT NULL,
            publication_id TEXT NOT NULL,
            title TEXT NOT NULL,
            format TEXT NOT NULL,
            published_on TEXT NOT NULL,
            observed_on TEXT NOT NULL,
            source TEXT NOT NULL,
            origin TEXT NOT NULL,
            post_id INTEGER REFERENCES posts(id),
            views INTEGER,
            reach INTEGER,
            reactions INTEGER,
            comments INTEGER,
            shares INTEGER,
            recorded_at TEXT NOT NULL,
            UNIQUE (company_id, platform, publication_id, observed_on, origin, source)
        )""")


def parse_import(payload):
    if payload.format == "json":
        try:
            rows = json.loads(payload.content.removeprefix("\ufeff"))
        except (ValueError, RecursionError) as exc:
            raise DomainError("Проверьте синтаксис JSON.", 422) from exc
        if not isinstance(rows, list):
            raise DomainError("В JSON нужен массив записей.", 422)
    else:
        try:
            reader = csv.DictReader(io.StringIO(payload.content.removeprefix("\ufeff")), strict=True)
            fields = reader.fieldnames or []
            if not fields or len(fields) != len(set(fields)) or set(fields) - MetricInput.model_fields.keys():
                raise DomainError("Проверьте заголовки CSV. Разделитель — запятая.", 422)
            rows = []
            for row in reader:
                if None in row or None in row.values():
                    raise DomainError(f"Проверьте число полей в строке CSV №{reader.line_num}.", 422)
                rows.append({key: value.strip() for key, value in row.items() if value.strip()})
        except csv.Error as exc:
            raise DomainError("Проверьте синтаксис CSV.", 422) from exc
    if not 1 <= len(rows) <= 1000:
        raise DomainError("Добавьте от 1 до 1000 записей за импорт.", 422)
    result = []
    for number, row in enumerate(rows, 1):
        try:
            if payload.format == "csv":
                for field in COUNT_FIELDS:
                    if field in row:
                        row[field] = int(row[field])
            result.append(MetricInput.model_validate(row))
        except (ValidationError, ValueError) as exc:
            raise DomainError(f"Запись №{number}: проверьте обязательные поля, даты и целые значения метрик.", 422) from exc
    return result


def _check_link(service, connection, cid, row):
    if row.post_id is None:
        return
    post = service._post_row(connection, cid, row.post_id)
    if post["status"] != "published":
        raise DomainError("Метрики можно привязать к опубликованному посту.", 422)
    external_id = post["message_id"]
    if external_id and post["platform"] == "telegram":
        external_id = f"{post['channel_id']}:{external_id}"
    if row.platform != post["platform"] or row.publication_id != external_id:
        raise DomainError("Площадка и ID замера должны совпадать с опубликованным постом.", 422)


def store_metrics(service, cid, rows, origin, actor, *, synced_connection=None, access_check=None):
    written_at = utc_now()
    with service.db.transaction() as connection:
        service.require_actor(connection, cid, actor, EDITOR_ROLES)
        if access_check is not None:
            access_check(connection)
        if synced_connection is not None:
            from .social_platforms import get_social_connection

            current = get_social_connection(connection, cid, rows[0].platform, include_secret=True)
            if not current["enabled"] or any(current[field] != synced_connection[field] for field in ("destination_id", "token")):
                raise DomainError("Подключение площадки изменилось. Повторите запрос.", 409)
        for row in rows:
            _check_link(service, connection, cid, row)
            connection.execute("""INSERT INTO analytics_metrics (
                company_id, platform, publication_id, title, format, published_on, observed_on,
                source, origin, post_id, views, reach, reactions, comments, shares, recorded_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (company_id, platform, publication_id, observed_on, origin, source) DO UPDATE SET
                title=excluded.title, format=excluded.format, published_on=excluded.published_on,
                post_id=excluded.post_id, views=excluded.views, reach=excluded.reach, reactions=excluded.reactions,
                comments=excluded.comments, shares=excluded.shares, recorded_at=excluded.recorded_at""", (
                cid, row.platform, row.publication_id, row.title, row.format, row.published_on.isoformat(),
                row.observed_on.isoformat(), row.source, origin, row.post_id, *(getattr(row, field) for field in COUNT_FIELDS),
                written_at,
            ))
        audit_row(connection, cid, "analytics.metrics_recorded", f"Записано замеров: {len(rows)}.", actor)
    return {"recorded": len(rows)}


async def sync_metrics(service, cid, payload, actor, *, destination=None, skip_empty=False, access_check=None):
    from .social_platforms import fetch_social_metrics, get_social_connection

    with service.db.read() as connection:
        service.require_actor(connection, cid, actor, EDITOR_ROLES)
        if access_check is not None:
            access_check(connection)
        _check_link(service, connection, cid, payload)
        company = service._company_row(connection, cid)
        social = get_social_connection(connection, cid, payload.platform, include_secret=True)
        observed_on = datetime.now(ZoneInfo(company["timezone"])).date()
    if payload.published_on > observed_on:
        raise DomainError("Дата публикации должна быть не позже сегодняшней.", 422)
    if not social["configured"] or not social["enabled"]:
        raise DomainError("Подключите и включите площадку в настройках компании.", 422)
    if destination is not None and social["destination_id"] != destination:
        raise DomainError("Подключение площадки изменилось.", 409)
    try:
        raw = await fetch_social_metrics(payload.platform, social, payload.publication_id)
    except integrations.IntegrationError as error:
        raise DomainError(str(error), 429 if error.status_code == 429 else 502) from error
    aliases = {"reactions": "likes", "shares": "reposts"}
    data = {field: raw[field] if field in raw else raw.get(aliases.get(field)) for field in COUNT_FIELDS}
    if all(value is None for value in data.values()):
        if skip_empty:
            return {"recorded": 0}
        raise DomainError("Площадка не вернула метрики для этой публикации.", 422)
    try:
        snapshot = MetricInput(**payload.model_dump(), observed_on=observed_on,
                               source=f"{payload.platform.upper()} API · {social['destination_id']}", **data)
    except ValidationError as error:
        raise DomainError("Площадка вернула метрики в неожиданном формате.", 502) from error
    return store_metrics(service, cid, [snapshot], "api", actor, synced_connection=social, access_check=access_check)


def _rollup(items):
    coverage = {field: 0 for field in COUNT_FIELDS}
    totals = {field: None for field in COUNT_FIELDS}
    er_posts = er_interactions = er_reach = 0
    for item in items:
        for field in COUNT_FIELDS:
            value = item[field]
            if value is not None:
                coverage[field] += 1
                totals[field] = (totals[field] or 0) + value
        if item["reach"] and all(item[field] is not None for field in ("reactions", "comments", "shares")):
            er_posts += 1
            er_reach += item["reach"]
            er_interactions += item["reactions"] + item["comments"] + item["shares"]
    return {"publications": len(items), "coverage": coverage, "totals": totals,
            "er": {"percent": round(100 * er_interactions / er_reach, 2) if er_reach else None,
                   "denominator": "reach", "reach": er_reach if er_posts else None,
                   "interactions": er_interactions if er_posts else None, "publications": er_posts}}


def _report(summary, formats):
    count = summary["publications"]
    if count == 0:
        return {"findings": ["За выбранный период нет замеров."], "hypotheses": []}
    findings = [f"Публикаций с замерами: {count}. Форматов: {len(formats)}."]
    best_views = sorted((entry for entry in formats if entry["totals"]["views"] is not None),
                        key=lambda entry: entry["totals"]["views"] / entry["coverage"]["views"], reverse=True)
    views = summary["totals"]["views"]
    if views is not None:
        covered = summary["coverage"]["views"]
        findings.append(f"Сумма просмотров: {views} по {covered} из {count} публикаций.")
        if len(best_views) > 1:
            first, second = best_views[:2]
            first_avg = first["totals"]["views"] / first["coverage"]["views"]
            second_avg = second["totals"]["views"] / second["coverage"]["views"]
            if first_avg > second_avg:
                findings.append(f"Формат «{first['format']}» набрал больше всего просмотров в среднем: "
                                f"{first_avg:.1f} по {first['coverage']['views']} публикациям с данными о просмотрах.")
    er = summary["er"]
    if er["percent"] is not None:
        findings.append(f"ER {er['percent']:.2f}% по {er['publications']} из {count} публикаций. Сумма охватов: {er['reach']}.")
    else:
        findings.append("Для расчёта ER нужен положительный охват и все три вида взаимодействий в одном замере.")
    hypotheses = []
    if len(formats) > 1:
        hypotheses.append("Проверьте, связан ли формат с вовлечённостью. Сравните публикации одной площадки с одинаковым сроком с момента выхода.")
    return {"findings": findings, "hypotheses": hypotheses}


def analytics_report(service, cid, start, end, actor):
    if start and end and start > end:
        raise DomainError("Конец периода должен быть не раньше начала.", 422)
    start_value = start.isoformat() if start else None
    end_value = end.isoformat() if end else None
    with service.db.read() as connection:
        service.require_actor(connection, cid, actor, MEMBER_ROLES)
        rows = connection.execute("""WITH ranked AS (
            SELECT platform, publication_id, title, format, published_on, observed_on, source, origin,
                   post_id, views, reach, reactions, comments, shares,
                   ROW_NUMBER() OVER (PARTITION BY platform, publication_id
                                      ORDER BY observed_on DESC, recorded_at DESC, id DESC) AS snapshot_rank
            FROM analytics_metrics WHERE company_id=?
        )
        SELECT platform, publication_id, title, format, published_on, observed_on, source, origin, post_id,
               views, reach, reactions, comments, shares
        FROM ranked WHERE snapshot_rank=1 AND (? IS NULL OR published_on>=?) AND (? IS NULL OR published_on<=?)
        ORDER BY published_on DESC, platform, publication_id""", (cid, start_value, start_value, end_value, end_value)).fetchall()
    items = [dict(row) for row in rows]
    grouped = {}
    for item in items:
        grouped.setdefault(item["format"], []).append(item)
    summary = _rollup(items)
    formats = [{"format": name, **_rollup(members)} for name, members in sorted(grouped.items())]
    return {"items": items, "summary": summary, "formats": formats, "report": _report(summary, formats),
            "er_formula": ER_FORMULA}


def register_analytics_routes(api, service, member, editor):
    @api.get("/companies/{cid}/analytics")
    def overview(cid: int, start: date | None = Query(None), end: date | None = Query(None), user=Depends(member)):
        return analytics_report(service, cid, start, end, user)

    @api.post("/companies/{cid}/analytics/metrics", status_code=201)
    def record_metric(cid: int, payload: MetricInput, user=Depends(editor)):
        return store_metrics(service, cid, [payload], "manual", user)

    @api.post("/companies/{cid}/analytics/import", status_code=201)
    def import_metrics(cid: int, payload: MetricImport, user=Depends(editor)):
        return store_metrics(service, cid, parse_import(payload), payload.format, user)

    @api.post("/companies/{cid}/analytics/sync", status_code=201)
    async def sync(cid: int, payload: MetricSync, user=Depends(editor)):
        return await sync_metrics(service, cid, payload, user)
