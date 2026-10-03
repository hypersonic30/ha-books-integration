"""Per-person accounts: nobody reads with (and overwrites the progress of) somebody else's account; Tolino only for people with
a Tolino; "your book is here" goes to whoever asked."""
import time

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigSubentryData
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.books.const import DOMAIN
from custom_components.books.wishes import titles_match

from .conftest import ABS, CHAPTARR, ENTRY_DATA
from .test_views import ROOTFOLDERS, SEARCH_BOOK

KOMGA = "http://komga.test:25600"
MYLAR = "http://mylar.test:8090"
BRIDGE = "http://bridge.test:8099"
JSON = {"Content-Type": "application/json"}
CLIENT_ID = "https://books.test/"


class As:
    """A test client that is logged in as one particular Home Assistant user."""

    def __init__(self, client, token):
        self._c, self._h = client, {"Authorization": f"Bearer {token}"}

    def patch(self, url, **kw):
        return self._c.patch(url, headers={**self._h, **kw.pop("headers", {})}, **kw)

    def get(self, url, **kw):
        return self._c.get(url, headers={**self._h, **kw.pop("headers", {})}, **kw)

    def post(self, url, **kw):
        return self._c.post(url, headers={**self._h, **kw.pop("headers", {})}, **kw)


@pytest.fixture
async def cast(hass):
    """Anna (Tolino), Ben (no Tolino, no own Audiobookshelf token), Cara (not set up at all)."""
    mk = lambda name: hass.auth.async_create_user(name, group_ids=["system-users"])
    return {"anna": await mk("Anna"), "ben": await mk("Ben"), "cara": await mk("Cara")}


@pytest.fixture
async def household(hass, cast, monkeypatch):
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)
    person = lambda user, **kw: ConfigSubentryData(
        subentry_type="user", title=user.name, unique_id=user.id,
        data={"ha_user": user.id, "komga_api_key": "", "abs_token": "", "notify_service": "", "tolino": False, **kw})
    entry = MockConfigEntry(
        domain=DOMAIN, title="Books",
        data={**ENTRY_DATA, "komga_url": KOMGA, "komga_api_key": "komga-shared", "mylar_url": MYLAR, "mylar_api_key": "mylar-key",
              "tolino_url": BRIDGE, "tolino_token": "bridge-token"},
        subentries_data=[
            person(cast["anna"], komga_api_key="komga-anna", abs_token="abs-anna", notify_service="notify.anna_phone", tolino=True),
            person(cast["ben"], komga_api_key="komga-ben", notify_service="notify.ben_phone"),
        ])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
def login(hass, hass_client_no_auth):
    async def _login(user):
        refresh = await hass.auth.async_create_refresh_token(user, CLIENT_ID)
        return As(await hass_client_no_auth(), hass.auth.async_create_access_token(refresh))
    return _login


def _bridge_knows(aioclient_mock, *names):
    """The bridge's /status as the person form sees it: which Thalia accounts exist."""
    aioclient_mock.get(f"{BRIDGE}/status", json={"logged_in": True, "accounts": list(names) or ["default"]}, headers=JSON)
    aioclient_mock.get(f"{BRIDGE}/accounts", json={"accounts": list(names) or ["default"]}, headers=JSON)


def _last_headers(aioclient_mock):
    return aioclient_mock.mock_calls[-1][3]


# --- everybody reads with their own account ---------------------------------------------------------

async def test_each_person_reads_komga_with_their_own_key(hass, household, cast, login, aioclient_mock):
    aioclient_mock.get(f"{KOMGA}/api/v1/series", json={"content": []}, headers=JSON)
    seen = {}
    for who in ("anna", "ben", "cara"):
        r = await (await login(cast[who])).get("/api/books/komga/v1/series")
        seen[who] = _last_headers(aioclient_mock)["X-API-Key"] if r.status == 200 else None
        if who == "cara":
            assert r.status == 403 and (await r.json())["code"] == "no_person"
    assert seen == {"anna": "komga-anna", "ben": "komga-ben", "cara": None}                # Cara is no person: no access at all


async def test_progress_is_written_to_the_asking_persons_account(hass, household, cast, login, aioclient_mock):
    aioclient_mock.patch(f"{KOMGA}/api/v1/books/B1/read-progress", status=204)
    c = await login(cast["ben"])
    r = await c.patch("/api/books/komga/v1/books/B1/read-progress", json={"page": 5, "completed": False})
    assert r.status == 204 and _last_headers(aioclient_mock)["X-API-Key"] == "komga-ben"
    assert [h["X-API-Key"] for *_, h in aioclient_mock.mock_calls] == ["komga-ben"]          # Anna's key was never involved


async def test_audiobookshelf_token_is_per_person_and_falls_back_to_the_shared_one(hass, household, cast, login, aioclient_mock):
    aioclient_mock.get(f"{ABS}/api/me", json={}, headers=JSON)
    tokens = {}
    for who in ("anna", "ben", "cara"):
        await (await login(cast[who])).get("/api/books/abs/me")
        tokens[who] = _last_headers(aioclient_mock)["Authorization"]
    assert tokens == {"anna": "Bearer abs-anna", "ben": "Bearer abs-token", "cara": "Bearer abs-token"}   # Ben has a Komga key but no ABS token


