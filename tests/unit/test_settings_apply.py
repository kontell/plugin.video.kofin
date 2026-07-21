"""L1: library picker round-trip and the settings diff engine's add/remove
dispatch against a fake library manager (plan §5 step 5 DoD)."""

import pytest

from kofin.plugin import librarypicker
from kofin.plugin.router import Request
from kofin.service.settings_apply import SettingsApplier
from kofin.sync import db as sync_db
from kofin.sync import kofindb
from tests.unit.fakes import FakeAddon, FakeWindow


class FakeDialog:
    multiselect_result = None
    yesno_result = True
    calls = []

    def multiselect(self, heading, options, preselect=None):
        FakeDialog.calls.append(("multiselect", heading, list(options), preselect))
        return FakeDialog.multiselect_result

    def yesno(self, heading, message):
        FakeDialog.calls.append(("yesno", heading, message))
        return FakeDialog.yesno_result

    def notification(self, *args, **kwargs):
        FakeDialog.calls.append(("notification", args))


class FakeLibraryManager:
    def __init__(self):
        self.commands = []

    def enqueue_command(self, command, data=None):
        self.commands.append((command, data))


class FakeService:
    def __init__(self):
        self._online = True
        self._restart_requested = False
        self.library = FakeLibraryManager()
        self.reregistered = 0

    def _start_library(self):
        pass

    def _on_ws_connected(self):
        self.reregistered += 1


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    FakeAddon.store = {}
    FakeWindow.store = {}
    FakeDialog.calls = []
    FakeDialog.multiselect_result = None
    FakeDialog.yesno_result = True
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcgui.Dialog", FakeDialog)
    monkeypatch.setattr("xbmcvfs.exists", lambda p: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda p: str(tmp_path))
    # Real strings carry a %s placeholder; the FakeAddon fallback does not.
    monkeypatch.setattr(
        "kofin.core.settings.localized", lambda sid: "string-%d %%s" % sid
    )

    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    yield
    sync_db.reset_overrides()


def seed_views(*views):
    with sync_db.Database("kofin") as opened:
        mapping = kofindb.JellyfinDatabase(opened.cursor)
        for view_id, name, media in views:
            mapping.add_view(view_id, name, media)


def seed_whitelist(*entries):
    sync = sync_db.get_sync()
    sync["Whitelist"] = list(entries)
    sync_db.save_sync(sync)


# --- picker ------------------------------------------------------------------


VIEWS = [
    {"Id": "v-movies", "Name": "Movies", "CollectionType": "movies"},
    {"Id": "v-live", "Name": "Live TV", "CollectionType": "livetv"},
    {"Id": "v-shows", "Name": "Shows", "CollectionType": "tvshows"},
    {"Id": "v-music", "Name": "Tunes", "CollectionType": "music"},
    {"Id": "v-photos", "Name": "Photos", "CollectionType": "homevideos"},
    {"Id": "v-mixed", "Name": "Mixed", "CollectionType": None},
]


def test_syncable_views_filters_types():
    result = librarypicker.syncable_views(VIEWS)
    assert [v["Id"] for v in result] == ["v-movies", "v-shows", "v-music", "v-mixed"]
    assert result[-1]["Media"] == "mixed"  # None CollectionType -> mixed


def picker_env(monkeypatch):
    FakeAddon.store.update(
        {"isLoggedIn": "true", "accessToken": "t", "serverAddress": "http://s"}
    )

    class FakeApi:
        def views(self):
            return {"Items": VIEWS}

    monkeypatch.setattr(
        "kofin.plugin.librarypicker.Api",
        type(
            "A", (), {"from_credentials": staticmethod(lambda http, creds: FakeApi())}
        ),
    )


def test_picker_round_trips_selection(monkeypatch):
    picker_env(monkeypatch)
    FakeAddon.store["librarySelection"] = "v-shows"
    FakeDialog.multiselect_result = [0, 2]  # Movies + Tunes

    librarypicker.select_libraries(Request("plugin://x", -1, {}))

    assert FakeAddon.store["librarySelection"] == "v-movies,v-music"
    # Pre-check reflected the stored selection.
    call = FakeDialog.calls[0]
    assert call[0] == "multiselect"
    assert call[3] == [1]  # index of v-shows among syncable candidates


