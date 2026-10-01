"""Diagnostics that can be shared (every secret blanked), and a repair hint when a service refuses a key."""
import json

import pytest
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.books.const import DOMAIN
from custom_components.books.diagnostics import async_get_config_entry_diagnostics

from .conftest import ABS, CHAPTARR, ENTRY_DATA, admin_person
from .test_komga import KOMGA
from .test_mylar import MYLAR

H = {"Content-Type": "application/json"}
SECRETS = ["chap-secret-key", "abs-secret-token", "komga-secret-key", "mylar-secret-key", "bridge-secret-token", "person-komga-secret", "person-abs-secret",
           "notify.secret_phone"]


async def _setup(hass, **person):
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={
        **ENTRY_DATA, "chaptarr_api_key": "chap-secret-key", "abs_token": "abs-secret-token", "komga_url": KOMGA, "komga_api_key": "komga-secret-key",
        "mylar_url": MYLAR, "mylar_api_key": "mylar-secret-key", "tolino_url": "http://bridge.test:8099", "tolino_token": "bridge-secret-token",
        "notify_service": "notify.secret_phone"},
        subentries_data=[await admin_person(hass, komga_api_key="person-komga-secret", abs_token="person-abs-secret", notify_service="notify.secret_phone",
                                            tolino=True, auto_send=True, **person)])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture(autouse=True)
def quiet_watcher(monkeypatch):
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False, "accounts": {}, "last_error": None}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)


# --- diagnostics -----------------------------------------------------------------------------------------------------------------------------

async def test_the_diagnostics_contain_no_secret_at_all(hass):
    entry = await _setup(hass)
    text = json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str)
    for secret in SECRETS:
        assert secret not in text, secret
    users = await hass.auth.async_get_users()
    assert not any(u.id in text for u in users)                                   # not even the Home Assistant user ids


async def test_the_diagnostics_still_say_what_is_going_on(hass):
    entry = await _setup(hass)
    d = await async_get_config_entry_diagnostics(hass, entry)
    assert d["entry"]["chaptarr_api_key"] == "**REDACTED**" and d["entry"]["komga_url"]                 # the structure stays, the secrets go
    person = d["people"][0]
    assert person["tolino"] is True and person["auto_send"] is True and person["komga_api_key"] == "**REDACTED**"
    assert d["jobs"]["default"]["auto_send"]["enabled"] is True and d["jobs"]["default"]["sent_books"] == 0
    assert d["waiting_for_arrival"] == {"book": 0, "manga": 0} and d["bridge"]["reachable"] is True


async def test_diagnostics_work_on_a_bare_entry_too(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    d = await async_get_config_entry_diagnostics(hass, entry)
    assert d["people"] == [] and d["jobs"] == {}


async def test_the_diagnostics_show_what_is_waiting_for_arrival(hass):
    entry = await _setup(hass)
    wishes = hass.data[DOMAIN]["wishes"]
    uid = next(iter(hass.data[DOMAIN]["users"]))
    await wishes.async_add("book", uid, title="Ein Buch"); await wishes.async_add("manga", uid, issue="448514")
    d = await async_get_config_entry_diagnostics(hass, entry)
    assert d["waiting_for_arrival"] == {"book": 1, "manga": 1} and "Ein Buch" not in json.dumps(d)       # counts only, no titles


# --- repair hints for a refused key -------------------------------------------------------------------------------------------------------------

def _issues(hass):
    return {i[1] for i in ir.async_get(hass).issues if i[0] == DOMAIN and i[1].startswith("auth_")}


@pytest.mark.parametrize("service,path,mock,expected", [
    ("Komga", "/api/books/komga/v1/series", lambda m: m.get(f"{KOMGA}/api/v1/series", status=401, json={}, headers=H), "auth_komga_"),
    ("Chaptarr", "/api/books/chaptarr/queue", lambda m: m.get(f"{CHAPTARR}/api/v1/queue", status=401, json={}, headers=H), "auth_chaptarr_"),
    ("Audiobookshelf", "/api/books/abs/libraries", lambda m: m.get(f"{ABS}/api/libraries", status=401, json={}, headers=H), "auth_audiobookshelf_"),
    ("Mylar", "/api/books/mylar/getIndex", lambda m: m.get(f"{MYLAR}/api", json={"success": False, "error": {"code": 460, "message": "Missing API key"}}, headers=H), "auth_mylar_"),
])
async def test_a_refused_key_raises_a_hint_for_that_service_and_person(hass, hass_client, aioclient_mock, service, path, mock, expected):
    await _setup(hass)
    mock(aioclient_mock)
    resp = await (await hass_client()).get(path)
    assert resp.status in (200, 401)                                                  # the answer still reaches the card unchanged
    ids = _issues(hass)
    assert len(ids) == 1 and next(iter(ids)).startswith(expected)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, next(iter(ids)))
    assert issue.translation_key == "upstream_auth" and issue.translation_placeholders["service"] == service
    assert issue.translation_placeholders["person"] not in ("-", "") and not issue.is_fixable


async def test_the_hint_goes_away_when_the_key_works_again(hass, hass_client, aioclient_mock):
    await _setup(hass)
    aioclient_mock.get(f"{KOMGA}/api/v1/series", status=401, json={}, headers=H)
    c = await hass_client()
    await c.get("/api/books/komga/v1/series")
    assert len(_issues(hass)) == 1
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{KOMGA}/api/v1/series", json={"content": []}, headers=H)
    await c.get("/api/books/komga/v1/series")
    assert _issues(hass) == set()


@pytest.mark.parametrize("status", [404, 403, 500, 503])
async def test_other_errors_are_not_a_key_problem(hass, hass_client, aioclient_mock, status):
    await _setup(hass)
    aioclient_mock.get(f"{KOMGA}/api/v1/series", status=status, json={}, headers=H)
    await (await hass_client()).get("/api/books/komga/v1/series")
    assert _issues(hass) == set()


async def test_the_shared_audiobookshelf_token_of_the_wish_check_raises_a_hint_too(hass, aioclient_mock):
    await _setup(hass)
    wishes = hass.data[DOMAIN]["wishes"]
    await wishes.async_add("book", next(iter(hass.data[DOMAIN]["users"])), title="Ein Buch")
    aioclient_mock.get(f"{ABS}/api/libraries", status=401, json={}, headers=H)
    await wishes._check()
    assert _issues(hass) == {"auth_audiobookshelf_shared"}
    assert ir.async_get(hass).async_get_issue(DOMAIN, "auth_audiobookshelf_shared").translation_placeholders["person"] == "-"


def test_the_hint_has_its_texts():
    from pathlib import Path
    base = Path(__file__).parent.parent / "custom_components" / "books"
    for f in ("strings.json", "translations/en.json", "translations/de.json"):
        issue = json.loads((base / f).read_text())["issues"]["upstream_auth"]
        assert "{service}" in issue["title"] and "{person}" in issue["description"] and "{service}" in issue["description"], f
