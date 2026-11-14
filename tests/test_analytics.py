import json

from server import social_platforms
from test_backend import SUPER, api, sign_in


PATH = "/api/companies/1/analytics"


def metric(**overrides):
    return {"platform": "vk", "publication_id": "-123_1", "title": "Собрание", "format": "Фото",
            "published_on": "2026-01-02", "observed_on": "2026-01-04", "source": "Сводка ВКонтакте",
            "views": 200, "reach": 100, "reactions": 8, "comments": 1, "shares": 1, **overrides}


def test_latest_snapshot_counts_and_tenant_isolation(api):
    client, _ = api
    assert client.get(PATH).json()["summary"]["totals"]["views"] is None
    for row in (metric(), metric(observed_on="2026-01-05", views=500, reactions=0),
                metric(publication_id="-123_2", format="Видео", views=None, shares=None)):
        assert client.post(PATH + "/metrics", json=row).status_code == 201
    data = client.get(PATH + "?start=2026-01-02&end=2026-01-02").json()
    rollup = data["summary"]
    assert rollup["publications"] == 2
    assert rollup["totals"]["views"] == 500 and rollup["coverage"]["views"] == 1
    assert rollup["er"] == {"percent": 2.0, "denominator": "reach", "reach": 100, "interactions": 2, "publications": 1}
    assert [entry["format"] for entry in data["formats"]] == ["Видео", "Фото"]
    assert data["items"][0]["observed_on"] == "2026-01-05"
    assert client.get(PATH + "?end=2026-01-01").json()["items"] == []
    assert client.get(PATH + "?start=2026-01-03&end=2026-01-02").status_code == 422
    assert client.get(PATH.replace("/1/", "/2/")).json()["items"] == []
    with sign_in(client.app, SUPER) as owner:
        assert owner.get(PATH).status_code == 403
        assert owner.post(PATH + "/metrics", json=metric()).status_code == 403


def test_import_preserves_missing_and_zero_and_fails_atomically(api):
    client, _ = api
    valid = metric()
    rejected = client.post(PATH + "/import", json={"format": "json", "content": json.dumps([valid, metric(reach=-1)])})
    assert rejected.status_code == 422
    assert client.get(PATH).json()["items"] == []
    csv = ("platform,publication_id,title,format,published_on,observed_on,source,views,reach\n"
           "vk,-123_1,Собрание,Фото,2026-01-02,2026-01-04,Экспорт,0,\n")
    assert client.post(PATH + "/import", json={"format": "csv", "content": csv}).json() == {"recorded": 1}
    stored, = client.get(PATH).json()["items"]
    assert (stored["source"], stored["origin"], stored["views"], stored["reach"]) == ("Экспорт", "csv", 0, None)


def test_sync_maps_provider_metrics_without_inventing_values(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(social_platforms, "get_social_connection", lambda *a, **kw: {
        "configured": True, "enabled": True, "destination_id": "-123", "token": "synthetic-token"})

    async def fetch(platform, connection, message_id):
        assert (platform, connection["destination_id"], message_id) == ("vk", "-123", "-123_1")
        return {"views": 4, "likes": 2, "reposts": 0}

    monkeypatch.setattr(social_platforms, "fetch_social_metrics", fetch)
    payload = {key: metric()[key] for key in ("platform", "publication_id", "title", "format", "published_on")}
    assert client.post(PATH + "/sync", json=payload).status_code == 201
    saved, = client.get(PATH).json()["items"]
    assert saved["origin"] == "api"
    assert [saved[field] for field in ("views", "reactions", "shares", "comments", "reach")] == [4, 2, 0, None, None]
