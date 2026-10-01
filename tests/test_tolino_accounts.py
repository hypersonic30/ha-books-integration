"""Several Thalia accounts (one per person): the form, and that everything - sending, the list of sent books, auto-send, progress
sync, the bridge alerts - runs per account and never touches somebody else's."""
import time

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.components.persistent_notification import _async_get_or_create_notifications
from homeassistant.config_entries import ConfigSubentryData
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.books.const import DOMAIN
from custom_components.books.tolino_registry import registry_for
from custom_components.books.users import tolino_accounts, tolino_config

from .conftest import ABS, ENTRY_DATA

BRIDGE = "http://bridge.test:8099"
JSON = {"Content-Type": "application/json"}
CLIENT_ID = "https://books.test/"


class As:
    def __init__(self, client, token):
        self._c, self._h = client, {"Authorization": f"Bearer {token}"}

    def get(self, url, **kw): return self._c.get(url, headers=self._h, **kw)
    def post(self, url, **kw): return self._c.post(url, headers=self._h, **kw)


def bridge_status(aioclient_mock, names=("default", "cara"), per_account=None):
    """The fake bridge's /status: lists the accounts and answers for the one named in the X-Tolino-Account header."""
    async def side(method, url, data):
        account = aioclient_mock.mock_calls[-1][3].get("X-Tolino-Account", "default")
        body = {"logged_in": True, "accounts": list(names), "account": account, **(per_account or {}).get(account, {})}
        return AiohttpClientMockResponse(method, url, json=body, headers=JSON)
    aioclient_mock.get(f"{BRIDGE}/status", side_effect=side)


def person(user, **kw):
    return ConfigSubentryData(subentry_type="user", title=user.name, unique_id=user.id,
                              data={"ha_user": user.id, "komga_api_key": "", "abs_token": "", "notify_service": "", "tolino": False,
                                    "tolino_account": "", "auto_send": False, "sync_progress": False, "sync_progress_write": False, **kw})


async def _family(hass):
    users = {n: await hass.auth.async_create_user(n.title(), group_ids=["system-users"]) for n in ("anna", "ben", "cara")}
    entry = MockConfigEntry(
        domain=DOMAIN, title="Books", data={**ENTRY_DATA, "tolino_url": BRIDGE, "tolino_token": "bridge-token"},
        subentries_data=[
            person(users["anna"], abs_token="abs-anna", tolino=True),                                              # the default account
            person(users["ben"], abs_token="abs-ben"),                                                              # no Tolino
            person(users["cara"], abs_token="abs-cara", tolino=True, tolino_account="cara", auto_send=True, sync_progress=True,
                   notify_service="notify.cara_phone"),
        ])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return users, entry


@pytest.fixture
async def family(hass, monkeypatch):
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False, "accounts": {}}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)
    return await _family(hass)


@pytest.fixture
def login(hass, hass_client_no_auth):
    async def _login(user):
        refresh = await hass.auth.async_create_refresh_token(user, CLIENT_ID)
        return As(await hass_client_no_auth(), hass.auth.async_create_access_token(refresh))
    return _login


def item(aioclient_mock, item_id="abc123"):
    aioclient_mock.get(f"{ABS}/api/items/{item_id}", json={"media": {"metadata": {"title": "Ein Buch"}, "ebookFile": {
        "ebookFormat": "epub", "metadata": {"filename": f"{item_id}.epub"}}}}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/items/{item_id}/ebook", content=b"PK-epub")
    aioclient_mock.get(f"{ABS}/api/items/{item_id}/cover", content=b"\xff\xd8\xff\xe0c")


def upload_returns(aioclient_mock, by_account):
    async def side(method, url, data):
        account = aioclient_mock.mock_calls[-1][3].get("X-Tolino-Account", "default")
        return AiohttpClientMockResponse(method, url, json={"deliverableId": by_account[account], "cover": True}, headers=JSON)
    aioclient_mock.post(f"{BRIDGE}/upload", side_effect=side)


def calls_to(aioclient_mock, suffix, method=None):
    return [c for c in aioclient_mock.mock_calls if str(c[1]).endswith(suffix) and (method is None or c[0] == method)]


# --- the form ------------------------------------------------------------------------------------------------------

async def _flow(hass, entry, **extra):
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "user"), context={"source": config_entries.SOURCE_USER})
    if not extra:
        return flow
    base = {"komga_api_key": "", "abs_token": "", "notify_service": "", "tolino": False, "tolino_account": "", "auto_send": False,
            "sync_progress": False, "sync_progress_write": False, "notify_test": False}
    return await hass.config_entries.subentries.async_configure(flow["flow_id"], {**base, **extra})


