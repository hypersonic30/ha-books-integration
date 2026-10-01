"""Books — Home Assistant proxy for Chaptarr + Audiobookshelf, used by the books card."""
from __future__ import annotations

from datetime import timedelta
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    AUTO_SEND_INTERVAL_SECONDS,
    WISH_INTERVAL_SECONDS,
    CONF_DEBUG_LOGGING,
    DEFAULT_TOLINO_ACCOUNT,
    DOMAIN,
    RESCUE_INTERVAL_SECONDS,
    SYNC_INTERVAL_SECONDS,
)
from .api import TolinoBridgeClient
from .rescue import ImportRescue
from .jobs import async_ensure_jobs, async_refresh_users
from .tolino_move import async_move_account
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
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_MOVE_TOLINO_ACCOUNT = "move_tolino_account"

# Setting the level here also governs the submodules (they inherit it).
_PKG_LOGGER = logging.getLogger(__package__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def _move(call: ServiceCall) -> dict:
        moved = await async_move_account(hass, call.data["from_account"], call.data["to_account"])
        return {"moved_books": moved}

    hass.services.async_register(
        DOMAIN, SERVICE_MOVE_TOLINO_ACCOUNT, _move,
        schema=vol.Schema({vol.Optional("from_account", default=DEFAULT_TOLINO_ACCOUNT): cv.string,
                           vol.Required("to_account"): cv.string}),
        supports_response=SupportsResponse.OPTIONAL)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.setdefault(DOMAIN, {})
    # Views and the rescue read this live on every request/tick, so a
    # reconfigure applies without re-registering anything.
    data["config"] = dict(entry.data)
    data["users"] = users_from_entry(entry)
    # People are added/changed/removed without a reload: everything reads data["users"] live.
    entry.async_on_unload(entry.add_update_listener(async_refresh_users))

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

    # One progress-sync and one auto-send job per bridge account (= per person with a Tolino); the default account's are
    # also reachable as data["progress_sync"] / data["auto_send"].
    await async_ensure_jobs(hass)

    async def _tick_sync(now=None) -> None:
        for jobs in list(data["jobs"].values()):
            await jobs["progress_sync"].async_tick(now)

    async def _tick_auto_send(now=None) -> None:
        for jobs in list(data["jobs"].values()):
            await jobs["auto_send"].async_tick(now)

    entry.async_on_unload(async_track_time_interval(hass, _tick_sync, timedelta(seconds=SYNC_INTERVAL_SECONDS)))
    entry.async_on_unload(async_track_time_interval(hass, _tick_auto_send, timedelta(seconds=AUTO_SEND_INTERVAL_SECONDS)))

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
