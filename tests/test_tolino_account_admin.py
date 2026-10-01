"""Managing the bridge's Thalia accounts from Home Assistant: create, rename "default", remove. Nothing is stored in Home Assistant (above all
not the password), every path ends in a message, and the bridge is asked first so a refusal changes nothing here."""
import logging

import aiohttp
import pytest
from homeassistant import config_entries
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.books.const import DOMAIN
from custom_components.books.tolino_registry import registry_for

from .conftest import ENTRY_DATA
from .test_tolino_accounts import BRIDGE, JSON, _family, calls_to  # noqa: F401

SECRET = "t0p-s3cret-pw!"


class FakeBridge:
    """A bridge that remembers its accounts: /status, POST /accounts, DELETE /accounts/NAME, POST /accounts/default/rename."""

    def __init__(self, aioclient_mock, accounts=("default", "cara")):
        self.accounts, self.mock, self.refuse, self.old_bridge = list(accounts), aioclient_mock, {}, False
        aioclient_mock.get(f"{BRIDGE}/status", side_effect=self._status)
        aioclient_mock.get(f"{BRIDGE}/accounts", side_effect=self._list)
        aioclient_mock.post(f"{BRIDGE}/accounts", side_effect=self._create)
        aioclient_mock.post(f"{BRIDGE}/accounts/default/rename", side_effect=self._rename)
        aioclient_mock.delete(f"{BRIDGE}/accounts/cara", side_effect=self._remove("cara"))
        aioclient_mock.delete(f"{BRIDGE}/accounts/anna", side_effect=self._remove("anna"))

    def _body(self):
        return self.mock.mock_calls[-1][2]

    def _refused(self, key, method, url):
        if key in self.refuse:
            status, body = self.refuse[key]
            return AiohttpClientMockResponse(method, url, status=status, json=body, headers=JSON)
        return None

    async def _status(self, method, url, data):
        """Like the real bridge: it answers for ONE account (the header's, else "default") - 404 if there is none such."""
        account = self.mock.mock_calls[-1][3].get("X-Tolino-Account", "default")
        if account not in self.accounts:
            return AiohttpClientMockResponse(method, url, status=404, json={"error": "unknown_account", "detail": account}, headers=JSON)
        return AiohttpClientMockResponse(method, url, json={"logged_in": True, "account": account, "accounts": list(self.accounts)}, headers=JSON)

    async def _list(self, method, url, data):
        if self.old_bridge:                                       # a bridge before 0.7: no such route
            return AiohttpClientMockResponse(method, url, status=404, text="404: Not Found")
        return AiohttpClientMockResponse(method, url, json={"accounts": list(self.accounts)}, headers=JSON)

    async def _create(self, method, url, data):
        if r := self._refused("create", method, url):
            return r
        self.accounts.append(self._body()["name"])
        return AiohttpClientMockResponse(method, url, status=201, json={"name": self._body()["name"], "logged_in": True}, headers=JSON)

    async def _rename(self, method, url, data):
        if r := self._refused("rename", method, url):
            return r
        self.accounts[self.accounts.index("default")] = self._body()["to"]
        return AiohttpClientMockResponse(method, url, json={"renamed": self._body()["to"], "logged_in": True}, headers=JSON)

    def _remove(self, name):
        async def side(method, url, data):
            if r := self._refused("remove", method, url):
                return r
            self.accounts.remove(name)
            return AiohttpClientMockResponse(method, url, json={"removed": name}, headers=JSON)
        return side


@pytest.fixture
async def fam(hass, monkeypatch):
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False, "accounts": {}}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)
    return await _family(hass)               # Anna (default), Ben (no tolino), Cara (account "cara")


async def _flow(hass, entry):
    return await hass.config_entries.subentries.async_init((entry.entry_id, "tolino_account"), context={"source": config_entries.SOURCE_USER})


async def _pick(hass, flow, option):
    return await hass.config_entries.subentries.async_configure(flow["flow_id"], {"next_step_id": option})


async def _submit(hass, result, data):
    return await hass.config_entries.subentries.async_configure(result["flow_id"], data)


# --- the menu --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("accounts,options", [
    (("default", "cara"), ["create", "rename", "remove"]),
    (("anna", "cara"), ["create", "remove"]),                        # default has been renamed already
    (("default",), ["create", "rename"]),                            # nothing but default: nothing to remove
])
async def test_the_menu_offers_what_makes_sense(hass, fam, aioclient_mock, accounts, options):
    users, entry = fam
    FakeBridge(aioclient_mock, accounts)
    result = await _flow(hass, entry)
    assert result["type"].value == "menu" and result["menu_options"] == options