async def test_the_dropdown_lists_the_accounts_the_bridge_knows(hass, family, aioclient_mock):
    users, entry = family
    bridge_status(aioclient_mock, ("default", "cara", "ben"))
    sub_user = await hass.auth.async_create_user("Dora", group_ids=["system-users"])
    flow = await _flow(hass, entry)
    field = next(v for k, v in flow["data_schema"].schema.items() if str(k) == "tolino_account")
    assert [o["value"] for o in field.config["options"]] == ["default", "cara", "ben"]


@pytest.mark.parametrize("names,account,errors", [
    (("default", "cara", "dora"), "dora", {}),                                                 # a free account the bridge has
    (("default", "cara", "dora"), "niemand", {"tolino_account": "tolino_account_unknown"}),   # the bridge has no such account
    (("default", "cara", "dora"), "cara", {"tolino_account": "tolino_account_taken"}),        # somebody else uses it
    (("default", "cara", "dora"), "default", {"tolino_account": "tolino_account_taken"}),     # "default" written out: Anna has it
    (("default", "cara", "dora"), "", {"tolino_account": "tolino_account_taken"}),            # empty = default: Anna has it
    (("default", "cara", "dora"), "Dora", {"tolino_account": "tolino_account_invalid"}),      # the bridge only takes a-z0-9_-
    (("default", "cara", "dora"), "a" * 33, {"tolino_account": "tolino_account_invalid"}),
    (("default",), "dora", {"tolino_account": "tolino_account_unknown"}),                     # an older bridge: just the default account
])
async def test_account_rules_in_the_form(hass, family, aioclient_mock, names, account, errors):
    users, entry = family
    bridge_status(aioclient_mock, names)
    dora = await hass.auth.async_create_user("Dora", group_ids=["system-users"])
    result = await _flow(hass, entry, ha_user=dora.id, tolino=True, tolino_account=account)
    if errors:
        assert result["type"].value == "form" and result["errors"] == errors
    else:
        assert result["type"].value == "create_entry"
        stored = next(s for s in entry.subentries.values() if s.unique_id == dora.id)
        assert stored.data["tolino_account"] == "dora" and stored.data["tolino"] is True


async def test_an_account_name_without_a_tolino_is_refused(hass, family, aioclient_mock):
    users, entry = family
    bridge_status(aioclient_mock)
    dora = await hass.auth.async_create_user("Dora", group_ids=["system-users"])
    result = await _flow(hass, entry, ha_user=dora.id, tolino=False, tolino_account="cara")
    assert result["errors"] == {"tolino_account": "tolino_person_required"}


async def test_a_bridge_that_does_not_answer_does_not_block_saving(hass, family, aioclient_mock):
    users, entry = family
    aioclient_mock.get(f"{BRIDGE}/status", exc=aiohttp.ClientConnectionError("down"))
    dora = await hass.auth.async_create_user("Dora", group_ids=["system-users"])
    result = await _flow(hass, entry, ha_user=dora.id, tolino=True, tolino_account="dora")
    assert result["type"].value == "create_entry"                       # cannot be checked right now; the format was


