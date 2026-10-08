"""Stage native work, restart Kodi externally, and verify durable replay."""

import json
import os
from pathlib import Path
import sys
import traceback

import xbmcaddon
import xbmcvfs

addon = xbmcaddon.Addon("plugin.video.kofin.phase3")
sys.path.insert(0, str(Path(addon.getAddonInfo("path")) / "lib"))
profile = Path(xbmcvfs.translatePath(addon.getAddonInfo("profile")))

from kofin.sync.backends.api.store import MovieStore, namespace
from kofin.sync.backends.api.movies import Movies, rpc


def audit(event, args):
    if event == "sqlite3.connect":
        assert Path(args[0]).resolve() == profile / "kofin.db"


sys.addaudithook(audit)
store = MovieStore(namespace("phase3-server", "phase3-user"))
backend = Movies(store)
statefile = profile / "restart-state.json"
resultfile = profile / "restart-result.json"
result = {"complete": False}


def foreign():
    return sorted(
        row["movieid"]
        for row in rpc("VideoLibrary.GetMovies", {"properties": ["file"]}).get(
            "movies", []
        )
        if not row["file"].startswith("plugin://plugin.video.kofin.phase3/")
    )


try:
    if sys.argv[1] == "prepare":
        assert not backend.read(), "run after lifecycle cleanup"
        item = {
            "Id": "f" * 32,
            "Type": "Movie",
            "Name": "Phase3 restart fixture",
            "Overview": "Before restart",
        }
        before = foreign()
        store.publish([item], library="fixture")
        backend.reconcile()
        original = backend.read()[item["Id"]]["movieid"]
        store.publish([dict(item, Overview="After restart")])
        # Model interruption after a scanner generation was pinned.
        store.pin()
        statefile.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "native_id": original,
                    "foreign": before,
                    "item_id": item["Id"],
                }
            )
        )
        result.update(prepared=True)
    else:
        state = json.loads(statefile.read_text())
        assert os.getpid() != state["pid"], "Kodi did not restart"
        try:
            assert store.pending()
            backend.reconcile()
            row = backend.read()[state["item_id"]]
            assert (
                row["movieid"] == state["native_id"] and row["plot"] == "After restart"
            )
            assert not store.pending()
            backend.reconcile(repair=True)
            assert len(backend.read()) == 1
            result.update(
                complete=True,
                process_restarted=True,
                pending_replayed=True,
                stable_native_id=True,
                repeat_without_duplicates=True,
            )
        finally:
            store.publish([], library="fixture")
            backend.reconcile()
            result["cleanup"] = not backend.read()
            result["foreign_unchanged"] = state["foreign"] == foreign()
except Exception:
    result["error"] = traceback.format_exc()
finally:
    resultfile.write_text(json.dumps(result, indent=2) + "\n")