async def test_without_a_bridge_it_says_so(hass, aioclient_mock, monkeypatch):
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await _flow(hass, entry)
    assert result["type"].value == "abort" and result["reason"] == "no_bridge"


async def test_a_bridge_that_does_not_answer_is_reported(hass, fam, aioclient_mock):
    users, entry = fam
    aioclient_mock.get(f"{BRIDGE}/status", exc=aiohttp.ClientConnectionError("refused"))
    aioclient_mock.get(f"{BRIDGE}/accounts", exc=aiohttp.ClientConnectionError("refused"))
    result = await _flow(hass, entry)
    assert result["type"].value == "abort" and result["reason"] == "bridge_unreachable"


# --- create ------------------------------------------------------------------------------------------------------------

async def test_create_sends_the_credentials_once_and_stores_nothing_here(hass, fam, aioclient_mock, caplog):
    caplog.set_level(logging.DEBUG)
    users, entry = fam
    bridge = FakeBridge(aioclient_mock)
    subentries_before = dict(entry.subentries)
    form = await _pick(hass, await _flow(hass, entry), "create")
    assert form["type"].value == "form" and form["step_id"] == "create"
    done = await _submit(hass, form, {"name": "dora", "thalia_user": " dora@thalia.de ", "thalia_password": SECRET})
    assert done["type"].value == "abort" and done["reason"] == "account_created" and done["description_placeholders"] == {"name": "dora"}
    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST" and str(c[1]).endswith("/accounts")]
    assert len(posts) == 1 and posts[0][2] == {"name": "dora", "user": "dora@thalia.de", "password": SECRET}
    assert posts[0][3]["Authorization"] == "Bearer bridge-token" and "dora" in bridge.accounts
    # the password goes nowhere else
    assert SECRET not in repr(dict(entry.data)) and SECRET not in repr([s.data for s in entry.subentries.values()])
    assert SECRET not in repr(hass.data[DOMAIN]) and SECRET not in caplog.text
    assert dict(entry.subentries) == subentries_before                                     # this flow creates no entry


@pytest.mark.parametrize("name,error", [("Dora", "name_invalid"), ("default", "name_invalid"), ("a b", "name_invalid"), ("x" * 33, "name_invalid"),
                                         ("", "name_invalid"), ("cara", "name_taken")])
async def test_create_checks_the_name_before_it_asks_the_bridge(hass, fam, aioclient_mock, name, error):
    users, entry = fam
    FakeBridge(aioclient_mock)
    form = await _pick(hass, await _flow(hass, entry), "create")
    result = await _submit(hass, form, {"name": name, "thalia_user": "d@x", "thalia_password": SECRET})
    assert result["type"].value == "form" and result["errors"] == {"name": error}
    assert not calls_to(aioclient_mock, "/accounts", "POST")


@pytest.mark.parametrize("user,password", [("", SECRET), ("d@x", "")])
async def test_create_needs_both_e_mail_and_password(hass, fam, aioclient_mock, user, password):
    users, entry = fam
    FakeBridge(aioclient_mock)
    form = await _pick(hass, await _flow(hass, entry), "create")
    result = await _submit(hass, form, {"name": "dora", "thalia_user": user, "thalia_password": password})
    assert result["errors"] == {"thalia_password": "credentials_missing"} and not calls_to(aioclient_mock, "/accounts", "POST")


@pytest.mark.parametrize("status,body,error", [
    (409, {"error": "account_exists", "detail": "exists"}, "name_taken"),
    (400, {"error": "bad_request", "detail": "name"}, "name_invalid"),
    (503, {"error": "captcha", "detail": "blocked"}, "login_captcha"),
    (503, {"error": "rejected", "detail": "wrong password"}, "login_rejected"),
    (503, {"error": "no_device", "detail": "no device"}, "login_no_device"),
    (503, {"error": "login_backoff", "detail": "wait"}, "login_backoff"),
    (503, {"error": "login_error", "detail": "chrome crashed"}, "login_failed"),
    (502, {"error": "bosh", "detail": "x"}, "login_failed"),
])
async def test_create_translates_what_the_bridge_says_and_can_be_retried(hass, fam, aioclient_mock, status, body, error):
    users, entry = fam
    bridge = FakeBridge(aioclient_mock)
    bridge.refuse["create"] = (status, body)
    form = await _pick(hass, await _flow(hass, entry), "create")
    result = await _submit(hass, form, {"name": "dora", "thalia_user": "dora@thalia.de", "thalia_password": SECRET})
    assert result["type"].value == "form" and result["errors"] == {"base": error}
    assert result["description_placeholders"]["detail"] == body["detail"]
    shown = {str(k): k.description for k in result["data_schema"].schema}                  # the form keeps name and e-mail, never the password
    assert shown["name"]["suggested_value"] == "dora" and shown["thalia_user"]["suggested_value"] == "dora@thalia.de"
    assert shown["thalia_password"] is None or "suggested_value" not in (shown["thalia_password"] or {})
    bridge.refuse.clear()
    again = await _submit(hass, result, {"name": "dora", "thalia_user": "dora@thalia.de", "thalia_password": SECRET})
    assert again["type"].value == "abort" and again["reason"] == "account_created"          # a retry goes through


