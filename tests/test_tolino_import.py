"""Importing the ebooks of a person's tolino account into Audiobookshelf: only purchases, once, tagged for the person."""
import io
import time
import zipfile

import pytest
from homeassistant.components.persistent_notification import _async_get_or_create_notifications
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.books import tolino_import
from custom_components.books.const import DOMAIN
from custom_components.books.tags import person_tag
from custom_components.books.tolino_send import SendError, async_send_to_tolino
from custom_components.books.users import get_users

from .conftest import ABS, ENTRY_DATA, admin_person

BRIDGE = "http://bridge.test:8199"
JSON = {"Content-Type": "application/json"}
LIB = "lib-ebooks"
OPF = ('<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
       '<dc:title>{t}</dc:title><dc:creator>{a}</dc:creator></metadata></package>')
CONTAINER = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
             '<rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')


def epub(title="Flüsterwald", author="Andreas Suchanek"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OPS/book.opf", OPF.format(t=title, a=author))
    return buf.getvalue()


def purchase(n, title=None, kind="purchase"):
    return {"id": f"uuid-{n}", "publicationId": f"DT0400.{n}_A1" if kind == "purchase" else f"bosh_3_{n}", "kind": kind,
            "title": title or f"Buch {n}", "authors": ["Eva Muster"], "isbn": str(n), "protection": "WATERMARK"}


async def _no_tick(self, _now=None):
    return None


async def _setup(hass, monkeypatch, on=True, known=()):
    """`known`: the titles Audiobookshelf already has (None = read them from the mocked library listing)."""
    monkeypatch.setattr(tolino_import.TolinoImporter, "async_tick", _no_tick)             # the run that starts on switching on: tests call async_run
    if known is not None:
        async def titles(self, abs_client, library_ids):
            return {tolino_import._norm(t) for t in known}
        monkeypatch.setattr(tolino_import.TolinoImporter, "_known_titles", titles)
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)
    monkeypatch.setattr(tolino_import, "FIND_WAIT_S", 0)
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA, "tolino_url": BRIDGE, "tolino_token": "bt"},
                            subentries_data=[await admin_person(hass, tolino=True, import_tolino=on)])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN]["jobs"]["default"]["import"]


def abs_mocks(aioclient_mock, items=(), libraries=None, upload_status=200):
    """Audiobookshelf: the ebook library (with a folder), its items, the upload and the tag write."""
    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": libraries or [
        {"id": LIB, "name": "eBooks", "mediaType": "book", "folders": [{"id": "folder1", "fullPath": "/ebooks"}]}]}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/libraries/{LIB}/items", json={"results": list(items)}, headers=JSON)
    aioclient_mock.post(f"{ABS}/api/upload", status=upload_status, text="OK")
    aioclient_mock.get(f"{ABS}/api/items/new1", json={"id": "new1", "media": {"tags": []}}, headers=JSON)
    aioclient_mock.patch(f"{ABS}/api/items/new1/media", json={}, headers=JSON)


def bridge_mocks(aioclient_mock, books, files=None, file_status=200, file_json=None):
    aioclient_mock.get(f"{BRIDGE}/purchases", json={"count": len(books), "books": books}, headers=JSON)
    for b in books:
        if file_status == 200:
            aioclient_mock.get(f"{BRIDGE}/purchases/{b['id']}/file", content=(files or {}).get(b["id"]) or epub(b["title"], "Eva Muster"))
        else:
            aioclient_mock.get(f"{BRIDGE}/purchases/{b['id']}/file", status=file_status, json=file_json, headers=JSON)


def new_item(title, at=None):
    return {"id": "new1", "addedAt": at or int(time.time() * 1000) + 5000, "media": {"metadata": {"title": title}}}


def calls(aioclient_mock, method, suffix):
    return [c for c in aioclient_mock.mock_calls if c[0] == method and str(c[1]).endswith(suffix)]


def notes(hass):
    return _async_get_or_create_notifications(hass)


async def test_off_by_default_and_nothing_is_touched(hass, monkeypatch, aioclient_mock):
    job = await _setup(hass, monkeypatch, on=False)
    assert job.enabled is False and aioclient_mock.call_count == 0 and job.state["active"] is False


