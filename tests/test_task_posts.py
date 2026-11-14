import asyncio
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from PIL import Image, ImageDraw

from server import assistant_tasks as tasks, integrations
from server.brand_images import LogoPlacement
from test_assistant_tasks import BASE, add_task, ok  # noqa: F401
from test_backend import api, configure  # noqa: F401


REQUEST = "Создай пост с темой номер 5 и сгенерируй фото для поста"
TOPICS = ("1. День охраны труда\n2. Советы новичкам\n3. Как работает склад\n"
          "4. Бережное отношение к оборудованию\n5. Over Size: история большого формата")
EVIDENCE = "Первый лист Over Size размером 12 000 × 3 210 мм выпущен в июле 2022 года."
BODY = "Большой формат\n\n" + EVIDENCE


def arrange(api, monkeypatch, *, allow_image=True, valid_evidence=True):
    client, service = api
    configure(client, openai_key="mock-key", telegram_token="1:telegram", requests_limit=25)
    material = ok(client.post(BASE + "/knowledge", json={
        "title": "История большого формата", "text": EVIDENCE, "category": "business", "status": "active",
    }), 201)
    source_id = material["source_id"]
    calls = []

    async def text_provider(endpoint, headers, payload, timeout):
        request = json.loads(payload["input"][0]["content"])
        schema = payload["text"]["format"]["name"]
        calls.append(schema)
        if schema == "assistant_answer":
            text = {"answer_type": "proposal", "reply": TOPICS, "questions": [], "facts": [], "proposals": []}
        else:
            assert schema == "assistant_post"
            assert request["request"] == REQUEST
            assert request["topic_from_history"] == "Over Size: история большого формата"
            assert any(TOPICS in item["text"] for item in request["conversation"])
            assert request["with_image"] and request["caption_limit"] == 1024
            assert [source["id"] for source in request["materials"]] == [source_id]
            text = {
                "title": "Over Size: история большого формата", "body": BODY, "questions": [],
                "image_brief": "Недокументальная иллюстрация: большой лист стекла.",
                "contains_company_facts": True,
                "facts": [{"text": EVIDENCE, "source_ids": [source_id],
                           "evidence": [{"source_id": source_id, "quote": EVIDENCE if valid_evidence else TOPICS}]}],
            }
        return httpx.Response(200, json={"status": "completed", "output_text": json.dumps(text),
                                        "usage": {"input_tokens": 100, "output_tokens": 20}})

    brand = service.db.path.parent / "brands" / "1"
    brand.mkdir(parents=True)
    for name, colour in (("light", "white"), ("dark", "black")):
        logo = Image.new("RGBA", (100, 25))
        ImageDraw.Draw(logo).rectangle((3, 2, 94, 21), fill=colour)
        logo.save(brand / f"{name}.png")
    clean = io.BytesIO()
    Image.new("RGB", (320, 240), "grey").save(clean, "PNG")

    async def image_provider(endpoint, headers, payload, timeout):
        calls.append("image")
        assert payload["tools"][0]["type"] == "image_generation"
        assert "лист стекла" in payload["input"][0]["content"][0]["text"]
        if not allow_image and calls.count("image") == 1:
            return httpx.Response(429)
        from base64 import b64encode

        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "image_generation_call", "status": "completed",
             "result": b64encode(clean.getvalue()).decode("ascii")},
        ]})

    async def place_logo(*_):
        return LogoPlacement(variant="dark", corner="bottom_right", width=0.35), 100, 20

    monkeypatch.setattr(tasks, "_post_generation", text_provider)
    monkeypatch.setattr(integrations, "_post_generation", image_provider)
    monkeypatch.setattr(integrations, "_logo_placement", place_logo)
    publish = AsyncMock(side_effect=AssertionError("No publication before approval"))
    monkeypatch.setattr(integrations, "publish_telegram", publish)
    with service.db.transaction() as connection:
        actor = connection.execute("SELECT user_id AS id FROM memberships WHERE company_id=1 AND role='admin'").fetchone()
        connection.execute("UPDATE memberships SET telegram_id='42' WHERE company_id=1 AND user_id=?", (actor["id"],))
    actor = {"id": actor["id"], "telegram_id": "42"}
    topic = tasks.enqueue_task(service, 1, actor, "topics", "Предложи темы постов", telegram_chat_id="42",
                               telegram_bot_id="1", telegram_update_id=100)
    prior = asyncio.run(tasks.process_tasks(service, 1))
    assert prior["status"] == "completed" and not prior["post_id"] and TOPICS in prior["reply"]
    pending = tasks.append_task_message(service, 1, topic["id"], actor, REQUEST, telegram_chat_id="42",
                                        telegram_bot_id="1", telegram_update_id=101)
    return service, actor, topic, pending, calls, publish, clean.getvalue(), source_id


