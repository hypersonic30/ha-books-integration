"""Reading progress of audiobooks between the tolino cloud and Audiobookshelf: positions, both directions, what is left alone."""
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.books.audio_positions import medialoc_to_time, time_to_medialoc, tolino_progress
from custom_components.books.const import DOMAIN

from .conftest import ABS, ENTRY_DATA, admin_person

BRIDGE = "http://bridge.test:8199"
JSON = {"Content-Type": "application/json"}
ITEM, PID = "hb1", "DT0244.9783837154757"
# the real "Such den Osterhasen": lengths of the 11 tracks in seconds (as Audiobookshelf measures the files), and the tolino side
DURATIONS = [89.053, 163.413, 180.56, 156.4, 150.147, 112.307, 113.24, 113.613, 140.853, 48.267, 152.427]
TRACK_MS = [89104, 163448, 180611, 156447, 150178, 112353, 113290, 113660, 140900, 48310, 152470]


def test_a_tolino_position_becomes_the_abs_time():
    assert medialoc_to_time("#medialoc(1,0)", DURATIONS) == 0
    assert medialoc_to_time("#medialoc(3,91)", DURATIONS) == pytest.approx(89.053 + 163.413 + 91)
    assert medialoc_to_time("#medialoc(11,8)", DURATIONS) == pytest.approx(sum(DURATIONS[:10]) + 8)
    assert medialoc_to_time("#medialoc(2,9999)", DURATIONS) == pytest.approx(89.053 + 163.413)       # never beyond the track


@pytest.mark.parametrize("position", [None, "", "#medialoc(0,5)", "#medialoc(12,5)", "#medialoc(3)", "OEBPS/a.xhtml#point(/1/2:0)", "#medialoc(3,91);x"])
def test_what_is_no_position_of_this_audiobook_is_ignored(position):
    assert medialoc_to_time(position, DURATIONS) is None


def test_abs_time_becomes_a_tolino_position():
    assert time_to_medialoc(0, DURATIONS) == "#medialoc(1,0)"
    assert time_to_medialoc(30.7, DURATIONS) == "#medialoc(1,30)"                                       # whole seconds, rounded down
    assert time_to_medialoc(89.053, DURATIONS) == "#medialoc(2,0)"                                      # the boundary belongs to the next track
    assert time_to_medialoc(sum(DURATIONS[:4]) + 31, DURATIONS) == "#medialoc(5,31)"
    assert time_to_medialoc(sum(DURATIONS) + 500, DURATIONS) == "#medialoc(11,152)"                     # beyond the end: the end of the last track
    assert time_to_medialoc(-1, DURATIONS) is None and time_to_medialoc(5, []) is None


def test_both_ways_agree_within_a_second():
    for t in (0, 12.4, 89, 250.2, 600, 1000.9, 1419):
        back = medialoc_to_time(time_to_medialoc(t, DURATIONS), DURATIONS)
        assert 0 <= t - back < 1.0


@pytest.mark.parametrize("track,seconds,expected", [(1, 15, 0.0236111), (1, 31, 0.0347222), (2, 1, 0.0756944), (2, 67, 0.1215278), (3, 91, 0.2520833), (5, 31, 0.4444444), (11, 8, 0.9)])
def test_the_progress_matches_what_the_app_wrote(track, seconds, expected):
    """Real bookmarks of the tolino app; the formula is an estimate (about a second of play time), so a small tolerance."""
    played = sum(DURATIONS[: track - 1]) + seconds
    assert tolino_progress(played, TRACK_MS, 1440) == pytest.approx(expected, abs=0.0015)


def test_the_progress_stays_between_0_and_1():
    assert tolino_progress(-5, TRACK_MS, 1440) >= 0 and tolino_progress(99999, TRACK_MS, 1440) == 1.0 and tolino_progress(10, TRACK_MS, 0) == 0.0


# --- the sync job ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
async def synced(hass, monkeypatch):
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA, "tolino_url": BRIDGE, "tolino_token": "bt"},
                            subentries_data=[await admin_person(hass, tolino=True, sync_progress=True, sync_progress_write=True)])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.data[DOMAIN]["registries"]["default"].async_set(ITEM, PID, "imported-audio")
    return hass.data[DOMAIN]["jobs"]["default"]["progress_sync"]


def audio_item():
    return {"id": ITEM, "media": {"duration": sum(DURATIONS), "audioFiles": [{"index": i + 1, "duration": d} for i, d in enumerate(DURATIONS)]}}


