"""Take something off the download list: what the cards' trash buttons do.

Chaptarr has one API key for everybody and the proxy's allow-list (chaptarr_policy.py) lets nothing through that deletes, so removing is its own
small, fixed procedure instead of a free DELETE:

  1. the queue entry goes (`removeFromClient`: the download is taken off the downloader too), optionally onto the blocklist so the same release is
     not grabbed again, and Chaptarr does not look for another one (`skipRedownload`);
  2. optionally the author (with everything Chaptarr knows of him, often a whole catalog) - but only when no file of him is on disk;
  3. optionally just the book - but only when it has no file.

Files that were imported are never touched (`deleteFiles=false`; a book or author that already has files is kept and reported)."""
from __future__ import annotations

import logging

from .api import ChaptarrClient, UpstreamError

_LOGGER = logging.getLogger(__name__)


class RemoveError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code, message, status


def _files(resource: dict) -> int:
    stats = (resource or {}).get("statistics") or {}
    return int(stats.get("bookFileCount") or 0) + (1 if int(stats.get("sizeOnDisk") or 0) > 0 else 0)


async def async_remove_from_chaptarr(client: ChaptarrClient, *, queue_id: int | None = None, book_id: int | None = None, blocklist: bool = True,
                                     remove_book: bool = False, remove_author: bool = False) -> dict:
    """Returns {"removed": {"queue", "book", "author"}, "kept": [reasons]}; raises RemoveError / UpstreamError."""
    if queue_id is None and book_id is None:
        raise RemoveError("bad_request", "queue_id or book_id is required", 400)
    if queue_id is None and not (remove_book or remove_author):
        raise RemoveError("bad_request", "nothing to remove", 400)
    removed = {"queue": False, "book": False, "author": False}
    kept: list[str] = []
    author_id = None
    if queue_id is not None:
        records = ((await client.get("/queue", params={"page": 1, "pageSize": 200, "includeUnknownAuthorItems": "true"})) or {}).get("records", [])
        record = next((r for r in records if r.get("id") == queue_id), None)
        if record is None:
            raise RemoveError("not_found", "Dieser Download ist nicht mehr in der Warteschlange.", 404)
        book_id, author_id = record.get("bookId"), record.get("authorId")
        await client.request("DELETE", f"/queue/{queue_id}", params={
            "removeFromClient": "true", "blocklist": "true" if blocklist else "false", "skipRedownload": "true"})
        removed["queue"] = True
    if remove_book or remove_author:
        if book_id is None:
            kept.append("no_book")
        else:
            try:
                book = await client.get(f"/book/{book_id}")
            except UpstreamError as exc:
                if exc.status != 404:
                    raise
                book = None
            if book is None:
                kept.append("book_gone")
            else:
                author_id = author_id or book.get("authorId")
                if remove_author and author_id:
                    author = await client.get(f"/author/{author_id}")
                    if _files(author):
                        kept.append("author_has_files")
                    else:
                        await client.request("DELETE", f"/author/{author_id}", params={"deleteFiles": "false", "addImportListExclusion": "false"})
                        removed["author"] = removed["book"] = True               # his books go with him
                if remove_book and not removed["book"]:
                    if _files(book):
                        kept.append("book_has_files")
                    else:
                        await client.request("DELETE", f"/book/{book_id}", params={"deleteFiles": "false", "addImportListExclusion": "false"})
                        removed["book"] = True
    _LOGGER.info("books: removed from the download list: %s (kept: %s)", removed, kept)
    return {"removed": removed, "kept": kept}