def test_telegram_numbered_topic_becomes_a_branded_post_awaiting_review(api, monkeypatch):
    service, actor, topic, queued, calls, publish, clean, source_id = arrange(api, monkeypatch)
    staged = []

    async def image_stage(item):
        assert item["status"] == "running" and item["request_id"] == queued["request_id"]
        post = service.post(1, item["post_id"])
        assert post["status"] == "draft" and post["body"] == BODY and not post["photo_url"]
        staged.append(item["post_id"])

    result = asyncio.run(tasks.process_tasks(service, 1, on_post_image=image_stage))
    assert result["id"] == topic["id"] and result["status"] == "completed", result
    assert staged == [result["post_id"]]
    post = service.post(1, result["post_id"])
    assert post["status"] == "review" and post["version"] == result["post_version"]
    assert post["body"] == BODY and not post["questions"] and post["source_ids"] == [source_id]
    assert post["logo"]["corner"] == "bottom_right" and not post["logo_pending"]
    media, = service.publication_media(1, post)
    assert Path(media["file_path"]).read_bytes() != clean
    assert calls == ["assistant_answer", "assistant_post", "image"]
    publish.assert_not_awaited()
    repeated = tasks.append_task_message(service, 1, topic["id"], actor, REQUEST, telegram_chat_id="42",
                                         telegram_bot_id="1", telegram_update_id=101)
    assert repeated["post_id"] == result["post_id"]
    assert asyncio.run(tasks.process_tasks(service, 1)) is None
    assert len(service.posts(1)) == 1


def test_failed_photo_retry_reuses_text_and_publication(api, monkeypatch):
    service, actor, topic, _, calls, publish, _, _ = arrange(api, monkeypatch, allow_image=False)
    first = asyncio.run(tasks.process_tasks(service, 1))
    assert first["status"] == "failed" and first["post_id"], first
    draft = service.post(1, first["post_id"])
    assert draft["status"] == "draft" and draft["body"] == BODY and not draft["photo_url"]
    tasks._retry(service, 1, topic["id"], actor)
    second = asyncio.run(tasks.process_tasks(service, 1))
    assert second["status"] == "completed" and second["post_id"] == first["post_id"], second
    assert service.post(1, first["post_id"])["status"] == "review"
    assert calls == ["assistant_answer", "assistant_post", "image", "image"]
    assert len(service.posts(1)) == 1
    publish.assert_not_awaited()


def test_earlier_topic_list_cannot_substitute_for_material_evidence(api, monkeypatch):
    service, _, _, _, calls, _, _, _ = arrange(api, monkeypatch, valid_evidence=False)
    result = asyncio.run(tasks.process_tasks(service, 1))
    assert result["status"] == "needs_info" and result["questions"] and result["post_id"] is None, result
    assert not service.posts(1)
    assert calls == ["assistant_answer", "assistant_post"]


def test_evidence_edit_during_image_request_blocks_review(api, monkeypatch):
    service, _, _, _, calls, _, _, source_id = arrange(api, monkeypatch)
    place_logo = integrations._logo_placement

    async def update_evidence(*args):
        with service.db.transaction() as connection:
            connection.execute("UPDATE sources SET text='Обновлённый материал.' WHERE id=?", (source_id,))
        return await place_logo(*args)

    monkeypatch.setattr(integrations, "_logo_placement", update_evidence)
    result = asyncio.run(tasks.process_tasks(service, 1))
    assert result["status"] == "failed" and result["post_id"], result
    post = service.post(1, result["post_id"])
    assert post["status"] == "draft" and post["version"] == result["post_version"]
    assert calls == ["assistant_answer", "assistant_post", "image"]


def test_restart_pins_partial_post_version_without_replaying_generation(api, monkeypatch):
    service, _, _, _, calls, _, _, _ = arrange(api, monkeypatch)

    async def restart(item):
        assert item["post_id"] and item["post_version"] is None
        tasks.initialize_tasks(service)

    result = asyncio.run(tasks.process_tasks(service, 1, on_post_image=restart))
    assert result["status"] == "interrupted" and result["post_id"], result
    post = service.post(1, result["post_id"])
    assert post["status"] == "draft" and post["version"] == result["post_version"]
    assert asyncio.run(tasks.process_tasks(service, 1)) is None
    assert calls == ["assistant_answer", "assistant_post"]
