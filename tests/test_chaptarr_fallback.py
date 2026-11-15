"""Second-stage search: Chaptarr's own search for a wanted book stays empty -> ask Prowlarr directly with German
umlauts spelled out -> hand a strict match to NZBGet under Chaptarr's own watched category."""
import asyncio
import base64
from datetime import timedelta

import aiohttp
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.books.chaptarr_fallback import (
    ISSUE_UNREACHABLE,
    ChaptarrFallbackSearch,
    author_surname,
    best_release,
    release_matches,
    significant_words,
    transliterate,
)
from custom_components.books.const import (
    CONF_FALLBACK_SEARCH,
    CONF_NOTIFY_SERVICE,
    CONF_NZBGET_PASSWORD,
    CONF_NZBGET_URL,
    CONF_NZBGET_USERNAME,
    CONF_PROWLARR_API_KEY,
    CONF_PROWLARR_URL,
    DOMAIN,
    FALLBACK_MAX_ATTEMPTS,
)

from .conftest import CHAPTARR, ENTRY_DATA

PROWLARR = "http://prowlarr.test"
NZBGET = "http://nzbget.test"
JSON = {"Content-Type": "application/json"}

# Real titles captured live on 2026-11-13 while diagnosing this: Chaptarr's own search for "Benjamin Blümchen -
# Der Weihnachtstraum" (real ü) found nothing, Prowlarr directly found these once "ü" was spelled out as "ue".
# Used as fixtures so the filter is tested against the actual false positives/true hits, not invented ones.
REAL_TV_EPISODES = [
    {"title": "Benjamin.Blumchen.S02E25.German.1080p.RTLP.WEB-DL.AAC2.0.H.264-GLOTZE", "protocol": "usenet",
     "size": 900_000_000, "categories": []},
    {"title": "Benjamin.Blumchen.S01E26.German.1080p.RTLP.WEB-DL.AAC2.0.H.264-GLOTZE", "protocol": "usenet",
     "size": 900_000_000, "categories": []},
]
REAL_HITS = [
    {"title": "Benjamin Bluemchen-Folge 52 Der Weihnachtstraum-16BIT-44-KHZ-WEB-FLAC-2018-WALKMAN",
     "size": 308554012, "categories": [{"id": 3040, "name": "Audio/Lossless"}], "protocol": "usenet",
     "downloadUrl": f"{PROWLARR}/1/download?link=a", "fileName": "benjamin-2018.nzb"},
    {"title": "Benjamin Bluemchen-Folge 52 Der Weihnachtstraum-16BIT-44-KHZ-WEB-FLAC-2008-WALKMAN",
     "size": 397035905, "categories": [{"id": 3040, "name": "Audio/Lossless"}], "protocol": "usenet",
     "downloadUrl": f"{PROWLARR}/1/download?link=b", "fileName": "benjamin-2008.nzb"},
    {"title": "BENJAMIN BLUEMCHEN - 052 - Der Weihnachtstraum.mp3",
     "size": 24613006, "categories": [{"id": 3050, "name": "Audio/Other"}], "protocol": "usenet",
     "downloadUrl": f"{PROWLARR}/1/download?link=c", "fileName": "benjamin.mp3.nzb"},
]
WANTED_TITLE = "Benjamin Blümchen - Der Weihnachtstraum"
WANTED_WORDS = significant_words(WANTED_TITLE)
OLD_ENOUGH = (dt_util.utcnow() - timedelta(days=2)).isoformat()
WANTED_RECORD = {
    "localBookId": "1164", "title": WANTED_TITLE,
    "authorTitle": "donnelly, elfie Benjamin Blümchen - Der Weihnachtstraum",
    "monitored": True, "audiobookMonitored": True, "ebookMonitored": False, "added": OLD_ENOUGH,
}


def _entry_data(**overrides):
    return {
        **ENTRY_DATA,
        CONF_PROWLARR_URL: PROWLARR, CONF_PROWLARR_API_KEY: "prowlarr-key",
        CONF_NZBGET_URL: NZBGET, CONF_NZBGET_USERNAME: "nzb-user", CONF_NZBGET_PASSWORD: "nzb-pw",
        CONF_FALLBACK_SEARCH: True,
        **overrides,
    }


async def _setup(hass, **overrides) -> ChaptarrFallbackSearch:
    entry = MockConfigEntry(domain=DOMAIN, data=_entry_data(**overrides), title="Books")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN]["chaptarr_fallback"]


