import asyncio
import json
from datetime import datetime, timezone

import httpx

from server import assistant_tasks as tasks
from test_backend import EDITOR, REVIEWER, SUPER, add_person, api, configure, sign_in  # noqa: F401


BASE = "/api/companies/1"


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def add_task(client, kind="chat", prompt="What do we know?", **fields):
    return ok(client.post(BASE + "/tasks", json={"kind": kind, "prompt": prompt, **fields}), 201)


def provider(monkeypatch, answer):
    recorded = []

    async def respond(endpoint, headers, payload, timeout):
        recorded.append(json.loads(payload["input"][0]["content"]))
        assert payload["store"] is False
        response = answer(recorded[-1]) if callable(answer) else answer
        return httpx.Response(200, json={
            "status": "completed",
            "output_text": json.dumps({
                "answer_type": "factual" if response.get("facts") else "proposal",
                "reply": "", "questions": [], "facts": [], "proposals": [], **response,
            }),
            "usage": {"input_tokens": 100, "output_tokens": 20},
        })

    monkeypatch.setattr(tasks, "_post_generation", respond)
    return recorded


def test_context_conversation_usage_and_queue_order(api, monkeypatch):
    client, service = api
    configure(client, openai_key="mock-key", requests_limit=2)
    active = ok(client.post(BASE + "/knowledge", json={
        "title": "Opening hours", "text": "The office opens at nine", "category": "business", "status": "active",
    }), 201)
    ok(client.post(BASE + "/knowledge", json={
        "title": "Assumption", "text": "Never send a draft", "category": "business",
    }), 201)
    ok(client.post("/api/companies/2/knowledge", json={
        "title": "Other company", "text": "Never mix tenant context", "category": "business", "status": "active",
    }), 201)
    selected = ok(client.post(BASE + "/sources", json={"title": "Office", "text": "The main office"}), 201)
    fact = {"source_id": active["source_id"], "evidence": "opens at nine"}
    requests = provider(monkeypatch, {"reply": "The office opens at nine", "facts": [fact]})

    first = add_task(client, source_ids=[selected["id"]])
    second = add_task(client, prompt="Next queued question")
    first_url = f"{BASE}/tasks/{first['id']}"
    assert first["status"] == "queued"
    assert first["messages"][0]["text"] == first["prompt"]
    assert client.post(first_url + "/messages", json={"text": "Too early"}).status_code == 409
    assert asyncio.run(tasks.process_tasks(service, 1))["id"] == first["id"]
    assert ok(client.get(f"{BASE}/tasks/{second['id']}"))["status"] == "queued"
    result = ok(client.get(first_url))
    assert result["status"] == "completed"
    assert result["facts"] == [fact]
    assert result["messages"][-1]["role"] == "assistant"
    assert {item["id"] for item in requests[0]["materials"]} == {selected["id"], active["source_id"]}
    assert "mock-key" not in json.dumps(requests[0])

    ok(client.post(first_url + "/messages", json={"text": "Please elaborate"}))
    assert asyncio.run(tasks.process_tasks(service, 1))["id"] == second["id"]
    exhausted = asyncio.run(tasks.process_tasks(service, 1))
    assert exhausted["id"] == first["id"]
    assert exhausted["status"] == "failed"
    assert exhausted["usage_id"] is None
    assert len(requests) == 2
    assert service.usage(1)["input_tokens"] == 200
    assert service.usage(1)["output_tokens"] == 40

    configure(client, requests_limit=3)
    assert ok(client.post(first_url + "/retry"))["status"] == "queued"
    assert asyncio.run(tasks.process_tasks(service, 1))["status"] == "completed"
    assert requests[-1]["conversation"] == [
        {"role": "user", "text": first["prompt"]},
        {"role": "assistant", "text": "The office opens at nine"},
        {"role": "user", "text": "Please elaborate"},
    ]
    assert asyncio.run(tasks.process_tasks(service, 1)) is None


