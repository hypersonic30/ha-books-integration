"""Sensors that make the background jobs visible: last progress sync and last auto-sent book."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import TolinoBridgeClient, get_config
from .const import DOMAIN, SIGNAL_AUTOSEND_UPDATED, SIGNAL_SYNC_UPDATED

DEVICE = DeviceInfo(identifiers={(DOMAIN, "tolino_bridge")}, name="tolino-Bridge")


class _JobSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_info = DEVICE
    _signal = ""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self._hass, self._signal, self._updated))

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()


class ProgressSyncSensor(_JobSensor):
    """State: time of the last reading-progress run; attributes: what that run did."""

    _attr_translation_key = "progress_sync"
    _attr_unique_id = "books_tolino_progress_sync"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _signal = SIGNAL_SYNC_UPDATED

    @property
    def native_value(self):
        run = self._hass.data[DOMAIN]["progress_sync"].last_run
        return run["at"] if run else None

    @property
    def extra_state_attributes(self) -> dict:
        sync = self._hass.data[DOMAIN]["progress_sync"]
        run = sync.last_run or {}
        return {"enabled": sync.enabled, "write_enabled": sync.write_enabled, "imported": run.get("imported"),
                "exported": run.get("exported"), "skipped": run.get("skipped")}


class AutoSendSensor(_JobSensor):
    """State: title of the book auto-sent last (survives restarts); attributes: when, how many in total."""

    _attr_translation_key = "auto_send"
    _attr_unique_id = "books_tolino_auto_send"
    _signal = SIGNAL_AUTOSEND_UPDATED

    @property
    def native_value(self):
        return (self._hass.data[DOMAIN]["auto_send"].state.get("last_sent") or {}).get("title")

    @property
    def extra_state_attributes(self) -> dict:
        job = self._hass.data[DOMAIN]["auto_send"]
        last = job.state.get("last_sent") or {}
        return {"enabled": job.enabled, "sent_at": last.get("at"), "total_sent": job.state.get("total_sent", 0),
                "given_up": len(job.state.get("failed") or {}), "since_ms": job.state.get("since")}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    if TolinoBridgeClient(hass, get_config(hass)).configured:
        async_add_entities([ProgressSyncSensor(hass), AutoSendSensor(hass)])