async def test_create_reports_a_bridge_that_dies_midway(hass, fam, aioclient_mock):
    users, entry = fam
    FakeBridge(aioclient_mock)
    form = await _pick(hass, await _flow(hass, entry), "create")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BRIDGE}/status", json={"accounts": ["default", "cara"]}, headers=JSON)
    aioclient_mock.get(f"{BRIDGE}/accounts", json={"accounts": ["default", "cara"]}, headers=JSON)
    aioclient_mock.post(f"{BRIDGE}/accounts", exc=aiohttp.ClientConnectionError("gone"))
    result = await _submit(hass, form, {"name": "dora", "thalia_user": "d@x", "thalia_password": SECRET})
    assert result["errors"] == {"base": "bridge_unreachable"}


# --- rename "default" ---------------------------------------------------------------------------------------------------

async def test_rename_switches_the_person_and_carries_their_books_over_in_one_go(hass, fam, aioclient_mock):
    users, entry = fam
    bridge = FakeBridge(aioclient_mock)
    await registry_for(hass, "default").async_set("b1", "d1", "b1.epub")
    await registry_for(hass, "default").async_set("b2", "d2", "b2.epub")
    form = await _pick(hass, await _flow(hass, entry), "rename")
    assert form["description_placeholders"]["person"] == "Anna"                              # who is affected is shown
    done = await _submit(hass, form, {"to": "anna"})
    assert done["type"].value == "abort" and done["reason"] == "account_renamed" and done["description_placeholders"] == {"name": "anna", "books": "2"}
    assert bridge.accounts == ["anna", "cara"]
    anna = next(s for s in entry.subentries.values() if s.unique_id == users["anna"].id)
    assert anna.data["tolino_account"] == "anna"
    assert set(registry_for(hass, "anna").items) == {"b1", "b2"} and registry_for(hass, "default").items == {}
    assert "anna" in hass.data[DOMAIN]["jobs"]


async def test_rename_asks_the_bridge_first_so_a_refusal_changes_nothing_here(hass, fam, aioclient_mock):
    users, entry = fam
    bridge = FakeBridge(aioclient_mock)
    bridge.refuse["rename"] = (409, {"error": "account_exists", "detail": "exists"})
    await registry_for(hass, "default").async_set("b1", "d1", "b1.epub")
    form = await _pick(hass, await _flow(hass, entry), "rename")
    result = await _submit(hass, form, {"to": "anna"})
    assert result["errors"] == {"base": "name_taken"}
    anna = next(s for s in entry.subentries.values() if s.unique_id == users["anna"].id)
    assert anna.data["tolino_account"] == "" and set(registry_for(hass, "default").items) == {"b1"}


@pytest.mark.parametrize("to,error", [("Anna", "name_invalid"), ("default", "name_invalid"), ("cara", "name_taken")])
async def test_rename_checks_the_new_name(hass, fam, aioclient_mock, to, error):
    users, entry = fam
    FakeBridge(aioclient_mock)
    form = await _pick(hass, await _flow(hass, entry), "rename")
    result = await _submit(hass, form, {"to": to})
    assert result["errors"] == {"to": error} and not calls_to(aioclient_mock, "/rename", "POST")


async def test_rename_when_nobody_uses_default_only_renames_in_the_bridge(hass, aioclient_mock, monkeypatch):
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False, "accounts": {}}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)
    from .conftest import person
    user = await hass.auth.async_create_user("Ben", group_ids=["system-users"])
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA, "tolino_url": BRIDGE, "tolino_token": "bridge-token"},
                            subentries_data=[person(user)])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    bridge = FakeBridge(aioclient_mock, ("default",))
    form = await _pick(hass, await _flow(hass, entry), "rename")
    assert form["description_placeholders"]["person"] == "-"
    done = await _submit(hass, form, {"to": "anna"})
    assert done["reason"] == "account_renamed" and done["description_placeholders"]["books"] == "0" and bridge.accounts == ["anna"]


# --- remove ----------------------------------------------------------------------------------------------------------------

