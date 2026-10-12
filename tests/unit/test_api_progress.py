"""The library-update bar of the API build: gated, rate-limited, never per item."""

import pytest

from kofin.sync.backends.api import progress as progress_module
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    backend,
    kodi,
    methods,
    movie,
    store,
)


class Bar:
    instances = []

    def __init__(self):
        self.events = []
        Bar.instances.append(self)

    def create(self, heading, message=""):
        self.events.append(("create", heading, message))

    def update(self, percent=0, heading=None, message=None):
        self.events.append(("update", percent, message))

    def close(self):
        self.events.append(("close",))


SETTINGS = {}


@pytest.fixture
def bar(monkeypatch):
    Bar.instances = []
    SETTINGS.clear()
    SETTINGS.update({"showLibraryUpdateProgress": True, "syncProgressThreshold": 0})
    monkeypatch.setattr(progress_module.xbmcgui, "DialogProgressBG", Bar)
    monkeypatch.setattr(
        progress_module.settings, "get_bool", lambda k: SETTINGS.get(k, False)
    )
    monkeypatch.setattr(
        progress_module.settings, "get_int", lambda k: SETTINGS.get(k, 0)
    )
    monkeypatch.setattr(progress_module.Progress, "INTERVAL", 0.0)
    return Bar


def test_bar_counts_items_off_and_closes(bar):
    bar_ = progress_module.Progress()
    bar_.begin(4)
    for _ in range(4):
        bar_.step("Movie")
    bar_.close()
    (dialog,) = bar.instances
    assert dialog.events[0][0] == "create" and dialog.events[-1] == ("close",)
    assert [e[1] for e in dialog.events if e[0] == "update"] == [25, 50, 75, 100]
    assert dialog.events[-2][2].endswith("4 / 4")


def test_bar_respects_the_setting_and_the_threshold(bar):
    SETTINGS["syncProgressThreshold"] = 10
    small = progress_module.Progress()
    small.begin(5)
    small.step("Movie")
    small.close()
    assert bar.instances == []
    SETTINGS["showLibraryUpdateProgress"] = False
    off = progress_module.Progress()
    off.begin(50)
    off.close()
    assert bar.instances == []


def test_bar_paints_at_most_once_an_interval_and_on_a_phase_change(bar, monkeypatch):
    """DialogProgressBG.update waits on the app thread; a pass must not paint
    from every item."""
    monkeypatch.setattr(progress_module.Progress, "INTERVAL", 1e9)
    bar_ = progress_module.Progress()
    bar_.begin(300)
    for _ in range(300):
        bar_.step("Movie")
    bar_.note("phase")
    (dialog,) = bar.instances
    updates = [e for e in dialog.events if e[0] == "update"]
    assert len(updates) <= 2 and updates[-1][2] == "phase"


def test_enumeration_tracks_pages_from_the_first_total(bar):
    bar_ = progress_module.Progress()
    bar_.track("Movie", 500, 1788)
    bar_.track("Movie", 1788, 1788)
    bar_.close()
    (dialog,) = bar.instances
    assert [e[1] for e in dialog.events if e[0] == "update"] == [27, 100]


def test_pass_shows_the_bar_and_lets_kodi_show_its_scan(store, backend, kodi, bar):
    store.publish([movie("m%02d" % i) for i in range(3)], library=LIB)
    backend.reconcile()
    (dialog,) = bar.instances
    assert dialog.events[0][0] == "create" and dialog.events[-1] == ("close",)
    assert any(
        e[0] == "update" and (e[2] or "").endswith("3 / 3") for e in dialog.events
    )
    scans = methods(kodi, "VideoLibrary.Scan")
    assert scans and all(p["showdialogs"] is True for p in scans)


def test_music_scans_never_show_kodi_dialogs(store, backend, kodi, bar):
    """With a dialog the music scanner starts its file-counter thread, which
    lists every directory a second time, concurrently."""
    from tests.unit.apifixtures import song

    store.publish([song("s%02d" % i) for i in range(3)], library=LIB)
    backend.reconcile()
    scans = methods(kodi, "AudioLibrary.Scan")
    assert scans and all(p["showdialogs"] is False for p in scans)


def test_pass_keeps_kodi_quiet_when_the_setting_is_off(store, backend, kodi, bar):
    SETTINGS["showLibraryUpdateProgress"] = False
    store.publish([movie("m%02d" % i) for i in range(3)], library=LIB)
    backend.reconcile()
    assert bar.instances == []
    scans = methods(kodi, "VideoLibrary.Scan")
    assert scans and all(p["showdialogs"] is False for p in scans)


