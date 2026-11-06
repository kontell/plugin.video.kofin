"""Kofin beside another source of the same show, and a pass that keeps failing."""

from types import SimpleNamespace

import pytest

from kofin.sync.backends.api import library, removal
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    LIB2,
    SHOW,
    SHOW2,
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


def shared(store, backend, kodi, also=None):
    """Our show filed, beside ``also`` when given, and sharing its row with
    an episode of the other source /local/Shows/; returns that episode's
    Kodi id."""
    items = show_bundle()
    if also:
        items += show_bundle(also, prefix="f")
    store.publish(items, library=LIB)
    backend.reconcile()
    _, local = merge(kodi)
    kodi.sources = [{"file": "/local/Shows/", "label": "Shows"}]
    return local


def test_a_clear_stopped_after_kodi_ran_it_still_has_the_other_source_scanned(
    store, backend, kodi, monkeypatch
):
    """The clear is waited for when the pass is stopped, and the library is
    cleared again by the next pass, which finds no shared row left to read
    the other source from: the scan owed was recorded before the delete."""
    shared(store, backend, kodi)
    clear = removal.clear_library

    def stopped(native, library, kinds, rows=0):
        clear(native, library, kinds, rows)
        raise InterruptedError("native sync stopped")

    monkeypatch.setattr(removal, "clear_library", stopped)
    store.publish([], library=LIB)
    with pytest.raises(InterruptedError):
        backend.reconcile()
    assert "/local/Shows/" not in kodi.scanned
    monkeypatch.setattr(removal, "clear_library", clear)
    backend.reconcile()
    assert "/local/Shows/" in kodi.scanned
    assert not store.pending() and not store.owed_scans()


def test_a_library_whose_shared_rows_cannot_be_read_stays_until_they_can(
    store, backend, kodi, monkeypatch
):
    local = shared(store, backend, kodi)
    foreign = backend.readback.foreign_episodes

    def refused(tvshowid):
        raise RuntimeError("Kodi refused VideoLibrary.GetEpisodes")

    monkeypatch.setattr(backend.readback, "foreign_episodes", refused)
    store.publish([], library=LIB)
    with pytest.raises(RuntimeError, match="remain pending"):
        backend.reconcile()
    assert local in kodi.rows["Episode"]
    assert SHOW in {i.item_id for i, _, _ in store.pending()}
    monkeypatch.setattr(backend.readback, "foreign_episodes", foreign)
    backend.reconcile()
    assert local not in kodi.rows["Episode"]
    assert "/local/Shows/" in kodi.scanned
    assert not store.pending() and not store.owed_scans()


def test_a_scan_owed_that_fails_is_made_by_the_next_pass(
    store, backend, kodi, monkeypatch
):
    """The library is not cleared a second time for it."""
    shared(store, backend, kodi)
    scan = backend.scan

    def refused(directories, scanner="video", **kwargs):
        if directories == ["/local/Shows/"]:
            raise RuntimeError("Kodi refused VideoLibrary.Scan")
        return scan(directories, scanner, **kwargs)

    monkeypatch.setattr(backend, "scan", refused)
    store.publish([], library=LIB)
    backend.reconcile()
    assert not store.pending()
    assert store.owed_scans() == ["/local/Shows/"]
    monkeypatch.setattr(backend, "scan", scan)
    store.publish([movie()], library=LIB2)
    backend.reconcile()
    assert "/local/Shows/" in kodi.scanned
    assert not store.owed_scans()


def test_a_shared_show_removed_alone_and_stopped_still_has_the_scan_made(
    store, backend, kodi, monkeypatch
):
    """Removed by its id, the show is confirmed only after the scan, so a
    pass stopped during it leaves the removal for the next pass."""
    shared(store, backend, kodi, also=SHOW2)
    scan = backend.scan

    def stopped(directories, scanner="video", **kwargs):
        if directories == ["/local/Shows/"]:
            raise InterruptedError("native sync stopped")
        return scan(directories, scanner, **kwargs)

    monkeypatch.setattr(backend, "scan", stopped)
    store.publish(show_bundle(SHOW2, prefix="f"), library=LIB)
    with pytest.raises(InterruptedError):
        backend.reconcile()
    assert SHOW in {i.item_id for i, _, _ in store.pending()}
    monkeypatch.setattr(backend, "scan", scan)
    backend.reconcile()
    assert "/local/Shows/" in kodi.scanned
    assert not store.pending() and not store.owed_scans()


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