async def test_editing_the_person_who_holds_an_account_is_fine(hass, family, aioclient_mock):
    users, entry = family
    bridge_status(aioclient_mock)
    aioclient_mock.get(f"{ABS}/api/me", json={"username": "cara"}, headers=JSON)
    cara = next(s for s in entry.subentries.values() if s.unique_id == users["cara"].id)
    async_mock_service(hass, "notify", "cara_phone")
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "user"), context={"source": config_entries.SOURCE_RECONFIGURE, "subentry_id": cara.subentry_id})
    done = await hass.config_entries.subentries.async_configure(flow["flow_id"], {
        "komga_api_key": "", "abs_token": "abs-cara", "notify_service": "notify.cara_phone", "tolino": True, "tolino_account": "cara",
        "auto_send": True, "sync_progress": True, "sync_progress_write": False, "notify_test": False})
    assert done["type"].value == "abort" and done["reason"] == "reconfigure_successful"


# --- running per account ------------------------------------------------------------------------------------------------

async def test_every_account_has_its_own_jobs_registry_and_settings(hass, family):
    assert tolino_accounts(hass) == ["default", "cara"]
    jobs = hass.data[DOMAIN]["jobs"]
    assert set(jobs) == {"default", "cara"}
    assert jobs["default"]["auto_send"] is hass.data[DOMAIN]["auto_send"]                    # the names the rest of the code knows
    assert not jobs["default"]["auto_send"].enabled and jobs["cara"]["auto_send"].enabled
    assert not jobs["default"]["progress_sync"].enabled and jobs["cara"]["progress_sync"].enabled
    assert tolino_config(hass, "default")["abs_token"] == "abs-anna" and tolino_config(hass, "cara")["abs_token"] == "abs-cara"
    assert registry_for(hass, "default") is hass.data[DOMAIN]["tolino_sent"] and registry_for(hass, "cara") is not registry_for(hass, "default")


async def test_the_default_account_keeps_its_storage_key_and_others_get_their_own(hass, family):
    assert registry_for(hass, "default")._store.key == "books_tolino_sent"                   # nothing to migrate
    assert registry_for(hass, "cara")._store.key == "books_tolino_sent_cara"
    assert hass.data[DOMAIN]["jobs"]["cara"]["auto_send"]._store.key == "books_tolino_autosend_cara"
    assert hass.data[DOMAIN]["jobs"]["default"]["auto_send"]._store.key == "books_tolino_autosend"


async def test_a_person_sends_to_their_own_account_with_their_own_abs_token(hass, family, login, aioclient_mock):
    users, _ = family
    item(aioclient_mock)
    upload_returns(aioclient_mock, {"default": "d-anna", "cara": "d-cara"})
    r = await (await login(users["cara"])).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert r.status == 200 and (await r.json())["deliverableId"] == "d-cara"
    up = calls_to(aioclient_mock, "/upload")[0]
    assert up[3]["X-Tolino-Account"] == "cara" and up[3]["Authorization"] == "Bearer bridge-token"
    assert calls_to(aioclient_mock, "/ebook")[0][3]["Authorization"] == "Bearer abs-cara"
    assert registry_for(hass, "cara").get("abc123")["deliverableId"] == "d-cara" and registry_for(hass, "default").get("abc123") is None


async def test_the_default_account_needs_no_header(hass, family, login, aioclient_mock):
    users, _ = family
    item(aioclient_mock)
    upload_returns(aioclient_mock, {"default": "d-anna"})
    r = await (await login(users["anna"])).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert r.status == 200 and "X-Tolino-Account" not in calls_to(aioclient_mock, "/upload")[0][3]     # an older bridge works too


