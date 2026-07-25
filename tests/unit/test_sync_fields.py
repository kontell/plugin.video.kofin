"""L1 units for ``fields.find_library``: the whitelisted-ancestor lookup the
writers fall back on when an item arrives without library context.

The lookup costs an ``/Items/{id}/Ancestors`` round trip the server is slow to
answer (~450ms against a real library), and it was paid once per item — which
is what held a queued backlog to roughly one item a second. Siblings share a
parent, hence a library, so the answer memoizes exactly per parent.
"""

import pytest

from kofin.sync import fields


class CountingApi:
    """Records every /Ancestors walk so the tests can count round trips."""

    def __init__(self, chain_by_id):
        self.chain_by_id = chain_by_id
        self.calls = []

    def ancestors(self, item_id):
        self.calls.append(item_id)
        return self.chain_by_id.get(item_id, [])


LIBRARY = {"Id": "lib2", "Name": "Music"}
ARTIST = {"Id": "artist1", "Name": "Some Artist"}


@pytest.fixture
def whitelist(monkeypatch):
    """Patch the whitelist find_library reads out of sync.json."""
    state = {"Whitelist": ["lib2"]}

    monkeypatch.setattr(
        "kofin.sync.db.get_sync",
        lambda: {
            "Libraries": [],
            "RestorePoints": {},
            "Whitelist": list(state["Whitelist"]),
            "SortedViews": [],
        },
    )
    return state


def album_api(album_ids):
    return CountingApi({album_id: [ARTIST, LIBRARY] for album_id in album_ids})


def test_resolves_the_whitelisted_ancestor(whitelist):
    api = album_api(["al1"])

    found = fields.find_library(api, {"Id": "al1", "ParentId": "artist1"})

    assert found == LIBRARY
    assert api.calls == ["al1"]


def test_siblings_share_one_lookup(whitelist):
    """Ten tracks of one album used to cost ten round trips."""
    api = CountingApi(
        {"so%d" % n: [ARTIST, LIBRARY] for n in range(10)},
    )
    cache = {}

    for n in range(10):
        found = fields.find_library(api, {"Id": "so%d" % n, "ParentId": "al1"}, cache)
        assert found == LIBRARY

    assert api.calls == ["so0"]


def test_different_parents_are_resolved_separately(whitelist):
    api = album_api(["al1", "al2"])
    cache = {}

    fields.find_library(api, {"Id": "al1", "ParentId": "artist1"}, cache)
    fields.find_library(api, {"Id": "al2", "ParentId": "artist2"}, cache)

    assert api.calls == ["al1", "al2"]


def test_without_a_cache_nothing_is_memoized(whitelist):
    """The realtime callers that pass no cache keep their old behaviour."""
    api = album_api(["al1", "al2"])

    fields.find_library(api, {"Id": "al1", "ParentId": "artist1"})
    fields.find_library(api, {"Id": "al2", "ParentId": "artist1"})

    assert api.calls == ["al1", "al2"]


def test_parentless_items_are_not_memoized(whitelist):
    """No parent, no key: a top-level item cannot stand in for a sibling."""
    api = album_api(["al1", "al2"])
    cache = {}

    fields.find_library(api, {"Id": "al1"}, cache)
    fields.find_library(api, {"Id": "al2"}, cache)

    assert api.calls == ["al1", "al2"]
    assert cache == {}


def test_a_dropped_library_is_not_served_from_the_memo(whitelist):
    """The whitelist can change mid-drain from the settings dialog; a memo that
    outlived it would keep writing into a de-selected library."""
    api = album_api(["so1", "so2"])
    cache = {}

    assert fields.find_library(api, {"Id": "so1", "ParentId": "al1"}, cache) == LIBRARY

    whitelist["Whitelist"] = []

    assert fields.find_library(api, {"Id": "so2", "ParentId": "al1"}, cache) == {}
    assert api.calls == ["so1", "so2"]


def test_unwhitelisted_items_report_no_library(whitelist):
    api = CountingApi({"x1": [{"Id": "other", "Name": "Other"}]})

    assert fields.find_library(api, {"Id": "x1", "ParentId": "p1"}, {}) == {}


def test_mixed_libraries_match_on_their_bare_id(whitelist):
    """Mixed entries are stored prefixed; the ancestor carries the bare id."""
    whitelist["Whitelist"] = ["Mixed:lib9"]
    api = CountingApi({"m1": [{"Id": "lib9", "Name": "Mixed"}]})

    found = fields.find_library(api, {"Id": "m1", "ParentId": "p1"}, {})

    assert found == {"Id": "lib9", "Name": "Mixed"}