async def test_a_purchase_is_uploaded_tagged_for_the_person_and_remembered(hass, monkeypatch, aioclient_mock):
    job = await _setup(hass, monkeypatch)
    bridge_mocks(aioclient_mock, [purchase(1, "Flüsterwald"), purchase(2, "Totenfang", kind="upload")])
    abs_mocks(aioclient_mock, [new_item("Flüsterwald")])
    summary = await job.async_run()
    assert summary["imported"] == ["Flüsterwald"]                                          # the upload kind is not imported
    assert len(calls(aioclient_mock, "POST", "/api/upload")) == 1 and not calls(aioclient_mock, "GET", "/purchases/uuid-2/file")
    owner = next(iter(get_users(hass).values()))
    patch = calls(aioclient_mock, "PATCH", "/api/items/new1/media")
    assert [c[2] for c in patch] == [{"tags": [person_tag(owner)]}]
    assert job.state["done"]["DT0400.1_A1"]["item_id"] == "new1" and job.is_imported("new1")
    again = await job.async_run()                                                          # second run: nothing new
    assert again["imported"] == [] and len(calls(aioclient_mock, "POST", "/api/upload")) == 1


async def test_what_is_already_in_audiobookshelf_is_not_uploaded_again(hass, monkeypatch, aioclient_mock):
    job = await _setup(hass, monkeypatch, known=None)
    bridge_mocks(aioclient_mock, [purchase(1, "Flüsterwald – Band 1")])
    abs_mocks(aioclient_mock, [{"id": "old", "addedAt": 1, "media": {"metadata": {"title": "Flüsterwald - Band 1"}}}])
    summary = await job.async_run()
    assert summary["already"] == ["Flüsterwald – Band 1"] and not calls(aioclient_mock, "POST", "/api/upload")
    assert not calls(aioclient_mock, "GET", "/purchases/uuid-1/file")                      # not even downloaded
    assert job.state["done"]["DT0400.1_A1"]["how"] == "already_in_abs" and not job.is_imported("old")


async def test_a_big_account_takes_a_few_runs(hass, monkeypatch, aioclient_mock):
    job = await _setup(hass, monkeypatch)
    books = [purchase(n) for n in range(1, 6)]
    bridge_mocks(aioclient_mock, books)
    abs_mocks(aioclient_mock, [new_item(b["title"]) for b in books])
    first = await job.async_run()
    assert len(first["imported"]) == tolino_import.MAX_PER_RUN and first["left"] == 2
    second = await job.async_run()
    assert len(second["imported"]) == 2 and second["left"] == 0


async def test_a_missing_upload_permission_stops_the_run_and_is_retried(hass, monkeypatch, aioclient_mock):
    job = await _setup(hass, monkeypatch)
    bridge_mocks(aioclient_mock, [purchase(1)])
    abs_mocks(aioclient_mock, [new_item("Buch 1")], upload_status=403)
    summary = await job.async_run()
    assert summary["imported"] == [] and job.state["done"] == {} and job.state["failed"] == {}
    assert "books_import_perm" in notes(hass)
    aioclient_mock.clear_requests()
    bridge_mocks(aioclient_mock, [purchase(1)])
    abs_mocks(aioclient_mock, [new_item("Buch 1")])                                        # permission granted
    assert (await job.async_run())["imported"] == ["Buch 1"] and "books_import_perm" not in notes(hass)


async def test_a_book_that_cannot_be_imported_is_reported_once(hass, monkeypatch, aioclient_mock):
    job = await _setup(hass, monkeypatch)
    bridge_mocks(aioclient_mock, [purchase(1)], file_status=422, file_json={"error": "drm", "detail": "encrypted"})
    abs_mocks(aioclient_mock)
    summary = await job.async_run()
    assert summary["failed"] == ["Buch 1"] and "books_import_DT0400.1_A1" in notes(hass)
    await job.async_run()
    assert len(calls(aioclient_mock, "GET", "/purchases/uuid-1/file")) == 1                # never again


async def test_an_unreachable_bridge_changes_nothing(hass, monkeypatch, aioclient_mock):
    job = await _setup(hass, monkeypatch)
    aioclient_mock.get(f"{BRIDGE}/purchases", status=503, json={"error": "login_backoff"}, headers=JSON)
    abs_mocks(aioclient_mock)
    assert (await job.async_run())["imported"] == [] and job.state["done"] == {} and not calls(aioclient_mock, "POST", "/api/upload")


