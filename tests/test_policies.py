"""Chaptarr and Audiobookshelf: strict allow-lists (the proxies used to pass everything except Chaptarr's settings), and paths without tricks.
The first list in each block is exactly what the cards call (checked against books-card.js); anything beyond is refused."""
import pytest

from custom_components.books.abs_policy import abs_allowed
from custom_components.books.chaptarr_policy import chaptarr_allowed

from .conftest import ABS, CHAPTARR

H = {"Content-Type": "application/json"}

# --- what books-card.js sends today -------------------------------------------------------------------------------------------------
CARD_CHAPTARR = [("GET", "book/lookup"), ("GET", "search"), ("GET", "queue"), ("GET", "wanted/missing"), ("POST", "command")]
CARD_ABS = [("GET", "libraries"), ("GET", "libraries/lib_1/items"), ("GET", "me"), ("GET", "me/items-in-progress"), ("GET", "items/li_abc-123"),
            ("GET", "items/li_abc-123/cover"), ("GET", "items/li_abc-123/ebook"), ("GET", "items/li_abc-123/file/987654321"),
            ("POST", "items/li_abc-123/play"), ("POST", "session/play_1/sync"), ("POST", "session/play_1/close"), ("PATCH", "me/progress/li_abc-123")]


@pytest.mark.parametrize("method,path", CARD_CHAPTARR + [("GET", "history"), ("GET", "calendar"), ("GET", "book/12"), ("GET", "author/3"), ("get", "/queue/"),
                                                         ("GET", "queue/status"), ("GET", "release"), ("GET", "author/lookup")])
def test_chaptarr_allowed(method, path):
    assert chaptarr_allowed(method, path)


@pytest.mark.parametrize("method,path", [
    ("DELETE", "book/5"), ("DELETE", "author/3"), ("DELETE", "queue/7"), ("DELETE", "blocklist/1"), ("PUT", "book/5"), ("PUT", "author/3"),
    ("POST", "book"), ("POST", "author"), ("POST", "book/monitor"), ("POST", "blocklist/bulk"), ("POST", "release"), ("POST", "queue/grab/1"),
    ("GET", "indexer"), ("GET", "system/status"), ("GET", "config/host"), ("GET", "apikey"), ("GET", "user"), ("GET", "backup"), ("GET", "log"),
    ("GET", "downloadclient"), ("GET", "rootfolder"), ("GET", "filesystem"), ("GET", "book/abc"), ("GET", "book/5/files"), ("GET", "book/1234567890"),
    ("PATCH", "book/5"), ("HEAD", "queue"), ("OPTIONS", "queue"), ("GET", ""), ("GET", "queue/../indexer"), ("GET", "queue?x=1"),
])
def test_chaptarr_blocked(method, path):
    assert not chaptarr_allowed(method, path)


@pytest.mark.parametrize("method,path", CARD_ABS + [("get", "/libraries/"), ("GET", "items/li_1/ebook/ino9"), ("GET", "me/progress/li_1")])
def test_abs_allowed(method, path):
    assert abs_allowed(method, path)


@pytest.mark.parametrize("method,path", [
    ("GET", "users"), ("GET", "users/root"), ("GET", "api-keys"), ("GET", "tokens"), ("GET", "backups"), ("GET", "filesystem"), ("GET", "settings"),
    ("GET", "libraries/lib_1/stats"), ("GET", "libraries/lib_1/authors"), ("GET", "items/li_1/cover/../x"), ("GET", "me/listening-stats"),
    ("DELETE", "items/li_1"), ("DELETE", "libraries/lib_1"), ("DELETE", "me/progress/li_1"), ("PATCH", "items/li_1/media"), ("PATCH", "libraries/lib_1"),
    ("POST", "libraries/lib_1/scan"), ("POST", "libraries"), ("POST", "items/li_1/scan"), ("POST", "upload"), ("POST", "users"), ("POST", "authorize"),
    ("PUT", "items/li_1"), ("GET", "items/li_1/file/1/../../users"), ("GET", "items/" + "a" * 65), ("HEAD", "libraries"), ("GET", ""),
])
def test_abs_blocked(method, path):
    assert not abs_allowed(method, path)


