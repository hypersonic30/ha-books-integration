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

import aiohttp
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ChaptarrClient, TolinoBridgeClient, UpstreamError, get_config
from .abs_policy import abs_allowed
from .auth_watch import key_accepted, key_rejected
from .chaptarr_policy import chaptarr_allowed
from .komga_policy import komga_allowed
from .mylar_policy import mylar_request
from .tolino_registry import async_ensure_registry
from .restriction import gate, is_restricted
from .tags import person_tag
from .users import NO_PERSON, access_denied, account_for_user, config_for, get_users, tolino_allowed, user_of
from .tolino_send import SendError, async_send_to_tolino
from .const import (
    CHAPTARR_ALLOWED_COMMANDS,
    CONF_ABS_TOKEN,
    CONF_ABS_URL,
    CONF_CHAPTARR_API_KEY,
    CONF_CHAPTARR_URL,
    CONF_DEBUG_LOGGING,
    CONF_KOMGA_API_KEY,
    CONF_KOMGA_URL,
    CONF_MYLAR_API_KEY,
    CONF_MYLAR_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
    PASSTHROUGH_REQUEST_HEADERS,
    PASSTHROUGH_RESPONSE_HEADERS,
    SLOW_REQUEST_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

# Paths whose Chaptarr calls fan out to every indexer / metadata provider.
_SLOW_CHAPTARR_PREFIXES = ("release", "search", "book/lookup", "author/lookup")


def _odd_path(path: str) -> bool:
    """Dot segments, backslashes, NULs and left-over percent signs have no place in the paths the cards use (Home Assistant filters most of
    them already; this closes the rest, e.g. `..\\`)."""
    return any(seg in (".", "..") for seg in path.split("/")) or any(ch in path for ch in ("\\", "\x00", "%"))


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
        if access_denied(self._hass, user_of(request)):
            return web.json_response(NO_PERSON, status=403)
        if (blocked := await gate(self._hass, user_of(request), self.service)) is not None:
            return web.json_response(blocked, status=403)
        if _odd_path(path):
            return web.json_response({"error": "Invalid path"}, status=400)
        cfg = config_for(self._hass, user_of(request))     # the asking person's own Komga key / Audiobookshelf token
        try:
            return await self._route(request, path, method, cfg)
        except aiohttp.ClientConnectionError as exc:  # refused, unreachable, or dropped mid-request (restart)
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
            if upstream.status == 401:
                key_rejected(self._hass, self.service, user_of(request))          # a repair hint: the key was refused
            elif upstream.status < 400:
                key_accepted(self._hass, self.service, user_of(request))
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
        if not chaptarr_allowed(method, path):
            return web.json_response(
                {"error": f"{method} /{path} is not available through Home Assistant (Chaptarr's settings and anything that changes or deletes stay in Chaptarr)"},
                status=403,
            )
        segment = path.strip("/").split("/", 1)[0].lower()
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
        if not abs_allowed(method, path):
            return web.json_response({"error": f"{method} /{path} is not available through Home Assistant"}, status=403)
        # Media files can be many hours long; only bound the connect phase.
        timeout = None if "/file/" in path or path.endswith(("/ebook", "/download")) else 30
        return await self._stream(
            request, method, f"{base}/api/{path}",
            {"Authorization": f"Bearer {cfg.get(CONF_ABS_TOKEN, '')}"}, cfg, timeout,
        )


class KomgaProxyView(_ProxyBase):
    """/api/books/komga/{path} -> Komga /api/{path}, restricted by komga_policy (reading, progress, rescan)."""

    url = "/api/books/komga/{path:.*}"
    name = "api:books:komga"
    service = "Komga"

    async def _route(self, request, path, method, cfg):
        base = (cfg.get(CONF_KOMGA_URL) or "").rstrip("/")
        key = cfg.get(CONF_KOMGA_API_KEY) or ""
        if not base or not key:
            return web.json_response({"error": "Komga is not configured"}, status=503)
        if not komga_allowed(method, path):
            return web.json_response({"error": f"{method} /{path} is not available through Home Assistant"}, status=403)
        # Page images are small, but a book can be hundreds of them: stream, bound only the connect phase.
        return await self._stream(request, method, f"{base}/api/{path.strip('/')}", {"X-API-Key": key}, cfg, 60)


def _json_body(raw: bytes, status: int) -> bytes:
    """Mylar answers some commands (queueIssue, unqueueIssue, ...) with the plain text "OK", and errors as plain text.
    The card parses every answer as JSON, so give it the same envelope Mylar uses everywhere else."""
    try:
        json.loads(raw)
        return raw
    except ValueError:
        text = raw.decode("utf-8", "replace").strip()
        if status < 400:
            return json.dumps({"success": True, "data": text}).encode()
        return json.dumps({"success": False, "error": {"code": status, "message": text[:300] or f"HTTP {status}"}}).encode()


class MylarProxyView(_ProxyBase):
    """/api/books/mylar/{command} -> Mylar /api?cmd={command}&apikey=..., restricted by mylar_policy.

    Mylar reports errors as HTTP 200 + {"success": false}; the answers are small JSON, so they are buffered."""

    url = "/api/books/mylar/{path:.*}"
    name = "api:books:mylar"
    service = "Mylar"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        self._running: set[tuple] = set()

    async def _route(self, request, path, method, cfg):
        base = (cfg.get(CONF_MYLAR_URL) or "").rstrip("/")
        key = cfg.get(CONF_MYLAR_API_KEY) or ""
        if not base or not key:
            return web.json_response({"error": "Mylar is not configured"}, status=503)
        checked = mylar_request(method, path.strip("/"), request.query)
        if checked is None:
            return web.json_response({"error": f"{method} /{path} is not available through Home Assistant"}, status=403)
        params, background = checked
        session = async_get_clientsession(self._hass, verify_ssl=cfg.get(CONF_VERIFY_SSL, True))
        url, upstream_params = f"{base}/api", {**params, "apikey": key}
        if background:
            task_key = tuple(sorted(params.items()))
            if params["cmd"] == "queueIssue":
                await self._hass.data[DOMAIN]["wishes"].async_add("manga", user_of(request), issue=params["id"])
            if task_key not in self._running:  # a second click while the first search runs would only repeat it
                self._running.add(task_key)
                self._hass.async_create_background_task(
                    self._fire(session, url, upstream_params, task_key), f"books_mylar_{params['cmd']}")
            return web.json_response({"success": True, "data": "queued"}, status=202)
        async with session.get(url, params=upstream_params, allow_redirects=False,
                               timeout=aiohttp.ClientTimeout(total=SLOW_REQUEST_TIMEOUT, sock_connect=10)) as upstream:
            raw = await upstream.read()
            try:
                answer = json.loads(raw)
            except ValueError:
                answer = None
            if isinstance(answer, dict) and answer.get("success") is False and (answer.get("error") or {}).get("code") == 460:
                key_rejected(self._hass, self.service, user_of(request))          # Mylar: "Missing API key" / wrong key
            elif upstream.status < 400:
                key_accepted(self._hass, self.service, user_of(request))
            return web.Response(status=upstream.status, body=_json_body(raw, upstream.status),
                                content_type="application/json", charset="utf-8")

    async def _fire(self, session, url, params, task_key) -> None:
        try:
            async with session.get(url, params=params, allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=1800, sock_connect=10)) as upstream:
                await upstream.read()
        except Exception as exc:  # noqa: BLE001 - nobody is waiting for this answer
            _LOGGER.warning("books Mylar background %s failed: %s", params.get("cmd"), exc)
        finally:
            self._running.discard(task_key)


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
        if access_denied(self._hass, user_of(request)):
            return web.json_response(NO_PERSON, status=403)
        if (blocked := await gate(self._hass, user_of(request), "Chaptarr")) is not None:
            return web.json_response(blocked, status=403)
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
        if any(r["ok"] for r in results):
            author = book.get("author") if isinstance(book.get("author"), dict) else {}
            await self._hass.data[DOMAIN]["wishes"].async_add(
                "book", user_of(request), title=str(book.get("title") or ""),
                author=str(author.get("authorName") or book.get("authorTitle") or ""))
        status = 200 if all(r["ok"] for r in results) else 207
        return web.json_response({"results": results, "search_started": search_started}, status=status)


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
        if access_denied(self._hass, user_of(request)):
            return web.json_response(NO_PERSON, status=403)
        account = account_for_user(self._hass, user_of(request))
        bridge = TolinoBridgeClient(self._hass, get_config(self._hass), account)
        if not bridge.configured or not tolino_allowed(self._hass, user_of(request)):
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
            "sent": (await async_ensure_registry(self._hass, account)).public(),
        })

    async def post(self, request: web.Request) -> web.Response:
        if access_denied(self._hass, user_of(request)):
            return web.json_response(NO_PERSON, status=403)
        if not tolino_allowed(self._hass, user_of(request)):
            return web.json_response({"error": "No Tolino is set up for your account", "code": "no_tolino"}, status=403)
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "Invalid JSON", "code": "bad_request"}, status=400)
        item_id = data.get("abs_item_id") if isinstance(data, dict) else None
        if not isinstance(item_id, str):
            return web.json_response({"error": "abs_item_id is required", "code": "bad_request"}, status=400)
        try:
            result = await async_send_to_tolino(self._hass, item_id, force=data.get("force") is True,
                                                cfg=config_for(self._hass, user_of(request)),
                                                account=account_for_user(self._hass, user_of(request)))
        except SendError as exc:
            return web.json_response({"error": exc.message, "code": exc.code, **exc.extra}, status=exc.status)
        result.pop("title", None)
        return web.json_response(result)


