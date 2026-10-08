"""Builders, fixtures and stand-ins shared by the API backend tests."""

import copy
import queue
from types import SimpleNamespace

import pytest

from kofin.core import kodirpc
from kofin.sync import private
from kofin.sync.backends.api import native
from kofin.sync.backends.api.library import Library
from kofin.sync.backends.api.native import Native
from kofin.sync.backends.api.store import Store, namespace
from tests.unit.apikodi import SERVER, Kodi

LIB = "1" * 32
LIB2 = "2" * 32
SHOW = "a1" * 16
SHOW2 = "b2" * 16


def movie(item_id="a", **values):
    item = dict(
        Id=item_id,
        Type="Movie",
        Name="Fixture " + item_id,
        Overview="Original",
        RunTimeTicks=1200000000,
        UserData={"Played": False, "PlaybackPositionTicks": 120000000},
        MediaStreams=[
            {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080}
        ],
        People=[{"Name": "Fixture actor", "Type": "Actor", "Role": "Original"}],
    )
    item.update(values)
    return item


def series(item_id=SHOW, **values):
    item = dict(
        Id=item_id,
        Type="Series",
        Name="Show " + item_id[:2],
        Overview="About the show",
        PremiereDate="2001-02-03T00:00:00Z",
        Status="Ended",
        Genres=["Drama"],
        People=[{"Name": "Lead", "Type": "Actor", "Role": "Self"}],
        ProviderIds={"Tvdb": "77"},
        UserData={"IsFavorite": True},
    )
    item.update(values)
    return item


def season(item_id, series_id=SHOW, number=1, **values):
    item = dict(
        Id=item_id,
        Type="Season",
        Name="Season %d" % number,
        IndexNumber=number,
        SeriesId=series_id,
        Overview="",
    )
    item.update(values)
    return item


def episode(item_id, series_id=SHOW, season_number=1, number=1, **values):
    item = dict(
        Id=item_id,
        Type="Episode",
        Name="Episode " + item_id,
        SeriesId=series_id,
        SeriesName="Show",
        ParentIndexNumber=season_number,
        IndexNumber=number,
        Overview="Plot",
        RunTimeTicks=600000000,
        PremiereDate="2001-02-10T00:00:00Z",
        UserData={"Played": False, "PlaybackPositionTicks": 0},
        MediaStreams=[{"Type": "Video", "Codec": "h264"}],
        People=[{"Name": "Guest", "Type": "GuestStar"}],
    )
    item.update(values)
    return item


def musicvideo(item_id="m", **values):
    item = dict(
        Id=item_id,
        Type="MusicVideo",
        Name="Clip " + item_id,
        Artists=["Band"],
        Album="Album",
        RunTimeTicks=2000000000,
        UserData={"Played": False, "PlaybackPositionTicks": 0},
        MediaStreams=[{"Type": "Video", "Codec": "h264"}],
        People=[],
    )
    item.update(values)
    return item


def boxset(item_id, name, members, **values):
    item = dict(Id=item_id, Type="BoxSet", Name=name, KofinMembers=sorted(members))
    item.update(values)
    return item


def show_bundle(series_id=SHOW, episodes=2, seasons=(1,), prefix="e"):
    items = [series(series_id)]
    for number in seasons:
        items.append(season(series_id[:4] + "s%d" % number, series_id, number))
    for index in range(episodes):
        items.append(
            episode(
                "%s%s%d" % (prefix, series_id[:2], index + 1),
                series_id,
                seasons[0],
                index + 1,
            )
        )
    return items


@pytest.fixture
def store(tmp_path, monkeypatch):
    private.reset_overrides()
    private.set_path_override("kofin", str(tmp_path / "kofin.db"))
    monkeypatch.setattr(private, "addon_data_path", lambda: str(tmp_path))
    store = Store(namespace("server", "user"))
    store.initialize(SERVER)
    yield store
    private.reset_overrides()


@pytest.fixture
def kodi(store, monkeypatch):
    """The fake answers at kodirpc, so every module's ``rpc`` reaches it."""
    fake = Kodi(store)

    def call(method, params=None):
        try:
            return fake.rpc(method, params)
        except RuntimeError:
            return None

    def batch(requests):
        return [call(method, params) for method, params in requests]

    monkeypatch.setattr(kodirpc, "call", call)
    monkeypatch.setattr(kodirpc, "batch", batch)
    monkeypatch.setattr(native.xbmc, "getCondVisibility", lambda _: False)
    monkeypatch.setattr(
        native,
        "Monitor",
        lambda: SimpleNamespace(finished=0, waitForAbort=lambda _: False),
    )
    fake.builtins = []
    monkeypatch.setattr(native.xbmc, "executebuiltin", fake.builtins.append)
    return fake


