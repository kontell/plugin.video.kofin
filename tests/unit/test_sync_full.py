"""FullSync queue behavior: a library deleted server-side must not wedge
the sync (it 404s forever otherwise — the queue only drops entries that
complete), and a crash-resumed queue must not carry duplicates."""

import pytest

from kofin.core.http import HttpError
from kofin.sync.full_sync import FullSync


class FakeServer:
    """server.item() by canned status: 200 -> payload, else HttpError."""

    def __init__(self, status_by_id):
        self.status_by_id = status_by_id

    def item(self, item_id):
        status = self.status_by_id[item_id]
        if status != 200:
            raise HttpError(status, "GET /Items/%s -> %d" % (item_id, status))
        return {"Id": item_id, "CollectionType": "movies"}


@pytest.fixture
def fullsync(monkeypatch):
    monkeypatch.setattr("kofin.sync.full_sync.save_sync", lambda sync: None)
    monkeypatch.setattr("kofin.sync.full_sync.notification", lambda *a, **kw: None)
    sync = FullSync(library=None, server=None)
    sync.sync = {"Libraries": [], "Whitelist": [], "RestorePoints": {}}
    yield sync


def test_deleted_library_dropped_not_whitelisted(fullsync):
    fullsync.server = FakeServer({"gone1": 404})
    fullsync.sync["Libraries"] = ["gone1"]
    failures = []

    fullsync.process_libraries(["gone1"], failures)

    assert failures == []
    assert fullsync.sync["Libraries"] == []
    assert fullsync.sync["Whitelist"] == []


def test_other_http_errors_still_fail_and_keep_the_entry(fullsync):
    fullsync.server = FakeServer({"flaky1": 500})
    fullsync.sync["Libraries"] = ["flaky1"]
    failures = []

    fullsync.process_libraries(["flaky1"], failures)

    assert len(failures) == 1
    assert isinstance(failures[0], HttpError)
    assert fullsync.sync["Libraries"] == ["flaky1"]
    assert fullsync.sync["Whitelist"] == []


def test_a_failing_library_does_not_abandon_the_ones_after_it(fullsync, monkeypatch):
    """The failures list and start()'s ``raise failures[0]`` always meant
    collect-and-continue, but the try sat around the loop, so the first
    failure ended the walk: a 500 on library one left library two waiting
    for the resume backoff instead of syncing now."""
    fullsync.server = FakeServer({"flaky1": 500, "lib2": 200})
    fullsync.sync["Libraries"] = ["flaky1", "lib2"]
    fullsync.library = RecordingLibrary()
    monkeypatch.setattr(fullsync, "movies", lambda library: None)
    monkeypatch.setattr(fullsync, "_media_type", lambda library_id: "movies")
    failures = []

    fullsync.process_libraries(["flaky1", "lib2"], failures)

    assert len(failures) == 1
    assert isinstance(failures[0], HttpError)
    # The failed one stays queued for the resume backoff; the other landed.
    assert fullsync.sync["Libraries"] == ["flaky1"]
    assert fullsync.sync["Whitelist"] == ["lib2"]
    # The last library is not published from the loop: start()'s end-of-sync
    # refresh covers it, failure or not, and runs before the re-raise
    # (test_a_failed_run_still_gets_the_end_of_sync_refresh).
    assert fullsync.library.refreshed == []


def test_an_exit_exception_abandons_the_rest(fullsync, monkeypatch):
    """Kodi quitting, the service stopping or the server going away is not a
    library failure -- every remaining library would raise the same -- so it
    propagates at once and nothing after it is attempted."""
    from kofin.sync.shims import LibraryExitException

    class ExitingServer(FakeServer):
        def item(self, item_id):
            if item_id == "exit1":
                raise LibraryExitException("Server not online, exiting...")
            return super().item(item_id)

    fullsync.server = ExitingServer({"lib2": 200})
    fullsync.sync["Libraries"] = ["exit1", "lib2"]
    attempted = []
    monkeypatch.setattr(fullsync, "movies", lambda library: attempted.append(library))
    failures = []

    with pytest.raises(LibraryExitException):
        fullsync.process_libraries(["exit1", "lib2"], failures)

    assert failures == []
    assert attempted == []
    assert fullsync.sync["Libraries"] == ["exit1", "lib2"]
    assert fullsync.sync["Whitelist"] == []