def test_picker_cancel_changes_nothing(monkeypatch):
    picker_env(monkeypatch)
    FakeAddon.store["librarySelection"] = "v-shows"
    FakeDialog.multiselect_result = None

    librarypicker.select_libraries(Request("plugin://x", -1, {}))

    assert FakeAddon.store["librarySelection"] == "v-shows"


# --- diff engine -------------------------------------------------------------


def ready_applier(service):
    """An applier past the startup guard, baselined at construction time."""
    applier = SettingsApplier(service)
    applier.mark_ready()
    return applier


def test_additions_dispatch_sync(monkeypatch):
    seed_views(("v-movies", "Movies", "movies"))
    seed_whitelist()
    service = FakeService()
    applier = ready_applier(service)

    FakeAddon.store["librarySelection"] = "v-movies"
    applier.apply()

    assert service.library.commands == [("SyncLibrary", {"Id": "v-movies"})]


def test_removals_confirm_then_dispatch(monkeypatch):
    seed_views(("v-movies", "Movies", "movies"), ("v-shows", "Shows", "tvshows"))
    seed_whitelist("v-movies", "v-shows")
    FakeAddon.store["librarySelection"] = "v-movies,v-shows"
    service = FakeService()
    applier = ready_applier(service)

    FakeDialog.yesno_result = True
    FakeAddon.store["librarySelection"] = "v-movies"
    applier.apply()

    assert ("RemoveLibrary", {"Id": "v-shows"}) in service.library.commands
    assert not any(c[0] == "SyncLibrary" for c in service.library.commands)
    # The confirm listed the library by name.
    yesno = [c for c in FakeDialog.calls if c[0] == "yesno"][0]
    assert "Shows" in yesno[2]


def test_removal_declined_restores_selection(monkeypatch):
    seed_views(("v-movies", "Movies", "movies"))
    seed_whitelist("v-movies")
    FakeAddon.store["librarySelection"] = "v-movies"
    service = FakeService()
    applier = ready_applier(service)

    FakeDialog.yesno_result = False
    FakeAddon.store["librarySelection"] = ""
    applier.apply()

    assert service.library.commands == []
    assert FakeAddon.store["librarySelection"] == "v-movies"
    # Snapshot follows the restore: a further no-op apply dispatches nothing.
    applier.apply()
    assert service.library.commands == []


# --- startup guard (S2 regression: restart must not prompt a removal) --------


def test_changes_ignored_before_ready():
    """Kodi fires onSettingsChanged during its startup settings-load; the
    applier must not act until the service marks it ready."""
    seed_views(("v-movies", "Movies", "movies"))
    seed_whitelist()
    service = FakeService()
    applier = SettingsApplier(service)  # not ready

    FakeAddon.store["librarySelection"] = "v-movies"
    applier.apply()

    assert service.library.commands == []


def test_startup_transient_empty_does_not_prompt_removal():
    """The exact live bug: a synced library, its id persisted in
    librarySelection, and a transient empty read at startup must NOT be taken
    as 'user removed everything'. Reproduces a plain Kodi restart."""
    seed_views(("v-docs", "Documentaries", "tvshows"))
    seed_whitelist("v-docs")
    FakeAddon.store["librarySelection"] = "v-docs"  # persisted, correct
    service = FakeService()
    applier = SettingsApplier(service)  # snapshot reads "v-docs"

    # Startup storm: onSettingsChanged fires while a transient read returns "".
    FakeAddon.store["librarySelection"] = ""
    applier.apply()
    FakeAddon.store["librarySelection"] = "v-docs"  # settled real value
    applier.apply()

    # Nothing prompted, nothing removed.
    assert service.library.commands == []
    assert not any(c[0] == "yesno" for c in FakeDialog.calls)

    # Settings settle; the applier re-baselines to the real value.
    applier.mark_ready()
    applier.apply()
    assert service.library.commands == []
    assert not any(c[0] == "yesno" for c in FakeDialog.calls)


class FailOnceAddon(FakeAddon):
    """One Addon instantiation whose settings load failed: it hands back ""
    for the guarded setting. Every later instance reads correctly — the shape
    of the live failure, where get_str builds a fresh Addon per call."""

    failed_reads = 0
    fail_setting = "librarySelection"

    def getSetting(self, setting_id: str) -> str:
        if setting_id == FailOnceAddon.fail_setting and FailOnceAddon.failed_reads:
            FailOnceAddon.failed_reads -= 1
            return ""
        return self.store.get(setting_id, "")


