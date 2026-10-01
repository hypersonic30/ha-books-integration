"""Automatically send NEW ebooks to the Tolino Cloud (opt-in, default off).

Every AUTO_SEND_INTERVAL_SECONDS the newest items of Audiobookshelf's book libraries are looked at; an item is sent when
it was added after the feature was switched on (`since`), has an ebook Tolino can take (EPUB/PDF, or a Kindle format the
bridge converts) and was not sent before. The existing library is never touched: switching the option on only sets
`since`; switching it off and on again sets it anew.

Failures: a book that cannot be sent at all (unsupported/too large/conversion failed) is reported once and not retried;
a problem with the bridge or Thalia (unreachable, login blocked, ...) is transient - nothing is marked, the next run
tries again and the run stops early instead of hammering the bridge.
"""
from __future__ import annotations

import logging
import time

import aiohttp

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import AbsClient, TolinoBridgeClient, UpstreamError
from .users import tolino_config, tolino_user_id
from .const import CONF_AUTO_SEND, DEFAULT_AUTO_SEND, DOMAIN, SIGNAL_AUTOSEND_UPDATED, TOLINO_CONVERTIBLE, TOLINO_FORMATS
from .notify_helper import async_push
from .tolino_send import SendError, async_send_to_tolino

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "books_tolino_autosend"
MAX_PER_RUN = 5                  # a bulk import must not become a bulk upload against Thalia's bot protection
PAGE_SIZE = 50
SKEW_MARGIN_MS = 2 * 60 * 1000   # Audiobookshelf's clock vs. Home Assistant's
# Book-specific problems: retrying the same book changes nothing.
PERMANENT = {"bad_type", "no_ebook", "too_large", "convert_failed", "no_converter", "bad_request"}
# Problems with the bridge/Thalia rather than the book: stop this run, try again later.
BRIDGE_DOWN = {"unreachable", "login_backoff", "captcha", "rejected", "2fa", "no_credentials", "no_device",
               "bridge_auth", "not_configured", "bosh", "browser", "timeout", "stale_session", "state", "token", "waf"}


class AutoSender:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store = Store(hass, 1, STORAGE_KEY)
        self.state: dict = {"active": False, "since": 0, "failed": {}}
        self.last_run: dict | None = None

    @property
    def enabled(self) -> bool:
        cfg = tolino_config(self._hass)
        return bool(cfg.get(CONF_AUTO_SEND, DEFAULT_AUTO_SEND)) and TolinoBridgeClient(self._hass, cfg).configured

    async def async_start(self) -> None:
        """Load state and note on/off transitions (the entry reloads when the option changes)."""
        self.state.update(await self._store.async_load() or {})
        self.state.setdefault("failed", {})
        owner = tolino_user_id(self._hass)
        if self.enabled and (not self.state.get("active") or self.state.get("owner") != owner):
            self.state.update(active=True, owner=owner, since=int(time.time() * 1000) - SKEW_MARGIN_MS, failed={})
            _LOGGER.info("books: auto-send switched on; only books added from now on are sent")
            await self._store.async_save(self.state)
        elif not self.enabled and self.state.get("active"):
            self.state["active"] = False
            await self._store.async_save(self.state)

    async def async_tick(self, _now=None) -> dict | None:
        if not self.enabled:
            return None
        try:
            return await self.async_run()
        except Exception:  # noqa: BLE001 - a background job must never take anything down
            _LOGGER.exception("books: auto-send failed")
            return None

    async def _candidates(self, abs_client: AbsClient) -> list[dict]:
        """New book items with an ebook, newest first (libraries are read newest-first and only as far as needed)."""
        since = int(self.state["since"])
        found: list[dict] = []
        libraries = (await abs_client.get("/libraries") or {}).get("libraries", [])
        for library in libraries:
            if library.get("mediaType") != "book":
                continue
            page = 0
            while True:
                data = await abs_client.get(f"/libraries/{library['id']}/items", params={
                    "sort": "addedAt", "desc": 1, "limit": PAGE_SIZE, "page": page, "minified": 1})
                results = (data or {}).get("results", [])
                for item in results:
                    if int(item.get("addedAt") or 0) <= since:
                        results = []                          # sorted newest first: everything after is older
                        break
                    found.append(item)
                if len(results) < PAGE_SIZE:
                    break
                page += 1
        return sorted(found, key=lambda i: int(i.get("addedAt") or 0))      # oldest of the new ones first

    async def async_run(self) -> dict:
        summary = {"checked": 0, "sent": [], "failed": [], "skipped": []}
        registry = self._hass.data[DOMAIN]["tolino_sent"]
        abs_client = AbsClient(self._hass, tolino_config(self._hass))
        try:
            items = await self._candidates(abs_client)
        except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("books: auto-send cannot list Audiobookshelf: %s", exc)
            return summary
        allowed = TOLINO_FORMATS | TOLINO_CONVERTIBLE
        for item in items:
            item_id = item["id"]
            if registry.get(item_id) or self.state["failed"].get(item_id):
                continue
            if str((item.get("media") or {}).get("ebookFormat") or "").lower() not in allowed:
                continue                                       # no ebook (yet), or a format nobody can convert
            if len(summary["sent"]) >= MAX_PER_RUN:
                summary["skipped"].append(item_id)
                continue
            summary["checked"] += 1
            title = ((item.get("media") or {}).get("metadata") or {}).get("title") or item_id
            try:
                await async_send_to_tolino(self._hass, item_id, auto=True)
                summary["sent"].append(item_id)
                self.state["last_sent"] = {"title": title, "at": dt_util.utcnow().isoformat(), "item_id": item_id}
                self.state["total_sent"] = int(self.state.get("total_sent", 0)) + 1
                await self._store.async_save(self.state)
                _LOGGER.info("books: auto-sent '%s' to the Tolino Cloud", title)
            except SendError as exc:
                if exc.code == "already_sent":
                    continue
                if exc.code in PERMANENT:
                    await self._report(item_id, title, exc)
                    summary["failed"].append(item_id)
                    continue
                _LOGGER.warning("books: auto-send paused at '%s': %s (will retry)", title, exc)
                if exc.code in BRIDGE_DOWN:
                    break                                      # the bridge/Thalia is the problem: stop, retry next run
        self.last_run = {"at": dt_util.utcnow(), **{k: len(v) if isinstance(v, list) else v for k, v in summary.items()}}
        async_dispatcher_send(self._hass, SIGNAL_AUTOSEND_UPDATED)
        return summary

    async def _report(self, item_id: str, title: str, exc: SendError) -> None:
        self.state["failed"][item_id] = {"error": exc.code, "at": int(time.time() * 1000)}
        await self._store.async_save(self.state)
        message = f"„{title}“ konnte nicht automatisch an tolino gesendet werden: {exc.message}. Es wird nicht erneut versucht; du kannst es in der Card von Hand senden."
        _LOGGER.warning("books: auto-send gave up on '%s': %s", title, exc)
        persistent_notification.async_create(self._hass, message, title="tolino: automatisches Senden",
                                             notification_id=f"books_autosend_{item_id}")
        await async_push(self._hass, "tolino: automatisches Senden", message)
