"""The API build's library status: what the settings dialog shows."""

import time

from kofin.sync import private
from kofin.sync.backends.api import status
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    LIB2,
    album_bundle,
    movie,
    show_bundle,
    store,
)

MUSIC = "music-library"
TEXT = {
    314: "Preparing...",
    20177: "Done",
    30262: "Sync status",
    30408: "Removing library",
    30863: "Pending",
    30864: "Failed",
    30865: "Last full update",
    30867: "Repair to add codec details",
    30868: "Kodi cannot import",
}
LABELS = {"Movie": "Movies", "Episode": "Episodes", "Audio": "Songs"}


def _views(monkeypatch, selected, **views):
    with private.Database() as db:
        for library, (name, media) in views.items():
            db.cursor.execute(
                "INSERT OR REPLACE INTO view VALUES (?,?,?)", (library, name, media)
            )
    monkeypatch.setattr(private, "get_sync", lambda: {"Whitelist": list(selected)})


def _file(store, kind, count):
    """Give ``count`` items of a kind a row in Kodi."""
    records = sorted(
        (item_id, record)
        for item_id, record in store.records(pinned=False).items()
        if record.kind == kind
    )
    assert len(records) >= count
    for number, (item_id, record) in enumerate(records[:count]):
        assert store.remember(item_id, record.generation, number + 1, {}, kind)


def _rows(store, document=None, scanner=2):
    return status.rows(
        document or {},
        status.snapshot(store, scanner),
        TEXT.__getitem__,
        LABELS.__getitem__,
        clock=time.gmtime,
    )


def test_a_library_is_counted_by_what_a_viewer_counts(store, monkeypatch):
    store.publish([movie("a"), movie("b"), movie("c")], library=LIB)
    store.publish(show_bundle(episodes=3), library=LIB2)
    store.publish(album_bundle(songs=4), library=MUSIC)
    views = {LIB: ("Films", "movies"), LIB2: ("Shows", "tvshows")}
    views[MUSIC] = ("Music", "music")
    _views(monkeypatch, [LIB, LIB2, MUSIC], **views)
    _file(store, "Movie", 2)
    _file(store, "Episode", 1)
    for library in (LIB, LIB2):
        store.set_scanner_format(library, 2)

    rows = _rows(store)
    assert rows[:4] == [
        "Sync status:  Done",
        "Films:  2 / 3 Movies",
        "Music:  0 / 4 Songs",
        "Shows:  1 / 3 Episodes",
    ]
    assert rows[4].startswith("Pending:  ")


def test_what_kodi_cannot_file_is_counted_apart(store, monkeypatch):
    """A special with no episode number is settled without a row; a library
    one short of the server's count must not read as a failed import."""
    store.publish(show_bundle(episodes=3), library=LIB2)
    _views(monkeypatch, [LIB2], **{LIB2: ("Shows", "tvshows")})
    store.set_scanner_format(LIB2, 2)
    _file(store, "Episode", 2)
    assert "Kodi cannot import:  1" not in _rows(store)
    item_id, record = max(
        (item_id, record)
        for item_id, record in store.records(pinned=False).items()
        if record.kind == "Episode"
    )
    assert store.remember(item_id, record.generation, None, {}, "Episode")

    rows = _rows(store)
    assert rows[1:3] == ["Shows:  2 / 3 Episodes", "Kodi cannot import:  1"]


def test_the_state_is_shown_with_when_it_began(store, monkeypatch):
    _views(monkeypatch, [])
    status.write("Updating library")
    began = status.read()["since"]
    # Said again, it began when it first did.
    status.write("Updating library")
    assert status.read()["since"] == began

    rows = _rows(store, {"state": "Updating library", "since": 0.0})
    assert rows[0] == "Sync status:  Updating library"
    rows = _rows(store, {"state": "Updating library", "since": 86400.0 + 3660})
    assert rows[0] == "Sync status:  Updating library (1970-01-02 01:01)"


def test_a_library_listed_for_the_first_time_is_named_before_it_has_a_row(
    store, monkeypatch
):
    """Until its first listing is published the catalogue knows nothing of
    a library, and the listing is minutes of a first import."""
    store.publish([movie("a")], library=LIB)
    _views(monkeypatch, [LIB], **{LIB: ("Films", "movies")})
    status.write("Updating library", ["Shows", "Films"])
    began = status.read()["since"]

    rows = _rows(store, status.read())
    assert rows[1:3] == ["Shows:  Preparing...", "Films:  0 / 1 Movies"]
    # The listing ended; the update goes on, and began when it did.
    status.write("Updating library")
    assert status.read() == {"state": "Updating library", "since": began, "listing": []}


def test_a_library_being_removed_says_so(store, monkeypatch):
    store.publish([movie("a"), movie("b")], library=LIB)
    _views(monkeypatch, [], **{LIB: ("Films", "movies")})
    _file(store, "Movie", 2)
    store.publish([], library=LIB)

    assert _rows(store)[1:3] == ["Films:  Removing library", "Pending:  2"]


def test_a_failure_is_counted_and_one_reason_given(store, monkeypatch):
    store.publish([movie("a"), movie("b")], library=LIB)
    _views(monkeypatch, [LIB], **{LIB: ("Films", "movies")})
    store.failed("a", store.records(pinned=False)["a"].generation, "no native row")

    assert _rows(store)[2:4] == ["Pending:  2", "Failed:  1 (no native row)"]


