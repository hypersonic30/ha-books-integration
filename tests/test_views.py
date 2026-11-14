"""Proxy views: auth headers, blocked areas, command allowlist, streaming, add-book payload."""
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.books.const import DOMAIN
from custom_components.books.views import build_book_payload, default_root_folders

from .conftest import ABS, CHAPTARR, person
from .test_chaptarr_fallback import JSON, NZBGET, PROWLARR, REAL_HITS, WANTED_TITLE, _entry_data

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
    aioclient_mock.post(f"{CHAPTARR}/api/v1/command", json={"id": 9})
    client = await hass_client()
    resp = await client.post("/api/books/add", json={"book": SEARCH_BOOK, "media_types": ["ebook", "audiobook"]})
    assert resp.status == 200
    body = await resp.json()
    results = body["results"]
    assert [r["media_type"] for r in results] == ["audiobook", "ebook"]
    posted = [c[2] for c in aioclient_mock.mock_calls if c[0] == "POST" and str(c[1]).endswith("/book")]
    assert [b["mediaType"] for b in posted] == ["audiobook", "ebook"]
    # Chaptarr ignores searchForNewBook, so the integration searches explicitly.
    commands = [c[2] for c in aioclient_mock.mock_calls if c[0] == "POST" and str(c[1]).endswith("/command")]
    assert commands == [{"name": "BookSearch", "bookIds": [200, 200]}]
    assert body["search_started"] is True
    for body in posted:
        prefix = body["mediaType"]
        assert body["author"][f"{prefix}MonitorNewItems"] == "none"
        assert body["author"]["addOptions"]["monitor"] == "specificBook"


async def test_add_book_rejects_bad_input(hass, setup_entry, hass_client):
    client = await hass_client()
    resp = await client.post("/api/books/add", json={"book": SEARCH_BOOK, "media_types": ["comic"]})
    assert resp.status == 400


