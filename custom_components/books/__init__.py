"""Books — Home Assistant proxy for Chaptarr + Audiobookshelf, used by the books card."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    AUTO_SEND_INTERVAL_SECONDS,
    WISH_INTERVAL_SECONDS,
    CONF_DEBUG_LOGGING,
    DOMAIN,
    RESCUE_INTERVAL_SECONDS,
    SYNC_INTERVAL_SECONDS,
)
from .api import TolinoBridgeClient
from .rescue import ImportRescue
from .tolino_autosend import AutoSender
from .tolino_registry import SentRegistry
from .tolino_sync import ProgressSync
from .tolino_watch import TolinoWatcher
from .users import users_from_entry
from .wishes import Wishes
from .views import (
    AbsProxyView,
    AddBookView,
    ChaptarrMediaView,
    ChaptarrProxyView,
    RescueStatusView,
    KomgaProxyView,
    MylarProxyView,
    TolinoAutoSendView,
    TolinoSyncView,
    TolinoView,
)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

# Setting the level here also governs the submodules (they inherit it).
_PKG_LOGGER = logging.getLogger(__package__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.setdefault(DOMAIN, {})
    # Views and the rescue read this live on every request/tick, so a
    # reconfigure applies without re-registering anything.
    data["config"] = dict(entry.data)
    data["users"] = users_from_entry(entry)
    # People are added/changed/removed without a reload: everything reads data["users"] live.
    entry.async_on_unload(entry.add_update_listener(_refresh_users))

    _PKG_LOGGER.setLevel(logging.DEBUG if entry.data.get(CONF_DEBUG_LOGGING) else logging.NOTSET)

    # HTTP views can't be unregistered; hass.data survives entry reloads, so
    # register them once per HA run.
    if not data.get("views_registered"):
        for view in (ChaptarrProxyView, ChaptarrMediaView, AbsProxyView, AddBookView, RescueStatusView, TolinoView, TolinoSyncView, TolinoAutoSendView, KomgaProxyView, MylarProxyView):
            hass.http.register_view(view(hass))
        data["views_registered"] = True

    if "wishes" not in data:
        data["wishes"] = Wishes(hass)
        await data["wishes"].async_load()
    entry.async_on_unload(
        async_track_time_interval(hass, data["wishes"].async_tick, timedelta(seconds=WISH_INTERVAL_SECONDS))
    )

    if "tolino_sent" not in data:
        data["tolino_sent"] = SentRegistry(hass)
        await data["tolino_sent"].async_load()

    data["progress_sync"] = data.get("progress_sync") or ProgressSync(hass)
    entry.async_on_unload(
        async_track_time_interval(hass, data["progress_sync"].async_tick, timedelta(seconds=SYNC_INTERVAL_SECONDS))
    )

    data["auto_send"] = data.get("auto_send") or AutoSender(hass)
    await data["auto_send"].async_start()
    entry.async_on_unload(
        async_track_time_interval(hass, data["auto_send"].async_tick, timedelta(seconds=AUTO_SEND_INTERVAL_SECONDS))
    )

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


async def _refresh_users(hass: HomeAssistant, entry: ConfigEntry) -> None:
    hass.data.setdefault(DOMAIN, {})["users"] = users_from_entry(entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data.get(DOMAIN, {}).pop("config", None)
        hass.data.get(DOMAIN, {}).pop("tolino_watcher", None)
    return ok
