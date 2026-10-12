"""The C4 request kinds, built from the acknowledged mappings: a favourite
episode's tag, a season's plot, a file's stream flags, an emptied set; and
the acknowledgement of the kinds no public read confirms."""

import json
import os

import pytest

from kofin.sync.backends.api import companion, metadata, paths
from tests.unit import contractschema
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    SHOW,
    backend,
    boxset,
    episode,
    kodi,
    methods,
    movie,
    season,
    series,
    store,
)

SCHEMA = contractschema.load(companion.SCHEMA_PATH)
FLAGGED_STREAMS = [
    {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080},
    {"Type": "Audio", "Codec": "aac", "IsDefault": True},
    {"Type": "Audio", "Codec": "dts"},
    {"Type": "Subtitle", "Language": "eng", "IsForced": True, "IsDefault": True},
    {"Type": "Subtitle", "Language": "eng", "IsHearingImpaired": True},
]


@pytest.fixture(autouse=True)
def filed_by_this_build(store):
    """The library's rows carry their streams, as every library this build
    imports does (metadata.SCANNER_FORMAT)."""
    store.set_scanner_format(LIB, metadata.SCANNER_FORMAT)


def _document(store):
    path = companion.requests_path()
    if not os.path.exists(path):
        return {"requests": []}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _by_kind(store):
    grouped = {}
    for request in _document(store)["requests"]:
        grouped.setdefault(request["kind"], []).append(request)
    return grouped


def _status(status):
    path = companion.status_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"v": 1, "requests": status}, handle)


def _bundle(favorite=True, plot="The season's plot", streams=None):
    return [
        series(SHOW),
        season("s1", SHOW, 1, Overview=plot),
        episode(
            "e1",
            SHOW,
            1,
            1,
            UserData={
                "IsFavorite": favorite,
                "Played": False,
                "PlaybackPositionTicks": 0,
            },
            MediaStreams=streams if streams is not None else FLAGGED_STREAMS,
        ),
        movie("m1", MediaStreams=FLAGGED_STREAMS),
        movie("m2"),
    ]


def test_the_metadata_kinds_are_built_from_the_mappings(store, backend, kodi):
    backend.setup()
    store.publish(_bundle(), library=LIB)
    backend.reconcile()
    document = _document(store)
    contractschema.check_document(document, SCHEMA)
    by_kind = _by_kind(store)
    (tags,) = by_kind[companion.EPISODE_TAGS]
    assert tags["id"] == "episode_tags:e1" and tags["item"] == "e1"
    assert tags["owner"] == {"episodeid": kodi.owned("Episode")["e1"]["episodeid"]}
    assert tags["tags"] == [companion.FAVORITE_EPISODE_TAG]
    (plot,) = by_kind[companion.SEASON_PLOT]
    season_row = next(r for r in kodi.rows["Season"].values() if r["season"] == 1)
    assert plot["owner"] == {"seasonid": season_row["seasonid"]}
    assert plot["plot"] == "The season's plot" and plot["id"] == "season_plot:s1"
    flags = {r["item"]: r for r in by_kind[companion.STREAM_FLAGS]}
    assert sorted(flags) == ["e1", "m1"]  # m2 has no flagged stream
    assert flags["m1"]["media"] == "movie"
    assert flags["m1"]["owner"] == {"movieid": kodi.owned("Movie")["m1"]["movieid"]}
    assert flags["m1"]["flags"] == {"audio": [1, 0], "subtitle": [65, 128]}
    assert flags["e1"]["media"] == "episode"
    assert flags["e1"]["owner"] == {
        "episodeid": kodi.owned("Episode")["e1"]["episodeid"]
    }
    # The mappings carry what the requests were built from.
    assert store.mapping("e1").applied["favorite"] is True
    assert store.mapping("s1").applied["plot"] == "The season's plot"
    assert store.mapping("m1").applied["flags"] == {
        "audio": [1, 0],
        "subtitle": [65, 128],
    }
    assert "flags" not in store.mapping("m2").applied
    assert companion.REMOVE_SET not in by_kind


