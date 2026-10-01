"""What the cards may ask Audiobookshelf: the library, the item's cover/EPUB/audio files, starting and syncing a playback session and the
person's OWN reading progress. Not users, API keys, libraries' settings, deleting or editing items, scans, backups or anything else - whatever
the token behind the request would be allowed to do. (Komga and Mylar have such lists as well.)"""
from __future__ import annotations

import re

_ID = r"[A-Za-z0-9_-]{1,64}"
_INO = r"[A-Za-z0-9_-]{1,40}"

_ALLOWED: dict[str, tuple[re.Pattern, ...]] = {
    "GET": tuple(re.compile(f"^{p}$") for p in (
        r"libraries", rf"libraries/{_ID}", rf"libraries/{_ID}/items",
        r"me", r"me/items-in-progress", rf"me/progress/{_ID}",
        rf"items/{_ID}", rf"items/{_ID}/cover", rf"items/{_ID}/ebook", rf"items/{_ID}/ebook/{_INO}",
        rf"items/{_ID}/file/{_INO}", rf"items/{_ID}/download",
    )),
    "POST": tuple(re.compile(f"^{p}$") for p in (rf"items/{_ID}/play", rf"session/{_ID}/sync", rf"session/{_ID}/close")),
    "PATCH": tuple(re.compile(f"^{p}$") for p in (rf"me/progress/{_ID}",)),
}


def abs_allowed(method: str, path: str) -> bool:
    return any(rx.match(path.strip("/")) for rx in _ALLOWED.get(method.upper(), ()))