def test_item_gone_server_side_is_skipped_not_fatal(fullsync):
    """A show deleted after it was paged 404s on the writer's /Seasons fetch.
    Live phase 5: that aborted the whole library and re-fired a sync-failed
    toast on every service start, forever."""

    def apply(obj, item):
        raise HttpError(404, "GET /Shows/%s/Seasons -> 404" % item["Id"])

    assert fullsync.apply_or_skip(apply, None, {"Id": "gone-show"}, "Series") == (
        False,
        None,
    )


def test_item_other_http_error_still_aborts_the_pass(fullsync):
    """The guard is for dead ids only — a 500 is a real failure and must not
    be downgraded into a silently incomplete library."""

    def apply(obj, item):
        raise HttpError(500, "GET /Shows/%s/Seasons -> 500" % item["Id"])

    with pytest.raises(HttpError):
        fullsync.apply_or_skip(apply, None, {"Id": "flaky-show"}, "Series")


def test_item_applied_normally_reports_success(fullsync):
    written = []
    assert fullsync.apply_or_skip(
        lambda obj, item: written.append(item["Id"]),
        None,
        {"Id": "live-show"},
        "Series",
    ) == (True, None)
    assert written == ["live-show"]


def test_synced_library_still_whitelisted(fullsync, monkeypatch):
    fullsync.server = FakeServer({"lib1": 200})
    fullsync.sync["Libraries"] = ["lib1"]
    monkeypatch.setattr(fullsync, "movies", lambda library: None)
    failures = []

    fullsync.process_libraries(["lib1"], failures)

    assert failures == []
    assert fullsync.sync["Libraries"] == []
    assert fullsync.sync["Whitelist"] == ["lib1"]


class RecordingLibrary:
    """The Library slice start() touches once the passes are done."""

    def __init__(self):
        self.refreshed = []
        self.forced = []

    def stamp_watermark_if_empty(self):
        pass

    def refresh_libraries(self, databases, force_reload=False):
        self.refreshed.append(set(databases))
        self.forced.append(force_reload)


def run_start(fullsync, monkeypatch, update):
    toasts = []
    monkeypatch.setattr(
        "kofin.sync.full_sync.notification",
        lambda *a, **kw: toasts.append(a[0] if a else kw),
    )
    monkeypatch.setattr("kofin.sync.full_sync.localized", lambda code: str(code))
    monkeypatch.setattr(fullsync, "_media_type", lambda library_id: "music")
    monkeypatch.setattr(fullsync, "process_libraries", lambda libraries, failures: None)

    fullsync.library = RecordingLibrary()
    fullsync.update_library = update
    fullsync.sync["Libraries"] = ["lib2"]
    fullsync.start()

    return toasts


def test_update_mode_does_not_announce_a_completed_sync(fullsync, monkeypatch):
    """Update mode only plans: prune diffs the library and hands the work to
    the incremental pipeline, which reports its own progress. The completion
    toast fired seconds after queueing a 22k-item backlog, claiming it was
    already written."""
    assert run_start(fullsync, monkeypatch, update=True) == []


def test_full_sync_still_announces_completion(fullsync, monkeypatch):
    toasts = run_start(fullsync, monkeypatch, update=False)

    assert len(toasts) == 1
    assert "30409" in toasts[0]


def test_update_mode_plans_without_refreshing(fullsync, monkeypatch):
    """Update mode hands every write to the incremental pipeline, so the
    refresh belongs to the drain that lands it: refreshing at plan time
    re-rendered every widget for rows that had not changed yet
    (widget-refresh-plan F2/D4)."""
    run_start(fullsync, monkeypatch, update=True)

    assert fullsync.library.refreshed == []


def test_full_sync_refreshes_what_it_wrote(fullsync, monkeypatch):
    run_start(fullsync, monkeypatch, update=False)

    assert fullsync.library.refreshed == [{"music"}]


def test_resumed_queue_is_deduplicated(fullsync, monkeypatch):
    monkeypatch.setattr(
        "kofin.sync.full_sync.get_sync",
        lambda: {
            "Libraries": ["a", "Boxsets:x", "a", "b", "Boxsets:x", "a"],
            "Whitelist": [],
            "RestorePoints": {},
        },
    )
    started = []
    monkeypatch.setattr(fullsync, "start", lambda: started.append(True))

    fullsync.libraries()

    assert fullsync.sync["Libraries"] == ["a", "Boxsets:x", "b"]
    assert started == [True]


# --- failure ceilings (healing-loops-plan F3) --------------------------------


def _toast_recorder(monkeypatch):
    toasts = []
    monkeypatch.setattr(
        "kofin.sync.full_sync.notification",
        lambda *args, **kwargs: toasts.append(args),
    )
    return toasts