def test_a_failing_repair_with_nothing_pending_backs_off_too(store, monkeypatch):
    """A complete listing that matches the catalogue leaves nothing pending
    and still owes the repair; a pass that fails before any item is failed
    leaves the store as it was."""
    w = worker(store, Server({LIB: []}), [LIB], monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library, "status", lambda *a, **k: None)
    runs = []

    def reconcile(repair=False):
        runs.append(clock[0])
        raise RuntimeError(STUCK)

    native = SimpleNamespace(reconcile=reconcile, settings_owed=False)
    w._repair = True
    w._catchup_due = 5000.0
    pulled = []
    while clock[0] < 140:
        try:
            w.apply(native)
        except RuntimeError as error:
            w._log_failure("API native work remains pending", error)
            pulled.append(w._catchup_due)
            w._catchup_due = 5000.0
        clock[0] += 1
    assert not store.has_pending()
    assert gaps(runs) == [5, 10, 20]
    # Only the first failure brings the catch-up forward.
    assert pulled == [130.0, 5000.0, 5000.0, 5000.0]


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


# -- a movie Kodi grouped with another source's copy -------------------------

LOCAL_MOVIE = "/local/Movies/Movie (2020)/Movie (2020).mkv"


def group(kodi, item_id="a"):
    """What Kodi's automatic grouping of similar videos makes of our movie
    when another source's copy is scanned: theirs the default version, the
    row listed under their file, ours a version of it."""
    row = next(
        r
        for r in kodi.rows["Movie"].values()
        if (r.get("uniqueid") or {}).get("kofin", "").endswith(":" + item_id)
    )
    row["versions"] = [row["file"]]
    row["file"] = LOCAL_MOVIE
    return row["movieid"]


def test_a_movie_grouped_with_another_sources_copy_stays_ours(store, backend, kodi):
    store.publish([movie()], library=LIB)
    backend.reconcile()
    movieid = group(kodi)
    kodi.calls.clear()
    store.publish([movie(UserData={"Played": True, "PlayCount": 1})])
    backend.reconcile()
    assert not store.pending()
    assert not methods(kodi, "VideoLibrary.RefreshMovie")
    assert kodi.rows["Movie"][movieid]["playcount"] == 1
    assert kodi.rows["Movie"][movieid]["file"] == LOCAL_MOVIE


def test_a_grouped_movie_whose_cast_moved_is_set_not_refreshed(store, backend, kodi):
    """Refreshed, the row would be read from the other source's file."""
    store.publish([movie()], library=LIB)
    backend.reconcile()
    group(kodi)
    kodi.calls.clear()
    store.publish([movie(People=[{"Name": "Someone New", "Type": "Actor"}])])
    backend.reconcile()
    assert not methods(kodi, "VideoLibrary.RefreshMovie")
    assert not store.pending()


def test_a_grouped_movie_leaving_alone_goes_by_id_and_their_source_is_scanned(
    store, backend, kodi, monkeypatch
):
    store.publish([movie(), movie("b")], library=LIB)
    backend.reconcile()
    movieid = group(kodi)
    kodi.sources = [{"file": "/local/Movies/", "label": "Movies"}]
    scans = []
    monkeypatch.setattr(
        backend, "scan", lambda d, scanner="video", **kw: scans.append(list(d))
    )
    store.publish([movie("b")], library=LIB)
    backend.reconcile()
    assert {"movieid": movieid} in methods(kodi, "VideoLibrary.RemoveMovie")
    assert ["/local/Movies/"] in scans
    assert not store.pending()


