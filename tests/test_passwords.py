import pytest
from fastapi.testclient import TestClient

from server import main
from server.db import verify_password
from test_backend import api, add_person, sign_in, join_company, ADMIN, EDITOR, REVIEWER


NEW_PASSWORD = 'New-private-password-123!'


def login(client, account, password):
    return client.post('/api/auth/login', json={'email': account['email'], 'password': password})


def test_change_revokes_all_sessions_keeps_data_and_audits_no_secrets(api):
    client, service = api
    admin_id = client.get('/api/auth/me').json()['id']
    second = sign_in(client.app, ADMIN)
    created = client.post('/api/companies/1/sources', json={'title': 'Материал', 'text': 'Не терять'}).json()
    response = client.post('/api/auth/password', json={'current_password': ADMIN['password'], 'new_password': NEW_PASSWORD})
    assert response.status_code == 204
    assert 'Max-Age=0' in response.headers['set-cookie']
    assert client.get('/api/auth/me').status_code == 401
    assert second.get('/api/auth/me').status_code == 401
    assert login(client, ADMIN, ADMIN['password']).status_code == 401
    assert login(client, ADMIN, NEW_PASSWORD).status_code == 200
    assert client.get('/api/companies/1/sources').json()[0] == created
    assert len(client.get('/api/companies').json()) == 3
    audit = client.get('/api/companies/1/activity').json()
    assert any(item['action'] == 'account.password_changed' for item in audit)
    assert NEW_PASSWORD not in str(audit) and ADMIN['password'] not in str(audit)
    with service.db.read() as conn:
        encoded = conn.execute('SELECT password_hash FROM users WHERE id=?', (admin_id,)).fetchone()[0]
    assert NEW_PASSWORD not in encoded and verify_password(NEW_PASSWORD, encoded)


@pytest.mark.parametrize('current,new,status', [
    ('wrong-password', NEW_PASSWORD, 400),
    (ADMIN['password'], ADMIN['password'], 400),
    (ADMIN['password'], 'short', 422),
    (ADMIN['password'], 'a' * 257, 422),
])
def test_rejected_change_does_not_revoke_or_echo_password(api, current, new, status):
    client, _ = api
    response = client.post('/api/auth/password', json={'current_password': current, 'new_password': new})
    assert response.status_code == status
    assert current not in response.text and new not in response.text
    assert client.get('/api/auth/me').status_code == 200
    assert login(client, ADMIN, ADMIN['password']).status_code == 200


def test_admin_reset_revokes_shared_account_sessions_without_changing_roles(api):
    client, _ = api
    target = add_person(client, EDITOR)
    join_company(client, EDITOR, 2, 'approver')
    employee = sign_in(client.app, EDITOR)
    other = sign_in(client.app, EDITOR)
    response = client.post(f'/api/companies/1/users/{target["id"]}/password', json={'new_password': NEW_PASSWORD})
    assert response.status_code == 204
    assert employee.get('/api/auth/me').status_code == 401
    assert other.get('/api/auth/me').status_code == 401
    assert client.get('/api/auth/me').status_code == 200
    assert login(employee, EDITOR, EDITOR['password']).status_code == 401
    assert login(employee, EDITOR, NEW_PASSWORD).status_code == 200
    assert [(c['id'], c['role']) for c in employee.get('/api/companies').json()] == [(1, 'editor'), (2, 'approver')]
    for cid in (1, 2):
        assert any(a['action'] == 'account.password_reset' for a in client.get(f'/api/companies/{cid}/activity').json())


def test_reset_enforces_role_tenant_target_and_csrf(api):
    client, service = api
    target = add_person(client, EDITOR)
    add_person(client, REVIEWER)
    path = f'/api/companies/1/users/{target["id"]}/password'
    body = {'new_password': NEW_PASSWORD}
    for account in (EDITOR, REVIEWER):
        employee = sign_in(client.app, account)
        assert employee.post(path, json=body).status_code == 403
        # Every signed-in role can change its own password.
        assert employee.post('/api/auth/password', json={'current_password': account['password'], **body}).status_code == 204
    assert TestClient(client.app).post(path, json=body).status_code == 401
    assert TestClient(client.app).post('/api/auth/password', json={'current_password': ADMIN['password'], **body}).status_code == 401
    assert client.post(path.replace('/1/', '/2/'), json=body).status_code == 404
    own_id = client.get('/api/auth/me').json()['id']
    assert client.post(f'/api/companies/1/users/{own_id}/password', json=body).status_code == 403
    telegram_only = service.set_telegram_member(1, 12345)
    assert client.post(f'/api/companies/1/users/{telegram_only["id"]}/password', json=body).status_code == 409
    assert client.post(path, json=body, headers={'Origin': 'https://wrong.example'}).status_code == 403
    assert client.post('/api/auth/password', json={'current_password': ADMIN['password'], **body},
                       headers={'Origin': 'https://wrong.example'}).status_code == 403


def test_old_password_login_racing_reset_cannot_create_session(api, monkeypatch):
    client, _ = api
    target = add_person(client, EDITOR)
    original_verify = main.verify_password
    called = False

    def verify_and_reset(password, encoded):
        nonlocal called
        result = original_verify(password, encoded)
        if password == EDITOR['password'] and not called:
            called = True
            assert client.post(f'/api/companies/1/users/{target["id"]}/password',
                               json={'new_password': NEW_PASSWORD}).status_code == 204
        return result

    monkeypatch.setattr(main, 'verify_password', verify_and_reset)
    employee = TestClient(client.app)
    assert login(employee, EDITOR, EDITOR['password']).status_code == 401
    assert employee.get('/api/auth/me').status_code == 401
    assert login(employee, EDITOR, NEW_PASSWORD).status_code == 200
