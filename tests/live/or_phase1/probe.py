"""Isolated Kodi interpreter probe, invoked with RunScript(path,sql|api,label).

Place source packages under <probe>/packages/<label>, fixtures under fixtures/,
and the two fixture modules and fakes.py beside this file. Reports and all
SQLite files are confined to this owned test profile. Never installs/replaces
Kofin or changes the production library, settings or window properties.
"""

import importlib.abc
import json
from pathlib import Path
import sys
import traceback

import xbmc
import xbmcaddon
import xbmcgui

ROOT = Path(__file__).resolve().parent
MODE, LABEL = sys.argv[1:3]
SOURCE = ROOT / "packages" / LABEL
WORK = ROOT / (LABEL + "-work")
sys.dont_write_bytecode = True
sys.path.insert(0, str(SOURCE / "lib"))
for addon in ("script.module.requests", "script.module.websocket"):
    try:
        sys.path.append(str(Path(xbmcaddon.Addon(addon).getAddonInfo("path")) / "lib"))
    except RuntimeError:
        pass


def require(condition, message="phase1 invariant failed"):
    # Kodi can compile scripts with assertions disabled. Checks and their
    # side effects must execute under Python optimization as well.
    if not condition:
        raise RuntimeError(message)


def main():
    from fakes import FakeAddon, FakeWindow
    from kofin.core import settings

    # Save system addon queries for the actual capability check. Only Kofin's
    # configuration and window state are replaced, within this interpreter.
    xbmcgui.Window = FakeWindow
    require(
        (Path(settings.__file__).resolve().is_relative_to(SOURCE.resolve())),
        (settings.__file__),
    )
    settings.get_addon = lambda: FakeAddon()
    settings.addon_path = lambda: str(SOURCE)
    settings.addon_data_path = lambda: str(WORK)
    FakeAddon.store = {"downloadsEnabled": "true", "chapterImages": "true"}
    FakeWindow.store = {}
    emitted = []
    xbmc.executebuiltin = lambda command, *a, **k: emitted.append(command)
    original_rpc = xbmc.executeJSONRPC

    def read_only_rpc(request):
        payload = json.loads(request)
        require(
            (
                payload["method"].startswith(
                    (
                        "JSONRPC.",
                        "Application.Get",
                        "VideoLibrary.Get",
                        "AudioLibrary.Get",
                        "Player.Get",
                        "Settings.Get",
                        "Addons.Get",
                    )
                )
            ),
            (payload["method"]),
        )
        return original_rpc(request)

    xbmc.executeJSONRPC = read_only_rpc
    if MODE == "sql":
        from sql_pipeline import main as sql_main

        result = sql_main(SOURCE, WORK, ROOT / "fixtures")
        result["ui_events_recorded"] = len(emitted)
        return result

    require((MODE == "api"), ("phase1 invariant failed"))
    WORK.mkdir(exist_ok=False)
    denied = (
        "kofin.core.addonxml",
        "kofin.sync.db",
        "kofin.sync.schema",
        "kofin.sync.kodidb",
        "kofin.sync.writers",
        "kofin.sync.backends.sql",
        "kofin.sync.clean",
        "kofin.service.artcache",
        "kofin.service.chapters",
        "kofin.downloads.manager",
        "kofin.downloads.repoint",
    )

    class NativeImports(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            require(
                (
                    not any(
                        fullname == p or fullname.startswith(p + ".") for p in denied
                    )
                ),
                (fullname),
            )

    sys.meta_path.insert(0, NativeImports())

    def audit(event, args):
        if event == "sqlite3.connect":
            require(
                (Path(args[0]).resolve() == WORK / "kofin.db"),
                ("non-private SQLite access"),
            )
        if event == "open" and isinstance(args[0], (str, bytes)):
            require(
                (
                    not str(args[0])
                    .rsplit("/", 1)[-1]
                    .startswith(("MyVideos", "MyMusic", "Textures"))
                ),
                ("native database access"),
            )

    sys.addaudithook(audit)
    from kofin.sync import private

    private.ADDON_DATA = str(WORK) + "/"
    private.set_path_override("kofin", str(WORK / "kofin.db"))
    from kofin.service.main import Service
    from kofin.sync.backends.api.capabilities import inspect

    from kofin import buildconfig

    require((buildconfig.BACKEND == "api"), (buildconfig.BACKEND))
    service = Service()
    prepared = []
    original_prepare = service._prepare_api_backend

    def prepare():
        prepared.append("called")
        try:
            original_prepare()
        except Exception:
            prepared.append(traceback.format_exc())
            raise

    service._prepare_api_backend = prepare
    service.settings_apply.mark_ready()
    service.abortRequested = lambda: True
    service._start_library()
    service._start_downloads()
    service._start_chapter_sweep()
    service.player._start_chapter_thumbs({"Type": "Movie"})
    require(
        (
            service.library is None
            and service.downloads is None
            and service.artcache is None
        ),
        ("phase1 invariant failed"),
    )
    require((service.run() is False), ("phase1 invariant failed"))
    require(((WORK / "api-capabilities.json").exists()), (prepared))
    report = json.loads((WORK / "api-capabilities.json").read_text())
    require(
        (report["private_state"] == "ready" and not report["native_sync_enabled"]),
        ("phase1 invariant failed"),
    )
    # The existing dirty P1D build is explicitly approved for development.
    approved = inspect(allow_dirty=True)
    require((approved["interfaces_passed"]), (approved))
    return {
        "service_started": True,
        "private_state": "ready",
        "native_access": 0,
        "capabilities": approved,
    }


try:
    result = {"mode": MODE, "label": LABEL, "complete": True, "result": main()}
except Exception:
    result = {"mode": MODE, "label": LABEL, "error": traceback.format_exc()}
(ROOT / (LABEL + ".json")).write_text(json.dumps(result, indent=2) + "\n")
