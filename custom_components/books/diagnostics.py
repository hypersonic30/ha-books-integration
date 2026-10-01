"""Diagnostics a user can share: the setup and the state of the jobs, with every key, token, address of a person and notify target blanked.
Nothing here makes a network request."""
from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

TO_REDACT = {"chaptarr_api_key", "abs_token", "komga_api_key", "mylar_api_key", "tolino_token", "notify_service", "ha_user", "komga_name",
             "abs_name", "title", "unique_id"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    data = hass.data.get(DOMAIN, {})
    jobs = {}
    for account, job in (data.get("jobs") or {}).items():
        sync, auto = job["progress_sync"], job["auto_send"]
        jobs[account] = {
            "auto_send": {"enabled": auto.enabled, "active": auto.state.get("active"), "total_sent": auto.state.get("total_sent", 0),
                          "given_up": len(auto.state.get("failed") or {}), "since_ms": auto.state.get("since")},
            "progress_sync": {"enabled": sync.enabled, "write_enabled": sync.write_enabled, "last_run": {
                k: v for k, v in (sync.last_run or {}).items() if k != "at"}},
            "sent_books": len(((data.get("registries") or {}).get(account)).items) if (data.get("registries") or {}).get(account) else 0,
        }
    wishes = data.get("wishes")
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "people": [async_redact_data({"title": sub.title, "unique_id": sub.unique_id, **sub.data}, TO_REDACT) for sub in entry.subentries.values()],
        "jobs": jobs,
        "waiting_for_arrival": {"book": sum(1 for w in (wishes.items if wishes else []) if w["kind"] == "book"),
                                "manga": sum(1 for w in (wishes.items if wishes else []) if w["kind"] == "manga")},
        "bridge": {k: v for k, v in ((data.get("tolino_watcher").data if data.get("tolino_watcher") else None) or {}).items()
                   if k in ("reachable", "logged_in", "problem", "last_error")},
    }
