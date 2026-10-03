"""Reading progress between the tolino Cloud and Audiobookshelf (and so the card), for books sent through the integration.

Opt-in (config: sync_progress, default off; sync_progress_write, default off, adds the card -> tolino direction).
Every SYNC_INTERVAL_SECONDS, per book, in two directions:

  tolino -> Audiobookshelf   reads the bridge's /progress (read-only on Tolino's side)
  Audiobookshelf -> tolino   writes the bookmark / finished flag through the bridge's PUT /progress/{id}

Newest wins, and loops are avoided by remembering per book the last state seen on each side: `progress_modified` /
`progress_finished` (Tolino's bookmark time and finished flag, which also covers what we wrote there ourselves) and
`abs_seen` (Audiobookshelf's lastUpdate, which also covers what we wrote there ourselves). A side counts as "changed"
only when it moved past what we last saw.

Positions: see positions.py (Tolino counts document nodes, not CFI elements; paragraph precision). Audiobooks: audio_positions.py (track and
second); their "finished" is not synced yet (how the app marks it is not known).
"""
from __future__ import annotations

import logging
import zipfile
from xml.etree import ElementTree as ET

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .api import AbsClient, TolinoBridgeClient, UpstreamError
from .tolino_registry import registry_for
from .users import tolino_config
from .const import (
    CONF_SYNC_PROGRESS,
    CONF_SYNC_PROGRESS_WRITE,
    DEFAULT_SYNC_PROGRESS,
    DEFAULT_TOLINO_ACCOUNT,
    DEFAULT_SYNC_PROGRESS_WRITE,
    DOMAIN,
    EVENT_PROGRESS_SYNCED,
    SIGNAL_SYNC_UPDATED,
    TOLINO_MAX_BYTES,
    TOLINO_UPLOAD_TIMEOUT,
)
from .audio_positions import medialoc_to_time, time_to_medialoc, tolino_progress
from .positions import Epub, cfi_to_point, point_to_cfi

_LOGGER = logging.getLogger(__name__)

MIN_PROGRESS_DELTA = 0.005          # below this a difference is noise, not something to write to the other side
MIN_AUDIO_DELTA_S = 3.0             # the same for an audiobook: places closer than this (seconds) are the same place
_SYNC_ERRORS = (UpstreamError, aiohttp.ClientError, TimeoutError, zipfile.BadZipFile, ET.ParseError, KeyError)


