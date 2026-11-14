import asyncio
from io import BytesIO
import socket

import httpx
import pytest
from docx import Document

from server import company_seed, integrations, knowledge
from test_backend import EDITOR, REVIEWER, SUPER, action, add_person, api, configure, new_post, sign_in


PATH = "/api/companies/1/knowledge"
PUBLIC_IP = "93.184.216.34"


def test_knowledge_tenant_access_and_mutation_roles(api):
    admin, _ = api
    add_person(admin, EDITOR)
    add_person(admin, REVIEWER)
    with sign_in(admin.app, EDITOR) as editor, sign_in(admin.app, REVIEWER) as reviewer, sign_in(admin.app, SUPER) as owner:
        payload = {"title": "Internal policy", "text": "Company-only text", "category": "decision"}
        created = editor.post(PATH, json=payload)
        assert created.status_code == 201, created.text
        entry_id = created.json()["id"]
        entry_path = f"{PATH}/{entry_id}"
        assert reviewer.get(PATH).json() == [created.json()]
        assert reviewer.post(PATH, json=payload).status_code == 403
        assert reviewer.patch(entry_path, json={"status": "active"}).status_code == 403
        assert reviewer.delete(entry_path).status_code == 403
        assert reviewer.post(PATH + "/import-url", json={"url": "https://example.org/"}).status_code == 403
        assert owner.get(PATH).status_code == 403
        assert owner.post(PATH, json=payload).status_code == 403
        assert editor.get("/api/companies/2/knowledge").status_code == 404
        assert admin.get("/api/companies/2/knowledge").json() == []
        assert admin.patch(f"/api/companies/2/knowledge/{entry_id}", json={"text": "cross-tenant"}).status_code == 404
        assert admin.delete(f"/api/companies/2/knowledge/{entry_id}").status_code == 404


