"""Auto-send of NEW ebooks: only after switching on, only new items, sensible failure handling."""
import time

import aiohttp
import pytest
from homeassistant.components.persistent_notification import _async_get_or_create_notifications
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.books import tolino_autosend
from custom_components.books.const import DOMAIN

from .conftest import ABS, ENTRY_DATA, admin_person

BRIDGE = "http://bridge.test:8199"
JSON = {"Content-Type": "application/json"}
LIB = "lib1"
NOW = int(time.time() * 1000)
NEW = NOW + 1000
OLD = NOW - 10 * 60 * 1000


async def _setup(hass, monkeypatch, auto=True, extra=None):
    async def healthy(self):
        return {"reachable": True, "logged_in": True, "problem": False}
    monkeypatch.setattr("custom_components.books.tolino_watch.TolinoWatcher._async_update_data", healthy)
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA, "tolino_url": BRIDGE, "tolino_token": "bt", **(extra or {})},
                            subentries_data=[await admin_person(hass, tolino=True, auto_send=auto)])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    job = hass.data[DOMAIN]["jobs"]["default"]["auto_send"]
    job.state["since"] = NOW                                   # deterministic: "new" means added after NOW
    return job


@pytest.fixture
async def job(hass, monkeypatch):
    return await _setup(hass, monkeypatch)


