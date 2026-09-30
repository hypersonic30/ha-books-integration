"""HTTP views bridging the books card to Chaptarr and Audiobookshelf.

Every view requires a Home Assistant login. The card never sees the Chaptarr
API key or the Audiobookshelf token — both are added here, server-side.
Responses are streamed (not buffered) so 30-hour M4B files and EPUBs pass
through with Range support; <audio>/<img> tags reach these GET views through
Home Assistant signed paths (?authSig=…), since they can't send a bearer header.
"""
from __future__ import annotations

import copy
import json
import logging
import re

import aiohttp
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AbsClient, ChaptarrClient, TolinoBridgeClient, UpstreamError, get_config
from .const import (
    CHAPTARR_ALLOWED_COMMANDS,
    CHAPTARR_BLOCKED_SEGMENTS,
    CONF_ABS_TOKEN,
    CONF_ABS_URL,
    CONF_CHAPTARR_API_KEY,
    CONF_CHAPTARR_URL,
    CONF_DEBUG_LOGGING,
    CONF_VERIFY_SSL,
    DOMAIN,
    PASSTHROUGH_REQUEST_HEADERS,
    PASSTHROUGH_RESPONSE_HEADERS,
    SLOW_REQUEST_TIMEOUT,
    TOLINO_FORMATS,
    TOLINO_MAX_BYTES,
    TOLINO_MAX_COVER_BYTES,
    TOLINO_UPLOAD_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

# Paths whose Chaptarr calls fan out to every indexer / metadata provider.
_SLOW_CHAPTARR_PREFIXES = ("release", "search", "book/lookup", "author/lookup")


class _ProxyBase(HomeAssistantView):
    requires_auth = True
    service = ""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request, path: str) -> web.StreamResponse:
        return await self._handle(request, path, "GET")

    async def post(self, request: web.Request, path: str) -> web.StreamResponse:
        return await self._handle(request, path, "POST")

    async def put(self, request: web.Request, path: str) -> web.StreamResponse:
        return await self._handle(request, path, "PUT")

    async def patch(self, request: web.Request, path: str) -> web.StreamResponse:
        return await self._handle(request, path, "PATCH")

    async def delete(self, request: web.Request, path: str) -> web.StreamResponse:
        return await self._handle(request, path, "DELETE")

    async def _handle(self, request: web.Request, path: str, method: str) -> web.StreamResponse:
        cfg = get_config(self._hass)
        try:
            return await self._route(request, path, method, cfg)
        except aiohttp.ClientConnectorError as exc:
            _LOGGER.error("books %s proxy cannot connect [%s %s]: %s", self.service, method, path, exc)
            return web.json_response({"error": f"Cannot connect to {self.service}: {exc}"}, status=503)
        except TimeoutError:
            return web.json_response({"error": f"{self.service} did not answer in time"}, status=504)
        except (ConnectionResetError, aiohttp.ClientPayloadError):
            # Browser went away mid-stream (seeking, closing the player) — normal.
            raise
        except Exception as exc:  # noqa: BLE001 - never let the proxy 500 opaquely
            _LOGGER.error("books %s proxy error [%s %s]: %s", self.service, method, path, exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _route(self, request, path, method, cfg) -> web.StreamResponse:
        raise NotImplementedError

    async def _stream(
        self,
        request: web.Request,
        method: str,
        url: str,
        auth_headers: dict,
        cfg: dict,
        timeout: float | None,
    ) -> web.StreamResponse:
        headers = dict(auth_headers)
        for name in PASSTHROUGH_REQUEST_HEADERS:
            if name in request.headers:
                headers[name] = request.headers[name]
        body = await request.read() if request.can_read_body else b""
        if body:
            headers["Content-Type"] = request.headers.get("Content-Type", "application/json")
        # authSig belongs to Home Assistant's signed-path auth, never forward it.
        params = [(k, v) for k, v in request.query.items() if k != "authSig"]

        if cfg.get(CONF_DEBUG_LOGGING):
            _LOGGER.debug("books %s proxy -> %s %s", self.service, method, url)

        session = async_get_clientsession(self._hass, verify_ssl=cfg.get(CONF_VERIFY_SSL, True))
        async with session.request(
            method,
            url,
            headers=headers,
            params=params,
            data=body or None,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=timeout, sock_connect=10),
        ) as upstream:
            response = web.StreamResponse(status=upstream.status)
            for name in PASSTHROUGH_RESPONSE_HEADERS:
                if name in upstream.headers:
                    response.headers[name] = upstream.headers[name]
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await response.write(chunk)
            await response.write_eof()
            return response