def _missing_world(aioclient_mock, records=(WANTED_RECORD,)):
    aioclient_mock.get(f"{CHAPTARR}/api/v1/wanted/missing", json={"records": list(records)}, headers=JSON)


def _downloadclient_world(aioclient_mock, audiobook="Audiobook", ebook="Ebook"):
    """Mirrors the real Chaptarr setup (verified live 2026-11-14): an enabled usenet NZBGet client whose
    audiobookCategory/ebookCategory fields are the ones Chaptarr's completed-download handling actually
    watches - a hidden, unused "musicCategory" field (a Lidarr-codebase leftover) is deliberately NOT what we
    read, so a test asserting the old fixed-default behavior would catch a regression back to it."""
    aioclient_mock.get(f"{CHAPTARR}/api/v1/downloadclient", json=[
        {"protocol": "usenet", "enable": True, "fields": [
            {"name": "audiobookCategory", "value": audiobook}, {"name": "ebookCategory", "value": ebook},
            {"name": "musicCategory", "value": "Chaptarr"},
        ]},
    ], headers=JSON)


def _nzb_posts(aioclient_mock):
    return [c for c in aioclient_mock.mock_calls if c[0] == "POST" and str(c[1]).startswith(NZBGET)]


# -- pure matching logic, exercised against the real recorded data -----------------------------------------

def test_transliterates_german_umlauts():
    assert transliterate("Benjamin Blümchen") == "Benjamin Bluemchen"
    assert transliterate("Müßiggänger") == "Muessiggaenger"


def test_the_real_tv_episodes_are_rejected_and_the_real_audio_releases_match():
    assert not any(release_matches(r, WANTED_WORDS) for r in REAL_TV_EPISODES)
    assert all(release_matches(r, WANTED_WORDS) for r in REAL_HITS)


def test_best_release_prefers_the_smallest_file_when_no_title_names_the_author():
    chosen = best_release(REAL_HITS, WANTED_WORDS, surname=author_surname(WANTED_RECORD))
    assert chosen["title"].endswith(".mp3")       # none of the 3 real titles mention "Donnelly" (the book author)


def test_author_surname_from_authortitle():
    assert author_surname(WANTED_RECORD) == "donnelly"
    assert author_surname({"authorTitle": "no comma in this one"}) is None


def test_mutation_without_the_episode_filter_a_false_positive_would_slip_through():
    """Every wanted word is present - only the S52E01-style marker must keep this out. If _EPISODE_RE's check is
    removed, this goes red."""
    trap = {"title": "Benjamin Bluemchen S52E01 Der Weihnachtstraum", "protocol": "usenet", "categories": []}
    assert all(word in trap["title"].lower() for word in WANTED_WORDS)        # the word-match alone would accept it
    assert not release_matches(trap, WANTED_WORDS)


def test_mutation_without_the_category_filter_an_unrelated_release_would_slip_through():
    """Same words again, but a category (PC software) no book/audiobook release would plausibly use. If
    category_ok's check is removed, this goes red."""
    trap = {"title": "Benjamin Bluemchen Der Weihnachtstraum Setup.exe", "protocol": "usenet",
            "categories": [{"id": 4050, "name": "PC/0day"}]}
    assert all(word in trap["title"].lower() for word in WANTED_WORDS)
    assert not release_matches(trap, WANTED_WORDS)


def test_give_up_after_max_attempts_and_not_before(hass):
    job = ChaptarrFallbackSearch(hass)
    summary = {"given_up": []}
    for _ in range(FALLBACK_MAX_ATTEMPTS - 1):
        job._give_up_or_retry("1164", "Der Weihnachtstraum", summary)
    assert job.state["tried"]["1164"]["attempts"] == FALLBACK_MAX_ATTEMPTS - 1
    assert "1164" not in job.state["failed"] and not summary["given_up"]
    job._give_up_or_retry("1164", "Der Weihnachtstraum", summary)
    assert "1164" not in job.state["tried"]
    assert job.state["failed"]["1164"]["title"] == "Der Weihnachtstraum"
    assert summary["given_up"] == ["Der Weihnachtstraum"]


# -- full run, mocked Chaptarr/Prowlarr/NZBGet ---------------------------------------------------------------

