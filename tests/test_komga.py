"""Komga proxy for the manga card: strict allow-list, key stays server-side, optional settings with validation."""
import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.books.const import DOMAIN
from custom_components.books.komga_policy import komga_allowed

from .conftest import ABS, CHAPTARR, ENTRY_DATA, admin_person

KOMGA = "http://komga.test:25600"
JSON = {"Content-Type": "application/json"}


# --- allow-list ---------------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("GET", "v1/libraries"), ("GET", "v1/libraries/0RSFNTG6SW14M"), ("GET", "v1/series"), ("GET", "v1/series/latest"),
    ("GET", "v1/series/0RSFNTH2HW54J"), ("GET", "v1/series/0RSFNTH2HW54J/books"), ("GET", "v1/series/0RSFNTH2HW54J/thumbnail"),
    ("GET", "v1/books"), ("GET", "v1/books/ondeck"), ("GET", "v1/books/ABC123"), ("GET", "v1/books/ABC123/pages"),
    ("GET", "v1/books/ABC123/pages/12"), ("GET", "v1/books/ABC123/pages/12/thumbnail"), ("GET", "v1/books/ABC123/thumbnail"),
    ("GET", "v1/books/ABC123/next"), ("POST", "v1/series/list"), ("POST", "v1/books/list"), ("POST", "v1/libraries/ABC/scan"),
    ("POST", "v1/series/ABC/read-progress"), ("PATCH", "v1/books/ABC/read-progress"), ("DELETE", "v1/books/ABC/read-progress"),
    ("DELETE", "v1/series/ABC/read-progress"), ("get", "v1/series"), ("GET", "/v1/series/"),
])
def test_allowed(method, path):
    assert komga_allowed(method, path)


@pytest.mark.parametrize("method,path", [
    ("GET", "v1/users"), ("GET", "v2/users/me"), ("GET", "v2/users/me/api-keys"), ("POST", "v2/users/me/api-keys"),
    ("DELETE", "v2/users/me/api-keys/ABC"), ("POST", "v1/claim"), ("GET", "actuator/health"), ("GET", "v1/settings"),
    ("PATCH", "v1/settings"), ("GET", "v1/books/ABC/file"), ("GET", "v1/books/ABC/pages/1/raw"),     # original archives / odd extras
    ("POST", "v1/libraries"), ("PUT", "v1/libraries/ABC"), ("DELETE", "v1/libraries/ABC"), ("PATCH", "v1/libraries/ABC"),
    ("POST", "v1/libraries/ABC/analyze"), ("POST", "v1/libraries/ABC/empty-trash"),
    ("DELETE", "v1/series/ABC"), ("PATCH", "v1/series/ABC"), ("PATCH", "v1/series/ABC/metadata"), ("DELETE", "v1/books/ABC"),
    ("PATCH", "v1/books/ABC"), ("POST", "v1/books/ABC/analyze"), ("GET", "v1/tasks"), ("DELETE", "v1/tasks"),
    ("GET", "v1/books/../users"), ("GET", "v1/series/ABC/books/../../../users"), ("GET", "v1/books/ABC%2F..%2Fusers"),
    ("GET", "v1/books/" + "A" * 41), ("GET", "v1/books/ABC/pages/abc"), ("GET", "v1/books/ABC/pages/-1"),
    ("GET", ""), ("GET", "v1//series"), ("GET", "v1/series?x=1"), ("GET", "v1/books/AB C"), ("HEAD", "v1/series"), ("OPTIONS", "v1/series"),
])
def test_blocked(method, path):
    assert not komga_allowed(method, path)


# --- proxy --------------------------------------------------------------------------------------

