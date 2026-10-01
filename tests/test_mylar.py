"""Mylar3 proxy for the manga card: per-command allow-list, key stays server-side, slow searches run in the background."""
import asyncio

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.books.const import DOMAIN
from custom_components.books.mylar_policy import mylar_request

from .conftest import ABS, CHAPTARR, ENTRY_DATA

MYLAR = "http://mylar.test:8090"
JSON = {"Content-Type": "application/json"}
OK = {"success": True, "data": "ok"}


# --- allow-list -----------------------------------------------------------------------------------

@pytest.mark.parametrize("method,cmd,query,background", [
    ("GET", "findComic", {"name": "Attack on Titan"}, False), ("GET", "findComic", {"name": "Ä ö ü – 進撃"}, False),
    ("GET", "getIndex", {}, False), ("GET", "getComic", {"id": "72459"}, False), ("GET", "getWanted", {}, False),
    ("GET", "getHistory", {}, False), ("POST", "addComic", {"id": "72459"}, False), ("POST", "queueIssue", {"id": "448514"}, True),
    ("POST", "unqueueIssue", {"id": "448514"}, False), ("POST", "forceSearch", {}, True),
    ("POST", "pauseComic", {"id": "72459"}, False), ("POST", "resumeComic", {"id": "72459"}, False),
    ("post", "addComic", {"id": "72459", "authSig": "x"}, False),
])
def test_allowed(method, cmd, query, background):
    checked = mylar_request(method, cmd, query)
    assert checked is not None and checked[1] is background
    assert checked[0]["cmd"] == cmd and "authSig" not in checked[0]


@pytest.mark.parametrize("method,cmd,query", [
    # the dangerous half of Mylar's API
    ("POST", "delComic", {"id": "1"}), ("GET", "delComic", {"id": "1"}), ("POST", "shutdown", {}), ("POST", "restart", {}),
    ("POST", "update", {}), ("GET", "getAPI", {}), ("GET", "getLogs", {}), ("POST", "clearLogs", {}),
    ("GET", "listProviders", {}), ("POST", "addProvider", {"name": "x"}), ("POST", "delProvider", {"name": "x"}),
    ("POST", "changeProvider", {"name": "x"}), ("POST", "forceProcess", {"nzb_name": "x", "nzb_folder": "/etc"}),
    ("POST", "issueProcess", {"comicid": "1", "folder": "/"}), ("GET", "downloadNZB", {"nzbname": "x"}), ("POST", "changeStatus", {"id": "1"}),
    ("", "findComic", {"name": "x"}), ("GET", "", {}), ("GET", "doesnotexist", {}),
    # right command, wrong verb (reads are GET, anything that changes state is POST)
    ("POST", "findComic", {"name": "x"}), ("GET", "addComic", {"id": "1"}), ("GET", "queueIssue", {"id": "1"}), ("GET", "forceSearch", {}),
    ("DELETE", "getIndex", {}), ("PUT", "addComic", {"id": "1"}),
    # parameters: nothing extra, nothing missing, nothing odd
    ("GET", "findComic", {}), ("GET", "findComic", {"name": ""}), ("GET", "findComic", {"name": "a" * 101}),
    ("GET", "findComic", {"name": "x\nINJECT"}), ("GET", "findComic", {"name": "x", "apikey": "attacker"}),
    ("GET", "findComic", {"name": "x", "cmd": "delComic"}), ("GET", "getIndex", {"id": "1"}), ("GET", "getWanted", {"issues": "True"}),
    ("POST", "addComic", {}), ("POST", "addComic", {"id": ""}), ("POST", "addComic", {"id": "1&cmd=delComic"}),
    ("POST", "addComic", {"id": "1", "extra": "x"}), ("POST", "addComic", {"id": "../1"}), ("POST", "addComic", {"id": "1" * 25}),
    ("POST", "forceSearch", {"id": "1"}),
])
def test_blocked(method, cmd, query):
    assert mylar_request(method, cmd, query) is None


# --- proxy ----------------------------------------------------------------------------------------

@pytest.fixture
async def mylar_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA, "mylar_url": MYLAR, "mylar_api_key": "mylar-key"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _upstream(aioclient_mock):
    q = aioclient_mock.mock_calls[-1][1].query
    return dict(q)