def test_docx_source_snapshots_and_active_context(api):
    admin, service = api

    document = Document()
    document.add_paragraph("Original published specifications")
    content = BytesIO()
    document.save(content)
    response = admin.post("/api/companies/1/uploads", files={
        "file": ("information.docx", content.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    })
    assert response.status_code == 201, response.text
    original = response.json()
    foreign_source = admin.post("/api/companies/2/sources", json={"title": "Tenant 2", "text": "Never share"}).json()
    payload = {"title": "Reference", "category": "business"}
    assert admin.post(PATH, json={**payload, "source_id": foreign_source["id"], "text": "Override"}).status_code == 404
    assert admin.post(PATH, json=payload).status_code == 422
    assert admin.post(PATH, json={**payload, "text": "test\x00text"}).status_code == 422
    result = admin.post(PATH, json={**payload, "source_id": original["id"]})
    assert result.status_code == 201, result.text
    draft = result.json()
    entry_path = f"{PATH}/{draft['id']}"

    def context(cid=1, **kwargs):
        with service.db.read() as connection:
            return knowledge.knowledge_source_rows(connection, cid, **kwargs)

    assert draft["status"] == "draft"
    assert draft["text"] == original["text"]
    assert draft["source_id"] != original["id"]
    assert admin.post(PATH, json={**payload, "source_id": draft["source_id"]}).status_code == 422
    assert context() == []
    assert admin.patch(entry_path, json={"text": None}).status_code == 422
    assert admin.patch(entry_path, json={"status": "archived"}).status_code == 422
    response = admin.patch(entry_path, json={"status": "active", "text": "Revised specifications", "category": "faq"})
    assert response.status_code == 200, response.text
    active = response.json()
    assert active["source_id"] != draft["source_id"]
    assert active["text"] == context()[0]["text"]
    assert active["category"] == context()[0]["category"]
    assert [source["id"] for source in context()] == [active["source_id"]]
    assert [source["text"] for source in context(max_chars=7)] == ["Revised"]
    assert context(2) == []
    assert context(max_chars=0) == []
    assert service.source(1, draft["source_id"])["text"] == original["text"]
    assert admin.patch(entry_path, json={"status": "active"}).json()["source_id"] == active["source_id"]
    assert admin.delete(entry_path).status_code == 204
    assert admin.delete(entry_path).status_code == 204
    assert context() == []
    assert admin.get(PATH).json()[0]["status"] == "archived"
    assert service.source(1, active["source_id"])["text"] == active["text"]


def test_salavat_seed_is_targeted_idempotent_and_keeps_excerpt(api):
    admin, service = api
    with service.db.transaction() as connection:
        connection.execute("UPDATE companies SET name='Другой завод' WHERE id=1")
        connection.execute("UPDATE companies SET name='Салаватстекло' WHERE id=2")
    assert company_seed.seed_salavat_knowledge(service) == []
    assert admin.get(PATH).json() == []
    assert admin.get("/api/companies/2/knowledge").json() == []
    with service.db.transaction() as connection:
        connection.execute("UPDATE companies SET name='Салаватстекло' WHERE id=1")

    added = company_seed.seed_salavat_knowledge(service)
    assert len(added) == 7
    assert company_seed.seed_salavat_knowledge(service) == []
    entries = admin.get(PATH).json()
    assert {entry["id"] for entry in entries} == set(added)
    assert all(entry["origin_url"] == "https://salstek.ru/" for entry in entries)
    draft = [entry for entry in entries if entry["status"] == "draft"]
    assert len(draft) == 2
    assert any("1228" in entry["text"] and "2014" in entry["text"] for entry in draft)
    assert any("07:30" in entry["text"] and "21:00" in entry["text"] for entry in draft)
    assert admin.get("/api/companies/2/knowledge").json() == []

    entry_path = f"{PATH}/{added[0]}"
    assert admin.patch(entry_path, json={"text": "Edited company profile"}).status_code == 200
    assert admin.delete(entry_path).status_code == 204
    assert company_seed.seed_salavat_knowledge(service) == []
    assert next(entry for entry in admin.get(PATH).json() if entry["id"] == added[0])["status"] == "archived"
    with service.db.read() as connection:
        provenance = connection.execute("SELECT * FROM knowledge_seed_provenance WHERE company_id=1").fetchall()
        assert {row["entry_id"] for row in provenance} == set(added)
        assert {row["seed_label"] for row in provenance} == {company_seed.EXCERPT_LABEL}
        (excerpt_source_id,) = {row["excerpt_source_id"] for row in provenance}
        assert excerpt_source_id not in {source["id"] for source in knowledge.knowledge_source_rows(connection, 1)}
    assert service.source(1, excerpt_source_id)["text"] == company_seed.EXCERPT


def test_generation_uses_current_confirmed_knowledge_and_preserves_existing_citation(api, monkeypatch):
    admin, service = api
    configure(admin, openai_key="synthetic-key")
    payload = {"title": "Company profile", "category": "business"}
    active = admin.post(PATH, json={**payload, "text": "Confirmed profile", "status": "active"}).json()
    admin.post(PATH, json={**payload, "text": "Draft profile"})
    admin.post("/api/companies/2/knowledge", json={**payload, "text": "Other tenant", "status": "active"})
    entry_path = f"{PATH}/{active['id']}"
    recorded = []

    async def generate(company, sources, instruction, credentials):
        confirmed = [source for source in sources if source["kind"] == "knowledge"]
        recorded.append([source["id"] for source in confirmed])
        facts = [{"text": source["text"], "source_ids": [source["id"]],
                  "evidence": [{"source_id": source["id"], "quote": source["text"]}]} for source in confirmed]
        return {"title": "Draft post", "body": "Editor draft", "questions": [], "facts": facts, "events": [],
                "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(integrations, "generate_post", generate)
    post_path, post = new_post(admin)
    generated = action(admin, post_path, "generate", post)
    assert generated["facts"][0]["evidence"] == [{"source_id": active["source_id"], "quote": "Confirmed profile"}]
    assert admin.patch(entry_path, json={"text": "Updated profile"}).status_code == 200
    assert admin.delete(entry_path).status_code == 204
    assert admin.get(post_path).json()["facts"] == generated["facts"]
    assert service.source(1, active["source_id"])["text"] == "Confirmed profile"
    regenerated = action(admin, post_path, "generate", generated)
    assert regenerated["facts"] == []
    assert recorded == [[active["source_id"]], []]


class WebsiteResponse(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def mock_website(monkeypatch, handler):
    client = httpx.AsyncClient

    def mock_client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(knowledge.httpx, "AsyncClient", mock_client)


def mock_dns(monkeypatch, answers):
    async def resolve(loop, host, port, *, type):
        assert port == 443 and type == socket.SOCK_STREAM
        return [(socket.AF_INET, type, socket.IPPROTO_TCP, "", (address, port)) for address in answers[host]]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)


def test_import_pins_each_hop_and_saves_draft(api, monkeypatch):
    admin, service = api
    mock_dns(monkeypatch, {"docs.example.": [PUBLIC_IP], "www.example.": ["1.1.1.1"]})
    visited = []

    def server(request):
        host = request.headers["Host"]
        assert request.extensions["sni_hostname"] == host
        assert request.headers["Accept-Encoding"] == "identity"
        visited.append((host, request.url.host))
        if host == "docs.example":
            return httpx.Response(302, headers={"Location": "https://www.example/about#section"})
        assert request.url.path == "/about"
        body = b"<title>About Us</title><p>Public information.</p><script>script_content()</script><style>css{}</style>"
        return httpx.Response(200, headers={"Content-Type": "text/html"}, stream=WebsiteResponse([body]))

    mock_website(monkeypatch, server)
    response = admin.post(PATH + "/import-url", json={"url": "https://docs.example/"})
    assert response.status_code == 201, response.text
    entry = response.json()
    assert entry["origin_url"] == "https://www.example/about"
    assert entry["title"] == "About Us"
    assert entry["text"] == "Public information."
    assert entry["status"] == "draft"
    assert visited == [("docs.example", PUBLIC_IP), ("www.example", "1.1.1.1")]
    with service.db.read() as connection:
        assert knowledge.knowledge_source_rows(connection, 1) == []


@pytest.mark.parametrize("url", [
    "http://example.org/", "https://127.0.0.1/", "https://169.254.169.254/", "https://168.63.129.16/",
    "https://[::ffff:127.0.0.1]/", "https://[64:ff9b::a00:1]/", "https://[fe80::1]/", "https://localhost/",
    "https://192.0.2.2/", "https://100.100.100.200/", "https://224.0.0.1/", "https://example.org:8443/",
    "https://user@example.org/", "https://@example.org/", "https://example.org\\@127.0.0.1/",
    "https://mixed.example/", "https://2130706433/",
])
def test_import_rejects_local_and_unsafe_destinations(api, monkeypatch, url):
    admin, _ = api
    mock_dns(monkeypatch, {"mixed.example.": [PUBLIC_IP, "10.0.0.9"]})
    mock_website(monkeypatch, lambda request: pytest.fail(f"Unexpected HTTP request: {request.url}"))
    response = admin.post(PATH + "/import-url", json={"url": url})
    assert response.status_code == 422, response.text


def test_import_revalidates_redirect_and_limits_redirect_count(api, monkeypatch):
    admin, _ = api
    mock_dns(monkeypatch, {"docs.example.": [PUBLIC_IP], "local.example.": ["127.0.0.1"]})
    visited = []
    destinations = ["https://local.example/", "/loop"]

    def server(request):
        assert request.url.host == PUBLIC_IP
        visited.append(request.url.path)
        return httpx.Response(302, headers={"Location": destinations[0]})

    mock_website(monkeypatch, server)
    assert admin.post(PATH + "/import-url", json={"url": "https://docs.example/"}).status_code == 422
    assert visited == ["/"]
    destinations.pop(0)
    assert admin.post(PATH + "/import-url", json={"url": "https://docs.example/"}).status_code == 422
    assert visited[1:] == ["/", *(["/loop"] * knowledge.MAX_REDIRECTS)]
    assert admin.get(PATH).json() == []


@pytest.mark.parametrize("headers,chunks,status", [
    ({"content-type": "text/html", "content-length": "101"}, [b"text"], 413),
    ({"content-type": "text/html"}, [b"<p>", b"a" * 100], 413),
    ({"content-type": "text/html", "content-encoding": "gzip"}, [b"text"], 415),
    ({"content-type": "application/octet-stream"}, [b"text"], 415),
    ({"content-type": "text/html"}, [b"<script>no text</script>"], 422),
])
def test_import_body_limits_and_supported_content(api, monkeypatch, headers, chunks, status):
    admin, _ = api
    monkeypatch.setattr(knowledge, "MAX_IMPORT_BYTES", 100)
    mock_website(monkeypatch, lambda request: httpx.Response(200, headers=headers, stream=WebsiteResponse(chunks)))
    response = admin.post(PATH + "/import-url", json={"url": f"https://{PUBLIC_IP}/"})
    assert response.status_code == status, response.text
    assert admin.get(PATH).json() == []


def test_import_timeout_includes_dns(api, monkeypatch):
    admin, _ = api

    async def slow_dns(loop, host, port, *, type):
        await asyncio.sleep(1)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", slow_dns)
    monkeypatch.setattr(knowledge, "IMPORT_TIMEOUT", 0.02)
    mock_website(monkeypatch, lambda request: pytest.fail("Unexpected HTTP request"))
    response = admin.post(PATH + "/import-url", json={"url": "https://docs.example/"})
    assert response.status_code == 504, response.text