def listing(aioclient_mock, items, libraries=None):
    aioclient_mock.get(f"{ABS}/api/libraries", json={"libraries": libraries or [
        {"id": LIB, "mediaType": "book"}, {"id": "pod", "mediaType": "podcast"}]}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/libraries/{LIB}/items", json={"results": sorted(items, key=lambda i: -i["addedAt"])}, headers=JSON)


def it(item_id, added=NEW, fmt="epub", title=None):
    return {"id": item_id, "addedAt": added, "media": {"ebookFormat": fmt, "metadata": {"title": title or f"Buch {item_id}"}}}


def book(aioclient_mock, item_id, fmt="epub"):
    aioclient_mock.get(f"{ABS}/api/items/{item_id}", json={"media": {"metadata": {"title": f"Buch {item_id}"},
                       "ebookFile": {"ebookFormat": fmt, "metadata": {"filename": f"{item_id}.{fmt}"}}}}, headers=JSON)
    aioclient_mock.get(f"{ABS}/api/items/{item_id}/ebook", content=b"BYTES-" + item_id.encode())
    aioclient_mock.get(f"{ABS}/api/items/{item_id}/cover", content=b"\xff\xd8\xff\xe0c")


def upload_ok(aioclient_mock, did="d1"):
    aioclient_mock.post(f"{BRIDGE}/upload", json={"deliverableId": did, "cover": True}, headers=JSON)


def uploads(aioclient_mock):
    return [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/upload")]


async def test_off_by_default(hass, monkeypatch, aioclient_mock):
    job = await _setup(hass, monkeypatch, auto=False)
    assert job.enabled is False and await job.async_tick() is None and aioclient_mock.call_count == 0
    assert job.state["active"] is False


async def test_switching_on_sets_a_baseline_and_touches_nothing(hass, monkeypatch, aioclient_mock):
    before = int(time.time() * 1000)
    job = await _setup(hass, monkeypatch, auto=True)
    assert job.state["active"] is True and before - 3 * 60 * 1000 < job.state["since"] <= before
    assert aioclient_mock.call_count == 0                                     # enabling alone sends and reads nothing


async def test_sends_a_new_epub(hass, job, aioclient_mock):
    listing(aioclient_mock, [it("n1")]); book(aioclient_mock, "n1"); upload_ok(aioclient_mock)
    summary = await job.async_run()
    assert summary["sent"] == ["n1"] and summary["failed"] == []
    assert len(uploads(aioclient_mock)) == 1 and hass.data[DOMAIN]["tolino_sent"].get("n1")["deliverableId"] == "d1"


async def test_old_items_and_non_books_are_left_alone(hass, job, aioclient_mock):
    listing(aioclient_mock, [it("old", OLD), it("n1")]); book(aioclient_mock, "n1"); book(aioclient_mock, "old"); upload_ok(aioclient_mock)
    summary = await job.async_run()
    assert summary["sent"] == ["n1"]                                          # the existing library is never touched
    assert not any("/items/old" in str(c[1]) for c in aioclient_mock.mock_calls)
    assert not any("/libraries/pod/" in str(c[1]) for c in aioclient_mock.mock_calls)   # podcast libraries are not read


async def test_items_without_a_usable_ebook_are_ignored_not_failed(hass, job, aioclient_mock):
    listing(aioclient_mock, [it("audio", fmt=None), it("comic", fmt="cbz")])
    summary = await job.async_run()
    assert summary["sent"] == [] and summary["failed"] == [] and uploads(aioclient_mock) == []
    assert job.state["failed"] == {}                                          # may get its ebook later


async def test_already_sent_books_are_skipped(hass, job, aioclient_mock):
    await hass.data[DOMAIN]["tolino_sent"].async_set("n1", "dx", "x.epub")
    listing(aioclient_mock, [it("n1")]); book(aioclient_mock, "n1"); upload_ok(aioclient_mock)
    assert (await job.async_run())["sent"] == [] and uploads(aioclient_mock) == []


async def test_kindle_formats_are_sent_for_conversion(hass, job, aioclient_mock):
    listing(aioclient_mock, [it("m1", fmt="mobi")]); book(aioclient_mock, "m1", "mobi"); upload_ok(aioclient_mock)
    assert (await job.async_run())["sent"] == ["m1"]


async def test_second_run_is_idempotent(hass, job, aioclient_mock):
    listing(aioclient_mock, [it("n1")]); book(aioclient_mock, "n1"); upload_ok(aioclient_mock)
    await job.async_run()
    assert (await job.async_run())["sent"] == [] and len(uploads(aioclient_mock)) == 1


async def test_at_most_five_per_run_the_rest_follows(hass, job, aioclient_mock):
    ids = [f"n{i}" for i in range(7)]
    listing(aioclient_mock, [it(i, NEW + n) for n, i in enumerate(ids)]); upload_ok(aioclient_mock)
    for i in ids: book(aioclient_mock, i)
    first = await job.async_run()
    assert len(first["sent"]) == tolino_autosend.MAX_PER_RUN and len(first["skipped"]) == 2
    assert first["sent"] == ids[:5]                                           # oldest new one first
    second = await job.async_run()
    assert second["sent"] == ids[5:]


async def test_permanent_failure_is_reported_once_and_not_retried(hass, monkeypatch, aioclient_mock):
    pushes = async_mock_service(hass, "notify", "mobile_app_t")
    job = await _setup(hass, monkeypatch, extra={"notify_service": "notify.mobile_app_t"})
    listing(aioclient_mock, [it("bad", fmt="mobi"), it("good", NEW + 5)]); book(aioclient_mock, "bad", "mobi"); book(aioclient_mock, "good")
    aioclient_mock.post(f"{BRIDGE}/upload", status=422, json={"error": "convert_failed", "detail": "DRM-geschützt?"}, headers=JSON)
    first = await job.async_run()
    assert first["failed"][0] == "bad"
    assert len(pushes) >= 1 and "DRM" in pushes[0].data["message"] and "Buch bad" in pushes[0].data["message"]
    assert f"books_autosend_bad" in _async_get_or_create_notifications(hass)
    assert job.state["failed"]["bad"]["error"] == "convert_failed"
    n_uploads = len(uploads(aioclient_mock)); sent_pushes = len(pushes)
    await job.async_run()
    assert "bad" not in (await job.async_run())["sent"] and len(pushes) == sent_pushes     # silent afterwards
    assert len([c for c in uploads(aioclient_mock)]) >= n_uploads


async def test_bridge_trouble_is_transient_and_stops_the_run(hass, job, aioclient_mock):
    listing(aioclient_mock, [it("a", NEW), it("b", NEW + 5)]); book(aioclient_mock, "a"); book(aioclient_mock, "b")
    aioclient_mock.post(f"{BRIDGE}/upload", status=503, json={"error": "captcha", "detail": "blocked"}, headers=JSON)
    summary = await job.async_run()
    assert summary["sent"] == [] and summary["failed"] == [] and len(uploads(aioclient_mock)) == 1   # stopped after the first
    assert job.state["failed"] == {}                                                                 # nothing is given up on
    aioclient_mock.clear_requests(); listing(aioclient_mock, [it("a", NEW), it("b", NEW + 5)]); book(aioclient_mock, "a"); book(aioclient_mock, "b"); upload_ok(aioclient_mock)
    assert (await job.async_run())["sent"] == ["a", "b"]                                              # retried next time


async def test_bridge_unreachable_stops_quietly(hass, job, aioclient_mock):
    listing(aioclient_mock, [it("a")]); book(aioclient_mock, "a")
    aioclient_mock.post(f"{BRIDGE}/upload", exc=aiohttp.ClientConnectionError("down"))
    summary = await job.async_run()
    assert summary["sent"] == [] and job.state["failed"] == {}


async def test_audiobookshelf_errors_do_not_crash(hass, job, aioclient_mock):
    aioclient_mock.get(f"{ABS}/api/libraries", status=500, json={"error": "x"}, headers=JSON)
    assert await job.async_run() == {"checked": 0, "sent": [], "failed": [], "skipped": []}
    assert await job.async_tick() is not None                                  # the timer wrapper survives too


async def test_switching_off_and_on_again_resets_the_baseline(hass, monkeypatch):
    job = await _setup(hass, monkeypatch, auto=True)
    job.state["since"] = 12345
    await job._store.async_save(job.state)                                     # what a real run leaves behind
    person = next(iter(hass.data[DOMAIN]["users"].values()))
    person["auto_send"] = False
    await job.async_start()
    assert job.state["active"] is False and job.state["since"] == 12345
    person["auto_send"] = True
    await job.async_start()
    assert job.state["active"] is True and job.state["since"] > NOW - 5 * 60 * 1000          # new baseline, old gap not sent


async def test_needs_a_bridge(hass, monkeypatch):
    entry = MockConfigEntry(domain=DOMAIN, title="Books", data={**ENTRY_DATA},
                            subentries_data=[await admin_person(hass, tolino=True, auto_send=True)])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id); await hass.async_block_till_done()
    assert hass.data[DOMAIN]["jobs"]["default"]["auto_send"].enabled is False


async def test_manual_endpoint(hass, job, hass_client, hass_client_no_auth, aioclient_mock):
    listing(aioclient_mock, [it("n1")]); book(aioclient_mock, "n1"); upload_ok(aioclient_mock)
    assert (await (await hass_client_no_auth()).post("/api/books/tolino-autosend")).status == 401
    resp = await (await hass_client()).post("/api/books/tolino-autosend")
    assert resp.status == 200 and (await resp.json())["sent"] == ["n1"]


async def test_manual_endpoint_refuses_when_off(hass, monkeypatch, hass_client):
    await _setup(hass, monkeypatch, auto=False)
    resp = await (await hass_client()).post("/api/books/tolino-autosend")
    assert resp.status == 409 and (await resp.json())["code"] == "autosend_disabled"
