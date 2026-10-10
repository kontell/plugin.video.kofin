"""Cast images warmed through Kodi's image VFS (the API build)."""

from urllib.parse import quote

from kofin.core import kodirpc
from kofin.service import castart
from tests.unit.apifixtures import LIB, movie, store  # noqa: F401

SERVER = "http://server"


def test_portraits_are_the_rows_cast_urls_each_once(store):
    people = [
        {"Name": "A", "Type": "Actor", "Id": "p1", "PrimaryImageTag": "t1"},
        {"Name": "B", "Type": "Director", "Id": "p2", "PrimaryImageTag": "t2"},
        {"Name": "C", "Type": "Actor", "Id": "p3"},
    ]
    store.publish([movie(People=people), movie("b", People=people[:1])], library=LIB)
    urls = castart.portraits(store, SERVER)
    assert [u.split("?")[0] for u in urls] == [
        SERVER + "/Items/p1/Images/Primary",
        SERVER + "/Items/p2/Images/Primary",
    ]


def test_a_batch_warms_what_kodi_does_not_hold_through_the_image_vfs(
    store, monkeypatch
):
    people = [
        {"Name": "A", "Type": "Actor", "Id": "p1", "PrimaryImageTag": "t1"},
        {"Name": "B", "Type": "Actor", "Id": "p2", "PrimaryImageTag": "t2"},
        {"Name": "D", "Type": "Actor", "Id": "p4", "PrimaryImageTag": "t4"},
    ]
    store.publish([movie(People=people)], library=LIB)
    held = castart.portraits(store, SERVER)[0]
    calls = []

    def call(method, params=None):
        calls.append((method, params))
        assert method == "Textures.GetTextures"
        assert params["filter"]["value"] == SERVER
        return {"textures": [{"url": held}]}

    monkeypatch.setattr(kodirpc, "call", call)
    opened = []

    class File:
        def __init__(self, path, mode="r"):
            opened.append(path)
            self.path = path

        def size(self):
            return 0 if "p4" in self.path else 1234

        def close(self):
            pass

    monkeypatch.setattr(castart.xbmcvfs, "File", File)
    monkeypatch.setattr(castart.settings, "get_str", lambda *a: SERVER, raising=False)
    warmer = castart.CastArt(store_factory=lambda: store)
    store.initialize(SERVER)
    assert warmer.seed_batch() == 1
    wanted = [u for u in castart.portraits(store, SERVER) if u != held]
    assert opened == ["image://%s/" % quote(u, safe="") for u in wanted]
    # The one Kodi could not cache is not asked again this generation; the
    # held one never was.
    opened.clear()
    assert warmer.seed_batch() == 0 and opened == []
    assert len(calls) == 2


def test_seed_all_runs_to_the_end_and_stops_for_playback(store, monkeypatch):
    people = [
        {"Name": str(i), "Type": "Actor", "Id": "p%d" % i, "PrimaryImageTag": "t"}
        for i in range(60)
    ]
    store.publish([movie(People=people)], library=LIB)
    store.initialize(SERVER)
    monkeypatch.setattr(kodirpc, "call", lambda *a, **k: {"textures": []})
    opened = []

    class File:
        def __init__(self, path, mode="r"):
            opened.append(path)

        def size(self):
            return 1

        def close(self):
            pass

    monkeypatch.setattr(castart.xbmcvfs, "File", File)
    warmer = castart.CastArt(store_factory=lambda: store)
    # The stub player answers "playing" to everything; nothing plays here.
    monkeypatch.setattr(castart.CastArt, "_playing", staticmethod(lambda: False))
    assert warmer.seed_all() == 60 and len(opened) == 60
    opened.clear()
    store.publish(
        [
            movie(
                People=people
                + [{"Name": "x", "Type": "Actor", "Id": "px", "PrimaryImageTag": "t"}]
            )
        ],
        library=LIB,
    )
    # Only the new portrait is outstanding; playback stops the run first.
    monkeypatch.setattr(castart.CastArt, "_playing", staticmethod(lambda: True))
    assert warmer.seed_all() == 0 and opened == []
    monkeypatch.setattr(castart.CastArt, "_playing", staticmethod(lambda: False))
    assert warmer.seed_all() == 1 and len(opened) == 1 and "px" in opened[0]
