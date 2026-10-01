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


def _jobs(hass: HomeAssistant) -> dict[str, dict]:
    return hass.data[DOMAIN]["jobs"]


class ProgressSyncSensor(_JobSensor):
    """State: time of the last reading-progress run (of any account); attributes: what the default account's run did, and per account."""

    _attr_translation_key = "progress_sync"
    _attr_unique_id = "books_tolino_progress_sync"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _signal = SIGNAL_SYNC_UPDATED

    @property
    def native_value(self):
        runs = [j["progress_sync"].last_run["at"] for j in _jobs(self._hass).values() if j["progress_sync"].last_run]
        return max(runs) if runs else None

    @property
    def extra_state_attributes(self) -> dict:
        def one(sync) -> dict:
            run = sync.last_run or {}
            return {"enabled": sync.enabled, "write_enabled": sync.write_enabled, "imported": run.get("imported"),
                    "exported": run.get("exported"), "skipped": run.get("skipped")}
        jobs = _jobs(self._hass)
        first = next((j["progress_sync"] for j in jobs.values()), None)
        return {**(one(first) if first else {}), "accounts": {a: one(j["progress_sync"]) for a, j in jobs.items()}}


class AutoSendSensor(_JobSensor):
    """State: title of the book auto-sent last (any account; survives restarts); attributes: when, how many in total."""

    _attr_translation_key = "auto_send"
    _attr_unique_id = "books_tolino_auto_send"
    _signal = SIGNAL_AUTOSEND_UPDATED

    @property
    def native_value(self):
        sent = [j["auto_send"].state.get("last_sent") for j in _jobs(self._hass).values() if j["auto_send"].state.get("last_sent")]
        return max(sent, key=lambda s: s.get("at") or "")["title"] if sent else None

    @property
    def extra_state_attributes(self) -> dict:
        def one(job) -> dict:
            last = job.state.get("last_sent") or {}
            return {"enabled": job.enabled, "sent_at": last.get("at"), "total_sent": job.state.get("total_sent", 0),
                    "given_up": len(job.state.get("failed") or {}), "since_ms": job.state.get("since")}
        jobs = _jobs(self._hass)
        first = next((j["auto_send"] for j in jobs.values()), None)
        return {**(one(first) if first else {}), "accounts": {a: one(j["auto_send"]) for a, j in jobs.items()}}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    if TolinoBridgeClient(hass, get_config(hass)).configured:
        async_add_entities([ProgressSyncSensor(hass), AutoSendSensor(hass)])
