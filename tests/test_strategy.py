import json
from datetime import datetime, timedelta, timezone

import pytest

from server import analytics, assistant_tasks, strategy
from server.db import Database
from server.domain import DomainError, Service


NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


@pytest.fixture
def service(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    db.initialize()
    result = Service(db)
    analytics.initialize_analytics(result)
    assistant_tasks.initialize_tasks(result)
    strategy.initialize_strategy(result)
    with db.transaction() as connection:
        connection.executemany("INSERT INTO companies(id,name) VALUES(?,?)", [(1, "Первая"), (2, "Вторая")])
        connection.executemany(
            "INSERT INTO users(id,name,email) VALUES(?,?,?)",
            [(1, "Админ", "admin@example.org"), (2, "Редактор", "editor@example.org")],
        )
        connection.executemany("INSERT INTO memberships(company_id,user_id,role) VALUES(?,?,?)",
                               [(1, 1, "admin"), (2, 1, "admin"), (1, 2, "editor")])
    return result


def publish_sample(service, *, views, media, offset=12, observation_age=7, company=1,
                   platform="telegram", channel="-100555", origin="manual"):
    published = NOW - timedelta(days=offset)
    observed = published + timedelta(days=observation_age)
    with service.db.transaction() as connection:
        post_id = connection.execute(
            """INSERT INTO posts(company_id,title,kind,status,source_ids,created_at,channel_id,platform,
               template,approved_snapshot) VALUES(?,'Название','general','published','[]',?,?,?,'announcement',?)""",
            (company, published.isoformat(), channel, platform,
             json.dumps({"media": [{"mime_type": mime} for mime in media]})),
        ).lastrowid
        external = str(post_id)
        connection.execute("UPDATE posts SET message_id=? WHERE id=?", (external, post_id))
        connection.execute("INSERT INTO activity(company_id,action,detail,created_at) VALUES(?,?,?,?)",
                           (company, "post.published", f"Опубликован пост №{post_id}.", published.isoformat()))
        if views is not None:
            identifier = f"{channel}:{external}" if platform == "telegram" else external
            connection.execute(
                """INSERT INTO analytics_metrics(company_id,platform,publication_id,title,format,published_on,
                   observed_on,source,origin,post_id,views,recorded_at)
                   VALUES(?,?,?,'Название','Формат из импорта',?,?,?,?,?,?,?)""",
                (company, platform, identifier, published.date().isoformat(), observed.date().isoformat(), "Замер",
                 origin, post_id, views, observed.isoformat()),
            )
    return post_id


def populate(service):
    for views in (100, 120, 150):
        publish_sample(service, views=views, media=[])
    for views in (300, 380, 10000):
        publish_sample(service, views=views, media=["video/mp4"])


def test_strategy_scheduled_once_and_confirmation_adds_experiment_only(service):
    populate(service)
    with service.db.transaction() as connection:
        assistant_tasks._save_plan(connection, 1, assistant_tasks.PlanCreate(
            title="План редактора", planned_on="2026-10-20",
        ))
    strategy.refresh_strategy(service, 1, now=NOW)
    first = strategy.strategy_overview(service, 1, 1)
    assert first["assessment"]["status"] == "recommendations"
    proposal, = first["recommendations"]
    assert proposal["preferred_format"] == "Видео" and proposal["comparison_format"] == "Текст"
    winner, runner_up = proposal["evidence"]["groups"]
    assert (winner["median_views"], runner_up["median_views"]) == (380, 120)
    assert len({sample["metric_id"] for sample in winner["observations"] + runner_up["observations"]}) == 6
    strategy.refresh_strategy(service, 1, now=NOW + timedelta(hours=1))
    assert strategy.strategy_overview(service, 1, 1) == first
    assert strategy.strategy_overview(service, 2, 1)["recommendations"] == []
    with service.db.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM content_plan").fetchone()[0] == 1
    strategy.apply_strategy(service, 1, proposal["id"], proposal["version"], 2, now=NOW)
    finished, = strategy.strategy_overview(service, 1, 1)["recommendations"]
    assert finished["status"] == "applied" and finished["version"] == proposal["version"] + 1
    with pytest.raises(DomainError) as stale:
        strategy.apply_strategy(service, 1, proposal["id"], proposal["version"], 2, now=NOW)
    assert stale.value.status_code == 409
    with service.db.read() as connection:
        plans = connection.execute("SELECT id,title,format,post_id FROM content_plan ORDER BY id").fetchall()
        assert [plan["title"] for plan in plans] == ["План редактора", proposal["title"]]
        assert plans[-1]["format"] == "video" and plans[-1]["post_id"] is None
        assert connection.execute("SELECT COUNT(*) FROM posts WHERE status='published'").fetchone()[0] == 6
    strategy.refresh_strategy(service, 1, now=NOW + timedelta(days=1))
    assert len(strategy.strategy_overview(service, 1, 1)["recommendations"]) == 1


def test_strategy_missing_stats_age_and_destination_are_not_merged(service):
    for _ in range(3):
        publish_sample(service, views=100, media=[])
        publish_sample(service, views=500, media=["video/mp4"], channel="-100987")
        publish_sample(service, views=500, media=["video/mp4"], observation_age=11)
        publish_sample(service, views=None, media=["video/mp4"])
    strategy.refresh_strategy(service, 1, now=NOW)
    result = strategy.strategy_overview(service, 1, 1)
    assert result["assessment"]["status"] == "insufficient" and result["recommendations"] == []


def test_strategy_rechecks_evidence_and_revoked_actor(service):
    populate(service)
    strategy.refresh_strategy(service, 1, now=NOW)
    proposal, = strategy.strategy_overview(service, 1, 1)["recommendations"]
    with service.db.transaction() as connection:
        connection.execute("UPDATE memberships SET active=0 WHERE company_id=1 AND user_id=2")
    with pytest.raises(DomainError) as denied:
        strategy.apply_strategy(service, 1, proposal["id"], proposal["version"], 2, now=NOW)
    assert denied.value.status_code == 403
    with pytest.raises(DomainError) as expired:
        strategy.apply_strategy(service, 1, proposal["id"], proposal["version"], 1, now=NOW + timedelta(days=7))
    assert expired.value.status_code == 409
    with service.db.transaction() as connection:
        connection.execute("UPDATE analytics_metrics SET views=123 WHERE id=?", (
            proposal["evidence"]["groups"][0]["observations"][0]["metric_id"],
        ))
    with pytest.raises(DomainError) as changed:
        strategy.apply_strategy(service, 1, proposal["id"], proposal["version"], 1, now=NOW)
    assert changed.value.status_code == 409
    strategy.refresh_strategy(service, 1, 1, now=NOW)
    revised = strategy.strategy_overview(service, 1, 1)["recommendations"]
    assert any(row["id"] == proposal["id"] and row["status"] == "superseded" for row in revised)
    with service.db.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM content_plan").fetchone()[0] == 0