def world(aioclient_mock, books, abs_progress=None, put_status=200):
    aioclient_mock.get(f"{BRIDGE}/progress", json={"books": books}, headers=JSON)
    aioclient_mock.get(f"{BRIDGE}/audiobooks/{PID}", json={"tracks": [{"number": i + 1, "duration_ms": ms} for i, ms in enumerate(TRACK_MS)], "duration_s": 1440}, headers=JSON)
    aioclient_mock.put(f"{BRIDGE}/progress/{PID}", status=put_status, json={"modified": 7777, "position": "x", "finished": False}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/items/{ITEM}", json=audio_item(), headers=JSON)
    if abs_progress is None:
        aioclient_mock.get(f"{ABS}/api/me/progress/{ITEM}", status=404, json={"error": "nf"}, headers=JSON)
    else:
        aioclient_mock.get(f"{ABS}/api/me/progress/{ITEM}", json=abs_progress, headers=JSON)
    aioclient_mock.patch(f"{ABS}/api/me/progress/{ITEM}", json={"success": True}, headers=JSON)


def sent(aioclient_mock, method):
    return [c[2] for c in aioclient_mock.mock_calls if c[0] == method and (str(c[1]).endswith(f"/progress/{PID}") or str(c[1]).endswith(f"/me/progress/{ITEM}"))]


async def test_where_the_app_stopped_is_where_audiobookshelf_continues(hass, synced, aioclient_mock):
    world(aioclient_mock, {PID: {"progress": 0.2521, "position": "#medialoc(3,91)", "modified": 2000, "page": "3", "pages": "11", "finished": False}})
    result = await synced.async_sync()
    assert result["imported"] == [ITEM]
    body, = sent(aioclient_mock, "PATCH")
    assert body["currentTime"] == pytest.approx(89.053 + 163.413 + 91) and body["duration"] == pytest.approx(sum(DURATIONS)) and body["isFinished"] is False
    assert body["progress"] == pytest.approx((89.053 + 163.413 + 91) / sum(DURATIONS))
    assert not sent(aioclient_mock, "PUT")                                                           # nothing goes back for the same place


async def test_where_abs_stopped_is_where_the_app_continues(hass, synced, aioclient_mock):
    world(aioclient_mock, {}, abs_progress={"currentTime": 589.426 + 31, "duration": sum(DURATIONS), "isFinished": False, "lastUpdate": 5000})
    result = await synced.async_sync()
    assert result["exported"] == [ITEM]
    body, = sent(aioclient_mock, "PUT")
    assert body["position"] == "#medialoc(5,31)" and 0.43 < body["progress"] < 0.45
    assert not sent(aioclient_mock, "PATCH")


async def test_the_same_place_is_not_written_again(hass, synced, aioclient_mock):
    world(aioclient_mock, {PID: {"progress": 0.25, "position": "#medialoc(3,91)", "modified": 2000, "page": "3", "pages": "11", "finished": False}},
          abs_progress={"currentTime": 89.053 + 163.413 + 92.5, "duration": sum(DURATIONS), "isFinished": False, "lastUpdate": 9000})   # 1.5 s apart
    await synced.async_sync()
    assert not sent(aioclient_mock, "PUT")                                                           # closer than 3 seconds: the same place


async def test_a_finished_audiobook_in_abs_does_not_touch_tolino(hass, synced, aioclient_mock):
    world(aioclient_mock, {}, abs_progress={"currentTime": sum(DURATIONS), "duration": sum(DURATIONS), "isFinished": True, "lastUpdate": 5000})
    result = await synced.async_sync()
    assert result["exported"] == [] and not sent(aioclient_mock, "PUT")


async def test_the_newer_side_wins_for_audiobooks_too(hass, synced, aioclient_mock):
    world(aioclient_mock, {PID: {"progress": 0.25, "position": "#medialoc(3,91)", "modified": 2000, "page": "3", "pages": "11", "finished": False}},
          abs_progress={"currentTime": 700.0, "duration": sum(DURATIONS), "isFinished": False, "lastUpdate": 99999})                   # ABS is newer
    await synced.async_sync()
    put, = sent(aioclient_mock, "PUT")
    assert put["position"] == time_to_medialoc(700.0, DURATIONS) and not sent(aioclient_mock, "PATCH")


async def test_a_position_that_does_not_fit_the_audiobook_writes_nothing_and_is_not_retried(hass, synced, aioclient_mock):
    world(aioclient_mock, {PID: {"progress": 0.9, "position": "#medialoc(40,5)", "modified": 2000, "page": "40", "pages": "40", "finished": False}})
    first = await synced.async_sync()
    assert first["imported"] == [ITEM] and not sent(aioclient_mock, "PATCH")
    await synced.async_sync()
    assert len([c for c in aioclient_mock.mock_calls if c[0] == "GET" and str(c[1]).endswith(f"/me/progress/{ITEM}")]) <= 4          # no endless loop
