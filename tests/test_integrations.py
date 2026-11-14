import asyncio
import json

import httpx
import pytest

from server import integrations


TOKEN = "123:fixture_token"
COMPANY = {"name": "Тестовая компания", "provider": "openai", "calendar_enabled": True}
SOURCE = {"id": 7, "text": "Турнир состоится 20 ноября 2026 года.", "kind": "text"}
FACT = {
    "text": "Турнир состоится 20 ноября.",
    "source_ids": [7],
    "evidence": [{"source_id": 7, "quote": SOURCE["text"]}],
}
EVENT = {
    "title": "Турнир", "event_date": "2026-11-20", "description": "",
    "source_id": 7, "evidence": SOURCE["text"],
}


@pytest.fixture
def mock_http(monkeypatch):
    original_client = httpx.AsyncClient

    def install(handler):
        requests = []

        async def capture(request):
            requests.append(request)
            return handler(request)

        def client(**kwargs):
            kwargs.pop('proxy', None)  # Never route synthetic requests through a real local proxy.
            return original_client(transport=httpx.MockTransport(capture), **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", client)
        return requests

    return install


def draft_response(**changes):
    draft = {"title": "Турнир", "body": FACT["text"], "questions": [], "facts": [FACT], "events": [EVENT]}
    draft.update(changes)
    return {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(draft)}]}],
        "usage": {"input_tokens": 321, "output_tokens": 45},
    }


def generate(company=COMPANY, sources=None, credentials=None):
    return asyncio.run(integrations.generate_post(
        company, [SOURCE] if sources is None else sources, "Инструкция редактору.",
        {"openai_key": "sk-test"} if credentials is None else credentials,
    ))


def test_openai_sends_a_schema_and_accepts_exact_grounding(mock_http):
    requests = mock_http(lambda _: httpx.Response(200, json=draft_response()))
    draft = generate()
    assert draft["events"] == [EVENT]
    assert draft["facts"] == [FACT]
    assert (draft["input_tokens"], draft["output_tokens"]) == (321, 45)
    assert requests[0].url == "https://api.openai.com/v1/responses"
    assert requests[0].headers["authorization"] == "Bearer sk-test"
    payload = json.loads(requests[0].content)
    assert payload["store"] is False
    assert payload["text"]["format"]["strict"] is True
    assert "Инструкция редактору" in payload["instructions"]
    assert SOURCE["text"] in payload["input"][0]["content"][1]["text"]


