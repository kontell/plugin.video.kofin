"""Exercise the packaged API code on real Piers using a renamed fixture addon.

Only addon identity and service auto-start differ from the package. The production
addon, its settings, and all native content outside our namespace are preserved.
"""

import json
from pathlib import Path
import sys
import time
import traceback
from types import SimpleNamespace

import xbmc
import xbmcaddon
import xbmcvfs

ADDON = "plugin.video.kofin.phase3"
addon = xbmcaddon.Addon(ADDON)
sys.path.insert(0, str(Path(addon.getAddonInfo("path")) / "lib"))
profile = Path(xbmcvfs.translatePath(addon.getAddonInfo("profile")))
profile.mkdir(parents=True, exist_ok=True)

from kofin.sync.backends.api.store import MovieStore, namespace
from kofin.sync.backends.api.movies import Movies, rpc
from kofin.sync.backends.api.library import fetch_movies
from kofin.sync.catalogue import BackendMismatch


# A production behavior importing a native database is a test failure.
def audit(event, args):
    if event == "sqlite3.connect":
        assert Path(args[0]).resolve() == profile / "kofin.db", "native database access"


sys.addaudithook(audit)

store = MovieStore(namespace("phase3-server", "phase3-user"))
store.initialize("http://127.0.0.1:9")
backend = Movies(store)


def movie(i=0):
    return dict(
        Id=("%032x" % i),
        Type="Movie",
        Name="Kofin phase3 movie %d" % i,
        Overview="Phase3 original plot",
        SortName="Phase3 %03d" % i,
        OriginalTitle="Phase3 original",
        ProductionYear=2020,
        PremiereDate="2020-02-03",
        DateCreated="2020-02-03T04:05:06Z",
        OfficialRating="PG",
        RunTimeTicks=1200000000,
        Genres=["Fixture"],
        Studios=[{"Name": "Phase3 studio"}],
        ProductionLocations=["United Kingdom"],
        Tags=["phase3-test"],
        Taglines=["Phase3 tagline"],
        ProviderIds={"Imdb": "phase3-%d" % i},
        CommunityRating=7.5,
        CriticRating=80,
        VoteCount=20,
        People=[{"Name": "Phase3 actor", "Type": "Actor", "Role": "Original role"}],
        MediaStreams=[
            {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080}
        ],
        UserData={"Played": False, "PlaybackPositionTicks": 120000000},
    )


def foreign():
    data = rpc("VideoLibrary.GetMovies", {"properties": ["file", "uniqueid"]}).get(
        "movies", []
    )
    return sorted(
        row["movieid"]
        for row in data
        if not row.get("file", "").startswith("plugin://" + ADDON + "/")
    )


def save(result):
    (profile / "result.json").write_text(json.dumps(result, indent=2) + "\n")


