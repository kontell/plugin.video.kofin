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


# -- subscriptions and the episode/album arms (plan W4.6) ---------------------


def test_show_subscriptions_round_trip():
    assert auto.subscribed_shows() == []
    assert auto.toggle_show("s1") is True
    assert auto.toggle_show("s2") is True
    assert auto.subscribed_shows() == ["s1", "s2"]
    assert auto.toggle_show("s1") is False
    assert auto.subscribed_shows() == ["s2"]
    auto.save_subscribed_shows(["a", "a", "", "b"])  # dedupes and drops empties
    assert auto.subscribed_shows() == ["a", "b"]


def episode_entry(item_id, series_id):
    return Entry("Episode", item_id, "E", series="Show", series_id=series_id)


def test_subscribed_shows_download_uncapped_with_series_origins(env):
    auto.save_subscribed_shows(["s1"])
    entries = [episode_entry("e%d" % index, "s1") for index in range(20)]
    entries.append(episode_entry("x1", "s2"))  # not subscribed, toggle off

    assert auto.queue_new_episodes(entries) == 20  # a whole season: no cap
    assert env == [
        (
            auto.ipc.DOWNLOAD_ADD,
            {"Ids": ["e%d" % index for index in range(20)], "Origin": "auto:s1"},
        )
    ]


def test_global_episodes_cap_spares_subscriptions(env, monkeypatch):
    monkeypatch.setattr(auto.settings, "localized", lambda i: "L%d %%s" % i)
    FakeAddon.store["downloadsAutoEpisodes"] = "true"
    auto.save_subscribed_shows(["s1"])
    entries = [episode_entry("sub%d" % index, "s1") for index in range(3)]
    entries += [episode_entry("bulk%d" % index, "s9") for index in range(15)]

    assert auto.queue_new_episodes(entries) == 3  # the import skipped whole
    assert len(env) == 1 and env[0][1]["Origin"] == "auto:s1"


def test_global_episodes_group_by_series(env):
    FakeAddon.store["downloadsAutoEpisodes"] = "true"
    store.queue(store.Download(jellyfin_id="a2", queued_at=100))  # already live
    entries = [
        episode_entry("a1", "sA"),
        episode_entry("a2", "sA"),
        episode_entry("b1", "sB"),
    ]

    assert auto.queue_new_episodes(entries) == 2
    assert env == [
        (auto.ipc.DOWNLOAD_ADD, {"Ids": ["a1"], "Origin": "auto:sA"}),
        (auto.ipc.DOWNLOAD_ADD, {"Ids": ["b1"], "Origin": "auto:sB"}),
    ]


class FakeAlbumApi:
    def __init__(self, tracks_by_album):
        self._tracks = tracks_by_album

    def items(self, params):
        rows = self._tracks.get(params.get("ParentId"), [])
        return {"Items": rows, "TotalRecordCount": len(rows)}


def test_new_albums_expand_to_tracks(env):
    FakeAddon.store["downloadsAutoAlbums"] = "true"
    api = FakeAlbumApi(
        {
            "al1": [{"Id": "t1"}, {"Id": "t2"}, {"Id": "t3", "CanDownload": False}],
            "al2": [{"Id": "t4"}],
        }
    )
    store.queue(store.Download(jellyfin_id="t4", queued_at=100))  # already live
    entries = [
        Entry("MusicAlbum", "al1", "One"),
        Entry("MusicAlbum", "al2", "Two"),
        Entry("MusicArtist", "ar1", "Artist"),  # never expanded: no per-artist arm
    ]

    assert auto.queue_new_albums(api, entries) == 2
    assert env == [(auto.ipc.DOWNLOAD_ADD, {"Ids": ["t1", "t2"], "Origin": "auto:new"})]


def test_new_albums_bulk_import_skips(env, monkeypatch):
    monkeypatch.setattr(auto.settings, "localized", lambda i: "L%d %%s" % i)
    FakeAddon.store["downloadsAutoAlbums"] = "true"
    entries = [Entry("MusicAlbum", "al%d" % index, "A") for index in range(9)]
    assert auto.queue_new_albums(FakeAlbumApi({}), entries) == 0
    assert env == []


def test_queue_new_content_runs_every_arm(env, monkeypatch):
    FakeAddon.store.update(
        {"downloadsAutoMovies": "true", "downloadsAutoEpisodes": "true"}
    )
    entries = [Entry("Movie", "m1", "M"), episode_entry("e1", "s1")]
    auto.queue_new_content(FakeAlbumApi({}), entries)
    origins = [payload["Origin"] for _message, payload in env]
    assert origins == ["auto:new", "auto:s1"]

    env.clear()
    FakeAddon.store["downloadsEnabled"] = "false"
    auto.queue_new_content(FakeAlbumApi({}), entries)
    assert env == []  # the master toggle gates every arm
