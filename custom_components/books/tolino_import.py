"""Import the ebooks of a person's tolino account into Audiobookshelf (opt-in per person, default off).

Every IMPORT_INTERVAL_SECONDS the bridge is asked for the account's ebooks. Purchases (not the account's own uploads - those
came from Audiobookshelf in the first place) that are not yet known are downloaded through the bridge (watermark file removed,
see the bridge's purchases.py), uploaded to the ebook library with Audiobookshelf's upload API and tagged `für NAME` (tags.py),
so the Books card shows them under that person. What is already in Audiobookshelf (same title) is not uploaded again. Switching it
on takes the whole stock once; at most MAX_PER_RUN books per run, so a big account just takes a few runs.

Imported books are never sent back: the auto-send job and the card's "An tolino" know them (`is_imported`). They are also entered in the
account's registry of sent books (cloud id = the purchase's publication id), so the reading-progress sync (tolino_sync.py) treats them like
any other book of this account: same file in Audiobookshelf and on the device, so positions map exactly.
Needs the "update" and "upload" permissions of the shared Audiobookshelf user (never admin).

Audiobooks (MP3) have their own two switches, "Hörbücher" and "Hörspiele": the shop does not say which is which, so a title counts as a
Hörspiel when three or more readers are listed or the text says "Hörspiel" (`classify_audio`), otherwise as a Hörbuch. They are loaded track
by track the way the web reader does (bridge: /audiobooks/{id}, /purchases/{id}/track/{n}) into a temporary folder, uploaded in one go
(Audiobookshelf's upload takes many files; the shop's file names keep the order) into the library named like "Hörbücher" / "Hörspiele"
and tagged like ebooks. One audiobook per run (they are big). Not registered for the progress sync yet."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

import aiohttp

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import AbsClient, TolinoBridgeClient, UpstreamError, get_config
from .const import (
    CONF_IMPORT_TOLINO,
    CONF_IMPORT_TOLINO_AUDIOBOOKS,
    CONF_IMPORT_TOLINO_RADIOPLAYS,
    CONF_NOTIFY_SERVICE,
    DEFAULT_TOLINO_ACCOUNT,
    DOMAIN,
)
from .notify_helper import async_push
from .tags import async_tag_item, person_tag
from .tolino_registry import async_ensure_registry
from .tolino_send import _bridge_error
from .users import get_users, tolino_config, tolino_user_id
from .wishes import _norm

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "books_tolino_import"
MAX_PER_RUN = 3
MAX_AUDIO_PER_RUN = 1
MAX_EPUB_BYTES = 300 * 1024 * 1024
MAX_TRACK_BYTES = 400 * 1024 * 1024
FIND_TRIES = 8                      # Audiobookshelf's folder watcher needs a moment to create the item after an upload
FIND_TRIES_AUDIO = 25               # ... and more for dozens of audio files
FIND_WAIT_S = 3
AUDIO_KINDS = {"audiobook", "radioplay"}
TMP_DIR = "books_import_tmp"
CLOCK_SKEW_MS = 2 * 60 * 1000
# Book-specific: trying again changes nothing. Everything else (bridge/Thalia/Audiobookshelf trouble) stops the run and is retried.
PERMANENT = {"drm", "not_epub", "too_large", "bad_download_info", "no_tracks", "not_audio", "bad_audiobook_info"}
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


_LIBRARY_NAMES = {
    "ebook": {"ebook", "ebooks"},
    "audiobook": {"hörbuch", "hörbücher", "hoerbuch", "hoerbuecher", "audiobook", "audiobooks"},
    "radioplay": {"hörspiel", "hörspiele", "hoerspiel", "hoerspiele", "radioplay", "radioplays"},
}
KIND_LABEL = {"audiobook": "Hörbuch", "radioplay": "Hörspiel"}


def target_library(libraries: list[dict], kind: str = "ebook") -> dict | None:
    """The library for `kind`, found by its name ("eBooks", "Hörbücher", "Hörspiele"). Only ebooks fall back to the one and only book library."""
    books = [lib for lib in libraries if lib.get("mediaType") == "book"]
    named = [lib for lib in books if re.sub(r"[^a-zäöüß]", "", (lib.get("name") or "").lower()) in _LIBRARY_NAMES[kind]]
    if named:
        return named[0]
    return books[0] if kind == "ebook" and len(books) == 1 else None


def classify_audio(book: dict, info: dict) -> str:
    """"radioplay" (Hörspiel) or "audiobook" (Hörbuch). The shop has no such field: three or more listed readers, or the word Hörspiel in the
    title, subtitle, blurb or keywords, make it a Hörspiel; anything else is a Hörbuch."""
    text = " ".join([book.get("title") or "", book.get("subtitle") or "", book.get("abstract") or "", " ".join(book.get("keywords") or [])])
    if re.search(r"h(?:ö|oe|o)rspiel", text, re.I) or len(info.get("readers") or []) >= 3:
        return "radioplay"
    return "audiobook"


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

    @staticmethod
    def _kinds(cfg: dict) -> set[str]:
        """What this person wants imported: any of "ebook", "audiobook" (Hörbücher), "radioplay" (Hörspiele)."""
        return {k for k, key in (("ebook", CONF_IMPORT_TOLINO), ("audiobook", CONF_IMPORT_TOLINO_AUDIOBOOKS), ("radioplay", CONF_IMPORT_TOLINO_RADIOPLAYS))
                if cfg.get(key)}

    @property
    def enabled(self) -> bool:
        cfg = tolino_config(self._hass, self.account)
        return bool(self._kinds(cfg)) and TolinoBridgeClient(self._hass, cfg, self.account).configured

    def is_imported(self, item_id: str) -> bool:
        return self.imported_at(item_id) is not None

    def imported_at(self, item_id: str) -> str | None:
        return next((v.get("at") or "" for v in self.state["done"].values() if v.get("item_id") == item_id), None)

    async def _register(self, item_id: str, publication_id: str) -> None:
        """Enter the book in the account's registry of books that are on both sides (what the progress sync works from)."""
        registry = await async_ensure_registry(self._hass, self.account)
        if not registry.get(item_id):
            await registry.async_set(item_id, publication_id, "imported.epub")

    async def async_start(self) -> None:
        """Load the state; a new owner of the account starts from scratch (their cloud is a different one)."""
        self.state.update(await self._store.async_load() or {})
        self.state.setdefault("done", {})
        self.state.setdefault("failed", {})
        self.state.setdefault("audio_kind", {})
        owner = tolino_user_id(self._hass, self.account)
        kinds = sorted(self._kinds(tolino_config(self._hass, self.account))) if self.enabled else []
        added = bool(set(kinds) - set(self.state.get("kinds") or []))      # a kind that was not on before (ebooks, Hörbücher, Hörspiele)
        if self.enabled and (not self.state.get("active") or self.state.get("owner") != owner):
            fresh = self.state.get("owner") != owner
            self.state.update(active=True, owner=owner, kinds=kinds, **({"done": {}, "failed": {}, "audio_kind": {}} if fresh else {"failed": {}}))
            _LOGGER.info("books: tolino import switched on for account %s", self.account)
            await self._store.async_save(self.state)
            self._hass.async_create_task(self.async_tick())             # do not wait for the next interval
        elif self.enabled and added:
            self.state.update(kinds=kinds, failed={})
            _LOGGER.info("books: tolino import for account %s now also takes %s", self.account, ", ".join(kinds))
            await self._store.async_save(self.state)
            self._hass.async_create_task(self.async_tick())             # a switch turned on while another one was already on: start now as well
        elif not self.enabled and self.state.get("active"):
            self.state.update(active=False, kinds=[])
            await self._store.async_save(self.state)
        elif self.state.get("kinds") != kinds and self.enabled:
            self.state["kinds"] = kinds                                  # a kind was switched off: remember it, nothing to start
            await self._store.async_save(self.state)
        for pid, entry in self.state["done"].items():                    # books imported before they were registered
            if entry.get("how") == "imported" and entry.get("item_id") and entry.get("media", "ebook") == "ebook":
                await self._register(entry["item_id"], pid)

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
        summary = {"imported": [], "already": [], "failed": [], "left": 0, "audio": {}}
        cfg = tolino_config(self._hass, self.account)
        kinds = self._kinds(cfg)
        bridge = TolinoBridgeClient(self._hass, cfg, self.account)
        abs_client = AbsClient(self._hass, get_config(self._hass))        # the shared user: it holds the update/upload permissions
        owner = get_users(self._hass).get(tolino_user_id(self._hass, self.account) or "", {})
        tag = person_tag(owner)
        ctx: dict = {}                                                    # what is read from Audiobookshelf is read once per run
        if "ebook" in kinds:
            await self._run_ebooks(bridge, abs_client, tag, summary, ctx)
        if kinds & AUDIO_KINDS:
            await self._run_audio(bridge, abs_client, tag, summary, ctx, kinds)
        return self._finish(summary, owner)

    async def _load_abs(self, abs_client: AbsClient, ctx: dict) -> None:
        if "known" not in ctx:
            ctx["libraries"] = (await abs_client.get("/libraries") or {}).get("libraries", [])
            ctx["known"] = await self._known_titles(abs_client, [lib["id"] for lib in ctx["libraries"] if lib.get("mediaType") == "book"])

    async def _run_ebooks(self, bridge: TolinoBridgeClient, abs_client: AbsClient, tag: str | None, summary: dict, ctx: dict) -> None:
        try:
            books = [b for b in ((await bridge.get("/purchases")) or {}).get("books", []) if b.get("kind") == "purchase"]
        except UpstreamError as exc:
            _LOGGER.warning("books: tolino import cannot list the account's books: %s", _bridge_error(exc)[:2])
            return
        except (aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("books: tolino import cannot reach the bridge: %s", exc)
            return
        todo = [b for b in books if b["publicationId"] not in self.state["done"] and b["publicationId"] not in self.state["failed"]]
        await self._retag(abs_client, tag)
        if not todo:
            return
        try:
            await self._load_abs(abs_client, ctx)
            library = target_library(ctx["libraries"])
            if library is None:
                raise ImportError_("no_library", "no Audiobookshelf library named 'eBooks' (or exactly one book library)")
        except ImportError_ as exc:
            self._alert("books_import_library", "tolino-Import: keine Ziel-Bibliothek", f"{exc.message}. Lege in Audiobookshelf eine Bibliothek „eBooks“ an.")
            return
        except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("books: tolino import cannot read Audiobookshelf: %s", exc)
            return
        known = ctx["known"]
        for n, book in enumerate(todo):
            pid, title = book["publicationId"], book.get("title") or book["publicationId"]
            if _norm(title) in known:
                self.state["done"][pid] = {"title": title, "at": dt_util.utcnow().isoformat(), "how": "already_in_abs", "item_id": None}
                summary["already"].append(title)
                continue
            if len(summary["imported"]) >= MAX_PER_RUN:
                summary["left"] += len(todo) - n
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
            if item_id:
                await self._register(item_id, pid)
            known.add(_norm(title))
            summary["imported"].append(title)
            await self._store.async_save(self.state)                     # after every book: a restart must not repeat uploads

    async def _run_audio(self, bridge: TolinoBridgeClient, abs_client: AbsClient, tag: str | None, summary: dict, ctx: dict, kinds: set[str]) -> None:
        try:
            books = [b for b in ((await bridge.get("/purchases", params={"media": "audiobook"})) or {}).get("books", []) if b.get("kind") == "purchase"]
        except UpstreamError as exc:
            _LOGGER.warning("books: tolino import cannot list the account's audiobooks: %s", _bridge_error(exc)[:2])
            return
        except (aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("books: tolino import cannot reach the bridge: %s", exc)
            return
        todo = [b for b in books if b["publicationId"] not in self.state["done"] and b["publicationId"] not in self.state["failed"]]
        await self._retag(abs_client, tag)
        if not todo:
            return
        try:
            await self._load_abs(abs_client, ctx)
        except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("books: tolino import cannot read Audiobookshelf: %s", exc)
            return
        known, imported = ctx["known"], 0
        for n, book in enumerate(todo):
            pid, title = book["publicationId"], book.get("title") or book["publicationId"]
            if _norm(title) in known:
                self.state["done"][pid] = {"title": title, "at": dt_util.utcnow().isoformat(), "how": "already_in_abs", "item_id": None, "media": "audiobook"}
                summary["already"].append(title)
                continue
            try:
                info = await bridge.get(f"/audiobooks/{pid}")
            except UpstreamError as exc:
                code, detail, _ = _bridge_error(exc)
                if code in PERMANENT:
                    self.state["failed"][pid] = {"title": title, "error": code, "at": int(time.time() * 1000)}
                    summary["failed"].append(title)
                    self._alert(f"books_import_{pid}", "tolino-Import", f"„{title}“ konnte nicht importiert werden: {detail or code}. Es wird nicht erneut versucht.")
                    continue
                _LOGGER.warning("books: tolino import paused at audiobook '%s': %s", title, code)
                break
            except (aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.warning("books: tolino import cannot reach the bridge: %s", exc)
                break
            kind = self.state["audio_kind"].get(pid) or classify_audio(book, info)
            self.state["audio_kind"][pid] = kind
            if kind not in kinds:
                continue                                                  # this person did not switch that kind on: it stays in the cloud (and counts when they do)
            if info.get("title") and _norm(info["title"]) in known:
                self.state["done"][pid] = {"title": title, "at": dt_util.utcnow().isoformat(), "how": "already_in_abs", "item_id": None, "media": kind}
                summary["already"].append(title)
                continue
            if imported >= MAX_AUDIO_PER_RUN:
                summary["left"] += 1
                continue
            library = target_library(ctx["libraries"], kind)
            if library is None:
                self._alert(f"books_import_library_{kind}", f"tolino-Import: keine Bibliothek für {KIND_LABEL[kind]}er",
                            f"In Audiobookshelf gibt es keine Bibliothek „{'Hörbücher' if kind == 'audiobook' else 'Hörspiele'}“. Lege sie an, dann geht es weiter.")
                continue
            _LOGGER.info("books: importing %s '%s' (%d tracks, %d readers) from tolino account %s", KIND_LABEL[kind], title, len(info.get("tracks") or []),
                         len(info.get("readers") or []), self.account)
            try:
                item_id = await self._import_audio(bridge, abs_client, library, book, info, tag)
            except ImportError_ as exc:
                if exc.code in PERMANENT:
                    self.state["failed"][pid] = {"title": title, "error": exc.code, "at": int(time.time() * 1000)}
                    summary["failed"].append(title)
                    self._alert(f"books_import_{pid}", "tolino-Import", f"„{title}“ konnte nicht importiert werden: {exc.message}. Es wird nicht erneut versucht.")
                    continue
                _LOGGER.warning("books: tolino import paused at audiobook '%s': %s", title, exc)
                break
            self.state["done"][pid] = {"title": title, "at": dt_util.utcnow().isoformat(), "how": "imported", "item_id": item_id, "media": kind}
            known.add(_norm(title))
            known.add(_norm(info.get("title") or ""))
            summary["imported"].append(title)
            summary["audio"][title] = kind
            imported += 1
            await self._store.async_save(self.state)

    async def _import_audio(self, bridge: TolinoBridgeClient, abs_client: AbsClient, library: dict, book: dict, info: dict, tag: str | None) -> str | None:
        """Download every track into a temporary folder, upload them together, find the new item and tag it. Raises ImportError_."""
        tracks = info.get("tracks") or []
        if not tracks:
            raise ImportError_("no_tracks", "the audiobook lists no tracks")
        folder = (library.get("folders") or [{}])[0].get("id")
        if not folder:
            raise ImportError_("no_library", "the library has no folder")
        title = info.get("title") or book.get("title") or "Unbekannt"
        author = ", ".join(info.get("authors") or book.get("authors") or []) or "Unbekannt"
        workdir = Path(self._hass.config.path(TMP_DIR)) / re.sub(r"[^A-Za-z0-9_.-]", "_", book["publicationId"])
        await self._hass.async_add_executor_job(shutil.rmtree, workdir, True)
        await self._hass.async_add_executor_job(lambda: workdir.mkdir(parents=True, exist_ok=True))
        handles: list = []
        try:
            files: list[tuple[str, Path]] = []
            used: set[str] = set()
            for track in tracks:
                number = int(track["number"])
                try:
                    data, headers = await bridge.fetch_file(f"/purchases/{book['id']}/track/{number}", max_bytes=MAX_TRACK_BYTES, timeout=900)
                except UpstreamError as exc:
                    code, detail, _ = _bridge_error(exc)
                    raise ImportError_("too_large" if exc.status == 413 else code, f"Spur {number}: {detail or code}") from exc
                except (aiohttp.ClientError, TimeoutError) as exc:
                    raise ImportError_("unreachable", f"Spur {number}: {exc}") from exc
                name = _safe(unquote(headers.get("X-Filename") or ""), f"{number:02d}.mp3")
                if not name[:1].isdigit():
                    name = f"{number:02d}_{name}"                          # Audiobookshelf orders the files by name
                if name in used:
                    name = f"{number:02d}_{name}"
                used.add(name)
                path = workdir / name
                await self._hass.async_add_executor_job(path.write_bytes, data)
                files.append((name, path))
                del data
            started = int(time.time() * 1000) - CLOCK_SKEW_MS
            handles = await self._hass.async_add_executor_job(lambda: [open(path, "rb") for _, path in files])
            form = aiohttp.FormData(quote_fields=False)
            for key, value in (("title", title), ("author", author), ("library", library["id"]), ("folder", folder)):
                form.add_field(key, value)
            for i, ((name, _), handle) in enumerate(zip(files, handles)):
                form.add_field(str(i), handle, filename=name, content_type="audio/mpeg")
            try:
                await abs_client.request("POST", "/upload", data=form, timeout=3600)
            except UpstreamError as exc:
                if exc.status in (401, 403):
                    self._alert("books_import_perm", "tolino-Import: Recht fehlt",
                                "Der Audiobookshelf-Benutzer der Haupteinstellungen braucht die Rechte „Hochladen“ und „Aktualisieren“ (kein Admin).")
                raise ImportError_("abs_refused", f"Audiobookshelf upload: HTTP {exc.status}") from exc
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise ImportError_("abs_unreachable", str(exc)) from exc
            persistent_notification.async_dismiss(self._hass, "books_import_perm")
        finally:
            await self._hass.async_add_executor_job(lambda: [h.close() for h in handles])
            await self._hass.async_add_executor_job(shutil.rmtree, workdir, True)
        item_id = await self._find(abs_client, library["id"], title, started, tries=FIND_TRIES_AUDIO)
        if item_id and tag:
            await self._tag(abs_client, item_id, tag)
        elif not item_id:
            _LOGGER.warning("books: '%s' is uploaded, but the new item was not found yet; it is tagged on a later run", title)
        _LOGGER.info("books: imported audiobook '%s' (%d tracks) from tolino account %s", title, len(tracks), self.account)
        return item_id

    def _finish(self, summary: dict, owner: dict | None = None) -> dict:
        self._hass.async_create_task(self._store.async_save(self.state))
        self.last_run = {"at": dt_util.utcnow(), **{k: len(v) if isinstance(v, list) else v for k, v in summary.items()}}
        if summary["imported"] and owner and owner.get(CONF_NOTIFY_SERVICE):
            label = lambda t: f"„{t}“" + (f" ({KIND_LABEL[summary['audio'][t]]})" if t in summary.get("audio", {}) else "")
            names = ", ".join(label(t) for t in summary["imported"][:5]) + (" …" if len(summary["imported"]) > 5 else "")
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

    async def _find(self, abs_client: AbsClient, library_id: str, title: str, since_ms: int, tries: int = FIND_TRIES) -> str | None:
        for attempt in range(tries):
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
            libraries = (await abs_client.get("/libraries") or {}).get("libraries", [])
        except (UpstreamError, aiohttp.ClientError, TimeoutError):
            return
        for kind in {e.get("media", "ebook") for e in pending.values()}:
            library = target_library(libraries, kind)
            if library is None:
                continue
            try:
                data = await abs_client.get(f"/libraries/{library['id']}/items", params={"sort": "addedAt", "desc": 1, "limit": 60, "minified": 1})
            except (UpstreamError, aiohttp.ClientError, TimeoutError):
                continue
            for pid, entry in pending.items():
                if entry.get("media", "ebook") != kind:
                    continue
                for item in (data or {}).get("results", []):
                    if _norm(((item.get("media") or {}).get("metadata") or {}).get("title", "")) == _norm(entry["title"]):
                        entry["item_id"] = item["id"]
                        if kind == "ebook":
                            await self._register(item["id"], pid)
                        await self._tag(abs_client, item["id"], tag)
                        break

    def _alert(self, notification_id: str, title: str, message: str) -> None:
        _LOGGER.warning("books: %s: %s", title, message)
        persistent_notification.async_create(self._hass, message, title=title, notification_id=notification_id)


def importer_for(hass: HomeAssistant, account: str = DEFAULT_TOLINO_ACCOUNT) -> TolinoImporter | None:
    return (hass.data.get(DOMAIN, {}).get("jobs", {}).get(account) or {}).get("import")