class TolinoSyncView(HomeAssistantView):
    """POST /api/books/tolino-sync - run the reading-progress import now (same job the timer runs)."""

    url = "/api/books/tolino-sync"
    name = "api:books:tolino-sync"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        if access_denied(self._hass, user_of(request)):
            return web.json_response(NO_PERSON, status=403)
        if not tolino_allowed(self._hass, user_of(request)):
            return web.json_response({"error": "No Tolino is set up for your account", "code": "no_tolino"}, status=403)
        sync = self._hass.data[DOMAIN]["jobs"].get(account_for_user(self._hass, user_of(request)), {}).get("progress_sync")
        if sync is None or not sync.enabled:
            return web.json_response({"error": "Progress sync is switched off or no Tolino bridge is configured",
                                      "code": "sync_disabled"}, status=409)
        return web.json_response(await sync.async_sync())


class TolinoAutoSendView(HomeAssistantView):
    """POST /api/books/tolino-autosend - run the auto-send job now (same job the timer runs)."""

    url = "/api/books/tolino-autosend"
    name = "api:books:tolino-autosend"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        if access_denied(self._hass, user_of(request)):
            return web.json_response(NO_PERSON, status=403)
        if not tolino_allowed(self._hass, user_of(request)):
            return web.json_response({"error": "No Tolino is set up for your account", "code": "no_tolino"}, status=403)
        job = self._hass.data[DOMAIN]["jobs"].get(account_for_user(self._hass, user_of(request)), {}).get("auto_send")
        if job is None or not job.enabled:
            return web.json_response({"error": "Auto-send is switched off or no Tolino bridge is configured",
                                      "code": "autosend_disabled"}, status=409)
        return web.json_response(await job.async_run())


