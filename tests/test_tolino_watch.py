"""Bridge watcher: binary sensor + one alert per incident (after 2 bad polls) + one all-clear."""
import aiohttp
from homeassistant.components.persistent_notification import _async_get_or_create_notifications
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.books.const import DOMAIN

from .conftest import ENTRY_DATA, admin_person

BRIDGE = "http://bridge.test:8199"
SENSOR = "binary_sensor.tolino_bridge_problem"
JSON = {"Content-Type": "application/json"}
OK = {"logged_in": True, "last_error": None, "session_age_s": 60, "relogin_in_s": 28000, "login_backoff_s": 0}
LOGGED_OUT = {"logged_in": False, "last_error": "captcha: blocked", "login_backoff_s": 600}


async def _setup(hass, aioclient_mock, status, extra=None):
    """aioclient_mock must exist before setup: the watcher polls once in the background right away."""
    aioclient_mock.get(f"{BRIDGE}/status", json=status, headers=JSON)
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={
        **ENTRY_DATA, "tolino_url": BRIDGE, "tolino_token": "bridge-token", **(extra or {})},
        subentries_data=[await admin_person(hass, tolino=True)])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN]["tolino_watcher"]


def _respond(aioclient_mock, **kw):
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BRIDGE}/status", headers=JSON, **kw)


async def test_no_sensor_without_bridge(hass, setup_entry):
    assert hass.states.get(SENSOR) is None
    assert "tolino_watcher" not in hass.data[DOMAIN]


async def test_healthy_bridge_is_off(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, OK)
    state = hass.states.get(SENSOR)
    assert state.state == "off"
    assert state.attributes["logged_in"] is True and state.attributes["relogin_in_s"] == 28000
    assert aioclient_mock.mock_calls[-1][3]["Authorization"] == "Bearer bridge-token"


async def test_logged_out_bridge_is_a_problem(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, LOGGED_OUT)
    state = hass.states.get(SENSOR)
    assert state.state == "on" and state.attributes["last_error"] == "captcha: blocked"


async def test_unreachable_bridge_is_a_problem(hass, aioclient_mock):
    aioclient_mock.get(f"{BRIDGE}/status", exc=aiohttp.ClientConnectionError("refused"))
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={
        **ENTRY_DATA, "tolino_url": BRIDGE, "tolino_token": "t"}, subentries_data=[await admin_person(hass, tolino=True)])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.state == "on" and state.attributes["reachable"] is False


async def test_one_alert_after_two_bad_polls_and_one_all_clear(hass, aioclient_mock):
    pushes = async_mock_service(hass, "notify", "mobile_app_test")
    watcher = await _setup(hass, aioclient_mock, LOGGED_OUT, {"notify_service": "notify.mobile_app_test"})
    assert pushes == []                                        # poll 1 (startup): not yet
    await watcher.async_refresh()                              # poll 2: announce
    await hass.async_block_till_done()
    assert len(pushes) == 1
    assert "captcha: blocked" in pushes[0].data["message"] and pushes[0].data["title"] == "tolino-Bridge Problem"
    assert "books_tolino_bridge" in _async_get_or_create_notifications(hass)
    await watcher.async_refresh()                              # poll 3: still bad, no repeat
    await hass.async_block_till_done()
    assert len(pushes) == 1
    _respond(aioclient_mock, json=OK)
    await watcher.async_refresh()                              # recovered
    await hass.async_block_till_done()
    assert len(pushes) == 2 and "läuft wieder" in pushes[1].data["message"]
    assert "books_tolino_bridge" not in _async_get_or_create_notifications(hass)
    assert hass.states.get(SENSOR).state == "off"


async def test_short_hiccup_stays_quiet(hass, aioclient_mock):
    pushes = async_mock_service(hass, "notify", "mobile_app_test")
    watcher = await _setup(hass, aioclient_mock, LOGGED_OUT, {"notify_service": "notify.mobile_app_test"})
    _respond(aioclient_mock, json=OK)
    await watcher.async_refresh()                              # bad, then good: never reached 2 in a row
    await hass.async_block_till_done()
    _respond(aioclient_mock, json=LOGGED_OUT)
    await watcher.async_refresh()                              # bad again: counter restarted at 1
    await hass.async_block_till_done()
    assert pushes == []


async def test_new_incident_alerts_again(hass, aioclient_mock):
    pushes = async_mock_service(hass, "notify", "mobile_app_test")
    watcher = await _setup(hass, aioclient_mock, LOGGED_OUT, {"notify_service": "notify.mobile_app_test"})
    await watcher.async_refresh(); await hass.async_block_till_done()          # alert #1
    _respond(aioclient_mock, json=OK)
    await watcher.async_refresh(); await hass.async_block_till_done()          # all-clear
    _respond(aioclient_mock, json=LOGGED_OUT)
    await watcher.async_refresh(); await watcher.async_refresh(); await hass.async_block_till_done()   # alert #2
    assert [p.data["title"] for p in pushes] == ["tolino-Bridge Problem", "tolino-Bridge", "tolino-Bridge Problem"]


async def test_unload_and_reload_keep_working(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, OK)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "off"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert "tolino_watcher" not in hass.data.get(DOMAIN, {})