def test_a_cleared_movie_library_removes_a_grouped_movie_by_id(
    store, backend, kodi, monkeypatch
):
    """The clear finds a movie by the file its row lists, theirs here."""
    store.publish([movie()], library=LIB)
    backend.reconcile()
    movieid = group(kodi)
    kodi.sources = [{"file": "/local/Movies/", "label": "Movies"}]
    scans = []
    monkeypatch.setattr(
        backend, "scan", lambda d, scanner="video", **kw: scans.append(list(d))
    )
    kodi.calls.clear()
    store.publish([], library=LIB)
    backend.reconcile()
    calls = [m for m, _ in kodi.calls]
    assert calls.index("VideoLibrary.RemoveMovie") < calls.index(
        "VideoLibrary.SetSourceContent"
    )
    assert movieid not in kodi.rows["Movie"]
    assert ["/local/Movies/"] in scans


def test_a_first_movie_import_asks_kodi_about_no_file(store, backend, kodi):
    store.publish([movie(), movie("b")], library=LIB)
    backend.reconcile()
    assert not methods(kodi, "Files.GetFileDetails")
    assert not store.pending()


# -- the review of #401 ------------------------------------------------------


def test_a_new_failure_brings_the_catch_up_forward_and_a_repeat_does_not(failing):
    w, clock, runs, errors, until = failing
    w._catchup_due = clock[0] + 1800
    try:
        w.apply(SimpleNamespace(reconcile=_raise, settings_owed=False))
    except RuntimeError as error:
        w._log_failure("API native work remains pending", error)
    assert w._catchup_due <= clock[0] + 30
    w._catchup_due = clock[0] + 1800
    clock[0] += 1000
    try:
        w.apply(SimpleNamespace(reconcile=_raise, settings_owed=False))
    except RuntimeError as error:
        w._log_failure("API native work remains pending", error)
    assert w._catchup_due == clock[0] - 1000 + 1800


def test_a_pass_that_succeeds_lets_go_of_the_failure_it_held(failing):
    w, clock, runs, errors, until = failing
    errors.extend([STUCK, STUCK, None])
    # Failed at 100 and at 105, succeeded at 115.
    until(116)
    assert runs == [100, 105, 115]
    assert w._failure is None and w._repeated is None


def test_the_clear_asks_for_other_sources_episodes_show_by_show(store, backend, kodi):
    """An episode of another source on a show of nobody else's is never read."""
    store.publish(show_bundle(), library=LIB)
    backend.reconcile()
    elsewhere = kodi.next_id = kodi.next_id + 1
    kodi.rows["Episode"][elsewhere] = {
        "episodeid": elsewhere,
        "tvshowid": 9999,
        "file": "/local/Other/Show/S01E01.mkv",
        "season": 1,
        "episode": 1,
        "uniqueid": {},
    }
    store.publish([], library=LIB)
    backend.reconcile()
    foreign = [
        p
        for p in methods(kodi, "VideoLibrary.GetEpisodes")
        if (p.get("filter") or {}).get("operator") == "doesnotcontain"
    ]
    assert foreign and all("tvshowid" in p for p in foreign)
    assert not methods(kodi, "Files.GetSources")


# -- rows Kodi removes that we did not ------------------------------------------


def our_row(kodi, kind, item_id):
    return next(
        (
            r
            for r in kodi.rows[kind].values()
            if (r.get("uniqueid") or {}).get("kofin", "").endswith(":" + item_id)
        ),
        None,
    )


def test_a_show_removed_with_another_sources_is_filed_again(store, backend, kodi):
    """Removing their source deletes the show both hold, our episodes too."""
    store.publish(show_bundle(), library=LIB)
    backend.reconcile()
    tvshowid, local = merge(kodi, keep_ids=True, folder=LOCAL)
    episodes = len(
        [r for r in kodi.rows["Episode"].values() if r["tvshowid"] == tvshowid]
    )
    kodi.remove_content(LOCAL)
    assert our_row(kodi, "Series", SHOW) is None
    assert backend.refile_removed() == len(show_bundle())
    backend.reconcile()
    row = our_row(kodi, "Series", SHOW)
    assert row is not None and row["tvshowid"] != tvshowid
    ours = [
        r for r in kodi.rows["Episode"].values() if r["tvshowid"] == row["tvshowid"]
    ]
    assert len(ours) == episodes - 1
    assert not store.pending()