class ChaptarrProxyView(_ProxyBase):
    """/api/books/chaptarr/{path} -> Chaptarr /api/v1/{path}."""

    url = "/api/books/chaptarr/{path:.*}"
    name = "api:books:chaptarr"
    service = "Chaptarr"

    async def _route(self, request, path, method, cfg):
        base = cfg.get(CONF_CHAPTARR_URL, "").rstrip("/")
        if not base:
            return web.json_response({"error": "Chaptarr is not configured"}, status=503)
        segment = path.strip("/").split("/", 1)[0].split("?", 1)[0].lower()
        if segment in CHAPTARR_BLOCKED_SEGMENTS:
            return web.json_response(
                {"error": f"'{segment}' is managed in Chaptarr's own settings, not through Home Assistant"},
                status=403,
            )
        if segment == "command" and method == "POST":
            try:
                command = json.loads(await request.read() or b"{}")
            except ValueError:
                return web.json_response({"error": "Invalid JSON"}, status=400)
            if command.get("name") not in CHAPTARR_ALLOWED_COMMANDS:
                return web.json_response(
                    {"error": f"Command '{command.get('name')}' is not allowed through Home Assistant"},
                    status=403,
                )
        timeout = SLOW_REQUEST_TIMEOUT if path.lower().startswith(_SLOW_CHAPTARR_PREFIXES) else 30
        return await self._stream(
            request, method, f"{base}/api/v1/{path}",
            {"X-Api-Key": cfg.get(CONF_CHAPTARR_API_KEY, "")}, cfg, timeout,
        )


class ChaptarrMediaView(_ProxyBase):
    """/api/books/chaptarr-media/{path} -> Chaptarr's cached cover images."""

    url = "/api/books/chaptarr-media/{path:.*}"
    name = "api:books:chaptarr-media"
    service = "Chaptarr"

    async def _route(self, request, path, method, cfg):
        base = cfg.get(CONF_CHAPTARR_URL, "").rstrip("/")
        if method != "GET" or not path.startswith(("MediaCover/", "MediaCoverProxy/")):
            return web.json_response({"error": "Not found"}, status=404)
        return await self._stream(
            request, "GET", f"{base}/{path}",
            {"X-Api-Key": cfg.get(CONF_CHAPTARR_API_KEY, "")}, cfg, 30,
        )


class AbsProxyView(_ProxyBase):
    """/api/books/abs/{path} -> Audiobookshelf /api/{path} (streams audio/EPUB with Range)."""

    url = "/api/books/abs/{path:.*}"
    name = "api:books:abs"
    service = "Audiobookshelf"

    async def _route(self, request, path, method, cfg):
        base = cfg.get(CONF_ABS_URL, "").rstrip("/")
        if not base:
            return web.json_response({"error": "Audiobookshelf is not configured"}, status=503)
        # Media files can be many hours long; only bound the connect phase.
        timeout = None if "/file/" in path or path.endswith(("/ebook", "/download")) else 30
        return await self._stream(
            request, method, f"{base}/api/{path}",
            {"Authorization": f"Bearer {cfg.get(CONF_ABS_TOKEN, '')}"}, cfg, timeout,
        )


