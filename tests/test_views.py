"""Proxy views: auth headers, blocked areas, command allowlist, streaming, add-book payload."""
from custom_components.books.views import build_book_payload, default_root_folders

from .conftest import ABS, CHAPTARR

ROOTFOLDERS = [
    {"id": 1, "path": "/data/media/ebooks", "folderType": 2, "isEffectiveDefaultEbook": True,
     "isEffectiveDefaultAudiobook": False, "ebook": {"qualityProfileId": 1, "metadataProfileId": 2}},
    {"id": 2, "path": "/data/media/audiobooks", "folderType": 1, "isEffectiveDefaultEbook": False,
     "isEffectiveDefaultAudiobook": True, "audiobook": {"qualityProfileId": 2, "metadataProfileId": 1}},
]
SEARCH_BOOK = {
    "title": "Die Chroniken von Alsea", "foreignBookId": "gr:1000001", "goodreadsBookId": "gr:1000011",
    "mediaType": "audiobook", "monitored": False, "localBookId": 12, "editions": [{"title": "x", "monitored": False}],
    "author": {"authorName": "Erika Muster", "foreignAuthorId": "hc:2000001", "monitored": False},
}


async def test_chaptarr_proxy_adds_api_key(hass, setup_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{CHAPTARR}/api/v1/queue", json={"records": []}, headers={"Content-Type": "application/json"})
    client = await hass_client()
    resp = await client.get("/api/books/chaptarr/queue?page=1")
    assert resp.status == 200
    assert await resp.json() == {"records": []}
    method, url, _, headers = aioclient_mock.mock_calls[-1]
    assert headers["X-Api-Key"] == "chaptarr-key"
    assert url.query.get("page") == "1"


async def test_chaptarr_proxy_blocks_settings(hass, setup_entry, hass_client, aioclient_mock):
    client = await hass_client()
    for path in ("indexer", "downloadclient/1", "system/shutdown", "config/host", "qualityprofile/1"):
        resp = await client.get(f"/api/books/chaptarr/{path}")
        assert resp.status == 403, path
    assert aioclient_mock.call_count == 0


async def test_chaptarr_proxy_command_allowlist(hass, setup_entry, hass_client, aioclient_mock):
    aioclient_mock.post(f"{CHAPTARR}/api/v1/command", json={"id": 1})
    client = await hass_client()
    bad = await client.post("/api/books/chaptarr/command", json={"name": "Backup"})
    assert bad.status == 403
    good = await client.post("/api/books/chaptarr/command", json={"name": "BookSearch", "bookIds": [1]})
    assert good.status == 200
    assert aioclient_mock.call_count == 1


async def test_chaptarr_proxy_requires_login(hass, setup_entry, hass_client_no_auth):
    client = await hass_client_no_auth()
    resp = await client.get("/api/books/chaptarr/queue")
    assert resp.status == 401


async def test_abs_proxy_streams_with_range_and_bearer(hass, setup_entry, hass_client, aioclient_mock):
    aioclient_mock.get(
        f"{ABS}/api/items/abc/file/1", status=206, content=b"\x00" * 1000,
        headers={"Content-Type": "audio/mp4", "Content-Range": "bytes 0-999/5000", "Accept-Ranges": "bytes"},
    )
    client = await hass_client()
    resp = await client.get("/api/books/abs/items/abc/file/1", headers={"Range": "bytes=0-999"})
    assert resp.status == 206
    assert resp.headers["Content-Range"] == "bytes 0-999/5000"
    assert resp.headers["Content-Type"] == "audio/mp4"
    assert len(await resp.read()) == 1000
    _, _, _, headers = aioclient_mock.mock_calls[-1]
    assert headers["Authorization"] == "Bearer abs-token"
    assert headers["Range"] == "bytes=0-999"


async def test_abs_proxy_strips_auth_sig(hass, setup_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{ABS}/api/items/abc/cover", content=b"jpg", headers={"Content-Type": "image/jpeg"})
    client = await hass_client()
    resp = await client.get("/api/books/abs/items/abc/cover?width=400")
    assert resp.status == 200
    _, url, _, _ = aioclient_mock.mock_calls[-1]
    assert "authSig" not in url.query and url.query.get("width") == "400"


def test_default_root_folders():
    roots = default_root_folders(ROOTFOLDERS)
    assert roots["ebook"] == {"path": "/data/media/ebooks", "qualityProfileId": 1, "metadataProfileId": 2}
    assert roots["audiobook"]["path"] == "/data/media/audiobooks"


def test_payload_monitors_only_this_book_for_ebook():
    roots = default_root_folders(ROOTFOLDERS)
    p = build_book_payload(SEARCH_BOOK, "ebook", roots["ebook"], search=True)
    a = p["author"]
    assert a["addOptions"]["monitor"] == "specificBook"
    assert a["addOptions"]["booksToMonitor"] == ["gr:1000001"]
    assert a["addOptions"]["searchForMissingBooks"] is False
    assert a["ebookMonitorNewItems"] == "none"
    assert a["ebookMonitorFuture"] is False and a["ebookMonitorExisting"] == 2
    assert a["ebookRootFolderPath"] == "/data/media/ebooks" and a["ebookQualityProfileId"] == 1
    assert "audiobookRootFolderPath" not in a
    assert p["mediaType"] == "ebook" and p["ebookMonitored"] is True and p["audiobookMonitored"] is False
    assert p["id"] == 0 and p["localBookId"] is None
    assert p["addOptions"]["searchForNewBook"] is True
    assert p["editions"][0]["monitored"] is True
    # The search result itself is untouched (it is reused for the second media type).
    assert SEARCH_BOOK["editions"][0]["monitored"] is False and "addOptions" not in SEARCH_BOOK["author"]


async def test_add_book_both_media_types(hass, setup_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{CHAPTARR}/api/v1/rootfolder", json=ROOTFOLDERS)
    aioclient_mock.post(f"{CHAPTARR}/api/v1/book", json={"id": 200, "title": "Die Chroniken von Alsea"})
    client = await hass_client()
    resp = await client.post("/api/books/add", json={"book": SEARCH_BOOK, "media_types": ["ebook", "audiobook"]})
    assert resp.status == 200
    results = (await resp.json())["results"]
    assert [r["media_type"] for r in results] == ["audiobook", "ebook"]
    posted = [c[2] for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert [b["mediaType"] for b in posted] == ["audiobook", "ebook"]
    for body in posted:
        prefix = body["mediaType"]
        assert body["author"][f"{prefix}MonitorNewItems"] == "none"
        assert body["author"]["addOptions"]["monitor"] == "specificBook"


async def test_add_book_rejects_bad_input(hass, setup_entry, hass_client):
    client = await hass_client()
    resp = await client.post("/api/books/add", json={"book": SEARCH_BOOK, "media_types": ["comic"]})
    assert resp.status == 400
