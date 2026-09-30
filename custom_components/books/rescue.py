"""Automatic repair of Chaptarr imports blocked by a same-author title mix-up.

German editions often name the series like book 1 ("Die Chroniken von Alsea"),
so a download for book 2 named "Die Chroniken von Alsea 02 - Der Sturm" is
parsed as book 1. Chaptarr then refuses with "Completed download
was grabbed for <book 2> (BookId X), but import matched <book 1> (BookId Y)".
Chaptarr already knows the right answer — the book it grabbed the release for —
so this module performs the manual import a human would do, but only when it
is unambiguous:

* the ONLY rejection reason is exactly that grabbed-vs-matched mismatch,
* both books belong to the same author and have the same media type,
* the grabbed book has no file yet and has an edition of that media type,
* the download contains files the book's quality profile allows.

Everything else is left alone. Each download gets one attempt; a failed
attempt notifies the admin instead of retrying forever.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
import logging
import re
import time

import aiohttp

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant

from .api import ChaptarrClient, UpstreamError, get_config
from .const import CONF_NOTIFY_SERVICE, CONF_RESCUE_IMPORTS, DEFAULT_RESCUE_IMPORTS

_LOGGER = logging.getLogger(__name__)

MISMATCH_RE = re.compile(
    r"^Rejected: Completed download was grabbed for .*?\(BookId (?P<grabbed>\d+)[,)].*?"
    r"but import matched .*?\(BookId (?P<matched>\d+)[,)]",
    re.DOTALL,
)
TEXT_QUALITIES_PREFERENCE = ("EPUB", "AZW3", "MOBI", "PDF", "Unknown Text")
COMMAND_POLL_SECONDS = 2
COMMAND_TIMEOUT_SECONDS = 180


def parse_mismatch(record: dict) -> tuple[int, int] | None:
    """(grabbed_book_id, matched_book_id) if every rejection is the mismatch, else None."""
    messages = [
        msg
        for status in record.get("statusMessages") or []
        for msg in status.get("messages") or []
    ]
    if not messages:
        return None
    pairs = set()
    for msg in messages:
        match = MISMATCH_RE.match(msg.strip())
        if match is None:
            return None
        grabbed_part, _, matched_part = msg.partition("but import matched")
        if grabbed_part.count("BookId") != 1 or matched_part.count("BookId") != 1:
            return None  # grabbed for several books at once — ambiguous, leave it to a human
        pairs.add((int(match["grabbed"]), int(match["matched"])))
    if len(pairs) != 1:
        return None
    grabbed, matched = pairs.pop()
    if grabbed == matched or record.get("bookId") not in (None, grabbed):
        return None
    return grabbed, matched


def pick_edition(editions: list[dict], media_type: str) -> dict | None:
    """The grabbed book's monitored edition of the right media type."""
    def fits(edition: dict) -> bool:
        is_ebook = bool(edition.get("isEbook"))
        return is_ebook if media_type == "ebook" else not is_ebook

    candidates = [e for e in editions if fits(e)]
    monitored = [e for e in candidates if e.get("monitored")]
    return (monitored or candidates or [None])[0]


def pick_files(candidates: list[dict], media_type: str, allowed_qualities: set[str]) -> list[dict]:
    """Files to import: the single best allowed text file, or every allowed audio file."""
    allowed = [
        c for c in candidates
        if ((c.get("quality") or {}).get("quality") or {}).get("name") in allowed_qualities
    ]
    if media_type == "audiobook":
        return allowed
    for quality in TEXT_QUALITIES_PREFERENCE:
        for candidate in allowed:
            if candidate["quality"]["quality"]["name"] == quality:
                return [candidate]
    return allowed[:1]


def allowed_quality_names(profile: dict) -> set[str]:
    names: set[str] = set()

    def walk(items: list[dict]) -> None:
        for item in items:
            if item.get("items"):
                if item.get("allowed"):
                    names.update(sub["quality"]["name"] for sub in item["items"] if sub.get("quality"))
                walk(item["items"])
            elif item.get("allowed") and item.get("quality"):
                names.add(item["quality"]["name"])

    walk(profile.get("items") or [])
    return names


