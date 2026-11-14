import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from server import billing, knowledge, research
from server.db import Database
from server.domain import DomainError, Service


NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "store_secret", lambda key: key.encode())
    monkeypatch.setattr(research, "reveal_secret", lambda key: key.decode())
    db = Database(tmp_path / "test.sqlite")
    db.initialize()
    result = Service(db)
    knowledge.initialize_knowledge(result)
    research.initialize_research(result)
    with db.transaction() as connection:
        connection.executemany("INSERT INTO companies(id,name) VALUES(?,?)", [(1, "Первая"), (2, "Вторая")])
        connection.executemany(
            "INSERT INTO users(id,name,email) VALUES(?,?,?)",
            [(1, "Админ", "admin@example.org"), (2, "Редактор", "editor@example.org"),
             (3, "Проверяющий", "reviewer@example.org")],
        )
        connection.executemany("INSERT INTO memberships(company_id,user_id,role) VALUES(?,?,?)",
                               [(1, 1, "admin"), (2, 1, "admin"), (1, 2, "editor"), (1, 3, "approver")])
    return result


def enable_search(service, *, tariff=True):
    if tariff:
        with service.db.transaction() as connection:
            connection.execute("INSERT INTO billing_tariff_catalogs(version,tariffs) VALUES(?,?)", ("test", json.dumps([
                {"provider": "yandex", "kind": "search", "model": billing.SEARCH_MODEL, "currency": "RUB",
                 "rub_per_unit": 1, "per_request": 1},
            ])))
    research._patch_settings(service, 1, 1, research.SettingsPatch(
        version=0, enabled=True, query="производители стекла Урал", folder_id="folder", api_key="test-secret",
    ))


def search_data():
    return base64.b64encode(b"""<yandexsearch><response><results><grouping>
      <group><doc><url>https://example.org/news</url><title>Glass <hlword>plant</hlword></title>
      <passages><passage>Public <hlword>result</hlword></passage></passages></doc></group>
      <group><doc><url>https://www.example.org/contacts</url><title>Duplicate</title></doc></group>
      <group><doc><url>https://127.0.0.1/</url><title>Private</title></doc></group>
    </grouping></results></response></yandexsearch>""").decode()


def test_discovery_is_bounded_billed_unverified_and_tenant_scoped(service, monkeypatch):
    enable_search(service)
    calls = []

    async def search(query, folder, key):
        calls.append((query, folder, key))
        return search_data()

    monkeypatch.setattr(research, "_call_search", search)
    asyncio.run(research.refresh_research(service, 1, now=NOW))
    asyncio.run(research.refresh_research(service, 1, now=NOW + timedelta(hours=1)))
    asyncio.run(research.refresh_research(service, 2, now=NOW))
    data = research.research_overview(service, 1, 3, now=NOW)
    assert len(calls) == 1
    assert data["settings"]["configured"] and "test-secret" not in json.dumps(data)
    assert not data["competitors"] and len(data["candidates"]) == 1
    candidate = data["candidates"][0]
    assert candidate["state"] == "unverified" and candidate["title"] == "Glass plant"
    with service.db.read() as connection:
        charge = connection.execute("SELECT kind,status,metered_kopecks FROM billing_charges").fetchone()
        assert tuple(charge) == ("search", "settled", 100)
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0] == 0
    assert research.research_overview(service, 2, 1, now=NOW)["candidates"] == []
    payload = research.CompetitorCreate(name="Завод", url=candidate["url"], candidate_id=candidate["id"])
    with pytest.raises(DomainError):
        research._edit_competitor(service, 2, 1, payload)
    research._edit_competitor(service, 1, 2, payload)
    assert research.research_overview(service, 1, 2, now=NOW)["candidates"][0]["state"] == "selected"


def test_search_requires_current_admin_and_tariff(service, monkeypatch):
    enable_search(service, tariff=False)

    async def unexpected(*args):
        raise AssertionError("No external call is permitted")

    monkeypatch.setattr(research, "_call_search", unexpected)
    with pytest.raises(DomainError) as denied:
        asyncio.run(research.discover(service, 1, 2, now=NOW))
    assert denied.value.status_code == 403
    asyncio.run(research.refresh_research(service, 1, now=NOW))
    assert "тарифа" in research.research_overview(service, 1, 1, now=NOW)["settings"]["last_error"]
    with service.db.transaction() as connection:
        connection.execute("UPDATE memberships SET active=0 WHERE company_id=1 AND user_id=1")
    asyncio.run(research.refresh_research(service, 1, now=NOW + timedelta(days=1)))
    with service.db.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_searches").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM billing_charges").fetchone()[0] == 0


@pytest.mark.parametrize("url", ["http://example.org", "https://192.168.1.1/path", "https://localhost",
                                 "https://[::1]/", "https://user:pass@example.org/", "https://example.org:8443/"])
def test_private_destinations_rejected(service, url):
    with pytest.raises(DomainError):
        research._edit_competitor(service, 1, 2, research.CompetitorCreate(name="Не подходит", url=url))


class Stream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        yield self.body


