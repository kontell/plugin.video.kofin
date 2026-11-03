"""Kofin beside another source of the same show, and a pass that keeps failing."""

from types import SimpleNamespace

import pytest

from kofin.sync.backends.api import library
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    SHOW,
    Server,
    backend,
    episode,
    kodi,
    methods,
    movie,
    show_bundle,
    store,
    worker,
)

STUCK = "1 native operations remain pending: Season has no native row yet"


@pytest.fixture
def failing(store, monkeypatch):
    """A worker with work pending whose pass fails as ``errors`` says, one
    entry a pass (None: the pass succeeds), on a clock the test moves."""
    store.publish([movie()], library=LIB)
    w = worker(store, Server({LIB: [movie()]}), [LIB], monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library, "status", lambda *a, **k: None)
    runs = []
    errors = []

    def reconcile(repair=False):
        runs.append(clock[0])
        message = errors.pop(0) if errors else STUCK
        if message is not None:
            raise RuntimeError(message)

    native = SimpleNamespace(reconcile=reconcile, settings_owed=False)

    def until(end):
        """Tick once a second until ``end``, as the library thread does."""
        while clock[0] < end:
            try:
                w.apply(native)
            except RuntimeError:
                pass
            clock[0] += 1

    return w, clock, runs, errors, until


def gaps(runs):
    return [b - a for a, b in zip(runs, runs[1:])]


def test_a_pass_failing_the_same_way_waits_twice_as_long_each_time(failing):
    w, clock, runs, errors, until = failing
    until(260)
    assert gaps(runs) == [5, 10, 20, 40, 80]


def test_the_wait_stops_growing_at_the_catch_up_interval(failing, monkeypatch):
    monkeypatch.setattr(library, "RETRY_MAX", 20.0)
    w, clock, runs, errors, until = failing
    until(180)
    assert set(gaps(runs)[2:]) == {20}


def test_new_work_is_tried_at_once(failing, store):
    w, clock, runs, errors, until = failing
    until(108)
    assert runs == [100, 105]
    store.publish([movie("b")], library=LIB)
    until(109)
    assert runs == [100, 105, 108]


def test_a_different_failure_starts_the_wait_again(failing):
    w, clock, runs, errors, until = failing
    errors.extend([STUCK, STUCK, STUCK, "another failure"])
    until(200)
    assert gaps(runs)[:4] == [5, 10, 20, 5]


def test_a_pass_that_succeeds_forgets_the_failure(failing):
    w, clock, runs, errors, until = failing
    errors.extend([STUCK, STUCK, None])
    until(125)
    # Failed, failed 5 s later, succeeded 10 s after that. The work is still
    # pending (this pass acknowledges nothing), so the next tick runs it, and
    # that failure is a first one.
    assert gaps(runs)[:4] == [5, 10, 1, 5]


def test_a_repeated_failure_is_logged_whole_once(failing, monkeypatch):
    w, clock, runs, errors, until = failing
    logged = []
    monkeypatch.setattr(
        library,
        "LOG",
        SimpleNamespace(
            exception=lambda *a: logged.append("exception"),
            debug=lambda *a: logged.append("debug"),
        ),
    )
    for _ in range(3):
        try:
            w.apply(SimpleNamespace(reconcile=_raise, settings_owed=False))
        except RuntimeError as error:
            w._log_failure("API native work remains pending", error)
        clock[0] += 1000
    assert logged == ["exception", "debug", "debug"]


def _raise(repair=False):
    raise RuntimeError(STUCK)


# -- a show another source shares --------------------------------------------

LOCAL = "/local/Shows/Show/"


def merge(kodi, keep_ids=False, folder=None, series=SHOW):
    """What Kodi makes of our show's row when another source of the same
    show is scanned: our ids gone (scanned after ours) or the row listed
    under the other source's folder (scanned before), and an episode of
    theirs filed on it."""
    row = next(
        r
        for r in kodi.rows["Series"].values()
        if (r.get("uniqueid") or {}).get("kofin", "").endswith(series)
    )
    if not keep_ids:
        row["uniqueid"] = {"imdb": "tt0000001"}
    if folder:
        row["file"] = folder
    local = kodi.next_id = kodi.next_id + 1
    kodi.rows["Episode"][local] = {
        "episodeid": local,
        "tvshowid": row["tvshowid"],
        "file": LOCAL + "Season 1/Show - S01E99.mkv",
        "title": "Local",
        "season": 1,
        "episode": 99,
        "uniqueid": {},
        "playcount": 1,
    }
    return row["tvshowid"], local


