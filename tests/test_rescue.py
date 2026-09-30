"""Import rescue: parsing and the full repair flow against a mocked Chaptarr."""
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.books.const import DOMAIN
from custom_components.books.rescue import (
    allowed_quality_names,
    parse_mismatch,
    pick_edition,
    pick_files,
)

from .conftest import CHAPTARR

# Exact format of Chaptarr's rejection message (fictional titles/IDs).
REAL_MSG = (
    "Rejected: Completed download was grabbed for 'The Storm' (BookId 107, work hc:1000002), "
    "but import matched 'Chronicles of Alsea' (BookId 105, work hc:1000001)."
)
API = f"{CHAPTARR}/api/v1"


def _record(messages, book_id=107, state="importBlocked", download_id="dl1"):
    return {
        "title": "Erika.Muster.-.Die.Chroniken.von.Alsea.02.-.Der.Sturm",
        "downloadId": download_id,
        "bookId": book_id,
        "trackedDownloadState": state,
        "statusMessages": [{"title": "file.epub", "messages": messages}],
    }


def test_parse_real_message():
    assert parse_mismatch(_record([REAL_MSG])) == (107, 105)


def test_parse_same_message_for_several_files():
    assert parse_mismatch(_record([REAL_MSG, REAL_MSG, REAL_MSG])) == (107, 105)


def test_parse_rejects_any_other_reason():
    assert parse_mismatch(_record([REAL_MSG, "Rejected: Not an upgrade for existing book file(s)"])) is None
    assert parse_mismatch(_record(["No supported audio or ebook files were found in /data/x"])) is None
    assert parse_mismatch(_record([])) is None


def test_parse_rejects_grab_for_several_books():
    msg = ("Rejected: Completed download was grabbed for 'A' (BookId 1, work hc:1), 'B' (BookId 2, work hc:2), "
           "but import matched 'C' (BookId 3, work hc:3).")
    assert parse_mismatch(_record([msg], book_id=1)) is None


def test_parse_rejects_queue_book_disagreeing_with_message():
    assert parse_mismatch(_record([REAL_MSG], book_id=999)) is None


def test_pick_edition_prefers_monitored_ebook():
    editions = [
        {"id": 1, "isEbook": False, "monitored": True},
        {"id": 2, "isEbook": True, "monitored": False},
        {"id": 3, "isEbook": True, "monitored": True},
    ]
    assert pick_edition(editions, "ebook")["id"] == 3
    assert pick_edition(editions, "audiobook")["id"] == 1
    assert pick_edition([{"id": 9, "isEbook": False}], "ebook") is None


def _cand(name, quality):
    return {"path": f"/dl/{name}", "quality": {"quality": {"name": quality}}}


def test_pick_files_ebook_takes_single_best_allowed():
    cands = [_cand("a.azw3", "AZW3"), _cand("a.mobi", "MOBI"), _cand("a.epub", "EPUB"), _cand("a.pdf", "PDF")]
    assert [f["path"] for f in pick_files(cands, "ebook", {"EPUB", "Unknown Text"})] == ["/dl/a.epub"]
    assert pick_files([_cand("a.mobi", "MOBI")], "ebook", {"EPUB"}) == []


def test_pick_files_audiobook_takes_all_allowed_tracks():
    cands = [_cand("01.mp3", "MP3"), _cand("02.mp3", "MP3"), _cand("cover.nfo", "Unknown Text")]
    assert len(pick_files(cands, "audiobook", {"MP3", "M4B"})) == 2


def test_allowed_quality_names_walks_groups():
    profile = {"items": [
        {"quality": {"name": "PDF"}, "allowed": False},
        {"quality": {"name": "EPUB"}, "allowed": True},
        {"name": "group", "allowed": True, "items": [{"quality": {"name": "MP3"}}, {"quality": {"name": "M4B"}}]},
    ]}
    assert allowed_quality_names(profile) == {"EPUB", "MP3", "M4B"}