async def test_adding_and_removing_people_applies_without_a_reload(hass, household, cast, login, aioclient_mock):
    aioclient_mock.get(f"{KOMGA}/api/v1/series", json={}, headers=JSON)
    sub = next(s for s in household.subentries.values() if s.unique_id == cast["ben"].id)
    hass.config_entries.async_remove_subentry(household, sub.subentry_id)
    await hass.async_block_till_done()
    calls_before = aioclient_mock.call_count
    r = await (await login(cast["ben"])).get("/api/books/komga/v1/series")
    assert r.status == 403 and aioclient_mock.call_count == calls_before                     # removed: no access, nothing reaches Komga
    hass.config_entries.async_update_subentry(household, next(iter(household.subentries.values())), data={
        **next(iter(household.subentries.values())).data, "komga_api_key": "komga-anna-2"})
    await hass.async_block_till_done()
    await (await login(cast["anna"])).get("/api/books/komga/v1/series")
    assert _last_headers(aioclient_mock)["X-API-Key"] == "komga-anna-2"


async def test_without_any_person_everything_is_as_before(hass, setup_entry, hass_client, aioclient_mock):
    aioclient_mock.get(f"{ABS}/api/me", json={}, headers=JSON)
    await (await hass_client()).get("/api/books/abs/me")
    assert _last_headers(aioclient_mock)["Authorization"] == "Bearer abs-token"


# --- Tolino only for people who have one ---------------------------------------------------------------

async def test_tolino_button_only_for_people_with_a_tolino(hass, household, cast, login, aioclient_mock):
    aioclient_mock.get(f"{BRIDGE}/status", json={"logged_in": True}, headers=JSON)
    assert (await (await (await login(cast["anna"])).get("/api/books/tolino")).json())["enabled"] is True
    body = await (await (await login(cast["ben"])).get("/api/books/tolino")).json()
    assert body == {"enabled": False}                                                          # a person without a tolino
    r = await (await login(cast["cara"])).get("/api/books/tolino")
    assert r.status == 403 and (await r.json())["code"] == "no_person"                        # no person at all


@pytest.mark.parametrize("path", ["/api/books/tolino", "/api/books/tolino-sync", "/api/books/tolino-autosend"])
async def test_people_without_a_tolino_cannot_use_the_tolino_endpoints(hass, household, cast, login, aioclient_mock, path):
    c = await login(cast["ben"])
    resp = await c.post(path, json={"abs_item_id": "abc123"})
    assert resp.status == 403 and (await resp.json())["code"] == "no_tolino" and aioclient_mock.call_count == 0


async def test_background_tolino_jobs_use_the_tolino_persons_account(hass, household):
    from custom_components.books.users import tolino_config
    assert tolino_config(hass)["abs_token"] == "abs-anna"


async def test_people_exist_but_nobody_has_a_tolino_means_no_bridge_jobs(hass, household):
    from custom_components.books.users import tolino_config
    for sub in household.subentries.values():
        hass.config_entries.async_update_subentry(household, sub, data={**sub.data, "tolino": False})
    await hass.async_block_till_done()
    assert tolino_config(hass)["tolino_url"] == "" and tolino_config(hass)["tolino_token"] == ""


# --- "your book is here" goes to whoever asked ---------------------------------------------------------

async def test_a_requested_book_notifies_only_the_person_who_asked(hass, household, cast, login, aioclient_mock):
    anna_msgs = async_mock_service(hass, "notify", "anna_phone")
    ben_msgs = async_mock_service(hass, "notify", "ben_phone")
    aioclient_mock.get(f"{CHAPTARR}/api/v1/rootfolder", json=ROOTFOLDERS)
    aioclient_mock.post(f"{CHAPTARR}/api/v1/book", json={"id": 200, "title": "Die Chroniken von Alsea"})
    aioclient_mock.post(f"{CHAPTARR}/api/v1/command", json={"id": 9})
    resp = await (await login(cast["anna"])).post("/api/books/add", json={"book": SEARCH_BOOK, "media_types": ["ebook"]})
    assert resp.status == 200
    wishes = hass.data[DOMAIN]["wishes"]
    assert [(w["kind"], w["user"], w["title"]) for w in wishes.items] == [("book", cast["anna"].id, "Die Chroniken von Alsea")]

    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": [{"id": "L1", "mediaType": "book"}]}, headers=JSON)
    new = lambda title, at: {"id": "i1", "addedAt": at, "media": {"metadata": {"title": title}}}
    now_ms = int(time.time() * 1000)
    aioclient_mock.get(f"{ABS}/api/libraries/L1/items", json={"results": [new("Ein ganz anderes Buch", now_ms + 1000)]}, headers=JSON)
    await wishes._check()
    assert not anna_msgs and len(wishes.items) == 1                      # something else arrived: keep waiting

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": [{"id": "L1", "mediaType": "book"}]}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/libraries/L1/items", json={"results": [new("Die Chroniken von Alsea", now_ms + 1000)]}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/items/i1", json={"id": "i1", "media": {"tags": []}}, headers=JSON)
    aioclient_mock.patch(f"{ABS}/api/items/i1/media", json={"updated": True}, headers=JSON)
    await wishes._check()
    await hass.async_block_till_done()
    assert len(anna_msgs) == 1 and "Die Chroniken von Alsea" in anna_msgs[0].data["message"]
    assert not ben_msgs                                                   # nobody else hears about it
    assert wishes.items == []
    await wishes._check()
    assert len(anna_msgs) == 1                                            # once