# --- through the real proxy: nothing blocked reaches the server ---------------------------------------------------------------------------
@pytest.mark.parametrize("method,path", [
    ("delete", "book/5?deleteFiles=true"), ("delete", "author/3?deleteFiles=true"), ("put", "book/5"), ("post", "blocklist/bulk"), ("post", "book/monitor"),
    ("get", "indexer"), ("get", "apikey"), ("delete", "queue/7"), ("post", "release"),
])
async def test_destructive_chaptarr_requests_never_reach_chaptarr(hass, setup_entry, hass_client, aioclient_mock, method, path):
    resp = await getattr(await hass_client(), method)(f"/api/books/chaptarr/{path}", **({"json": {}} if method in ("put", "post") else {}))
    assert resp.status == 403 and aioclient_mock.call_count == 0


@pytest.mark.parametrize("method,path", [
    ("get", "users"), ("get", "api-keys"), ("delete", "items/abc"), ("post", "libraries/x/scan"), ("get", "backups"), ("patch", "libraries/x"),
    ("get", "libraries/x/stats"), ("delete", "me/progress/abc"),
])
async def test_dangerous_audiobookshelf_requests_never_reach_audiobookshelf(hass, setup_entry, hass_client, aioclient_mock, method, path):
    resp = await getattr(await hass_client(), method)(f"/api/books/abs/{path}", **({"json": {}} if method in ("post", "patch") else {}))
    assert resp.status == 403 and aioclient_mock.call_count == 0


@pytest.mark.parametrize("path", ["queue/..%5Cindexer", "queue/%252e%252e/x", "queue/a%00b", "queue/a\\b"])
async def test_odd_paths_are_refused_before_anything_else(hass, setup_entry, hass_client, aioclient_mock, path):
    from yarl import URL
    for prefix in ("chaptarr", "abs", "komga/v1", "mylar"):
        resp = await (await hass_client()).get(URL(f"/api/books/{prefix}/{path}", encoded=True))
        assert resp.status in (400, 403) and aioclient_mock.call_count == 0, (prefix, path, resp.status)


async def test_what_the_cards_need_still_works_end_to_end(hass, setup_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{CHAPTARR}/api/v1/queue", json={"records": []}, headers=H)
    aioclient_mock.get(f"{CHAPTARR}/api/v1/wanted/missing", json={"records": []}, headers=H)
    aioclient_mock.get(f"{CHAPTARR}/api/v1/book/lookup", json=[], headers=H)
    aioclient_mock.post(f"{CHAPTARR}/api/v1/command", json={"id": 1}, headers=H)
    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": []}, headers=H)
    aioclient_mock.get(f"{ABS}/api/items/li_1/file/12", content=b"audio")
    aioclient_mock.patch(f"{ABS}/api/me/progress/li_1", json={}, headers=H)
    aioclient_mock.post(f"{ABS}/api/session/s1/sync", json={}, headers=H)
    c = await hass_client()
    assert (await c.get("/api/books/chaptarr/queue?page=1&pageSize=50&includeBook=true")).status == 200
    assert (await c.get("/api/books/chaptarr/wanted/missing?page=1")).status == 200
    assert (await c.get("/api/books/chaptarr/book/lookup?term=a..b")).status == 200              # dots in the QUERY are fine
    assert (await c.post("/api/books/chaptarr/command", json={"name": "BookSearch", "bookIds": [1]})).status == 200
    assert (await c.get("/api/books/abs/libraries")).status == 200
    assert (await (await c.get("/api/books/abs/items/li_1/file/12")).read()) == b"audio"
    assert (await c.patch("/api/books/abs/me/progress/li_1", json={"currentTime": 1})).status == 200
    assert (await c.post("/api/books/abs/session/s1/sync", json={})).status == 200