def test_tenant_roles_and_queued_authorization_recheck(api, monkeypatch):
    client, service = api
    editor_user = add_person(client, EDITOR)
    add_person(client, REVIEWER)
    with sign_in(client.app, EDITOR) as editor, sign_in(client.app, REVIEWER) as reviewer, sign_in(
        client.app, SUPER
    ) as owner:
        foreign = ok(client.post("/api/companies/2/sources", json={"title": "Foreign", "text": "Private"}), 201)
        assert editor.post(BASE + "/tasks", json={
            "kind": "chat", "prompt": "Cross-tenant source", "source_ids": [foreign["id"]],
        }).status_code == 404
        task = add_task(editor)
        assert ok(reviewer.get(BASE + "/tasks"))[0]["id"] == task["id"]
        assert reviewer.post(BASE + "/tasks", json={"kind": "chat", "prompt": "Forbidden"}).status_code == 403
        for resource in ("tasks", "plan", "routines"):
            assert owner.get(f"{BASE}/{resource}").status_code == 403
        assert client.get(f"/api/companies/2/tasks/{task['id']}").status_code == 404
        with service.db.transaction() as connection:
            connection.execute(
                "UPDATE memberships SET role='approver' WHERE company_id=1 AND user_id=?", (editor_user["id"],)
            )
        recorded = provider(monkeypatch, {"reply": "Never called"})
        result = asyncio.run(tasks.process_tasks(service, 1))
        assert result["status"] == "failed"
        assert result["usage_id"] is None
        assert recorded == []


def test_crash_recovery_and_uncertain_request_are_not_replayed(api, monkeypatch):
    client, service = api
    configure(client, openai_key="mock-key")
    task = add_task(client)
    running, attempt_id = tasks._claim(service, 1)
    assert running["status"] == "running"
    with service.db.read() as connection:
        raw_company = service._company_row(connection, 1)
        company = service._company_data(raw_company)
    assert tasks._reserve(service, 1, task["id"], company, attempt_id)
    next_task = add_task(client)

    tasks.initialize_tasks(service)
    assert ok(client.get(f"{BASE}/tasks/{task['id']}"))["status"] == "interrupted"
    requests = provider(monkeypatch, {"reply": "Completed queued request"})
    assert asyncio.run(tasks.process_tasks(service, 1))["id"] == next_task["id"]
    assert asyncio.run(tasks.process_tasks(service, 1)) is None
    assert len(requests) == 1
    assert service.usage(1)["requests"] == 2


def test_generated_plan_is_grounded_and_converts_to_a_single_draft(api, monkeypatch):
    client, service = api
    configure(client, openai_key="mock-key")
    source = ok(client.post(BASE + "/sources", json={"title": "Safety day", "text": "A safety session is confirmed"}), 201)
    plan = {
        "title": "Safety session promotion", "planned_on": "2028-03-15", "format": "post",
        "brief": "Introduce the safety session", "source_ids": [source["id"]],
    }
    response = {"reply": "One proposed item", "proposals": [plan]}
    provider(monkeypatch, {**response, "facts": [{"source_id": source["id"], "evidence": "Nonexistent quote"}]})
    add_task(client, kind="plan", source_ids=[source["id"]])
    ungrounded = asyncio.run(tasks.process_tasks(service, 1))
    assert ungrounded["status"] == "needs_info"
    assert ungrounded["reply"] == ""
    assert ok(client.get(BASE + "/plan")) == []

    provider(monkeypatch, {**response, "facts": [{"source_id": source["id"], "evidence": "safety session is confirmed"}]})
    task = add_task(client, kind="plan", source_ids=[source["id"]])
    assert asyncio.run(tasks.process_tasks(service, 1))["status"] == "completed"
    item, = ok(client.get(BASE + "/plan"))
    assert item["task_id"] == task["id"]
    assert item["status"] == "proposed"
    item_url = f"{BASE}/plan/{item['id']}"
    assert client.post(f"/api/companies/2/plan/{item['id']}/post").status_code == 404
    post = ok(client.post(item_url + "/post"), 201)
    assert post["status"] == "draft"
    assert post["scheduled_at"] is None
    assert post["source_ids"] == [source["id"]]
    assert post["body"] == ""
    assert item["brief"] == plan["brief"]
    assert ok(client.post(item_url + "/post"), 201)["id"] == post["id"]
    assert ok(client.patch(item_url, json={"planned_on": None}))["status"] == "drafted"
    assert client.delete(item_url).status_code == 204
    assert service.post(1, post["id"])["status"] == "draft"