async def test_the_same_book_can_be_sent_to_both_accounts(hass, family, login, aioclient_mock):
    users, _ = family
    item(aioclient_mock)
    upload_returns(aioclient_mock, {"default": "d-anna", "cara": "d-cara"})
    aioclient_mock.get(f"{BRIDGE}/library", json={"books": [{"deliverableId": "d-anna"}, {"deliverableId": "d-cara"}]}, headers=JSON)
    assert (await (await login(users["anna"])).post("/api/books/tolino", json={"abs_item_id": "abc123"})).status == 200
    assert (await (await login(users["cara"])).post("/api/books/tolino", json={"abs_item_id": "abc123"})).status == 200   # not "already sent"
    again = await (await login(users["cara"])).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert again.status == 409                                                                  # but a second time for the same person is


async def test_each_person_sees_only_their_own_sent_books(hass, family, login, aioclient_mock):
    users, _ = family
    bridge_status(aioclient_mock)
    await registry_for(hass, "cara").async_set("only-cara", "d1", "x.epub")
    await registry_for(hass, "default").async_set("only-anna", "d2", "y.epub")
    for who, mine, theirs in (("cara", "only-cara", "only-anna"), ("anna", "only-anna", "only-cara")):
        body = await (await (await login(users[who])).get("/api/books/tolino")).json()
        assert mine in body["sent"] and theirs not in body["sent"], who
    assert calls_to(aioclient_mock, "/status")[-1][3].get("X-Tolino-Account") is None          # Anna asked the default account


async def test_status_for_a_person_asks_their_account(hass, family, login, aioclient_mock):
    users, _ = family
    bridge_status(aioclient_mock, per_account={"cara": {"logged_in": False, "last_error": "captcha"}})
    body = await (await (await login(users["cara"])).get("/api/books/tolino")).json()
    assert body["logged_in"] is False and body["error"] == "captcha"
    assert calls_to(aioclient_mock, "/status")[-1][3]["X-Tolino-Account"] == "cara"


async def test_people_without_a_tolino_see_nothing_of_all_this(hass, family, login, aioclient_mock):
    users, _ = family
    body = await (await (await login(users["ben"])).get("/api/books/tolino")).json()
    assert body == {"enabled": False} and aioclient_mock.call_count == 0


async def test_auto_send_runs_per_account_with_that_persons_settings(hass, family, aioclient_mock):
    users, _ = family
    new = int(time.time() * 1000) + 60_000
    lib = "L1"
    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": [{"id": lib, "mediaType": "book"}]}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/libraries/{lib}/items", json={"results": [
        {"id": "n1", "addedAt": new, "media": {"ebookFormat": "epub", "metadata": {"title": "Neu"}}}]}, headers=JSON)
    item(aioclient_mock, "n1")
    upload_returns(aioclient_mock, {"default": "d-anna", "cara": "d-cara"})
    jobs = hass.data[DOMAIN]["jobs"]
    assert await jobs["default"]["auto_send"].async_tick() is None                           # Anna switched it off
    await registry_for(hass, "default").async_set("n1", "d-anna-old", "n1.epub")            # Anna has it already - Cara still does not
    result = await jobs["cara"]["auto_send"].async_tick()
    assert result["sent"] == ["n1"]
    up = calls_to(aioclient_mock, "/upload")
    assert len(up) == 1 and up[0][3]["X-Tolino-Account"] == "cara"
    assert calls_to(aioclient_mock, "/libraries")[0][3]["Authorization"] == "Bearer abs-cara"
    assert registry_for(hass, "cara").get("n1")["deliverableId"] == "d-cara"
    assert registry_for(hass, "default").get("n1")["deliverableId"] == "d-anna-old"           # Anna's entry is untouched
    assert jobs["cara"]["auto_send"].state["last_sent"]["title"] and not jobs["default"]["auto_send"].state.get("last_sent")


async def test_auto_send_skips_what_its_own_account_already_has(hass, family, aioclient_mock):
    new = int(time.time() * 1000) + 60_000
    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": [{"id": "L1", "mediaType": "book"}]}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/libraries/L1/items", json={"results": [
        {"id": "n1", "addedAt": new, "media": {"ebookFormat": "epub", "metadata": {"title": "Neu"}}}]}, headers=JSON)
    item(aioclient_mock, "n1")
    upload_returns(aioclient_mock, {"default": "d-anna", "cara": "d-cara"})
    await registry_for(hass, "cara").async_set("n1", "d-cara-old", "n1.epub")
    result = await hass.data[DOMAIN]["jobs"]["cara"]["auto_send"].async_tick()
    assert result["sent"] == [] and not calls_to(aioclient_mock, "/upload")