async def test_a_strict_match_is_handed_to_nzbget_under_chaptarrs_audiobook_category(hass, aioclient_mock):
    job = await _setup(hass)
    _missing_world(aioclient_mock)
    _downloadclient_world(aioclient_mock)
    aioclient_mock.get(f"{PROWLARR}/api/v1/search", json=REAL_HITS, headers=JSON)
    aioclient_mock.get(f"{PROWLARR}/1/download", content=b"fake nzb bytes")
    aioclient_mock.post(f"{NZBGET}/jsonrpc", json={"version": "1.1", "result": 555}, headers=JSON)

    summary = await job.async_tick()

    assert summary["grabbed"] == [WANTED_TITLE]
    assert job.state["done"]["1164"]["nzb_id"] == 555
    posts = _nzb_posts(aioclient_mock)
    assert len(posts) == 1
    params = posts[0][2]["params"]
    assert len(params) == 11
    assert params[0] == "benjamin.mp3.nzb"                                 # the smallest of the 3 real matches
    assert base64.b64decode(params[1]) == b"fake nzb bytes"
    # WANTED_RECORD is audiobookMonitored=True -> Chaptarr's "Audiobook" category, never the unused "Chaptarr"
    # legacy field (a real bug this guards against: see _downloadclient_world's docstring).
    assert params[2] == "Audiobook"
    assert params[6] == "books-fallback-1164"                              # DupeKey: a second line of defense


async def test_an_ebook_type_book_is_handed_to_nzbget_under_chaptarrs_ebook_category(hass, aioclient_mock):
    job = await _setup(hass)
    ebook_record = {**WANTED_RECORD, "audiobookMonitored": False, "ebookMonitored": True}
    _missing_world(aioclient_mock, records=[ebook_record])
    _downloadclient_world(aioclient_mock)
    aioclient_mock.get(f"{PROWLARR}/api/v1/search", json=REAL_HITS, headers=JSON)
    aioclient_mock.get(f"{PROWLARR}/1/download", content=b"fake nzb bytes")
    aioclient_mock.post(f"{NZBGET}/jsonrpc", json={"version": "1.1", "result": 555}, headers=JSON)

    summary = await job.async_tick()

    assert summary["grabbed"] == [WANTED_TITLE]
    params = _nzb_posts(aioclient_mock)[0][2]["params"]
    assert params[2] == "Ebook"


def _calls_to(aioclient_mock, base_url: str) -> int:
    return len([c for c in aioclient_mock.mock_calls if str(c[1]).startswith(base_url)])


async def test_a_second_run_does_not_grab_the_same_book_again(hass, aioclient_mock):
    job = await _setup(hass)
    _missing_world(aioclient_mock)
    _downloadclient_world(aioclient_mock)
    aioclient_mock.get(f"{PROWLARR}/api/v1/search", json=REAL_HITS, headers=JSON)
    aioclient_mock.get(f"{PROWLARR}/1/download", content=b"fake nzb bytes")
    aioclient_mock.post(f"{NZBGET}/jsonrpc", json={"version": "1.1", "result": 555}, headers=JSON)
    await job.async_tick()
    prowlarr_before, nzbget_before = _calls_to(aioclient_mock, PROWLARR), _calls_to(aioclient_mock, NZBGET)

    summary = await job.async_tick()                                       # re-checks Chaptarr's wanted list again, legitimately

    assert summary == {"grabbed": [], "given_up": []}
    assert _calls_to(aioclient_mock, PROWLARR) == prowlarr_before
    assert _calls_to(aioclient_mock, NZBGET) == nzbget_before               # neither was asked again for the same book


async def test_each_successful_grab_is_saved_immediately_not_batched_at_the_end(hass, aioclient_mock, monkeypatch):
    """Two candidates, both find a match. If the save after each grab were removed (batched only at the very end
    instead), a crash between the two grabs would lose the first one's result - it would be re-grabbed later even
    though NZBGet already has it. This goes red if that per-item save is removed."""
    job = await _setup(hass)
    second = {**WANTED_RECORD, "localBookId": "2000", "title": "Irgendein Anderes Buch",
              "authorTitle": "schmidt, hans Irgendein Anderes Buch"}
    _missing_world(aioclient_mock, records=[WANTED_RECORD, second])
    _downloadclient_world(aioclient_mock)

    async def search(method, url, data):
        from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse
        hits = REAL_HITS if "weihnachtstraum" in url.query.get("query", "").lower() else [
            {"title": "Irgendein.Anderes.Buch-SCAN", "protocol": "usenet", "categories": [], "size": 1000,
             "downloadUrl": f"{PROWLARR}/1/download?link=d", "fileName": "anderes.nzb"},
        ]
        return AiohttpClientMockResponse(method, url, json=hits)

    aioclient_mock.get(f"{PROWLARR}/api/v1/search", side_effect=search)
    aioclient_mock.get(f"{PROWLARR}/1/download", content=b"fake nzb bytes")
    aioclient_mock.post(f"{NZBGET}/jsonrpc", json={"version": "1.1", "result": 1}, headers=JSON)

    saved_sizes = []
    original_save = job._store.async_save

    async def spy(data):
        saved_sizes.append(len(data["done"]))
        return await original_save(data)

    monkeypatch.setattr(job._store, "async_save", spy)

    summary = await job.async_tick()

    assert sorted(summary["grabbed"]) == sorted([WANTED_TITLE, "Irgendein Anderes Buch"])
    assert saved_sizes.count(1) >= 1            # a save happened with only the first grab done, before the second