async def test_a_book_that_was_already_there_does_not_count(hass, household, cast, login, aioclient_mock):
    msgs = async_mock_service(hass, "notify", "anna_phone")
    wishes = hass.data[DOMAIN]["wishes"]
    await wishes.async_add("book", cast["anna"].id, title="Die Chroniken von Alsea", author="Erika Muster")
    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": [{"id": "L1", "mediaType": "book"}]}, headers=JSON)
    old = {"id": "i1", "addedAt": int((time.time() - 3600) * 1000), "media": {"metadata": {"title": "Die Chroniken von Alsea"}}}
    aioclient_mock.get(f"{ABS}/api/libraries/L1/items", json={"results": [old]}, headers=JSON)
    await wishes._check()
    assert not msgs and len(wishes.items) == 1


async def test_a_manga_volume_notifies_the_person_who_queued_it(hass, household, cast, login, aioclient_mock):
    ben_msgs = async_mock_service(hass, "notify", "ben_phone")
    anna_msgs = async_mock_service(hass, "notify", "anna_phone")
    aioclient_mock.get(f"{MYLAR}/api", json={"success": True, "data": "queued"}, headers=JSON)
    resp = await (await login(cast["ben"])).post("/api/books/mylar/queueIssue?id=448514")
    assert resp.status == 202
    await hass.async_block_till_done()
    wishes = hass.data[DOMAIN]["wishes"]
    assert [(w["kind"], w["user"], w["issue"]) for w in wishes.items] == [("manga", cast["ben"].id, "448514")]

    aioclient_mock.clear_requests()
    history = [{"IssueID": "448514", "ComicName": "Attack on Titan", "Issue_Number": "1", "Status": "Snatched"}]
    aioclient_mock.get(f"{MYLAR}/api", json={"success": True, "data": history}, headers=JSON)
    await wishes._check()
    assert not ben_msgs                                                   # downloading, not filed yet
    aioclient_mock.clear_requests()
    history.insert(0, {"IssueID": "448514", "ComicName": "Attack on Titan", "Issue_Number": "1", "Status": "Post-Processed"})
    aioclient_mock.get(f"{MYLAR}/api", json={"success": True, "data": history}, headers=JSON)
    await wishes._check()
    await hass.async_block_till_done()
    assert len(ben_msgs) == 1 and "Attack on Titan · Band 1" in ben_msgs[0].data["message"] and "Komga" in ben_msgs[0].data["message"]
    assert not anna_msgs and wishes.items == []


async def test_wishes_expire_and_survive_unknown_people(hass, household, cast):
    wishes = hass.data[DOMAIN]["wishes"]
    await wishes.async_add("book", cast["anna"].id, title="Uralt")
    wishes.items[0]["ts"] = time.time() - 40 * 24 * 3600
    await wishes.async_add("book", None, title="Niemand")                  # no user -> not tracked at all
    await wishes._check()
    assert wishes.items == []


def test_title_matching_is_forgiving_but_not_silly():
    assert titles_match("Harry Potter und der Stein der Weisen", "Harry Potter und der Stein der Weisen (Harry Potter 1)")
    assert titles_match("Die Chroniken von Alsea", "die chroniken von alsea")
    assert not titles_match("Die Chroniken von Alsea", "Ein ganz anderes Buch")
    assert not titles_match("Er", "Herr der Ringe")                         # too short to mean anything


# --- the form: add / edit a person ---------------------------------------------------------------------------

@pytest.fixture
async def plain_entry(hass, cast, monkeypatch):
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA, "komga_url": KOMGA, "komga_api_key": "k"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _add(hass, entry, **extra):
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "user"), context={"source": config_entries.SOURCE_USER})
    return flow, await hass.config_entries.subentries.async_configure(flow["flow_id"], extra) if extra else flow


