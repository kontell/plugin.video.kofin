"""The pager on its own (P2.4, downloader._get_items): no Kodi, no
settings, no window -- an api that answers pages, the page size and the
pool width as arguments, and the stop check injected."""

import threading

import pytest

from kofin.sync import downloader
from kofin.sync.shims import LibraryExitException


class PagingApi:
    """Answers /Items by StartIndex/Limit over a fixed list; records every
    request; can fail one page or the count."""

    user_id = "user1"

    def __init__(self, count, fail_page=None, fail_count=False):
        self.items = [{"Id": "i%d" % i} for i in range(count)]
        self.requests = []
        self.fail_page = fail_page
        self.fail_count = fail_count
        self.lock = threading.Lock()

    def get(self, url, params):
        with self.lock:
            self.requests.append(dict(params))
        if params.get("EnableTotalRecordCount") and params.get("Limit") == 1:
            if self.fail_count:
                raise RuntimeError("count failed")
            return {"TotalRecordCount": len(self.items), "Items": []}
        start = params["StartIndex"]
        if self.fail_page is not None and start == self.fail_page:
            raise RuntimeError("page %d failed" % start)
        return {"Items": self.items[start : start + params["Limit"]]}


def pages(api, limit=2, threads=2, start=0, should_stop=lambda: None):
    query = {"url": "/Items", "params": {"StartIndex": start} if start else {}}
    return downloader._get_items(api, query, limit, threads, should_stop)


def test_pages_arrive_in_order_each_a_fresh_dict():
    api = PagingApi(5)

    got = list(pages(api, limit=2, threads=3))

    assert [[i["Id"] for i in page["Items"]] for page in got] == [
        ["i0", "i1"],
        ["i2", "i3"],
        ["i4"],
    ]
    assert all(page["TotalRecordCount"] == 5 for page in got)
    assert [page["RestorePoint"]["params"]["StartIndex"] for page in got] == [0, 2, 4]
    assert got[0] is not got[1] and got[0]["Items"] is not got[1]["Items"]
    assert got[0]["RestorePoint"]["url"] == "/Items"


def test_a_kept_page_survives_the_next_one():
    """The fork yielded one dict, cleared after each page; a consumer that
    held on to it saw it emptied."""
    api = PagingApi(4)
    first = None
    for page in pages(api, limit=2):
        if first is None:
            first = page
    assert [i["Id"] for i in first["Items"]] == ["i0", "i1"]


def test_the_restore_point_names_only_pages_already_handed_out():
    api = PagingApi(6)
    seen = []
    for page in pages(api, limit=2, threads=3):
        seen.append(page["RestorePoint"]["params"]["StartIndex"])
        if len(seen) == 2:
            break
    assert seen == [0, 2]


def test_resumes_from_the_start_index_it_was_given():
    api = PagingApi(6)

    got = list(pages(api, limit=2, start=4))

    assert [[i["Id"] for i in page["Items"]] for page in got] == [["i4", "i5"]]
    assert all(
        r["StartIndex"] >= 4 for r in api.requests if "Limit" in r and r["Limit"] != 1
    )


def test_the_page_size_and_pool_width_are_arguments_not_settings():
    api = PagingApi(7)

    got = list(pages(api, limit=3, threads=1))

    assert [len(page["Items"]) for page in got] == [3, 3, 1]
    assert {r["Limit"] for r in api.requests if r.get("Limit") != 1} == {3}


def test_a_failed_count_raises_instead_of_yielding_nothing():
    api = PagingApi(3, fail_count=True)
    with pytest.raises(RuntimeError, match="count failed"):
        list(pages(api))


def test_a_failed_page_raises_and_the_pool_shuts_down():
    api = PagingApi(6, fail_page=2)
    with pytest.raises(RuntimeError, match="page 2 failed"):
        list(pages(api, limit=2, threads=2))


def test_should_stop_is_asked_before_every_page():
    """The fork's @stop on the generator ran once, at creation; a service
    shutdown mid-walk went unnoticed until the writer's own @stop."""
    api = PagingApi(6)
    asked = []

    def should_stop():
        asked.append(True)
        if len(asked) == 3:  # before the count, before page 1, then stop
            raise LibraryExitException("stopping")

    got = []
    with pytest.raises(LibraryExitException):
        for page in pages(api, limit=2, threads=2, should_stop=should_stop):
            got.append(page)

    assert len(got) == 1
    assert len(asked) == 3


def test_an_empty_result_set_yields_no_pages():
    api = PagingApi(0)
    assert list(pages(api)) == []
