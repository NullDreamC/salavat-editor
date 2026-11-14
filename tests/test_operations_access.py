"""Exercise new modules through the application's real route registration and tenancy."""
from test_roles import system
from test_backend import EDITOR, add_person, sign_in


def test_new_editorial_sections_keep_global_accounts_out(system):
    owner, admin, _ = system
    for section in ('research', 'strategy', 'telegram-observation', 'attribution'):
        path = '/api/companies/1/' + section
        assert owner.get(path).status_code == 403, section
        response = admin.get(path)
        assert response.status_code == 200, (section, response.text)
        assert admin.get(path.replace('/1/', '/2/')).status_code == 404, section
    assert owner.get('/api/companies/1/billing').status_code == 200
    assert admin.get('/api/companies/1/billing').status_code == 200
    assert owner.get('/api/companies/1/crm').status_code == 403
    assert admin.get('/api/companies/1/crm').status_code == 200
    assert admin.get('/api/companies/2/crm').status_code == 404


def test_editor_cannot_manage_new_integrations_or_budget(system):
    _, admin, _ = system
    add_person(admin, EDITOR)
    editor = sign_in(admin.app, EDITOR)
    try:
        assert editor.get('/api/companies/1/billing').status_code == 403
        assert editor.get('/api/companies/1/crm').status_code == 403
        assert editor.patch('/api/companies/1/telegram-observation', json={
            'analytics_enabled': True, 'comments_enabled': True,
        }).status_code == 403
        assert editor.patch('/api/companies/1/attribution/settings', json={
            'enabled': True, 'target_url': 'https://example.org/',
        }).status_code == 403
        assert editor.post('/api/companies/1/attribution/key').status_code == 403
    finally:
        editor.close()


def test_only_authenticated_crm_ingestion_can_bypass_browser_origin(system):
    _, admin, _ = system
    # A connected CRM has no browser origin. It must still authenticate its own request.
    endpoint = '/api/companies/1/attribution/events'
    response = admin.post(endpoint, json={}, headers={'Origin': 'https://unrelated.example'})
    assert response.status_code in (401, 403, 422)
    assert admin.post('/api/companies/1/sources', json={'title': 'x', 'text': 'x'},
                      headers={'Origin': 'https://unrelated.example'}).status_code == 403
