"""Watches the tolino-bridge: a coordinator polling /status, and a binary sensor that is ON when something is wrong.

Thalia's bot protection can block the bridge's login at any time; without this you'd only find out when a send
fails. A problem is announced (persistent notification + push) after two bad polls in a row, so a bridge that is
just restarting doesn't cry wolf, and again once when it recovers.
"""
from __future__ import annotations

from datetime import timedelta
import logging

import aiohttp

from homeassistant.components import persistent_notification
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .api import TolinoBridgeClient, UpstreamError, get_config
from .const import CONF_NOTIFY_SERVICE, DEFAULT_TOLINO_ACCOUNT, DOMAIN
from .users import get_users, tolino_accounts, tolino_user_id
from .notify_helper import async_push

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(minutes=5)
BAD_POLLS_BEFORE_ALERT = 2
NOTIFICATION_ID = "books_tolino_bridge"


BRIDGE_KEY = "bridge"                        # the incident "the whole bridge is unreachable" (one alert, not one per account)


def _targets(raw: str | None) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


class TolinoWatcher(DataUpdateCoordinator[dict]):
    """Polls /status of every bridge account that somebody uses. The top-level fields of the data are those of the first account;
    `problem` is on when the bridge or any account has one; `accounts` has them all.

    Who hears about it: the notify target of the main settings (the administrator) always, plus every person whose tolino
    account is affected - on their own target (not twice if it is also the administrator's)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name="Tolino bridge", update_interval=POLL_INTERVAL)
        self._bad_polls: dict[str, int] = {}
        self._alerted: set[str] = set()

    async def _async_update_data(self) -> dict:
        accounts = tolino_accounts(self.hass)
        if not accounts:                                       # nobody uses the bridge: nothing to watch
            await self._clear_all()
            return {"reachable": True, "logged_in": True, "problem": False, "accounts": {}}
        results = {account: await self._poll(account) for account in accounts}
        bridge_down = all(not r["reachable"] for r in results.values())
        named = len(accounts) > 1 or accounts[0] != DEFAULT_TOLINO_ACCOUNT
        # One incident for a bridge that does not answer; account incidents only while the bridge does.
        await self._track(BRIDGE_KEY, bridge_down, None, accounts)
        for account, data in results.items():
            await self._track(account, (not bridge_down) and data["reachable"] and not data["logged_in"], data, [account], named)
        for gone in {k for k in (set(self._bad_polls) | self._alerted) if k != BRIDGE_KEY} - set(accounts):
            await self._clear(gone)                              # an account nobody uses any more
        return {**results[accounts[0]], "accounts": results, "problem": bridge_down or any(r["problem"] for r in results.values())}

    async def _poll(self, account: str) -> dict:
        bridge = TolinoBridgeClient(self.hass, get_config(self.hass), account)
        try:
            status = await bridge.get("/status")
            data = {"reachable": True, "logged_in": bool(status.get("logged_in")),
                    "last_error": status.get("last_error"), "session_age_s": status.get("session_age_s"),
                    "relogin_in_s": status.get("relogin_in_s"), "login_backoff_s": status.get("login_backoff_s")}
        except UpstreamError as exc:
            data = {"reachable": True, "logged_in": False, "last_error": f"bridge answered {exc.status}"}
        except (aiohttp.ClientError, TimeoutError) as exc:
            data = {"reachable": False, "logged_in": False, "last_error": f"nicht erreichbar ({exc})"}
        data["problem"] = not (data["reachable"] and data["logged_in"])
        return data

    @staticmethod
    def _notification_id(key: str) -> str:
        return NOTIFICATION_ID if key in (BRIDGE_KEY, DEFAULT_TOLINO_ACCOUNT) else f"{NOTIFICATION_ID}_{key}"

    def _people(self, accounts: list[str]) -> list[tuple[str, str]]:
        """(account, notify target) of the people using these accounts."""
        out = []
        for account in accounts:
            uid = tolino_user_id(self.hass, account)
            if uid:
                out.append((account, get_users(self.hass)[uid].get(CONF_NOTIFY_SERVICE) or ""))
        return out

    async def _track(self, key: str, bad: bool, data: dict | None, accounts: list[str], named: bool = True) -> None:
        if bad:
            self._bad_polls[key] = self._bad_polls.get(key, 0) + 1
            if self._bad_polls[key] >= BAD_POLLS_BEFORE_ALERT and key not in self._alerted:
                self._alerted.add(key)
                await self._alert(key, data, accounts, named)
        else:
            self._bad_polls[key] = 0
            if key in self._alerted:
                self._alerted.discard(key)
                await self._all_clear(key, accounts, named)

    async def _alert(self, key: str, data: dict | None, accounts: list[str], named: bool) -> None:
        reason = (data or {}).get("last_error") or "unbekannter Grund"
        if key == BRIDGE_KEY:
            title, admin = "tolino-Bridge Problem", ("Die tolino-Bridge ist gerade nicht erreichbar. Bücher lassen sich so nicht an tolino senden, "
                                                     "der Lesefortschritt wird nicht abgeglichen. Details: `deploy.sh status` bzw. Logs auf dem Server.")
            person = "Die tolino-Bridge ist gerade nicht erreichbar. Bis sie wieder läuft, werden keine Bücher gesendet und dein Lesefortschritt nicht abgeglichen."
        else:
            label = f" ({key})" if named else ""
            whose = f" für das Konto „{key}“" if named else ""
            title = f"tolino-Bridge Problem{label}"
            admin = (f"Die tolino-Bridge ist{whose} gerade nicht einsatzbereit: {reason}. "
                     "Bücher lassen sich so nicht an tolino senden. Details: `deploy.sh status` bzw. Logs auf dem Server.")
            person = (f"Dein tolino-Konto ist gerade nicht eingeloggt: {reason}. Solange das so ist, werden keine Bücher an dein tolino "
                      "gesendet und dein Lesefortschritt nicht abgeglichen. Der Verwalter ist informiert.")
        persistent_notification.async_create(self.hass, admin, title=title, notification_id=self._notification_id(key))
        await self._push(title, admin, "tolino-Konto Problem" if key != BRIDGE_KEY else title, person, accounts)

    async def _all_clear(self, key: str, accounts: list[str], named: bool) -> None:
        persistent_notification.async_dismiss(self.hass, self._notification_id(key))
        if key == BRIDGE_KEY:
            title, admin, person = "tolino-Bridge", "Die tolino-Bridge läuft wieder.", "Die tolino-Bridge läuft wieder."
        else:
            label = f" ({key})" if named else ""
            title = f"tolino-Bridge{label}"
            admin = "Die tolino-Bridge läuft wieder." if not named else f"Die tolino-Bridge läuft für das Konto „{key}“ wieder."
            person = "Dein tolino-Konto läuft wieder."
        await self._push(title, admin, "tolino-Konto" if key != BRIDGE_KEY else title, person, accounts)

    async def _push(self, admin_title: str, admin_text: str, person_title: str, person_text: str, accounts: list[str]) -> None:
        """The administrator's targets get the full text; each affected person gets theirs on their own target - unless that
        target is one of the administrator's (they have read the full text already)."""
        await async_push(self.hass, admin_title, admin_text)
        known = set(_targets(get_config(self.hass).get(CONF_NOTIFY_SERVICE)))
        for _account, raw in self._people(accounts):
            mine = [t for t in _targets(raw) if t not in known]
            if mine:
                await async_push(self.hass, person_title, person_text, targets=",".join(mine))
                known.update(mine)

    async def _clear(self, key: str) -> None:
        self._bad_polls.pop(key, None)
        if key in self._alerted:
            self._alerted.discard(key)
            persistent_notification.async_dismiss(self.hass, self._notification_id(key))

    async def _clear_all(self) -> None:
        for key in list(set(self._bad_polls) | self._alerted):
            await self._clear(key)


class TolinoBridgeProblem(CoordinatorEntity[TolinoWatcher], BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_unique_id = "books_tolino_bridge_problem"
    _attr_device_info = DeviceInfo(identifiers={(DOMAIN, "tolino_bridge")}, name="tolino-Bridge")

    @property
    def is_on(self) -> bool | None:
        return None if self.coordinator.data is None else self.coordinator.data["problem"]

    @property
    def extra_state_attributes(self) -> dict:
        d = self.coordinator.data or {}
        attrs = {k: d.get(k) for k in ("reachable", "logged_in", "last_error", "session_age_s", "relogin_in_s", "login_backoff_s")}
        attrs["accounts"] = {a: {k: r.get(k) for k in ("reachable", "logged_in", "last_error", "problem")}
                             for a, r in (d.get("accounts") or {}).items()}
        return attrs


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    watcher = hass.data.get(DOMAIN, {}).get("tolino_watcher")
    if watcher is not None:
        async_add_entities([TolinoBridgeProblem(watcher)])
