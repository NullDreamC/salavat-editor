import asyncio

import pytest

from server import integrations
from server.domain import DomainError
from test_backend import api, configure, new_post, write


def claim_status(service, post):
    with service.db.read() as connection:
        return connection.execute(
            "SELECT status FROM post_generation_claims WHERE company_id=? AND post_id=? AND version=?",
            (1, post["id"], post["version"]),
        ).fetchone()["status"]


def test_concurrent_post_generation_reserves_once(api, monkeypatch):
    client, service = api
    configure(client, openai_key='synthetic-key')
    _, post = new_post(client)
    calls = []

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def generate(company, sources, instruction, credentials):
            calls.append(True)
            started.set()
            await release.wait()
            return {'title': 'Проверка', 'body': '', 'questions': ['Что сообщить?'],
                    'facts': [], 'events': [], 'input_tokens': 5, 'output_tokens': 3}

        monkeypatch.setattr(integrations, 'generate_post', generate)
        first = asyncio.create_task(service.generate(1, post['id'], post['version']))
        await started.wait()
        with pytest.raises(DomainError) as error:
            await service.generate(1, post['id'], post['version'])
        assert error.value.status_code == 409
        release.set()
        result = await first
        assert result['status'] == 'needs_info'

    asyncio.run(scenario())
    assert len(calls) == 1
    assert service.usage(1)['requests'] == 1


def test_explicit_retry_after_provider_failure_is_allowed(api, monkeypatch):
    client, service = api
    configure(client, openai_key='synthetic-key')
    _, post = new_post(client)

    async def failure(*args):
        raise integrations.GenerationError('Нет ответа провайдера.')

    monkeypatch.setattr(integrations, 'generate_post', failure)
    for _ in range(2):
        with pytest.raises(DomainError):
            asyncio.run(service.generate(1, post['id'], post['version']))
    assert service.usage(1)['requests'] == 2


@pytest.mark.parametrize("attach_knowledge", [False, True])
def test_knowledge_archived_during_generation_requires_review(api, monkeypatch, attach_knowledge):
    client, service = api
    configure(client, openai_key="synthetic-key", channel_id="@test_channel")
    actor = client.get("/api/auth/me").json()
    post_path, post = new_post(client)
    material_id = post["source_ids"][0]
    knowledge_path = "/api/companies/1/knowledge"
    created = client.post(knowledge_path, json={
        "title": "Meeting details", "category": "business", "status": "active",
        "text": "The meeting takes place on 2027-06-14.",
    })
    assert created.status_code == 201, created.text
    entry = created.json()
    knowledge_id = entry["source_id"]
    if attach_knowledge:
        post = write(client, post_path, post, source_ids=[material_id, knowledge_id])
    seen_sources = []

    async def generate(company, sources, instruction, credentials):
        seen_sources.append([source["id"] for source in sources])
        material = sources[0]
        material_evidence = {"source_id": material_id, "quote": material["text"]}
        facts = [{"text": "The meeting was announced.", "source_ids": [material_id],
                  "evidence": [material_evidence]}]
        events = []
        if len(seen_sources) == 1:
            archived = client.delete(f"{knowledge_path}/{entry['id']}")
            assert archived.status_code == 204, archived.text
            knowledge_evidence = {"source_id": knowledge_id, "quote": entry["text"]}
            facts.extend([
                {"text": "The date appears in the company reference.", "source_ids": [knowledge_id],
                 "evidence": [knowledge_evidence]},
                {"text": "Both materials confirm the date.", "source_ids": [material_id, knowledge_id],
                 "evidence": [material_evidence, knowledge_evidence]},
            ])
            events.append({"source_id": knowledge_id, "title": "The meeting", "event_date": "2027-06-14",
                           "evidence": entry["text"], "description": ""})
        return {"title": "The meeting", "body": "The meeting is scheduled for June 14.", "questions": [],
                "facts": facts, "events": events, "input_tokens": 5, "output_tokens": 3}

    monkeypatch.setattr(integrations, "generate_post", generate)
    generated = asyncio.run(service.generate(1, post["id"], post["version"], actor=actor))
    assert generated["status"] == "needs_info"
    assert any("База знаний изменилась" in question for question in generated["questions"])
    assert [fact["source_ids"] for fact in generated["facts"]] == [[material_id]]
    assert claim_status(service, post) == "completed"
    assert seen_sources == [[material_id, knowledge_id]]
    assert service.usage(1)["requests"] == 1
    assert service.events(1) == []
    with pytest.raises(DomainError) as error:
        service.submit(1, generated["id"], generated["version"], actor=actor)
    assert error.value.status_code == 409

    if attach_knowledge:
        with pytest.raises(DomainError) as error:
            asyncio.run(service.generate(1, generated["id"], generated["version"], actor=actor))
        assert error.value.status_code == 422
        generated = write(client, post_path, generated, source_ids=[material_id])
    retried = asyncio.run(service.generate(1, generated["id"], generated["version"], actor=actor))
    assert retried["status"] == "draft"
    assert retried["questions"] == []
    assert claim_status(service, generated) == "completed"
    assert seen_sources == [[material_id, knowledge_id], [material_id]]
    assert service.usage(1)["requests"] == 2


def test_result_write_failure_releases_claim_and_preserves_usage(api, monkeypatch):
    client, service = api
    configure(client, openai_key="synthetic-key")
    _, post = new_post(client)
    actor = client.get("/api/auth/me").json()
    calls = []

    async def generate(company, sources, instruction, credentials):
        calls.append(True)
        return {"title": "Отложенная встреча", "body": "", "questions": ["Когда пройдёт встреча?"],
                "facts": [], "events": [], "input_tokens": 5, "output_tokens": 3}

    update_post = service._update_post

    def fail_after_writing(*args, **kwargs):
        update_post(*args, **kwargs)
        raise RuntimeError("Result could not be saved")

    monkeypatch.setattr(integrations, "generate_post", generate)
    with monkeypatch.context() as failing:
        failing.setattr(service, "_update_post", fail_after_writing)
        with pytest.raises(RuntimeError, match="Result could not be saved"):
            asyncio.run(service.generate(1, post["id"], post["version"], actor=actor))

    assert service.post(1, post["id"]) == post
    assert claim_status(service, post) == "failed"
    assert len(calls) == 1
    usage = service.usage(1)
    assert (usage["requests"], usage["input_tokens"], usage["output_tokens"]) == (1, 5, 3)

    retried = asyncio.run(service.generate(1, post["id"], post["version"], actor=actor))
    assert retried["status"] == "needs_info"
    assert claim_status(service, post) == "completed"
    assert len(calls) == 2