async def test_each_account_starts_auto_send_from_its_own_switch_on(hass, family):
    users, entry = family
    jobs = hass.data[DOMAIN]["jobs"]
    cara_since = jobs["cara"]["auto_send"].state["since"]
    anna = next(s for s in entry.subentries.values() if s.unique_id == users["anna"].id)
    hass.config_entries.async_update_subentry(entry, anna, data={**anna.data, "auto_send": True})
    await hass.async_block_till_done()
    assert jobs["default"]["auto_send"].state["active"] and jobs["default"]["auto_send"].state["since"] >= cara_since   # starts now, not at Cara's time
    assert jobs["cara"]["auto_send"].state["since"] == cara_since                              # Cara's start point is untouched


async def test_progress_sync_only_looks_at_its_own_accounts_books(hass, family, aioclient_mock):
    users, _ = family
    await registry_for(hass, "default").async_set("anna-book", "d-anna", "a.epub")
    await registry_for(hass, "cara").async_set("cara-book", "d-cara", "c.epub")
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    cara = next(s for s in entry.subentries.values() if s.unique_id == users["cara"].id)
    hass.config_entries.async_update_subentry(entry, cara, data={**cara.data, "sync_progress_write": True})   # with write-back every book is looked at
    await hass.async_block_till_done()
    aioclient_mock.get(f"{BRIDGE}/progress", json={"books": {}}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/me/progress/cara-book", status=404, json={}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/me/progress/anna-book", status=404, json={}, headers=JSON)
    summary = await hass.data[DOMAIN]["jobs"]["cara"]["progress_sync"].async_sync()
    progress_calls = calls_to(aioclient_mock, "/progress")
    assert len(progress_calls) == 1 and progress_calls[0][3]["X-Tolino-Account"] == "cara"
    assert summary["checked"] == 1
    looked_at = [str(c[1]) for c in aioclient_mock.mock_calls if "/me/progress/" in str(c[1])]
    assert looked_at == [f"{ABS}/api/me/progress/cara-book"]                                   # Anna's books are not Cara's business


async def test_the_sync_and_autosend_buttons_run_the_requesters_account(hass, family, login, aioclient_mock):
    users, _ = family
    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": []}, headers=JSON)
    assert (await (await login(users["anna"])).post("/api/books/tolino-autosend")).status == 409    # Anna's switch is off
    assert (await (await login(users["cara"])).post("/api/books/tolino-autosend")).status == 200
    assert (await (await login(users["ben"])).post("/api/books/tolino-autosend")).status == 403      # no Tolino at all
    assert (await (await login(users["anna"])).post("/api/books/tolino-sync")).status == 409
    await registry_for(hass, "cara").async_set("cara-book", "d1", "c.epub")
    aioclient_mock.get(f"{BRIDGE}/progress", json={"books": {}}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/me/progress/cara-book", status=404, json={}, headers=JSON)
    assert (await (await login(users["cara"])).post("/api/books/tolino-sync")).status == 200


async def test_the_sensors_summarise_all_accounts(hass, family):
    from homeassistant.helpers import entity_registry as er
    reg = er.async_get(hass)
    ent = reg.async_get_entity_id("sensor", DOMAIN, "books_tolino_auto_send")
    sensor = hass.states.get(ent)
    assert set(sensor.attributes["accounts"]) == {"default", "cara"}
    assert sensor.attributes["accounts"]["cara"]["enabled"] is True and sensor.attributes["accounts"]["default"]["enabled"] is False


# --- the bridge alerts, per account ----------------------------------------------------------------------------------------

BAD = {"logged_in": False, "last_error": "captcha: blocked"}


async def _watched(hass, aioclient_mock, per_account):
    bridge_status(aioclient_mock, per_account=per_account)                          # must exist before setup: the watcher polls at once
    async_mock_service(hass, "notify", "admin_phone")
    users = {n: await hass.auth.async_create_user(n.title(), group_ids=["system-users"]) for n in ("anna", "cara")}
    entry = MockConfigEntry(
        domain=DOMAIN, title="Books",
        data={**ENTRY_DATA, "tolino_url": BRIDGE, "tolino_token": "t", "notify_service": "notify.admin_phone"},
        subentries_data=[person(users["anna"], tolino=True), person(users["cara"], tolino=True, tolino_account="cara")])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN]["tolino_watcher"]