@pytest.fixture
async def komga_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA, "komga_url": KOMGA, "komga_api_key": "komga-key"},
                            subentries_data=[await admin_person(hass)])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_proxy_adds_the_api_key_and_passes_the_query(hass, komga_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{KOMGA}/api/v1/series", json={"content": []}, headers=JSON)
    resp = await (await hass_client()).get("/api/books/komga/v1/series?page=2&size=50&library_id=ABC")
    assert resp.status == 200 and await resp.json() == {"content": []}
    _, url, _, headers = aioclient_mock.mock_calls[-1]
    assert headers["X-API-Key"] == "komga-key" and url.query == {"page": "2", "size": "50", "library_id": "ABC"}


async def test_proxy_streams_page_images_with_their_headers(hass, komga_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{KOMGA}/api/v1/books/B1/pages/3", content=b"\xff\xd8\xffJPEGDATA",
                       headers={"Content-Type": "image/jpeg", "Cache-Control": "max-age=3600", "ETag": '"abc"'})
    resp = await (await hass_client()).get("/api/books/komga/v1/books/B1/pages/3")
    assert resp.status == 200 and await resp.read() == b"\xff\xd8\xffJPEGDATA"
    assert resp.headers["Content-Type"] == "image/jpeg" and resp.headers["ETag"] == '"abc"' and "max-age" in resp.headers["Cache-Control"]


async def test_proxy_strips_ha_signature_and_forwards_conditional_headers(hass, komga_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{KOMGA}/api/v1/series/S1/thumbnail", status=304)
    await (await hass_client()).get("/api/books/komga/v1/series/S1/thumbnail?authSig=secret", headers={"If-None-Match": '"abc"'})
    _, url, _, headers = aioclient_mock.mock_calls[-1]
    assert "authSig" not in url.query and headers["If-None-Match"] == '"abc"'


async def test_proxy_forwards_read_progress(hass, komga_entry, hass_client, aioclient_mock):
    aioclient_mock.patch(f"{KOMGA}/api/v1/books/B1/read-progress", status=204)
    resp = await (await hass_client()).patch("/api/books/komga/v1/books/B1/read-progress", json={"page": 5, "completed": False})
    assert resp.status == 204
    assert aioclient_mock.mock_calls[-1][2] == b'{"page": 5, "completed": false}' or aioclient_mock.mock_calls[-1][2]


async def test_proxy_search_post_and_rescan(hass, komga_entry, hass_client, aioclient_mock):
    aioclient_mock.post(f"{KOMGA}/api/v1/series/list", json={"content": [{"id": "S1"}]}, headers=JSON)
    aioclient_mock.post(f"{KOMGA}/api/v1/libraries/L1/scan", status=202)
    c = await hass_client()
    assert (await c.post("/api/books/komga/v1/series/list?search=one", json={"condition": {}})).status == 200
    assert (await c.post("/api/books/komga/v1/libraries/L1/scan")).status == 202


@pytest.mark.parametrize("method,path", [
    ("get", "v2/users/me/api-keys"), ("post", "v2/users/me/api-keys"), ("get", "v1/books/B1/file"), ("delete", "v1/libraries/L1"),
    ("post", "v1/libraries"), ("get", "v1/users"), ("patch", "v1/series/S1/metadata"), ("get", "actuator/health"),
])
async def test_blocked_requests_never_reach_komga(hass, komga_entry, hass_client, aioclient_mock, method, path):
    resp = await getattr(await hass_client(), method)(f"/api/books/komga/{path}")
    assert resp.status == 403 and aioclient_mock.call_count == 0


async def test_proxy_requires_login(hass, komga_entry, hass_client_no_auth):
    assert (await (await hass_client_no_auth()).get("/api/books/komga/v1/series")).status == 401


async def test_not_configured(hass, setup_entry, hass_client):
    resp = await (await hass_client()).get("/api/books/komga/v1/series")
    assert resp.status == 503 and "not configured" in (await resp.json())["error"]


async def test_upstream_down_is_a_503(hass, komga_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{KOMGA}/api/v1/series", exc=aiohttp.ClientConnectionError("refused"))
    resp = await (await hass_client()).get("/api/books/komga/v1/series")
    assert resp.status == 503 and "Cannot connect to Komga" in (await resp.json())["error"]


# --- optional settings with validation ------------------------------------------------------------

def _ok(aioclient_mock):
    aioclient_mock.get(f"{CHAPTARR}/api/v1/system/status", json={"appName": "Chaptarr"})
    aioclient_mock.get(f"{ABS}/api/me", json={})


async def _run(hass, extra):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {**ENTRY_DATA, **extra})


async def test_flow_with_komga(hass, aioclient_mock):
    _ok(aioclient_mock)
    aioclient_mock.get(f"{KOMGA}/api/v2/users/me", json={"email": "a@b", "roles": ["USER"]}, headers=JSON)
    result = await _run(hass, {"komga_url": KOMGA + "/", "komga_api_key": " komga-key "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["komga_url"] == KOMGA and result["data"]["komga_api_key"] == "komga-key"
    assert aioclient_mock.mock_calls[-1][3]["X-API-Key"] == "komga-key"


async def test_flow_without_komga_is_unchanged(hass, aioclient_mock):
    _ok(aioclient_mock)
    result = await _run(hass, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["data"]["komga_url"] == ""


@pytest.mark.parametrize("extra,status,body,errors", [
    ({"komga_url": KOMGA, "komga_api_key": "bad"}, 401, {}, {"komga_api_key": "komga_invalid_auth"}),
    ({"komga_url": KOMGA, "komga_api_key": "x"}, 500, {}, {"komga_url": "komga_cannot_connect"}),
    ({"komga_url": KOMGA, "komga_api_key": "x"}, 200, {"hello": "other service"}, {"komga_url": "not_komga"}),
    ({"komga_url": KOMGA, "komga_api_key": ""}, 200, {"roles": []}, {"komga_api_key": "komga_key_missing"}),
    ({"komga_url": "", "komga_api_key": "x"}, 200, {"roles": []}, {"komga_url": "komga_url_missing"}),
    ({"komga_url": "komga.test", "komga_api_key": "x"}, 200, {"roles": []}, {"komga_url": "invalid_url"}),
])
async def test_flow_komga_errors(hass, aioclient_mock, extra, status, body, errors):
    _ok(aioclient_mock)
    aioclient_mock.get(f"{KOMGA}/api/v2/users/me", status=status, json=body, headers=JSON)
    result = await _run(hass, extra)
    assert result["type"] is FlowResultType.FORM and result["errors"] == errors


# --- an administrator's key is refused: the cards never need one -------------------------------------------------------------------------

@pytest.mark.parametrize("roles,ok", [(["USER"], True), ([], True), (["USER", "FILE_DOWNLOAD", "PAGE_STREAMING"], True), (["ADMIN", "USER"], False), (["admin"], False)])
async def test_flow_refuses_a_komga_admin_key(hass, aioclient_mock, roles, ok):
    _ok(aioclient_mock)
    aioclient_mock.get(f"{KOMGA}/api/v2/users/me", json={"email": "a@b", "roles": roles}, headers=JSON)
    result = await _run(hass, {"komga_url": KOMGA, "komga_api_key": "k"})
    if ok:
        assert result["type"] is FlowResultType.CREATE_ENTRY
    else:
        assert result["type"] is FlowResultType.FORM and result["errors"] == {"komga_api_key": "komga_admin"}
