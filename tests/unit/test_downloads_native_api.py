"""Downloads and Kodi's library: nothing of Kodi's moves; the pass carries
the badge and the tag, and the play state is read back by id."""

import pytest

from kofin.downloads import EPISODE_TAG, TAG, nativeport, store as downloads
from kofin.downloads.store import Download
from kofin.sync.backends.api import downloaded, identity, native
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    backend,
    episode,
    kodi,
    methods,
    movie,
    piers,
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
    assert (done, series) == ({"m", "e"}, {"show": {"e"}})
    assert downloaded.flag("Movie", "m", done, series) == "downloaded"
    assert downloaded.flag("Episode", "e", done, series) == "downloaded"
    assert downloaded.flag("Series", "show", done, series) == "downloaded"
    assert downloaded.flag("Movie", "q", done, series) == ""
    # A season is marked on the caller's word: nothing here names one.
    assert downloaded.flag("Season", "s", done, series) == ""
    assert downloaded.flag("Season", "s", done, series, season=True) == "downloaded"
    desired = {"art": {"poster": "p"}, "tag": ["kofin.library.x"]}
    downloaded.apply("Movie", desired, "downloaded")
    assert desired["art"][downloaded.BADGE_ART] == downloaded.BADGE_URL
    assert desired["tag"] == sorted(["kofin.library.x", TAG])
    # A season has the badge and no tag: Kodi keeps none for a season.
    season_row = {"art": {}}
    downloaded.apply("Season", season_row, "downloaded")
    assert season_row == {"art": {downloaded.BADGE_ART: downloaded.BADGE_URL}}
    # An episode takes a tag its show never carries, and only where its
    # desired state has tags (a Kodi that keeps an episode's tags).
    episode_row = {"art": {}, "tag": ["kofin.library.x"]}
    downloaded.apply("Episode", episode_row, "downloaded")
    assert episode_row["tag"] == sorted(["kofin.library.x", EPISODE_TAG])
    untagged = {"art": {}}
    downloaded.apply("Episode", untagged, "downloaded")
    assert "tag" not in untagged
    plain = {"art": {}, "tag": []}
    downloaded.apply("Movie", plain, "")
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
    assert port.attached(row, "/dl") is True
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
    assert TAG not in kodi_row["tag"]
    assert not store.pending()


def downloaded_episode(item_id="ea11"):
    from tests.unit.apifixtures import SHOW

    downloads.queue(Download(jellyfin_id=item_id, media_type="episode", series_id=SHOW))
    downloads.finish(item_id, "Shows/x/%s.mkv" % item_id, "mkv", 1)
    return downloads.get(item_id)


def badged(kodi):
    """The rows carrying the badge: episodes and shows by item id, seasons
    by number."""
    found = {
        kind: sorted(
            item_id
            for item_id, row in kodi.owned(kind).items()
            if downloaded.BADGE_ART in row["art"]
        )
        for kind in ("Episode", "Series")
    }
    found["Season"] = sorted(
        row["season"]
        for row in kodi.rows["Season"].values()
        if downloaded.BADGE_ART in row["art"]
    )
    return found


def test_an_episode_s_download_badges_its_season_and_its_show(store, backend, kodi):
    """A skin marks the show and the season on the way down to the file. The
    season is found by the episode's number, which is how Kodi files it; the
    show's other season stays plain."""
    from tests.unit.apifixtures import SHOW, show_bundle

    backend.setup()
    store.publish(
        show_bundle(episodes=2, seasons=(1, 2))
        + [episode("late", season_number=2, number=1)],
        library=LIB,
    )
    backend.reconcile()
    assert not store.pending()
    kodi.calls.clear()
    row = downloaded_episode()
    port = nativeport.NativeApi()
    port.attached(row, "/dl")
    backend.reconcile()
    assert not store.pending()
    assert badged(kodi) == {"Episode": ["ea11"], "Season": [1], "Series": [SHOW]}
    (show,) = kodi.rows["Series"].values()
    assert TAG in show["tag"] and EPISODE_TAG not in show["tag"]
    assert tagged_episodes(kodi) == ["ea11"]
    assert len(methods(kodi, "VideoLibrary.SetSeasonDetails")) == 1
    # The download goes: all three badges go with it, and both tags.
    downloads.remove("ea11")
    port.detached(row)
    backend.reconcile()
    assert not store.pending()
    assert badged(kodi) == {"Episode": [], "Season": [], "Series": []}
    (show,) = kodi.rows["Series"].values()
    assert TAG not in show["tag"]
    assert tagged_episodes(kodi) == []


def tagged_episodes(kodi):
    return sorted(
        item_id
        for item_id, row in kodi.owned("Episode").items()
        if EPISODE_TAG in row.get("tag", [])
    )


