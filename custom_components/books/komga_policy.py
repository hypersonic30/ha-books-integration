"""What the manga card may ask Komga. A strict allow-list: reading, progress and "rescan this library" - nothing that
touches users, API keys, libraries' settings, files on disk or downloads of the original archives."""
from __future__ import annotations

import re

_ID = r"[A-Za-z0-9]{1,40}"
_N = r"\d{1,6}"

_ALLOWED: dict[str, tuple[re.Pattern, ...]] = {
    "GET": tuple(re.compile(f"^{p}$") for p in (
        r"v1/libraries", rf"v1/libraries/{_ID}",
        r"v1/series", r"v1/series/latest", r"v1/series/new", r"v1/series/updated", rf"v1/series/{_ID}",
        rf"v1/series/{_ID}/books", rf"v1/series/{_ID}/thumbnail",
        r"v1/books", r"v1/books/latest", r"v1/books/ondeck", rf"v1/books/{_ID}",
        rf"v1/books/{_ID}/pages", rf"v1/books/{_ID}/pages/{_N}", rf"v1/books/{_ID}/pages/{_N}/thumbnail",
        rf"v1/books/{_ID}/thumbnail", rf"v1/books/{_ID}/next", rf"v1/books/{_ID}/previous",
    )),
    "POST": tuple(re.compile(f"^{p}$") for p in (
        r"v1/series/list", r"v1/books/list", rf"v1/libraries/{_ID}/scan", rf"v1/series/{_ID}/read-progress",
    )),
    "PATCH": tuple(re.compile(f"^{p}$") for p in (rf"v1/books/{_ID}/read-progress",)),
    "DELETE": tuple(re.compile(f"^{p}$") for p in (rf"v1/books/{_ID}/read-progress", rf"v1/series/{_ID}/read-progress")),
}


def komga_allowed(method: str, path: str) -> bool:
    return any(rx.match(path.strip("/")) for rx in _ALLOWED.get(method.upper(), ()))
