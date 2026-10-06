"""Taking something off the download list: queue entry (and blocklist), optionally the book and the author - never anything with files."""
import pytest

from .conftest import CHAPTARR

JSON = {"Content-Type": "application/json"}
QUEUE = {"records": [{"id": 522730524, "bookId": 882, "authorId": 20, "title": "Death Note 01  Tsugumi Ohba, Takeshi Obata"},
                     {"id": 111, "bookId": 5, "authorId": 7, "title": "Anderes"}]}
EMPTY = {"bookFileCount": 0, "sizeOnDisk": 0}


def world(aioclient_mock, book_stats=EMPTY, author_stats=EMPTY, queue=QUEUE):
    aioclient_mock.get(f"{CHAPTARR}/api/v1/queue", json=queue, headers=JSON)
    aioclient_mock.delete(f"{CHAPTARR}/api/v1/queue/522730524", status=200, json={}, headers=JSON)
    aioclient_mock.get(f"{CHAPTARR}/api/v1/book/882", json={"id": 882, "authorId": 20, "statistics": book_stats}, headers=JSON)
    aioclient_mock.get(f"{CHAPTARR}/api/v1/author/20", json={"id": 20, "authorName": "Tsugumi Ohba", "statistics": author_stats}, headers=JSON)
    aioclient_mock.delete(f"{CHAPTARR}/api/v1/book/882", status=200, json={}, headers=JSON)
    aioclient_mock.delete(f"{CHAPTARR}/api/v1/author/20", status=200, json={}, headers=JSON)


def deletes(aioclient_mock):
    return [(str(c[1]).split("/api/v1")[1].split("?")[0], dict(c[1].query)) for c in aioclient_mock.mock_calls if c[0] == "DELETE"]


async def post(hass_client, body):
    return await (await hass_client()).post("/api/books/downloads/remove", json=body)


async def test_the_queue_entry_goes_off_the_downloader_and_onto_the_blocklist(hass, setup_entry, hass_client, aioclient_mock):
    world(aioclient_mock)
    resp = await post(hass_client, {"queue_id": 522730524})
    assert resp.status == 200 and (await resp.json()) == {"removed": {"queue": True, "book": False, "author": False}, "kept": []}
    assert deletes(aioclient_mock) == [("/queue/522730524", {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "true"})]
    assert aioclient_mock.mock_calls[-1][3]["X-Api-Key"] == "chaptarr-key"


async def test_it_can_leave_the_release_off_the_blocklist(hass, setup_entry, hass_client, aioclient_mock):
    world(aioclient_mock)
    await post(hass_client, {"queue_id": 522730524, "blocklist": False})
    assert deletes(aioclient_mock)[0][1]["blocklist"] == "false"


async def test_the_author_goes_with_everything_chaptarr_knows_of_him_when_no_file_exists(hass, setup_entry, hass_client, aioclient_mock):
    world(aioclient_mock)
    resp = await post(hass_client, {"queue_id": 522730524, "remove_author": True, "remove_book": True})
    assert (await resp.json())["removed"] == {"queue": True, "book": True, "author": True}
    assert deletes(aioclient_mock) == [("/queue/522730524", {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "true"}),
                                       ("/author/20", {"deleteFiles": "false", "addImportListExclusion": "false"})]       # one delete covers his books


async def test_an_author_with_files_is_kept_and_so_is_a_book_with_files(hass, setup_entry, hass_client, aioclient_mock):
    world(aioclient_mock, book_stats={"bookFileCount": 1, "sizeOnDisk": 5}, author_stats={"bookFileCount": 3, "sizeOnDisk": 100})
    body = await (await post(hass_client, {"queue_id": 522730524, "remove_author": True, "remove_book": True})).json()
    assert body["removed"] == {"queue": True, "book": False, "author": False} and body["kept"] == ["author_has_files", "book_has_files"]
    assert [d[0] for d in deletes(aioclient_mock)] == ["/queue/522730524"]                    # nothing but the queue entry


async def test_a_book_alone_can_go_while_the_author_stays(hass, setup_entry, hass_client, aioclient_mock):
    world(aioclient_mock)
    body = await (await post(hass_client, {"queue_id": 522730524, "remove_book": True})).json()
    assert body["removed"]["book"] is True and body["removed"]["author"] is False
    assert [d[0] for d in deletes(aioclient_mock)] == ["/queue/522730524", "/book/882"]
    assert deletes(aioclient_mock)[1][1] == {"deleteFiles": "false", "addImportListExclusion": "false"}      # imported files are never deleted


async def test_a_wanted_book_without_a_queue_entry_can_be_removed_too(hass, setup_entry, hass_client, aioclient_mock):
    world(aioclient_mock)
    body = await (await post(hass_client, {"book_id": 882, "remove_book": True})).json()
    assert body["removed"] == {"queue": False, "book": True, "author": False} and [d[0] for d in deletes(aioclient_mock)] == ["/book/882"]


async def test_an_entry_that_is_not_in_the_queue_any_more_is_a_404_and_nothing_is_deleted(hass, setup_entry, hass_client, aioclient_mock):
    world(aioclient_mock, queue={"records": []})
    resp = await post(hass_client, {"queue_id": 522730524})
    assert resp.status == 404 and (await resp.json())["code"] == "not_found" and deletes(aioclient_mock) == []


@pytest.mark.parametrize("body", [
    {}, {"queue_id": "5"}, {"queue_id": 0}, {"queue_id": -3}, {"queue_id": True}, {"queue_id": 1.5}, {"queue_id": 1, "book_id": 2},
    {"book_id": 5}, {"book_id": 5, "remove_book": "yes"}, {"queue_id": 5, "blocklist": 1}, {"queue_id": 5, "remove_author": None},
])
async def test_bad_requests_are_refused_before_chaptarr_is_asked(hass, setup_entry, hass_client, aioclient_mock, body):
    world(aioclient_mock)
    resp = await post(hass_client, body)
    assert resp.status == 400 and not aioclient_mock.mock_calls


async def test_chaptarr_trouble_is_reported_not_swallowed(hass, setup_entry, hass_client, aioclient_mock):
    world(aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{CHAPTARR}/api/v1/queue", json=QUEUE, headers=JSON)
    aioclient_mock.delete(f"{CHAPTARR}/api/v1/queue/522730524", status=500, json={"message": "boom"}, headers=JSON)
    resp = await post(hass_client, {"queue_id": 522730524})
    assert resp.status == 502


async def test_the_proxy_still_refuses_deletes(hass, setup_entry, hass_client, aioclient_mock):
    """The new endpoint is the only way to remove something: the free-form proxy stays closed for DELETE."""
    aioclient_mock.delete(f"{CHAPTARR}/api/v1/queue/1", status=200, json={})
    client = await hass_client()
    for path in ("queue/1?removeFromClient=true", "book/5?deleteFiles=true", "author/20"):
        assert (await client.delete(f"/api/books/chaptarr/{path}")).status == 403
    assert not aioclient_mock.mock_calls