class ProgressSync:
    def __init__(self, hass: HomeAssistant, account: str = DEFAULT_TOLINO_ACCOUNT) -> None:
        self._hass = hass
        self.account = account                      # one job per bridge account (= per person with a Tolino)
        self.last_run: dict | None = None
        self._durations_cache: dict[str, list[float] | None] = {}

    @property
    def enabled(self) -> bool:
        cfg = tolino_config(self._hass, self.account)
        return bool(cfg.get(CONF_SYNC_PROGRESS, DEFAULT_SYNC_PROGRESS)) and TolinoBridgeClient(self._hass, cfg, self.account).configured

    @property
    def write_enabled(self) -> bool:
        return self.enabled and bool(tolino_config(self._hass, self.account).get(CONF_SYNC_PROGRESS_WRITE, DEFAULT_SYNC_PROGRESS_WRITE))

    async def async_tick(self, _now=None) -> dict | None:
        if not self.enabled:
            return None
        try:
            return await self.async_sync()
        except Exception:  # noqa: BLE001 - a background job must never take anything down
            _LOGGER.exception("books: reading-progress sync failed")
            return None

    async def async_sync(self) -> dict:
        cfg = tolino_config(self._hass, self.account)
        registry = registry_for(self._hass, self.account)
        write = self.write_enabled
        summary = {"checked": 0, "imported": [], "exported": [], "skipped": []}
        self._durations_cache = {}
        if not registry.items:
            return summary
        bridge = TolinoBridgeClient(self._hass, cfg, self.account)
        try:
            books = (await bridge.get("/progress", timeout=120)).get("books", {})
        except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("books: progress sync: bridge not reachable (%s)", exc)
            return summary
        abs_client = AbsClient(self._hass, cfg)
        for item_id, sent in list(registry.items.items()):
            state = books.get(sent["deliverableId"])
            t_seen, f_seen, a_seen = sent.get("progress_modified", 0), sent.get("progress_finished", False), sent.get("abs_seen", 0)
            tolino_t = int((state or {}).get("modified") or 0)
            tolino_f = bool((state or {}).get("finished"))
            tolino_changed = bool(state) and (tolino_t > t_seen or tolino_f != f_seen)
            if not tolino_changed and not write:
                continue                                   # read-only mode: only look when Tolino has news
            summary["checked"] += 1
            try:
                current = await self._abs_progress(abs_client, item_id)
                abs_t = int((current or {}).get("lastUpdate") or 0)
                abs_unseen = current is not None and abs_t > a_seen
                if tolino_changed and abs_unseen and abs_t > tolino_t:
                    action = "export" if write else "skip"  # Audiobookshelf holds a newer state Tolino doesn't have
                elif tolino_changed:
                    action = "import"
                elif write and abs_unseen:
                    action = "export"
                else:
                    continue
                if action == "import":
                    await self._import(abs_client, registry, item_id, sent, state, tolino_t, tolino_f)
                    summary["imported"].append(item_id)
                    self._hass.bus.async_fire(EVENT_PROGRESS_SYNCED, {"item_id": item_id, "direction": "tolino_to_abs", "finished": tolino_f})
                elif action == "export":
                    wrote = await self._export(bridge, abs_client, registry, item_id, sent, state, current, abs_t)
                    summary["exported" if wrote else "skipped"].append(item_id)
                    if wrote:
                        self._hass.bus.async_fire(EVENT_PROGRESS_SYNCED, {"item_id": item_id, "direction": "abs_to_tolino",
                                                                           "finished": bool(current.get("isFinished"))})
                else:
                    await registry.async_update(item_id, progress_modified=tolino_t, progress_finished=tolino_f)
                    summary["skipped"].append(item_id)
            except _SYNC_ERRORS as exc:
                _LOGGER.warning("books: progress sync for %s failed: %s", item_id, exc)
        self.last_run = {"at": dt_util.utcnow(), **{k: len(v) if isinstance(v, list) else v for k, v in summary.items()}}
        async_dispatcher_send(self._hass, SIGNAL_SYNC_UPDATED)
        return summary

    # -- helpers ------------------------------------------------------------------------------------------------

    @staticmethod
    async def _abs_progress(abs_client: AbsClient, item_id: str) -> dict | None:
        try:
            return await abs_client.get(f"/me/progress/{item_id}")
        except UpstreamError as exc:
            if exc.status != 404:
                raise
            return None                                    # never opened in Audiobookshelf

    @staticmethod
    async def _epub(abs_client: AbsClient, item_id: str) -> Epub:
        return Epub(await abs_client.fetch_bytes(f"/items/{item_id}/ebook", max_bytes=TOLINO_MAX_BYTES, timeout=TOLINO_UPLOAD_TIMEOUT))

    async def _durations(self, abs_client: AbsClient, item_id: str) -> list[float] | None:
        """The lengths (s) of an audiobook's files in play order, or None when the item is no audiobook (cached for one run)."""
        if item_id not in self._durations_cache:
            item = await abs_client.get(f"/items/{item_id}", params={"expanded": 1})
            files = sorted([f for f in (((item or {}).get("media") or {}).get("audioFiles") or []) if not f.get("exclude")], key=lambda f: f.get("index") or 0)
            self._durations_cache[item_id] = [float(f.get("duration") or 0) for f in files] or None
        return self._durations_cache[item_id]

    @staticmethod
    async def _is_epub(abs_client: AbsClient, item_id: str) -> bool:
        item = await abs_client.get(f"/items/{item_id}")
        return str((((item or {}).get("media") or {}).get("ebookFile") or {}).get("ebookFormat") or "").lower() == "epub"

    # -- tolino -> Audiobookshelf ---------------------------------------------------------------------------------

    async def _import_audio(self, abs_client, registry, item_id, state, tolino_t, durations) -> None:
        body: dict = {}
        current = medialoc_to_time(state.get("position"), durations)
        if current is not None:
            total = sum(durations)
            body = {"currentTime": current, "duration": total, "progress": min(1.0, current / total), "isFinished": False}
            await abs_client.request("PATCH", f"/me/progress/{item_id}", json=body)
            _LOGGER.info("books: progress sync tolino -> Audiobookshelf (audio): %s %s", item_id, body)
        after = await self._abs_progress(abs_client, item_id)
        await registry.async_update(item_id, progress_modified=tolino_t, progress_finished=False, abs_seen=int((after or {}).get("lastUpdate") or 0))

    async def _import(self, abs_client, registry, item_id, sent, state, tolino_t, tolino_f) -> None:
        durations = await self._durations(abs_client, item_id)
        if durations:
            return await self._import_audio(abs_client, registry, item_id, state, tolino_t, durations)
        body: dict = {"isFinished": tolino_f}
        progress = 1 if tolino_f and state.get("progress") is None else state.get("progress")
        if progress is not None:
            body["ebookProgress"] = 1 if tolino_f else progress
        if state.get("position") and not tolino_f and await self._is_epub(abs_client, item_id):
            cfi = point_to_cfi(await self._epub(abs_client, item_id), state["position"])
            if cfi:
                body["ebookLocation"] = cfi
        await abs_client.request("PATCH", f"/me/progress/{item_id}", json=body)
        after = await self._abs_progress(abs_client, item_id)
        _LOGGER.info("books: progress sync tolino -> Audiobookshelf: %s %s", item_id, body)
        await registry.async_update(item_id, progress_modified=tolino_t, progress_finished=tolino_f,
                                    abs_seen=int((after or {}).get("lastUpdate") or 0))

    # -- Audiobookshelf -> tolino ---------------------------------------------------------------------------------

    async def _export_audio(self, bridge, registry, item_id, sent, state, current, abs_t, durations) -> bool:
        """An audiobook: the place in Audiobookshelf becomes a tolino position (the finished flag is left alone, see the module text)."""
        played = float(current.get("currentTime") or 0)
        position = time_to_medialoc(played, durations)
        there = medialoc_to_time((state or {}).get("position"), durations)
        if current.get("isFinished") or not position or (there is not None and abs(there - played) < MIN_AUDIO_DELTA_S):
            await registry.async_update(item_id, abs_seen=abs_t)
            return False
        info = await bridge.get(f"/audiobooks/{sent['deliverableId']}")
        body = {"position": position, "progress": tolino_progress(played, [t["duration_ms"] for t in info.get("tracks") or []], info.get("duration_s") or 0)}
        result = await bridge.request("PUT", f"/progress/{sent['deliverableId']}", json=body, timeout=120)
        _LOGGER.info("books: progress sync Audiobookshelf -> tolino (audio): %s %s", item_id, body)
        await registry.async_update(item_id, abs_seen=abs_t, progress_modified=int(result.get("modified") or 0), progress_finished=False)
        return True

    async def _export(self, bridge, abs_client, registry, item_id, sent, state, current, abs_t) -> bool:
        """Returns True if something was written to Tolino (False: nothing material, only noted as seen)."""
        durations = await self._durations(abs_client, item_id)
        if durations:
            return await self._export_audio(bridge, registry, item_id, sent, state, current, abs_t, durations)
        body: dict = {}
        abs_finished = bool(current.get("isFinished"))
        if abs_finished != bool((state or {}).get("finished")):
            body["finished"] = abs_finished
        cfi, abs_progress = current.get("ebookLocation"), current.get("ebookProgress")
        if not abs_finished and cfi and abs_progress is not None and await self._is_epub(abs_client, item_id):
            point = cfi_to_point(await self._epub(abs_client, item_id), cfi)
            if point and (state is None or point != state.get("position")
                          or abs(float(abs_progress) - float(state.get("progress") or 0)) >= MIN_PROGRESS_DELTA):
                body.update(progress=max(0.0, min(1.0, float(abs_progress))), position=point)
        if not body:
            await registry.async_update(item_id, abs_seen=abs_t)
            return False
        result = await bridge.request("PUT", f"/progress/{sent['deliverableId']}", json=body, timeout=120)
        _LOGGER.info("books: progress sync Audiobookshelf -> tolino: %s %s", item_id, body)
        await registry.async_update(item_id, abs_seen=abs_t, progress_modified=int(result.get("modified") or 0),
                                    progress_finished=bool(result.get("finished")))
        return True