async def test_remove_deletes_the_account_and_what_we_remember_about_it(hass, fam, aioclient_mock):
    users, entry = fam
    bridge = FakeBridge(aioclient_mock, ("default", "cara", "anna"))
    await hass.data[DOMAIN]["jobs"]["cara"]["auto_send"]._store.async_save({"since": 5})
    form = await _pick(hass, await _flow(hass, entry), "remove")
    # "anna" is used by nobody (Anna still works through "default")
    done = await _submit(hass, form, {"account": "anna", "confirm": True})
    assert done["type"].value == "abort" and done["reason"] == "account_removed" and done["description_placeholders"] == {"name": "anna"}
    assert bridge.accounts == ["default", "cara"] and [c[0] for c in aioclient_mock.mock_calls if "/accounts/anna" in str(c[1])] == ["DELETE"]


async def test_remove_forgets_the_list_of_sent_books_so_a_later_account_of_that_name_starts_clean(hass, fam, aioclient_mock):
    users, entry = fam
    FakeBridge(aioclient_mock, ("default", "cara", "anna"))
    from custom_components.books.tolino_registry import async_ensure_registry
    reg = await async_ensure_registry(hass, "anna")
    await reg.async_set("old-book", "d-old", "o.epub")
    form = await _pick(hass, await _flow(hass, entry), "remove")
    await _submit(hass, form, {"account": "anna", "confirm": True})
    assert "anna" not in hass.data[DOMAIN]["registries"]
    fresh = await async_ensure_registry(hass, "anna")                                       # same name again, later
    assert fresh.items == {}


async def test_remove_refuses_an_account_a_person_still_uses(hass, fam, aioclient_mock):
    users, entry = fam
    bridge = FakeBridge(aioclient_mock)
    form = await _pick(hass, await _flow(hass, entry), "remove")
    result = await _submit(hass, form, {"account": "cara", "confirm": True})
    assert result["errors"] == {"account": "account_in_use"} and "cara" in bridge.accounts
    assert not calls_to(aioclient_mock, "/accounts/cara", "DELETE")


async def test_remove_needs_the_confirmation(hass, fam, aioclient_mock):
    users, entry = fam
    FakeBridge(aioclient_mock, ("default", "cara", "anna"))
    form = await _pick(hass, await _flow(hass, entry), "remove")
    result = await _submit(hass, form, {"account": "anna", "confirm": False})
    assert result["errors"] == {"confirm": "confirm_required"} and not calls_to(aioclient_mock, "/accounts/anna", "DELETE")


async def test_remove_passes_on_a_refusal_of_the_bridge(hass, fam, aioclient_mock):
    users, entry = fam
    bridge = FakeBridge(aioclient_mock, ("default", "cara", "anna"))
    bridge.refuse["remove"] = (503, {"error": "login_error", "detail": "busy"})
    form = await _pick(hass, await _flow(hass, entry), "remove")
    result = await _submit(hass, form, {"account": "anna", "confirm": True})
    assert result["errors"] == {"base": "login_failed"} and "anna" in bridge.accounts


async def test_the_default_account_can_never_be_removed_here(hass, fam, aioclient_mock):
    users, entry = fam
    FakeBridge(aioclient_mock, ("default",))
    result = await _flow(hass, entry)
    assert "remove" not in result["menu_options"]


# --- found against the real bridge: after renaming "default", /status without a name is a 404 ----------------------------------------

async def test_after_the_rename_the_forms_still_find_the_accounts(hass, fam, aioclient_mock):
    users, entry = fam
    FakeBridge(aioclient_mock, ("anna", "cara"))                        # no "default": /status without a header answers 404
    menu = await _flow(hass, entry)
    assert menu["type"].value == "menu" and menu["menu_options"] == ["create", "remove"]        # not "the bridge does not answer"


async def test_the_person_form_still_offers_the_accounts_after_the_rename(hass, fam, aioclient_mock):
    users, entry = fam
    FakeBridge(aioclient_mock, ("anna", "cara", "dora"))
    dora = await hass.auth.async_create_user("Dora", group_ids=["system-users"])
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "user"), context={"source": config_entries.SOURCE_USER})
    field = next(v for k, v in flow["data_schema"].schema.items() if str(k) == "tolino_account")
    assert [o["value"] for o in field.config["options"]] == ["anna", "cara", "dora"]


async def test_an_older_bridge_without_the_list_route_still_works(hass, fam, aioclient_mock):
    users, entry = fam
    bridge = FakeBridge(aioclient_mock, ("default",))
    bridge.old_bridge = True                                            # GET /accounts is a 404: the list comes from /status
    menu = await _flow(hass, entry)
    assert menu["type"].value == "menu" and menu["menu_options"] == ["create", "rename"]