def test_no_flags_are_asked_for_rows_filed_without_their_streams(store, backend, kodi):
    """A library an older build filed has no stream rows for a flag to land
    on. Asked for anyway, every request was refused on every pass: 5,632 of
    them on one television, until the library was repaired."""
    from kofin.sync.backends.api.store import REBUILDING

    backend.setup()
    store.publish(_bundle(streams=FLAGGED_STREAMS), library=LIB)
    for scanner in (1, REBUILDING):
        store.set_scanner_format(LIB, scanner)
        backend.reconcile()
        by_kind = _by_kind(store)
        assert companion.STREAM_FLAGS not in by_kind
        # What does not need a stream row is asked for as before.
        assert companion.SEASON_PLOT in by_kind and companion.EPISODE_TAGS in by_kind
    store.set_scanner_format(LIB, metadata.SCANNER_FORMAT)
    backend.reconcile()
    assert companion.STREAM_FLAGS in _by_kind(store)


def test_an_acknowledged_kind_leaves_the_document_until_its_content_moves(
    store, backend, kodi
):
    backend.setup()
    store.publish(_bundle(), library=LIB)
    backend.reconcile()
    by_kind = _by_kind(store)
    (plot,) = by_kind[companion.SEASON_PLOT]
    flags = {r["item"]: r for r in by_kind[companion.STREAM_FLAGS]}
    _status(
        {
            plot["id"]: {
                "state": "done",
                "reason": "",
                "hash": companion.content_hash(plot),
            },
            flags["m1"]["id"]: {
                "state": "done",
                "reason": "",
                "hash": companion.content_hash(flags["m1"]),
            },
            flags["e1"]["id"]: {
                "state": "waiting",
                "reason": "scan",
                "hash": companion.content_hash(flags["e1"]),
            },
            "unknown:x": {"state": "done", "reason": "", "hash": "h"},
        }
    )
    store.publish(_bundle(), library=LIB)
    backend.reconcile()
    by_kind = _by_kind(store)
    assert companion.SEASON_PLOT not in by_kind
    assert [r["item"] for r in by_kind[companion.STREAM_FLAGS]] == ["e1"]
    assert store.mapping("s1").applied["companion"] == {
        companion.SEASON_PLOT: companion.content_hash(plot)
    }
    assert store.mapping("m1").applied["companion"] == {
        companion.STREAM_FLAGS: companion.content_hash(flags["m1"])
    }
    # The acknowledgement survives a re-plan of the row, and a moved plot is
    # a new request.
    store.publish(_bundle(plot="A rewritten plot"), library=LIB)
    backend.reconcile()
    by_kind = _by_kind(store)
    (plot,) = by_kind[companion.SEASON_PLOT]
    assert plot["plot"] == "A rewritten plot"
    assert store.mapping("m1").applied["companion"][
        companion.STREAM_FLAGS
    ] == companion.content_hash(flags["m1"])
    assert (
        companion.content_hash(plot)
        != store.mapping("s1").applied["companion"][companion.SEASON_PLOT]
    )


def _done(*requests):
    _status(
        {
            r["id"]: {"state": "done", "reason": "", "hash": companion.content_hash(r)}
            for r in requests
        }
    )


UNFLAGGED_STREAMS = [
    {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080},
    {"Type": "Audio", "Codec": "aac"},
    {"Type": "Audio", "Codec": "dts"},
    {"Type": "Subtitle", "Language": "eng"},
    {"Type": "Subtitle", "Language": "eng"},
]


def _plain_episode():
    # A season needs an importable episode to have a row in Kodi at all.
    return episode("e1", SHOW, 1, 1, MediaStreams=UNFLAGGED_STREAMS)


