import pytest

from server import social_platforms
from test_backend import action, api, new_post, write


def connect(client, monkeypatch, platform, destination):
    result = client.patch(f'/api/companies/1/social/{platform}', json={
        'destination_id': destination, 'token': 'synthetic-token', 'enabled': True,
    })
    assert result.status_code == 200, result.text


@pytest.mark.parametrize('platform,destination', [('vk', '-123'), ('max', '-456')])
def test_platform_publication_and_approval_invalidation(api, monkeypatch, platform, destination):
    client, service = api
    connect(client, monkeypatch, platform, destination)
    path, post = new_post(client)
    post = write(client, path, post, body='Проверяем формат публикаций.', platform=platform)
    assert post['channel_id'] == destination
    assert post['created_by'] is not None
    post = action(client, path, 'submit', post)
    post = action(client, path, 'approve', post)
    calls = []

    async def publish(selected, connection, body, media):
        calls.append((selected, connection['destination_id'], body))
        return {'message_id': '-123_7' if selected == 'vk' else 'message-7'}

    monkeypatch.setattr(social_platforms, 'publish_social', publish)
    # The destination is part of the approved version, not silently changed from settings.
    other = '-124' if platform == 'vk' else '-457'
    connect(client, monkeypatch, platform, other)
    denied = client.post(path + '/publish', json={'version': post['version']})
    assert denied.status_code == 409 and not calls
    post = write(client, path, post, channel_id=other)
    assert post['status'] == 'draft'
    post = action(client, path, 'submit', post)
    post = action(client, path, 'approve', post)
    sent = action(client, path, 'publish', post)
    assert sent['status'] == 'published'
    assert calls == [(platform, other, 'Проверяем формат публикаций.')]
    assert client.post(path + '/publish', json={'version': sent['version']}).status_code == 409


def test_social_unknown_delivery_locks_post(api, monkeypatch):
    client, _ = api
    connect(client, monkeypatch, 'vk', '-123')
    path, post = new_post(client)
    post = write(client, path, post, body='Тест.', platform='vk')
    post = action(client, path, 'submit', post)
    post = action(client, path, 'approve', post)

    async def publish(*args):
        raise social_platforms.SocialPublicationError('Обрыв соединения.', uncertain=True)

    monkeypatch.setattr(social_platforms, 'publish_social', publish)
    response = client.post(path + '/publish', json={'version': post['version']})
    assert response.status_code == 502
    locked = client.get(path).json()
    assert locked['publication_uncertain'] and locked['status'] == 'failed'
    assert client.patch(path, json={'version': locked['version'], 'body': 'Другой текст'}).status_code == 409


def test_platform_switch_discards_approval(api, monkeypatch):
    client, _ = api
    connect(client, monkeypatch, 'vk', '-123')
    path, post = new_post(client)
    post = write(client, path, post, body='Тест.')
    post = action(client, path, 'submit', post)
    post = action(client, path, 'approve', post)
    post = write(client, path, post, platform='vk')
    assert post['status'] == 'draft' and post['platform'] == 'vk'
    assert client.post(path + '/publish', json={'version': post['version']}).status_code == 409