@pytest.fixture
def backend(store, kodi):
    backend = Native(store)
    kodi.monitor = backend.monitor
    return backend


def methods(kodi, name):
    return [p for m, p in kodi.calls if m == name]


class Server:
    """A Jellyfin stand-in: libraries of items, collections, change feed."""

    server = SERVER
    user_id = "user"

    def __init__(self, libraries, boxsets=(), views=None):
        self.libraries = libraries
        self.boxsets = list(boxsets)
        self.views_list = views or [
            {"Id": lib, "CollectionType": collection, "Name": "Lib " + lib[:1]}
            for lib, collection in (
                (LIB, "movies"),
                (LIB2, "tvshows"),
            )
        ]
        self.updated = []
        self.deleted = set()

    def views(self):
        return {"Items": self.views_list}

    def all_items(self):
        for items in self.libraries.values():
            yield from items
        yield from self.boxsets

    def item(self, item_id):
        for item in self.all_items():
            if item["Id"] == item_id:
                return copy.deepcopy(item)
        raise OSError("item no longer available")

    def items(self, params):
        if params.get("Ids"):
            wanted = params["Ids"].split(",")
            rows = [copy.deepcopy(i) for i in self.all_items() if i["Id"] in wanted]
            return {"Items": rows, "TotalRecordCount": len(rows)}
        types = (params.get("IncludeItemTypes") or "").split(",")
        parent = params.get("ParentId")
        if parent in self.libraries:
            rows = [i for i in self.libraries[parent] if i["Type"] in types]
        elif parent:
            box = next(b for b in self.boxsets if b["Id"] == parent)
            rows = [{"Id": m, "Type": "Movie"} for m in box["KofinMembers"]]
        else:
            rows = [b for b in self.boxsets if "BoxSet" in types]
        if params.get("MinDateLastSaved"):
            rows = [
                r
                for r in rows
                if r.get("DateLastSaved", "") >= params["MinDateLastSaved"]
            ]
        start = params.get("StartIndex", 0)
        limit = params.get("Limit", len(rows))
        return {
            "Items": copy.deepcopy(rows[start : start + limit]),
            "TotalRecordCount": len(rows),
        }

    def ancestors(self, item_id):
        for lib, items in self.libraries.items():
            if any(i["Id"] == item_id for i in items):
                return [{"Id": lib, "Type": "CollectionFolder"}]
        return []

    def update_user_data(self, item_id, payload):
        self.updated.append((item_id, payload))


def worker(store, server, selected, monkeypatch, feed=None):
    from kofin.sync.backends.api import library

    w = Library.__new__(Library)
    w.store = store
    w.api = server
    w.player = None
    w._stop_event = SimpleNamespace(is_set=lambda: False, wait=lambda s: False)
    w._reload_owed = set()
    w._catchup_due = 0.0
    w._full_due = False
    w._repair = False
    w.changefeed = feed
    w._unsynced_tried = set()
    w._queue = queue.Queue()
    selections = (
        selected
        if isinstance(selected, list) and selected and isinstance(selected[0], list)
        else [list(selected)]
    )
    replies = iter(selections)
    last = [selections[-1]]

    def get_list(_):
        try:
            last[0] = next(replies)
        except StopIteration:
            pass
        return last[0]

    monkeypatch.setattr(library.settings, "get_list", get_list)
    monkeypatch.setattr(library.settings, "get_str", lambda _: "")
    monkeypatch.setattr(library.settings, "set_str", lambda *_: None)
    monkeypatch.setattr(library.private, "get_sync", lambda: {"Whitelist": []})
    monkeypatch.setattr(library.private, "save_sync", lambda state: None)
    if feed is None:
        monkeypatch.setattr(library.changefeed, "detect", lambda api: None)
    return w


class Feed:
    tier = "kofin"

    def __init__(self, records, userdata=(), server_time=1000, retention=None):
        self.records = records
        self.userdata = list(userdata)
        self.server_time = server_time
        self.retention = retention
        self.asked = []

    def changes(self, last_sync, include, libraries=None):
        from kofin.sync.changefeed import ChangeSet, Envelope

        self.asked.append((last_sync, tuple(include), tuple(libraries or ())))
        return ChangeSet(
            records=list(self.records),
            userdata=self.userdata,
            envelope=Envelope(
                server_time=self.server_time, retention_cutoff=self.retention
            ),
        )

    def server_now(self):
        return self.server_time