def test_a_piers_badges_a_downloaded_episode_without_tagging_it(store, piers, backend):
    """Piers keeps no tags for an episode, so the episode has the badge
    alone, and its show the badge and the tag."""
    from tests.unit.apifixtures import show_bundle

    backend.setup()
    store.publish(show_bundle(episodes=2), library=LIB)
    nativeport.NativeApi().attached(downloaded_episode(), "/dl")
    backend.reconcile()
    assert not store.pending()
    assert badged(piers)["Episode"] == ["ea11"]
    assert tagged_episodes(piers) == []
    (show,) = piers.rows["Series"].values()
    assert TAG in show["tag"]
    assert all("tag" not in p for p in methods(piers, "VideoLibrary.SetEpisodeDetails"))


def test_an_episode_acknowledged_before_it_carried_its_tag_is_built_again(
    store, backend, kodi, monkeypatch
):
    """A downloaded episode acknowledged when the mark meant the badge alone
    has the same mark as one acknowledged by this serializer: only
    metadata.SERIALIZER, which the inputs token carries, tells them apart."""
    from kofin.sync.backends.api import metadata
    from tests.unit.apifixtures import show_bundle

    backend.setup()
    store.publish(show_bundle(episodes=1), library=LIB)
    row = downloaded_episode()
    backend.reconcile()
    assert tagged_episodes(kodi) == ["ea11"]
    # The row and its acknowledgement as the serializer before left them.
    kodi.owned("Episode")["ea11"]["tag"].remove(EPISODE_TAG)
    mapping = store.mapping("ea11")
    applied = dict(mapping.applied)
    applied["owned"] = dict(
        applied["owned"],
        tag=[t for t in applied["owned"]["tag"] if t != EPISODE_TAG],
    )
    record = store.records(kind="Episode")["ea11"]
    with monkeypatch.context() as earlier:
        earlier.setattr(metadata, "SERIALIZER", 4)
        applied["inputs"] = metadata.inputs_token(
            backend.server_url(),
            store.namespace,
            record.library,
            backend.separator,
            downloaded="downloaded",
            features=backend.features,
        )
    assert applied["inputs"] != mapping.applied["inputs"]
    store.remember(
        "ea11", store.state("ea11").desired, mapping.kodi_id, applied, "Episode"
    )
    # The service start hands every done file to the pass.
    nativeport.NativeApi().attached(row, "/dl")
    backend.reconcile()
    assert tagged_episodes(kodi) == ["ea11"]


def test_a_show_acknowledged_before_it_carried_a_badge_is_built_again(
    store, backend, kodi, monkeypatch
):
    """A show with a download acknowledged when the mark meant the tag alone
    has the same mark as one acknowledged by this serializer: only
    metadata.SERIALIZER, which the inputs token carries, tells them apart."""
    from kofin.sync.backends.api import metadata
    from tests.unit.apifixtures import SHOW, show_bundle

    backend.setup()
    store.publish(show_bundle(episodes=1), library=LIB)
    row = downloaded_episode()
    backend.reconcile()
    assert badged(kodi)["Series"] == [SHOW]
    # The row and its acknowledgement as the serializer before left them.
    (show,) = kodi.rows["Series"].values()
    del show["art"][downloaded.BADGE_ART]
    mapping = store.mapping(SHOW)
    applied = dict(mapping.applied)
    applied["owned"] = dict(
        applied["owned"],
        art=[k for k in applied["owned"]["art"] if k != downloaded.BADGE_ART],
    )
    seasons = [r.item for r in store.records(kind="Season", parent_id=SHOW).values()]
    with monkeypatch.context() as earlier:
        earlier.setattr(metadata, "SERIALIZER", 2)
        applied["inputs"] = metadata.inputs_token(
            backend.server_url(),
            store.namespace,
            LIB,
            backend.separator,
            seasons,
            None,
            downloaded="downloaded",
        )
    assert applied["inputs"] != mapping.applied["inputs"]
    store.remember(SHOW, store.state(SHOW).desired, mapping.kodi_id, applied, "Series")
    # The service start hands every done file to the pass.
    nativeport.NativeApi().attached(row, "/dl")
    backend.reconcile()
    assert badged(kodi)["Series"] == [SHOW]


def scans(kodi):
    return [p["directory"] for p in methods(kodi, "VideoLibrary.Scan")]


