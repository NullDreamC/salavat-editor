import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from server import reports
from server.integrations import TelegramError
from test_backend import EDITOR, REVIEWER, SUPER, add_person, api, configure, sign_in


PATH = "/api/companies/1/reports"
TOKEN = "123:synthetic-token"
NOW = datetime(2026, 11, 14, 7, tzinfo=timezone.utc)
ADMIN_ID = 2


def _linked_admin(client, service):
    configure(client, telegram_token=TOKEN)
    with service.db.transaction() as connection:
        connection.execute("UPDATE memberships SET telegram_id='700' WHERE company_id=1 AND user_id=?", (ADMIN_ID,))


def test_preview_uses_local_period_and_only_saved_telegram_observations(api, monkeypatch):
    client, service = api
    monkeypatch.setattr(reports, "_now", lambda now=None: now or NOW)
    with service.db.transaction() as connection:
        connection.executemany(
            "INSERT INTO activity(company_id,action,detail,created_at) VALUES (?,'post.published','test',?)",
            [(1, "2026-11-12T20:59:59+00:00"), (1, "2026-11-12T21:00:00+00:00"),
             (1, "2026-11-13T20:59:59+00:00"), (1, "2026-11-13T21:00:00+00:00"),
             (2, "2026-11-13T08:00:00+00:00")],
        )
        connection.execute(
            "INSERT INTO posts(company_id,title,kind,status,source_ids,created_at,channel_id,template) "
            "VALUES (1,'Пост','general','review','[]','2026-11-01T00:00:00+00:00','','')",
        )
        connection.execute(
            "INSERT INTO assistant_tasks(company_id,actor_id,kind,prompt,source_ids,status,created_at,updated_at) "
            "VALUES (1,?,'ask','test','[]','completed','2026-11-13T10:00:00+00:00','2026-11-14T00:00:00+00:00')",
            (ADMIN_ID,),
        )
        connection.executemany(
            "INSERT INTO analytics_metrics(company_id,platform,publication_id,title,format,published_on,observed_on,"
            "source,origin,views,recorded_at) VALUES (?,?,?,'Пост','Фото','2026-11-13',?,?,'manual',?,?)",
            [
                (1, "telegram", "1", "2026-11-13", "Первый", 12, "2026-11-13T10:00:00+00:00"),
                (1, "telegram", "1", "2026-11-14", "Второй", 0, "2026-11-14T06:00:00+00:00"),
                (1, "telegram", "1", "2026-11-14", "Поздний", 300, "2026-11-14T08:00:00+00:00"),
                (1, "vk", "1", "2026-11-13", "Тест", 500, "2026-11-13T10:00:00+00:00"),
                (2, "telegram", "1", "2026-11-13", "Тест", 600, "2026-11-13T10:00:00+00:00"),
            ],
        )

    response = client.get(PATH + "/preview?days=1")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["start_on"] == report["end_on"] == "2026-11-13"
    assert report["timezone"] == "Europe/Moscow"
    assert "Публикаций через кабинет за период — 2" in report["text"]
    assert "Задач помощнику создано — 1. Из них выполнено к моменту отчёта — 1" in report["text"]
    assert "На согласовании — 1" in report["text"]
    assert "Даты последних замеров — 14.11.2026" in report["text"]
    assert "Просмотры — 0 (по 1 из 1)" in report["text"]
    assert "Охват — нет данных" in report["text"]
    assert "Замеров нет." in client.get(PATH.replace("/1/", "/3/") + "/preview?days=1").json()["text"]
    assert client.get(PATH + "/preview?days=4").status_code == 422
    assert client.get(PATH).json()["reports"] == []


