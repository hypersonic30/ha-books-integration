"""Second-stage search: when Chaptarr's own search for a wanted book stays empty, try Prowlarr directly.

Chaptarr builds its search terms from the book's metadata (Goodreads/Hardcover), including real umlauts. Some
indexers spell a release differently (e.g. German "ü" transliterated as "ue" instead), so Chaptarr's own search
finds nothing even though the release exists. Every FALLBACK_INTERVAL_SECONDS, books that have stayed "wanted"
for a while are retried with the title transliterated (ä/ö/ü/ß -> ae/oe/ue/ss) against Prowlarr directly. A strict
match (all significant title words present, no "S01E02"-style episode naming, a plausible Newznab category) is
handed to NZBGet as a plain NZB under the category Chaptarr's own download client already watches
(`CONF_NZBGET_CATEGORY`), so Chaptarr's completed-download handling imports it exactly as if it had found it itself
- Chaptarr has no "push a release" API of its own.

Off by default (`CONF_FALLBACK_SEARCH`); needs Prowlarr and NZBGet credentials in the main settings. Mirrors
tolino_import.py's shape (Store-backed state, async_start/async_tick, bounded work per run) but is a single global
job, not one per Tolino account, so it is wired directly in __init__.py rather than through jobs.py."""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import ChaptarrClient, NzbgetClient, ProwlarrClient, UpstreamError, get_config
from .const import (
    CONF_FALLBACK_SEARCH,
    CONF_NZBGET_CATEGORY,
    CONF_VERIFY_SSL,
    DEFAULT_NZBGET_CATEGORY,
    DOMAIN,
    FALLBACK_MAX_ATTEMPTS,
    FALLBACK_MAX_PER_RUN,
    FALLBACK_MIN_AGE_SECONDS,
    FALLBACK_RETRY_SECONDS,
    FALLBACK_SCAN_LIMIT,
    NEWZNAB_ALLOWED_RANGES,
    SIGNAL_FALLBACK_UPDATED,
    SLOW_REQUEST_TIMEOUT,
)
from .notify_helper import async_push

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "books_chaptarr_fallback"
ISSUE_UNREACHABLE = "fallback_search_unreachable"
MAX_NZB_BYTES = 20 * 1024 * 1024          # an .nzb is metadata (file list), not the media itself - generous but not unbounded

_DIGRAPHS = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"}
_STOPWORDS = {"der", "die", "das", "des", "dem", "den", "und", "ein", "eine", "einer", "the", "a", "an", "of"}
_EPISODE_RE = re.compile(r"s\d{2}e\d{2}", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+")


def transliterate(text: str) -> str:
    for umlaut, plain in _DIGRAPHS.items():
        text = text.replace(umlaut, plain)
    return text


def significant_words(title: str) -> list[str]:
    words = _WORD_RE.findall(transliterate(title).lower())
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS]


def category_ok(categories) -> bool:
    """True unless the release names categories and none of them are plausible for a book/audiobook
    (keeps out TV/movie/software releases that only share words with the title)."""
    ids = [c.get("id") for c in (categories or []) if isinstance(c, dict) and isinstance(c.get("id"), int)]
    if not ids:
        return True
    return any(cid in rng for cid in ids for rng in NEWZNAB_ALLOWED_RANGES)


def release_matches(release: dict, wanted_words: list[str]) -> bool:
    if release.get("protocol") != "usenet" or not wanted_words:
        return False
    title = str(release.get("title") or "")
    if _EPISODE_RE.search(title):
        return False
    if not category_ok(release.get("categories")):
        return False
    low = title.lower()
    return all(word in low for word in wanted_words)


def author_surname(rec: dict) -> str | None:
    """Chaptarr's wanted/missing formats authorTitle as "surname, firstname booktitle" - cheaper than a second
    API call for the author. Used only as a tie-breaker, never a hard filter (real audio-drama releases often
    credit the publisher/label in the title, not the book's author)."""
    author_title = str(rec.get("authorTitle") or "")
    surname, _, rest = author_title.partition(",")
    return surname.strip().lower() if rest else None


def best_release(releases: list[dict], wanted_words: list[str], surname: str | None) -> dict | None:
    matching = [r for r in releases if isinstance(r, dict) and release_matches(r, wanted_words)]
    if not matching:
        return None

    def score(r: dict) -> tuple[int, int]:
        has_surname = bool(surname) and surname in str(r.get("title") or "").lower()
        return (0 if has_surname else 1, int(r.get("size") or 0))

    return min(matching, key=score)


async def _fetch_nzb_bytes(hass: HomeAssistant, url: str, verify_ssl: bool) -> bytes:
    """GET a Prowlarr download link directly (it is a full URL with its own apikey, a different host than our
    configured Prowlarr client base, and not JSON, so the shared `_Client` helper does not fit here)."""
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=SLOW_REQUEST_TIMEOUT)) as resp:
        if resp.status >= 400:
            raise UpstreamError(resp.status, (await resp.read())[:300].decode(errors="replace"))
        body = bytearray()
        async for chunk in resp.content.iter_chunked(256 * 1024):
            body += chunk
            if len(body) > MAX_NZB_BYTES:
                raise UpstreamError(413, "nzb too large")
        return bytes(body)