def test_public_feed_observations_deduplicate_and_keep_unknowns(service, monkeypatch):
    feed = b"""<rss><channel>
      <item><title>Published</title><link>https://example.org/p/1</link>
      <pubDate>Mon, 12 Oct 2026 10:00:00 +0300</pubDate><description><![CDATA[<b>Public</b> news]]></description>
      <enclosure type="image/jpeg" url="https://example.org/image.jpg"/></item>
      <item><title>Undated</title><link>https://example.org/p/2</link></item>
      <item><title>Duplicate URL</title><link>https://example.org/p/2#anchor</link></item>
      <item><title>Unsafe link</title><link>http://example.org/p/3</link></item>
    </channel></rss>"""
    client = httpx.AsyncClient
    requests = []

    def handler(request):
        requests.append(str(request.url))
        assert request.headers["host"] == "example.org"
        assert request.extensions["sni_hostname"] == "example.org"
        return httpx.Response(200, headers={"Content-Type": "application/rss+xml"}, stream=Stream(feed))

    async def resolve(host):
        assert host == "example.org"
        return "142.250.186.174"

    monkeypatch.setattr(knowledge, "_resolve_public_ip", resolve)
    monkeypatch.setattr(knowledge.httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(handler), **kwargs
    ))
    research._edit_competitor(service, 1, 2, research.CompetitorCreate(name="Источник", url="https://example.org/feed"))
    asyncio.run(research.collect_competitor(service, 1, 1, 2, now=NOW))
    asyncio.run(research.collect_competitor(service, 1, 1, 2, now=NOW + timedelta(days=1)))
    data = research.research_overview(service, 1, 3, now=NOW + timedelta(days=1))
    assert len(requests) == 2 and len(data["publications"]) == 2
    assert data["publications"][0]["excerpt"] == "Public news"
    assert data["publications"][1]["published_on"] is None
    assert data["comparison"]["competitors"] == [{"competitor_id": 1, "observed_publications": 1,
        "undated_publications": 1, "formats": {"photo": 1}, "views_total": None, "views_coverage": 0}]


def test_public_refresh_refuses_private_redirect_and_retries_later(service, monkeypatch):
    client = httpx.AsyncClient
    calls = []

    async def resolve(host):
        if host == "example.org":
            return "142.250.186.174"
        return await public_resolve(host)

    public_resolve = knowledge._resolve_public_ip

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/admin"})

    monkeypatch.setattr(knowledge, "_resolve_public_ip", resolve)
    monkeypatch.setattr(knowledge.httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(handler), **kwargs
    ))
    research._edit_competitor(service, 1, 2, research.CompetitorCreate(name="Источник", url="https://example.org/feed"))
    with pytest.raises(DomainError) as error:
        asyncio.run(research.collect_competitor(service, 1, 1, 2, now=NOW))
    assert error.value.status_code == 422 and len(calls) == 1
    with pytest.raises(DomainError) as delayed:
        asyncio.run(research.collect_competitor(service, 1, 1, 2, now=NOW + timedelta(minutes=1)))
    assert delayed.value.status_code == 429
    assert research.research_overview(service, 1, 1, now=NOW)["competitors"][0]["last_error"]


def test_telegram_preview_extracts_own_channel_and_exact_views_only():
    page = research._PublicPage("channel")
    page.feed("""<div class="tgme_widget_message" data-post="channel/10">
      <div class="tgme_widget_message_text">Пост<script>ignore me</script><b>с фото</b></div>
      <a class="tgme_widget_message_photo_wrap"></a><span class="tgme_widget_message_views">1.2K</span>
      <a class="tgme_widget_message_date"><time datetime="2026-10-12T17:13:00+00:00"></time></a></div>
      <div class="tgme_widget_message" data-post="another/10"></div>
      <div class="tgme_widget_message" data-post="channel/11"><span class="tgme_widget_message_views">0</span></div>""")
    first, second = page.publications
    assert first["url"] == "https://t.me/channel/10" and first["published_on"] == "2026-10-12"
    assert first["excerpt"] == "Пост с фото" and first["format"] == "photo" and first["views"] is None
    assert second["views"] == 0 and second["published_on"] is None


def test_news_collection_uses_bounded_same_site_links_with_real_dates(service, monkeypatch):
    client = httpx.AsyncClient
    calls = []
    pages = {
        "/": '<a href="/news">Новости</a><a href="https://unrelated.org/news/a">Другое</a>',
        "/news": '<a href="/news/a">А</a><a href="/news/b">Б</a><a href="/news/c">В</a>',
        "/news/a": '<title>Новости завода</title><meta property="article:published_time" content="2026-10-12">',
        "/news/b": '<meta property="og:title" content="Новость без даты"><meta property="og:type" content="article">',
    }

    async def resolve(host):
        assert host == "example.org"
        return "142.250.186.174"

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, headers={"Content-Type": "text/html; charset=utf-8"},
                              stream=Stream(pages[request.url.path].encode()))

    monkeypatch.setattr(knowledge, "_resolve_public_ip", resolve)
    monkeypatch.setattr(knowledge.httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(handler), **kwargs
    ))
    research._edit_competitor(service, 1, 2, research.CompetitorCreate(name="Источник", url="https://example.org/"))
    asyncio.run(research.collect_competitor(service, 1, 1, 2, now=NOW))
    assert calls == ["/", "/news", "/news/a", "/news/b"]
    item, = research.research_overview(service, 1, 3, now=NOW)["publications"]
    assert item["url"] == "https://example.org/news/a"
    assert item["title"] == "Новости завода" and item["published_on"] == "2026-10-12"
