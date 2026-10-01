"""Per-person accounts. Every person gets a config subentry: a Home Assistant user plus their own Komga key, Audiobookshelf
token, notify target and (optionally) the Tolino bridge. The proxies pick the account of whoever is asking, so reading
progress and bookmarks are never shared; anybody without a subentry keeps using the shared account from the main settings."""
from __future__ import annotations

from homeassistant.components.http import KEY_HASS_USER
from homeassistant.core import HomeAssistant

from .api import get_config
from .const import (
    CONF_ABS_TOKEN,
    CONF_HA_USER,
    CONF_KOMGA_API_KEY,
    CONF_TOLINO_TOKEN,
    CONF_TOLINO_URL,
    CONF_USER_TOLINO,
    DOMAIN,
    SUBENTRY_USER,
)


def users_from_entry(entry) -> dict[str, dict]:
    """HA user id -> that person's settings, in the order they were added."""
    return {
        sub.data[CONF_HA_USER]: dict(sub.data)
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_USER and sub.data.get(CONF_HA_USER)
    }


def get_users(hass: HomeAssistant) -> dict[str, dict]:
    return hass.data.get(DOMAIN, {}).get("users", {})


def user_of(request) -> str | None:
    user = request.get(KEY_HASS_USER)
    return user.id if user else None


def config_for(hass: HomeAssistant, user_id: str | None) -> dict:
    """The shared settings, with this person's own Komga key / Audiobookshelf token where they have one."""
    cfg = get_config(hass)
    person = get_users(hass).get(user_id or "")
    if not person:
        return cfg
    merged = dict(cfg)
    for key in (CONF_KOMGA_API_KEY, CONF_ABS_TOKEN):
        if person.get(key):
            merged[key] = person[key]
    return merged


def tolino_allowed(hass: HomeAssistant, user_id: str | None) -> bool:
    """Without any person configured everybody keeps the old behaviour; otherwise only people marked as Tolino users."""
    users = get_users(hass)
    return True if not users else bool(users.get(user_id or "", {}).get(CONF_USER_TOLINO))


def tolino_config(hass: HomeAssistant) -> dict:
    """Settings for the background Tolino jobs (auto-send, progress sync): the first Tolino person's Audiobookshelf account.
    People exist but nobody uses a Tolino -> the bridge counts as not configured."""
    users = get_users(hass)
    if not users:
        return get_config(hass)
    for uid, person in users.items():
        if person.get(CONF_USER_TOLINO):
            return config_for(hass, uid)
    return {**get_config(hass), CONF_TOLINO_URL: "", CONF_TOLINO_TOKEN: ""}