class ImportRescue:
    """Polls Chaptarr's queue and repairs unambiguous grabbed-vs-matched blocks."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._attempted: set[str] = set()
        self._lock = asyncio.Lock()
        self.in_progress: set[str] = set()
        self.events: deque[dict] = deque(maxlen=30)

    @property
    def enabled(self) -> bool:
        return bool(get_config(self._hass).get(CONF_RESCUE_IMPORTS, DEFAULT_RESCUE_IMPORTS))

    async def async_tick(self, _now=None) -> None:
        if not self.enabled or self._lock.locked():
            return
        async with self._lock:
            client = ChaptarrClient(self._hass, get_config(self._hass))
            try:
                queue = await client.get("/queue", params={"page": 1, "pageSize": 100})
            except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.debug("books rescue: queue unavailable: %s", exc)
                return
            for record in (queue or {}).get("records", []):
                download_id = record.get("downloadId")
                if (
                    not download_id
                    or download_id in self._attempted
                    or record.get("trackedDownloadState") != "importBlocked"
                ):
                    continue
                ids = parse_mismatch(record)
                if ids is None:
                    continue
                self._attempted.add(download_id)
                self.in_progress.add(download_id)
                try:
                    await self._rescue(client, record, *ids)
                finally:
                    self.in_progress.discard(download_id)

    async def _rescue(self, client: ChaptarrClient, record: dict, grabbed_id: int, matched_id: int) -> None:
        title = record.get("title", "?")
        try:
            book = await client.get(f"/book/{grabbed_id}")
            matched = await client.get(f"/book/{matched_id}")
            if book.get("authorId") != matched.get("authorId"):
                return self._skip(title, "different authors")
            media_type = book.get("mediaType")
            if media_type not in ("ebook", "audiobook") or matched.get("mediaType") != media_type:
                return self._skip(title, "media type mismatch")
            if (book.get("statistics") or {}).get("bookFileCount"):
                return self._skip(title, f"'{book.get('title')}' already has a file")

            edition = pick_edition(await client.get("/edition", params={"bookId": grabbed_id}), media_type)
            if edition is None:
                return await self._fail(title, book, f"no {media_type} edition for this book")

            author = await client.get(f"/author/{book['authorId']}")
            profile_id = author.get(f"{media_type}QualityProfileId")
            profile = await client.get(f"/qualityprofile/{profile_id}") if profile_id else {}
            allowed = allowed_quality_names(profile)

            candidates = await client.get(
                "/manualimport",
                params={"downloadId": record["downloadId"], "filterExistingFiles": "false"},
                timeout=60,
            )
            files = pick_files(candidates or [], media_type, allowed)
            if not files:
                return await self._fail(title, book, "the download has no file the quality profile allows")

            command = await client.post("/command", {
                "name": "ManualImport",
                "importMode": "auto",
                "replaceExistingFiles": False,
                "files": [{
                    "path": f["path"],
                    "authorId": book["authorId"],
                    "bookId": grabbed_id,
                    "editionId": edition["id"],
                    "quality": f["quality"],
                    "indexerFlags": f.get("indexerFlags", 0),
                    "downloadId": record["downloadId"],
                    "disableReleaseSwitching": False,
                } for f in files],
            })
            status = await self._wait_for_command(client, command["id"])
            if status.get("status") != "completed":
                return await self._fail(title, book, f"import command {status.get('status')}: "
                                                     f"{status.get('message') or status.get('exception', '')[:200]}")
            refreshed = await client.get(f"/book/{grabbed_id}")
            if not (refreshed.get("statistics") or {}).get("bookFileCount"):
                return await self._fail(title, book, "Chaptarr accepted the import but the book still has no file")
            self._event("rescued", title, book.get("title"), f"{len(files)} file(s) → {media_type}")
            _LOGGER.info("books rescue: imported '%s' as '%s' (book %s)", title, book.get("title"), grabbed_id)
        except (UpstreamError, aiohttp.ClientError, TimeoutError, KeyError) as exc:
            await self._fail(title, None, f"{exc.__class__.__name__}: {exc}")

    async def _wait_for_command(self, client: ChaptarrClient, command_id: int) -> dict:
        deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
        while True:
            status = await client.get(f"/command/{command_id}")
            if status.get("status") in ("completed", "failed", "aborted", "cancelled", "orphaned"):
                return status
            if time.monotonic() > deadline:
                return {"status": "timeout"}
            await asyncio.sleep(COMMAND_POLL_SECONDS)

    def _event(self, kind: str, download: str, book: str | None, detail: str) -> None:
        self.events.appendleft({
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "kind": kind, "download": download, "book": book, "detail": detail,
        })

    def _skip(self, download: str, reason: str) -> None:
        _LOGGER.info("books rescue: leaving '%s' alone (%s)", download, reason)
        self._event("skipped", download, None, reason)

    async def _fail(self, download: str, book: dict | None, reason: str) -> None:
        book_title = (book or {}).get("title")
        _LOGGER.warning("books rescue: could not import '%s': %s", download, reason)
        self._event("failed", download, book_title, reason)
        message = (
            f"Chaptarr could not import **{book_title or download}** automatically.\n\n"
            f"Download: `{download}`\nReason: {reason}\n\n"
            "Assign it manually in Chaptarr → Activity → Queue."
        )
        persistent_notification.async_create(
            self._hass, message, title="Books: import needs attention",
            notification_id=f"books_rescue_{abs(hash(download))}",
        )
        service = (get_config(self._hass).get(CONF_NOTIFY_SERVICE) or "").strip()
        if service:
            domain, _, name = service.partition(".")
            if not name:
                domain, name = "notify", domain
            if self._hass.services.has_service(domain, name):
                await self._hass.services.async_call(
                    domain, name,
                    {"title": "Buch-Import fehlgeschlagen",
                     "message": f"{book_title or download}: {reason}"},
                    blocking=False,
                )
            else:
                _LOGGER.warning("books rescue: notify service '%s' does not exist", service)