def played(store, item_id="ea11"):
    store.publish(
        [episode(item_id, SHOW, 1, 1, UserData={"Played": True, "PlayCount": 1})]
    )


def test_a_show_another_source_rewrote_is_owned_again(store, backend, kodi):
    store.publish(show_bundle(), library=LIB)
    backend.reconcile()
    tvshowid, local = merge(kodi)
    played(store)
    backend.reconcile()
    # Its episode was found through the show's row, and the show is owed a
    # comparison.
    assert store.state(SHOW).status == "pending"
    backend.reconcile()
    row = kodi.rows["Series"][tvshowid]
    assert row["uniqueid"]["kofin"] == "%s:%s" % (store.namespace, SHOW)
    assert methods(kodi, "VideoLibrary.RefreshTVShow") == [
        {"tvshowid": tvshowid, "ignorenfo": False, "refreshepisodes": False}
    ]
    assert local in kodi.rows["Episode"]
    assert not store.pending()


def test_a_show_under_another_sources_folder_is_ours_and_never_refreshed(
    store, backend, kodi
):
    store.publish(show_bundle(), library=LIB)
    backend.reconcile()
    tvshowid, local = merge(kodi, keep_ids=True, folder=LOCAL)
    played(store)
    backend.reconcile()
    assert not store.pending()
    assert not methods(kodi, "VideoLibrary.RefreshTVShow")
    ours = [r for r in kodi.rows["Episode"].values() if r.get("tvshowid") == tvshowid]
    assert any(r.get("playcount") == 1 and r["episodeid"] != local for r in ours)


def test_ids_another_source_took_off_its_own_folders_row_are_set(store, backend, kodi):
    """Refreshed, the row would read the other source's NFO."""
    store.publish(show_bundle(), library=LIB)
    backend.reconcile()
    tvshowid, _ = merge(kodi, folder=LOCAL)
    played(store)
    backend.reconcile()
    backend.reconcile()
    assert not methods(kodi, "VideoLibrary.RefreshTVShow")
    sent = [
        p
        for p in methods(kodi, "VideoLibrary.SetTVShowDetails")
        if p["tvshowid"] == tvshowid
    ]
    assert sent and sent[-1]["uniqueid"]["kofin"] == "%s:%s" % (
        store.namespace,
        SHOW,
    )
    assert not store.pending()


def test_two_of_our_shows_on_one_row_leave_it_to_the_one_it_names(store, backend, kodi):
    other = "b2" * 16
    store.publish(show_bundle() + show_bundle(other, prefix="f"), library=LIB)
    backend.reconcile()
    rows = {
        r["uniqueid"]["kofin"].split(":")[1]: r for r in kodi.rows["Series"].values()
    }
    kept, gone = rows[SHOW]["tvshowid"], rows[other]["tvshowid"]
    # Kodi filed the second show on the first one's row.
    for row in kodi.rows["Episode"].values():
        if row["tvshowid"] == gone:
            row["tvshowid"] = kept
    del kodi.rows["Series"][gone]
    kodi.calls.clear()
    store.invalidate([other])
    backend.reconcile()
    assert not store.pending()
    assert store.mapping(other).kodi_id == kept
    assert kodi.rows["Series"][kept]["uniqueid"]["kofin"].endswith(SHOW)
    assert not [
        p
        for p in methods(kodi, "VideoLibrary.SetTVShowDetails")
        if p["tvshowid"] == kept
    ]


def test_a_cleared_library_has_the_other_source_scanned_again(
    store, backend, kodi, monkeypatch
):
    """The clear deletes a shared show with the other source's episodes;
    a scan of their source files it again."""
    store.publish(show_bundle(), library=LIB)
    backend.reconcile()
    merge(kodi)
    kodi.sources = [
        {"file": "/local/", "label": "All"},
        {"file": "/local/Shows/", "label": "Shows"},
        {"file": "/elsewhere/", "label": "Films"},
    ]
    scans = []
    scan = backend.scan

    def recording(directories, scanner="video", **kwargs):
        scans.append((list(directories), len(kodi.calls)))
        return scan(directories, scanner, **kwargs)

    monkeypatch.setattr(backend, "scan", recording)
    store.publish([], library=LIB)
    backend.reconcile()
    cleared = next(
        i
        for i, (m, p) in enumerate(kodi.calls)
        if m == "VideoLibrary.SetSourceContent" and p.get("clearmode") == "remove"
    )
    rescans = [at for directories, at in scans if directories == ["/local/Shows/"]]
    assert rescans and rescans[0] > cleared