def test_failing_library_toasts_once_per_service_lifetime(fullsync, monkeypatch):
    from types import SimpleNamespace

    toasts = _toast_recorder(monkeypatch)
    fullsync.server = FakeServer({"flaky1": 500})
    fullsync.library = SimpleNamespace(sync_failure_toasted=set())

    for _ in range(3):
        failures = []
        fullsync.process_libraries(["flaky1"], failures)
        assert len(failures) == 1

    assert len(toasts) == 1


def test_failing_library_without_a_manager_still_toasts(fullsync, monkeypatch):
    """library=None constructions (tests, tools) keep the old per-attempt
    behavior rather than crashing on the dedup set."""
    toasts = _toast_recorder(monkeypatch)
    fullsync.server = FakeServer({"flaky1": 500})

    for _ in range(2):
        fullsync.process_libraries(["flaky1"], [])

    assert len(toasts) == 2


def test_library_exception_keeps_the_entry(fullsync, monkeypatch):
    """A non-exit LibraryException is a pass-level failure (the prune-map
    truncation guard, for one). The fork swallowed it and dropped the entry
    as synced; it must stay queued for the resume backoff instead."""
    from kofin.sync.shims import LibraryException

    _toast_recorder(monkeypatch)
    fullsync.server = FakeServer({"lib1": 200})
    fullsync.sync["Libraries"] = ["lib1"]
    fullsync.update_library = True
    monkeypatch.setattr(
        FullSync,
        "prune",
        lambda self, library, library_id, dialog=None: (_ for _ in ()).throw(
            LibraryException("prune map paged without a TotalRecordCount")
        ),
    )

    failures = []
    fullsync.process_libraries(["lib1"], failures)

    assert len(failures) == 1
    assert isinstance(failures[0], LibraryException)
    assert fullsync.sync["Libraries"] == ["lib1"]
    assert fullsync.sync["Whitelist"] == []


# --- the one-sync-at-a-time claim (audit finding #11) -------------------------


class ClaimLibrary:
    """The claim half of Library, standing in for the real manager."""

    def __init__(self):
        import threading

        self._full_sync_lock = threading.Lock()
        self._full_sync_running = False
        self.released = 0

    def claim_full_sync(self):
        with self._full_sync_lock:
            if self._full_sync_running:
                return False
            self._full_sync_running = True
            return True

    def release_full_sync(self):
        with self._full_sync_lock:
            self._full_sync_running = False
            self.released += 1


def test_a_second_sync_on_the_same_library_is_refused(monkeypatch):
    monkeypatch.setattr("kofin.sync.full_sync.notification", lambda *a, **kw: None)
    library = ClaimLibrary()

    first = FullSync(library, server=None)
    with pytest.raises(Exception, match="already running"):
        FullSync(library, server=None)

    first.release()
    FullSync(library, server=None).release()  # the claim came back


def test_the_claim_dies_with_its_library(monkeypatch):
    """The fork kept this in a class-level Borg dict, which outlived the
    service's object graph: an orphaned sync left it True and the *new*
    Library refused to sync at all until Kodi restarted (audit finding #11)."""
    monkeypatch.setattr("kofin.sync.full_sync.notification", lambda *a, **kw: None)
    orphaned = ClaimLibrary()
    FullSync(orphaned, server=None)  # never released: the old manager is gone

    rebuilt = ClaimLibrary()
    FullSync(rebuilt, server=None)  # must not raise


def test_exit_releases_the_claim(monkeypatch):
    monkeypatch.setattr("kofin.sync.full_sync.notification", lambda *a, **kw: None)
    monkeypatch.setattr("kofin.sync.full_sync.state.set_sync_active", lambda on: None)
    library = ClaimLibrary()

    with FullSync(library, server=None):
        pass

    assert library.released == 1
    assert library._full_sync_running is False


# -- per-library publishing (docs/widget-refresh-plan.md; B2 on a Pi 3B) ------


class PublishFullSync(FullSync):
    """FullSync with the library pass stubbed, so process_libraries' own
    publishing is what the test observes."""

    def process_library(self, library):
        return self.synced_result.get(library, True)


class PublishLibrary(ClaimLibrary, RecordingLibrary):
    """Both halves of the Library that process_libraries touches: the claim
    FullSync takes at construction, and the refresh it hands each finished
    library to."""

    def __init__(self):
        ClaimLibrary.__init__(self)
        RecordingLibrary.__init__(self)


