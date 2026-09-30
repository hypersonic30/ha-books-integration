"""Remembers which Audiobookshelf items were already sent to the Tolino Cloud, so a second tap can't create a duplicate."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

STORAGE_VERSION = 1
STORAGE_KEY = "books_tolino_sent"


class SentRegistry:
    """abs_item_id -> {deliverableId, filename, at}. Persisted; the Tolino Cloud stays the source of truth
    (callers verify against the bridge's library before trusting an entry)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.items: dict[str, dict] = {}

    async def async_load(self) -> None:
        self.items = await self._store.async_load() or {}

    def get(self, item_id: str) -> dict | None:
        return self.items.get(item_id)

    async def async_set(self, item_id: str, deliverable_id: str, filename: str) -> None:
        self.items[item_id] = {"deliverableId": deliverable_id, "filename": filename,
                               "at": dt_util.utcnow().isoformat()}
        await self._store.async_save(self.items)

    async def async_update(self, item_id: str, **fields) -> None:
        """Merge extra fields (e.g. the last imported reading state) into an existing entry."""
        if item_id in self.items:
            self.items[item_id].update(fields)
            await self._store.async_save(self.items)

    async def async_remove(self, item_id: str) -> None:
        if self.items.pop(item_id, None) is not None:
            await self._store.async_save(self.items)

    def public(self) -> dict[str, dict]:
        """What the card may see: when, never the cloud ids."""
        return {k: {"at": v["at"]} for k, v in self.items.items()}
