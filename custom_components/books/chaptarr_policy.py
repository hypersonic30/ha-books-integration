"""What the cards may ask Chaptarr. A strict allow-list - searching, looking at the queue/wanted list/history/calendar and starting a few
harmless commands (see CHAPTARR_ALLOWED_COMMANDS). Nothing that changes or deletes books, authors, files, queue entries or blocklists, and none of
Chaptarr's settings. Adding a book goes through POST /api/books/add (own view), not through this proxy.

Chaptarr has ONE API key for everybody, so this list - not the key - is what keeps an ordinary person from deleting the library
(`DELETE book/5?deleteFiles=true` used to pass)."""
from __future__ import annotations

import re

_ID = r"\d{1,9}"

_ALLOWED: dict[str, tuple[re.Pattern, ...]] = {
    "GET": tuple(re.compile(f"^{p}$") for p in (
        r"queue", r"queue/details", r"queue/status", r"wanted/missing", r"wanted/cutoff",
        r"search", r"book/lookup", r"author/lookup",
        r"book", rf"book/{_ID}", r"author", rf"author/{_ID}",
        r"history", r"calendar", r"command", rf"command/{_ID}",
        r"release",                                           # interactive release search (read-only)
    )),
    "POST": tuple(re.compile(f"^{p}$") for p in (r"command",)),   # the command's name is checked against CHAPTARR_ALLOWED_COMMANDS
}


def chaptarr_allowed(method: str, path: str) -> bool:
    return any(rx.match(path.strip("/")) for rx in _ALLOWED.get(method.upper(), ()))