async def test_no_match_counts_as_a_try_and_notifies_only_once_exhausted(hass, aioclient_mock):
    job = await _setup(hass, **{CONF_NOTIFY_SERVICE: "notify.mgr"})
    msgs = async_mock_service(hass, "notify", "mgr")
    _missing_world(aioclient_mock)
    aioclient_mock.get(f"{PROWLARR}/api/v1/search", json=[], headers=JSON)
    # No downloadclient mock registered: a miss must never ask Chaptarr for its categories - a call would error
    # the test. (Also guards against fetching categories before knowing whether there is a hit at all.)

    await job.async_tick()

    assert job.state["tried"]["1164"]["attempts"] == 1
    assert not msgs


async def test_the_notification_names_the_grabbed_book(hass, aioclient_mock):
    job = await _setup(hass, **{CONF_NOTIFY_SERVICE: "notify.mgr"})
    msgs = async_mock_service(hass, "notify", "mgr")
    _missing_world(aioclient_mock)
    _downloadclient_world(aioclient_mock)
    aioclient_mock.get(f"{PROWLARR}/api/v1/search", json=REAL_HITS, headers=JSON)
    aioclient_mock.get(f"{PROWLARR}/1/download", content=b"fake nzb bytes")
    aioclient_mock.post(f"{NZBGET}/jsonrpc", json={"version": "1.1", "result": 555}, headers=JSON)

    await job.async_tick()
    await hass.async_block_till_done()

    assert len(msgs) == 1 and WANTED_TITLE in msgs[0].data["message"]


async def test_too_young_a_wish_is_not_tried_yet(hass, aioclient_mock):
    job = await _setup(hass)
    fresh = {**WANTED_RECORD, "added": dt_util.utcnow().isoformat()}
    _missing_world(aioclient_mock, records=[fresh])
    # no Prowlarr/NZBGet mocks registered at all - a call to either would error the test

    summary = await job.async_tick()

    assert summary == {"grabbed": [], "given_up": []}


async def test_repair_issue_appears_when_prowlarr_is_unreachable_and_clears_once_it_recovers(hass, aioclient_mock):
    job = await _setup(hass)
    _missing_world(aioclient_mock)
    aioclient_mock.get(f"{PROWLARR}/api/v1/search", exc=aiohttp.ClientConnectionError("down"))

    await job.async_tick()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREACHABLE) is not None

    aioclient_mock.clear_requests()
    _missing_world(aioclient_mock)
    aioclient_mock.get(f"{PROWLARR}/api/v1/search", json=[], headers=JSON)          # Prowlarr is back, just no match

    await job.async_tick()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREACHABLE) is None


async def test_disabled_without_the_toggle_even_with_full_credentials(hass, aioclient_mock):
    job = await _setup(hass, **{CONF_FALLBACK_SEARCH: False})
    assert job.enabled is False
    assert await job.async_tick() is None
    assert not aioclient_mock.mock_calls


async def test_disabled_without_nzbget_credentials(hass, aioclient_mock):
    job = await _setup(hass, **{CONF_NZBGET_USERNAME: ""})
    assert job.enabled is False


# -- check_now: triggered right after the add flow's own BookSearch comes back empty -------------------------

