"""Import the ebooks of a person's tolino account into Audiobookshelf (opt-in per person, default off).

Every IMPORT_INTERVAL_SECONDS the bridge is asked for the account's ebooks. Purchases (not the account's own uploads - those
came from Audiobookshelf in the first place) that are not yet known are downloaded through the bridge (watermark file removed,
see the bridge's purchases.py), uploaded to the ebook library with Audiobookshelf's upload API and tagged `für NAME` (tags.py),
so the Books card shows them under that person. What is already in Audiobookshelf (same title) is not uploaded again. Switching it
on takes the whole stock once; at most MAX_PER_RUN books per run, so a big account just takes a few runs.

Imported books are never sent back: the auto-send job and the card's "An tolino" know them (`is_imported`).
Needs the "update" and "upload" permissions of the shared Audiobookshelf user (never admin)."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
import zipfile
from xml.etree import ElementTree

import aiohttp

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import AbsClient, TolinoBridgeClient, UpstreamError, get_config
from .const import CONF_IMPORT_TOLINO, CONF_NOTIFY_SERVICE, DEFAULT_TOLINO_ACCOUNT, DOMAIN
from .notify_helper import async_push
from .tags import async_tag_item, person_tag
from .tolino_send import _bridge_error
from .users import get_users, tolino_config, tolino_user_id
from .wishes import _norm

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "books_tolino_import"
MAX_PER_RUN = 3
MAX_EPUB_BYTES = 300 * 1024 * 1024
FIND_TRIES = 8                      # Audiobookshelf's folder watcher needs a moment to create the item after an upload
FIND_WAIT_S = 3
CLOCK_SKEW_MS = 2 * 60 * 1000
# Book-specific: trying again changes nothing. Everything else (bridge/Thalia/Audiobookshelf trouble) stops the run and is retried.
PERMANENT = {"drm", "not_epub", "too_large", "bad_download_info"}
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container", "o": "http://www.idpf.org/2007/opf", "d": "http://purl.org/dc/elements/1.1/"}


def epub_meta(data: bytes) -> dict:
    """{title, author} from the EPUB's own OPF ({} when it cannot be read): the shop's author field is sometimes a placeholder."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        container = ElementTree.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.find(".//c:rootfile", _NS).get("full-path")
        root = ElementTree.fromstring(z.read(opf_path))
        title = (root.findtext(".//d:title", namespaces=_NS) or "").strip()
        author = (root.findtext(".//d:creator", namespaces=_NS) or "").strip()
        return {k: v for k, v in (("title", title), ("author", author)) if v}
    except (KeyError, ValueError, AttributeError, zipfile.BadZipFile, ElementTree.ParseError):
        return {}


def _safe(text: str, fallback: str) -> str:
    return _UNSAFE.sub(" ", text or "").strip(" .") or fallback


def target_library(libraries: list[dict]) -> dict | None:
    """The ebook library: the book library named like "eBooks"; with only one book library, that one."""
    books = [lib for lib in libraries if lib.get("mediaType") == "book"]
    named = [lib for lib in books if re.sub(r"[^a-z]", "", (lib.get("name") or "").lower()) in ("ebook", "ebooks")]
    return named[0] if named else (books[0] if len(books) == 1 else None)


