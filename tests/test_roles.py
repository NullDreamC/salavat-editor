import time

import pytest
from fastapi.testclient import TestClient

from server.db import Database, SCHEMA, hash_password
from server.domain import DomainError
from server.main import create_app
from test_backend import SUPER, ADMIN, EDITOR, sign_in, add_person, join_company


@pytest.fixture
def system(tmp_path):
    app = create_app(Database(tmp_path / 'roles.sqlite3'))
    with TestClient(app) as owner:
        assert owner.post('/api/auth/setup', json=SUPER).json()['role'] == 'super_admin'
        invite = owner.post('/api/companies/1/invitations', json={'role': 'admin'}).json()
        local = TestClient(app)
        assert local.post(f'/api/invitations/{invite["token"]}/accept', json=ADMIN).json()['role'] == 'admin'
        yield owner, local, app.state.service
        local.close()


def test_owner_gets_only_anonymous_access_fields_and_can_manage_them(system):
    owner, admin, service = system
    person = add_person(admin, {**EDITOR, 'telegram_id': '123456789'})
    data = owner.get('/api/companies/1/access').json()
    assert all(set(row) == {'id', 'company_id', 'role', 'active'} for row in data)
    assert person['id'] in [row['id'] for row in data]
    assert all(value not in str(data) for value in (EDITOR['name'], EDITOR['email'], '123456789', EDITOR['password']))
    employee = sign_in(admin.app, EDITOR)
    path = f'/api/companies/1/access/{person["id"]}'
    assert owner.patch(path, json={'role': 'approver'}).json()['role'] == 'approver'
    assert employee.get('/api/auth/me').status_code == 401
    assert owner.patch(path, json={'active': False}).json()['active'] is False
    assert owner.patch(path, json={'telegram_id': '999', 'email': 'secret@example.test'}).status_code == 422
    assert owner.patch(path, json={'role': 'super_admin'}).status_code == 422
    assert owner.patch(path.replace('/1/', '/2/'), json={'active': True}).status_code == 404
    assert owner.patch('/api/companies/1/access/1', json={'role': 'admin'}).status_code == 404
    with service.db.read() as conn:
        assert conn.execute('SELECT 1 FROM memberships WHERE user_id=1').fetchone() is None


def test_owner_can_change_own_password_without_company_memberships(system):
    owner, _, _ = system
    assert owner.post('/api/auth/password', json={'current_password': SUPER['password'], 'new_password': 'new-owner-password-123'}).status_code == 204
    assert owner.get('/api/auth/me').status_code == 401
    assert owner.post('/api/auth/login', json={'email': SUPER['email'], 'password': 'new-owner-password-123'}).json()['role'] == 'super_admin'
    assert len(owner.get('/api/companies').json()) == 3


def test_owner_cannot_read_profiles_or_indirect_personal_data(system):
    owner, admin, _ = system
    person = add_person(admin, EDITOR)
    source = admin.post('/api/companies/1/uploads', files={'file': ('private.txt', b'Personal details', 'text/plain')}).json()
    for path in ('users', 'sources', 'posts', 'events', 'activity'):
        assert owner.get('/api/companies/1/' + path).status_code == 403
    assert owner.get(source['file_url']).status_code == 403
    assert owner.post('/api/companies/1/users', json={**EDITOR, 'email': 'new@example.test'}).status_code == 403
    assert owner.patch(f'/api/companies/1/users/{person["id"]}', json={'telegram_id': '888'}).status_code == 403
    assert owner.post(f'/api/companies/1/users/{person["id"]}/password', json={'new_password': 'new-secret-pass-123'}).status_code == 403
    assert owner.post('/api/companies/1/posts', json={'source_ids': [source['id']]}).status_code == 403
    assert owner.get('/api/companies/1/settings').status_code == 200
    assert owner.get('/api/companies/1/usage').status_code == 200
    assert owner.patch('/api/companies/1', json={'name': 'Новая компания'}).status_code == 200
    assert owner.post('/api/companies', json={'name': 'Четвёртая компания'}).status_code == 201


def test_company_admin_is_scoped_and_last_admin_is_protected(system):
    owner, admin, _ = system
    own = admin.get('/api/auth/me').json()
    assert [c['id'] for c in admin.get('/api/companies').json()] == [1]
    assert admin.post('/api/companies', json={'name': 'Не разрешено'}).status_code == 403
    assert admin.get('/api/companies/1/access').status_code == 403
    assert admin.get('/api/companies/2/users').status_code == 404
    assert admin.patch('/api/companies/2', json={'name': 'Не разрешено'}).status_code == 404
    assert admin.patch(f'/api/companies/1/users/{own["id"]}', json={'active': False}).status_code == 409
    person = add_person(admin, EDITOR)
    assert admin.patch(f'/api/companies/1/users/{person["id"]}', json={'role': 'admin'}).status_code == 200
    assert admin.patch(f'/api/companies/1/users/{own["id"]}', json={'role': 'editor'}).status_code == 200
    assert admin.get('/api/companies/1/users').status_code == 403
    # Owner can revoke even the last company admin and issue a replacement invitation.
    assert owner.patch(f'/api/companies/1/access/{person["id"]}', json={'active': False}).status_code == 200
    assert owner.post('/api/companies/1/invitations', json={'role': 'admin'}).status_code == 201


