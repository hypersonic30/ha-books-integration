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


def person_tag(person: dict | None) -> str | None:
    name = ((person or {}).get("_name") or "").strip()
    return f"{TAG_PREFIX}{name}" if name else None


async def async_tag_item(client: AbsClient, item_id: str, tag: str) -> bool:
    """Add `tag` to the item (its other tags stay); True when something was written."""
    item = await client.get(f"/items/{item_id}")
    tags = [str(t) for t in ((item or {}).get("media") or {}).get("tags") or []]
    if tag in tags:
        return False
    await client.request("PATCH", f"/items/{item_id}/media", json={"tags": tags + [tag]})
    _LOGGER.debug("books: tagged item %s with %r", item_id, tag)
    return True