async def test_check_now_grabs_right_away_without_waiting_for_the_age_gate(hass, aioclient_mock):
    """The book was *just* added (added=now), which the hourly sweep's FALLBACK_MIN_AGE_SECONDS would skip -
    check_now must not care about that: Chaptarr's search just ran, so it already had its fair chance."""
    job = await _setup(hass)
    _downloadclient_world(aioclient_mock)
    aioclient_mock.get(f"{PROWLARR}/api/v1/search", json=REAL_HITS, headers=JSON)
    aioclient_mock.get(f"{PROWLARR}/1/download", content=b"fake nzb bytes")
    aioclient_mock.post(f"{NZBGET}/jsonrpc", json={"version": "1.1", "result": 555}, headers=JSON)

    result = await job.check_now("1164", WANTED_TITLE, "Elfie Donnelly")           # is_audiobook defaults True

    assert result == "grabbed"
    assert job.state["done"]["1164"]["nzb_id"] == 555
    posts = _nzb_posts(aioclient_mock)
    assert len(posts) == 1
    assert posts[0][2]["params"][2] == "Audiobook"


async def test_check_now_for_an_ebook_picks_the_ebook_category(hass, aioclient_mock):
    job = await _setup(hass)
    _downloadclient_world(aioclient_mock)
    aioclient_mock.get(f"{PROWLARR}/api/v1/search", json=REAL_HITS, headers=JSON)
    aioclient_mock.get(f"{PROWLARR}/1/download", content=b"fake nzb bytes")
    aioclient_mock.post(f"{NZBGET}/jsonrpc", json={"version": "1.1", "result": 555}, headers=JSON)

    result = await job.check_now("1164", WANTED_TITLE, "Elfie Donnelly", is_audiobook=False)

    assert result == "grabbed"
    assert _nzb_posts(aioclient_mock)[0][2]["params"][2] == "Ebook"


async def test_a_hit_whose_chaptarr_category_lookup_fails_flags_the_repair_issue(hass, aioclient_mock):
    """Mutation guard: if the categories lookup were not wrapped in the same try/except as the NZBGet append,
    an unreachable Chaptarr at grab time would raise instead of being treated like an unreachable Prowlarr/NZBGet
    (repair issue, no crash)."""
    job = await _setup(hass)
    aioclient_mock.get(f"{CHAPTARR}/api/v1/downloadclient", exc=aiohttp.ClientConnectionError("down"))
    aioclient_mock.get(f"{PROWLARR}/api/v1/search", json=REAL_HITS, headers=JSON)

    result = await job.check_now("1164", WANTED_TITLE)

    assert result == "error"
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREACHABLE) is not None
    assert not _nzb_posts(aioclient_mock)                                   # never reached NZBGet


async def test_check_now_counts_a_miss_as_a_try_so_the_hourly_sweep_waits_before_retrying(hass, aioclient_mock):
    job = await _setup(hass)
    aioclient_mock.get(f"{PROWLARR}/api/v1/search", json=[], headers=JSON)

    result = await job.check_now("1164", WANTED_TITLE)

    assert result == "no_match"
    assert job.state["tried"]["1164"]["attempts"] == 1


async def test_check_now_reports_disabled_without_doing_anything(hass, aioclient_mock):
    job = await _setup(hass, **{CONF_FALLBACK_SEARCH: False})

    result = await job.check_now("1164", WANTED_TITLE)

    assert result == "disabled"
    assert not aioclient_mock.mock_calls


async def test_check_now_skips_a_book_already_grabbed_or_given_up(hass, aioclient_mock):
    job = await _setup(hass)
    job.state["done"]["1164"] = {"title": WANTED_TITLE}
    job.state["failed"]["2000"] = {"title": "Anderes"}

    assert await job.check_now("1164", WANTED_TITLE) == "already_done"
    assert await job.check_now("2000", "Anderes") == "given_up"
    assert not aioclient_mock.mock_calls


async def test_check_now_flags_the_repair_issue_when_prowlarr_is_unreachable(hass, aioclient_mock):
    job = await _setup(hass)
    aioclient_mock.get(f"{PROWLARR}/api/v1/search", exc=aiohttp.ClientConnectionError("down"))

    result = await job.check_now("1164", WANTED_TITLE)

    assert result == "error"
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREACHABLE) is not None


async def test_check_now_waits_for_a_sweep_already_running_rather_than_racing_it(hass, aioclient_mock):
    """Both check_now and the hourly sweep mutate self.state through the same lock - check_now must block on it,
    not run alongside it."""
    job = await _setup(hass)
    aioclient_mock.get(f"{PROWLARR}/api/v1/search", json=[], headers=JSON)

    async with job._running:                        # simulate a sweep already holding the lock
        task = asyncio.ensure_future(job.check_now("1164", WANTED_TITLE))
        await asyncio.sleep(0)
        assert not task.done()                       # blocked on the same lock, not racing it

    assert await task == "no_match"
