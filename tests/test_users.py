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


def _last_headers(aioclient_mock):
    return aioclient_mock.mock_calls[-1][3]


# --- everybody reads with their own account ---------------------------------------------------------

async def test_each_person_reads_komga_with_their_own_key(hass, household, cast, login, aioclient_mock):
    aioclient_mock.get(f"{KOMGA}/api/v1/series", json={"content": []}, headers=JSON)
    seen = {}
    for who in ("anna", "ben", "cara"):
        await (await login(cast[who])).get("/api/books/komga/v1/series")
        seen[who] = _last_headers(aioclient_mock)["X-API-Key"]
    assert seen == {"anna": "komga-anna", "ben": "komga-ben", "cara": "komga-shared"}      # Cara: no account of her own -> shared


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
    await (await login(cast["ben"])).get("/api/books/komga/v1/series")
    assert _last_headers(aioclient_mock)["X-API-Key"] == "komga-shared"
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
    for who in ("ben", "cara"):
        body = await (await (await login(cast[who])).get("/api/books/tolino")).json()
        assert body == {"enabled": False}, who


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


async def test_the_dropdown_only_offers_people_who_are_not_added_yet(hass, household, cast):
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