def build_book_payload(book: dict, media_type: str, root: dict, search: bool) -> dict:
    """Chaptarr POST /book payload that monitors ONLY this book for this media type.

    Mirrors Chaptarr's own getNewBook/getNewAuthor with "Only This Book", but
    sets the author gate, current-book seed and new-item policy explicitly for
    the one media type being added — Chaptarr's dialog silently falls back to
    "All books" for the second media type when both are added at once.
    Both the current (monitorNewItems) and the older (monitorExisting/
    monitorFuture) field names are sent, so it works across Chaptarr versions.
    """
    new_book = copy.deepcopy(book)
    author = new_book.get("author") or {}
    prefix = "audiobook" if media_type == "audiobook" else "ebook"
    other = "ebook" if prefix == "audiobook" else "audiobook"
    book_to_monitor = (
        new_book.get("foreignId") or new_book.get("foreignBookId")
        or new_book.get("hardcoverBookId") or new_book.get("goodreadsBookId")
    )

    author["addOptions"] = {
        "monitor": "specificBook",
        "booksToMonitor": [book_to_monitor],
        "searchForMissingBooks": False,
    }
    author["metadataProfileId"] = root["metadataProfileId"]
    author[f"{prefix}QualityProfileId"] = root["qualityProfileId"]
    author[f"{prefix}MetadataProfileId"] = root["metadataProfileId"]
    author[f"{prefix}RootFolderPath"] = root["path"]
    author[f"{prefix}Monitored"] = True
    author[f"{prefix}MonitorNewItems"] = "none"
    author[f"{prefix}MonitorExisting"] = 2  # legacy: author monitored, no existing books
    author[f"{prefix}MonitorFuture"] = False  # legacy: don't auto-monitor new releases
    new_book["author"] = author

    new_book["id"] = 0
    new_book["localBookId"] = None
    new_book["mediaType"] = media_type
    new_book[f"{prefix}Monitored"] = True
    new_book[f"{other}Monitored"] = False
    new_book["monitored"] = True
    new_book["addOptions"] = {**(new_book.get("addOptions") or {}), "searchForNewBook": search}
    editions = new_book.get("editions") or []
    if editions:
        if not any(e.get("monitored") for e in editions):
            editions[0]["monitored"] = True
            editions[0]["manualAdd"] = False
    else:
        new_book["editions"] = [{
            "monitored": True, "manualAdd": False,
            "title": new_book.get("title"), "overview": new_book.get("overview") or "",
        }]
    return new_book


def default_root_folders(root_folders: list[dict]) -> dict[str, dict]:
    """Effective default root folder + profiles per media type."""
    result: dict[str, dict] = {}
    for rf in root_folders:
        for media_type, flag in (("audiobook", "isEffectiveDefaultAudiobook"), ("ebook", "isEffectiveDefaultEbook")):
            if not rf.get(flag):
                continue
            nested = rf.get(media_type) or {}
            result[media_type] = {
                "path": rf["path"],
                "qualityProfileId": nested.get("qualityProfileId") or rf.get(f"{media_type}QualityProfileId"),
                "metadataProfileId": nested.get("metadataProfileId") or rf.get(f"{media_type}MetadataProfileId"),
            }
    return result


class AddBookView(HomeAssistantView):
    """POST /api/books/add {book, media_types: ["ebook","audiobook"], search: true}."""

    url = "/api/books/add"
    name = "api:books:add"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        book = data.get("book")
        media_types = [m for m in data.get("media_types", []) if m in ("audiobook", "ebook")]
        if not isinstance(book, dict) or not media_types:
            return web.json_response({"error": "book and media_types are required"}, status=400)

        client = ChaptarrClient(self._hass, get_config(self._hass))
        try:
            roots = default_root_folders(await client.get("/rootfolder"))
        except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
            return web.json_response({"error": f"Chaptarr: {exc}"}, status=502)

        results = []
        # Audiobook first, like Chaptarr's own dialog: the first add creates the author.
        for media_type in sorted(media_types):
            root = roots.get(media_type)
            if root is None:
                results.append({"media_type": media_type, "ok": False,
                                "error": f"No default {media_type} root folder in Chaptarr"})
                continue
            payload = build_book_payload(book, media_type, root, bool(data.get("search", True)))
            try:
                added = await client.post("/book", payload, timeout=SLOW_REQUEST_TIMEOUT)
                results.append({"media_type": media_type, "ok": True,
                                "id": (added or {}).get("id"), "title": (added or {}).get("title")})
            except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.warning("books: adding %s '%s' failed: %s", media_type, book.get("title"), exc)
                results.append({"media_type": media_type, "ok": False, "error": str(exc)})

        # Chaptarr accepts addOptions.searchForNewBook on POST /book but does
        # not act on it (verified against 0.9.x: only DownloadAuthorMedia runs),
        # so search explicitly for exactly the books that were just added.
        added_ids = [r["id"] for r in results if r["ok"] and r.get("id")]
        search_started = False
        if data.get("search", True) and added_ids:
            try:
                await client.post("/command", {"name": "BookSearch", "bookIds": added_ids})
                search_started = True
            except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.warning("books: search for %s could not be started: %s", added_ids, exc)
        status = 200 if all(r["ok"] for r in results) else 207
        return web.json_response({"results": results, "search_started": search_started}, status=status)
        return web.json_response({"results": results}, status=status)