def test_a_library_with_no_shared_show_asks_no_sources(store, backend, kodi):
    store.publish(show_bundle(), library=LIB)
    backend.reconcile()
    store.publish([], library=LIB)
    backend.reconcile()
    assert not methods(kodi, "Files.GetSources")


def test_a_first_import_asks_for_no_show_through_its_episodes(store, backend, kodi):
    """Only a show Kodi has filed for us can sit on another source's row."""
    store.publish(show_bundle() + show_bundle("b2" * 16, prefix="f"), library=LIB)
    backend.reconcile()
    lookups = [
        p
        for p in methods(kodi, "VideoLibrary.GetEpisodes")
        if p.get("limits") == {"start": 0, "end": 1}
    ]
    assert lookups == []
    assert not store.pending()


def test_a_failing_repair_backs_off_too(failing):
    """A complete listing makes the pass after it a repair, and a repair
    that keeps failing would otherwise run again on every tick."""
    w, clock, runs, errors, until = failing
    w._repair = True
    until(140)
    assert gaps(runs) == [5, 10, 20]


def test_a_viewers_command_tries_the_pass_at_once(failing):
    w, clock, runs, errors, until = failing
    until(108)
    assert runs == [100, 105]
    w._bar = lambda: SimpleNamespace(attend=lambda: None)
    w.command("UpdateLibrary", {})
    until(109)
    assert runs == [100, 105, 108]


def test_a_shared_show_leaving_alone_has_the_other_source_scanned(
    store, backend, kodi, monkeypatch
):
    other = "b2" * 16
    store.publish(show_bundle() + show_bundle(other, prefix="f"), library=LIB)
    backend.reconcile()
    tvshowid, _ = merge(kodi)
    kodi.sources = [{"file": "/local/Shows/", "label": "Shows"}]
    scans = []
    scan = backend.scan

    def recording(directories, scanner="video", **kwargs):
        scans.append((list(directories), len(kodi.calls)))
        return scan(directories, scanner, **kwargs)

    monkeypatch.setattr(backend, "scan", recording)
    store.publish(show_bundle(other, prefix="f"), library=LIB)
    backend.reconcile()
    removed = [
        i
        for i, (m, p) in enumerate(kodi.calls)
        if m == "VideoLibrary.RemoveTVShow" and p["tvshowid"] == tvshowid
    ]
    assert removed
    assert [at for d, at in scans if d == ["/local/Shows/"] and at > removed[0]]
    assert not store.pending()


def test_a_shared_show_under_another_sources_folder_is_removed_not_forgotten(
    store, backend, kodi
):
    other = "b2" * 16
    store.publish(show_bundle() + show_bundle(other, prefix="f"), library=LIB)
    backend.reconcile()
    tvshowid, _ = merge(kodi, keep_ids=True, folder=LOCAL)
    store.publish(show_bundle(other, prefix="f"), library=LIB)
    backend.reconcile()
    assert {"tvshowid": tvshowid} in methods(kodi, "VideoLibrary.RemoveTVShow")
    assert not store.pending()


def test_a_collection_removal_ends_when_only_another_sources_movies_hold_the_set(
    store, backend, kodi
):
    from tests.unit.apifixtures import boxset

    store.publish([movie(), boxset("box", "Pair", ["a"])], library=LIB)
    backend.reconcile()
    local = kodi.next_id = kodi.next_id + 1
    kodi.rows["Movie"][local] = {
        "movieid": local,
        "file": "/local/Movies/Local Set Movie (2019)/Local Set Movie (2019).mkv",
        "title": "Local Set Movie",
        "set": "Pair",
        "uniqueid": {"imdb": "tt9000099"},
    }
    store.publish([], removed=["box"])
    backend.reconcile()
    assert not store.pending()
    assert kodi.rows["Movie"][local]["set"] == "Pair"
    assert not methods(kodi, "VideoLibrary.RemoveMovieSet")
