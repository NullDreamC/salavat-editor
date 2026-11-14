from test_roles import system
from test_backend import EDITOR, add_person, join_company, sign_in
from server.telegram_link import _prepare


def test_remove_access_preserves_other_company_and_authored_content(system):
    owner, admin, service = system
    person = add_person(admin, {**EDITOR, 'telegram_id': '123456789'})
    join_company(owner, EDITOR, 2, 'editor')
    employee = sign_in(owner.app, EDITOR)
    source = employee.post('/api/companies/1/sources', json={'title': 'Материал', 'text': 'Факты'}).json()
    post = employee.post('/api/companies/1/posts', json={'source_ids': [source['id']]}).json()
    path = f'/api/companies/1/access/{person["id"]}'
    assert admin.delete(path).status_code == 403
    assert employee.delete(path).status_code == 403
    assert owner.delete(path.replace('/1/', '/3/')).status_code == 404
    assert owner.delete('/api/companies/1/access/1').status_code == 404
    assert owner.delete(path).status_code == 204
    assert owner.delete(path).status_code == 404
    assert owner.patch(path, json={'active': True}).status_code == 404
    assert person['id'] not in [p['id'] for p in owner.get('/api/companies/1/access').json()]
    assert employee.get('/api/auth/me').status_code == 200
    assert employee.get('/api/companies/1/posts').status_code == 404
    assert [c['id'] for c in employee.get('/api/companies').json()] == [2]
    assert admin.get(f'/api/companies/1/posts/{post["id"]}').status_code == 200
    assert service.telegram_members(1, '123456789') is None
    with service.db.read() as connection:
        assert connection.execute('SELECT 1 FROM users WHERE id=?', (person['id'],)).fetchone()
    join_company(owner, EDITOR, 1, 'editor')
    assert person['id'] in [p['id'] for p in owner.get('/api/companies/1/access').json()]


def test_remove_last_membership_revokes_session_and_old_links(system):
    owner, admin, service = system
    uid = admin.get('/api/auth/me').json()['id']
    invitation = admin.post('/api/companies/1/invitations', json={'role': 'editor'}).json()
    with service.db.transaction() as connection:
        _prepare(connection)
        connection.execute('INSERT INTO telegram_links VALUES(?,?,?,?,?)', (1, uid, 'dummy-hash', 'dummy-bot', 9999999999))
    assert owner.delete(f'/api/companies/1/access/{uid}').status_code == 204
    assert admin.get('/api/auth/me').status_code == 401
    assert owner.get(f'/api/invitations/{invitation["token"]}').status_code == 410
    with service.db.read() as connection:
        assert connection.execute('SELECT 1 FROM telegram_links WHERE company_id=1 AND user_id=?', (uid,)).fetchone() is None
    # The global administrator can replace the last company admin via an invitation.
    assert owner.post('/api/companies/1/invitations', json={'role': 'admin'}).status_code == 201
