"""Execute an extracted API distribution with native imports/files denied."""

import importlib
import importlib.abc
import json
import os
from pathlib import Path
import pkgutil
import sys

package, profile, repository = map(Path, sys.argv[1:])
sys.dont_write_bytecode = True
sys.path.insert(0, str(package / "lib"))
sys.path.append(str(repository))

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
        if any(fullname == p or fullname.startswith(p + ".") for p in denied):
            raise AssertionError("native import: " + fullname)


sys.meta_path.insert(0, NativeImports())


def audit(event, args):
    if event == "sqlite3.connect":
        assert Path(args[0]).resolve() == profile / "kofin.db", args[0]
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0]))
        assert not path.name.startswith(("MyVideos", "MyMusic", "Textures")), str(path)
        if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            assert path.resolve().is_relative_to(
                profile
            ), "write outside profile: " + str(path)


sys.addaudithook(audit)

from tests.unit.fakes import FakeAddon, FakeWindow
import xbmcaddon
import xbmcgui
from kofin.core import settings
from kofin.sync import private

xbmcaddon.Addon = FakeAddon
xbmcgui.Window = FakeWindow
settings.addon_data_path = lambda: str(profile)
settings.addon_path = lambda: str(package)
private.addon_data_path = lambda: str(profile)
private.set_path_override("kofin", str(profile / "kofin.db"))
FakeAddon.store = {
    "downloadsEnabled": "true",
    "chapterImages": "true",
    "precacheActorArt": "true",
    "librarySelection": "old-sql-library",
}
FakeWindow.store = {}

# Import every shipped module, including modules reached outside native sync.
import kofin

loaded = []
for module in pkgutil.walk_packages(kofin.__path__, "kofin."):
    importlib.import_module(module.name)
    loaded.append(module.name)

from kofin.buildconfig import BACKEND
from kofin.service.main import Service
from kofin.plugin import router
from kofin.sync.catalogue import Catalogue
from kofin.sync.model import MediaItem

assert BACKEND == "api"
store = Catalogue("fixture-server/fixture-user")
assert store.stage(MediaItem.from_dto({"Id": "movie", "Type": "Movie"})) == 1
assert store.state("movie").applied == 0
service = Service()
service.settings_apply.mark_ready()
service._start_library()
service._start_downloads()
service._start_chapter_sweep()
service.player._start_chapter_thumbs({"Type": "Movie"})
assert (
    service.library is None and service.downloads is None and service.artcache is None
)
service.abortRequested = lambda: True
assert service.run() is False
report = json.loads((profile / "api-capabilities.json").read_text())
assert report["backend"] == "api" and not report["native_sync_enabled"]
assert report["private_state"] == "ready"
for route in router.ROUTES:
    assert router._resolve(route) is not None
    if route in router.LEGACY_MODES:
        assert router._resolve(route) is router._unavailable
print(
    json.dumps(
        {
            "imported_modules": len(loaded),
            "service_started": True,
            "private_state": "ready",
            "native_access": 0,
        }
    )
)

# A damaged private store or unavailable capability probe cannot stop the
# service's otherwise usable dynamic connection/playback lifecycle.
from unittest.mock import patch

service = Service()
service.abortRequested = lambda: True
with patch.object(service, "_prepare_api_backend", side_effect=OSError("fixture")):
    assert service.run() is False

# Old favourites or settings URLs must release a directory handle even when
# the feature behind them is unavailable in this distribution.
with patch("xbmcplugin.endOfDirectory") as ended, patch("kofin.core.toast.show"):
    for route in router.LEGACY_MODES:
        router.dispatch(["plugin://plugin.video.kofin/", "71", "?mode=" + route])
        ended.assert_called_with(71, succeeded=False)
        ended.reset_mock()