def test_factual_answer_requires_evidence(api, monkeypatch):
    client, service = api
    configure(client, openai_key="mock-key")
    provider(monkeypatch, {"answer_type": "factual", "reply": "The office opens at nine"})
    add_task(client)
    result = asyncio.run(tasks.process_tasks(service, 1))
    assert result["status"] == "needs_info"
    assert result["reply"] == ""
    assert result["questions"]


def test_archived_knowledge_invalidates_in_flight_response(api, monkeypatch):
    client, service = api
    configure(client, openai_key="mock-key")
    active = ok(client.post(BASE + "/knowledge", json={
        "title": "Opening hours", "text": "The office opens at nine", "category": "business", "status": "active",
    }), 201)

    def archive_while_processing(request):
        assert any(item["id"] == active["source_id"] for item in request["materials"])
        assert client.delete(f"{BASE}/knowledge/{active['id']}").status_code == 204
        return {"reply": "The office opens at nine",
                "facts": [{"source_id": active["source_id"], "evidence": "opens at nine"}]}

    provider(monkeypatch, archive_while_processing)
    add_task(client)
    result = asyncio.run(tasks.process_tasks(service, 1))
    assert result["status"] == "needs_info"
    assert result["reply"] == ""
    assert result["facts"] == []
    assert service.usage(1)["input_tokens"] == 100


def test_routines_are_disabled_by_default_and_match_one_local_slot(api):
    client, service = api
    configure(client, timezone="Europe/Moscow")
    routine_url = BASE + "/routines"
    payload = {"name": "Weekly plan", "kind": "plan", "prompt": "Prepare ideas", "frequency": "weekly", "time": "09:15"}
    assert client.post(routine_url, json=payload).status_code == 422
    routine = ok(client.post(routine_url, json={**payload, "weekday": 0}), 201)
    item_url = f"{routine_url}/{routine['id']}"
    assert not routine["enabled"]
    assert ok(client.post(item_url + "/run"), 201)["routine_id"] == routine["id"]
    monday = datetime(2030, 3, 4, 6, 15, 25, tzinfo=timezone.utc)
    assert tasks.due_routines(service, 1, now=monday) == []
    assert ok(client.patch(item_url, json={"enabled": True}))["enabled"]
    assert client.patch(item_url, json={"weekday": None}).status_code == 422
    assert tasks.due_routines(service, 1, now=monday.replace(minute=14)) == []
    assert tasks.due_routines(service, 1, now=monday.replace(day=5)) == []
    generated, = tasks.due_routines(service, 1, now=monday.replace(minute=35))
    assert generated["routine_id"] == routine["id"]
    assert tasks.due_routines(service, 1, now=monday.replace(hour=10)) == []
    assert ok(client.patch(item_url, json={"time": "10:00"}))["time"] == "10:00"
    assert tasks.due_routines(service, 1, now=monday.replace(hour=10)) == []
    assert client.delete(item_url).status_code == 204
    assert ok(client.get(f"{BASE}/tasks/{generated['id']}"))["routine_id"] is None


