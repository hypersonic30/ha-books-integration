"""Reading progress tolino -> Audiobookshelf: CFI mapping, newest-wins, idempotence, failure handling, opt-in."""
import io
import zipfile

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.books.const import DOMAIN
from custom_components.books.tolino_sync import spine_hrefs, tolino_to_cfi

from .conftest import ABS, ENTRY_DATA

BRIDGE = "http://bridge.test:8199"
JSON = {"Content-Type": "application/json"}
ITEM = "abc123"
DID = "bosh_3_1"


def make_epub(spine=("a.xhtml", "b.xhtml", "c d.xhtml"), opf_dir="OEBPS"):
    manifest = "".join(f'<item id="i{n}" href="{h.replace(" ", "%20")}" media-type="application/xhtml+xml"/>' for n, h in enumerate(spine))
    manifest += '<item id="img" href="cover.jpg" media-type="image/jpeg"/>'
    itemrefs = "".join(f'<itemref idref="i{n}"/>' for n in range(len(spine)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", f'<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="{opf_dir}/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr(f"{opf_dir}/content.opf", f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0"><manifest>{manifest}</manifest><spine>{itemrefs}</spine></package>')
    return buf.getvalue()


# --- pure functions -------------------------------------------------------------------

def test_spine_hrefs_are_zip_root_relative_and_decoded():
    assert spine_hrefs(make_epub()) == ["OEBPS/a.xhtml", "OEBPS/b.xhtml", "OEBPS/c d.xhtml"]
    assert spine_hrefs(make_epub(opf_dir="OPS/text")) == ["OPS/text/a.xhtml", "OPS/text/b.xhtml", "OPS/text/c d.xhtml"]


HREFS = ["OEBPS/a.xhtml", "OEBPS/b.xhtml", "OEBPS/c d.xhtml"]


@pytest.mark.parametrize("position,expected", [
    ("OEBPS/a.xhtml#point(/1/4/230/1:138)", "epubcfi(/6/2!/4/230/1:0)"),       # text node: keep a (zero) offset
    ("OEBPS/b.xhtml#point(/1/4/10/5:1)", "epubcfi(/6/4!/4/10/5:0)"),
    ("OEBPS/c d.xhtml#point(/1/2/2/18/2/1:0)", "epubcfi(/6/6!/2/2/18/2/1:0)"),
    ("OEBPS/c%20d.xhtml#point(/1/4/2:0)", "epubcfi(/6/6!/4/2)"),                # element step: no offset, %20 matches
    ("./OEBPS/b.xhtml#point(/1/4/4:0)", "epubcfi(/6/4!/4/4)"),
])
def test_tolino_position_to_cfi(position, expected):
    assert tolino_to_cfi(position, HREFS) == expected


@pytest.mark.parametrize("position", [None, "", "OEBPS/a.xhtml", "OEBPS/zzz.xhtml#point(/1/4/2:0)", "OEBPS/a.xhtml#point(/4/2:0)",
                                      "OEBPS/a.xhtml#point(/1/x/2:0)", "OEBPS/a.xhtml#point()"])
def test_unmappable_positions_give_none(position):
    assert tolino_to_cfi(position, HREFS) is None


# --- the sync job ----------------------------------------------------------------------

@pytest.fixture
async def synced(hass, monkeypatch):
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA, "tolino_url": BRIDGE, "tolino_token": "bt", "sync_progress": True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.data[DOMAIN]["tolino_sent"].async_set(ITEM, DID, "x.epub")
    return hass.data[DOMAIN]["progress_sync"]


def state(progress=0.5, pos="OEBPS/b.xhtml#point(/1/4/2/1:9)", modified=2000, finished=False):
    return {"progress": progress, "position": pos, "modified": modified, "page": "5", "pages": "10", "finished": finished}


def mock_world(aioclient_mock, books, abs_progress=None, fmt="epub", epub=None, patch_status=200):
    aioclient_mock.get(f"{BRIDGE}/progress", json={"books": books}, headers=JSON)
    if abs_progress is None:
        aioclient_mock.get(f"{ABS}/api/me/progress/{ITEM}", status=404, json={"error": "not found"}, headers=JSON)
    else:
        aioclient_mock.get(f"{ABS}/api/me/progress/{ITEM}", json=abs_progress, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/items/{ITEM}", json={"media": {"ebookFile": {"ebookFormat": fmt}}}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/items/{ITEM}/ebook", content=epub or make_epub())
    aioclient_mock.patch(f"{ABS}/api/me/progress/{ITEM}", status=patch_status, json={"success": patch_status == 200}, headers=JSON)


def patches(aioclient_mock):
    return [c[2] for c in aioclient_mock.mock_calls if c[0] == "PATCH"]


async def test_imports_position_and_progress(hass, synced, aioclient_mock):
    mock_world(aioclient_mock, {DID: state()})
    summary = await synced.async_sync()
    assert summary["imported"] == [ITEM] and summary["skipped"] == []
    assert patches(aioclient_mock) == [{"isFinished": False, "ebookProgress": 0.5, "ebookLocation": "epubcfi(/6/4!/4/2/1:0)"}]
    sent = hass.data[DOMAIN]["tolino_sent"].get(ITEM)
    assert sent["progress_modified"] == 2000 and sent["progress_finished"] is False
    assert [c for c in aioclient_mock.mock_calls if c[0] == "PATCH"][0][3]["Authorization"] == "Bearer abs-token"


async def test_second_run_without_news_writes_nothing(hass, synced, aioclient_mock):
    mock_world(aioclient_mock, {DID: state()})
    await synced.async_sync(); await synced.async_sync()
    assert len(patches(aioclient_mock)) == 1
    assert sum(1 for c in aioclient_mock.mock_calls if str(c[1]).endswith("/ebook")) == 1     # spine cached too


async def test_newer_tolino_state_is_imported_again(hass, synced, aioclient_mock):
    mock_world(aioclient_mock, {DID: state(0.5, modified=2000)})
    await synced.async_sync()
    aioclient_mock.clear_requests(); mock_world(aioclient_mock, {DID: state(0.7, "OEBPS/c d.xhtml#point(/1/4/6/1:0)", 5000)})
    await synced.async_sync()
    assert patches(aioclient_mock)[-1]["ebookProgress"] == 0.7 and patches(aioclient_mock)[-1]["ebookLocation"] == "epubcfi(/6/6!/4/6/1:0)"


async def test_newer_audiobookshelf_state_is_never_overwritten(hass, synced, aioclient_mock):
    mock_world(aioclient_mock, {DID: state(modified=2000)}, abs_progress={"lastUpdate": 9000, "ebookProgress": 0.9})
    summary = await synced.async_sync()
    assert summary["skipped"] == [ITEM] and patches(aioclient_mock) == []
    await synced.async_sync()                                                   # and it is not reconsidered every tick
    assert sum(1 for c in aioclient_mock.mock_calls if "/me/progress/" in str(c[1]) and c[0] == "GET") == 1


async def test_finished_book(hass, synced, aioclient_mock):
    mock_world(aioclient_mock, {DID: state(1.0, "OEBPS/c d.xhtml#point(/1/4/2:0)", 3000, finished=True)})
    await synced.async_sync()
    assert patches(aioclient_mock) == [{"isFinished": True, "ebookProgress": 1}]        # no location for a finished book


async def test_finished_tag_without_any_bookmark(hass, synced, aioclient_mock):
    mock_world(aioclient_mock, {DID: {"progress": None, "position": None, "modified": 0, "finished": True}})
    await synced.async_sync()
    assert patches(aioclient_mock) == [{"isFinished": True, "ebookProgress": 1}]


async def test_reading_again_after_finished_clears_the_flag(hass, synced, aioclient_mock):
    mock_world(aioclient_mock, {DID: state(1.0, None, 3000, finished=True)})
    await synced.async_sync()
    aioclient_mock.clear_requests(); mock_world(aioclient_mock, {DID: state(0.05, "OEBPS/a.xhtml#point(/1/4/2/1:0)", 6000, finished=False)})
    await synced.async_sync()
    assert patches(aioclient_mock)[-1]["isFinished"] is False and patches(aioclient_mock)[-1]["ebookProgress"] == 0.05


async def test_non_epub_books_get_progress_but_no_location(hass, synced, aioclient_mock):
    mock_world(aioclient_mock, {DID: state()}, fmt="mobi")
    await synced.async_sync()
    assert patches(aioclient_mock) == [{"isFinished": False, "ebookProgress": 0.5}]
    assert not any(str(c[1]).endswith("/ebook") for c in aioclient_mock.mock_calls)     # never downloads a MOBI to parse it


async def test_unmappable_position_still_imports_progress(hass, synced, aioclient_mock):
    mock_world(aioclient_mock, {DID: state(0.3, "OEBPS/unknown.xhtml#point(/1/4/2:0)")})
    await synced.async_sync()
    assert patches(aioclient_mock) == [{"isFinished": False, "ebookProgress": 0.3}]


async def test_broken_epub_does_not_block_the_progress_import_of_others(hass, synced, aioclient_mock):
    mock_world(aioclient_mock, {DID: state()}, epub=b"not a zip")
    summary = await synced.async_sync()
    assert summary["imported"] == [] and patches(aioclient_mock) == []                    # retried next time, nothing half-written
    assert "progress_modified" not in hass.data[DOMAIN]["tolino_sent"].get(ITEM)


async def test_abs_write_failure_is_retried(hass, synced, aioclient_mock):
    mock_world(aioclient_mock, {DID: state()}, patch_status=500)
    assert (await synced.async_sync())["imported"] == []
    assert "progress_modified" not in hass.data[DOMAIN]["tolino_sent"].get(ITEM)
    aioclient_mock.clear_requests(); mock_world(aioclient_mock, {DID: state()})
    assert (await synced.async_sync())["imported"] == [ITEM]


async def test_bridge_down_is_quiet(hass, synced, aioclient_mock):
    aioclient_mock.get(f"{BRIDGE}/progress", exc=aiohttp.ClientConnectionError("down"))
    assert await synced.async_sync() == {"checked": 0, "imported": [], "skipped": []}
    assert patches(aioclient_mock) == []


async def test_books_not_sent_through_the_integration_are_ignored(hass, synced, aioclient_mock):
    mock_world(aioclient_mock, {"bosh_3_OTHER": state(), DID: state()})
    summary = await synced.async_sync()
    assert summary["checked"] == 1 and summary["imported"] == [ITEM]


async def test_tick_swallows_unexpected_errors(hass, synced, monkeypatch):
    async def boom(self):
        raise RuntimeError("boom")
    monkeypatch.setattr(type(synced), "async_sync", boom)
    assert await synced.async_tick() is None                                              # no exception escapes the timer


# --- opt-in & endpoint ------------------------------------------------------------------

async def test_off_by_default(hass, aioclient_mock, monkeypatch):
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA, "tolino_url": BRIDGE, "tolino_token": "bt"})   # no sync_progress key
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id); await hass.async_block_till_done()
    sync = hass.data[DOMAIN]["progress_sync"]
    assert sync.enabled is False and await sync.async_tick() is None
    assert aioclient_mock.call_count == 0


async def test_needs_a_bridge_even_when_switched_on(hass, monkeypatch):
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA, "sync_progress": True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id); await hass.async_block_till_done()
    assert hass.data[DOMAIN]["progress_sync"].enabled is False


async def test_manual_endpoint(hass, synced, hass_client, hass_client_no_auth, aioclient_mock):
    mock_world(aioclient_mock, {DID: state()})
    assert (await (await hass_client_no_auth()).post("/api/books/tolino-sync")).status == 401
    resp = await (await hass_client()).post("/api/books/tolino-sync")
    assert resp.status == 200 and (await resp.json())["imported"] == [ITEM]


async def test_manual_endpoint_refuses_when_off(hass, hass_client, setup_entry):
    resp = await (await hass_client()).post("/api/books/tolino-sync")
    assert resp.status == 409 and (await resp.json())["code"] == "sync_disabled"