class ImportError_(Exception):  # noqa: N818 - local control flow, never leaves this module
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class TolinoImporter:
    def __init__(self, hass: HomeAssistant, account: str = DEFAULT_TOLINO_ACCOUNT) -> None:
        self._hass = hass
        self.account = account
        self._store: Store = Store(hass, 1, STORAGE_KEY if account == DEFAULT_TOLINO_ACCOUNT else f"{STORAGE_KEY}_{account}")
        self.state: dict = {"active": False, "owner": None, "done": {}, "failed": {}}
        self.last_run: dict | None = None
        self._running = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        cfg = tolino_config(self._hass, self.account)
        return bool(cfg.get(CONF_IMPORT_TOLINO)) and TolinoBridgeClient(self._hass, cfg, self.account).configured

    def is_imported(self, item_id: str) -> bool:
        return any(v.get("item_id") == item_id for v in self.state["done"].values())

    async def async_start(self) -> None:
        """Load the state; a new owner of the account starts from scratch (their cloud is a different one)."""
        self.state.update(await self._store.async_load() or {})
        self.state.setdefault("done", {})
        self.state.setdefault("failed", {})
        owner = tolino_user_id(self._hass, self.account)
        if self.enabled and (not self.state.get("active") or self.state.get("owner") != owner):
            fresh = self.state.get("owner") != owner
            self.state.update(active=True, owner=owner, **({"done": {}, "failed": {}} if fresh else {"failed": {}}))
            _LOGGER.info("books: tolino import switched on for account %s", self.account)
            await self._store.async_save(self.state)
            self._hass.async_create_task(self.async_tick())             # do not wait for the next interval
        elif not self.enabled and self.state.get("active"):
            self.state["active"] = False
            await self._store.async_save(self.state)

    async def async_tick(self, _now=None) -> dict | None:
        if not self.enabled or self._running.locked():
            return None
        async with self._running:
            try:
                return await self.async_run()
            except Exception:  # noqa: BLE001 - a background job must never take anything down
                _LOGGER.exception("books: tolino import failed")
                return None

    # -- one run ----------------------------------------------------------------------------------------------

    async def async_run(self) -> dict:
        summary = {"imported": [], "already": [], "failed": [], "left": 0}
        cfg = tolino_config(self._hass, self.account)
        bridge = TolinoBridgeClient(self._hass, cfg, self.account)
        abs_client = AbsClient(self._hass, get_config(self._hass))        # the shared user: it holds the update/upload permissions
        owner = get_users(self._hass).get(tolino_user_id(self._hass, self.account) or "", {})
        tag = person_tag(owner)
        try:
            books = [b for b in ((await bridge.get("/purchases")) or {}).get("books", []) if b.get("kind") == "purchase"]
        except UpstreamError as exc:
            _LOGGER.warning("books: tolino import cannot list the account's books: %s", _bridge_error(exc)[:2])
            return summary
        except (aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("books: tolino import cannot reach the bridge: %s", exc)
            return summary
        todo = [b for b in books if b["publicationId"] not in self.state["done"] and b["publicationId"] not in self.state["failed"]]
        await self._retag(abs_client, tag)
        if not todo:
            return self._finish(summary)
        try:
            libraries = (await abs_client.get("/libraries") or {}).get("libraries", [])
            library = target_library(libraries)
            if library is None:
                raise ImportError_("no_library", "no Audiobookshelf library named 'eBooks' (or exactly one book library)")
            known = await self._known_titles(abs_client, [lib["id"] for lib in libraries if lib.get("mediaType") == "book"])
        except ImportError_ as exc:
            self._alert("books_import_library", "tolino-Import: keine Ziel-Bibliothek", f"{exc.message}. Lege in Audiobookshelf eine Bibliothek „eBooks“ an.")
            return summary
        except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("books: tolino import cannot read Audiobookshelf: %s", exc)
            return summary
        for n, book in enumerate(todo):
            pid, title = book["publicationId"], book.get("title") or book["publicationId"]
            if _norm(title) in known:
                self.state["done"][pid] = {"title": title, "at": dt_util.utcnow().isoformat(), "how": "already_in_abs", "item_id": None}
                summary["already"].append(title)
                continue
            if len(summary["imported"]) >= MAX_PER_RUN:
                summary["left"] = len(todo) - n
                break
            try:
                item_id = await self._import_one(bridge, abs_client, library, book, tag, known)
            except ImportError_ as exc:
                if exc.code in PERMANENT:
                    self.state["failed"][pid] = {"title": title, "error": exc.code, "at": int(time.time() * 1000)}
                    summary["failed"].append(title)
                    self._alert(f"books_import_{pid}", "tolino-Import", f"„{title}“ konnte nicht importiert werden: {exc.message}. Es wird nicht erneut versucht.")
                    continue
                _LOGGER.warning("books: tolino import paused at '%s': %s", title, exc)
                break                                                    # bridge/Audiobookshelf trouble: stop, retry next run
            if item_id is False:                                          # the book's own title is already in Audiobookshelf
                self.state["done"][pid] = {"title": title, "at": dt_util.utcnow().isoformat(), "how": "already_in_abs", "item_id": None}
                summary["already"].append(title)
                continue
            self.state["done"][pid] = {"title": title, "at": dt_util.utcnow().isoformat(), "how": "imported", "item_id": item_id}
            known.add(_norm(title))
            summary["imported"].append(title)
            await self._store.async_save(self.state)                     # after every book: a restart must not repeat uploads
        return self._finish(summary, owner)

    def _finish(self, summary: dict, owner: dict | None = None) -> dict:
        self._hass.async_create_task(self._store.async_save(self.state))
        self.last_run = {"at": dt_util.utcnow(), **{k: len(v) if isinstance(v, list) else v for k, v in summary.items()}}
        if summary["imported"] and owner and owner.get(CONF_NOTIFY_SERVICE):
            names = ", ".join(f"„{t}“" for t in summary["imported"][:5]) + (" …" if len(summary["imported"]) > 5 else "")
            self._hass.async_create_task(async_push(self._hass, "Aus deinem tolino importiert", f"Neu in der Bibliothek: {names}", targets=owner[CONF_NOTIFY_SERVICE]))
        return summary

    async def _known_titles(self, abs_client: AbsClient, library_ids: list[str]) -> set[str]:
        titles: set[str] = set()
        for lib in library_ids:
            page = 0
            while True:
                data = await abs_client.get(f"/libraries/{lib}/items", params={"limit": 500, "page": page, "minified": 1})
                results = (data or {}).get("results", [])
                titles |= {_norm(((i.get("media") or {}).get("metadata") or {}).get("title", "")) for i in results}
                if len(results) < 500:
                    break
                page += 1
        titles.discard("")
        return titles

    async def _import_one(self, bridge: TolinoBridgeClient, abs_client: AbsClient, library: dict, book: dict, tag: str | None,
                          known: set[str]) -> str | None | bool:
        """The new item's id (None: uploaded, not found yet); False when the book's own title is already in Audiobookshelf."""
        try:
            data = await bridge.fetch_bytes(f"/purchases/{book['id']}/file", max_bytes=MAX_EPUB_BYTES, timeout=300)
        except UpstreamError as exc:
            code, detail, _ = _bridge_error(exc)
            raise ImportError_("too_large" if exc.status == 413 else code, detail or code) from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ImportError_("unreachable", str(exc)) from exc
        meta = epub_meta(data)
        title = meta.get("title") or book.get("title") or "Unbekannt"
        if meta.get("title") and _norm(meta["title"]) in known:        # the shop's title differs from the book's own ("Feenspiele" / "The Faerie Games")
            return False
        author = meta.get("author") or ", ".join(book.get("authors") or []) or "Unbekannt"
        started = int(time.time() * 1000) - CLOCK_SKEW_MS
        folder = (library.get("folders") or [{}])[0].get("id")
        if not folder:
            raise ImportError_("no_library", "the library has no folder")
        form = aiohttp.FormData(quote_fields=False)
        for key, value in (("title", title), ("author", author), ("library", library["id"]), ("folder", folder)):
            form.add_field(key, value)
        form.add_field("0", data, filename=f"{_safe(title, 'Buch')}.epub", content_type="application/epub+zip")
        try:
            await abs_client.request("POST", "/upload", data=form, timeout=600)
        except UpstreamError as exc:
            if exc.status in (401, 403):
                self._alert("books_import_perm", "tolino-Import: Recht fehlt",
                            "Der Audiobookshelf-Benutzer der Haupteinstellungen braucht die Rechte „Hochladen“ und „Aktualisieren“ (kein Admin).")
            raise ImportError_("abs_refused", f"Audiobookshelf upload: HTTP {exc.status}") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ImportError_("abs_unreachable", str(exc)) from exc
        persistent_notification.async_dismiss(self._hass, "books_import_perm")
        item_id = await self._find(abs_client, library["id"], title, started)
        if item_id and tag:
            await self._tag(abs_client, item_id, tag)
        elif not item_id:
            _LOGGER.warning("books: '%s' is uploaded, but the new item was not found yet; it is tagged on a later run", title)
        _LOGGER.info("books: imported '%s' from tolino account %s", title, self.account)
        return item_id

    async def _find(self, abs_client: AbsClient, library_id: str, title: str, since_ms: int) -> str | None:
        for attempt in range(FIND_TRIES):
            if attempt:
                await asyncio.sleep(FIND_WAIT_S)
            try:
                data = await abs_client.get(f"/libraries/{library_id}/items", params={"sort": "addedAt", "desc": 1, "limit": 10, "minified": 1})
            except (UpstreamError, aiohttp.ClientError, TimeoutError):
                continue
            for item in (data or {}).get("results", []):
                got = ((item.get("media") or {}).get("metadata") or {}).get("title", "")
                if int(item.get("addedAt") or 0) >= since_ms and _norm(got) == _norm(title):
                    return item["id"]
        return None

    async def _tag(self, abs_client: AbsClient, item_id: str, tag: str) -> None:
        try:
            await async_tag_item(abs_client, item_id, tag)
        except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("books: cannot tag the imported book for %s (the Audiobookshelf user needs the 'update' permission): %s", tag, exc)

    async def _retag(self, abs_client: AbsClient, tag: str | None) -> None:
        """Uploaded earlier but not found/tagged at the time: find it now."""
        if not tag:
            return
        pending = {pid: e for pid, e in self.state["done"].items() if e.get("how") == "imported" and not e.get("item_id")}
        if not pending:
            return
        try:
            library = target_library((await abs_client.get("/libraries") or {}).get("libraries", []))
            if library is None:
                return
            data = await abs_client.get(f"/libraries/{library['id']}/items", params={"sort": "addedAt", "desc": 1, "limit": 60, "minified": 1})
        except (UpstreamError, aiohttp.ClientError, TimeoutError):
            return
        for pid, entry in pending.items():
            for item in (data or {}).get("results", []):
                if _norm(((item.get("media") or {}).get("metadata") or {}).get("title", "")) == _norm(entry["title"]):
                    entry["item_id"] = item["id"]
                    await self._tag(abs_client, item["id"], tag)
                    break

    def _alert(self, notification_id: str, title: str, message: str) -> None:
        _LOGGER.warning("books: %s: %s", title, message)
        persistent_notification.async_create(self._hass, message, title=title, notification_id=notification_id)


def importer_for(hass: HomeAssistant, account: str = DEFAULT_TOLINO_ACCOUNT) -> TolinoImporter | None:
    return (hass.data.get(DOMAIN, {}).get("jobs", {}).get(account) or {}).get("import")
