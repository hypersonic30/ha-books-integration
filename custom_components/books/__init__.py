"""Books — Home Assistant proxy for Chaptarr + Audiobookshelf, used by the books card."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_DEBUG_LOGGING, DOMAIN, RESCUE_INTERVAL_SECONDS
from .api import TolinoBridgeClient
from .rescue import ImportRescue
from .tolino_registry import SentRegistry
from .tolino_watch import TolinoWatcher
from .views import (
    AbsProxyView,
    AddBookView,
    ChaptarrMediaView,
    ChaptarrProxyView,
    RescueStatusView,
    TolinoView,
)

PLATFORMS = [Platform.BINARY_SENSOR]

# Setting the level here also governs the submodules (they inherit it).
_PKG_LOGGER = logging.getLogger(__package__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.setdefault(DOMAIN, {})
    # Views and the rescue read this live on every request/tick, so a
    # reconfigure applies without re-registering anything.
    data["config"] = dict(entry.data)

    _PKG_LOGGER.setLevel(logging.DEBUG if entry.data.get(CONF_DEBUG_LOGGING) else logging.NOTSET)

    # HTTP views can't be unregistered; hass.data survives entry reloads, so
    # register them once per HA run.
    if not data.get("views_registered"):
        for view in (ChaptarrProxyView, ChaptarrMediaView, AbsProxyView, AddBookView, RescueStatusView, TolinoView):
            hass.http.register_view(view(hass))
        data["views_registered"] = True

    if "tolino_sent" not in data:
        data["tolino_sent"] = SentRegistry(hass)
        await data["tolino_sent"].async_load()

    rescue = data.get("rescue") or ImportRescue(hass)
    data["rescue"] = rescue
    entry.async_on_unload(
        async_track_time_interval(hass, rescue.async_tick, timedelta(seconds=RESCUE_INTERVAL_SECONDS))
    )
    # Optional: only watch the bridge when one is configured.
    data.pop("tolino_watcher", None)
    if TolinoBridgeClient(hass, data["config"]).configured:
        data["tolino_watcher"] = TolinoWatcher(hass, entry)
        # Background: a bridge that is down must not delay Home Assistant's startup by its timeout.
        entry.async_create_background_task(hass, data["tolino_watcher"].async_refresh(), "books_tolino_first_poll")
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data.get(DOMAIN, {}).pop("config", None)
        hass.data.get(DOMAIN, {}).pop("tolino_watcher", None)
    return ok