result = {"complete": False, "checks": []}
save(result)
before = foreign()
try:
    result["application"] = rpc(
        "Application.GetProperties", {"properties": ["version"]}
    )
    if not store.prepared():
        try:
            backend.setup()
        except BackendMismatch:
            result["checks"].append("first-run gate refused existing native library")
        else:
            raise AssertionError("expected nonempty-profile refusal")
        # Explicit fixture-only preparation. No existing native row can match
        # this independently installed addon's paths and generated namespace.
        store.prepare_empty()
    store.publish([movie(0), movie(1)], library="fixture")
    start = time.monotonic()
    backend.reconcile()
    result["initial_seconds"] = time.monotonic() - start
    first = backend.read()
    assert len(first) == 2
    assert all(store.state(i).applied == store.state(i).desired for i in first)
    result["checks"].append(
        "snapshot scanner imported two movies with owned identities"
    )
    ids = {i: r["movieid"] for i, r in first.items()}
    backend.reconcile()
    assert ids == {i: r["movieid"] for i, r in backend.read().items()}
    result["checks"].append("repeat did not duplicate or change native identities")
    changed = movie(0)
    changed["Overview"] = "Phase3 changed plot"
    changed["UserData"] = {
        "Played": True,
        "PlayCount": 4,
        "PlaybackPositionTicks": 300000000,
    }
    store.publish([changed])
    backend.reconcile()
    row = backend.read()[changed["Id"]]
    assert row["movieid"] == ids[changed["Id"]] and row["playcount"] == 4
    assert abs(row["resume"]["position"] - 30) < 1
    result["checks"].append("scalar patch and watched/resume readback")
    changed["People"] = [
        {"Name": "Phase3 replacement actor", "Type": "Actor", "Role": "New role"}
    ]
    changed["MediaStreams"][0]["Width"] = 1280
    store.publish([changed])
    # Construct a fresh adapter over persisted desired work, as after restart.
    backend = Movies(MovieStore(store.namespace))
    backend.reconcile()
    refreshed = backend.read()[changed["Id"]]
    assert refreshed["movieid"] != ids[changed["Id"]]
    assert refreshed["playcount"] == 4 and abs(refreshed["resume"]["position"] - 30) < 1
    detail = rpc(
        "VideoLibrary.GetMovieDetails",
        {"movieid": refreshed["movieid"], "properties": ["cast", "streamdetails"]},
    )["moviedetails"]
    assert detail["cast"][0]["name"] == "Phase3 replacement actor"
    result["checks"].append(
        "refresh replaced ID, updated cast, restored userdata after restart"
    )
    changed.update(ProviderIds={}, CriticRating=None, Genres=[], Tags=[])
    store.publish([changed])
    backend.reconcile()
    cleared = backend.read()[changed["Id"]]
    assert "imdb" not in cleared["uniqueid"] and "critic" not in cleared["ratings"]
    assert cleared["genre"] == []
    result["checks"].append(
        "removed provider IDs, critic rating, tags and genres cleared"
    )
    from kofin.service.kodiuserdata import KodiUserData
    from kofin.sync.backends.api import movies as movie_module

    bridge = KodiUserData(SimpleNamespace())
    old_store = movie_module.current_store
    movie_module.current_store = lambda: store

    class Feedback(xbmc.Monitor):
        def onNotification(self, sender, method, data):
            if sender == "xbmc" and method == "VideoLibrary.OnUpdate":
                bridge.submit(json.loads(data))

    feedback = Feedback()
    try:
        # A server-sourced count is suppressed by value, while a different
        # local edit arriving during its expectation interval is accepted.
        store.expect(
            changed["Id"], store.state(changed["Id"]).desired, {"playcount": 4}
        )
        bridge.submit(
            {"item": {"id": cleared["movieid"], "type": "movie"}, "playcount": 4}
        )
        feedback.waitForAbort(0.3)
        assert not store.local_pending(), "server userdata echoed"
        rpc(
            "VideoLibrary.SetMovieDetails",
            {
                "movieid": cleared["movieid"],
                "playcount": 0,
                "resume": {"position": 0, "total": 120},
            },
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if store.local_pending():
                break
            feedback.waitForAbort(0.1)
        assert store.local_pending()[0][1]["playcount"] == 0
        result["checks"].append(
            "server echo suppressed and real native watched edit persisted"
        )
        for i, values in store.local_pending():
            store.local_done(i, values)
        changed["UserData"] = {
            "Played": False,
            "PlayCount": 0,
            "PlaybackPositionTicks": 0,
        }
        store.publish([changed])
        backend.reconcile()
    finally:
        bridge.stop()
        del feedback
        movie_module.current_store = old_store
    snapshot = store.snapshot()

    class Offline:
        user_id = "fixture"

        def items(self, params):
            if params["StartIndex"]:
                raise OSError("offline fixture")
            return {"Items": [movie(2)], "TotalRecordCount": 2}

    try:
        store.publish(fetch_movies(Offline(), "fixture"), library="fixture")
    except OSError:
        pass
    else:
        raise AssertionError("partial enumeration accepted")
    assert store.snapshot() == snapshot and len(backend.read()) == 2
    result["checks"].append(
        "offline second page retained complete snapshot and native movies"
    )
    store.publish([], removed=[movie(1)["Id"]])
    backend.reconcile()
    assert len(backend.read()) == 1
    result["checks"].append("owned removal")
    import playback

    result["playback"] = playback.exercise(
        store, backend, changed, addon.getAddonInfo("path")
    )
    result["checks"].append("native resolver playback, stop and completion reporting")
    result["complete"] = True
except Exception:
    result["error"] = traceback.format_exc()
    result["owned_art"] = {i: r.get("art") for i, r in backend.read().items()}
finally:
    try:
        store.publish([], library="fixture")
        backend.reconcile()
        result["cleanup"] = not backend.read()
    except Exception:
        result["cleanup_error"] = traceback.format_exc()
    result["foreign_unchanged"] = before == foreign()
    save(result)