async def test_an_upload_that_is_not_found_yet_is_tagged_on_a_later_run(hass, monkeypatch, aioclient_mock):
    job = await _setup(hass, monkeypatch)
    bridge_mocks(aioclient_mock, [purchase(1, "Flüsterwald")])
    abs_mocks(aioclient_mock, [])                                                          # the watcher has not created the item yet
    assert (await job.async_run())["imported"] == ["Flüsterwald"]
    assert job.state["done"]["DT0400.1_A1"]["item_id"] is None and not calls(aioclient_mock, "PATCH", "/api/items/new1/media")
    aioclient_mock.clear_requests()
    bridge_mocks(aioclient_mock, [purchase(1, "Flüsterwald")])
    abs_mocks(aioclient_mock, [new_item("Flüsterwald")])
    await job.async_run()
    assert job.state["done"]["DT0400.1_A1"]["item_id"] == "new1" and len(calls(aioclient_mock, "PATCH", "/api/items/new1/media")) == 1


async def test_imported_books_are_never_sent_back_to_tolino(hass, monkeypatch, aioclient_mock):
    job = await _setup(hass, monkeypatch)
    job.state["done"]["DT0400.1_A1"] = {"title": "Buch 1", "item_id": "new1", "how": "imported"}
    with pytest.raises(SendError) as exc:
        await async_send_to_tolino(hass, "new1", account="default")
    assert exc.value.code == "already_sent"
    auto = hass.data[DOMAIN]["jobs"]["default"]["auto_send"]
    assert auto._imported("new1") is True and auto._imported("other") is False


async def test_no_ebook_library_is_reported(hass, monkeypatch, aioclient_mock):
    job = await _setup(hass, monkeypatch)
    bridge_mocks(aioclient_mock, [purchase(1)])
    abs_mocks(aioclient_mock, libraries=[{"id": "a", "name": "Hörbücher", "mediaType": "book"}, {"id": "b", "name": "Hörspiele", "mediaType": "book"}])
    assert (await job.async_run())["imported"] == [] and "books_import_library" in notes(hass)


def test_title_and_author_come_from_the_epub():
    assert tolino_import.epub_meta(epub("Flüsterwald", "Andreas Suchanek")) == {"title": "Flüsterwald", "author": "Andreas Suchanek"}
    assert tolino_import.epub_meta(b"not a zip") == {}


def test_the_ebook_library_is_picked_by_name():
    libs = [{"id": "a", "name": "Hörbücher", "mediaType": "book"}, {"id": "b", "name": "eBooks", "mediaType": "book"}, {"id": "c", "name": "x", "mediaType": "podcast"}]
    assert tolino_import.target_library(libs)["id"] == "b"
    assert tolino_import.target_library(libs[:1])["id"] == "a"                             # the only book library
    assert tolino_import.target_library(libs[:1] + [{"id": "d", "name": "Hörspiele", "mediaType": "book"}]) is None


async def test_the_auto_send_run_skips_an_imported_book(hass, monkeypatch, aioclient_mock):
    job = await _setup(hass, monkeypatch)
    auto = hass.data[DOMAIN]["jobs"]["default"]["auto_send"]
    auto.state["since"] = 1
    item = {"id": "new1", "addedAt": int(time.time() * 1000), "media": {"ebookFormat": "epub", "metadata": {"title": "Buch 1"}}}
    other = {**item, "id": "other"}
    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": [{"id": LIB, "mediaType": "book"}]}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/libraries/{LIB}/items", json={"results": [item, other]}, headers=JSON)
    seen = []
    async def fake_send(hass_, item_id, **kw):
        seen.append(item_id)
        return {"ok": True}
    monkeypatch.setattr("custom_components.books.tolino_autosend.async_send_to_tolino", fake_send)
    job.state["done"]["DT0400.1_A1"] = {"title": "Buch 1", "item_id": "new1", "how": "imported"}
    await auto.async_run()
    assert seen == ["other"]                                                                # the imported one stays where it came from


async def test_the_books_own_title_counts_for_the_duplicate_check(hass, monkeypatch, aioclient_mock):
    """The shop calls it "Feenspiele - der Fantasy Bestseller", the EPUB (and so Audiobookshelf) "The Faerie Games"."""
    job = await _setup(hass, monkeypatch, known=["The Faerie Games"])
    bridge_mocks(aioclient_mock, [purchase(1, "Feenspiele - der Fantasy Bestseller")], files={"uuid-1": epub("The Faerie Games", "Michelle Madow")})
    abs_mocks(aioclient_mock)
    summary = await job.async_run()
    assert summary["already"] == ["Feenspiele - der Fantasy Bestseller"] and summary["imported"] == []
    assert not calls(aioclient_mock, "POST", "/api/upload") and job.state["done"]["DT0400.1_A1"]["how"] == "already_in_abs"
