""""Tell me when my book is there": who asked for what, and the push when it arrives.

Asking goes through the integration (the card's add button, the manga card's "Laden"), so the Home Assistant user is known.
Arrival is noticed by a timer: books show up as a new Audiobookshelf item with a matching title (a comparison of titles,
not a hard link - Chaptarr and Audiobookshelf share no id), manga volumes as "Post-Processed" in Mylar's history (exact:
the volume's id is known). Only the person who asked is notified; nobody hears about what the others load."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import AbsClient, UpstreamError, get_config
from .auth_watch import key_accepted, key_rejected
from .const import CONF_MYLAR_API_KEY, CONF_MYLAR_URL, CONF_NOTIFY_SERVICE, CONF_VERIFY_SSL, DOMAIN, REQUEST_TIMEOUT, WISH_MAX_AGE_SECONDS
from .notify_helper import async_push
from .users import get_users

_LOGGER = logging.getLogger(__name__)
STORAGE_KEY = "books_wishes"
EVENT_FULFILLED = "books_wish_fulfilled"
CLOCK_SKEW_MS = 5 * 60 * 1000


def _norm(text: str) -> str:
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text or "")        # "(Harry Potter 1)" and the like
    return re.sub(r"[^a-z0-9äöüß]+", "", text.lower())


def titles_match(wanted: str, found: str) -> bool:
    a, b = _norm(wanted), _norm(found)
    return len(a) >= 4 and len(b) >= 4 and (a == b or a in b or b in a)


class Wishes:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store = Store(hass, 1, STORAGE_KEY)
        self.items: list[dict] = []

    async def async_load(self) -> None:
        self.items = list((await self._store.async_load() or {}).get("items", []))

    async def _save(self) -> None:
        await self._store.async_save({"items": self.items})

    async def async_add(self, kind: str, user_id: str | None, **fields) -> None:
        if not user_id:
            return
        key = fields.get("issue") or _norm(fields.get("title", ""))
        if not key or any(w["kind"] == kind and w["user"] == user_id and (w.get("issue") or _norm(w.get("title", ""))) == key
                          for w in self.items):
            return
        self.items.append({"kind": kind, "user": user_id, "ts": time.time(), **fields})
        await self._save()

    # -- timer ----------------------------------------------------------

    async def async_tick(self, _now=None) -> None:
        try:
            await self._check()
        except Exception:  # noqa: BLE001 - a background job must never take anything down
            _LOGGER.exception("books: checking wishes failed")

    async def _check(self) -> None:
        now = time.time()
        fresh = [w for w in self.items if now - w["ts"] < WISH_MAX_AGE_SECONDS]
        changed = len(fresh) != len(self.items)
        self.items = fresh
        done: list[dict] = []
        books = [w for w in self.items if w["kind"] == "book"]
        manga = [w for w in self.items if w["kind"] == "manga"]
        if books:
            done += await self._books_arrived(books)
        if manga:
            done += await self._manga_arrived(manga)
        for wish, what in done:
            self.items.remove(wish)
            await self._tell(wish, what)
        if done or changed:
            await self._save()

    async def _books_arrived(self, wishes: list[dict]) -> list[tuple[dict, str]]:
        client = AbsClient(self._hass, get_config(self._hass))
        added: list[dict] = []
        try:
            for library in (await client.get("/libraries") or {}).get("libraries", []):
                if library.get("mediaType") != "book":
                    continue
                data = await client.get(f"/libraries/{library['id']}/items", params={
                    "sort": "addedAt", "desc": 1, "limit": 60, "minified": 1})
                added += (data or {}).get("results", [])
        except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.debug("books: wishes cannot list Audiobookshelf: %s", exc)
            if isinstance(exc, UpstreamError) and exc.status == 401:
                key_rejected(self._hass, "Audiobookshelf", None)                  # the shared token of the main settings
            return []
        key_accepted(self._hass, "Audiobookshelf", None)
        out = []
        for wish in wishes:
            for item in added:
                title = ((item.get("media") or {}).get("metadata") or {}).get("title", "")
                if int(item.get("addedAt") or 0) >= wish["ts"] * 1000 - CLOCK_SKEW_MS and titles_match(wish["title"], title):
                    out.append((wish, title or wish["title"]))
                    break
        return out

    async def _manga_arrived(self, wishes: list[dict]) -> list[tuple[dict, str]]:
        cfg = get_config(self._hass)
        base, key = (cfg.get(CONF_MYLAR_URL) or "").rstrip("/"), cfg.get(CONF_MYLAR_API_KEY) or ""
        if not base or not key:
            return []
        session = async_get_clientsession(self._hass, verify_ssl=cfg.get(CONF_VERIFY_SSL, True))
        try:
            async with session.get(f"{base}/api", params={"cmd": "getHistory", "apikey": key},
                                   timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                history = (await resp.json(content_type=None)).get("data") or []
        except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError) as exc:
            _LOGGER.debug("books: wishes cannot read Mylar's history: %s", exc)
            return []
        filed = {str(h.get("IssueID")): h for h in history if h.get("Status") == "Post-Processed"}
        return [(w, f'{filed[w["issue"]].get("ComicName", "")} · Band {filed[w["issue"]].get("Issue_Number", "")}'.strip(" ·"))
                for w in wishes if w["issue"] in filed]

    async def _tell(self, wish: dict, what: str) -> None:
        person = get_users(self._hass).get(wish["user"], {})
        self._hass.bus.async_fire(EVENT_FULFILLED, {"user": wish["user"], "kind": wish["kind"], "title": what})
        target = person.get(CONF_NOTIFY_SERVICE)
        if not target:
            return
        where = "in der Bibliothek" if wish["kind"] == "book" else "in Komga"
        await async_push(self._hass, "Neu in der Bibliothek", f"„{what}“ ist jetzt {where}.", targets=target)