def test_invitation_is_one_use_private_and_cannot_make_owner_a_member(system):
    owner, admin, _ = system
    invite = owner.post('/api/companies/2/invitations', json={'role': 'admin'}).json()
    path = f'/api/invitations/{invite["token"]}'
    assert owner.get(path).json() == {'company_name': 'Компания 2', 'role': 'admin'}
    assert owner.post(path + '/accept', json={key: EDITOR[key] for key in ('name', 'email', 'password')}).status_code == 403
    with TestClient(owner.app) as recipient:
        assert recipient.post(path + '/accept', json=SUPER).status_code == 400
        assert recipient.post(path + '/accept', json={key: EDITOR[key] for key in ('name', 'email', 'password')}).status_code == 200
        assert recipient.post(path + '/accept', json=ADMIN).status_code == 410
    listing = owner.get('/api/companies/2/invitations').json()
    assert all(set(item) == {'id', 'role', 'expires_at', 'status'} for item in listing)
    assert listing[0]['status'] == 'used' and EDITOR['email'] not in str(listing)
    assert admin.post('/api/companies/1/users', json=EDITOR).status_code == 409


def test_revoked_expired_and_deauthorized_issuer_invitations_fail(system):
    owner, admin, service = system
    invite = admin.post('/api/companies/1/invitations', json={'role': 'editor'}).json()
    assert owner.delete(f'/api/companies/2/invitations/{invite["id"]}').status_code == 404
    assert admin.delete(f'/api/companies/1/invitations/{invite["id"]}').status_code == 204
    assert owner.get(f'/api/invitations/{invite["token"]}').status_code == 410
    expired = owner.post('/api/companies/1/invitations', json={'role': 'admin'}).json()
    with service.db.transaction() as conn:
        conn.execute('UPDATE invitations SET expires_at=? WHERE id=?', (int(time.time()) - 1, expired['id']))
    assert owner.get(f'/api/invitations/{expired["token"]}').status_code == 410
    before_revoke = admin.post('/api/companies/1/invitations', json={'role': 'editor'}).json()
    uid = admin.get('/api/auth/me').json()['id']
    assert owner.patch(f'/api/companies/1/access/{uid}', json={'active': False}).status_code == 200
    assert owner.get(f'/api/invitations/{before_revoke["token"]}').status_code == 410


def test_company_admin_cannot_take_over_shared_account(system):
    owner, admin, _ = system
    person = add_person(admin, EDITOR)
    join_company(owner, EDITOR, 2, 'editor')
    assert admin.post(f'/api/companies/1/users/{person["id"]}/password', json={'new_password': 'takeover-pass-123'}).status_code == 409
    assert sign_in(owner.app, EDITOR).get('/api/auth/me').status_code == 200


def test_migration_revokes_old_global_memberships_without_losing_company_data(tmp_path):
    db = Database(tmp_path / 'legacy.sqlite3')
    with db.read() as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO companies(id,name) VALUES(1,'Салаватстекло')")
        for uid, is_global in [(1, 1), (2, 0)]:
            conn.execute('INSERT INTO users(id,name,email,password_hash,is_admin) VALUES(?,?,?,?,?)',
                         (uid, f'User {uid}', f'{uid}@example.test', hash_password('unchanged-password'), is_global))
            conn.execute("INSERT INTO memberships(company_id,user_id,role,telegram_id) VALUES(1,?,'admin',?)", (uid, str(uid)))
            conn.execute('INSERT INTO sessions VALUES(?,?,?)', (f'session-{uid}', uid, int(time.time()) + 1000))
        conn.execute("INSERT INTO sources(company_id,title,text,kind,created_at) VALUES(1,'Keep','Private','text','2026-11-13')")
    db.initialize()
    db.initialize()
    with db.read() as conn:
        assert [r['user_id'] for r in conn.execute('SELECT user_id FROM memberships')] == [2]
        assert [r['user_id'] for r in conn.execute('SELECT user_id FROM sessions')] == [2]
        assert conn.execute('SELECT text FROM sources').fetchone()['text'] == 'Private'
        # Even an accidentally restored membership must not bypass role separation.
        conn.execute("INSERT INTO memberships(company_id,user_id,role,telegram_id) VALUES(1,1,'admin','1')")
    app = create_app(db)
    service = app.state.service
    with pytest.raises(DomainError):
        service.create_source(1, 'No', 'No', actor=1)
    assert service.telegram_members(1, 1) is None
    assert [u['id'] for u in service.telegram_users(1)] == [2]
