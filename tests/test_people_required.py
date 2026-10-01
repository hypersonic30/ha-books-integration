"""People are required: whoever uses the cards needs a person (own accounts, own progress). No person -> no access, with a clear
reason, and a repair hint in Home Assistant while nobody has been added at all."""
import pytest
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.books.const import DOMAIN

from .conftest import ENTRY_DATA, person
from .test_users import BRIDGE, KOMGA, MYLAR, As  # noqa: F401 - shared helpers

ENDPOINTS = [
    ("get", "/api/books/chaptarr/queue"), ("get", "/api/books/chaptarr-media/MediaCover/1/poster.jpg"), ("get", "/api/books/abs/me"),
    ("get", "/api/books/komga/v1/series"), ("post", "/api/books/komga/v1/series/list"), ("get", "/api/books/mylar/getIndex"),
    ("post", "/api/books/mylar/addComic?id=1"), ("post", "/api/books/add"), ("get", "/api/books/tolino"), ("post", "/api/books/tolino"),
    ("post", "/api/books/tolino-sync"), ("post", "/api/books/tolino-autosend"),
]


async def _entry(hass, with_person):
    users = {n: await hass.auth.async_create_user(n.title(), group_ids=["system-users"]) for n in ("anna", "ben")}
    subs = [person(users["anna"])] if with_person else []
    entry = MockConfigEntry(domain=DOMAIN, title="Books", subentries_data=subs, data={
        **ENTRY_DATA, "komga_url": KOMGA, "komga_api_key": "k", "mylar_url": MYLAR, "mylar_api_key": "m",
        "tolino_url": BRIDGE, "tolino_token": "t"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return users, entry


@pytest.fixture
def login(hass, hass_client_no_auth):
    async def _login(user):
        refresh = await hass.auth.async_create_refresh_token(user, "https://books.test/")
        return As(await hass_client_no_auth(), hass.auth.async_create_access_token(refresh))
    return _login


@pytest.fixture(autouse=True)
def quiet_watcher(monkeypatch):
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False, "accounts": {}}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)


@pytest.mark.parametrize("method,path", ENDPOINTS)
async def test_somebody_without_a_person_gets_nothing_and_nothing_reaches_the_servers(hass, login, aioclient_mock, method, path):
    users, _ = await _entry(hass, with_person=True)                           # Anna is a person, Ben is not
    r = await getattr(await login(users["ben"]), method)(path, **({"json": {}} if method == "post" else {}))
    assert r.status == 403 and (await r.json())["code"] == "no_person"
    assert aioclient_mock.call_count == 0


@pytest.mark.parametrize("method,path", ENDPOINTS[:4])
async def test_with_no_person_at_all_nobody_gets_in(hass, login, aioclient_mock, method, path):
    users, _ = await _entry(hass, with_person=False)
    r = await getattr(await login(users["anna"]), method)(path)
    assert r.status == 403 and (await r.json())["code"] == "no_person" and aioclient_mock.call_count == 0


async def test_a_person_gets_in(hass, login, aioclient_mock):
    users, _ = await _entry(hass, with_person=True)
    aioclient_mock.get(f"{KOMGA}/api/v1/series", json={}, headers={"Content-Type": "application/json"})
    assert (await (await login(users["anna"])).get("/api/books/komga/v1/series")).status == 200


async def test_the_message_says_what_to_do(hass, login):
    users, _ = await _entry(hass, with_person=True)
    body = await (await (await login(users["ben"])).get("/api/books/abs/me")).json()
    assert "Add person" in body["error"] and "administrator" in body["error"]


async def test_a_repair_hint_while_nobody_has_been_added(hass):
    users, entry = await _entry(hass, with_person=False)
    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, "no_person")
    assert issue is not None and issue.translation_key == "no_person" and not issue.is_fixable


async def test_no_repair_hint_once_there_is_a_person(hass):
    users, entry = await _entry(hass, with_person=True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "no_person") is None


async def test_the_hint_goes_away_when_the_first_person_is_added_and_comes_back_when_the_last_is_removed(hass):
    from homeassistant.config_entries import ConfigSubentry
    users, entry = await _entry(hass, with_person=False)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "no_person")
    sub = ConfigSubentry(data=person(users["anna"]).data if hasattr(person(users["anna"]), "data") else person(users["anna"])["data"],
                         subentry_type="user", title="Anna", unique_id=users["anna"].id)
    hass.config_entries.async_add_subentry(entry, sub)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "no_person") is None
    hass.config_entries.async_remove_subentry(entry, sub.subentry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "no_person") is not None


def test_the_issue_has_its_texts():
    import json
    from pathlib import Path
    base = Path(__file__).parent.parent / "custom_components" / "books"
    for f in ("strings.json", "translations/en.json", "translations/de.json"):
        issue = json.loads((base / f).read_text())["issues"]["no_person"]
        assert issue["title"] and issue["description"], f
