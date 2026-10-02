"""The per-account jobs (auto-send, progress sync) and what depends on the list of people.

Everything reads `hass.data[DOMAIN]["users"]` live; this module brings the jobs in line whenever that list changes, and is also used by the
flows that rename or remove a Thalia account."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .tolino_autosend import AutoSender
from .tolino_import import TolinoImporter
from .tolino_registry import async_ensure_registry
from .tolino_sync import ProgressSync
from .users import tolino_accounts, users_from_entry

ISSUE_NO_PERSON = "no_person"


async def async_refresh_users(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = hass.data.setdefault(DOMAIN, {})
    data["users"] = users_from_entry(entry)
    await async_ensure_jobs(hass)                  # a switch turned on, a new Tolino person or account starts from now on


async def async_ensure_jobs(hass: HomeAssistant) -> None:
    data = hass.data[DOMAIN]
    jobs = data.setdefault("jobs", {})
    for account in tolino_accounts(hass):
        await async_ensure_registry(hass, account)            # loads it from storage when it is not in memory (after a restart)
        if account not in jobs:
            jobs[account] = {"auto_send": AutoSender(hass, account), "progress_sync": ProgressSync(hass, account),
                             "import": TolinoImporter(hass, account)}
        await jobs[account]["auto_send"].async_start()
        await jobs[account]["import"].async_start()
    for gone in set(jobs) - set(tolino_accounts(hass)):             # nobody uses this account any more: stop it cleanly
        await jobs[gone]["auto_send"].async_start()                 # (disabled now -> marks it inactive)
        await jobs[gone]["import"].async_start()
    check_people_issue(hass)


def check_people_issue(hass: HomeAssistant) -> None:
    """A repair hint while nobody has been added: without a person the cards have no access."""
    if hass.data[DOMAIN].get("users"):
        ir.async_delete_issue(hass, DOMAIN, ISSUE_NO_PERSON)
    else:
        ir.async_create_issue(hass, DOMAIN, ISSUE_NO_PERSON, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                              translation_key=ISSUE_NO_PERSON)


async def async_forget_account(hass: HomeAssistant, account: str) -> None:
    """A Thalia account was removed from the bridge: drop what Home Assistant remembers about it (the list of sent books refers to a
    cloud that no longer exists, and a later account of the same name must not inherit it)."""
    data = hass.data.get(DOMAIN, {})
    job = data.get("jobs", {}).pop(account, None)
    if job:
        await job["auto_send"]._store.async_remove()            # noqa: SLF001 - same package
        await job["import"]._store.async_remove()               # noqa: SLF001
    registry = data.get("registries", {}).pop(account, None)
    if registry is not None:
        await registry._store.async_remove()                     # noqa: SLF001
        if account == "default":
            data.pop("tolino_sent", None)