async def test_add_a_person_checks_the_accounts_and_shows_who_they_are(hass, plain_entry, cast, aioclient_mock):
    aioclient_mock.get(f"{KOMGA}/api/v2/users/me", json={"email": "anna@home", "roles": ["USER"]}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/me", json={"username": "anna"}, headers=JSON)
    flow, result = await _add(hass, plain_entry, ha_user=cast["anna"].id, komga_api_key=" key-a ", abs_token="tok-a",
                              notify_service="", tolino=False, notify_test=False)
    assert result["type"].value == "create_entry" and result["title"] == "Anna"
    sub = next(iter(plain_entry.subentries.values()))
    assert sub.unique_id == cast["anna"].id and sub.data["komga_api_key"] == "key-a" and sub.data["abs_token"] == "tok-a"
    assert sub.data["komga_name"] == "anna@home" and sub.data["abs_name"] == "anna"
    assert "notify_test" not in sub.data and "_title" not in sub.data
    assert hass.data[DOMAIN]["users"][cast["anna"].id]["komga_api_key"] == "key-a"           # live at once, no reload
    assert plain_entry.data["komga_api_key"] == "k"                                           # the shared account is untouched


async def test_the_dropdown_only_offers_people_who_are_not_added_yet(hass, household, cast, aioclient_mock):
    _bridge_knows(aioclient_mock, "default")
    flow = await hass.config_entries.subentries.async_init((household.entry_id, "user"), context={"source": config_entries.SOURCE_USER})
    options = next(iter(flow["data_schema"].schema.values())).config["options"]
    from .test_translations import PERSON_FIELDS
    assert {str(k) for k in flow["data_schema"].schema} == set(PERSON_FIELDS)            # the form and its texts cannot drift apart
    labels = {o["label"] for o in options}
    assert "Cara" in labels and "Anna" not in labels and "Ben" not in labels


@pytest.mark.parametrize("extra,errors", [
    ({"komga_api_key": "bad"}, {"komga_api_key": "komga_invalid_auth"}),
    ({"abs_token": "bad"}, {"abs_token": "abs_invalid_auth"}),
    ({"tolino": True}, {"tolino": "tolino_bridge_required"}),
    ({"notify_service": "notify.gibt_es_nicht"}, {"notify_service": "notify_unknown"}),
])
async def test_add_a_person_errors(hass, plain_entry, cast, aioclient_mock, extra, errors):
    aioclient_mock.get(f"{KOMGA}/api/v2/users/me", status=401)
    aioclient_mock.get(f"{ABS}/api/me", status=401)
    base = {"ha_user": cast["cara"].id, "komga_api_key": "", "abs_token": "", "notify_service": "", "tolino": False, "notify_test": False}
    _, result = await _add(hass, plain_entry, **{**base, **extra})
    assert result["type"].value == "form" and result["errors"] == errors
    assert not plain_entry.subentries


async def test_komga_key_without_a_komga_in_the_main_settings(hass, cast, monkeypatch):
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    _, result = await _add(hass, entry, ha_user=cast["anna"].id, komga_api_key="x", abs_token="", notify_service="", tolino=False, notify_test=False)
    assert result["errors"] == {"komga_api_key": "komga_not_configured"}


async def test_edit_a_person_keeps_the_user_and_updates_the_rest(hass, household, cast, aioclient_mock):
    _bridge_knows(aioclient_mock, "default")
    async_mock_service(hass, "notify", "ben_phone")
    aioclient_mock.get(f"{KOMGA}/api/v2/users/me", json={"email": "ben@home", "roles": []}, headers=JSON)
    sub = next(s for s in household.subentries.values() if s.unique_id == cast["ben"].id)
    flow = await hass.config_entries.subentries.async_init(
        (household.entry_id, "user"), context={"source": config_entries.SOURCE_RECONFIGURE, "subentry_id": sub.subentry_id})
    assert flow["type"].value == "form" and "ha_user" not in {str(k) for k in flow["data_schema"].schema}
    done = await hass.config_entries.subentries.async_configure(flow["flow_id"], {
        "komga_api_key": "komga-ben-new", "abs_token": "", "notify_service": "notify.ben_phone", "tolino": False, "notify_test": False})
    assert done["type"].value == "abort" and done["reason"] == "reconfigure_successful"
    now = household.subentries[sub.subentry_id]
    assert now.data["ha_user"] == cast["ben"].id and now.data["komga_api_key"] == "komga-ben-new" and now.data["komga_name"] == "ben@home"


async def test_the_test_message_is_sent_when_asked(hass, plain_entry, cast, aioclient_mock):
    msgs = async_mock_service(hass, "notify", "anna_phone")
    _, result = await _add(hass, plain_entry, ha_user=cast["anna"].id, komga_api_key="", abs_token="",
                           notify_service="notify.anna_phone", tolino=False, notify_test=True)
    await hass.async_block_till_done()
    assert result["type"].value == "create_entry" and len(msgs) == 1 and "Testnachricht" in msgs[0].data["message"]


# --- 0.10.1: auto-send and progress sync are per person; only one Tolino person for now --------------------

def _anna(household, cast):
    return next(s for s in household.subentries.values() if s.unique_id == cast["anna"].id)


def _set(hass, household, sub, **changes):
    hass.config_entries.async_update_subentry(household, sub, data={**household.subentries[sub.subentry_id].data, **changes})


async def test_the_switches_come_from_the_tolino_person_not_from_the_main_settings(hass, household, cast):
    auto, sync = hass.data[DOMAIN]["jobs"]["default"]["auto_send"], hass.data[DOMAIN]["jobs"]["default"]["progress_sync"]
    assert not auto.enabled and not sync.enabled and not sync.write_enabled                 # all off for Anna
    hass.config_entries.async_update_entry(household, data={**household.data, "auto_send": True, "sync_progress": True, "sync_progress_write": True})
    await hass.async_block_till_done()
    assert not auto.enabled and not sync.enabled and not sync.write_enabled                 # the main switches do not count once people exist
    _set(hass, household, _anna(household, cast), auto_send=True, sync_progress=True, sync_progress_write=False)
    await hass.async_block_till_done()
    assert auto.enabled and sync.enabled and not sync.write_enabled                         # ... Anna's do
    _set(hass, household, _anna(household, cast), sync_progress_write=True)
    await hass.async_block_till_done()
    assert sync.write_enabled


async def test_a_person_can_have_progress_sync_without_auto_send(hass, household, cast):
    _set(hass, household, _anna(household, cast), auto_send=False, sync_progress=True)
    await hass.async_block_till_done()
    assert hass.data[DOMAIN]["jobs"]["default"]["progress_sync"].enabled and not hass.data[DOMAIN]["jobs"]["default"]["auto_send"].enabled


async def test_switching_auto_send_on_starts_from_now_and_never_floods_old_books(hass, household, cast):
    auto = hass.data[DOMAIN]["jobs"]["default"]["auto_send"]
    assert not auto.state.get("active")
    before = int(time.time() * 1000)
    _set(hass, household, _anna(household, cast), auto_send=True)
    await hass.async_block_till_done()
    assert auto.state["active"] and auto.state["since"] >= before - 5 * 60 * 1000           # "only books added from now on"
    assert auto.state["owner"] == cast["anna"].id


async def test_a_new_tolino_person_starts_from_now_too(hass, household, cast):
    """The bridge stays on the whole time; only the person changes (Anna hands the Tolino over to Ben)."""
    auto = hass.data[DOMAIN]["jobs"]["default"]["auto_send"]
    ben = next(s for s in household.subentries.values() if s.unique_id == cast["ben"].id)
    _set(hass, household, _anna(household, cast), auto_send=True)
    _set(hass, household, ben, tolino=True, auto_send=True)                      # Anna is still the first Tolino person
    await hass.async_block_till_done()
    assert auto.state["owner"] == cast["anna"].id
    auto.state["since"] = 1                                                      # pretend it has been running for ages
    await auto._store.async_save(auto.state)
    _set(hass, household, _anna(household, cast), tolino=False, auto_send=False)  # ... now Ben is
    await hass.async_block_till_done()
    assert auto.state["active"] and auto.state["owner"] == cast["ben"].id and auto.state["since"] > 1000


async def test_turning_the_switch_off_and_on_again_starts_over(hass, household, cast):
    auto = hass.data[DOMAIN]["jobs"]["default"]["auto_send"]
    _set(hass, household, _anna(household, cast), auto_send=True)
    await hass.async_block_till_done()
    _set(hass, household, _anna(household, cast), auto_send=False)
    await hass.async_block_till_done()
    assert auto.state["active"] is False
    auto.state["since"] = 1
    await auto._store.async_save(auto.state)
    _set(hass, household, _anna(household, cast), auto_send=True)
    await hass.async_block_till_done()
    assert auto.state["since"] > 1000


@pytest.mark.parametrize("who,extra,errors", [
    ("cara", {"tolino": True}, {"tolino_account": "tolino_account_taken"}),                     # Anna already has the default account
    ("ben", {"auto_send": True}, {"auto_send": "tolino_person_required"}),
    ("ben", {"sync_progress": True}, {"sync_progress": "tolino_person_required"}),
    ("ben", {"import_tolino": True}, {"import_tolino": "tolino_person_required"}),
    ("ben", {"import_tolino_audiobooks": True}, {"import_tolino_audiobooks": "tolino_person_required"}),
    ("ben", {"import_tolino_radioplays": True}, {"import_tolino_radioplays": "tolino_person_required"}),
    ("anna", {"sync_progress_write": True, "sync_progress": False}, {"sync_progress_write": "sync_progress_required"}),
])
async def test_tolino_rules_in_the_person_form(hass, household, cast, aioclient_mock, who, extra, errors):
    aioclient_mock.get(f"{KOMGA}/api/v2/users/me", json={"email": "x", "roles": []}, headers=JSON)
    async_mock_service(hass, "notify", "anna_phone")
    async_mock_service(hass, "notify", "ben_phone")
    _bridge_knows(aioclient_mock, "default")
    base = {"komga_api_key": "", "abs_token": "", "notify_service": "", "tolino": False, "auto_send": False,
            "sync_progress": False, "sync_progress_write": False, "notify_test": False}
    if who == "cara":
        flow = await hass.config_entries.subentries.async_init((household.entry_id, "user"), context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.subentries.async_configure(flow["flow_id"], {**base, "ha_user": cast["cara"].id, **extra})
    else:
        sub = next(s for s in household.subentries.values() if s.unique_id == cast[who].id)
        flow = await hass.config_entries.subentries.async_init(
            (household.entry_id, "user"), context={"source": config_entries.SOURCE_RECONFIGURE, "subentry_id": sub.subentry_id})
        keep = {"tolino": sub.data["tolino"], "notify_service": sub.data["notify_service"]}
        result = await hass.config_entries.subentries.async_configure(flow["flow_id"], {**base, **keep, **extra})
    assert result["type"].value == "form" and result["errors"] == errors


async def test_the_tolino_person_can_be_edited_without_tripping_the_account_taken_rule(hass, household, cast, aioclient_mock):
    _bridge_knows(aioclient_mock, "default")
    aioclient_mock.get(f"{KOMGA}/api/v2/users/me", json={"email": "x", "roles": []}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/me", json={"username": "anna"}, headers=JSON)
    async_mock_service(hass, "notify", "anna_phone")
    sub = _anna(household, cast)
    flow = await hass.config_entries.subentries.async_init(
        (household.entry_id, "user"), context={"source": config_entries.SOURCE_RECONFIGURE, "subentry_id": sub.subentry_id})
    done = await hass.config_entries.subentries.async_configure(flow["flow_id"], {
        "komga_api_key": "komga-anna", "abs_token": "abs-anna", "notify_service": "notify.anna_phone", "tolino": True,
        "auto_send": True, "sync_progress": True, "sync_progress_write": True, "notify_test": False})
    assert done["type"].value == "abort" and done["reason"] == "reconfigure_successful"
    assert household.subentries[sub.subentry_id].data["auto_send"] is True


@pytest.mark.parametrize("kind,ok", [("user", True), ("guest", True), ("admin", False), ("root", False), ("Admin", False)])
async def test_a_person_with_an_audiobookshelf_admin_token_is_refused(hass, plain_entry, cast, aioclient_mock, kind, ok):
    aioclient_mock.get(f"{ABS}/api/me", json={"username": "anna", "type": kind}, headers=JSON)
    flow, result = await _add(hass, plain_entry, ha_user=cast["anna"].id, komga_api_key="", abs_token="tok", notify_service="", tolino=False,
                              auto_send=False, sync_progress=False, sync_progress_write=False, tolino_account="", notify_test=False)
    if ok:
        assert result["type"].value == "create_entry"
    else:
        assert result["type"].value == "form" and result["errors"] == {"abs_token": "abs_admin"} and not plain_entry.subentries


async def test_a_person_with_a_komga_admin_key_is_refused(hass, plain_entry, cast, aioclient_mock):
    aioclient_mock.get(f"{KOMGA}/api/v2/users/me", json={"email": "root@x", "roles": ["ADMIN", "USER"]}, headers=JSON)
    flow, result = await _add(hass, plain_entry, ha_user=cast["anna"].id, komga_api_key="k", abs_token="", notify_service="", tolino=False,
                              auto_send=False, sync_progress=False, sync_progress_write=False, tolino_account="", notify_test=False)
    assert result["errors"] == {"komga_api_key": "komga_admin"}


# --- tags: one library, told apart by "für <name>" -------------------------------------------------------------------------------------

async def _arrive(hass, cast, aioclient_mock, item_tags, patch_status=200):
    """Anna asked for a book; it shows up in Audiobookshelf as item i1 with `item_tags`."""
    await hass.data[DOMAIN]["wishes"].async_add("book", cast["anna"].id, title="Die Chroniken von Alsea", author="Erika Muster")
    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": [{"id": "L1", "mediaType": "book"}]}, headers=JSON)
    arrived = {"id": "i1", "addedAt": int(time.time() * 1000) + 1000, "media": {"metadata": {"title": "Die Chroniken von Alsea"}}}
    aioclient_mock.get(f"{ABS}/api/libraries/L1/items", json={"results": [arrived]}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/items/i1", json={"id": "i1", "media": {"tags": item_tags}}, headers=JSON)
    aioclient_mock.patch(f"{ABS}/api/items/i1/media", status=patch_status, json={}, headers=JSON)
    await hass.data[DOMAIN]["wishes"]._check()
    await hass.async_block_till_done()
    return [c[2] for c in aioclient_mock.mock_calls if c[0] == "PATCH"]


async def test_the_arrived_book_gets_the_tag_of_the_person_who_asked(hass, household, cast, aioclient_mock):
    bodies = await _arrive(hass, cast, aioclient_mock, ["Fantasy"])
    assert bodies == [{"tags": ["Fantasy", "für Anna"]}]                 # other tags stay


async def test_an_already_tagged_book_is_not_written_again(hass, household, cast, aioclient_mock):
    assert await _arrive(hass, cast, aioclient_mock, ["für Anna"]) == []


async def test_a_refused_tag_write_does_not_stop_the_notification(hass, household, cast, aioclient_mock):
    msgs = async_mock_service(hass, "notify", "anna_phone")
    await _arrive(hass, cast, aioclient_mock, [], patch_status=403)      # the ABS user lacks the "update" permission
    assert len(msgs) == 1 and hass.data[DOMAIN]["wishes"].items == []


async def test_people_endpoint_lists_everybody_and_marks_the_asker(hass, household, cast, login):
    body = await (await (await login(cast["ben"])).get("/api/books/people")).json()
    assert body == {"people": [{"name": "Anna", "tag": "für Anna", "me": False}, {"name": "Ben", "tag": "für Ben", "me": True}], "restricted": False, "can_tag": True, "shared_tag": "für alle"}


async def test_people_endpoint_needs_a_person(hass, household, login):
    stranger = await hass.auth.async_create_user("Stranger", group_ids=["system-users"])
    assert (await (await login(stranger)).get("/api/books/people")).status == 403


# --- child protection: a person limited to the books released for them ------------------------------------------------------------------

import aiohttp                                                                        # noqa: E402
from homeassistant.components.persistent_notification import _async_get_or_create_notifications  # noqa: E402

from custom_components.books import restriction                                      # noqa: E402

LIMITED_ME = {"type": "user", "permissions": {"accessAllTags": False, "selectedTagsNotAccessible": False}, "itemTagsSelected": ["für Ben", "für alle"]}
OPEN_ME = {"type": "user", "permissions": {"accessAllTags": True, "selectedTagsNotAccessible": False}, "itemTagsSelected": []}


@pytest.mark.parametrize("me,expected", [
    (LIMITED_ME, True), (OPEN_ME, False),
    ({**LIMITED_ME, "permissions": {"accessAllTags": False, "selectedTagsNotAccessible": True}}, False),      # a deny list: everything else stays visible
    ({**LIMITED_ME, "itemTagsSelected": []}, False),                                                          # nothing selected
    ({**LIMITED_ME, "type": "admin"}, False), ({}, False), (None, False),
])
def test_what_counts_as_limited(me, expected):
    assert restriction.limited(me) is expected


def _ben(household, cast):
    return next(s for s in household.subentries.values() if s.unique_id == cast["ben"].id)


async def _lock_ben(hass, household, cast):
    _set(hass, household, _ben(household, cast), restrict_books=True, abs_token="abs-ben")
    await hass.async_block_till_done()


async def test_a_restricted_person_gets_the_library_when_their_abs_user_is_limited(hass, household, cast, login, aioclient_mock):
    await _lock_ben(hass, household, cast)
    aioclient_mock.get(f"{ABS}/api/me", json=LIMITED_ME, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": []}, headers=JSON)
    resp = await (await login(cast["ben"])).get("/api/books/abs/libraries")
    assert resp.status == 200
    assert _last_headers(aioclient_mock)["Authorization"] == "Bearer abs-ben"           # their own user, never the shared one


async def test_a_restricted_person_is_locked_out_when_their_abs_user_is_not_limited(hass, household, cast, login, aioclient_mock):
    await _lock_ben(hass, household, cast)
    aioclient_mock.get(f"{ABS}/api/me", json=OPEN_ME, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": [{"id": "secret"}]}, headers=JSON)
    resp = await (await login(cast["ben"])).get("/api/books/abs/libraries")
    assert resp.status == 403 and (await resp.json())["code"] == "restricted_unverified"
    assert not [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/api/libraries")]       # nothing was forwarded
    assert f"books_restrict_{cast['ben'].id}" in _async_get_or_create_notifications(hass)


async def test_a_restricted_person_without_their_own_token_never_falls_back_to_the_shared_one(hass, household, cast, login, aioclient_mock):
    _set(hass, household, _ben(household, cast), restrict_books=True, abs_token="")
    await hass.async_block_till_done()
    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": []}, headers=JSON)
    resp = await (await login(cast["ben"])).get("/api/books/abs/libraries")
    assert resp.status == 403 and not aioclient_mock.mock_calls


async def test_an_unreachable_abs_refuses_a_restricted_person_unless_it_was_verified_a_moment_ago(hass, household, cast, login, aioclient_mock, monkeypatch):
    await _lock_ben(hass, household, cast)
    aioclient_mock.get(f"{ABS}/api/me", exc=aiohttp.ClientConnectionError())
    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": []}, headers=JSON)
    client = await login(cast["ben"])
    assert (await client.get("/api/books/abs/libraries")).status == 403                       # never verified: closed
    hass.data[DOMAIN]["restriction"][cast["ben"].id] = {"at": time.time() - 600, "ok": True}  # verified 10 minutes ago: still held
    assert (await client.get("/api/books/abs/libraries")).status == 200
    hass.data[DOMAIN]["restriction"][cast["ben"].id] = {"at": time.time() - 7200, "ok": True} # two hours ago: too old
    assert (await client.get("/api/books/abs/libraries")).status == 403


async def test_search_requests_and_downloads_are_closed_for_a_restricted_person(hass, household, cast, login, aioclient_mock):
    await _lock_ben(hass, household, cast)
    aioclient_mock.get(f"{CHAPTARR}/api/v1/search", json=[{"title": "Nichts für Kinder"}], headers=JSON)
    aioclient_mock.get(f"{CHAPTARR}/api/v1/queue", json={"records": []}, headers=JSON)
    aioclient_mock.post(f"{CHAPTARR}/api/v1/book", json={"id": 1})
    aioclient_mock.get(f"{MYLAR}/api", json={"success": True, "data": []}, headers=JSON)
    client = await login(cast["ben"])
    for path in ("/api/books/chaptarr/search?term=x", "/api/books/chaptarr/queue", "/api/books/mylar/findComic?name=x"):
        resp = await client.get(path)
        assert resp.status == 403 and (await resp.json())["code"] == "restricted", path
    add = await client.post("/api/books/add", json={"book": SEARCH_BOOK, "media_types": ["ebook"]})
    assert add.status == 403
    assert not aioclient_mock.mock_calls                                                      # not one request left Home Assistant
    assert (await (await client.get("/api/books/rescue")).json())["events"] == []


async def test_other_people_and_komga_are_not_affected_by_somebody_elses_lock(hass, household, cast, login, aioclient_mock):
    await _lock_ben(hass, household, cast)
    aioclient_mock.get(f"{CHAPTARR}/api/v1/queue", json={"records": []}, headers=JSON)
    aioclient_mock.get(f"{KOMGA}/api/v1/series", json={"content": []}, headers=JSON)
    assert (await (await login(cast["anna"])).get("/api/books/chaptarr/queue")).status == 200                # Anna is not restricted
    assert (await (await login(cast["ben"])).get("/api/books/komga/v1/series")).status == 200                # Komga is not touched yet


async def test_the_people_endpoint_tells_a_restricted_person_only_about_themselves(hass, household, cast, login):
    await _lock_ben(hass, household, cast)
    body = await (await (await login(cast["ben"])).get("/api/books/people")).json()
    assert body == {"people": [{"name": "Ben", "tag": "für Ben", "me": True}], "restricted": True, "can_tag": False, "shared_tag": "für alle"}


async def test_the_person_form_checks_the_lock(hass, household, cast, aioclient_mock):
    aioclient_mock.get(f"{KOMGA}/api/v2/users/me", json={"email": "x", "roles": []}, headers=JSON)
    async_mock_service(hass, "notify", "ben_phone")
    _bridge_knows(aioclient_mock, "default")
    base = {"komga_api_key": "", "abs_token": "", "notify_service": "", "tolino": False, "auto_send": False, "import_tolino": False,
            "sync_progress": False, "sync_progress_write": False, "notify_test": False, "restrict_books": True}
    sub = _ben(household, cast)

    async def save(**extra):
        flow = await hass.config_entries.subentries.async_init((household.entry_id, "user"), context={"source": config_entries.SOURCE_RECONFIGURE, "subentry_id": sub.subentry_id})
        return await hass.config_entries.subentries.async_configure(flow["flow_id"], {**base, **extra})

    assert (await save())["errors"] == {"restrict_books": "restrict_needs_abs_token"}                         # no own token
    aioclient_mock.get(f"{ABS}/api/me", json={**OPEN_ME, "username": "lena"}, headers=JSON)
    assert (await save(abs_token="abs-lena"))["errors"] == {"restrict_books": "restrict_abs_not_limited"}     # a user who sees everything
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{KOMGA}/api/v2/users/me", json={"email": "x", "roles": []}, headers=JSON)
    _bridge_knows(aioclient_mock, "default")
    aioclient_mock.get(f"{ABS}/api/me", json={**LIMITED_ME, "username": "lena"}, headers=JSON)
    result = await save(abs_token="abs-lena")
    assert result["type"] == "abort" and household.subentries[sub.subentry_id].data["restrict_books"] is True


# --- tagging from the card: release a book for a person or for everybody -----------------------------------------------------------------

def _tag_mocks(aioclient_mock, tags, patch_status=200):
    aioclient_mock.get(f"{ABS}/api/items/b1", json={"id": "b1", "media": {"tags": tags}}, headers=JSON)
    aioclient_mock.patch(f"{ABS}/api/items/b1/media", status=patch_status, json={}, headers=JSON)


def _patches(aioclient_mock):
    return [c[2] for c in aioclient_mock.mock_calls if c[0] == "PATCH"]


async def test_a_person_can_release_a_book_for_somebody(hass, household, cast, login, aioclient_mock):
    _tag_mocks(aioclient_mock, ["Fantasy"])
    resp = await (await login(cast["anna"])).post("/api/books/tags", json={"item_id": "b1", "tag": "für Ben", "tagged": True})
    assert resp.status == 200 and (await resp.json())["tags"] == ["für Ben"]                  # only the person/shared tags go back
    assert _patches(aioclient_mock) == [{"tags": ["Fantasy", "für Ben"]}]                     # the others stay
    shared = household.data["abs_token"]
    assert shared != "abs-anna" and _last_headers(aioclient_mock)["Authorization"] == f"Bearer {shared}"      # the shared user holds the update permission


async def test_taking_a_tag_back_removes_only_that_one(hass, household, cast, login, aioclient_mock):
    _tag_mocks(aioclient_mock, ["Fantasy", "für Ben", "für Anna", "für alle"])
    resp = await (await login(cast["anna"])).post("/api/books/tags", json={"item_id": "b1", "tag": "für Ben", "tagged": False})
    assert (await resp.json())["tags"] == ["für Anna", "für alle"]
    assert _patches(aioclient_mock) == [{"tags": ["Fantasy", "für Anna", "für alle"]}]


async def test_the_shared_tag_can_be_set_and_nothing_is_written_when_nothing_changes(hass, household, cast, login, aioclient_mock):
    _tag_mocks(aioclient_mock, ["für alle"])
    client = await login(cast["anna"])
    assert (await client.post("/api/books/tags", json={"item_id": "b1", "tag": "für alle", "tagged": True})).status == 200
    assert _patches(aioclient_mock) == []


@pytest.mark.parametrize("body", [
    {"item_id": "b1", "tag": "Fantasy", "tagged": True},                 # a free text tag
    {"item_id": "b1", "tag": "für Unbekannt", "tagged": True},           # nobody of that name
    {"item_id": "b1", "tag": "für Ben", "tagged": "yes"},
    {"item_id": "../x", "tag": "für Ben", "tagged": True}, {"tag": "für Ben", "tagged": True},
])
async def test_only_the_known_tags_and_valid_items_are_accepted(hass, household, cast, login, aioclient_mock, body):
    _tag_mocks(aioclient_mock, [])
    resp = await (await login(cast["anna"])).post("/api/books/tags", json=body)
    assert resp.status == 400 and not aioclient_mock.mock_calls


async def test_a_restricted_person_cannot_tag_not_even_for_themselves(hass, household, cast, login, aioclient_mock):
    await _lock_ben(hass, household, cast)
    _tag_mocks(aioclient_mock, [])
    resp = await (await login(cast["ben"])).post("/api/books/tags", json={"item_id": "b1", "tag": "für Ben", "tagged": True})
    assert resp.status == 403 and (await resp.json())["code"] == "restricted" and not aioclient_mock.mock_calls


async def test_a_missing_update_permission_is_explained(hass, household, cast, login, aioclient_mock):
    _tag_mocks(aioclient_mock, [], patch_status=403)
    resp = await (await login(cast["anna"])).post("/api/books/tags", json={"item_id": "b1", "tag": "für Ben", "tagged": True})
    assert resp.status == 502 and (await resp.json())["code"] == "abs_update_denied"
