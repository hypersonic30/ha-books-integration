"""Give a Tolino account a new name: carry over what Home Assistant remembers about it.

Used once, when the bridge's original account ("default") gets the name of the person it belongs to (`deploy.sh account
rename-default NAME`): the bridge moves the credentials, the session and the Chrome profile; this moves the list of books
that were sent (and with it the reading-progress links) and the auto-send state, so nothing is sent twice and nothing is lost."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .tolino_registry import async_ensure_registry
from .users import tolino_accounts

_LOGGER = logging.getLogger(__name__)
_FRESH_AUTO_SEND = {"active": False, "since": 0, "failed": {}}


async def async_move_account(hass: HomeAssistant, source: str, target: str) -> int:
    """Move the sent-books list and the auto-send state of account `source` to `target`; returns how many books moved."""
    if source == target:
        raise ServiceValidationError("The old and the new account name are the same.")
    if target not in tolino_accounts(hass) or target not in hass.data.get(DOMAIN, {}).get("jobs", {}):
        raise ServiceValidationError(
            f"No person uses the Tolino account '{target}' yet: open that person in the Books integration and enter it as the "
            "Tolino account first.")
    source_registry = await async_ensure_registry(hass, source)
    target_registry = await async_ensure_registry(hass, target)
    if target_registry.items:
        raise ServiceValidationError(
            f"The account '{target}' already has {len(target_registry.items)} sent books; nothing was moved so nothing is overwritten.")

    moved = len(source_registry.items)
    target_registry.items = dict(source_registry.items)
    await target_registry._store.async_save(target_registry.items)       # noqa: SLF001 - same package
    source_registry.items = {}
    await source_registry._store.async_save({})                           # noqa: SLF001

    jobs = hass.data[DOMAIN]["jobs"]
    source_job = (jobs.get(source) or {}).get("auto_send")
    target_job = jobs[target]["auto_send"]
    if source_job is not None and source_job.state.get("since"):
        # Everything but the owner (the target account has its own person): above all "since", so nothing old gets sent.
        target_job.state.update({k: v for k, v in source_job.state.items() if k != "owner"})
        await target_job._store.async_save(target_job.state)              # noqa: SLF001
        source_job.state.clear()
        source_job.state.update(_FRESH_AUTO_SEND)
        await source_job._store.async_save(source_job.state)              # noqa: SLF001
    _LOGGER.info("books: moved the Tolino account '%s' to '%s' (%d sent books)", source, target, moved)
    return moved


async def async_rename_default(hass: HomeAssistant, entry, to: str) -> int:
    """Rename the bridge's original account to `to` in one go: the bridge moves credentials, session and Chrome profile (the login is
    kept); the person who uses "default" is switched to the new name and the list of sent books follows. Returns how many books moved.
    Bridge errors (UpstreamError, aiohttp errors) are raised as they are, before anything in Home Assistant has been changed."""
    from .api import TolinoBridgeClient
    from .const import CONF_HA_USER, CONF_TOLINO_ACCOUNT, DEFAULT_TOLINO_ACCOUNT
    from .jobs import async_refresh_users
    from .users import tolino_user_id

    uid = tolino_user_id(hass, DEFAULT_TOLINO_ACCOUNT)
    await TolinoBridgeClient(hass, entry.data).request("POST", "/accounts/default/rename", json={"to": to}, timeout=120)
    if uid is None:
        return 0                                                      # nobody used it yet: nothing in Home Assistant to carry over
    sub = next(s for s in entry.subentries.values() if s.data.get(CONF_HA_USER) == uid)
    hass.config_entries.async_update_subentry(entry, sub, data={**sub.data, CONF_TOLINO_ACCOUNT: to})
    await async_refresh_users(hass, entry)                            # the new account has its jobs and registry now
    return await async_move_account(hass, DEFAULT_TOLINO_ACCOUNT, to)
