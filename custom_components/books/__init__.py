"""Books — Home Assistant proxy for Chaptarr + Audiobookshelf, used by the books card."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_DEBUG_LOGGING, DOMAIN, RESCUE_INTERVAL_SECONDS
from .rescue import ImportRescue
from .views import (
    AbsProxyView,
    AddBookView,
    ChaptarrMediaView,
    ChaptarrProxyView,
    RescueStatusView,
)

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
        for view in (ChaptarrProxyView, ChaptarrMediaView, AbsProxyView, AddBookView, RescueStatusView):
            hass.http.register_view(view(hass))
        data["views_registered"] = True

    rescue = data.get("rescue") or ImportRescue(hass)
    data["rescue"] = rescue
    entry.async_on_unload(
        async_track_time_interval(hass, rescue.async_tick, timedelta(seconds=RESCUE_INTERVAL_SECONDS))
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.get(DOMAIN, {}).pop("config", None)
    return True
