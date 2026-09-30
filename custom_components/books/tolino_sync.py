"""Reading progress: tolino Cloud -> Audiobookshelf (and so the card) for books that were sent through the integration.

Opt-in (config: sync_progress, default off). Every SYNC_INTERVAL_SECONDS the bridge's /progress is read (read-only on
Tolino's side) and newer states are written to Audiobookshelf. Newest wins: an Audiobookshelf state that is newer than
Tolino's is never overwritten.

Tolino positions look like 'OEBPS/part0045.xhtml#point(/1/4/230/1:138)'. That is an EPUB-CFI-style path below the
spine document; verified against a real book (the highlight Tolino recorded at such a path is exactly where epub.js
finds its text), except that Tolino's character offset counts extra spaces before punctuation. So the location
handed to the reader is the start of that text node / element: paragraph precision, which is what resuming needs.
"""
from __future__ import annotations

import io
import logging
import posixpath
import re
import zipfile
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import aiohttp

from homeassistant.core import HomeAssistant

from .api import AbsClient, TolinoBridgeClient, UpstreamError, get_config
from .const import CONF_SYNC_PROGRESS, DEFAULT_SYNC_PROGRESS, DOMAIN, TOLINO_MAX_BYTES, TOLINO_UPLOAD_TIMEOUT

_LOGGER = logging.getLogger(__name__)

_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container", "o": "http://www.idpf.org/2007/opf"}


def spine_hrefs(epub: bytes) -> list[str]:
    """Zip-root-relative hrefs of the spine documents, in reading order (what the CFI spine step counts)."""
    z = zipfile.ZipFile(io.BytesIO(epub))
    opf_path = ET.fromstring(z.read("META-INF/container.xml")).find(".//c:rootfile", _NS).get("full-path")
    base = posixpath.dirname(opf_path)
    root = ET.fromstring(z.read(opf_path))
    manifest = {i.get("id"): unquote(i.get("href", "")) for i in root.iterfind(".//o:manifest/o:item", _NS)}
    return [posixpath.normpath(posixpath.join(base, manifest[r.get("idref")]))
            for r in root.iterfind(".//o:spine/o:itemref", _NS) if r.get("idref") in manifest]


def tolino_to_cfi(position: str | None, hrefs: list[str]) -> str | None:
    """'OEBPS/a.xhtml#point(/1/4/230/1:138)' -> 'epubcfi(/6/{2n}!/4/230/1:0)', or None if it can't be mapped."""
    if not position or "#point(" not in position:
        return None
    href, _, rest = position.partition("#point(")
    path = rest.rstrip(")").split(":", 1)[0]
    if not re.fullmatch(r"(/\d+)+", path) or not path.startswith("/1/"):
        return None
    try:
        index = hrefs.index(posixpath.normpath(unquote(href)))
    except ValueError:
        return None
    steps = path[2:]                                   # drop the document-root step, keep /4/230/1
    # A char offset is only valid on a text node (odd last step); elements get no offset.
    offset = ":0" if int(steps.rsplit("/", 1)[1]) % 2 == 1 else ""
    return f"epubcfi(/6/{2 * (index + 1)}!{steps}{offset})"


class ProgressSync:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._spines: dict[tuple[str, str], list[str]] = {}

    @property
    def enabled(self) -> bool:
        cfg = get_config(self._hass)
        return bool(cfg.get(CONF_SYNC_PROGRESS, DEFAULT_SYNC_PROGRESS)) and TolinoBridgeClient(self._hass, cfg).configured

    async def async_tick(self, _now=None) -> dict | None:
        if not self.enabled:
            return None
        try:
            return await self.async_sync()
        except Exception:  # noqa: BLE001 - a background job must never take anything down
            _LOGGER.exception("books: reading-progress sync failed")
            return None

    async def async_sync(self) -> dict:
        cfg = get_config(self._hass)
        registry = self._hass.data[DOMAIN]["tolino_sent"]
        summary = {"checked": 0, "imported": [], "skipped": []}
        if not registry.items:
            return summary
        try:
            books = (await TolinoBridgeClient(self._hass, cfg).get("/progress", timeout=120)).get("books", {})
        except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("books: progress sync: bridge not reachable (%s)", exc)
            return summary
        abs_client = AbsClient(self._hass, cfg)
        for item_id, sent in list(registry.items.items()):
            state = books.get(sent["deliverableId"])
            if not state:
                continue
            summary["checked"] += 1
            modified, finished = int(state.get("modified") or 0), bool(state.get("finished"))
            if modified <= sent.get("progress_modified", 0) and finished == sent.get("progress_finished", False):
                continue                                   # nothing new since the last look
            try:
                applied = await self._apply(abs_client, item_id, sent, state, modified, finished)
            except (UpstreamError, aiohttp.ClientError, TimeoutError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
                _LOGGER.warning("books: progress sync for %s failed: %s", item_id, exc)
                continue                                   # retried next tick
            await registry.async_update(item_id, progress_modified=modified, progress_finished=finished)
            summary["imported" if applied else "skipped"].append(item_id)
        return summary

    async def _apply(self, abs_client: AbsClient, item_id: str, sent: dict, state: dict, modified: int, finished: bool) -> bool:
        try:
            current = await abs_client.get(f"/me/progress/{item_id}")
        except UpstreamError as exc:
            if exc.status != 404:
                raise
            current = None                                 # never opened in Audiobookshelf
        if current and int(current.get("lastUpdate") or 0) > modified:
            _LOGGER.debug("books: progress sync: Audiobookshelf is newer for %s, leaving it", item_id)
            return False
        item = await abs_client.get(f"/items/{item_id}")
        body: dict = {"isFinished": finished}
        progress = 1 if finished and state.get("progress") is None else state.get("progress")
        if progress is not None:
            body["ebookProgress"] = 1 if finished else progress
        ebook = ((item or {}).get("media") or {}).get("ebookFile") or {}
        if str(ebook.get("ebookFormat") or "").lower() == "epub" and state.get("position") and not finished:
            cfi = tolino_to_cfi(state["position"], await self._spine(abs_client, item_id, sent["deliverableId"]))
            if cfi:
                body["ebookLocation"] = cfi
        await abs_client.request("PATCH", f"/me/progress/{item_id}", json=body)
        _LOGGER.info("books: progress sync: %s -> %s", item_id, body)
        return True

    async def _spine(self, abs_client: AbsClient, item_id: str, deliverable_id: str) -> list[str]:
        key = (item_id, deliverable_id)
        if key not in self._spines:
            epub = await abs_client.fetch_bytes(f"/items/{item_id}/ebook", max_bytes=TOLINO_MAX_BYTES, timeout=TOLINO_UPLOAD_TIMEOUT)
            self._spines[key] = spine_hrefs(epub)
        return self._spines[key]