def _cleared_bundle(**season_values):
    return [
        series(SHOW),
        season("s1", SHOW, 1, Overview="", **season_values),
        _plain_episode(),
        movie("m1", MediaStreams=UNFLAGGED_STREAMS),
        movie("m2", MediaStreams=UNFLAGGED_STREAMS),
    ]


def test_a_cleared_plot_and_cleared_flags_are_asked_for_until_acknowledged(
    store, backend, kodi
):
    """The companion leaves what it wrote when a plot or flags request goes,
    so dropping the key left a plot the server emptied and flags it cleared
    on Kodi's rows. A row that carried either asks for the cleared value
    until exactly that request is acknowledged; a row that never did asks
    for nothing."""
    backend.setup()
    store.publish(
        [
            series(SHOW),
            season("s1", SHOW, 1, Overview="The season's plot"),
            _plain_episode(),
            movie("m1", MediaStreams=FLAGGED_STREAMS),
            movie("m2", MediaStreams=UNFLAGGED_STREAMS),
        ],
        library=LIB,
    )
    backend.reconcile()
    by_kind = _by_kind(store)
    (old_plot,) = by_kind[companion.SEASON_PLOT]
    (old_flags,) = by_kind[companion.STREAM_FLAGS]
    _done(old_plot, old_flags)
    store.publish(_cleared_bundle(), library=LIB)
    backend.reconcile()
    contractschema.check_document(_document(store), SCHEMA)
    by_kind = _by_kind(store)
    (plot,) = by_kind[companion.SEASON_PLOT]
    assert plot["plot"] == "" and plot["id"] == old_plot["id"]
    (flags,) = by_kind[companion.STREAM_FLAGS]  # e1 and m2 never carried a flag
    assert flags["item"] == "m1"
    assert flags["flags"] == {"audio": [0, 0], "subtitle": [0, 0]}
    assert store.mapping("s1").applied["plot"] == ""
    assert store.mapping("m1").applied["flags"] == flags["flags"]
    assert "flags" not in store.mapping("m2").applied
    assert "flags" not in store.mapping("e1").applied
    # The acknowledgement of the request before the clear is not this one's.
    store.publish(_cleared_bundle(Name="Season One"), library=LIB)
    backend.reconcile()
    assert companion.SEASON_PLOT in _by_kind(store)
    assert store.mapping("s1").applied["plot"] == ""
    # Acknowledged: the requests leave, and the keys go at the next plan.
    _done(plot, flags)
    store.publish([], library=LIB)
    backend.reconcile()
    by_kind = _by_kind(store)
    assert companion.SEASON_PLOT not in by_kind
    assert companion.STREAM_FLAGS not in by_kind
    bundle = _cleared_bundle(Name="Season 1")
    bundle[3] = movie("m1", MediaStreams=UNFLAGGED_STREAMS, Overview="moved")
    store.publish(bundle, library=LIB)
    backend.reconcile()
    assert "plot" not in store.mapping("s1").applied
    assert "flags" not in store.mapping("m1").applied
    by_kind = _by_kind(store)
    assert companion.SEASON_PLOT not in by_kind
    assert companion.STREAM_FLAGS not in by_kind


def test_an_acknowledgement_lands_in_the_pass_that_plans_its_row(store, backend, kodi):
    """The requests are built from the stored mappings and the companion's
    acknowledgements are written onto them, so every acknowledgement of the
    pass is committed before either: a row planned in the same pass keeps
    the hash the status reported."""
    backend.setup()
    store.publish(_bundle(), library=LIB)
    backend.reconcile()
    flags = {r["item"]: r for r in _by_kind(store)[companion.STREAM_FLAGS]}
    _done(flags["m1"])
    bundle = _bundle()
    bundle[3] = movie("m1", MediaStreams=FLAGGED_STREAMS, Overview="moved")
    store.publish(bundle, library=LIB)
    backend.reconcile()
    assert store.mapping("m1").applied["companion"] == {
        companion.STREAM_FLAGS: companion.content_hash(flags["m1"])
    }
    assert [r["item"] for r in _by_kind(store)[companion.STREAM_FLAGS]] == ["e1"]


