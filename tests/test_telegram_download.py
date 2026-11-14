import asyncio

import httpx
import pytest

from server import worker as worker_module
from server.domain import DomainError
from server.integrations import TelegramError
from test_worker import worker, COMPANY, EDITOR


PHOTO = {'photo': [{'file_id': 'fixture-file'}]}


def transport(worker, monkeypatch, handler):
    bot, call = worker
    call.return_value = {'file_path': 'photos/test.jpg'}
    client = httpx.AsyncClient
    options = []
    def make_client(**kwargs):
        options.append(dict(kwargs))
        kwargs.pop('proxy', None)
        return client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(worker_module.httpx, 'AsyncClient', make_client)
    monkeypatch.setattr(worker_module, 'http_options', lambda: {'proxy': 'http://proxy.invalid:8000'})
    return bot, call, options


def test_transient_connect_error_retries_file_without_repeating_storage(worker, monkeypatch):
    requests = []
    def response(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError('synthetic connection failure', request=request)
        return httpx.Response(200, content=b'complete-photo')
    bot, call, options = transport(worker, monkeypatch, response)
    asyncio.run(bot.download(COMPANY, 'test-token', PHOTO, EDITOR))
    assert len(requests) == 2
    assert options[0].get('proxy') and 'proxy' not in options[1]
    bot.service.store_upload.assert_called_once_with(1, 'test.jpg', b'complete-photo', 'image/jpeg', actor=EDITOR)


def test_permanent_network_failure_is_bounded_and_safe(worker, monkeypatch):
    def response(request):
        raise httpx.ConnectError('secret-token-url', request=request)
    bot, call, options = transport(worker, monkeypatch, response)
    with pytest.raises(DomainError) as raised:
        asyncio.run(bot.download(COMPANY, 'test-token', PHOTO, EDITOR))
    assert len(options) == 3
    assert 'скачать' in raised.value.detail
    assert 'secret' not in raised.value.detail
    bot.service.store_upload.assert_not_called()


def test_not_found_does_not_loop_or_store(worker, monkeypatch):
    bot, call, options = transport(worker, monkeypatch, lambda request: httpx.Response(404))
    with pytest.raises(DomainError):
        asyncio.run(bot.download(COMPANY, 'test-token', PHOTO, EDITOR))
    assert len(options) == 1
    bot.service.store_upload.assert_not_called()


def test_read_failure_discards_partial_bytes(worker, monkeypatch):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'partial-'
            raise httpx.ReadError('synthetic read failure')
    count = 0
    def response(request):
        nonlocal count
        count += 1
        return httpx.Response(200, stream=BrokenStream()) if count == 1 else httpx.Response(200, content=b'whole-photo')
    bot, call, options = transport(worker, monkeypatch, response)
    asyncio.run(bot.download(COMPANY, 'test-token', PHOTO, EDITOR))
    assert bot.service.store_upload.call_args.args[2] == b'whole-photo'
    bot.service.store_upload.assert_called_once()


def test_read_only_file_lookup_is_retried(worker, monkeypatch):
    bot, call, options = transport(worker, monkeypatch, lambda request: httpx.Response(200, content=b'photo'))
    call.side_effect = [TelegramError('unavailable'), {'file_path': 'photos/test.jpg'}]
    asyncio.run(bot.download(COMPANY, 'test-token', PHOTO, EDITOR))
    assert call.await_count == 2
    assert all(entry.args[1] == 'getFile' for entry in call.await_args_list)
    bot.service.store_upload.assert_called_once()


def test_oversized_download_is_not_retried(worker, monkeypatch):
    class HugeStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(21):
                yield b'x' * 1024 * 1024
    bot, call, options = transport(worker, monkeypatch, lambda request: httpx.Response(200, stream=HugeStream()))
    with pytest.raises(DomainError) as raised:
        asyncio.run(bot.download(COMPANY, 'test-token', PHOTO, EDITOR))
    assert raised.value.status_code == 413
    assert len(options) == 1
    bot.service.store_upload.assert_not_called()


def test_server_error_is_retried_without_following_redirects(worker, monkeypatch):
    count = 0
    def response(request):
        nonlocal count
        count += 1
        return httpx.Response(503) if count == 1 else httpx.Response(200, content=b'photo')
    bot, call, options = transport(worker, monkeypatch, response)
    asyncio.run(bot.download(COMPANY, 'test-token', PHOTO, EDITOR))
    assert count == 2
    assert all(option['follow_redirects'] is False for option in options)
    bot.service.store_upload.assert_called_once()