def test_rows_filed_before_the_stream_details_ask_for_a_repair(store, monkeypatch):
    store.publish([movie("a")], library=LIB)
    store.publish(album_bundle(songs=1), library=MUSIC)
    views = {LIB: ("Films", "movies"), MUSIC: ("Music", "music")}
    _views(monkeypatch, [LIB, MUSIC], **views)
    _file(store, "Movie", 1)
    _file(store, "Audio", 1)

    assert "Repair to add codec details:  Films" in _rows(store)
    store.set_scanner_format(LIB, 2)
    assert not [row for row in _rows(store) if row.startswith("Repair")]


def test_the_last_complete_listing_is_dated(store, monkeypatch):
    _views(monkeypatch, [])
    store.set_watermark(enumerated=86400.0)

    assert "Last full update:  1970-01-02 00:00" in _rows(store)


def test_a_document_of_another_shape_is_read_as_far_as_it_fits(store, monkeypatch):
    """The dialog's process reads what another wrote. A truthy ``since``
    that is not a number reached the clock and the report did not open."""
    import json

    _views(monkeypatch, [])
    with open(status.path(), "w", encoding="utf-8") as handle:
        json.dump(
            {"state": "Updating library", "since": "unknown", "listing": "Movies"},
            handle,
        )
    assert status.read() == {"state": "Updating library"}
    assert _rows(store, status.read())[0] == "Sync status:  Updating library"

    with open(status.path(), "w", encoding="utf-8") as handle:
        json.dump({"state": 7, "since": True, "listing": ["Movies", 3]}, handle)
    assert status.read() == {"listing": ["Movies"]}
    assert _rows(store, status.read())[:2] == [
        "Sync status:  Done",
        "Movies:  Preparing...",
    ]
    # A number no calendar has is no date, and nothing raised.
    assert _rows(store, {"state": "Updating library", "since": 1e300})[0] == (
        "Sync status:  Updating library"
    )


def test_two_writers_do_not_share_a_temporary_file(store, monkeypatch):
    """Two service generations overlap, each with a library thread. On one
    ".part" path the first rename took the other writer's file, and that
    writer's status was dropped."""
    import os
    import threading

    opened = []
    real_open = open

    def spy(name, mode="r", **kwargs):
        if "w" in mode:
            opened.append(os.path.basename(name))
        return real_open(name, mode, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    status.write("Updating library")
    other = threading.Thread(target=status.write, args=("Removing library",))
    other.start()
    other.join()
    assert len(opened) == 2 and opened[0] != opened[1]
    assert all(name.endswith(".part") for name in opened)
    assert status.read()["state"] == "Removing library"
    assert os.listdir(os.path.dirname(status.path())).count("sync-status.json") == 1
    assert not [n for n in os.listdir(os.path.dirname(status.path())) if ".part" in n]


def test_a_status_that_cannot_be_written_fails_nothing(store, monkeypatch, tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("")
    monkeypatch.setattr(private, "addon_data_path", lambda: str(blocked))

    status.write("Updating library")
    assert status.read() == {}


def _dialog(monkeypatch, store, answers):
    from kofin.plugin import actions
    from kofin.plugin.router import Request
    from kofin.sync.backends.api import identity
    from tests.unit.fakes import FakeDialog

    dialog = FakeDialog()
    dialog.select_answers = list(answers)
    shown = []
    monkeypatch.setattr(status.xbmcgui, "Dialog", lambda: dialog)
    monkeypatch.setattr(actions.toast, "show", lambda *a, **k: shown.append(a[0]))
    monkeypatch.setattr(actions.settings, "localized", lambda i: "L%d" % i)
    monkeypatch.setattr(status.xbmc, "getLocalizedString", lambda i: "K%d" % i)
    monkeypatch.setattr(identity, "current_store", lambda: store)
    actions.library_status(Request("plugin://x", -1, {}))
    return dialog, shown


def test_the_dialog_is_a_report_whose_first_row_reads_it_again(store, monkeypatch):
    store.publish([movie("a")], library=LIB)
    _views(monkeypatch, [LIB], **{LIB: ("Films", "movies")})

    # The refresh row, then back.
    dialog, shown = _dialog(monkeypatch, store, [0, -1])

    assert shown == []
    assert len(dialog.selects) == 2
    heading, rows, preselect = dialog.selects[0]
    assert heading == "L30859 \u2014 L30862"
    assert rows[0] == "K184" and preselect == 0
    assert rows[1] == "L30262:  K20177"
    assert rows[2].startswith("Films:  0 / 1")


def test_a_fact_that_is_picked_does_nothing_and_back_closes_the_report(
    store, monkeypatch
):
    """Only the first row is an action. A select dialog closes on any row,
    so a fact that was picked is shown again as it was, under the cursor."""
    store.publish([movie("a")], library=LIB)
    _views(monkeypatch, [LIB], **{LIB: ("Films", "movies")})
    reads = []
    real = status.snapshot
    monkeypatch.setattr(
        status, "snapshot", lambda *args: reads.append(1) or real(*args)
    )

    # A fact, another fact, the refresh row, then back.
    dialog, _ = _dialog(monkeypatch, store, [2, 1, 0, -1])

    assert len(dialog.selects) == 4
    assert [preselect for _, _, preselect in dialog.selects] == [0, 2, 1, 0]
    # The facts were read when the report opened and at the refresh row,
    # not when one of them was picked.
    assert len(reads) == 2
    assert dialog.selects[0][1] == dialog.selects[1][1] == dialog.selects[2][1]