def test_a_favourite_withdrawn_takes_its_request_with_it(store, backend, kodi):
    backend.setup()
    store.publish(_bundle(), library=LIB)
    backend.reconcile()
    assert companion.EPISODE_TAGS in _by_kind(store)
    store.publish(_bundle(favorite=False), library=LIB)
    backend.reconcile()
    assert companion.EPISODE_TAGS not in _by_kind(store)
    assert "favorite" not in store.mapping("e1").applied


def test_an_emptied_set_goes_to_the_companion_when_it_is_present(store, backend, kodi):
    kodi.addons[companion.ADDON_ID] = "0.1.0"
    backend.setup()
    store.publish([movie(), movie("b"), boxset("box", "Pair", ["a", "b"])], library=LIB)
    backend.reconcile()
    kodi.calls.clear()
    store.publish([], removed=["box"])
    backend.reconcile()
    assert not store.pending()
    assert kodi.owned("Movie")["a"]["set"] == ""
    # No clean of the root: the row stays for the companion, which is asked by title.
    assert methods(kodi, "VideoLibrary.Clean") == []
    assert [r["title"] for r in kodi.rows["BoxSet"].values()] == ["Pair"]
    (request,) = _by_kind(store)[companion.REMOVE_SET]
    assert request["title"] == "Pair" and request[
        "id"
    ] == "remove_set:" + companion.set_key("Pair")
    assert os.path.exists(companion.sets_journal_path())
    # Done, says the status: the journal entry and the request go.
    _status(
        {
            request["id"]: {
                "state": "done",
                "reason": "",
                "hash": companion.content_hash(request),
            }
        }
    )
    store.publish([movie(Overview="again")], library=LIB)
    backend.reconcile()
    assert companion.REMOVE_SET not in _by_kind(store)
    assert not os.path.exists(companion.sets_journal_path())


def test_a_done_for_an_earlier_removal_does_not_close_a_later_one(
    store, backend, kodi, monkeypatch
):
    """The request id of a set is its title's, so the same title emptied
    again has the id of the removal before it, and the status keeps that
    removal's ``done`` until the companion rewrites it. The journal entry
    goes only for the hash of the request it makes, which carries the
    entry's stamp."""
    clock = {"now": 1_800_000_000}
    monkeypatch.setattr(companion.time, "time", lambda: clock["now"])
    backend.setup()
    store.publish([movie()], library=LIB)
    backend.reconcile()
    companion.journal_set("Pair")
    backend.reconcile()
    (first,) = _by_kind(store)[companion.REMOVE_SET]
    assert first["since"] == 1_800_000_000
    _done(first)
    backend.reconcile()
    assert companion.REMOVE_SET not in _by_kind(store)
    # The same title, emptied again later; the status still says done.
    clock["now"] += 3600
    companion.journal_set("Pair")
    backend.reconcile()
    (second,) = _by_kind(store)[companion.REMOVE_SET]
    assert second["id"] == first["id"] and second["since"] == first["since"] + 3600
    assert os.path.exists(companion.sets_journal_path())
    _done(second)
    backend.reconcile()
    assert companion.REMOVE_SET not in _by_kind(store)
    assert not os.path.exists(companion.sets_journal_path())


def test_without_the_companion_the_root_is_cleaned_as_before(store, backend, kodi):
    backend.setup()
    store.publish([movie(), boxset("box", "Pair", ["a"])], library=LIB)
    backend.reconcile()
    store.publish([], removed=["box"])
    backend.reconcile()
    assert methods(kodi, "VideoLibrary.Clean") == [
        {"showdialogs": False, "directory": paths.root(store.namespace)}
    ]
    assert kodi.rows["BoxSet"] == {}
    assert companion.REMOVE_SET not in _by_kind(store)