_ABS_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_CONTENT_TYPES = {"epub": "application/epub+zip", "pdf": "application/pdf"}


def _bridge_error(exc: UpstreamError) -> tuple[str, str, int]:
    """(code, detail, http status) from a tolino-bridge error body."""
    try:
        body = json.loads(exc.message)
        code, detail = str(body.get("error") or "bridge"), str(body.get("detail") or "")
    except (ValueError, AttributeError):
        code, detail = "bridge", exc.message
    if exc.status == 401:
        return "bridge_auth", "The bridge rejected the token", 502
    if exc.status == 400:
        return code, detail, 415 if code == "bad_type" else 400
    return code, detail, 503 if exc.status == 503 else 502


def _upload_filename(item: dict, ebook_file: dict, fmt: str) -> str:
    name = ((ebook_file.get("metadata") or {}).get("filename") or "").strip()
    if not name.lower().endswith(f".{fmt}"):
        title = ((item.get("media") or {}).get("metadata") or {}).get("title") or "book"
        name = f"{title}.{fmt}"
    return _UNSAFE_FILENAME.sub("_", name)[:180]


class TolinoView(HomeAssistantView):
    """GET /api/books/tolino (bridge status) and POST {abs_item_id} (send the ebook to the Tolino Cloud).

    Home Assistant fetches the file from Audiobookshelf itself and hands it to
    the bridge, so the browser never handles the file or the bridge token.
    """

    url = "/api/books/tolino"
    name = "api:books:tolino"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        bridge = TolinoBridgeClient(self._hass, get_config(self._hass))
        if not bridge.configured:
            return web.json_response({"enabled": False})
        try:
            status = await bridge.get("/status")
        except UpstreamError as exc:
            code, detail, _ = _bridge_error(exc)
            return web.json_response({"enabled": True, "reachable": True, "logged_in": False,
                                      "error": code, "detail": detail})
        except (aiohttp.ClientError, TimeoutError) as exc:
            return web.json_response({"enabled": True, "reachable": False, "logged_in": False,
                                      "error": "unreachable", "detail": str(exc)})
        return web.json_response({
            "enabled": True, "reachable": True, "logged_in": bool(status.get("logged_in")),
            "error": status.get("last_error"), "login_backoff_s": status.get("login_backoff_s", 0),
            "sent": self._hass.data[DOMAIN]["tolino_sent"].public(),
        })

    @staticmethod
    async def _still_in_cloud(bridge: TolinoBridgeClient, deliverable_id: str) -> bool:
        """Was the earlier upload deleted in the cloud meanwhile? If we can't tell, assume it is still there."""
        try:
            library = await bridge.get("/library", timeout=TOLINO_UPLOAD_TIMEOUT)
        except (UpstreamError, aiohttp.ClientError, TimeoutError):
            return True
        return any(b.get("deliverableId") == deliverable_id for b in (library or {}).get("books", []))

    async def post(self, request: web.Request) -> web.Response:
        cfg = get_config(self._hass)
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "Invalid JSON", "code": "bad_request"}, status=400)
        item_id = data.get("abs_item_id") if isinstance(data, dict) else None
        if not isinstance(item_id, str) or not _ABS_ID.match(item_id):
            return web.json_response({"error": "abs_item_id is required", "code": "bad_request"}, status=400)
        bridge = TolinoBridgeClient(self._hass, cfg)
        if not bridge.configured:
            return web.json_response({"error": "The Tolino bridge is not configured", "code": "not_configured"},
                                     status=503)

        registry = self._hass.data[DOMAIN]["tolino_sent"]
        force = data.get("force") is True
        prior = registry.get(item_id)
        if prior and not force:
            if await self._still_in_cloud(bridge, prior["deliverableId"]):
                return web.json_response(
                    {"error": "This book is already in your Tolino Cloud", "code": "already_sent", "sent_at": prior["at"]},
                    status=409)
            await registry.async_remove(item_id)  # deleted in the cloud since -> a fresh send is fine
            prior = None

        abs_client = AbsClient(self._hass, cfg)
        try:
            item = await abs_client.get(f"/items/{item_id}")
            ebook_file = ((item or {}).get("media") or {}).get("ebookFile")
            if not ebook_file:
                return web.json_response({"error": "This item has no ebook file", "code": "no_ebook"}, status=422)
            fmt = str(ebook_file.get("ebookFormat") or "").lower()
            if fmt not in TOLINO_FORMATS:
                return web.json_response(
                    {"error": f"Tolino Cloud only accepts EPUB and PDF, not '{fmt or 'unknown'}'", "code": "bad_type"},
                    status=415)
            content = await abs_client.fetch_bytes(
                f"/items/{item_id}/ebook", max_bytes=TOLINO_MAX_BYTES, timeout=TOLINO_UPLOAD_TIMEOUT)
        except UpstreamError as exc:
            if exc.status == 413:
                return web.json_response({"error": "The ebook is larger than 100 MB", "code": "too_large"}, status=413)
            _LOGGER.warning("books: fetching ebook %s from Audiobookshelf failed: %s", item_id, exc)
            return web.json_response({"error": f"Audiobookshelf: {exc}", "code": "abs_error"}, status=502)
        except (aiohttp.ClientError, TimeoutError) as exc:
            return web.json_response({"error": f"Cannot reach Audiobookshelf: {exc}", "code": "abs_error"}, status=502)

        filename = _upload_filename(item, ebook_file, fmt)
        # Tolino shows a generated placeholder for uploads, so hand the bridge Audiobookshelf's cover.
        # Nice to have: without it the book still goes up.
        try:
            cover = await abs_client.fetch_bytes(
                f"/items/{item_id}/cover?format=jpeg", max_bytes=TOLINO_MAX_COVER_BYTES, timeout=30)
        except (UpstreamError, aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.info("books: no cover for %s (%s), uploading without", item_id, exc)
            cover = None
        # quote_fields=False: aiohttp would percent-encode the filename (Dämmerung -> D%C3%A4mmerung)
        # and aiohttp servers don't decode it. Quotes/backslashes/control chars are already stripped.
        form = aiohttp.FormData(quote_fields=False)
        form.add_field("file", content, filename=filename, content_type=_CONTENT_TYPES[fmt])
        if cover:
            form.add_field("cover", cover, filename="cover.jpg", content_type="image/jpeg")  # after `file`
        try:
            result = await bridge.request("POST", "/upload", data=form, timeout=TOLINO_UPLOAD_TIMEOUT)
        except UpstreamError as exc:
            code, detail, status = _bridge_error(exc)
            _LOGGER.warning("books: Tolino bridge refused '%s': %s %s", filename, code, detail)
            return web.json_response({"error": detail or code, "code": code}, status=status)
        except (aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("books: Tolino bridge unreachable: %s", exc)
            return web.json_response({"error": f"Cannot reach the Tolino bridge: {exc}", "code": "unreachable"},
                                     status=503)
        _LOGGER.info("books: sent '%s' to the Tolino Cloud", filename)
        new_id = (result or {}).get("deliverableId")
        if new_id:
            await registry.async_set(item_id, new_id, filename)
        replaced = None
        if prior and new_id and prior["deliverableId"] != new_id:
            # Replace, don't duplicate: the new copy is safely up, now drop the old one.
            try:
                await bridge.request("DELETE", f"/book/{prior['deliverableId']}", timeout=60)
                replaced = True
            except UpstreamError as exc:
                replaced = exc.status == 404  # already gone counts as replaced
                if not replaced:
                    _LOGGER.warning("books: could not remove the old Tolino copy of '%s': %s", filename, exc)
            except (aiohttp.ClientError, TimeoutError) as exc:
                replaced = False
                _LOGGER.warning("books: could not remove the old Tolino copy of '%s': %s", filename, exc)
        return web.json_response({"ok": True, "filename": filename, "deliverableId": new_id,
                                  "cover": (result or {}).get("cover"), "replaced": replaced})


class RescueStatusView(HomeAssistantView):
    """GET /api/books/rescue — recent import-rescue events for the card."""

    url = "/api/books/rescue"
    name = "api:books:rescue"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        rescue = self._hass.data.get(DOMAIN, {}).get("rescue")
        return web.json_response({
            "enabled": rescue is not None and rescue.enabled,
            "in_progress": sorted(rescue.in_progress) if rescue else [],
            "events": list(rescue.events) if rescue else [],
        })
