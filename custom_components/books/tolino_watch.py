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
from .const import DEFAULT_TOLINO_ACCOUNT, DOMAIN
from .users import tolino_accounts
from .notify_helper import async_push

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(minutes=5)
BAD_POLLS_BEFORE_ALERT = 2
NOTIFICATION_ID = "books_tolino_bridge"


class TolinoWatcher(DataUpdateCoordinator[dict]):
    """Polls /status of every bridge account that somebody uses. The top-level fields of the data are those of the first account
    (what the single account always showed); `problem` is on when any account has one; `accounts` has them all."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name="Tolino bridge", update_interval=POLL_INTERVAL)
        self._bad_polls: dict[str, int] = {}
        self._alerted: set[str] = set()

    async def _async_update_data(self) -> dict:
        accounts = tolino_accounts(self.hass)
        if not accounts:                                       # people exist, but nobody uses the bridge: nothing to watch
            return {"reachable": True, "logged_in": True, "problem": False, "accounts": {}}
        results = {account: await self._poll(account) for account in accounts}
        named = len(accounts) > 1 or accounts[0] != DEFAULT_TOLINO_ACCOUNT
        for account, data in results.items():
            await self._announce(account, data, named)
        for gone in (set(self._bad_polls) | self._alerted) - set(accounts):       # an account nobody uses any more
            self._bad_polls.pop(gone, None)
            if gone in self._alerted:
                self._alerted.discard(gone)
                persistent_notification.async_dismiss(self.hass, self._notification_id(gone))
        return {**results[accounts[0]], "accounts": results, "problem": any(r["problem"] for r in results.values())}

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
    def _notification_id(account: str) -> str:
        return NOTIFICATION_ID if account == DEFAULT_TOLINO_ACCOUNT else f"{NOTIFICATION_ID}_{account}"

    async def _announce(self, account: str, data: dict, named: bool) -> None:
        label = f" ({account})" if named else ""
        if data["problem"]:
            self._bad_polls[account] = self._bad_polls.get(account, 0) + 1
            if self._bad_polls[account] >= BAD_POLLS_BEFORE_ALERT and account not in self._alerted:
                self._alerted.add(account)
                reason = data.get("last_error") or "unbekannter Grund"
                whose = f" für das Konto „{account}“" if named else ""
                text = (f"Die tolino-Bridge ist{whose} gerade nicht einsatzbereit: {reason}. "
                        "Bücher lassen sich so nicht an tolino senden. Details: `deploy.sh status` bzw. Logs auf dem Server.")
                persistent_notification.async_create(
                    self.hass, text, title=f"tolino-Bridge Problem{label}", notification_id=self._notification_id(account))
                await async_push(self.hass, f"tolino-Bridge Problem{label}", text)
        else:
            self._bad_polls[account] = 0
            if account in self._alerted:
                self._alerted.discard(account)
                persistent_notification.async_dismiss(self.hass, self._notification_id(account))
                await async_push(self.hass, f"tolino-Bridge{label}", "Die tolino-Bridge läuft wieder." if not named else f"Die tolino-Bridge läuft für das Konto „{account}“ wieder.")


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
