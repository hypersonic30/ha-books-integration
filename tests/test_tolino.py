"""Send-to-Tolino: HA fetches the ebook from Audiobookshelf and hands it to the tolino-bridge."""
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.books.const import DOMAIN

from .conftest import ABS, CHAPTARR, ENTRY_DATA

BRIDGE = "http://bridge.test:8199"
ITEM = {"media": {"metadata": {"title": "Das Reich der Dämmerung"},
                  "ebookFile": {"ebookFormat": "epub", "metadata": {"filename": "Dämmerung.epub"}}}}
JSON = {"Content-Type": "application/json"}


@pytest.fixture
async def tolino_entry(hass, monkeypatch):
    # The bridge watcher has its own tests; here it must not make requests of its own (its first poll runs in
    # the background and would create the HTTP session before aioclient_mock is in place).
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)
    entry = MockConfigEntry(domain=DOMAIN, title="Books",
                            data={**ENTRY_DATA, "tolino_url": BRIDGE, "tolino_token": "bridge-token"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _mock_abs(aioclient_mock, item=ITEM, content=b"PK-epub-bytes", cover=b"\xff\xd8\xff\xe0cover"):
    aioclient_mock.get(f"{ABS}/api/items/abc123", json=item, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/items/abc123/ebook", content=content)
    if cover is None:
        aioclient_mock.get(f"{ABS}/api/items/abc123/cover", status=404, json={}, headers=JSON)
    else:
        aioclient_mock.get(f"{ABS}/api/items/abc123/cover", content=cover)


async def test_status_disabled_without_bridge(hass, setup_entry, hass_client):
    resp = await (await hass_client()).get("/api/books/tolino")
    assert await resp.json() == {"enabled": False}


async def test_status_reports_bridge_state(hass, tolino_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{BRIDGE}/status", json={"logged_in": True, "last_error": None, "login_backoff_s": 0},
                       headers=JSON)
    body = await (await (await hass_client()).get("/api/books/tolino")).json()
    assert body["enabled"] and body["reachable"] and body["logged_in"]
    assert aioclient_mock.mock_calls[-1][3]["Authorization"] == "Bearer bridge-token"


async def test_status_bridge_down(hass, tolino_entry, hass_client, aioclient_mock):
    import aiohttp
    aioclient_mock.get(f"{BRIDGE}/status", exc=aiohttp.ClientConnectionError("boom"))
    body = await (await (await hass_client()).get("/api/books/tolino")).json()
    assert body["enabled"] and not body["reachable"] and body["error"] == "unreachable"


async def test_send_ok(hass, tolino_entry, hass_client, aioclient_mock):
    _mock_abs(aioclient_mock)
    aioclient_mock.post(f"{BRIDGE}/upload", json={"deliverableId": "bosh_1", "title": "T"}, headers=JSON)
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert resp.status == 200
    assert await resp.json() == {"ok": True, "filename": "Dämmerung.epub", "deliverableId": "bosh_1", "cover": None, "replaced": None}
    calls = {str(c[1]): c for c in aioclient_mock.mock_calls}
    assert calls[f"{ABS}/api/items/abc123/ebook"][3]["Authorization"] == "Bearer abs-token"
    upload = calls[f"{BRIDGE}/upload"]
    assert upload[3]["Authorization"] == "Bearer bridge-token"
    assert upload[2] is not None  # multipart form body


async def test_send_rejects_formats_nobody_can_convert(hass, tolino_entry, hass_client, aioclient_mock):
    item = {"media": {"ebookFile": {"ebookFormat": "cbz"}}}
    _mock_abs(aioclient_mock, item)
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert resp.status == 415 and (await resp.json())["code"] == "bad_type"
    assert not any("upload" in str(c[1]) for c in aioclient_mock.mock_calls)


async def test_send_item_without_ebook(hass, tolino_entry, hass_client, aioclient_mock):
    _mock_abs(aioclient_mock, {"media": {"metadata": {"title": "Hörbuch"}}})
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert resp.status == 422 and (await resp.json())["code"] == "no_ebook"


@pytest.mark.parametrize("bad", ["../../etc", "a/b", "", None, 5, "x" * 65])
async def test_send_rejects_bad_item_id(hass, tolino_entry, hass_client, aioclient_mock, bad):
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": bad})
    assert resp.status == 400
    assert aioclient_mock.call_count == 0  # nothing reached Audiobookshelf


async def test_send_not_configured(hass, setup_entry, hass_client):
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert resp.status == 503 and (await resp.json())["code"] == "not_configured"


@pytest.mark.parametrize("bridge_status,body,http,code", [
    (503, {"error": "captcha", "detail": "blocked"}, 503, "captcha"),
    (503, {"error": "login_backoff", "detail": "wait"}, 503, "login_backoff"),
    (401, {"error": "unauthorized"}, 502, "bridge_auth"),
    (502, {"error": "bosh", "detail": "x"}, 502, "bosh"),
])
async def test_send_maps_bridge_errors(hass, tolino_entry, hass_client, aioclient_mock,
                                       bridge_status, body, http, code):
    _mock_abs(aioclient_mock)
    aioclient_mock.post(f"{BRIDGE}/upload", status=bridge_status, json=body, headers=JSON)
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert resp.status == http
    assert (await resp.json())["code"] == code


async def test_send_bridge_unreachable(hass, tolino_entry, hass_client, aioclient_mock):
    import aiohttp
    _mock_abs(aioclient_mock)
    aioclient_mock.post(f"{BRIDGE}/upload", exc=aiohttp.ClientConnectionError("down"))
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert resp.status == 503 and (await resp.json())["code"] == "unreachable"


async def test_send_unsafe_filename_is_sanitized(hass, tolino_entry, hass_client, aioclient_mock):
    item = {"media": {"metadata": {"title": 'A/B: "C"'}, "ebookFile": {"ebookFormat": "epub", "metadata": {}}}}
    _mock_abs(aioclient_mock, item)
    aioclient_mock.post(f"{BRIDGE}/upload", json={"deliverableId": "x"}, headers=JSON)
    body = await (await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})).json()
    assert body["filename"] == "A_B_ _C_.epub"


async def test_send_requires_login(hass, tolino_entry, hass_client_no_auth):
    resp = await (await hass_client_no_auth()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert resp.status == 401


# --- config flow --------------------------------------------------------------

def _flow_ok(aioclient_mock):
    aioclient_mock.get(f"{CHAPTARR}/api/v1/system/status", json={"appName": "Chaptarr"})
    aioclient_mock.get(f"{ABS}/api/me", json={})


async def _run(hass, extra):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {**ENTRY_DATA, **extra})


async def test_flow_without_bridge_still_works(hass, aioclient_mock):
    _flow_ok(aioclient_mock)
    result = await _run(hass, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["tolino_url"] == ""


async def test_flow_with_bridge(hass, aioclient_mock):
    _flow_ok(aioclient_mock)
    aioclient_mock.get(f"{BRIDGE}/status", json={"logged_in": False, "last_error": "captcha"}, headers=JSON)
    result = await _run(hass, {"tolino_url": BRIDGE + "/", "tolino_token": " bridge-token "})
    assert result["type"] is FlowResultType.CREATE_ENTRY  # logged out at Thalia is not a config error
    assert result["data"]["tolino_url"] == BRIDGE and result["data"]["tolino_token"] == "bridge-token"


@pytest.mark.parametrize("extra,status,errors", [
    ({"tolino_url": BRIDGE, "tolino_token": "bad"}, 401, {"tolino_token": "tolino_invalid_auth"}),
    ({"tolino_url": BRIDGE, "tolino_token": "x"}, 500, {"tolino_url": "tolino_cannot_connect"}),
    ({"tolino_url": BRIDGE, "tolino_token": ""}, 200, {"tolino_token": "tolino_token_missing"}),
    ({"tolino_url": "", "tolino_token": "x"}, 200, {"tolino_url": "tolino_url_missing"}),
    ({"tolino_url": "bridge.test", "tolino_token": "x"}, 200, {"tolino_url": "invalid_url"}),
])
async def test_flow_bridge_errors(hass, aioclient_mock, extra, status, errors):
    _flow_ok(aioclient_mock)
    aioclient_mock.get(f"{BRIDGE}/status", status=status, json={"logged_in": True}, headers=JSON)
    result = await _run(hass, extra)
    assert result["type"] is FlowResultType.FORM and result["errors"] == errors


async def test_flow_rejects_non_bridge(hass, aioclient_mock):
    _flow_ok(aioclient_mock)
    aioclient_mock.get(f"{BRIDGE}/status", json={"something": "else"}, headers=JSON)
    result = await _run(hass, {"tolino_url": BRIDGE, "tolino_token": "x"})
    assert result["errors"] == {"tolino_url": "not_tolino_bridge"}


async def test_send_too_large_declared(hass, tolino_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{ABS}/api/items/abc123", json=ITEM, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/items/abc123/ebook", content=b"x", headers={"Content-Length": str(101 * 1024 * 1024)})
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert resp.status == 413 and (await resp.json())["code"] == "too_large"


async def test_send_too_large_streamed(hass, tolino_entry, hass_client, aioclient_mock, monkeypatch):
    monkeypatch.setattr("custom_components.books.tolino_send.TOLINO_MAX_BYTES", 10)
    _mock_abs(aioclient_mock, content=b"x" * 500_000)
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert resp.status == 413
    assert not any("upload" in str(c[1]) for c in aioclient_mock.mock_calls)


async def test_send_abs_error(hass, tolino_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{ABS}/api/items/abc123", status=404, json={"error": "nf"}, headers=JSON)
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert resp.status == 502 and (await resp.json())["code"] == "abs_error"


async def test_send_keeps_umlauts_in_multipart_filename(hass, hass_client, socket_enabled):
    """aiohttp percent-encodes multipart filenames by default and aiohttp servers (the bridge) don't decode
    them, so 'Dämmerung' would arrive as 'D%C3%A4mmerung'. Real sockets: this is about the wire format."""
    import socket
    from aiohttp import web

    seen = {}

    async def item(request):
        return web.json_response(ITEM)

    async def ebook(request):
        return web.Response(body=b"PK-bytes")

    async def upload(request):
        part = await (await request.multipart()).next()
        seen["name"], seen["bytes"] = part.filename, await part.read()
        return web.json_response({"deliverableId": "d1"})

    app = web.Application()
    app.add_routes([web.get("/api/items/abc123", item), web.get("/api/items/abc123/ebook", ebook),
                    web.post("/upload", upload), web.get("/status", item)])
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    base = f"http://127.0.0.1:{port}"
    try:
        entry = MockConfigEntry(domain=DOMAIN, title="Books", data={
            **ENTRY_DATA, "abs_url": base, "tolino_url": base, "tolino_token": "t"})
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
        assert resp.status == 200, await resp.text()
    finally:
        await runner.cleanup()
    assert seen == {"name": "Dämmerung.epub", "bytes": b"PK-bytes"}


async def test_send_forwards_the_abs_cover(hass, tolino_entry, hass_client, aioclient_mock):
    _mock_abs(aioclient_mock)
    aioclient_mock.post(f"{BRIDGE}/upload", json={"deliverableId": "d1", "cover": True}, headers=JSON)
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert (await resp.json())["cover"] is True
    cover_call = next(c for c in aioclient_mock.mock_calls if "/cover" in str(c[1]))
    assert cover_call[1].query.get("format") == "jpeg"
    assert cover_call[3]["Authorization"] == "Bearer abs-token"


async def test_send_without_cover_still_uploads(hass, tolino_entry, hass_client, aioclient_mock):
    _mock_abs(aioclient_mock, cover=None)
    aioclient_mock.post(f"{BRIDGE}/upload", json={"deliverableId": "d1", "cover": None}, headers=JSON)
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert resp.status == 200 and (await resp.json())["cover"] is None


# --- duplicate protection / replace ------------------------------------------------

def _bridge_upload(aioclient_mock, did):
    aioclient_mock.post(f"{BRIDGE}/upload", json={"deliverableId": did, "cover": True}, headers=JSON)


def _bridge_library(aioclient_mock, *ids):
    aioclient_mock.get(f"{BRIDGE}/library", json={"count": len(ids), "books": [
        {"title": "T", "kind": "upload", "deliverableId": i} for i in ids]}, headers=JSON)


def _uploads(aioclient_mock):
    return [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/upload")]


def _deletes(aioclient_mock):
    return [str(c[1]) for c in aioclient_mock.mock_calls if c[0] == "DELETE"]


async def _send(client, **extra):
    return await client.post("/api/books/tolino", json={"abs_item_id": "abc123", **extra})


async def test_first_send_is_remembered_and_shown_in_status(hass, tolino_entry, hass_client, aioclient_mock):
    _mock_abs(aioclient_mock); _bridge_upload(aioclient_mock, "d1")
    aioclient_mock.get(f"{BRIDGE}/status", json={"logged_in": True}, headers=JSON)
    c = await hass_client()
    assert (await _send(c)).status == 200
    body = await (await c.get("/api/books/tolino")).json()
    assert list(body["sent"]) == ["abc123"] and "at" in body["sent"]["abc123"]
    assert "d1" not in str(body["sent"])                      # cloud ids stay server-side


async def test_second_send_asks_instead_of_duplicating(hass, tolino_entry, hass_client, aioclient_mock):
    _mock_abs(aioclient_mock); _bridge_upload(aioclient_mock, "d1"); _bridge_library(aioclient_mock, "d1")
    c = await hass_client()
    await _send(c)
    resp = await _send(c)
    assert resp.status == 409
    body = await resp.json()
    assert body["code"] == "already_sent" and body["sent_at"]
    assert len(_uploads(aioclient_mock)) == 1                 # nothing uploaded the second time


async def test_deleted_in_cloud_means_send_again(hass, tolino_entry, hass_client, aioclient_mock):
    _mock_abs(aioclient_mock); _bridge_upload(aioclient_mock, "d1"); _bridge_library(aioclient_mock, "other")
    c = await hass_client()
    await _send(c)
    resp = await _send(c)                                     # d1 is gone from the cloud library
    assert resp.status == 200 and len(_uploads(aioclient_mock)) == 2
    assert _deletes(aioclient_mock) == []                     # nothing left to replace


async def test_bridge_down_during_check_still_protects(hass, tolino_entry, hass_client, aioclient_mock):
    import aiohttp
    _mock_abs(aioclient_mock); _bridge_upload(aioclient_mock, "d1")
    aioclient_mock.get(f"{BRIDGE}/library", exc=aiohttp.ClientConnectionError("down"))
    c = await hass_client()
    await _send(c)
    assert (await _send(c)).status == 409


async def test_force_replaces_the_old_copy(hass, tolino_entry, hass_client, aioclient_mock):
    _mock_abs(aioclient_mock); _bridge_upload(aioclient_mock, "d1")
    c = await hass_client()
    await _send(c)
    aioclient_mock.clear_requests()
    _mock_abs(aioclient_mock); _bridge_upload(aioclient_mock, "d2")
    aioclient_mock.delete(f"{BRIDGE}/book/d1", json={"deleted": "d1"}, headers=JSON)
    resp = await _send(c, force=True)
    body = await resp.json()
    assert resp.status == 200 and body["deliverableId"] == "d2" and body["replaced"] is True
    assert _deletes(aioclient_mock) == [f"{BRIDGE}/book/d1"]
    hass_reg = hass.data[DOMAIN]["tolino_sent"]
    assert hass_reg.get("abc123")["deliverableId"] == "d2"    # now points at the new copy


async def test_force_survives_failed_delete_of_old_copy(hass, tolino_entry, hass_client, aioclient_mock):
    _mock_abs(aioclient_mock); _bridge_upload(aioclient_mock, "d1")
    c = await hass_client()
    await _send(c)
    aioclient_mock.clear_requests()
    _mock_abs(aioclient_mock); _bridge_upload(aioclient_mock, "d2")
    aioclient_mock.delete(f"{BRIDGE}/book/d1", status=502, json={"error": "bosh"}, headers=JSON)
    body = await (await _send(c, force=True)).json()
    assert body["ok"] and body["replaced"] is False           # new copy is up; old one couldn't be removed


async def test_sent_registry_survives_reload(hass, tolino_entry, hass_client, aioclient_mock):
    _mock_abs(aioclient_mock); _bridge_upload(aioclient_mock, "d1")
    aioclient_mock.get(f"{BRIDGE}/status", json={"logged_in": True}, headers=JSON)
    c = await hass_client()
    await _send(c)
    hass.data[DOMAIN].pop("tolino_sent")                      # what a HA restart does to the in-memory copy
    assert await hass.config_entries.async_reload(tolino_entry.entry_id)
    await hass.async_block_till_done()
    assert "abc123" in (await (await c.get("/api/books/tolino")).json())["sent"]


async def test_replace_with_old_bridge_is_not_reported_as_replaced(hass, tolino_entry, hass_client, aioclient_mock):
    """An older bridge has no DELETE route (bare 404). The old copy is still in the cloud: say so."""
    _mock_abs(aioclient_mock); _bridge_upload(aioclient_mock, "d1")
    c = await hass_client()
    await _send(c)
    aioclient_mock.clear_requests()
    _mock_abs(aioclient_mock); _bridge_upload(aioclient_mock, "d2")
    aioclient_mock.delete(f"{BRIDGE}/book/d1", status=404, text="404: Not Found")
    assert (await (await _send(c, force=True)).json())["replaced"] is False


async def test_replace_treats_bridge_not_found_as_already_gone(hass, tolino_entry, hass_client, aioclient_mock):
    _mock_abs(aioclient_mock); _bridge_upload(aioclient_mock, "d1")
    c = await hass_client()
    await _send(c)
    aioclient_mock.clear_requests()
    _mock_abs(aioclient_mock); _bridge_upload(aioclient_mock, "d2")
    aioclient_mock.delete(f"{BRIDGE}/book/d1", status=404, json={"error": "not_found", "detail": "gone"}, headers=JSON)
    assert (await (await _send(c, force=True)).json())["replaced"] is True


# --- conversion (MOBI/AZW3/... are converted to EPUB by the bridge) ------------------

@pytest.mark.parametrize("fmt", ["mobi", "azw3", "azw", "prc", "fb2", "lit"])
async def test_convertible_formats_are_sent_to_the_bridge(hass, tolino_entry, hass_client, aioclient_mock, fmt):
    item = {"media": {"metadata": {"title": "Kindle Buch"}, "ebookFile": {"ebookFormat": fmt, "metadata": {"filename": f"Kindle Buch.{fmt}"}}}}
    _mock_abs(aioclient_mock, item); _bridge_upload(aioclient_mock, "d1")
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert resp.status == 200, await resp.text()
    assert len(_uploads(aioclient_mock)) == 1


async def test_convertible_upload_carries_the_original_filename(hass, tolino_entry, hass_client, socket_enabled):
    """The bridge decides by the file extension, so a .mobi must reach it as .mobi (real sockets: wire format)."""
    import socket
    from aiohttp import web
    seen = {}

    async def item(request):
        return web.json_response({"media": {"metadata": {"title": "X"}, "ebookFile": {"ebookFormat": "azw3", "metadata": {"filename": "Die Verwandlung.azw3"}}}})

    async def ebook(request):
        return web.Response(body=b"AZW3-bytes")

    async def upload(request):
        part = await (await request.multipart()).next()
        seen["name"], seen["ctype"] = part.filename, part.headers.get("Content-Type")
        return web.json_response({"deliverableId": "d1"})

    app = web.Application()
    app.add_routes([web.get("/api/items/abc123", item), web.get("/api/items/abc123/ebook", ebook), web.post("/upload", upload)])
    runner = web.AppRunner(app); await runner.setup()
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    base = f"http://127.0.0.1:{port}"
    try:
        entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA, "abs_url": base, "tolino_url": base, "tolino_token": "t"})
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id); await hass.async_block_till_done()
        resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
        assert resp.status == 200, await resp.text()
    finally:
        await runner.cleanup()
    assert seen == {"name": "Die Verwandlung.azw3", "ctype": "application/octet-stream"}


@pytest.mark.parametrize("bridge_status,code,http", [(415, "no_converter", 415), (422, "convert_failed", 422)])
async def test_conversion_errors_keep_their_code(hass, tolino_entry, hass_client, aioclient_mock, bridge_status, code, http):
    item = {"media": {"metadata": {"title": "K"}, "ebookFile": {"ebookFormat": "mobi", "metadata": {"filename": "K.mobi"}}}}
    _mock_abs(aioclient_mock, item)
    aioclient_mock.post(f"{BRIDGE}/upload", status=bridge_status, json={"error": code, "detail": "why"}, headers=JSON)
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert resp.status == http and (await resp.json())["code"] == code
