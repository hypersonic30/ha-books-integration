"""Whose book is it: one Audiobookshelf library for everybody, told apart by a tag per person ("für Anna").

A book gets the tag of the person who asked for it (the wish, see wishes.py); books without such a tag - the whole existing
stock, or what was loaded in Chaptarr directly - are for everybody. The cards only READ the tags (chips per person); writing
them is done here, by the integration, with the shared Audiobookshelf token of the main settings (it needs the "update"
permission of that user, never admin). The proxy still refuses every write to items (abs_policy.py)."""
from __future__ import annotations

import logging

from .api import AbsClient

_LOGGER = logging.getLogger(__name__)
TAG_PREFIX = "für "
SHARED_TAG = "für alle"            # released for everybody (what a person with a limited Audiobookshelf user may see besides their own tag)


def person_tag(person: dict | None) -> str | None:
    name = ((person or {}).get("_name") or "").strip()
    return f"{TAG_PREFIX}{name}" if name else None


async def async_set_tag(client: AbsClient, item_id: str, tag: str, on: bool) -> list[str]:
    """Add (`on`) or remove `tag` on the item, every other tag stays. Returns the item's tags afterwards."""
    item = await client.get(f"/items/{item_id}")
    tags = [str(t) for t in ((item or {}).get("media") or {}).get("tags") or []]
    new = tags + [tag] if on and tag not in tags else [t for t in tags if t != tag] if not on else tags
    if new != tags:
        await client.request("PATCH", f"/items/{item_id}/media", json={"tags": new})
        _LOGGER.debug("books: item %s: tag %r %s", item_id, tag, "added" if on else "removed")
    return new


async def async_tag_item(client: AbsClient, item_id: str, tag: str) -> bool:
    """Add `tag` to the item (its other tags stay); True when something was written."""
    before = [str(t) for t in (((await client.get(f"/items/{item_id}")) or {}).get("media") or {}).get("tags") or []]
    return before != await async_set_tag(client, item_id, tag, True)