def test_an_unreadable_setting_means_no_bar_and_no_failure(bar, monkeypatch):
    """The API profile once shipped without the progress settings; Kodi then
    raises TypeError from getSettingBool, and a pass must not fail on it."""

    def broken(_):
        raise TypeError("Invalid setting type")

    monkeypatch.setattr(progress_module.settings, "get_bool", broken)
    monkeypatch.setattr(progress_module.settings, "get_int", broken)
    bar_ = progress_module.Progress()
    bar_.begin(500)
    bar_.step("Movie")
    bar_.close()
    assert bar.instances == []
    assert progress_module.show_dialogs() is False


def test_a_scan_moves_the_bar_by_kodis_own_count(
    store, backend, kodi, bar, monkeypatch
):
    """The scan is the longest phase and the pass counts nothing of its own
    until it ends; the bar reads Kodi's row count under the library instead."""
    from kofin.sync.backends.api import native as native_module
    from kofin.sync.backends.api import paths

    root = paths.library_dir(store.namespace, LIB, "music")
    state = {"scanning": False, "polls": 0, "count": 10}
    original = kodi.rpc

    def rpc(method, params=None):
        if method == "AudioLibrary.Scan":
            state["scanning"] = True
            return "OK"
        if (
            method == "AudioLibrary.GetSongs"
            and params.get("limits", {}).get("end") == 0
        ):
            return {
                "songs": [],
                "limits": {"start": 0, "end": 0, "total": state["count"]},
            }
        return original(method, params)

    def visible(flag):
        if flag == "Library.IsScanningMusic" and state["scanning"]:
            state["polls"] += 1
            state["count"] = 10 + min(state["polls"], 4)
            if state["polls"] >= 6:
                state["scanning"] = False
                backend.monitor.music_finished += 1
            return True
        return False

    monkeypatch.setattr(native_module, "rpc", rpc)
    monkeypatch.setattr(native_module.xbmc, "getCondVisibility", visible)
    monkeypatch.setattr(backend, "SCAN_POLL", 0.0)
    backend.progress.begin(100)
    backend.scan([root], "music", expected={root: 4})
    (dialog,) = bar.instances
    messages = [e[2] for e in dialog.events if e[0] == "update"]
    assert any(m.endswith("4 / 4") for m in messages)
    assert backend.progress.total == 100 and backend.progress.done == 0


def test_a_music_walk_is_one_phase_of_the_bar_across_its_parts(
    store, backend, kodi, bar, monkeypatch
):
    """The music pass owns the bar for its whole plan: Kodi's count of the
    library's songs climbs towards the plan's total across the parts,
    where a phase a part would start the bar again at every part."""
    from kofin.sync.backends.api import native as native_module
    from kofin.sync.backends.api import paths

    monkeypatch.setattr(paths, "MUSIC_PART", 1)
    state = {"scanning": False, "left": 0, "count": 10, "scans": []}
    original = kodi.rpc

    def rpc(method, params=None):
        if method == "AudioLibrary.Scan":
            state["scans"].append(params["directory"])
            state["scanning"], state["left"] = True, 3
            return "OK"
        if (
            method == "AudioLibrary.GetSongs"
            and params.get("limits", {}).get("end") == 0
        ):
            return {
                "songs": [],
                "limits": {"start": 0, "end": 0, "total": state["count"]},
            }
        return original(method, params)

    def visible(flag):
        if flag == "Library.IsScanningMusic" and state["scanning"]:
            state["count"] += 1
            state["left"] -= 1
            if state["left"] <= 0:
                state["scanning"] = False
                backend.monitor.music_finished += 1
            return True
        return False

    monkeypatch.setattr(native_module, "rpc", rpc)
    monkeypatch.setattr(native_module.xbmc, "getCondVisibility", visible)
    monkeypatch.setattr(backend, "SCAN_POLL", 0.0)
    backend.progress.begin(100)
    backend.music.scan(LIB, {"album-a", "album-b"}, expected=6)
    assert state["scans"] == [
        paths.music_part(store.namespace, LIB, 0),
        paths.music_part(store.namespace, LIB, 1),
    ]
    (dialog,) = bar.instances
    counts = [
        int(m.rsplit(" ", 3)[1])
        for m in (e[2] for e in dialog.events if e[0] == "update")
        if m.endswith("/ 6")
    ]
    # One count for the run: it never starts again at the second part.
    assert counts == sorted(counts) and counts[-1] >= 4
    assert backend.progress.total == 100 and backend.progress.done == 0
    assert (LIB, "album-a") in backend.music.rescanned


