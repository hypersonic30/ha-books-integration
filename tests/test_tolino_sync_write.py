"""Audiobookshelf -> tolino (opt-in on top of the import), newest wins, no ping-pong, failures retried."""
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.books.const import DOMAIN

from .conftest import ABS, ENTRY_DATA, admin_person
from .test_tolino_sync import BRIDGE, DID, ITEM, JSON, make_epub, mock_world, patches, state


async def _make(hass, monkeypatch, write):
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA, "tolino_url": BRIDGE, "tolino_token": "bt"},
                            subentries_data=[await admin_person(hass, tolino=True, sync_progress=True, sync_progress_write=write)])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.data[DOMAIN]["tolino_sent"].async_set(ITEM, DID, "x.epub")
    return hass.data[DOMAIN]["jobs"]["default"]["progress_sync"]


@pytest.fixture
async def rw(hass, monkeypatch):
    return await _make(hass, monkeypatch, True)


@pytest.fixture
async def ro(hass, monkeypatch):
    return await _make(hass, monkeypatch, False)


CFI = "epubcfi(/6/6!/4/4/1:0)"                                      # heading text of the compact test chapter
POINT = "OEBPS/c d.xhtml#point(/1/3/3/1:0)"


def abs_state(at=5000, progress=0.4, cfi=CFI, finished=False):
    return {"lastUpdate": at, "ebookProgress": progress, "ebookLocation": cfi, "isFinished": finished}


def mock_put(aioclient_mock, modified=9000, finished=False, status=200):
    aioclient_mock.put(f"{BRIDGE}/progress/{DID}", status=status, headers=JSON,
                       json={"modified": modified, "finished": finished, "progress": 0.4} if status == 200 else {"error": "bosh", "detail": "x"})


def puts(aioclient_mock):
    return [c[2] for c in aioclient_mock.mock_calls if c[0] == "PUT"]


def reg(hass):
    return hass.data[DOMAIN]["tolino_sent"].get(ITEM)


async def test_abs_progress_is_written_to_tolino(hass, rw, aioclient_mock):
    mock_world(aioclient_mock, {}, abs_progress=abs_state()); mock_put(aioclient_mock)
    summary = await rw.async_sync()
    assert summary["exported"] == [ITEM] and summary["imported"] == []
    assert puts(aioclient_mock) == [{"progress": 0.4, "position": POINT}]
    assert [c for c in aioclient_mock.mock_calls if c[0] == "PUT"][0][3]["Authorization"] == "Bearer bt"
    assert reg(hass)["abs_seen"] == 5000 and reg(hass)["progress_modified"] == 9000
    assert patches(aioclient_mock) == []                                      # nothing flows back into ABS


async def test_no_ping_pong_after_an_export(hass, rw, aioclient_mock):
    mock_world(aioclient_mock, {}, abs_progress=abs_state()); mock_put(aioclient_mock)
    await rw.async_sync()
    aioclient_mock.clear_requests()                                            # Tolino now reports exactly what we wrote
    mock_world(aioclient_mock, {DID: state(0.4, POINT, modified=9000)}, abs_progress=abs_state()); mock_put(aioclient_mock)
    summary = await rw.async_sync()
    assert summary["exported"] == [] and summary["imported"] == [] and puts(aioclient_mock) == [] and patches(aioclient_mock) == []


async def test_no_ping_pong_after_an_import(hass, rw, aioclient_mock, monkeypatch):
    """Our own write makes ABS's lastUpdate jump; that must not look like the user reading in the card."""
    reads = iter([None, {"lastUpdate": 7000, "ebookProgress": 0.5, "ebookLocation": CFI, "isFinished": False}])   # before / after our PATCH
    async def abs_progress(abs_client, item_id):
        return next(reads, {"lastUpdate": 7000, "ebookProgress": 0.5, "ebookLocation": CFI, "isFinished": False})
    monkeypatch.setattr(type(rw), "_abs_progress", staticmethod(abs_progress))
    mock_world(aioclient_mock, {DID: state(0.5, POINT, modified=2000)}); mock_put(aioclient_mock)
    assert (await rw.async_sync())["imported"] == [ITEM]
    assert reg(hass)["abs_seen"] == 7000                                          # read back right after our write
    summary = await rw.async_sync()                                               # ABS still at 7000, Tolino unchanged
    assert summary["exported"] == [] and summary["imported"] == [] and puts(aioclient_mock) == []


async def test_newer_abs_wins_a_conflict(hass, rw, aioclient_mock):
    mock_world(aioclient_mock, {DID: state(0.5, "OEBPS/a.xhtml#point(/1/4/2/1:0)", modified=2000)}, abs_progress=abs_state(8000, 0.4))
    mock_put(aioclient_mock)
    summary = await rw.async_sync()
    assert summary["exported"] == [ITEM] and patches(aioclient_mock) == [] and len(puts(aioclient_mock)) == 1


async def test_newer_tolino_wins_a_conflict(hass, rw, aioclient_mock):
    mock_world(aioclient_mock, {DID: state(0.5, POINT, modified=9000)}, abs_progress=abs_state(3000, 0.1)); mock_put(aioclient_mock)
    summary = await rw.async_sync()
    assert summary["imported"] == [ITEM] and puts(aioclient_mock) == []


async def test_finished_in_abs_marks_tolino_finished(hass, rw, aioclient_mock):
    mock_world(aioclient_mock, {DID: state(0.9, POINT, modified=1000)}, abs_progress=abs_state(5000, 1, None, finished=True))
    await hass.data[DOMAIN]["tolino_sent"].async_update(ITEM, progress_modified=1000)        # Tolino side already seen
    mock_put(aioclient_mock, finished=True)
    await rw.async_sync()
    assert puts(aioclient_mock) == [{"finished": True}]                                        # tag only, no bookmark
    assert reg(hass)["progress_finished"] is True


