import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from server import integrations
from test_backend import api, configure, add_person, sign_in, EDITOR, SUPER
from test_integrations import mock_http


def test_models_use_provider_credentials_and_live_ids(mock_http):
    def response(request):
        if request.url.host == 'api.openai.com':
            return httpx.Response(200, json={'data': [{'id': name} for name in ['gpt-7-new', 'gpt-4.1-mini', 'gpt-7-new']]})
        return httpx.Response(200, json={'data': [{'id': name} for name in [
            'gpt://folder/aliceai-llm/latest', 'gpt://folder/qwen3.6-35b-a3b',
            'gpt://other/private-model', 'art://folder/image', 'emb://folder/text',
        ]]})
    requests = mock_http(response)
    for provider, expected in [('openai', ['gpt-4.1-mini', 'gpt-7-new']),
                               ('yandex', ['aliceai-llm/latest', 'qwen3.6-35b-a3b'])]:
        models = asyncio.run(integrations.list_models({'provider': provider, 'yandex_folder_id': 'folder'},
                                                     {'openai_key': 'sk-test', 'yandex_key': 'test-yandex'}))
        assert models == expected
    assert [str(r.url) for r in requests] == ['https://api.openai.com/v1/models', 'https://ai.api.cloud.yandex.net/v1/models']
    assert requests[0].headers['authorization'] == 'Bearer sk-test'
    assert requests[1].headers['authorization'] == 'Api-Key test-yandex'
    assert requests[1].headers['openai-project'] == 'folder'
    assert all(r.method == 'GET' and not r.content for r in requests)


def test_editorial_model_list_excludes_specialized_models(mock_http):
    kept = ['gpt-4o', 'gpt-4.1-mini', 'gpt-6.1-sol', 'gpt-7-mini', 'o3', 'chat-latest',
            'ft:gpt-4.1-mini:company:editor:abc']
    excluded = ['tts-1', 'gpt-tts', 'gpt-4o-mini-tts', 'gpt-realtime', 'gpt-realtime-2.1',
                'gpt-live-1', 'gpt-audio', 'gpt-4o-transcribe', 'gpt-image-2.6-flare',
                'chatgpt-image-latest', 'whisper-1', 'text-embedding-3-small', 'omni-moderation-latest',
                'gpt-5-search-api', 'gpt-5.3-codex', 'o3-deep-research', 'computer-use-preview',
                'ft:gpt-4o-mini-tts:company:audio:abc']
    mock_http(lambda _: httpx.Response(200, json={'data': [{'id': name} for name in kept + excluded]}))
    assert asyncio.run(integrations.list_models({'provider': 'openai'}, {'openai_key': 'sk-test'})) == sorted(kept)


def test_models_fallback_is_scoped(monkeypatch, mock_http, tmp_path):
    path = tmp_path / 'OpenAI.txt'
    path.write_text('sk-test-local')
    monkeypatch.setattr(integrations, '_OPENAI_KEY_FILE', path)
    requests = mock_http(lambda _: httpx.Response(200, json={'data': []}))
    with pytest.raises(integrations.IntegrationError, match='ключ OpenAI'):
        asyncio.run(integrations.list_models({'provider': 'openai'}, {}))
    assert not requests
    assert asyncio.run(integrations.list_models({'provider': 'openai', 'allow_openai_fallback': True}, {})) == []
    assert requests[0].headers['authorization'] == 'Bearer sk-test-local'


@pytest.mark.parametrize('result', [401, 429, {'data': 'wrong'}, {'data': [{'id': None}]}, []])
def test_models_errors_do_not_leak_credentials(mock_http, result):
    mock_http(lambda _: httpx.Response(result if isinstance(result, int) else 200,
                                      json={'error': 'sk-test rejected'} if isinstance(result, int) else result))
    with pytest.raises(integrations.IntegrationError) as caught:
        asyncio.run(integrations.list_models({'provider': 'openai'}, {'openai_key': 'sk-test'}))
    assert 'sk-test' not in str(caught.value)


def test_models_network_timeout(mock_http):
    def timeout(request):
        raise httpx.ReadTimeout('secret-header', request=request)
    mock_http(timeout)
    with pytest.raises(integrations.IntegrationError, match='подключение'):
        asyncio.run(integrations.list_models({'provider': 'openai'}, {'openai_key': 'sk-test'}))


def test_model_endpoint_permissions_and_provider_errors(api, monkeypatch):
    client, service = api
    calls = []
    async def listing(company, credentials):
        calls.append((company['id'], company['provider'], credentials['openai_key']))
        return ['live-model']
    monkeypatch.setattr(integrations, 'list_models', listing)
    configure(client, openai_key='company-one')
    assert client.patch('/api/companies/2', json={'openai_key': 'company-two'}).status_code == 200
    path = '/api/companies/1/models?provider=openai'
    assert TestClient(client.app).get(path).status_code == 401
    add_person(client, EDITOR)
    editor = sign_in(client.app, EDITOR)
    assert editor.get(path).status_code == 403
    assert editor.get('/api/companies/2/models?provider=openai').status_code == 404
    assert calls == []
    assert client.get(path).json() == ['live-model']
    owner = sign_in(client.app, SUPER)
    assert owner.get('/api/companies/2/models?provider=yandex').json() == ['live-model']
    assert calls == [(1, 'openai', 'company-one'), (2, 'yandex', 'company-two')]
    assert client.get('/api/companies/1/models?provider=unknown').status_code == 422
    async def rejected(*args):
        raise integrations.IntegrationError('Ключ отклонён.', status_code=401)
    monkeypatch.setattr(integrations, 'list_models', rejected)
    assert client.get(path).status_code == 502
    assert client.get('/api/auth/me').status_code == 200
