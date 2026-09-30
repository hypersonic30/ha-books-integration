"""Reading progress: tolino Cloud -> Audiobookshelf (and so the card) for books that were sent through the integration.

Opt-in (config: sync_progress, default off). Every SYNC_INTERVAL_SECONDS the bridge's /progress is read (read-only on
Tolino's side) and newer states are written to Audiobookshelf. Newest wins: an Audiobookshelf state that is newer than
Tolino's is never overwritten.

Tolino positions ('OEBPS/part0045.xhtml#point(/1/4/230/1:138)') count child NODES of the document, not CFI elements; see
positions.py for the mapping (computed from the real document structure) and its limits (paragraph precision).
"""
from __future__ import annotations

import logging
import zipfile
from xml.etree import ElementTree as ET

import aiohttp

from homeassistant.core import HomeAssistant

from .api import AbsClient, TolinoBridgeClient, UpstreamError, get_config
from .const import CONF_SYNC_PROGRESS, DEFAULT_SYNC_PROGRESS, DOMAIN, TOLINO_MAX_BYTES, TOLINO_UPLOAD_TIMEOUT
from .positions import Epub, point_to_cfi

_LOGGER = logging.getLogger(__name__)

class ProgressSync:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

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
            epub = Epub(await abs_client.fetch_bytes(
                f"/items/{item_id}/ebook", max_bytes=TOLINO_MAX_BYTES, timeout=TOLINO_UPLOAD_TIMEOUT))
            cfi = point_to_cfi(epub, state["position"])
            if cfi:
                body["ebookLocation"] = cfi
        await abs_client.request("PATCH", f"/me/progress/{item_id}", json=body)
        _LOGGER.info("books: progress sync: %s -> %s", item_id, body)
        return True