async def test_search_adds_cmd_and_key_and_returns_the_list(hass, mylar_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{MYLAR}/api", json=[{"name": "Attack on Titan", "comicid": "72459"}], headers=JSON)
    resp = await (await hass_client()).get("/api/books/mylar/findComic?name=Attack%20on%20Titan&authSig=secret")
    assert resp.status == 200 and await resp.json() == [{"name": "Attack on Titan", "comicid": "72459"}]
    assert _upstream(aioclient_mock) == {"cmd": "findComic", "name": "Attack on Titan", "apikey": "mylar-key"}


async def test_client_cannot_smuggle_its_own_key_or_command(hass, mylar_entry, hass_client, aioclient_mock):
    c = await hass_client()
    for q in ("findComic?name=x&apikey=other", "findComic?name=x&cmd=delComic", "getIndex?cmd=delComic"):
        assert (await c.get(f"/api/books/mylar/{q}")).status == 403
    assert aioclient_mock.call_count == 0


async def test_add_is_a_post_and_waits_for_mylar(hass, mylar_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{MYLAR}/api", json=OK, headers=JSON)
    resp = await (await hass_client()).post("/api/books/mylar/addComic?id=72459")
    assert resp.status == 200 and (await resp.json())["success"] is True
    assert _upstream(aioclient_mock) == {"cmd": "addComic", "id": "72459", "apikey": "mylar-key"}


async def test_mylar_errors_pass_through(hass, mylar_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{MYLAR}/api", json={"success": False, "error": {"code": 460, "message": "Missing API key"}}, headers=JSON)
    resp = await (await hass_client()).get("/api/books/mylar/getIndex")
    assert resp.status == 200 and (await resp.json())["success"] is False


async def test_a_slow_search_does_not_block_the_card(hass, mylar_entry, hass_client, aioclient_mock):
    """queueIssue only answers when every indexer has been asked (> 60 s was seen live): the card gets 202 at once."""
    gate = asyncio.Event()

    async def slow(method, url, data):
        await gate.wait()
        return AiohttpClientMockResponse(method, url, json=OK, headers=JSON)

    aioclient_mock.get(f"{MYLAR}/api", side_effect=slow)
    c = await hass_client()
    resp = await asyncio.wait_for(c.post("/api/books/mylar/queueIssue?id=448514"), timeout=5)   # returns although Mylar is still busy
    assert resp.status == 202 and (await resp.json())["data"] == "queued"
    # a second click while the first search is still running must not start another one
    assert (await c.post("/api/books/mylar/queueIssue?id=448514")).status == 202
    await asyncio.sleep(0)
    assert aioclient_mock.call_count == 1 and _upstream(aioclient_mock) == {"cmd": "queueIssue", "id": "448514", "apikey": "mylar-key"}
    gate.set()
    await hass.async_block_till_done()
    # finished: the same issue can be queued again
    assert (await c.post("/api/books/mylar/queueIssue?id=448514")).status == 202
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 2


async def test_failed_background_search_is_survivable(hass, mylar_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{MYLAR}/api", exc=aiohttp.ClientConnectionError("refused"))
    c = await hass_client()
    assert (await c.post("/api/books/mylar/forceSearch")).status == 202
    await hass.async_block_till_done()
    assert (await c.post("/api/books/mylar/forceSearch")).status == 202       # not stuck as "running"
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 2


@pytest.mark.parametrize("method,path", [
    ("post", "delComic?id=1"), ("get", "getLogs"), ("post", "shutdown"), ("get", "getAPI"), ("post", "forceProcess?nzb_name=x&nzb_folder=/"),
    ("get", "listProviders"), ("get", ""), ("post", "findComic?name=x"),
])
async def test_blocked_requests_never_reach_mylar(hass, mylar_entry, hass_client, aioclient_mock, method, path):
    resp = await getattr(await hass_client(), method)(f"/api/books/mylar/{path}")
    assert resp.status == 403 and aioclient_mock.call_count == 0


async def test_proxy_requires_login(hass, mylar_entry, hass_client_no_auth):
    assert (await (await hass_client_no_auth()).get("/api/books/mylar/getIndex")).status == 401


async def test_not_configured(hass, setup_entry, hass_client):
    resp = await (await hass_client()).get("/api/books/mylar/getIndex")
    assert resp.status == 503 and "not configured" in (await resp.json())["error"]


async def test_upstream_down_is_a_503(hass, mylar_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{MYLAR}/api", exc=aiohttp.ClientConnectionError("refused"))
    resp = await (await hass_client()).get("/api/books/mylar/getIndex")
    assert resp.status == 503 and "Cannot connect to Mylar" in (await resp.json())["error"]


# --- optional settings with validation --------------------------------------------------------------

def _ok(aioclient_mock):
    aioclient_mock.get(f"{CHAPTARR}/api/v1/system/status", json={"appName": "Chaptarr"})
    aioclient_mock.get(f"{ABS}/api/me", json={})


async def _run(hass, extra):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {**ENTRY_DATA, **extra})


VERSION = {"success": True, "data": {"install_type": "docker", "current_version": "abc"}}


async def test_flow_with_mylar(hass, aioclient_mock):
    _ok(aioclient_mock)
    aioclient_mock.get(f"{MYLAR}/api", json=VERSION, headers=JSON)
    result = await _run(hass, {"mylar_url": MYLAR + "/", "mylar_api_key": " mylar-key "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["mylar_url"] == MYLAR and result["data"]["mylar_api_key"] == "mylar-key"
    assert _upstream(aioclient_mock) == {"cmd": "getVersion", "apikey": "mylar-key"}


async def test_flow_without_mylar_is_unchanged(hass, aioclient_mock):
    _ok(aioclient_mock)
    result = await _run(hass, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["data"]["mylar_url"] == ""


@pytest.mark.parametrize("extra,status,body,errors", [
    # Mylar answers HTTP 200 for a wrong/missing key - the body says so
    ({"mylar_url": MYLAR, "mylar_api_key": "bad"}, 200, {"success": False, "error": {"code": 460, "message": "Missing API key"}}, {"mylar_api_key": "mylar_invalid_auth"}),
    ({"mylar_url": MYLAR, "mylar_api_key": "x"}, 401, {}, {"mylar_api_key": "mylar_invalid_auth"}),
    ({"mylar_url": MYLAR, "mylar_api_key": "x"}, 500, {}, {"mylar_url": "mylar_cannot_connect"}),
    ({"mylar_url": MYLAR, "mylar_api_key": "x"}, 200, {"hello": "other service"}, {"mylar_url": "not_mylar"}),
    ({"mylar_url": MYLAR, "mylar_api_key": "x"}, 200, {"success": True, "data": []}, {"mylar_url": "not_mylar"}),
    ({"mylar_url": MYLAR, "mylar_api_key": "x"}, 200, {"success": False, "error": {"code": 500}}, {"mylar_url": "not_mylar"}),
    ({"mylar_url": MYLAR, "mylar_api_key": ""}, 200, VERSION, {"mylar_api_key": "mylar_key_missing"}),
    ({"mylar_url": "", "mylar_api_key": "x"}, 200, VERSION, {"mylar_url": "mylar_url_missing"}),
    ({"mylar_url": "mylar.test", "mylar_api_key": "x"}, 200, VERSION, {"mylar_url": "invalid_url"}),
])
async def test_flow_mylar_errors(hass, aioclient_mock, extra, status, body, errors):
    _ok(aioclient_mock)
    aioclient_mock.get(f"{MYLAR}/api", status=status, json=body, headers=JSON)
    result = await _run(hass, extra)
    assert result["type"] is FlowResultType.FORM and result["errors"] == errors


# --- Mylar's plain-text answers ("OK") must reach the card as JSON ----------------------------------

async def test_plain_text_ok_becomes_json(hass, mylar_entry, hass_client, aioclient_mock):
    """unqueueIssue answers the text 'OK' (no JSON): the card's callApi failed with 'unable to parse json response'."""
    aioclient_mock.get(f"{MYLAR}/api", text="OK")
    resp = await (await hass_client()).post("/api/books/mylar/unqueueIssue?id=448514")
    assert resp.status == 200 and resp.headers["Content-Type"].startswith("application/json")
    assert await resp.json() == {"success": True, "data": "OK"}


async def test_plain_text_error_becomes_a_json_error(hass, mylar_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{MYLAR}/api", text="Internal Server Error", status=500)
    resp = await (await hass_client()).post("/api/books/mylar/addComic?id=1")
    body = await resp.json()
    assert resp.status == 500 and body["success"] is False and "Internal Server Error" in body["error"]["message"]


async def test_real_json_is_passed_through_untouched(hass, mylar_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{MYLAR}/api", json=[{"name": "x"}], headers=JSON)
    resp = await (await hass_client()).get("/api/books/mylar/findComic?name=x")
    assert await resp.json() == [{"name": "x"}]
