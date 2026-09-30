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
from .const import DOMAIN
from .notify_helper import async_push

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(minutes=5)
BAD_POLLS_BEFORE_ALERT = 2
NOTIFICATION_ID = "books_tolino_bridge"


class TolinoWatcher(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name="Tolino bridge", update_interval=POLL_INTERVAL)
        self._bad_polls = 0
        self._alerted = False

    async def _async_update_data(self) -> dict:
        bridge = TolinoBridgeClient(self.hass, get_config(self.hass))
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
        await self._announce(data)
        return data

    async def _announce(self, data: dict) -> None:
        if data["problem"]:
            self._bad_polls += 1
            if self._bad_polls >= BAD_POLLS_BEFORE_ALERT and not self._alerted:
                self._alerted = True
                reason = data.get("last_error") or "unbekannter Grund"
                text = (f"Die tolino-Bridge ist gerade nicht einsatzbereit: {reason}. "
                        "Bücher lassen sich so nicht an tolino senden. Details: `deploy.sh status` bzw. Logs auf dem Server.")
                persistent_notification.async_create(
                    self.hass, text, title="tolino-Bridge Problem", notification_id=NOTIFICATION_ID)
                await async_push(self.hass, "tolino-Bridge Problem", text)
        else:
            self._bad_polls = 0
            if self._alerted:
                self._alerted = False
                persistent_notification.async_dismiss(self.hass, NOTIFICATION_ID)
                await async_push(self.hass, "tolino-Bridge", "Die tolino-Bridge läuft wieder.")


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
        return {k: d.get(k) for k in ("reachable", "logged_in", "last_error", "session_age_s",
                                       "relogin_in_s", "login_backoff_s")}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    watcher = hass.data.get(DOMAIN, {}).get("tolino_watcher")
    if watcher is not None:
        async_add_entities([TolinoBridgeProblem(watcher)])