def test_scheduling_roles_tenant_scope_and_idempotency(api, monkeypatch):
    client, service = api
    _linked_admin(client, service)
    editor = add_person(client, EDITOR)
    add_person(client, REVIEWER)
    monkeypatch.setattr(reports, "_now", lambda now=None: now or NOW)
    payload = reports.ScheduleCreate(frequency="weekly", weekday=0, time="09:00")
    schedule = reports.create_report_schedule(service, 1, ADMIN_ID, payload, request_key="intent-test")
    assert reports.create_report_schedule(service, 1, ADMIN_ID, payload, request_key="intent-test") == schedule
    assert len(client.get(PATH).json()["schedules"]) == 1
    assert client.get(PATH.replace("/1/", "/2/")).json()["schedules"] == []
    exists = f"{PATH}/schedules/{schedule['id']}"
    with sign_in(client.app, EDITOR) as session:
        assert session.get(PATH).json()["recipients"] == []
        assert session.post(exists + "/disable?version=1").status_code == 404
        assert session.post(PATH + "/schedules", json=payload.model_dump()).status_code == 422
        assign = payload.model_copy(update={"recipient_id": ADMIN_ID, "notify": False})
        assert session.post(PATH + "/schedules", json=assign.model_dump()).status_code == 403
    with sign_in(client.app, REVIEWER) as session:
        assert session.get(PATH + "/preview?days=1").status_code == 200
        assert session.post(PATH, json={"days": 1}).status_code == 403
    with sign_in(client.app, SUPER) as session:
        assert session.get(PATH).status_code == 403
    outsider = {**payload.model_dump(), "recipient_id": editor["id"]}
    assert client.post(PATH.replace("/1/", "/2/") + "/schedules", json=outsider).status_code == 422

    monday = datetime(2026, 11, 16, 6, 15, tzinfo=timezone.utc)
    created, = reports.due_reports(service, 1, now=monday)
    assert created["start_on"] == "2026-11-09" and created["end_on"] == "2026-11-15"
    assert reports.due_reports(service, 1, now=monday) == []
    assert reports.due_reports(service, 2, now=monday) == []
    disabled = client.post(exists + "/disable?version=1")
    assert disabled.status_code == 200 and not disabled.json()["enabled"]
    history, = client.get(PATH).json()["reports"]
    assert history["id"] == created["id"] and history["delivery_status"] == "cancelled"
    assert reports.create_report_schedule(service, 1, ADMIN_ID, payload, request_key="intent-test") == disabled.json()


def test_manual_send_and_delivery_recoverability(api):
    client, service = api
    _linked_admin(client, service)
    saved = client.post(PATH, json={"days": 1}).json()
    assert client.post(PATH, json={"days": 1}).json()["id"] == saved["id"]
    path = PATH + f"/{saved['id']}/send"
    pending = client.post(path).json()
    assert pending["delivery_status"] == "pending" and pending["text"] == saved["text"]
    assert client.post(path).json()["delivery_status"] == "pending"
    rejected = AsyncMock(side_effect=TelegramError("synthetic error", status_code=403))
    asyncio.run(reports.deliver_reports(service, 1, 123, rejected, token=TOKEN))
    rejected.assert_awaited_once_with(700, saved["text"])
    failed, = client.get(PATH).json()["reports"]
    assert failed["delivery_status"] == "failed" and failed["can_send"]
    assert "synthetic error" not in failed["delivery_error"]
    assert client.post(path).json()["delivery_status"] == "pending"
    delivered = AsyncMock(return_value={"message_id": 100})
    asyncio.run(reports.deliver_reports(service, 1, 123, delivered, token=TOKEN))
    asyncio.run(reports.deliver_reports(service, 1, 123, delivered, token=TOKEN))
    delivered.assert_awaited_once_with(700, saved["text"])
    sent, = client.get(PATH).json()["reports"]
    assert sent["delivery_status"] == "sent" and sent["delivered_at"]
    assert client.post(path).json()["delivery_status"] == "sent"


def test_uncertain_delivery_and_changed_target_are_never_replayed(api):
    client, service = api
    _linked_admin(client, service)
    saved = client.post(PATH, json={"days": 1}).json()
    path = PATH + f"/{saved['id']}/send"
    assert client.post(path).status_code == 202
    ambiguous = AsyncMock(side_effect=TelegramError("ambiguous", uncertain=True))
    asyncio.run(reports.deliver_reports(service, 1, 123, ambiguous, token=TOKEN))
    assert client.post(path).status_code == 409
    snapshot = client.post(PATH, json={"days": 7}).json()
    assert client.post(PATH + f"/{snapshot['id']}/send").status_code == 202
    with service.db.transaction() as connection:
        connection.execute("UPDATE memberships SET telegram_id='799' WHERE company_id=1 AND user_id=?", (ADMIN_ID,))
    asyncio.run(reports.deliver_reports(service, 1, 123, ambiguous, token=TOKEN))
    ambiguous.assert_awaited_once()
    assert [row["delivery_status"] for row in client.get(PATH).json()["reports"]] == ["cancelled", "uncertain"]


def test_disconnected_bot_cancels_pending_and_recovers_interrupted_sends(api, monkeypatch):
    client, service = api
    _linked_admin(client, service)
    monkeypatch.setattr(reports, "_now", lambda now=None: now or NOW)
    running, pending = [client.post(PATH, json={"days": days}).json() for days in (1, 7)]
    for report in (running, pending):
        assert client.post(PATH + f"/{report['id']}/send").status_code == 202
    with service.db.transaction() as connection:
        connection.execute(
            "UPDATE company_reports SET delivery_status='sending', claimed_at=? WHERE id=?",
            (NOW.timestamp() - 601, running["id"]),
        )
        connection.execute("UPDATE companies SET telegram_token=NULL WHERE id=1")
    assert reports.due_reports(service, 1, now=NOW) == []
    assert [row["delivery_status"] for row in client.get(PATH).json()["reports"]] == ["cancelled", "uncertain"]