def test_a_movie_kodi_removed_is_filed_again_and_compared(store, backend, kodi):
    store.publish([movie()], library=LIB)
    backend.reconcile()
    kodi.rpc(
        "VideoLibrary.RemoveMovie", {"movieid": our_row(kodi, "Movie", "a")["movieid"]}
    )
    assert backend.refile_removed() == 1
    assert store.mapping("a").applied == {}
    backend.reconcile()
    assert our_row(kodi, "Movie", "a") is not None
    assert store.mapping("a").applied.get("hash")
    assert not store.pending()


def test_a_row_kodi_still_holds_under_a_removed_number_is_compared_not_scanned(
    store, backend, kodi
):
    store.publish([movie()], library=LIB)
    backend.reconcile()
    backend.monitor.lost.add(("Movie", our_row(kodi, "Movie", "a")["movieid"]))
    assert backend.refile_removed() == 1
    kodi.calls.clear()
    backend.reconcile()
    assert not methods(kodi, "VideoLibrary.Scan")
    assert store.mapping("a").applied.get("hash")
    assert not store.pending()


def test_what_our_own_pass_removes_puts_nothing_back(store, backend, kodi):
    store.publish([movie(), movie("b")], library=LIB)
    backend.reconcile()
    store.publish([movie()], library=LIB)
    backend.reconcile()
    assert our_row(kodi, "Movie", "b") is None
    assert backend.monitor.lost
    assert backend.refile_removed() == 0
    assert not store.pending()


def test_a_removal_still_pending_is_left_to_the_pass(store, backend, kodi):
    store.publish([movie(), movie("b")], library=LIB)
    backend.reconcile()
    movieid = our_row(kodi, "Movie", "b")["movieid"]
    store.publish([movie()], library=LIB)
    kodi.accept_without_apply = True
    with pytest.raises(RuntimeError):
        backend.reconcile()
    kodi.accept_without_apply = False
    kodi.rpc("VideoLibrary.RemoveMovie", {"movieid": movieid})
    assert backend.refile_removed() == 0


def test_a_refresh_that_gives_the_new_row_the_old_number_puts_nothing_back(
    store, backend, kodi
):
    """SQLite numbers a new row from the highest one left."""
    store.publish([movie()], library=LIB)
    backend.reconcile()
    movieid = our_row(kodi, "Movie", "a")["movieid"]
    kodi.next_id = movieid
    store.publish([movie(People=[{"Name": "Someone New", "Type": "Actor"}])])
    backend.reconcile()
    assert methods(kodi, "VideoLibrary.RefreshMovie")
    assert our_row(kodi, "Movie", "a")["movieid"] == movieid
    assert ("Movie", movieid) in backend.monitor.lost
    assert backend.refile_removed() == 0
    assert not store.pending()


def test_the_monitor_hears_which_rows_kodi_removed():
    import json

    from kofin.sync.backends.api.native import Monitor

    monitor = Monitor()
    for data in (
        {"type": "movie", "id": 3},
        {"type": "tvshow", "id": 4, "transaction": True},
        {"type": "episode", "id": 5},
        {"type": "musicvideo", "id": 6},
        {"type": "set", "id": 7},
        {"type": "movie"},
    ):
        monitor.onNotification("xbmc", "VideoLibrary.OnRemove", json.dumps(data))
    monitor.onNotification(
        "xbmc", "VideoLibrary.OnUpdate", json.dumps({"type": "movie", "id": 8})
    )
    monitor.onNotification("xbmc", "VideoLibrary.OnRemove", "not json")
    assert monitor.lost == {
        ("Movie", 3),
        ("Series", 4),
        ("Episode", 5),
        ("MusicVideo", 6),
    }
    assert monitor.removed == 4