def test_post_ready_empty_read_is_corroborated_not_obeyed(monkeypatch):
    """The phase-5 live bug: settings.xml failed to load four minutes into a
    ready session, librarySelection read "", and every synced library was
    proposed for removal. A single failed read must never reach the handler."""
    seed_views(("v-docs", "Documentaries", "tvshows"))
    seed_whitelist("v-docs")
    FakeAddon.store["librarySelection"] = "v-docs"
    service = FakeService()
    applier = ready_applier(service)

    monkeypatch.setattr("xbmcaddon.Addon", FailOnceAddon)
    FailOnceAddon.failed_reads = 1
    applier.apply()

    # Not prompted, not removed, and the snapshot still holds the real value
    # so the library is not "already emptied" from the applier's point of view.
    assert service.library.commands == []
    assert not any(c[0] == "yesno" for c in FakeDialog.calls)
    assert applier.snapshot["librarySelection"] == "v-docs"

    # The failed read left nothing latched: a later genuine edit still applies.
    FailOnceAddon.failed_reads = 0
    seed_views(("v-docs", "Documentaries", "tvshows"), ("v-new", "New", "movies"))
    FakeAddon.store["librarySelection"] = "v-docs,v-new"
    applier.apply()
    assert service.library.commands == [("SyncLibrary", {"Id": "v-new"})]


def test_deliberate_deselect_all_still_removes(monkeypatch):
    """The guard must not swallow intent: an empty selection that survives the
    corroborating re-read is a real deselect-all and proceeds to the prompt."""
    seed_views(("v-docs", "Documentaries", "tvshows"))
    seed_whitelist("v-docs")
    FakeAddon.store["librarySelection"] = "v-docs"
    service = FakeService()
    applier = ready_applier(service)

    FakeDialog.yesno_result = True
    FakeAddon.store["librarySelection"] = ""  # reads empty every time
    applier.apply()

    assert service.library.commands == [("RemoveLibrary", {"Id": "v-docs"})]
    assert any(c[0] == "yesno" for c in FakeDialog.calls)


def test_mark_ready_rebaselines_and_is_idempotent():
    seed_views(("v-movies", "Movies", "movies"))
    seed_whitelist()
    service = FakeService()
    applier = SettingsApplier(service)

    # A selection that arrived during startup is absorbed into the baseline,
    # not dispatched (it predates readiness).
    FakeAddon.store["librarySelection"] = "v-movies"
    applier.mark_ready()
    applier.apply()
    assert service.library.commands == []

    # A real edit after ready is dispatched. v-movies is already synced, so
    # adding v-shows dispatches only the newcomer (additions are computed
    # against the whitelist, not the snapshot).
    seed_views(("v-movies", "Movies", "movies"), ("v-shows", "Shows", "tvshows"))
    seed_whitelist("v-movies")
    FakeAddon.store["librarySelection"] = "v-movies,v-shows"
    applier.apply()
    assert service.library.commands == [("SyncLibrary", {"Id": "v-shows"})]

    # mark_ready twice does not re-baseline away a pending change.
    before = dict(applier.snapshot)
    applier.mark_ready()
    assert applier.snapshot == before


def test_mixed_whitelist_entries_survive_selection(monkeypatch):
    seed_views(("v-mixed", "Mixed", "mixed"))
    seed_whitelist("Mixed:v-mixed")
    FakeAddon.store["librarySelection"] = "v-mixed"
    service = FakeService()
    applier = ready_applier(service)

    # Selection matches the synced set -> nothing to do.
    FakeAddon.store["librarySelection"] = "v-mixed,"
    applier.apply()
    assert service.library.commands == []

    # Dropping it removes by the raw whitelist entry (Mixed: prefix kept).
    FakeDialog.yesno_result = True
    FakeAddon.store["librarySelection"] = ""
    applier.apply()
    assert service.library.commands == [("RemoveLibrary", {"Id": "Mixed:v-mixed"})]


def test_ssl_handler(monkeypatch):
    # Device name is no longer a setting (kofin uses System.FriendlyName), so
    # only sslVerify remains among the scalar handlers.
    service = FakeService()
    FakeAddon.store["sslVerify"] = "true"
    applier = ready_applier(service)

    FakeAddon.store["sslVerify"] = "false"
    applier.apply()
    assert service._restart_requested is True