def test_connection_failure_retries_but_read_timeout_does_not(mock_http):
    calls = 0
    def connect_then_success(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError('connection did not open', request=request)
        return httpx.Response(200, json=draft_response())
    requests = mock_http(connect_then_success)
    assert generate()['body'] == FACT['text']
    assert len(requests) == 2

    def no_response(request):
        raise httpx.ReadTimeout('request may have reached provider', request=request)
    requests = mock_http(no_response)
    with pytest.raises(integrations.GenerationError):
        generate()
    assert len(requests) == 1


def test_yandex_receives_image_and_keeps_questions(mock_http, tmp_path):
    image = tmp_path / "poster.png"
    image.write_bytes(b"mock-image")
    source = {"id": 9, "kind": "image", "mime_type": "image/png", "file_path": image}
    response = draft_response(
        questions=["Какое время начала?"],
        facts=[{"text": "Турнир состоится.", "source_ids": [9], "evidence": [
            {"source_id": 9, "quote": "Турнир"},
        ]}],
    )
    response["usage"] = {"prompt_tokens": 10, "completion_tokens": 2}
    requests = mock_http(lambda _: httpx.Response(200, json=response))
    draft = generate(
        {**COMPANY, "provider": "yandex", "yandex_folder_id": "test-folder"},
        [source],
        {"yandex_key": "fixture-key"},
    )
    assert draft["questions"][0] == "Какое время начала?"
    assert "№9" in draft["questions"][1]
    assert draft["events"] == []
    assert draft["input_tokens"] == 10
    assert requests[0].url == "https://ai.api.cloud.yandex.net/v1/responses"
    assert requests[0].headers["openai-project"] == "test-folder"
    assert requests[0].headers["authorization"] == "Api-Key fixture-key"
    payload = json.loads(requests[0].content)
    assert payload["model"] == "gpt://test-folder/qwen3.6-35b-a3b"
    assert payload["input"][0]["content"][-1]["image_url"] == "data:image/png;base64,bW9jay1pbWFnZQ=="
    assert str(image) not in requests[0].content.decode()


@pytest.mark.parametrize(
    "changes",
    [
        {"facts": [{**FACT, "source_ids": [99]}]},
        {"facts": [{**FACT, "evidence": [{"source_id": 7, "quote": "Турнир перенесён."}]}]},
        {"events": [{**EVENT, "event_date": "2026-11-21"}]},
        {"events": [{**EVENT, "event_date": "20261120"}]},
    ],
)
def test_invalid_grounding_is_rejected(mock_http, changes):
    mock_http(lambda _: httpx.Response(200, json=draft_response(**changes)))
    with pytest.raises(integrations.GenerationError) as error:
        generate()
    assert (error.value.input_tokens, error.value.output_tokens) == (321, 45)


def test_malformed_model_draft_keeps_known_usage(mock_http):
    mock_http(lambda _: httpx.Response(200, json=draft_response(questions=8)))
    with pytest.raises(integrations.GenerationError, match="неожиданном формате") as error:
        generate()
    assert (error.value.input_tokens, error.value.output_tokens) == (321, 45)


def test_openai_local_key_only_used_when_explicitly_allowed(monkeypatch, mock_http, tmp_path):
    key_path = tmp_path / "OpenAI.txt"
    key_path.write_text('OPENAI_API_KEY="sk-fixture-local"\n', encoding="utf-8-sig")
    monkeypatch.setattr(integrations, "_OPENAI_KEY_FILE", key_path)
    requests = mock_http(lambda _: httpx.Response(200, json=draft_response()))
    with pytest.raises(integrations.GenerationError, match="Укажите ключ OpenAI"):
        generate(credentials={})
    assert not requests
    generate({**COMPANY, "allow_openai_fallback": True}, credentials={})
    generate({**COMPANY, "allow_openai_fallback": True}, credentials={"openai_key": "sk-override"})
    assert [req.headers["authorization"] for req in requests] == [
        "Bearer sk-fixture-local", "Bearer sk-override",
    ]


def test_cloud_failure_omits_provider_response_and_credentials(mock_http):
    mock_http(lambda _: httpx.Response(401, json={"error": {"message": "sk-test: rejected"}}))
    with pytest.raises(integrations.GenerationError) as result:
        generate()
    assert result.value.status_code == 401
    assert "sk-test" not in str(result.value)


def test_publish_telegram_plain_text(mock_http, caplog):
    caplog.set_level("INFO", logger="httpx")
    requests = mock_http(lambda _: httpx.Response(200, json={
        "ok": True, "result": {"message_id": 12, "chat": {"id": -10012345}},
    }))
    published = asyncio.run(integrations.publish_telegram(TOKEN, "-10012345", "Открытие 🏐", []))
    assert published == {"message_id": 12, "publication_url": "https://t.me/c/12345/12"}
    assert requests[0].url.path.endswith("/sendMessage")
    assert json.loads(requests[0].content)["text"] == "Открытие 🏐"
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("mime,method", [("image/png", "sendPhoto"), ("video/mp4", "sendVideo")])
def test_publish_telegram_single_attachment(mock_http, tmp_path, mime, method):
    file = tmp_path / "media"
    file.write_bytes(b"sample contents")
    requests = mock_http(lambda _: httpx.Response(200, json={"ok": True, "result": {"message_id": 2}}))
    published = asyncio.run(integrations.publish_telegram(
        TOKEN, "@channel", "🏐" * 1024, [{"mime_type": mime, "file_path": file}],
    ))
    assert published["publication_url"] == "https://t.me/channel/2"
    assert requests[0].url.path.endswith("/" + method)
    assert b"sample contents" in requests[0].content
    assert b'name="caption"' in requests[0].content


def test_publish_telegram_mixed_album_uses_one_request(mock_http, tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(b"photo-data")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video-data")
    requests = mock_http(lambda _: httpx.Response(200, json={
        "ok": True, "result": [{"message_id": 1}, {"message_id": 2}],
    }))
    result = asyncio.run(integrations.publish_telegram(
        TOKEN, "@channel", "Общий текст",
        [{"file_path": image, "mime_type": "image/png"}, {"file_path": video, "mime_type": "video/mp4"}],
    ))
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/sendMediaGroup")
    content = requests[0].content
    assert all(part in content for part in (
        b"attach://media0", b"attach://media1", b"photo-data", b"video-data", "Общий текст".encode(),
    ))
    assert result["message_id"] == 1


def test_telegram_timeout_is_uncertain_and_never_retried(mock_http):
    def timeout(request):
        raise httpx.ReadTimeout(f"Timeout posting to {request.url}", request=request)

    requests = mock_http(timeout)
    with pytest.raises(integrations.TelegramError) as error:
        asyncio.run(integrations.publish_telegram(TOKEN, "@channel", "Открытие", []))
    assert error.value.uncertain
    assert TOKEN not in str(error.value)
    assert len(requests) == 1


def test_telegram_rejects_long_caption_before_sending(mock_http):
    requests = mock_http(lambda _: pytest.fail("No HTTP request expected"))
    with pytest.raises(integrations.TelegramError, match="1024") as error:
        asyncio.run(integrations.publish_telegram(TOKEN, "@channel", "x" * 1025, [{}]))
    assert not error.value.uncertain
    assert not requests


def test_validate_publication_rejects_missing_attachment(tmp_path):
    media = [{"mime_type": "image/png", "file_path": tmp_path / "missing.png"}]
    with pytest.raises(integrations.TelegramError, match="открыть вложение"):
        integrations.validate_publication("Открытие", media)


def test_addressing_requires_matching_utf16_entity():
    message = {
        "caption": "🏐 @edit_bot добрый день",
        "caption_entities": [{"type": "mention", "offset": 3, "length": 9}],
    }
    assert integrations.message_is_addressed(message, "@Edit_Bot")
    assert not integrations.message_is_addressed(message, "@another_bot")
    assert not integrations.message_is_addressed({"caption": message["caption"]}, "edit_bot")
    message["caption_entities"][0]["offset"] = 1
    assert not integrations.message_is_addressed(message, "edit_bot")
    assert integrations.message_is_addressed({
        "text": "Редактор",
        "entities": [{"type": "text_mention", "offset": 0, "length": 8, "user": {"id": 33}}],
    }, "edit_bot", 33)
