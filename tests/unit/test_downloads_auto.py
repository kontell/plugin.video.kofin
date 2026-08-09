"""L1 units for the automatic download policy (plan W4.1/W4.4)."""

import pytest

from kofin.downloads import auto, store
from kofin.sync import db as sync_db
from kofin.sync.newcontent import Entry
from tests.unit.fakes import FakeAddon, FakeWindow


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    FakeAddon.store = {"downloadsEnabled": "true"}
    FakeWindow.store = {"kofin.online": "true"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    notified = []
    monkeypatch.setattr(auto.ipc, "notify", lambda m, d=None: notified.append((m, d)))
    yield notified
    sync_db.reset_overrides()


class FakeAutoApi:
    def __init__(self, episodes):
        self._episodes = list(episodes)
        self.item_params = []

    def items(self, params):
        self.item_params.append(dict(params))
        start = params.get("StartIndex", 0)
        rows = self._episodes[start : start + params.get("Limit", 200)]
        return {"Items": rows, "TotalRecordCount": len(self._episodes)}


def episode(item_id, played=False, downloadable=True):
    row = {"Id": item_id, "UserData": {"Played": played}}
    if not downloadable:
        row["CanDownload"] = False
    return row


def test_next_episode_ids_walks_forward_filtering(env):
    store.queue(store.Download(jellyfin_id="e4", queued_at=100))  # already live
    api = FakeAutoApi(
        [
            episode("e1"),  # before current: never considered
            episode("e2"),  # current
            episode("e3", played=True),  # watched: skipped
            episode("e4"),  # live in the store: skipped
            episode("e5", downloadable=False),  # server refuses: skipped
            episode("e6"),
            episode("e7"),
            episode("e8"),
        ]
    )
    assert auto.next_episode_ids(api, "s1", "e2", 2) == ["e6", "e7"]
    # The 10.11 arity rule: two SortBys, two SortOrders.
    params = api.item_params[0]
    assert params["SortBy"] == "ParentIndexNumber,IndexNumber"
    assert params["SortOrder"] == "Ascending,Ascending"


def test_trigger_next_gates_then_queues_with_the_series_origin(env):
    item = {"Id": "e2", "SeriesId": "s1"}
    api = FakeAutoApi([episode("e2"), episode("e3")])

    assert auto.trigger_next(api, item) is False  # toggle off
    FakeAddon.store["downloadsAutoNext"] = "true"
    assert auto.trigger_next(api, item) is False  # current not downloaded

    store.queue(store.Download(jellyfin_id="e2", queued_at=100))
    store.claim()
    store.finish("e2", "TV/S/Season 01/e2.mkv", "mkv", 1)

    FakeWindow.store["kofin.online"] = "false"
    assert auto.trigger_next(api, item) is False  # offline: nobody to ask
    FakeWindow.store["kofin.online"] = "true"

    assert auto.trigger_next(api, item) is True
    assert env == [(auto.ipc.DOWNLOAD_ADD, {"Ids": ["e3"], "Origin": "auto:s1"})]


def test_queue_new_movies_gates_caps_and_dedupes(env):
    entries = [Entry("Movie", "m1", "One"), Entry("Series", "s1", "Show")]

    assert auto.queue_new_movies(entries) == 0  # toggle off
    FakeAddon.store["downloadsAutoMovies"] = "true"

    store.queue(store.Download(jellyfin_id="m1", queued_at=100))
    assert auto.queue_new_movies(entries) == 0  # already live; series ignored
    assert env == []

    fresh = [Entry("Movie", "m%d" % index, "M") for index in range(2, 5)]
    assert auto.queue_new_movies(fresh) == 3
    assert env[-1] == (
        auto.ipc.DOWNLOAD_ADD,
        {"Ids": ["m2", "m3", "m4"], "Origin": auto.ORIGIN_NEW_MOVIES},
    )


def test_queue_new_movies_skips_a_bulk_import_entirely(env, monkeypatch):
    FakeAddon.store["downloadsAutoMovies"] = "true"
    toasts = []
    monkeypatch.setattr(auto.toast, "show", lambda *a, **k: toasts.append(a))
    monkeypatch.setattr(auto.settings, "localized", lambda i: "L%d %%s" % i)
    bulk = [Entry("Movie", "m%d" % index, "M") for index in range(10)]

    assert auto.queue_new_movies(bulk) == 0  # none, not an arbitrary few
    assert env == []
    assert toasts  # and it says so