async def test_only_the_broken_account_raises_an_alert_and_names_itself(hass, aioclient_mock):
    pushes = async_mock_service(hass, "notify", "admin_phone")
    entry, watcher = await _watched(hass, aioclient_mock, {"cara": BAD})
    pushes = async_mock_service(hass, "notify", "admin_phone")
    await watcher.async_refresh(); await hass.async_block_till_done()                       # second bad poll
    assert len(pushes) == 1 and pushes[0].data["title"] == "tolino-Bridge Problem (cara)"
    assert "Konto „cara“" in pushes[0].data["message"] and "captcha: blocked" in pushes[0].data["message"]
    notes = _async_get_or_create_notifications(hass)
    assert "books_tolino_bridge_cara" in notes and "books_tolino_bridge" not in notes          # Anna's account is fine
    state = hass.states.get("binary_sensor.tolino_bridge_problem")
    assert state.state == "on"
    assert state.attributes["accounts"]["cara"]["problem"] is True and state.attributes["accounts"]["default"]["problem"] is False


async def test_a_recovered_account_clears_only_its_own_alert(hass, aioclient_mock):
    entry, watcher = await _watched(hass, aioclient_mock, {"cara": BAD})
    pushes = async_mock_service(hass, "notify", "admin_phone")
    await watcher.async_refresh(); await hass.async_block_till_done()
    aioclient_mock.clear_requests()
    bridge_status(aioclient_mock)
    await watcher.async_refresh(); await hass.async_block_till_done()
    assert [p.data["title"] for p in pushes] == ["tolino-Bridge Problem (cara)", "tolino-Bridge (cara)"]
    assert "books_tolino_bridge_cara" not in _async_get_or_create_notifications(hass)
    assert hass.states.get("binary_sensor.tolino_bridge_problem").state == "off"


async def test_removing_the_person_dismisses_their_alert(hass, aioclient_mock):
    entry, watcher = await _watched(hass, aioclient_mock, {"cara": BAD})
    await watcher.async_refresh(); await hass.async_block_till_done()
    assert "books_tolino_bridge_cara" in _async_get_or_create_notifications(hass)
    cara = next(s for s in entry.subentries.values() if s.title == "Cara")
    hass.config_entries.async_remove_subentry(entry, cara.subentry_id)
    await hass.async_block_till_done()
    await watcher.async_refresh(); await hass.async_block_till_done()
    assert "books_tolino_bridge_cara" not in _async_get_or_create_notifications(hass)
    assert hass.states.get("binary_sensor.tolino_bridge_problem").state == "off"


async def test_nobody_uses_the_bridge_means_nothing_to_watch(hass, aioclient_mock):
    entry, watcher = await _watched(hass, aioclient_mock, {"cara": BAD})
    for sub in list(entry.subentries.values()):
        hass.config_entries.async_update_subentry(entry, sub, data={**sub.data, "tolino": False, "tolino_account": ""})
    await hass.async_block_till_done()
    aioclient_mock.mock_calls.clear()
    await watcher.async_refresh(); await hass.async_block_till_done()
    assert watcher.data["problem"] is False and watcher.data["accounts"] == {} and aioclient_mock.call_count == 0