async def test_add_book_without_search(hass, setup_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{CHAPTARR}/api/v1/rootfolder", json=ROOTFOLDERS)
    aioclient_mock.post(f"{CHAPTARR}/api/v1/book", json={"id": 201})
    client = await hass_client()
    resp = await client.post("/api/books/add", json={"book": SEARCH_BOOK, "media_types": ["ebook"], "search": False})
    assert resp.status == 200 and (await resp.json())["search_started"] is False
    assert not [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/command")]


# -- fallback search triggered right after the add flow's own BookSearch --------------------------------------

async def _setup_with_fallback(hass, hass_admin_user, hass_client, **overrides):
    entry = MockConfigEntry(domain=DOMAIN, data=_entry_data(**overrides), title="Books",
                            subentries_data=[person(hass_admin_user)])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return await hass_client()


async def test_add_book_falls_back_to_prowlarr_when_chaptarr_itself_found_nothing(
        hass, hass_admin_user, hass_client, aioclient_mock):
    client = await _setup_with_fallback(hass, hass_admin_user, hass_client)
    aioclient_mock.get(f"{CHAPTARR}/api/v1/rootfolder", json=ROOTFOLDERS)
    aioclient_mock.post(f"{CHAPTARR}/api/v1/book", json={"id": 1164, "title": WANTED_TITLE})
    aioclient_mock.post(f"{CHAPTARR}/api/v1/command", json={"id": 9})
    aioclient_mock.get(f"{CHAPTARR}/api/v1/command/9", json={"id": 9, "status": "completed"})
    aioclient_mock.get(f"{CHAPTARR}/api/v1/queue", json={"records": []})
    aioclient_mock.get(f"{CHAPTARR}/api/v1/book/1164", json={"id": 1164, "statistics": {"bookFileCount": 0}})
    _fallback_grab_mocks(aioclient_mock)

    resp = await client.post("/api/books/add", json={"book": SEARCH_BOOK, "media_types": ["audiobook"]})
    assert resp.status == 200
    await hass.async_block_till_done()

    job = hass.data[DOMAIN]["chaptarr_fallback"]
    assert job.state["done"]["1164"]["nzb_id"] == 555


def _fallback_grab_mocks(aioclient_mock) -> None:
    """Everything a real grab needs. Used by the negative tests too (fully mocked, not just omitted) so that if
    the guard they test for is ever removed, the call actually goes through and gets recorded - not swallowed
    by an unrelated "no mock registered" error that would make the assertion pass for the wrong reason."""
    aioclient_mock.get(f"{PROWLARR}/api/v1/search", json=REAL_HITS, headers=JSON)
    aioclient_mock.get(f"{PROWLARR}/1/download", content=b"fake nzb bytes")
    aioclient_mock.post(f"{NZBGET}/jsonrpc", json={"version": "1.1", "result": 555}, headers=JSON)


async def test_add_book_does_not_fall_back_once_chaptarr_grabbed_something_itself(
        hass, hass_admin_user, hass_client, aioclient_mock):
    client = await _setup_with_fallback(hass, hass_admin_user, hass_client)
    aioclient_mock.get(f"{CHAPTARR}/api/v1/rootfolder", json=ROOTFOLDERS)
    aioclient_mock.post(f"{CHAPTARR}/api/v1/book", json={"id": 1164, "title": WANTED_TITLE})
    aioclient_mock.post(f"{CHAPTARR}/api/v1/command", json={"id": 9})
    aioclient_mock.get(f"{CHAPTARR}/api/v1/command/9", json={"id": 9, "status": "completed"})
    aioclient_mock.get(f"{CHAPTARR}/api/v1/queue", json={"records": [{"id": 1, "bookId": 1164}]})
    aioclient_mock.get(f"{CHAPTARR}/api/v1/book/1164", json={"id": 1164, "statistics": {"bookFileCount": 0}})
    _fallback_grab_mocks(aioclient_mock)

    resp = await client.post("/api/books/add", json={"book": SEARCH_BOOK, "media_types": ["audiobook"]})
    assert resp.status == 200
    await hass.async_block_till_done()

    assert not [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(PROWLARR)]


async def test_add_book_does_not_fall_back_when_the_book_already_has_a_file(
        hass, hass_admin_user, hass_client, aioclient_mock):
    client = await _setup_with_fallback(hass, hass_admin_user, hass_client)
    aioclient_mock.get(f"{CHAPTARR}/api/v1/rootfolder", json=ROOTFOLDERS)
    aioclient_mock.post(f"{CHAPTARR}/api/v1/book", json={"id": 1164, "title": WANTED_TITLE})
    aioclient_mock.post(f"{CHAPTARR}/api/v1/command", json={"id": 9})
    aioclient_mock.get(f"{CHAPTARR}/api/v1/command/9", json={"id": 9, "status": "completed"})
    aioclient_mock.get(f"{CHAPTARR}/api/v1/queue", json={"records": []})
    aioclient_mock.get(f"{CHAPTARR}/api/v1/book/1164", json={"id": 1164, "statistics": {"bookFileCount": 1}})
    _fallback_grab_mocks(aioclient_mock)

    resp = await client.post("/api/books/add", json={"book": SEARCH_BOOK, "media_types": ["audiobook"]})
    assert resp.status == 200
    await hass.async_block_till_done()

    assert not [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(PROWLARR)]


async def test_add_book_skips_the_fallback_check_entirely_when_the_feature_is_off(
        hass, setup_entry, hass_client, aioclient_mock, monkeypatch):
    """With the feature off (the default: no Prowlarr/NZBGet configured), the background check must bail out
    before even waiting for the BookSearch command - not attempt it and merely fail to find anything useful to
    do. Checked directly (wait_for_command called or not), not by the absence of a network call: an attempt
    that hits an unmocked endpoint and errors internally would look the same as "never attempted" from the
    outside, which would make this test pass for the wrong reason."""
    called = []

    async def spy(client, command_id):
        called.append(command_id)
        return {"status": "completed"}

    monkeypatch.setattr("custom_components.books.views.wait_for_command", spy)
    aioclient_mock.get(f"{CHAPTARR}/api/v1/rootfolder", json=ROOTFOLDERS)
    aioclient_mock.post(f"{CHAPTARR}/api/v1/book", json={"id": 1164, "title": WANTED_TITLE})
    aioclient_mock.post(f"{CHAPTARR}/api/v1/command", json={"id": 9})
    client = await hass_client()

    resp = await client.post("/api/books/add", json={"book": SEARCH_BOOK, "media_types": ["audiobook"]})
    assert resp.status == 200
    await hass.async_block_till_done()

    assert called == []

