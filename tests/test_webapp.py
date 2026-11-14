import json

from fastapi.testclient import TestClient

from server.db import Database
from server.main import create_app, COOKIE
from server.webapp import public_url
from server.tunnel_watch import synchronize


def test_tunnel_domain_with_app_subdomain(tmp_path):
    (tmp_path / 'ssh-tunnel.log').write_text('https://old.lhr.life\nhttps://new123.app.lhr.life')
    synchronize(tmp_path)
    assert json.loads((tmp_path / 'webapp.json').read_text())['url'] == 'https://new123.app.lhr.life'
from test_backend import SUPER


def test_webapp_host_cookie_and_cross_origin_protection(tmp_path):
    db = Database(tmp_path / 'app.sqlite3')
    (tmp_path / 'webapp.json').write_text(json.dumps({'url': 'https://test.example'}))
    assert public_url(db) == 'https://test.example'
    app = create_app(db)
    with TestClient(app, base_url='https://test.example') as client:
        response = client.post('/api/auth/setup', json=SUPER, headers={'Origin': 'https://test.example'})
        assert response.status_code == 200
        cookie = response.headers['set-cookie']
        assert 'SameSite=none' in cookie and 'Secure' in cookie and 'HttpOnly' in cookie
        assert client.get('/api/auth/me').status_code == 200
        assert 'https://web.telegram.org' in response.headers['content-security-policy']
        assert 'x-frame-options' not in response.headers
        rejected = client.post('/api/auth/logout', headers={'Origin': 'https://malicious.example'})
        assert rejected.status_code == 403
        assert client.get('/api/auth/me').status_code == 200
        assert client.get('/api/companies', headers={'Host': 'unknown.example'}).status_code == 400
        assert client.get('/api/auth/me', headers={'Cookie': COOKIE + '=unknown'}).status_code == 401
        client.cookies.clear()
        payload = {'email': SUPER['email'], 'password': 'wrong'}
        for _ in range(15):
            assert client.post('/api/auth/login', json=payload).status_code == 401
        assert client.post('/api/auth/login', json=payload).status_code == 429


def test_invalid_webapp_config_never_expands_allowed_hosts(tmp_path):
    db = Database(tmp_path / 'app.sqlite3')
    for value in ('http://test.example', 'https://name:password@test.example', 'https://test.example/unexpected'):
        (tmp_path / 'webapp.json').write_text(json.dumps({'url': value}))
        assert public_url(db) == ''


def test_login_through_https_tunnel_without_forwarded_proto(tmp_path):
    db = Database(tmp_path / 'app.sqlite3')
    (tmp_path / 'webapp.json').write_text(json.dumps({'url': 'https://test.example'}))
    with TestClient(create_app(db), base_url='https://test.example') as client:
        assert client.post('/api/auth/setup', json=SUPER).status_code == 200
        client.cookies.clear()
        # localhost.run terminates TLS but sends plain HTTP without proxy headers.
        headers = {'Origin': 'https://test.example', 'Sec-Fetch-Site': 'same-origin'}
        response = client.post('http://test.example/api/auth/login', headers=headers,
                               json={'email': SUPER['email'], 'password': SUPER['password']})
        assert response.status_code == 200, response.text
        cookie = response.headers['set-cookie']
        assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=none' in cookie
        assert client.get('/api/auth/me').status_code == 200
        for origin in ('http://test.example', 'https://malicious.example', 'https://test.example.malicious.example'):
            response = client.post('http://test.example/api/auth/logout', headers={'Origin': origin})
            assert response.status_code == 403
        assert client.post('http://test.example/api/auth/logout', headers={
            **headers, 'Sec-Fetch-Site': 'cross-site',
        }).status_code == 403
        assert client.get('/api/auth/me').status_code == 200
        response = client.post('http://test.example/api/auth/logout', headers={
            **headers, 'Cookie': f'{COOKIE}={client.cookies.get(COOKIE)}',
        })
        assert response.status_code == 204
        assert 'Secure' in response.headers['set-cookie'] and 'SameSite=none' in response.headers['set-cookie']
        assert client.get('/api/auth/me').status_code == 401


def test_proxy_headers_cannot_authorize_an_unconfigured_origin(tmp_path):
    db = Database(tmp_path / 'app.sqlite3')
    (tmp_path / 'webapp.json').write_text(json.dumps({'url': 'https://test.example'}))
    with TestClient(create_app(db), base_url='https://test.example') as client:
        response = client.post('/api/auth/setup', json=SUPER, headers={
            'Origin': 'https://malicious.example', 'X-Forwarded-Host': 'malicious.example',
            'X-Forwarded-Proto': 'https',
        })
        assert response.status_code == 403


def test_rotated_tunnel_replaces_only_allowed_host_without_server_restart(tmp_path):
    db = Database(tmp_path / 'app.sqlite3')
    log = tmp_path / 'ssh-tunnel.log'
    log.write_text('https://first.lhr.life')
    synchronize(tmp_path)
    with TestClient(create_app(db), base_url='https://first.lhr.life') as client:
        assert client.get('/api/auth/setup').status_code == 200
        log.write_text('https://first.lhr.life\nhttps://next.lhr.life\nhttps://unrelated.example')
        synchronize(tmp_path)
        assert public_url(db) == 'https://next.lhr.life'
        assert client.get('/api/auth/setup').status_code == 400
        assert client.get('https://next.lhr.life/api/auth/setup').status_code == 200
        assert client.get('https://unrelated.example/api/auth/setup').status_code == 400
