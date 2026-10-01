"""Per-person accounts. Every person gets a config subentry: a Home Assistant user plus their own Komga key, Audiobookshelf
token, notify target and (optionally) the Tolino bridge. The proxies pick the account of whoever is asking, so reading
progress and bookmarks are never shared; anybody without a subentry keeps using the shared account from the main settings."""
from __future__ import annotations

from homeassistant.components.http import KEY_HASS_USER
from homeassistant.core import HomeAssistant

from .api import get_config
from .const import (
    CONF_ABS_TOKEN,
    CONF_AUTO_SEND,
    CONF_SYNC_PROGRESS,
    CONF_SYNC_PROGRESS_WRITE,
    CONF_HA_USER,
    CONF_KOMGA_API_KEY,
    CONF_TOLINO_ACCOUNT,
    CONF_TOLINO_TOKEN,
    CONF_TOLINO_URL,
    DEFAULT_TOLINO_ACCOUNT,
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


def account_of(person: dict) -> str:
    return person.get(CONF_TOLINO_ACCOUNT) or DEFAULT_TOLINO_ACCOUNT


def tolino_user_id(hass: HomeAssistant, account: str = DEFAULT_TOLINO_ACCOUNT) -> str | None:
    """The person who uses this account of the bridge (accounts are unique per person)."""
    return next((uid for uid, person in get_users(hass).items()
                 if person.get(CONF_USER_TOLINO) and account_of(person) == account), None)


def tolino_accounts(hass: HomeAssistant) -> list[str]:
    """The bridge accounts that have somebody using them. Without any person: the default account, as before."""
    users = get_users(hass)
    if not users:
        return [DEFAULT_TOLINO_ACCOUNT]
    return list(dict.fromkeys(account_of(p) for p in users.values() if p.get(CONF_USER_TOLINO)))


def account_for_user(hass: HomeAssistant, user_id: str | None) -> str:
    """Which bridge account this person sends to (the default account without any person set up)."""
    person = get_users(hass).get(user_id or "")
    return account_of(person) if person else DEFAULT_TOLINO_ACCOUNT


def tolino_config(hass: HomeAssistant, account: str = DEFAULT_TOLINO_ACCOUNT) -> dict:
    """Settings for the Tolino jobs (auto-send, progress sync) of one bridge account. Without any person: the main settings,
    as before. With people: the Audiobookshelf account and the switches of the person who uses that account; nobody does ->
    the bridge counts as not configured for it."""
    cfg = get_config(hass)
    if not get_users(hass):
        return cfg if account == DEFAULT_TOLINO_ACCOUNT else {**cfg, CONF_TOLINO_URL: "", CONF_TOLINO_TOKEN: ""}
    uid = tolino_user_id(hass, account)
    if uid is None:
        return {**cfg, CONF_TOLINO_URL: "", CONF_TOLINO_TOKEN: "",
                CONF_AUTO_SEND: False, CONF_SYNC_PROGRESS: False, CONF_SYNC_PROGRESS_WRITE: False}
    person = get_users(hass)[uid]
    return {**config_for(hass, uid), **{k: bool(person.get(k)) for k in (CONF_AUTO_SEND, CONF_SYNC_PROGRESS, CONF_SYNC_PROGRESS_WRITE)}}