def _mock_chaptarr(aioclient_mock, *, target_author=3, target_files_after=1, command_status="completed"):
    calls = {"book107": 0}

    async def book107(method, url, data):
        calls["book107"] += 1
        files = 0 if calls["book107"] == 1 else target_files_after
        from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse
        return AiohttpClientMockResponse(method, url, json={
            "id": 107, "title": "Der Sturm", "authorId": target_author,
            "mediaType": "ebook", "statistics": {"bookFileCount": files},
        })

    aioclient_mock.get(f"{API}/queue", json={"records": [_record([REAL_MSG])]})
    aioclient_mock.get(f"{API}/book/107", side_effect=book107)
    aioclient_mock.get(f"{API}/book/105", json={"id": 105, "title": "Die Chroniken von Alsea", "authorId": 3,
                                                "mediaType": "ebook", "statistics": {"bookFileCount": 1}})
    aioclient_mock.get(f"{API}/edition", json=[{"id": 656, "isEbook": True, "monitored": True},
                                               {"id": 700, "isEbook": False, "monitored": True}])
    aioclient_mock.get(f"{API}/author/3", json={"id": 3, "ebookQualityProfileId": 1})
    aioclient_mock.get(f"{API}/qualityprofile/1", json={"items": [
        {"quality": {"name": "EPUB"}, "allowed": True}, {"quality": {"name": "Unknown Text"}, "allowed": True},
        {"quality": {"name": "AZW3"}, "allowed": False}]})
    aioclient_mock.get(f"{API}/manualimport", json=[
        {"path": "/dl/x.azw3", "quality": {"quality": {"name": "AZW3"}}, "downloadId": "dl1"},
        {"path": "/dl/x.epub", "quality": {"quality": {"name": "EPUB"}}, "downloadId": "dl1"}])
    aioclient_mock.post(f"{API}/command", json={"id": 55})
    aioclient_mock.get(f"{API}/command/55", json={"id": 55, "status": command_status})
    return calls


async def test_rescue_imports_to_grabbed_book(hass, setup_entry, aioclient_mock):
    _mock_chaptarr(aioclient_mock)
    rescue = hass.data[DOMAIN]["rescue"]
    await rescue.async_tick()

    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert len(posts) == 1
    body = posts[0][2]
    assert body["name"] == "ManualImport"
    assert body["replaceExistingFiles"] is False
    assert [(f["path"], f["bookId"], f["editionId"], f["authorId"]) for f in body["files"]] == [
        ("/dl/x.epub", 107, 656, 3)
    ]
    assert rescue.events[0]["kind"] == "rescued"

    # Second tick: same download is never touched again.
    await rescue.async_tick()
    assert len([c for c in aioclient_mock.mock_calls if c[0] == "POST"]) == 1


async def test_rescue_leaves_other_authors_alone(hass, setup_entry, aioclient_mock):
    _mock_chaptarr(aioclient_mock, target_author=99)
    rescue = hass.data[DOMAIN]["rescue"]
    await rescue.async_tick()
    assert not [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert rescue.events[0]["kind"] == "skipped"


async def test_rescue_failure_creates_notification(hass, setup_entry, aioclient_mock):
    _mock_chaptarr(aioclient_mock, target_files_after=0)
    rescue = hass.data[DOMAIN]["rescue"]
    with patch("custom_components.books.rescue.persistent_notification.async_create") as notify:
        await rescue.async_tick()
    assert rescue.events[0]["kind"] == "failed"
    notify.assert_called_once()
    assert "Der Sturm" in notify.call_args.args[1]


async def test_rescue_failure_calls_configured_notify_service(hass, entry, aioclient_mock):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "notify_service": "notify.mobile_app_iphone"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    calls = async_mock_service(hass, "notify", "mobile_app_iphone")
    _mock_chaptarr(aioclient_mock, target_files_after=0)
    await hass.data[DOMAIN]["rescue"].async_tick()
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert "Der Sturm" in calls[0].data["message"]


async def test_rescue_disabled_does_nothing(hass, entry, aioclient_mock):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "rescue_imports": False})
    assert await hass.config_entries.async_setup(entry.entry_id)
    _mock_chaptarr(aioclient_mock)
    await hass.data[DOMAIN]["rescue"].async_tick()
    assert aioclient_mock.call_count == 0