async def test_unfinished_in_abs_clears_the_tolino_flag(hass, rw, aioclient_mock):
    mock_world(aioclient_mock, {DID: state(1.0, POINT, modified=1000, finished=True)}, abs_progress=abs_state(5000, 0.3))
    await hass.data[DOMAIN]["tolino_sent"].async_update(ITEM, progress_modified=1000, progress_finished=True)
    mock_put(aioclient_mock, finished=False)
    await rw.async_sync()
    assert puts(aioclient_mock)[0]["finished"] is False and puts(aioclient_mock)[0]["position"] == POINT


async def test_trivial_differences_are_not_written(hass, rw, aioclient_mock):
    mock_world(aioclient_mock, {DID: state(0.401, POINT, modified=1000)}, abs_progress=abs_state(5000, 0.4))
    await hass.data[DOMAIN]["tolino_sent"].async_update(ITEM, progress_modified=1000)
    mock_put(aioclient_mock)
    summary = await rw.async_sync()
    assert summary["skipped"] == [ITEM] and puts(aioclient_mock) == [] and reg(hass)["abs_seen"] == 5000


async def test_abs_state_without_a_location_writes_no_bookmark(hass, rw, aioclient_mock):
    mock_world(aioclient_mock, {}, abs_progress={"lastUpdate": 5000, "ebookProgress": 0.4, "isFinished": False}); mock_put(aioclient_mock)
    summary = await rw.async_sync()
    assert summary["skipped"] == [ITEM] and puts(aioclient_mock) == []


async def test_unmappable_cfi_writes_no_bookmark(hass, rw, aioclient_mock):
    mock_world(aioclient_mock, {}, abs_progress=abs_state(cfi="epubcfi(/6/99!/4/2/1:0)")); mock_put(aioclient_mock)
    summary = await rw.async_sync()
    assert summary["skipped"] == [ITEM] and puts(aioclient_mock) == []


async def test_non_epub_books_never_get_a_bookmark(hass, rw, aioclient_mock):
    mock_world(aioclient_mock, {}, abs_progress=abs_state(), fmt="mobi"); mock_put(aioclient_mock)
    summary = await rw.async_sync()
    assert summary["skipped"] == [ITEM] and puts(aioclient_mock) == []
    assert not any(str(c[1]).endswith("/ebook") for c in aioclient_mock.mock_calls)


async def test_bridge_failure_is_retried_next_tick(hass, rw, aioclient_mock):
    mock_world(aioclient_mock, {}, abs_progress=abs_state()); mock_put(aioclient_mock, status=502)
    assert (await rw.async_sync())["exported"] == []
    assert "abs_seen" not in reg(hass)                                            # not marked as handled
    aioclient_mock.clear_requests(); mock_world(aioclient_mock, {}, abs_progress=abs_state()); mock_put(aioclient_mock)
    assert (await rw.async_sync())["exported"] == [ITEM]


async def test_books_nobody_opened_are_left_alone(hass, rw, aioclient_mock):
    mock_world(aioclient_mock, {})                                                # ABS 404 (no progress), Tolino has nothing
    mock_put(aioclient_mock)
    assert (await rw.async_sync())["exported"] == [] and puts(aioclient_mock) == []


async def test_read_only_mode_never_writes_to_tolino(hass, ro, aioclient_mock):
    mock_world(aioclient_mock, {}, abs_progress=abs_state()); mock_put(aioclient_mock)
    assert ro.write_enabled is False
    summary = await ro.async_sync()
    assert summary["exported"] == [] and puts(aioclient_mock) == []
    assert not any("/me/progress/" in str(c[1]) for c in aioclient_mock.mock_calls)        # didn't even look at ABS


async def test_read_only_mode_does_not_overwrite_a_newer_abs_state(hass, ro, aioclient_mock):
    mock_world(aioclient_mock, {DID: state(modified=2000)}, abs_progress=abs_state(9000))
    summary = await ro.async_sync()
    assert summary["skipped"] == [ITEM] and patches(aioclient_mock) == [] and puts(aioclient_mock) == []


async def test_first_contact_newest_wins(hass, rw, aioclient_mock):
    """Both sides already have a state and we've never synced: the newer one decides."""
    mock_world(aioclient_mock, {DID: state(0.2, POINT, modified=1000)}, abs_progress=abs_state(4000, 0.6)); mock_put(aioclient_mock)
    assert (await rw.async_sync())["exported"] == [ITEM]
    aioclient_mock.clear_requests()
    await hass.data[DOMAIN]["tolino_sent"].async_update(ITEM, progress_modified=0, abs_seen=0)
    mock_world(aioclient_mock, {DID: state(0.8, POINT, modified=6000)}, abs_progress=abs_state(4000, 0.6)); mock_put(aioclient_mock)
    assert (await rw.async_sync())["imported"] == [ITEM]


async def test_write_option_defaults_to_off(hass, monkeypatch):
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA, "tolino_url": BRIDGE, "tolino_token": "bt"},
                            subentries_data=[await admin_person(hass, tolino=True, sync_progress=True)])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id); await hass.async_block_till_done()
    assert hass.data[DOMAIN]["jobs"]["default"]["progress_sync"].enabled is True and hass.data[DOMAIN]["jobs"]["default"]["progress_sync"].write_enabled is False