@pytest.fixture
def publisher(monkeypatch):
    monkeypatch.setattr("kofin.sync.full_sync.save_sync", lambda sync: None)
    monkeypatch.setattr("kofin.sync.full_sync.notification", lambda *a, **kw: None)
    sync = PublishFullSync(library=PublishLibrary(), server=None)
    sync.sync = {"Libraries": [], "Whitelist": [], "RestorePoints": {}}
    sync.synced_result = {}
    sync.update_library = False
    yield sync
    sync.release()


def media_types(monkeypatch, publisher, mapping):
    monkeypatch.setattr(
        publisher, "_media_type", lambda library_id: mapping[library_id]
    )


def test_each_finished_library_is_published_except_the_last(publisher, monkeypatch):
    """A full sync used to show nothing until the *last* library finished —
    43 minutes on a Pi 3B, with movies complete and browsable at 8. The final
    library is left to sync()'s end-of-sync refresh so the rows are not paid
    for twice."""
    media_types(
        monkeypatch, publisher, {"mov": "movies", "tv": "tvshows", "mus": "music"}
    )

    publisher.process_libraries(["mov", "tv", "mus"], [])

    assert publisher.library.refreshed == [{"video"}, {"video"}]


def test_a_music_library_publishes_the_music_database(publisher, monkeypatch):
    """Refreshing video for a music library left a freshly synced music
    library invisible in the music widgets; the split is per database."""
    media_types(monkeypatch, publisher, {"mus": "music", "mov": "movies"})

    publisher.process_libraries(["mus", "mov"], [])

    assert publisher.library.refreshed == [{"music"}]


def test_a_library_that_did_not_sync_is_not_published(publisher, monkeypatch):
    """process_library returning falsey means nothing landed — publishing it
    would buy a Kodi scan and a vacuum to show no new rows."""
    media_types(monkeypatch, publisher, {"mov": "movies", "tv": "tvshows"})
    publisher.synced_result = {"mov": False}

    publisher.process_libraries(["mov", "tv"], [])

    assert publisher.library.refreshed == []


def test_a_failed_run_still_gets_the_end_of_sync_refresh(publisher, monkeypatch):
    """start() re-raises a library failure only after its end-of-sync
    refresh, so what the other libraries wrote is shown -- with
    force_reload, which the per-library publish does not carry -- and the
    whole queue's databases are covered, the failed library's included."""
    media_types(monkeypatch, publisher, {"mov": "movies", "tv": "tvshows"})
    real = publisher.process_library

    def failing_first(library):
        if library == "mov":
            raise HttpError(500, "GET /Items/mov -> 500")
        return real(library)

    monkeypatch.setattr(publisher, "process_library", failing_first)
    publisher.sync["Libraries"] = ["mov", "tv"]

    with pytest.raises(HttpError):
        publisher.start()

    assert publisher.library.refreshed == [{"video"}]
    assert publisher.library.forced == [True]


def test_update_mode_publishes_nothing(publisher, monkeypatch):
    """Update mode only *plans*: the incremental drain that lands the work
    owns its own refresh, the same reason sync() skips it."""
    media_types(monkeypatch, publisher, {"mov": "movies", "tv": "tvshows"})
    publisher.update_library = True

    publisher.process_libraries(["mov", "tv"], [])

    assert publisher.library.refreshed == []


def test_a_single_library_is_left_to_the_end_of_sync_refresh(publisher, monkeypatch):
    media_types(monkeypatch, publisher, {"mov": "movies"})

    publisher.process_libraries(["mov"], [])

    assert publisher.library.refreshed == []


def test_the_end_of_sync_refresh_forces_the_reload(fullsync, monkeypatch):
    """The probes cannot be trusted by then: Library.HasContent can flip true
    mid-sync, so the end-of-sync refresh asks for the rebuild outright. Live on
    a Pi 3B, a movies reload rebuilt Home while music was empty and the music
    probe had already self-disarmed by the time music finished."""
    run_start(fullsync, monkeypatch, update=False)

    assert fullsync.library.forced == [True]


def test_a_mid_sync_publish_does_not_force_the_reload(publisher, monkeypatch):
    """Only the end of the sync knows everything has landed. A publish mid-run
    stays probe-gated, so it reveals a kind that is genuinely hidden and does
    not rebuild the skin for one that is already on screen."""
    media_types(monkeypatch, publisher, {"mov": "movies", "tv": "tvshows"})

    publisher.process_libraries(["mov", "tv"], [])

    assert publisher.library.forced == [False]