def test_opted_in_routine_preserves_creator_and_records_missed_slot_once(api):
    client, service = api
    task_payload = {"kind": "chat", "prompt": "Prepare ideas", "notify_telegram": True}
    routine_payload = {**task_payload, "name": "Daily ideas", "frequency": "daily", "time": "09:15", "enabled": True}
    assert client.post(BASE + "/tasks", json=task_payload).status_code == 422
    assert client.post(BASE + "/routines", json=routine_payload).status_code == 422
    configure(client, timezone="Europe/Moscow", telegram_token="10:synthetic")
    creator_id = ok(client.get("/api/auth/me"))["id"]
    with service.db.transaction() as conn:
        conn.execute("UPDATE memberships SET telegram_id='101' WHERE company_id=1 AND user_id=?", (creator_id,))
    source = ok(client.post(BASE + "/sources", json={"title": "Reference", "text": "Source text"}), 201)
    routine = ok(client.post(BASE + "/routines", json={**routine_payload, "source_ids": [source["id"]]}), 201)
    assert routine["notify_telegram"] is True
    user = add_person(client, EDITOR)
    with sign_in(client.app, EDITOR) as editor:
        edited = ok(editor.patch(f"{BASE}/routines/{routine['id']}", json={"name": "Revised ideas"}))
        assert edited["actor_id"] == user["id"]
        manual = ok(editor.post(f"{BASE}/routines/{routine['id']}/run"), 201)
        assert manual["actor_id"] == user["id"]
        assert manual["notify_telegram"] is True
    with service.db.transaction() as conn:
        conn.execute("DELETE FROM sources WHERE id=?", (source["id"],))
    current_time = datetime(2030, 3, 4, 6, 15, 25, tzinfo=timezone.utc)
    missed, = tasks.due_routines(service, 1, now=current_time)
    assert missed["routine_id"] == routine["id"]
    assert missed["status"] == "failed"
    assert missed["error"]
    assert tasks.due_routines(service, 1, now=current_time) == []
    candidate, = tasks.list_task_notifications(service, 1, 10)
    assert candidate["id"] == missed["id"]
    assert candidate["recipient_id"] == creator_id
    assert candidate["telegram_chat_id"] == "101"
    with service.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM activity WHERE action='assistant.routine_failed'").fetchone()[0] == 1


def test_telegram_replay_stale_prompt_and_current_bot_scope(api, monkeypatch):
    client, service = api
    configure(client, openai_key="mock-key")
    editor = add_person(client, {**EDITOR, "telegram_id": "123"})
    args = (service, 1, editor, "chat", "Original Telegram prompt")
    first = tasks.enqueue_task(*args, telegram_chat_id=123, telegram_bot_id=42, telegram_update_id=17)
    replay = tasks.enqueue_task(*args, telegram_chat_id=123, telegram_bot_id=42, telegram_update_id=17)
    assert replay["id"] == first["id"]
    assert replay["request_id"] == first["request_id"]
    assert len(tasks.list_telegram_tasks(service, 1, 42)) == 1
    assert tasks.list_telegram_tasks(service, 1, 99) == []
    provider(monkeypatch, {"reply": "An initial answer"})
    assert asyncio.run(tasks.process_tasks(service, 1))["status"] == "completed"

    args = (service, 1, first["id"], editor, "Telegram follow-up")
    latest = tasks.append_task_message(*args, telegram_bot_id=42, telegram_update_id=18,
                                       expected_request_id=first["request_id"])
    duplicate = tasks.append_task_message(*args, telegram_bot_id=42, telegram_update_id=18,
                                          expected_request_id=first["request_id"])
    assert latest["request_id"] != first["request_id"]
    assert duplicate["request_id"] == latest["request_id"]
    assert len(latest["messages"]) == 3
    assert asyncio.run(tasks.process_tasks(service, 1))["status"] == "completed"
    with sign_in(client.app, EDITOR) as editor_client:
        ok(editor_client.post(f"{BASE}/tasks/{first['id']}/messages", json={"text": "Moved to web"}))
    assert tasks.list_telegram_tasks(service, 1, 42) == []