class PeopleView(HomeAssistantView):
    """GET /api/books/people — who the library chips are for: every person with their tag, and which one is the asker."""

    url = "/api/books/people"
    name = "api:books:people"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        me = user_of(request)
        if access_denied(self._hass, me):
            return web.json_response(NO_PERSON, status=403)
        restricted = is_restricted(self._hass, me)
        people = [{"name": p["_name"], "tag": tag, "me": uid == me}
                  for uid, p in get_users(self._hass).items() if (tag := person_tag(p)) and (uid == me or not restricted)]
        return web.json_response({"people": people, "restricted": restricted})   # restricted: only themselves, the card closes search/downloads


class RescueStatusView(HomeAssistantView):
    """GET /api/books/rescue — recent import-rescue events for the card."""

    url = "/api/books/rescue"
    name = "api:books:rescue"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        rescue = self._hass.data.get(DOMAIN, {}).get("rescue")
        if is_restricted(self._hass, user_of(request)):                    # the events carry the titles of what Chaptarr imports
            return web.json_response({"enabled": False, "in_progress": [], "events": []})
        return web.json_response({
            "enabled": rescue is not None and rescue.enabled,
            "in_progress": sorted(rescue.in_progress) if rescue else [],
            "events": list(rescue.events) if rescue else [],
        })
