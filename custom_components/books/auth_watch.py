"""A repair hint when a service rejects a key: a renewed Komga key, a changed Audiobookshelf token, a rotated Mylar key. Without it the
card just shows errors. One issue per service and person, removed as soon as the service accepts the key again."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

SHARED = "shared"


def _issue_id(service: str, user_id: str | None) -> str:
    return f"auth_{service.lower()}_{user_id or SHARED}"


def key_rejected(hass: HomeAssistant, service: str, user_id: str | None) -> None:
    person = (hass.data.get(DOMAIN, {}).get("users", {}).get(user_id or "", {}) or {}).get("_name")
    known = hass.data.setdefault(DOMAIN, {}).setdefault("auth_issues", set())
    issue = _issue_id(service, user_id)
    known.add(issue)
    ir.async_create_issue(hass, DOMAIN, issue, is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key="upstream_auth",
                          translation_placeholders={"service": service, "person": person or "-"})


def key_accepted(hass: HomeAssistant, service: str, user_id: str | None) -> None:
    known = hass.data.get(DOMAIN, {}).get("auth_issues")
    issue = _issue_id(service, user_id)
    if known and issue in known:                       # normally nothing: this runs for every request
        known.discard(issue)
        ir.async_delete_issue(hass, DOMAIN, issue)