# -- what a viewer asked for ---------------------------------------------------


@pytest.fixture
def stages(monkeypatch):
    monkeypatch.setattr(progress_module, "stage", lambda code: "stage %d" % code)


def test_an_attended_bar_is_up_at_once_and_stays_until_the_work_is_finished(
    bar, stages
):
    """A library selected, updated, repaired or taken out. The bar used to
    be made by the first page of the first listing, only above the
    threshold, and closed between the listing and the pass: seconds of
    nothing after the request and again between the stages."""
    SETTINGS["syncProgressThreshold"] = 100
    bar_ = progress_module.Progress()
    bar_.attend()
    (dialog,) = bar.instances
    assert dialog.events == [("create", "Kofin", "stage 314")]
    # A listing of fewer items than the threshold is still shown.
    bar_.track("Movie", 3, 5)
    assert dialog.events[-1][:2] == ("update", 60)
    # The end of the listing is not the end of the work.
    bar_.close()
    assert dialog.events[-1][0] == "update"
    # A stretch that counts nothing says what it is and starts from nought.
    bar_.working(progress_module.SAVING)
    assert dialog.events[-1] == ("update", 0, "stage 33008")
    bar_.begin(4)
    bar_.step("Movie")
    bar_.close()
    assert ("close",) not in dialog.events
    bar_.finish()
    assert dialog.events[-1] == ("close",)
    assert not bar_.attended and len(bar.instances) == 1
    # Unattended again, the threshold decides as before.
    bar_.begin(5)
    assert len(bar.instances) == 1


def test_one_bar_carries_a_request_from_the_listing_through_the_pass(
    store, backend, kodi, bar, stages, monkeypatch
):
    from kofin.sync.backends.api import library
    from tests.unit.apifixtures import Server, worker

    SETTINGS["syncProgressThreshold"] = 100
    server = Server({LIB: [movie(), movie("b")]})
    w = worker(store, server, [LIB], monkeypatch)
    # As the library thread makes its pass: with its own bar.
    native = backend
    native.progress = w._bar()
    w.command("UpdateLibrary", {})
    (dialog,) = bar.instances
    assert dialog.events[0] == ("create", "Kofin", "stage 314")
    w.refresh()
    w.apply(native)
    w.settle_progress()
    assert len(bar.instances) == 1
    messages = [e[2] for e in dialog.events if e[0] == "update"]
    # Listed, saved, prepared, scanned, counted off, and only then closed.
    assert "stage 33008" in messages
    assert messages.index("stage 33008") < max(
        i for i, m in enumerate(messages) if m == "stage 314"
    )
    assert any((m or "").endswith("2 / 2") for m in messages)
    assert dialog.events[-1] == ("close",)
    assert dialog.events.count(("close",)) == 1
    assert library.ATTENDED


def test_a_tick_that_failed_takes_the_bar_down(store, bar, stages, monkeypatch):
    from tests.unit.apifixtures import Server, worker

    w = worker(store, Server({LIB: [movie()]}), [LIB], monkeypatch)
    w.command("UpdateLibrary", {})
    (dialog,) = bar.instances
    # Work is still owed, so the bar stays...
    w.settle_progress()
    assert dialog.events[-1] != ("close",)
    # ...but not across the retries of a failure.
    w.settle_progress(failed=True)
    assert dialog.events[-1] == ("close",)


def test_a_library_leaving_says_so_by_name(store, backend, kodi, bar, stages):
    from kofin.sync import private

    SETTINGS["syncProgressThreshold"] = 100
    store.publish([movie(), movie("b")], library=LIB)
    backend.reconcile()
    with private.Database() as db:
        db.cursor.execute(
            "INSERT OR REPLACE INTO view VALUES (?,?,?)", (LIB, "Films", "movies")
        )
    Bar.instances = []
    backend.progress.attend()
    store.publish([], library=LIB)
    backend.reconcile()
    (dialog,) = bar.instances
    messages = [e[2] for e in dialog.events if e[0] == "update"]
    assert "stage 30408: Films" in messages
    backend.progress.finish()
