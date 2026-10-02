"""Kinderschutz: a person can be limited to the books released for them.

How it is enforced: Audiobookshelf itself. The person has their OWN Audiobookshelf user whose tag access is limited (a list of allowed
tags, e.g. "für Lena" and "für alle"); Audiobookshelf then hides everything else - list, covers, files, "continue", search. Books without
an allowed tag are invisible to that user. The integration only makes sure that this really is the case and never lets such a person
fall back to the shared token:

  - saving the person checks the Audiobookshelf user (config_flow), and
  - every Audiobookshelf request of a restricted person is checked again every few minutes (`gate`): if the user is not limited
    (any more), the access is refused ("fail closed") and the administrator gets a notice.

Chaptarr (search, requests, downloads) and Mylar (manga search, downloads) are closed for restricted people altogether: the search shows
every title with its blurb and anyone could request anything. Komga is not touched yet."""
from __future__ import annotations

import logging
import time

import aiohttp

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant

from .api import AbsClient, UpstreamError, get_config
from .const import CONF_ABS_TOKEN, CONF_RESTRICT_BOOKS, DOMAIN
from .users import get_users

_LOGGER = logging.getLogger(__name__)
VERIFY_TTL_S = 300                 # how long a verdict counts
STALE_OK_S = 3600                  # Audiobookshelf unreachable: the last good verdict still counts this long, then access is refused
SERVICES_CLOSED = ("Chaptarr", "Mylar")

RESTRICTED = {"error": "Für dein Konto ist das gesperrt (Kinderschutz). Bücher gibt dir der Verwalter frei.", "code": "restricted"}
UNVERIFIED = {"error": "Die Sperre deines Kontos kann gerade nicht bestätigt werden, deshalb ist die Bibliothek geschlossen. "
                       "Bitte den Verwalter fragen.", "code": "restricted_unverified"}


def limited(me: dict) -> bool:
    """True when this Audiobookshelf user (the answer of /api/me) can only see the books with certain tags."""
    if not isinstance(me, dict) or str(me.get("type") or "").lower() in ("admin", "root"):
        return False
    perms = me.get("permissions") or {}
    return perms.get("accessAllTags") is False and perms.get("selectedTagsNotAccessible") is False and bool(me.get("itemTagsSelected"))


def is_restricted(hass: HomeAssistant, user_id: str | None) -> bool:
    return bool((get_users(hass).get(user_id or "") or {}).get(CONF_RESTRICT_BOOKS))


async def abs_limited(hass: HomeAssistant, token: str) -> bool | None:
    """Is the Audiobookshelf user behind `token` limited to tags? None: cannot tell (unreachable)."""
    if not token:
        return False
    client = AbsClient(hass, {**get_config(hass), CONF_ABS_TOKEN: token})
    try:
        return limited(await client.get("/me"))
    except UpstreamError as exc:
        return False if exc.status in (401, 403) else None
    except (aiohttp.ClientError, TimeoutError):
        return None


async def gate(hass: HomeAssistant, user_id: str | None, service: str) -> dict | None:
    """None: go ahead. Otherwise the JSON body of the 403 for this request."""
    if not is_restricted(hass, user_id):
        return None
    if service in SERVICES_CLOSED:
        return RESTRICTED
    if service != "Audiobookshelf":
        return None
    cache = hass.data[DOMAIN].setdefault("restriction", {})
    now, known = time.time(), cache.get(user_id)
    if known and now - known["at"] < VERIFY_TTL_S:
        return None if known["ok"] else UNVERIFIED
    verdict = await abs_limited(hass, (get_users(hass).get(user_id) or {}).get(CONF_ABS_TOKEN) or "")
    if verdict is None:                                           # cannot tell: an earlier good verdict holds for a while, never longer
        return None if known and known["ok"] and now - known["at"] < STALE_OK_S else UNVERIFIED
    cache[user_id] = {"at": now, "ok": verdict}
    name = (get_users(hass).get(user_id) or {}).get("_name") or user_id
    notice = f"books_restrict_{user_id}"
    if verdict:
        persistent_notification.async_dismiss(hass, notice)
        return None
    _LOGGER.warning("books: the Audiobookshelf user of %s is not limited to tags - access refused", name)
    persistent_notification.async_create(
        hass, f"Der Audiobookshelf-Benutzer von {name} ist nicht auf Tags beschränkt (oder hat keinen eigenen Token). Solange das so ist, "
              "bleibt die Bibliothek für diese Person geschlossen. In Audiobookshelf beim Benutzer „Zugriff auf alle Tags“ ausschalten "
              "und die erlaubten Tags wählen.", title="Bücher: Sperre nicht wirksam", notification_id=notice)
    return UNVERIFIED
