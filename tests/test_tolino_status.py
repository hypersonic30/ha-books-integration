"""Visibility of the background jobs: sensors (last sync / last auto-sent) and bus events for automations."""
import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.books.const import DOMAIN, EVENT_PROGRESS_SYNCED, EVENT_TOLINO_SENT

from .conftest import ENTRY_DATA
from .test_tolino import ITEM as _ITEM_, JSON, _bridge_upload, _mock_abs
from .test_tolino_autosend import BRIDGE, book, it, listing, upload_ok
from .test_tolino_autosend import _setup as _setup_auto
from .test_tolino_sync import ITEM, DID, mock_world, state
from .test_tolino_sync_write import CFI, POINT, _make, abs_state, mock_put

SYNC_UID, AUTO_UID = "books_tolino_progress_sync", "books_tolino_auto_send"


def entity(hass, uid):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, uid)
    return hass.states.get(entity_id) if entity_id else None


async def test_no_sensors_without_a_bridge(hass, setup_entry):
    assert entity(hass, SYNC_UID) is None and entity(hass, AUTO_UID) is None


async def test_sensors_exist_with_a_bridge_and_start_empty(hass, monkeypatch):
    await _setup_auto(hass, monkeypatch, auto=False)
    sync, auto = entity(hass, SYNC_UID), entity(hass, AUTO_UID)
    assert sync is not None and sync.state == "unknown" and sync.attributes["enabled"] is False
    assert auto is not None and auto.state == "unknown" and auto.attributes["total_sent"] == 0


async def test_sync_sensor_shows_the_last_run(hass, monkeypatch, aioclient_mock):
    rw = await _make(hass, monkeypatch, True)
    mock_world(aioclient_mock, {DID: state(0.5, "OEBPS/a.xhtml#point(/1/4/2/1:9)")})
    await rw.async_sync(); await hass.async_block_till_done()
    s = entity(hass, SYNC_UID)
    assert s.state not in ("unknown", "unavailable") and "T" in s.state                 # an ISO timestamp
    assert s.attributes["imported"] == 1 and s.attributes["exported"] == 0 and s.attributes["write_enabled"] is True
    aioclient_mock.clear_requests(); mock_world(aioclient_mock, {DID: state(0.5, "OEBPS/a.xhtml#point(/1/4/2/1:9)")})
    await rw.async_sync(); await hass.async_block_till_done()
    assert entity(hass, SYNC_UID).attributes["imported"] == 0                                  # shows the LAST run, not a total


async def test_auto_send_sensor_shows_the_last_book_and_survives_a_restart(hass, monkeypatch, aioclient_mock):
    job = await _setup_auto(hass, monkeypatch)
    listing(aioclient_mock, [it("n1", title="Das neue Buch")]); book(aioclient_mock, "n1"); upload_ok(aioclient_mock)
    await job.async_run(); await hass.async_block_till_done()
    a = entity(hass, AUTO_UID)
    assert a.state == "Das neue Buch" and a.attributes["total_sent"] == 1 and a.attributes["sent_at"]
    # like a Home Assistant restart: the in-memory job is gone, its stored state is not
    hass.data[DOMAIN].pop("auto_send"); hass.data[DOMAIN].pop("jobs")
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert await hass.config_entries.async_reload(entry.entry_id); await hass.async_block_till_done()
    a = entity(hass, AUTO_UID)
    assert a.state == "Das neue Buch" and a.attributes["total_sent"] == 1


async def test_auto_send_sensor_counts_given_up_books(hass, monkeypatch, aioclient_mock):
    job = await _setup_auto(hass, monkeypatch)
    listing(aioclient_mock, [it("bad", fmt="mobi")]); book(aioclient_mock, "bad", "mobi")
    aioclient_mock.post(f"{BRIDGE}/upload", status=422, json={"error": "convert_failed", "detail": "DRM"}, headers=JSON)
    await job.async_run(); await hass.async_block_till_done()
    assert entity(hass, AUTO_UID).attributes["given_up"] == 1


# --- events for automations ---------------------------------------------------------------------

async def test_manual_send_fires_an_event(hass, monkeypatch, hass_client, aioclient_mock):
    await _setup_auto(hass, monkeypatch, auto=False)
    events = async_capture_events(hass, EVENT_TOLINO_SENT)
    _mock_abs(aioclient_mock); _bridge_upload(aioclient_mock, "dX")
    resp = await (await hass_client()).post("/api/books/tolino", json={"abs_item_id": "abc123"})
    assert resp.status == 200
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data == {"item_id": "abc123", "title": "Das Reich der Dämmerung", "filename": "Dämmerung.epub", "deliverable_id": "dX",
                              "replaced": False, "auto": False}


async def test_auto_send_fires_an_event_marked_auto(hass, monkeypatch, aioclient_mock):
    job = await _setup_auto(hass, monkeypatch)
    events = async_capture_events(hass, EVENT_TOLINO_SENT)
    listing(aioclient_mock, [it("n1", title="Auto")]); book(aioclient_mock, "n1"); upload_ok(aioclient_mock, "dA")
    await job.async_run(); await hass.async_block_till_done()
    assert [e.data["auto"] for e in events] == [True] and events[0].data["item_id"] == "n1" and events[0].data["deliverable_id"] == "dA"


async def test_no_event_when_the_send_fails(hass, monkeypatch, aioclient_mock):
    job = await _setup_auto(hass, monkeypatch)
    events = async_capture_events(hass, EVENT_TOLINO_SENT)
    listing(aioclient_mock, [it("n1")]); book(aioclient_mock, "n1")
    aioclient_mock.post(f"{BRIDGE}/upload", status=503, json={"error": "captcha", "detail": "x"}, headers=JSON)
    await job.async_run(); await hass.async_block_till_done()
    assert events == []


async def test_progress_events_name_the_direction(hass, monkeypatch, aioclient_mock):
    rw = await _make(hass, monkeypatch, True)
    events = async_capture_events(hass, EVENT_PROGRESS_SYNCED)
    mock_world(aioclient_mock, {DID: state(0.5, POINT, modified=2000)})
    await rw.async_sync(); await hass.async_block_till_done()
    assert [(e.data["direction"], e.data["finished"]) for e in events] == [("tolino_to_abs", False)]
    aioclient_mock.clear_requests(); mock_world(aioclient_mock, {}, abs_progress=abs_state(9000, 0.4)); mock_put(aioclient_mock)
    await hass.data[DOMAIN]["tolino_sent"].async_update(ITEM, progress_modified=0, abs_seen=0)
    await rw.async_sync(); await hass.async_block_till_done()
    assert events[-1].data["direction"] == "abs_to_tolino"