class ChaptarrFallbackSearch:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store = Store(hass, 1, STORAGE_KEY)
        self.state: dict = {"tried": {}, "done": {}, "failed": {}}
        self.last_run: dict | None = None
        self._running = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        cfg = get_config(self._hass)
        if not cfg.get(CONF_FALLBACK_SEARCH):
            return False
        return ProwlarrClient(self._hass, cfg).configured and NzbgetClient(self._hass, cfg).configured

    async def async_load(self) -> None:
        self.state.update(await self._store.async_load() or {})
        self.state.setdefault("tried", {})
        self.state.setdefault("done", {})
        self.state.setdefault("failed", {})

    async def async_tick(self, _now=None) -> dict | None:
        if not self.enabled or self._running.locked():
            return None
        async with self._running:
            try:
                return await self._run()
            except Exception:  # noqa: BLE001 - a background job must never take anything down
                _LOGGER.exception("books: Chaptarr fallback search failed")
                return None

    async def check_now(self, book_id: str, title: str, author_name: str = "") -> str:
        """Try one specific book right away - e.g. right after Chaptarr's own search for it (just triggered by
        the card's "add") came back empty - instead of waiting for the next hourly sweep and its
        FALLBACK_MIN_AGE_SECONDS gate (Chaptarr already just had its fair, immediate chance).

        Returns "disabled", "already_done", "given_up" (no point retrying), "grabbed", "no_match" or "error"."""
        book_id = str(book_id)
        if not self.enabled:
            return "disabled"
        if book_id in self.state["done"]:
            return "already_done"
        if book_id in self.state["failed"]:
            return "given_up"
        cfg = get_config(self._hass)
        prowlarr = ProwlarrClient(self._hass, cfg)
        nzbget = NzbgetClient(self._hass, cfg)
        category = cfg.get(CONF_NZBGET_CATEGORY) or DEFAULT_NZBGET_CATEGORY
        verify_ssl = cfg.get(CONF_VERIFY_SSL, True)
        surname = author_name.rsplit(" ", 1)[-1] if author_name.strip() else ""
        rec = {"localBookId": book_id, "title": title, "authorTitle": f"{surname}, {title}" if surname else ""}
        summary = {"grabbed": [], "given_up": []}
        try:
            async with self._running:              # never run alongside the hourly sweep: both touch self.state
                ok = await self._attempt(prowlarr, nzbget, category, verify_ssl, rec, summary)
                await self._store.async_save(self.state)
        except Exception:  # noqa: BLE001 - triggered from the add flow's background task, must never raise there
            _LOGGER.exception("books: immediate fallback search for '%s' failed", title)
            return "error"
        if ok:
            ir.async_delete_issue(self._hass, DOMAIN, ISSUE_UNREACHABLE)
        else:
            ir.async_create_issue(self._hass, DOMAIN, ISSUE_UNREACHABLE, is_fixable=False,
                                  severity=ir.IssueSeverity.WARNING, translation_key=ISSUE_UNREACHABLE)
        if summary["grabbed"]:
            await async_push(self._hass, "Ersatzsuche erfolgreich",
                             "Über Prowlarr gefunden, Chaptarr importiert jetzt: " + ", ".join(summary["grabbed"]))
        for given_up_title in summary["given_up"]:
            await async_push(self._hass, "Ersatzsuche aufgegeben",
                             f"„{given_up_title}“ wurde auch über Prowlarr nicht gefunden. Bitte von Hand prüfen.")
        if summary["grabbed"] or summary["given_up"]:
            self.last_run = {"at": dt_util.utcnow(), **summary}
            async_dispatcher_send(self._hass, SIGNAL_FALLBACK_UPDATED)
        if not ok:
            return "error"
        return "grabbed" if summary["grabbed"] else ("given_up" if summary["given_up"] else "no_match")

    # -- one run --------------------------------------------------------------------------------------------

    def _candidates(self, records: list[dict]) -> list[dict]:
        now = time.time()
        out = []
        for rec in records:
            book_id = str(rec.get("localBookId") or rec.get("id") or "")
            if not book_id or not rec.get("monitored"):
                continue
            if not (rec.get("audiobookMonitored") or rec.get("ebookMonitored")):
                continue
            if book_id in self.state["done"] or book_id in self.state["failed"]:
                continue
            added = dt_util.parse_datetime(str(rec.get("added") or ""))
            if added is None or now - added.timestamp() < FALLBACK_MIN_AGE_SECONDS:
                continue
            tried = self.state["tried"].get(book_id)
            if tried and now - float(tried.get("at") or 0) < FALLBACK_RETRY_SECONDS:
                continue
            out.append(rec)
        return out[:FALLBACK_MAX_PER_RUN]

    async def _attempt(
        self, prowlarr: ProwlarrClient, nzbget: NzbgetClient, category: str, verify_ssl: bool, rec: dict, summary: dict,
    ) -> bool:
        """One candidate: search Prowlarr, match strictly, grab a good hit. Shared by the hourly sweep and
        `check_now`. Returns False only when Prowlarr/NZBGet themselves could not be reached (the caller stops
        and flags the repair issue); True otherwise, whether that found a release, a non-match, or a give-up."""
        book_id = str(rec.get("localBookId") or rec.get("id"))
        title = str(rec.get("title") or "")
        words = significant_words(title)
        try:
            results = await prowlarr.get(
                "/search", params={"query": transliterate(title), "type": "search", "limit": 25},
                timeout=SLOW_REQUEST_TIMEOUT,
            )
        except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.debug("books: fallback search cannot reach Prowlarr for '%s': %s", title, exc)
            return False
        release = best_release(results or [], words, author_surname(rec))
        if release is None:
            self._give_up_or_retry(book_id, title, summary)
            return True
        try:
            content = await _fetch_nzb_bytes(self._hass, release["downloadUrl"], verify_ssl)
            nzb_id = await nzbget.append(
                filename=str(release.get("fileName") or f"{release.get('title', title)}.nzb"),
                content_b64=base64.b64encode(content).decode(),
                category=category,
                dupe_key=f"books-fallback-{book_id}",
            )
        except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("books: fallback search found a release for '%s' but NZBGet refused it: %s", title, exc)
            return False
        self.state["done"][book_id] = {
            "title": title, "release": release.get("title"), "nzb_id": nzb_id, "at": dt_util.utcnow().isoformat(),
        }
        self.state["tried"].pop(book_id, None)
        # Saved right away, not batched for the end of the run: a 300+ MB import can take a while, and a restart
        # before this run finishes must not re-grab a book that already succeeded.
        await self._store.async_save(self.state)
        _LOGGER.info("books: fallback search found '%s' via Prowlarr, handed to NZBGet (id %s)", title, nzb_id)
        summary["grabbed"].append(title)
        return True

    async def _run(self) -> dict:
        cfg = get_config(self._hass)
        chaptarr = ChaptarrClient(self._hass, cfg)
        prowlarr = ProwlarrClient(self._hass, cfg)
        nzbget = NzbgetClient(self._hass, cfg)
        category = cfg.get(CONF_NZBGET_CATEGORY) or DEFAULT_NZBGET_CATEGORY
        verify_ssl = cfg.get(CONF_VERIFY_SSL, True)
        summary = {"grabbed": [], "given_up": []}
        try:
            missing = await chaptarr.get("/wanted/missing", params={
                "page": 1, "pageSize": FALLBACK_SCAN_LIMIT, "sortKey": "added", "sortDir": "descending"})
        except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.debug("books: fallback search cannot read Chaptarr's wanted list: %s", exc)
            return summary
        candidates = self._candidates((missing or {}).get("records") or [])
        if not candidates:
            return summary
        unreachable = False
        for rec in candidates:
            if not await self._attempt(prowlarr, nzbget, category, verify_ssl, rec, summary):
                unreachable = True
                break
        if unreachable:
            ir.async_create_issue(
                self._hass, DOMAIN, ISSUE_UNREACHABLE, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_UNREACHABLE,
            )
        else:
            ir.async_delete_issue(self._hass, DOMAIN, ISSUE_UNREACHABLE)
        await self._store.async_save(self.state)
        if summary["grabbed"]:
            await async_push(
                self._hass, "Ersatzsuche erfolgreich",
                "Über Prowlarr gefunden, Chaptarr importiert jetzt: " + ", ".join(summary["grabbed"]),
            )
        for title in summary["given_up"]:
            await async_push(
                self._hass, "Ersatzsuche aufgegeben",
                f"„{title}“ wurde auch über Prowlarr nicht gefunden. Bitte von Hand prüfen.",
            )
        self.last_run = {"at": dt_util.utcnow(), **summary}       # a datetime, not a string: feeds the timestamp sensor directly
        async_dispatcher_send(self._hass, SIGNAL_FALLBACK_UPDATED)
        return summary

    def _give_up_or_retry(self, book_id: str, title: str, summary: dict) -> None:
        attempts = int((self.state["tried"].get(book_id) or {}).get("attempts") or 0) + 1
        if attempts >= FALLBACK_MAX_ATTEMPTS:
            self.state["failed"][book_id] = {"title": title, "at": dt_util.utcnow().isoformat()}
            self.state["tried"].pop(book_id, None)
            summary["given_up"].append(title)
        else:
            self.state["tried"][book_id] = {"at": time.time(), "attempts": attempts}
