"""Downloads in the API build: nothing of Kodi's moves; the pass carries the
badge and the tag, and the play state is read back by id."""

import pytest

from kofin.downloads import TAG, nativeport, store as downloads
from kofin.downloads.store import Download
from kofin.sync.backends.api import downloaded, identity
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    backend,
    kodi,
    methods,
    movie,
    store,
)


@pytest.fixture(autouse=True)
def this_store(store, monkeypatch):
    # The port finds the catalogue through the logged-in credentials; here
    # it is the fixture's.
    monkeypatch.setattr(identity, "current_store", lambda: store)
    return store


def downloaded_movie(item_id="a"):
    downloads.queue(Download(jellyfin_id=item_id, media_type="movie"))
    downloads.finish(item_id, "Movies/Fixture/Fixture.mkv", "mkv", 1234)
    return downloads.get(item_id)


def test_marks_name_downloaded_items_and_the_shows_of_downloaded_episodes(store):
    downloads.queue(Download(jellyfin_id="m", media_type="movie"))
    downloads.finish("m", "Movies/m.mkv", "mkv", 1)
    downloads.queue(Download(jellyfin_id="e", media_type="episode", series_id="show"))
    downloads.finish("e", "Shows/s/e.mkv", "mkv", 1)
    downloads.queue(Download(jellyfin_id="q", media_type="movie"))  # still queued
    done, series = downloaded.marks()
    assert (done, series) == ({"m", "e"}, {"show"})
    assert downloaded.flag("Movie", "m", done, series) == "downloaded"
    assert downloaded.flag("Episode", "e", done, series) == "downloaded"
    assert downloaded.flag("Series", "show", done, series) == "downloaded"
    assert downloaded.flag("Movie", "q", done, series) == ""
    desired = {"art": {"poster": "p"}, "tag": ["kofin.library.x"]}
    downloaded.apply("Movie", "m", desired, done, series)
    assert desired["art"][downloaded.BADGE_ART] == downloaded.BADGE_URL
    assert desired["tag"] == sorted(["kofin.library.x", TAG])
    plain = {"art": {}, "tag": []}
    downloaded.apply("Movie", "q", plain, done, series)
    assert plain == {"art": {}, "tag": []}


def test_a_download_puts_the_badge_and_tag_on_the_row_and_its_removal_takes_them(
    store, backend, kodi
):
    backend.setup()
    store.publish([movie()], library=LIB)
    backend.reconcile()
    kodi.calls.clear()
    port = nativeport.NativeApi()
    row = downloaded_movie()
    # Attached: the pass is handed the item, and always has work.
    assert port.attached(row, "/dl") is True and port.restore(row, "/dl") is True
    backend.reconcile()
    (kodi_row,) = kodi.rows["Movie"].values()
    assert kodi_row["art"][downloaded.BADGE_ART] == downloaded.BADGE_URL
    assert TAG in kodi_row["tag"]
    patches = methods(kodi, "VideoLibrary.SetMovieDetails")
    assert len(patches) == 1 and patches[0]["art"][downloaded.BADGE_ART]
    assert store.mapping("a").applied["owned"]["art"].count(downloaded.BADGE_ART) == 1
    # Nothing else changed: the next pass sends nothing.
    kodi.calls.clear()
    store.publish([movie()], library=LIB)
    backend.reconcile()
    assert not methods(kodi, "VideoLibrary.SetMovieDetails")
    # The download goes: the badge and the tag go with it, nothing else does.
    downloads.remove("a")
    port.detached(row)
    backend.reconcile()
    (kodi_row,) = kodi.rows["Movie"].values()
    assert downloaded.BADGE_ART not in kodi_row["art"]
    assert TAG not in kodi_row["tag"] and "kofin.library." + LIB in kodi_row["tag"]
    assert not store.pending()


def test_the_play_state_is_read_back_by_the_mapped_id(store, backend, kodi):
    backend.setup()
    store.publish([movie()], library=LIB)
    backend.reconcile()
    row = downloaded_movie()
    port = nativeport.NativeApi()
    (kodi_row,) = kodi.rows["Movie"].values()
    # The fixture's row carries the server's resume point and no play yet.
    assert port.watched_locally(row) is False
    assert port.last_touch(row) == (0.0, True)
    kodi_row["playcount"] = 1
    kodi_row["lastplayed"] = "2026-01-02 03:04:05"
    kodi_row["resume"] = {"position": 0.0, "total": 120.0}
    assert port.watched_locally(row) is True
    stamp, resuming = port.last_touch(row)
    assert (
        stamp == nativeport._as_epoch("2026-01-02 03:04:05") > 0 and resuming is False
    )
    # An item Kodi never filed has no state to read.
    assert port.last_touch(Download(jellyfin_id="zz", media_type="movie")) is None
    assert port.mapped("a") is True and port.mapped("zz") is False


def test_the_sql_port_is_the_repoint_module(monkeypatch):
    from kofin.downloads import repoint

    calls = []
    monkeypatch.setattr(
        repoint, "restore", lambda row, root: calls.append(("restore", root)) or True
    )
    monkeypatch.setattr(
        repoint, "repoint", lambda row, root: calls.append(("repoint", root)) or False
    )
    monkeypatch.setattr(
        repoint, "stamp_tag", lambda row: calls.append(("tag", row.jellyfin_id))
    )
    monkeypatch.setattr(
        repoint, "stamp_badge", lambda row: calls.append(("badge", row.jellyfin_id))
    )
    port = nativeport.NativeSql()
    row = Download(jellyfin_id="a", media_type="movie")
    assert port.restore(row, "/dl") is True
    # The stamps go on whether or not the row moved.
    assert port.attached(row, "/dl") is False
    assert calls == [
        ("restore", "/dl"),
        ("repoint", "/dl"),
        ("tag", "a"),
        ("badge", "a"),
    ]


def test_a_season_s_download_menu_finds_its_episodes_through_the_catalogue(
    store, monkeypatch
):
    """The catalogue files an episode under its series, so a season is
    answered by the series' downloads whose payload names the season."""
    from kofin import buildconfig
    from tests.unit.apifixtures import SHOW, episode, season, series

    monkeypatch.setattr(buildconfig, "BACKEND", "api")
    store.publish(
        [
            series(),
            season("s1", number=1),
            season("s2", number=2),
            episode("e1", season_number=1, SeasonId="s1"),
            episode("e2", season_number=2, SeasonId="s2"),
        ],
        library=LIB,
    )
    for item_id in ("e1", "e2"):
        downloads.queue(
            Download(jellyfin_id=item_id, media_type="episode", series_id=SHOW)
        )
        downloads.finish(item_id, "Shows/x/%s.mkv" % item_id, "mkv", 1)
    assert downloads.container_states("s1") == {"e1": downloads.DONE}
    assert downloads.container_states("s2") == {"e2": downloads.DONE}
    assert set(downloads.container_states(SHOW)) == {"e1", "e2"}