def test_a_badge_that_moved_ends_the_pass_with_one_library_event(store, backend, kodi):
    """Kodi announces nothing for a setter that changes only art or tags,
    and a home widget reloads on an announcement alone, so a pass that moved
    a badge ends with one scan of a directory that is not there, after the
    last setter, however many rows the download marked."""
    from tests.unit.apifixtures import show_bundle

    backend.setup()
    store.publish(show_bundle(episodes=2) + [movie()], library=LIB)
    backend.reconcile()
    kodi.calls.clear()
    # A patch that moves no badge is not announced for.
    store.publish([movie(Overview="Edited")])
    backend.reconcile()
    assert len(methods(kodi, "VideoLibrary.SetMovieDetails")) == 1
    assert scans(kodi) == []
    # The episode, its season and its show: three setters, one event, and
    # the event after the last of them.
    row = downloaded_episode()
    port = nativeport.NativeApi()
    port.attached(row, "/dl")
    kodi.calls.clear()
    backend.reconcile()
    called = [method for method, _ in kodi.calls]
    setters = [i for i, m in enumerate(called) if m.endswith("Details") and ".Set" in m]
    assert len(setters) == 3
    assert scans(kodi) == [native.NOWHERE]
    assert called.index("VideoLibrary.Scan") > max(setters)
    assert methods(kodi, "VideoLibrary.Scan")[0]["showdialogs"] is False
    # A pass that finds the badges where it left them announces nothing.
    kodi.calls.clear()
    port.attached(row, "/dl")
    backend.reconcile()
    assert scans(kodi) == []
    # Taken off again: one event more.
    downloads.remove("ea11")
    port.detached(row)
    backend.reconcile()
    assert scans(kodi) == [native.NOWHERE]
    assert not store.pending()


def test_a_library_event_kodi_never_ends_does_not_fail_the_pass(
    store, backend, kodi, monkeypatch
):
    backend.setup()
    store.publish([movie()], library=LIB)
    backend.reconcile()
    row = downloaded_movie()
    nativeport.NativeApi().attached(row, "/dl")

    def never(*_args, **_kwargs):
        raise TimeoutError("no announcement")

    monkeypatch.setattr(backend, "announce_writes", never)
    backend.reconcile()
    assert not store.pending()
    (kodi_row,) = kodi.rows["Movie"].values()
    assert kodi_row["art"][downloaded.BADGE_ART] == downloaded.BADGE_URL


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


def test_a_season_s_download_menu_finds_its_episodes_through_the_catalogue(
    store, monkeypatch
):
    """The catalogue files an episode under its series, so a season is
    answered by the series' downloads whose payload names the season."""
    from tests.unit.apifixtures import SHOW, episode, season, series

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
    # Another profile's catalogue, in the same database, files e2 under s1:
    # the lookup reads the active namespace alone.
    from kofin.sync.backends.api.store import Store, namespace

    other = Store(namespace("other-server", "user"))
    other.initialize("http://other")
    other.publish(
        [
            series(),
            season("s1", number=1),
            episode("e2", season_number=1, SeasonId="s1"),
        ],
        library=LIB,
    )
    assert downloads.container_states("s1") == {"e1": downloads.DONE}
    assert downloads.container_states("s2") == {"e2": downloads.DONE}
    assert set(downloads.container_states(SHOW)) == {"e1", "e2"}


def test_the_downloaded_music_view_lists_the_finished_songs(
    store, backend, kodi, tmp_path, monkeypatch
):
    """A download keeps its row's plugin URL, so no smart playlist rule on
    the downloads folder can find it. The view is a playlist of the finished
    songs in album order, written by the port, and a smart playlist left in
    the folder is removed."""
    from kofin.sync import playlists
    from kofin.sync.backends.api import playlists as api_playlists
    from tests.unit.apifixtures import LIB2, album_bundle

    backend.setup()
    store.publish(album_bundle(songs=3), library=LIB2)
    backend.reconcile()
    root = tmp_path / "Kofin"
    root.mkdir()
    (root / playlists.DOWNLOADED_MUSIC_XSP).write_text("<smartplaylist/>")
    monkeypatch.setattr(playlists, "managed_dir", lambda root_=None: str(root))
    for item_id in ("tc33", "tc31"):
        downloads.queue(Download(jellyfin_id=item_id, media_type="song"))
        downloads.finish(item_id, "Music/Band/%s.flac" % item_id, "flac", 1)
    downloads.queue(Download(jellyfin_id="tc32", media_type="song"))
    nativeport.NativeApi().refresh_song_playlists({"tc31"})
    text = (root / playlists.DOWNLOADED_MUSIC_M3U8).read_text()
    resolver = api_playlists.Resolver(store)
    assert [line for line in text.splitlines() if line.startswith("plugin://")] == [
        resolver.entry(store.item(item_id), "Audio").path
        for item_id in ("tc31", "tc33")
    ]
    assert not (root / playlists.DOWNLOADED_MUSIC_XSP).exists()
    # A removal rewrites it.
    downloads.remove("tc33")
    nativeport.NativeApi().music_view(True)
    text = (root / playlists.DOWNLOADED_MUSIC_M3U8).read_text()
    assert [line for line in text.splitlines() if line.startswith("plugin://")] == [
        resolver.entry(store.item("tc31"), "Audio").path
    ]
