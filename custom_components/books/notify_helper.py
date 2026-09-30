"""Push to the notify targets configured in the integration (shared by the import rescue and the Tolino watcher)."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .api import get_config
from .const import CONF_NOTIFY_SERVICE

_LOGGER = logging.getLogger(__name__)


async def async_push(hass: HomeAssistant, title: str, message: str) -> None:
    """Notify every configured target (comma-separated).

    A target may be a notify *entity* (modern ``notify.send_message``, e.g.
    ``notify.iphone_von_max``) or a legacy notify *service* (e.g.
    ``notify.mobile_app_iphone_von_max``); entities win when both exist.
    """
    raw = get_config(hass).get(CONF_NOTIFY_SERVICE) or ""
    entities: list[str] = []
    for target in (t.strip() for t in raw.split(",")):
        if not target:
            continue
        if "." not in target:
            target = f"notify.{target}"
        domain, _, name = target.partition(".")
        if domain == "notify" and hass.states.get(target) is not None:
            entities.append(target)
        elif hass.services.has_service(domain, name):
            await hass.services.async_call(domain, name, {"title": title, "message": message}, blocking=False)
        else:
            _LOGGER.warning("books: '%s' is neither a notify entity nor a notify service", target)
    if entities:
        await hass.services.async_call(
            "notify", "send_message", {"entity_id": entities, "title": title, "message": message}, blocking=False)
