"""Send one Audiobookshelf ebook to the Tolino Cloud through the bridge (used by the card's button and the auto-send job).

Home Assistant fetches the file from Audiobookshelf itself and hands it to the bridge, so neither the file nor the bridge
token ever passes through a browser.
"""
from __future__ import annotations

import json
import logging
import re

import aiohttp

from homeassistant.core import HomeAssistant

from .api import AbsClient, TolinoBridgeClient, UpstreamError
from .users import tolino_config
from .const import (
    DOMAIN,
    EVENT_TOLINO_SENT,
    TOLINO_BRIDGE_TIMEOUT,
    TOLINO_CONVERTIBLE,
    TOLINO_FORMATS,
    TOLINO_MAX_BYTES,
    TOLINO_MAX_COVER_BYTES,
    TOLINO_UPLOAD_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


class SendError(Exception):
    """A send that did not happen; `code` is machine-readable, `status` the HTTP status the card gets."""

    def __init__(self, code: str, message: str, status: int, **extra) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status, self.extra = code, message, status, extra


_ABS_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_CONTENT_TYPES = {"epub": "application/epub+zip", "pdf": "application/pdf"}  # anything else: octet-stream


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
    if exc.status in (415, 422):  # no_converter / convert_failed
        return code, detail, exc.status
    return code, detail, 503 if exc.status == 503 else 502


def _upload_filename(item: dict, ebook_file: dict, fmt: str) -> str:
    name = ((ebook_file.get("metadata") or {}).get("filename") or "").strip()
    if not name.lower().endswith(f".{fmt}"):
        title = ((item.get("media") or {}).get("metadata") or {}).get("title") or "book"
        name = f"{title}.{fmt}"
    return _UNSAFE_FILENAME.sub("_", name)[:180]


async def _still_in_cloud(bridge: TolinoBridgeClient, deliverable_id: str) -> bool:
    """Was the earlier upload deleted in the cloud meanwhile? If we can't tell, assume it is still there."""
    try:
        library = await bridge.get("/library", timeout=TOLINO_UPLOAD_TIMEOUT)
    except (UpstreamError, aiohttp.ClientError, TimeoutError):
        return True
    return any(b.get("deliverableId") == deliverable_id for b in (library or {}).get("books", []))


async def async_send_to_tolino(hass: HomeAssistant, item_id: str, force: bool = False, auto: bool = False,
                               cfg: dict | None = None) -> dict:
    """Upload the ebook of Audiobookshelf item `item_id`. Raises SendError; returns the result dict on success."""
    if not _ABS_ID.match(item_id):
        raise SendError("bad_request", "abs_item_id is required", 400)
    cfg = cfg or tolino_config(hass)
    bridge = TolinoBridgeClient(hass, cfg)
    if not bridge.configured:
        raise SendError("not_configured", "The Tolino bridge is not configured", 503)

    registry = hass.data[DOMAIN]["tolino_sent"]
    prior = registry.get(item_id)
    if prior and not force:
        if await _still_in_cloud(bridge, prior["deliverableId"]):
            raise SendError("already_sent", "This book is already in your Tolino Cloud", 409, sent_at=prior["at"])
        await registry.async_remove(item_id)  # deleted in the cloud since -> a fresh send is fine
        prior = None

    abs_client = AbsClient(hass, cfg)
    try:
        item = await abs_client.get(f"/items/{item_id}")
        ebook_file = ((item or {}).get("media") or {}).get("ebookFile")
        if not ebook_file:
            raise SendError("no_ebook", "This item has no ebook file", 422)
        fmt = str(ebook_file.get("ebookFormat") or "").lower()
        if fmt not in TOLINO_FORMATS | TOLINO_CONVERTIBLE:
            raise SendError("bad_type", f"Tolino Cloud only accepts EPUB and PDF, not '{fmt or 'unknown'}'", 415)
        content = await abs_client.fetch_bytes(
            f"/items/{item_id}/ebook", max_bytes=TOLINO_MAX_BYTES, timeout=TOLINO_UPLOAD_TIMEOUT)
    except UpstreamError as exc:
        if exc.status == 413:
            raise SendError("too_large", "The ebook is larger than 100 MB", 413) from exc
        _LOGGER.warning("books: fetching ebook %s from Audiobookshelf failed: %s", item_id, exc)
        raise SendError("abs_error", f"Audiobookshelf: {exc}", 502) from exc
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise SendError("abs_error", f"Cannot reach Audiobookshelf: {exc}", 502) from exc

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
    form.add_field("file", content, filename=filename, content_type=_CONTENT_TYPES.get(fmt, "application/octet-stream"))
    if cover:
        form.add_field("cover", cover, filename="cover.jpg", content_type="image/jpeg")  # after `file`
    try:
        result = await bridge.request("POST", "/upload", data=form, timeout=TOLINO_BRIDGE_TIMEOUT)
    except UpstreamError as exc:
        code, detail, status = _bridge_error(exc)
        _LOGGER.warning("books: Tolino bridge refused '%s': %s %s", filename, code, detail)
        raise SendError(code, detail or code, status) from exc
    except (aiohttp.ClientError, TimeoutError) as exc:
        _LOGGER.warning("books: Tolino bridge unreachable: %s", exc)
        raise SendError("unreachable", f"Cannot reach the Tolino bridge: {exc}", 503) from exc
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
            # Only the bridge's own `not_found` means "already gone". A bare 404 is an older bridge
            # without DELETE /book/{id}: the old copy is still there.
            replaced = _bridge_error(exc)[0] == "not_found"
            if not replaced:
                _LOGGER.warning("books: could not remove the old Tolino copy of '%s': %s", filename, exc)
        except (aiohttp.ClientError, TimeoutError) as exc:
            replaced = False
            _LOGGER.warning("books: could not remove the old Tolino copy of '%s': %s", filename, exc)
    title = ((item or {}).get("media") or {}).get("metadata", {}).get("title")
    hass.bus.async_fire(EVENT_TOLINO_SENT, {"item_id": item_id, "title": title, "filename": filename,
                                            "deliverable_id": new_id, "replaced": bool(replaced), "auto": auto})
    return {"ok": True, "filename": filename, "deliverableId": new_id, "cover": (result or {}).get("cover"),
            "replaced": replaced, "title": title}
