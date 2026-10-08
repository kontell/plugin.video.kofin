"""Public video lifecycle and recovery, with no native database connection."""

import copy
import json
import time
from types import SimpleNamespace

import pytest

from kofin.sync import private
from kofin.sync.backends.api import metadata, native, paths
from kofin.sync.backends.api.library import Library, fetch_kind
from kofin.sync.backends.api.native import Native
from kofin.sync.backends.api.store import Store, namespace
from kofin.sync.catalogue import BackendMismatch
from tests.unit.apikodi import SERVER, Kodi

LIB = "1" * 32
LIB2 = "2" * 32
SHOW = "a1" * 16
SHOW2 = "b2" * 16


def movie(item_id="a", **values):
    item = dict(
        Id=item_id,
        Type="Movie",
        Name="Fixture " + item_id,
        Overview="Original",
        RunTimeTicks=1200000000,
        UserData={"Played": False, "PlaybackPositionTicks": 120000000},
        MediaStreams=[
            {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080}
        ],
        People=[{"Name": "Fixture actor", "Type": "Actor", "Role": "Original"}],
    )
    item.update(values)
    return item


def series(item_id=SHOW, **values):
    item = dict(
        Id=item_id,
        Type="Series",
        Name="Show " + item_id[:2],
        Overview="About the show",
        PremiereDate="2001-02-03T00:00:00Z",
        Status="Ended",
        Genres=["Drama"],
        People=[{"Name": "Lead", "Type": "Actor", "Role": "Self"}],
        ProviderIds={"Tvdb": "77"},
        UserData={"IsFavorite": True},
    )
    item.update(values)
    return item


def season(item_id, series_id=SHOW, number=1, **values):
    item = dict(
        Id=item_id,
        Type="Season",
        Name="Season %d" % number,
        IndexNumber=number,
        SeriesId=series_id,
        Overview="",
    )
    item.update(values)
    return item


def episode(item_id, series_id=SHOW, season_number=1, number=1, **values):
    item = dict(
        Id=item_id,
        Type="Episode",
        Name="Episode " + item_id,
        SeriesId=series_id,
        SeriesName="Show",
        ParentIndexNumber=season_number,
        IndexNumber=number,
        Overview="Plot",
        RunTimeTicks=600000000,
        PremiereDate="2001-02-10T00:00:00Z",
        UserData={"Played": False, "PlaybackPositionTicks": 0},
        MediaStreams=[{"Type": "Video", "Codec": "h264"}],
        People=[{"Name": "Guest", "Type": "GuestStar"}],
    )
    item.update(values)
    return item


def musicvideo(item_id="m", **values):
    item = dict(
        Id=item_id,
        Type="MusicVideo",
        Name="Clip " + item_id,
        Artists=["Band"],
        Album="Album",
        RunTimeTicks=2000000000,
        UserData={"Played": False, "PlaybackPositionTicks": 0},
        MediaStreams=[{"Type": "Video", "Codec": "h264"}],
        People=[],
    )
    item.update(values)
    return item


def boxset(item_id, name, members, **values):
    item = dict(Id=item_id, Type="BoxSet", Name=name, KofinMembers=sorted(members))
    item.update(values)
    return item


def show_bundle(series_id=SHOW, episodes=2, seasons=(1,), prefix="e"):
    items = [series(series_id)]
    for number in seasons:
        items.append(season(series_id[:4] + "s%d" % number, series_id, number))
    for index in range(episodes):
        items.append(
            episode(
                "%s%s%d" % (prefix, series_id[:2], index + 1),
                series_id,
                seasons[0],
                index + 1,
            )
        )
    return items


@pytest.fixture
def store(tmp_path, monkeypatch):
    private.reset_overrides()
    private.set_path_override("kofin", str(tmp_path / "kofin.db"))
    monkeypatch.setattr(private, "addon_data_path", lambda: str(tmp_path))
    store = Store(namespace("server", "user"))
    store.initialize(SERVER)
    yield store
    private.reset_overrides()


@pytest.fixture
def kodi(store, monkeypatch):
    fake = Kodi(store)
    monkeypatch.setattr(native, "rpc", fake.rpc)
    monkeypatch.setattr(native, "rpc_batch", fake.batch)
    monkeypatch.setattr(native.xbmc, "getCondVisibility", lambda _: False)
    monkeypatch.setattr(
        native,
        "Monitor",
        lambda: SimpleNamespace(finished=0, waitForAbort=lambda _: False),
    )
    fake.builtins = []
    monkeypatch.setattr(native.xbmc, "executebuiltin", fake.builtins.append)
    return fake


@pytest.fixture
def backend(store, kodi):
    backend = Native(store)
    kodi.monitor = backend.monitor
    return backend


def methods(kodi, name):
    return [p for m, p in kodi.calls if m == name]


# -- store ---------------------------------------------------------------------


def test_publication_pin_tombstone_restart_and_old_ack(store):
    store.publish([movie()], library=LIB)
    first = store.pin()
    store.publish([movie(Overview="New")], library=LIB)
    assert store.pin() == first
    assert store.records(pinned=False)["a"].item["Overview"] == "New"
    assert store.records()["a"].generation == 2
    assert not store.remember("a", 1, 10, {})
    reopened = Store(store.namespace)
    assert reopened.state("a").applied == 0
    reopened.unpin()
    assert reopened.records()["a"].generation == 2
    reopened.publish([], library=LIB)
    assert reopened.records() == {}
    assert reopened.state("a").operation == "remove"
    assert reopened.tombstones()["a"].library == LIB
    assert len(reopened.pending()) == 1
    assert reopened.forget("a", 3)
    assert json.loads(reopened.item("a") or "null") is None
    assert not reopened.pending()


def test_invalid_publication_is_atomic(store):
    store.publish([movie()], library=LIB)
    before = store.records(), store.generation()
    with pytest.raises(ValueError):
        store.publish([movie("b"), movie("c", Type="Photo")], library=LIB)
    with pytest.raises(ValueError):
        store.publish([movie("b")])
    with pytest.raises(ValueError):
        store.publish([episode("e1", SeriesId=None)], library=LIB)
    assert (store.records(), store.generation()) == before
    assert store.state("b") is None


def test_closed_intervals_are_collected_once_unreachable(store):
    store.publish([movie(), movie("b")], library=LIB)
    pinned = store.pin()
    store.publish([movie()], library=LIB)
    assert set(store.records()) == {"a", "b"}
    assert set(store.records(pinned=False)) == {"a"}
    with private.Database() as db:
        rows = db.cursor.execute("SELECT COUNT(*) FROM api_entry").fetchone()[0]
    assert rows == 2
    store.unpin()
    store.publish([movie(Overview="x")], library=LIB)
    with private.Database() as db:
        rows = db.cursor.execute("SELECT COUNT(*) FROM api_entry").fetchone()[0]
    # The pending removal keeps its last placement until it is applied.
    assert rows == 2
    assert store.tombstones()["b"].library == LIB
    assert store.forget("b", 2)
    with private.Database() as db:
        rows = db.cursor.execute("SELECT COUNT(*) FROM api_entry").fetchone()[0]
    assert rows == 1
    assert pinned == 1


def test_library_move_is_pending_without_metadata_change(store):
    store.publish([movie()], library=LIB)
    store.remember("a", 1, 7, {})
    store.publish([movie()], library=LIB2)
    assert store.state("a").desired == 2 and store.state("a").applied == 1
    assert store.entry("a").library == LIB2
    assert store.publish([movie()], library=LIB2) == store.generation()


def test_legacy_store_is_detected_and_retired(store):
    with private.Database() as db:
        db.cursor.execute("CREATE TABLE api_movie(namespace TEXT, item_id TEXT)")
        db.cursor.execute("CREATE TABLE api_snapshot(namespace TEXT, payload TEXT)")
    assert store.legacy_present()
    store.publish([movie()], library=LIB)
    store.retire_legacy()
    assert not store.legacy_present()
    assert store.records(pinned=False) == {}
    assert store.state("a") is None


# -- metadata ------------------------------------------------------------------


@pytest.mark.parametrize(
    "values, expected",
    [
        ({"ParentIndexNumber": 1, "IndexNumber": 3}, (1, 3)),
        ({"ParentIndexNumber": 0, "IndexNumber": 2}, (0, 2)),
        ({"ParentIndexNumber": 2, "IndexNumber": 0}, (2, 0)),
        ({"ParentIndexNumber": 2, "IndexNumber": None}, (2, 0)),
        ({"ParentIndexNumber": 0, "IndexNumber": 0}, None),
        ({"ParentIndexNumber": None, "IndexNumber": None}, None),
        (
            {"ParentIndexNumber": None, "IndexNumber": 4, "AbsoluteEpisodeNumber": 9},
            (1, 9),
        ),
    ],
)
def test_episode_numbering_follows_the_scanner_rules(values, expected):
    numbers = metadata.episode_numbers(episode("e", **values))
    if expected is None:
        assert numbers is None
    else:
        assert (numbers["season"], numbers["episode"]) == expected


def test_specials_sort_before_or_after_their_season():
    before = metadata.episode_numbers(
        episode(
            "e",
            season_number=0,
            number=1,
            AirsBeforeSeasonNumber=2,
            AirsBeforeEpisodeNumber=5,
        )
    )
    assert (before["sortseason"], before["sortepisode"]) == (2, 5)
    after = metadata.episode_numbers(
        episode("e", season_number=0, number=1, AirsAfterSeasonNumber=1)
    )
    assert (after["sortseason"], after["sortepisode"]) == (1, 4096)


def test_default_season_names_are_left_to_kodi():
    assert metadata.season_title(season("s", number=3)) == ""
    assert metadata.season_title(season("s", number=0, Name="Specials")) == ""
    assert metadata.season_title(season("s", number=1, Name="Book One")) == "Book One"


def test_show_hash_moves_for_membership_and_numbering_only():
    episodes = [episode("e1"), episode("e2", number=2)]
    base = metadata.show_hash(episodes)
    assert metadata.show_hash([dict(e, Overview="edited") for e in episodes]) == base
    assert metadata.show_hash(episodes[:1]) != base
    assert metadata.show_hash([episodes[0], dict(episodes[1], IndexNumber=3)]) != base
    assert metadata.hash_date(base).endswith("T00:00:00Z")


def test_compact_drops_blur_hashes_and_keeps_one_stream_list():
    item = movie(
        ImageBlurHashes={"Primary": {"x": "y"}},
        People=[{"Name": "A", "Type": "Actor", "ImageBlurHashes": {"Primary": {}}}],
        MediaSources=[
            {"Id": "s", "MediaStreams": [{"Type": "Video", "Codec": "hevc"}]}
        ],
    )
    del item["MediaStreams"]
    compact = metadata.compact(item)
    assert "ImageBlurHashes" not in compact
    assert "ImageBlurHashes" not in compact["People"][0]
    assert compact["MediaStreams"] == [{"Type": "Video", "Codec": "hevc"}]
    assert "MediaStreams" not in compact["MediaSources"][0]
    assert metadata.refresh_token(compact) == metadata.refresh_token(
        metadata.compact(copy.deepcopy(item))
    )


def test_native_metadata_normalizes_tags_studios_and_release_year():
    item = movie(
        Name=" Fixture title ",
        SortName=" Fixture sort \t",
        Tags=[" Tag ", "Tag", " "],
        Studios=[{"Name": " Studio one / Studio two "}],
        ProductionYear=1999,
        PremiereDate="2000-01-02T00:00:00Z",
    )
    result = metadata.details(item, "", "key", LIB)
    assert result["title"] == "Fixture title"
    assert result["sorttitle"] == "Fixture sort"
    assert result["tag"] == ["Tag", "kofin.library." + LIB]
    assert result["studio"] == ["Studio one", "Studio two"]
    assert result["year"] == 2000
    result = metadata.details(item, "", "key", LIB, separator=" | ")
    assert result["studio"] == ["Studio one / Studio two"]


def test_show_and_episode_details_speak_their_setters():
    show = metadata.details(
        series(), SERVER, "key", LIB, seasons=[season("s", number=1, Overview="arc")]
    )
    assert show["status"] == "Ended" and show["premiered"] == "2001-02-03"
    assert "Favorite tvshows" in show["tag"]
    assert "set" not in show and "playcount" not in show
    # tvshowcounts derives a show's date added from its episode files.
    assert "dateadded" not in show
    changed = metadata.details(
        series(),
        SERVER,
        "key",
        LIB,
        seasons=[season("s", number=1, Overview="other arc")],
    )
    assert changed["uniqueid"]["kofinrefresh"] != show["uniqueid"]["kofinrefresh"]
    ep = metadata.details(episode("e1"), SERVER, "key", LIB)
    assert (ep["season"], ep["episode"], ep["firstaired"]) == (1, 1, "2001-02-10")
    assert "tag" not in ep and ep["resume"]["position"] == 0
    clip = metadata.details(musicvideo(), SERVER, "key", LIB)
    assert clip["artist"] == ["Band"] and clip["album"] == "Album"
    assert metadata.details(boxset("b", " Set ", ["a"]), SERVER, "key", LIB) == {
        "title": "Set",
        "plot": "",
        "art": {},
    }


def test_collections_pick_one_set_per_movie_stably():
    sets = [boxset("b2", "Zeta", ["a", "b"]), boxset("b1", "alpha", ["a"])]
    assert metadata.collections_of(sets) == {"a": "alpha", "b": "Zeta"}


def test_scanner_listitem_never_stamps_a_zero_resume_point(monkeypatch):
    from unittest.mock import Mock

    tag = Mock()
    li = Mock()
    li.getVideoInfoTag.return_value = tag
    monkeypatch.setattr(metadata.xbmcgui, "ListItem", lambda *a, **k: li)
    metadata.listitem(movie(UserData={}), SERVER, "key", LIB)
    assert not tag.setResumePoint.called
    metadata.listitem(movie(), SERVER, "key", LIB, set_name="Trilogy")
    tag.setResumePoint.assert_called_once_with(12.0, 120.0)
    tag.setSet.assert_called_once_with("Trilogy")
    tag.reset_mock()
    metadata.listitem(
        series(),
        SERVER,
        "key",
        LIB,
        seasons=[season("s", number=2, Name="Book Two", Overview="arc")],
        episodes=[episode("e1")],
    )
    tag.addSeason.assert_called_once_with(2, "Book Two", "arc")
    li.setProperty.assert_called_with("hash", metadata.show_hash([episode("e1")]))
    assert not tag.setResumePoint.called


# -- lifecycle ---------------------------------------------------------------------


def test_complete_movie_lifecycle_and_foreign_item_survives(store, backend, kodi):
    backend.setup()
    store.publish([movie()], library=LIB)
    backend.reconcile()
    first = store.mapping("a").kodi_id
    assert store.state("a").applied == 1
    assert kodi.bindings == {
        paths.library_dir(store.namespace, LIB, "movies"): "movies"
    }
    assert kodi.scanned == [paths.library_dir(store.namespace, LIB, "movies")]
    # A scanner row built from the same policy needs no patch at all.
    assert not methods(kodi, "VideoLibrary.SetMovieDetails")
    store.publish(
        [
            movie(
                Overview="Changed",
                People=[{"Name": "New actor", "Type": "Actor"}],
                UserData={
                    "Played": True,
                    "PlayCount": 4,
                    "PlaybackPositionTicks": 300000000,
                },
            )
        ],
        library=LIB,
    )
    backend.reconcile()
    second = store.mapping("a").kodi_id
    assert second != first
    assert kodi.rows["Movie"][second]["playcount"] == 4
    assert kodi.rows["Movie"][second]["resume"]["position"] == 30
    kodi.rows["Movie"][900] = {
        "movieid": 900,
        "file": "/foreign/movie.mkv",
        "uniqueid": {"imdb": "foreign"},
    }
    store.publish([], library=LIB)
    backend.reconcile()
    assert set(kodi.rows["Movie"]) == {900}
    assert not store.pending()
    assert kodi.bindings == {}


def test_show_import_files_seasons_and_episodes_under_the_show(store, backend, kodi):
    items = show_bundle(seasons=(1, 2)) + [
        season(SHOW[:4] + "s0", SHOW, 0, Name="Specials")
    ]
    items[1]["Name"] = "Book One"
    store.publish(items, library=LIB)
    backend.reconcile()
    assert not store.pending()
    assert kodi.bindings == {
        paths.library_dir(store.namespace, LIB, "tvshows"): "tvshows",
        paths.show_dir(store.namespace, LIB, SHOW): "tvshows",
    }
    # Kodi finds a plugin folder's scraper only through the folder's own
    # binding, so every show is bound before the root is scanned.
    assert [p for m, p in kodi.calls if m == "VideoLibrary.SetSourceContent"][1] == {
        "path": paths.show_dir(store.namespace, LIB, SHOW),
        "content": "tvshows",
        "scraperid": "metadata.local",
        "containssingleitem": True,
        "refresh": False,
    }
    assert kodi.scanned == [paths.library_dir(store.namespace, LIB, "tvshows")]
    show = kodi.owned("Series")[SHOW]
    assert show["file"] == paths.show_dir(store.namespace, LIB, SHOW)
    assert "Favorite tvshows" in show["tag"]
    episodes = kodi.owned("Episode")
    assert len(episodes) == 2
    assert all(r["tvshowid"] == show["tvshowid"] for r in episodes.values())
    assert episodes["ea11"]["file"] == paths.playback_url(
        store.namespace, "Episode", LIB, "ea11", SHOW
    )
    seasons = {r["season"]: r for r in kodi.rows["Season"].values()}
    assert seasons[1]["title"] == "Book One"
    assert seasons[2]["title"] == "Season 2"
    assert store.mapping(SHOW[:4] + "s1").kodi_id == seasons[1]["seasonid"]
    # An empty specials season is invisible to GetSeasons: applied, no row.
    assert store.mapping(SHOW[:4] + "s0").kodi_id is None
    # addSeason named it at import; the defaults are Kodi's own labels.
    assert not methods(kodi, "VideoLibrary.SetSeasonDetails")
    assert not methods(kodi, "VideoLibrary.SetEpisodeDetails")
    store.publish([season(SHOW[:4] + "s1", SHOW, 1, Name="Book Uno")])
    backend.reconcile()
    assert [p["title"] for p in methods(kodi, "VideoLibrary.SetSeasonDetails")] == [
        "Book Uno"
    ]
    assert seasons[1]["title"] == "Book Uno"
    store.publish([season(SHOW[:4] + "s1", SHOW, 1)])
    backend.reconcile()
    # Withdrawn custom name: cleared once, then left to Kodi's label.
    assert [p["title"] for p in methods(kodi, "VideoLibrary.SetSeasonDetails")] == [
        "Book Uno",
        "",
    ]
    assert seasons[1]["title"] == "Season 1"
    assert not store.pending()


def test_episode_refresh_changes_id_and_restores_userdata(store, backend, kodi):
    store.publish(show_bundle(), library=LIB)
    backend.reconcile()
    old = store.mapping("ea11").kodi_id
    kodi.rows["Episode"][old]["playcount"] = 3
    store.publish([episode("ea11", People=[{"Name": "Other", "Type": "GuestStar"}])])
    backend.reconcile()
    # A real local edit beats the refresh: delivered first, never overwritten.
    assert store.local_pending() == [("ea11", {"playcount": 3})]
    # Delivered to the server and re-fetched, as flush_local does.
    store.local_done("ea11", {"playcount": 3})
    store.publish(
        [
            episode(
                "ea11",
                People=[{"Name": "Other", "Type": "GuestStar"}],
                UserData={"Played": True, "PlayCount": 3},
            )
        ]
    )
    backend.reconcile()
    new = store.mapping("ea11").kodi_id
    assert kodi.rows["Episode"][new]["playcount"] == 3
    assert new != old
    assert methods(kodi, "VideoLibrary.RefreshEpisode") == [
        {"episodeid": old, "ignorenfo": False}
    ]
    assert kodi.rows["Episode"][new]["uniqueid"][
        "kofinrefresh"
    ] == metadata.refresh_token(store.item("ea11"))
    assert not store.pending()


def test_show_cast_change_refreshes_the_show_and_every_episode_follows(
    store, backend, kodi
):
    store.publish(show_bundle(episodes=3), library=LIB)
    backend.reconcile()
    old_show = store.mapping(SHOW).kodi_id
    old_episodes = {i: store.mapping(i).kodi_id for i in ("ea11", "ea12", "ea13")}
    kodi.rows["Episode"][old_episodes["ea12"]]["playcount"] = 1
    store.publish([series(People=[{"Name": "Recast", "Type": "Actor"}])])
    backend.reconcile()
    assert methods(kodi, "VideoLibrary.RefreshTVShow") == [
        {"tvshowid": old_show, "ignorenfo": False, "refreshepisodes": True}
    ]
    assert store.mapping(SHOW).kodi_id != old_show
    for item_id, before in old_episodes.items():
        assert store.mapping(item_id).kodi_id != before
    # The local watched mark set before the refresh is carried as a local
    # edit rather than lost under the re-created row.
    assert store.local_pending() == [("ea12", {"playcount": 1})]
    assert not [i for i in store.pending() if i[0].item_id != "ea12"]


def test_removing_one_of_two_libraries_clears_only_that_one_in_one_call(
    store, backend, kodi
):
    membership = {}
    items = []
    for library, show_id in ((LIB, SHOW), (LIB2, SHOW2)):
        for item in show_bundle(show_id) + [movie("m" + library[0])]:
            items.append(item)
            membership[item["Id"]] = library
    store.publish(items, membership=membership, complete_libraries=[LIB, LIB2])
    backend.reconcile()
    assert len(kodi.owned("Series")) == 2 and len(kodi.owned("Movie")) == 2
    kodi.calls.clear()
    kodi.builtins.clear()
    store.publish([], library=LIB)
    backend.reconcile()
    clears = methods(kodi, "VideoLibrary.SetSourceContent")
    assert clears == [
        {
            "path": paths.library_root(store.namespace, LIB),
            "content": "none",
            "clearmode": "remove",
            "refresh": False,
        }
    ]
    assert not methods(kodi, "VideoLibrary.RemoveTVShow")
    assert not methods(kodi, "VideoLibrary.RemoveMovie")
    assert set(kodi.owned("Series")) == {SHOW2}
    assert set(kodi.owned("Movie")) == {"m2"}
    assert kodi.builtins == ["UpdateLibrary(video)"]
    assert not store.pending()
    assert set(kodi.bindings) == {
        paths.library_dir(store.namespace, LIB2, "tvshows"),
        paths.show_dir(store.namespace, LIB2, SHOW2),
        paths.library_dir(store.namespace, LIB2, "movies"),
    }
    assert set(store.bindings()) == set(kodi.bindings)
    # The survivor was re-read, not re-imported.
    assert store.mapping(SHOW2).kodi_id in kodi.rows["Series"]
    assert not any(m == "VideoLibrary.Scan" for m, _ in kodi.calls)


def test_episode_and_show_removals_confirm_with_scoped_readbacks(store, backend, kodi):
    store.publish(
        show_bundle(episodes=3) + show_bundle(SHOW2, episodes=1, prefix="f"),
        library=LIB,
    )
    backend.reconcile()
    kodi.calls.clear()
    store.publish([], removed=["ea13"])
    backend.reconcile()
    assert len(methods(kodi, "VideoLibrary.RemoveEpisode")) == 1
    assert len(methods(kodi, "VideoLibrary.GetEpisodes")) == 1
    assert set(kodi.owned("Episode")) == {"ea11", "ea12", "fb21"}
    kodi.calls.clear()
    store.publish([], removed=[SHOW2, "fb21", SHOW2[:4] + "s1"])
    backend.reconcile()
    assert len(methods(kodi, "VideoLibrary.RemoveTVShow")) == 1
    assert not methods(kodi, "VideoLibrary.RemoveEpisode")
    assert set(kodi.owned("Series")) == {SHOW}
    assert paths.show_dir(store.namespace, LIB, SHOW2) not in kodi.bindings
    assert paths.show_dir(store.namespace, LIB, SHOW2) not in store.bindings()
    assert not kodi.rows["Season"] or all(
        r["tvshowid"] == kodi.owned("Series")[SHOW]["tvshowid"]
        for r in kodi.rows["Season"].values()
    )
    assert not store.pending()


def test_unnumbered_special_and_empty_season_apply_without_a_row(store, backend, kodi):
    items = show_bundle() + [
        episode("ea19", season_number=0, number=0),
        season(SHOW[:4] + "s0", SHOW, 0, Name="Specials"),
        season(SHOW[:4] + "s7", SHOW, 7, Name="Unaired"),
    ]
    store.publish(items, library=LIB)
    backend.reconcile()
    assert "ea19" not in kodi.owned("Episode")
    assert not store.pending()
    assert store.mapping("ea19").kodi_id is None
    # Nor is the unnumbered special a reason to scan again.
    kodi.scanned.clear()
    store.publish([episode("ea19", season_number=0, number=0, Overview="edit")])
    backend.reconcile()
    assert kodi.scanned == [] and not store.pending()
    # The specials season holds only the unnumbered episode; season 7
    # holds nothing. Neither is visible to GetSeasons, so neither is a row.
    assert store.mapping(SHOW[:4] + "s0").kodi_id is None
    assert store.mapping(SHOW[:4] + "s7").kodi_id is None
    assert store.mapping(SHOW[:4] + "s1").kodi_id is not None


def test_collections_file_sets_at_import_and_patch_set_details(store, backend, kodi):
    store.publish(
        [
            movie(),
            movie("b"),
            movie("c"),
            boxset("box", "Trilogy", ["a", "b"], Overview="Three films"),
        ],
        library=LIB,
    )
    backend.reconcile()
    assert not store.pending()
    assert kodi.owned("Movie")["a"]["set"] == "Trilogy"
    assert kodi.owned("Movie")["c"]["set"] == ""
    assert not methods(kodi, "VideoLibrary.SetMovieDetails")
    sets = {r["title"]: r for r in kodi.rows["BoxSet"].values()}
    assert sets["Trilogy"]["plot"] == "Three films"
    assert store.mapping("box").kodi_id == sets["Trilogy"]["setid"]
    kodi.calls.clear()
    store.publish([boxset("box", "Trilogy", ["b", "c"], Overview="Three films")])
    store.invalidate({"a", "c"})
    backend.reconcile()
    assert kodi.owned("Movie")["a"]["set"] == ""
    assert kodi.owned("Movie")["c"]["set"] == "Trilogy"
    assert len(methods(kodi, "VideoLibrary.SetMovieDetails")) == 2
    kodi.calls.clear()
    store.publish([], library=LIB)
    backend.reconcile()
    assert methods(kodi, "VideoLibrary.Clean") == [
        {"showdialogs": False, "directory": paths.library_root(store.namespace, LIB)}
    ]
    assert kodi.rows["BoxSet"] == {}


def test_patches_travel_in_arrays_of_twenty_five(store, backend, kodi, monkeypatch):
    store.publish([movie("m%02d" % i) for i in range(30)], library=LIB)
    backend.reconcile()
    batches = []
    original = kodi.batch

    def counting(requests):
        batches.append(len(requests))
        return original(requests)

    monkeypatch.setattr(native, "rpc_batch", counting)
    store.publish(
        [movie("m%02d" % i, Overview="edited") for i in range(30)], library=LIB
    )
    backend.reconcile()
    # 30 setters as 25 + 5, each followed by its confirmation reads.
    assert batches == [25, 25, 5, 5]
    assert not store.pending()


def test_repair_updates_changed_id_and_preserves_local_art(store, backend, kodi):
    store.publish([movie()], library=LIB)
    backend.reconcile()
    row = kodi.rows["Movie"].pop(store.mapping("a").kodi_id)
    row["movieid"] = 400
    row["art"]["local"] = "image://https%3A%2F%2Ffixture.invalid%2Flocal.png/"
    kodi.rows["Movie"][400] = row
    backend.reconcile(repair=True)
    assert store.mapping("a").kodi_id == 400
    store.publish([movie(Overview="Changed")])
    backend.reconcile()
    assert (
        kodi.rows["Movie"][400]["art"]["local"] == "https://fixture.invalid/local.png"
    )
    assert kodi.rows["Movie"][400]["plot"] == "Changed"


def test_patch_failure_and_restart_does_not_duplicate_import(store, backend, kodi):
    store.publish([movie()], library=LIB)
    backend.reconcile()
    store.publish([movie(Overview="New")], library=LIB)
    kodi.fail = "VideoLibrary.SetMovieDetails"
    with pytest.raises(RuntimeError):
        backend.reconcile()
    assert store.state("a").applied == 1
    assert len(kodi.rows["Movie"]) == 1
    kodi.fail = ""
    backend.store = Store(store.namespace)
    backend.reconcile()
    assert store.state("a").applied == 2
    assert len(kodi.rows["Movie"]) == 1


def test_rpc_acceptance_without_patch_cannot_confirm(store, backend, kodi):
    store.publish([movie()], library=LIB)
    backend.reconcile()
    store.publish([movie(Overview="New")], library=LIB)
    kodi.accept_without_apply = True
    with pytest.raises(RuntimeError, match="readback differs"):
        backend.reconcile()
    assert store.state("a").applied == 1
    assert store.state("a").desired == 2


def test_stale_native_id_never_removes_foreign_content(store, backend, kodi):
    store.publish([movie(), movie("b")], library=LIB)
    backend.reconcile()
    native_id = store.mapping("a").kodi_id
    kodi.rows["Movie"][native_id].update(file="/foreign.mkv", uniqueid={})
    store.publish([], removed=["a"])
    with pytest.raises(RuntimeError, match="ownership"):
        backend.reconcile()
    assert native_id in kodi.rows["Movie"]
    assert not methods(kodi, "VideoLibrary.RemoveMovie")


def test_unconfirmed_removal_stays_pending(store, backend, kodi):
    store.publish([movie(), movie("b")], library=LIB)
    backend.reconcile()
    store.publish([], removed=["a"])
    kodi.accept_without_apply = True
    with pytest.raises(RuntimeError, match="removal not confirmed"):
        backend.reconcile()
    assert store.state("a").status == "pending"
    kodi.accept_without_apply = False
    backend.reconcile()
    assert not store.pending()
    assert len(kodi.rows["Movie"]) == 1


def test_first_run_gate_refuses_existing_library_and_other_namespace(
    store, backend, kodi
):
    kodi.rows["Movie"][4] = {"movieid": 4, "file": "/foreign.mkv", "uniqueid": {}}
    with pytest.raises(BackendMismatch, match="fresh"):
        backend.setup()
    assert not store.prepared()
    kodi.rows["Movie"].clear()
    backend.setup()
    other = Native(Store(namespace("another", "user")))
    with pytest.raises(BackendMismatch, match="another"):
        other.setup()


def test_legacy_rows_are_cleared_before_the_old_tables_go(store, backend, kodi):
    backend.setup()
    with private.Database() as db:
        db.cursor.execute("CREATE TABLE api_movie(namespace TEXT, item_id TEXT)")
    kodi.rows["Movie"][5] = {
        "movieid": 5,
        "file": paths.root(store.namespace) + "?mode=play&id=old",
        "uniqueid": {"kofin": store.namespace + ":old"},
    }
    backend.setup()
    assert kodi.rows["Movie"] == {}
    assert methods(kodi, "VideoLibrary.SetSourceContent")[0]["path"] == paths.root(
        store.namespace
    )
    assert kodi.builtins == ["UpdateLibrary(video)"]
    assert not store.legacy_present()
    assert store.prepared() == store.namespace


def test_expected_userdata_scoped_by_generation_and_expiry(store, monkeypatch):
    store.publish([movie()], library=LIB)
    store.expect("a", 1, {"playcount": 3})
    assert store.is_echo("a", "playcount", 3)
    assert not store.is_echo("a", "playcount", 0)
    store.publish([movie(Overview="second")], library=LIB)
    assert not store.is_echo("a", "playcount", 3)
    store.expect("a", 2, {"playcount": 3})
    monkeypatch.setattr("kofin.sync.backends.api.store.time.time", lambda: 10**12)
    assert not store.is_echo("a", "playcount", 3)


def test_real_local_edit_is_preserved_during_server_refresh(store, backend, kodi):
    store.publish([movie()], library=LIB)
    backend.reconcile()
    native_id = store.mapping("a").kodi_id
    kodi.rows["Movie"][native_id]["playcount"] = 3
    store.publish([movie(People=[{"Type": "Actor", "Name": "Another actor"}])])
    backend.reconcile()
    assert store.local_pending() == [("a", {"playcount": 3})]
    assert store.state("a").applied == 1
    assert kodi.rows["Movie"][native_id]["playcount"] == 3


def test_snapshot_pin_survives_interrupted_scan(store, backend, kodi, monkeypatch):
    store.publish([movie()], library=LIB)
    scan = backend.scan

    def interrupted(directories):
        backend._async_pending = True
        raise InterruptedError("restart")

    monkeypatch.setattr(backend, "scan", interrupted)
    with pytest.raises(InterruptedError):
        backend.reconcile()
    store.publish([movie(Overview="Later generation"), movie("b")], library=LIB)
    # The pinned membership is what the interrupted scan was listing; the
    # payload it serves is the newest, so no stale generation is applied.
    assert set(store.records()) == {"a"}
    assert set(store.records(pinned=False)) == {"a", "b"}
    monkeypatch.setattr(backend, "scan", scan)
    backend.reconcile()
    assert store.state("a").applied == 2
    # The pass that resumed the pinned listing does not add to it; the
    # next one, unpinned, imports the newcomer.
    assert store.state("b").applied == 0
    backend.reconcile()
    assert store.state("b").applied == 1
    assert len(kodi.rows["Movie"]) == 2


@pytest.mark.parametrize("finishes", [True, False])
def test_scan_wait_outlasts_a_long_scan_but_not_an_idle_one(
    store, backend, kodi, monkeypatch, finishes
):
    clock = [0.0]
    scanning = [False]
    remaining = [0]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(native.xbmc, "getCondVisibility", lambda _: scanning[0])
    original_rpc = kodi.rpc

    def slow_scan(method, params=None):
        if method == "VideoLibrary.Scan":
            scanning[0], remaining[0] = True, 10
            return "OK"
        return original_rpc(method, params)

    def tick(_):
        clock[0] += 30
        if scanning[0]:
            remaining[0] -= 1
            if not remaining[0]:
                scanning[0] = False
                if finishes:
                    original_rpc(
                        "VideoLibrary.Scan",
                        {
                            "directory": paths.library_dir(
                                store.namespace, LIB, "movies"
                            )
                        },
                    )
        return False

    monkeypatch.setattr(native, "rpc", slow_scan)
    backend.monitor.waitForAbort = tick
    store.publish([movie()], library=LIB)
    directory = paths.library_dir(store.namespace, LIB, "movies")
    if finishes:
        backend.scan([directory])
        assert clock[0] >= 300
        assert len(kodi.rows["Movie"]) == 1
    else:
        with pytest.raises(TimeoutError):
            backend.scan([directory])
        assert 300 + 60 <= clock[0] <= 300 + 60 + 30


# -- coordinator ---------------------------------------------------------------------


class Server:
    """A Jellyfin stand-in: libraries of items, collections, change feed."""

    server = SERVER
    user_id = "user"

    def __init__(self, libraries, boxsets=(), views=None):
        self.libraries = libraries
        self.boxsets = list(boxsets)
        self.views_list = views or [
            {"Id": lib, "CollectionType": collection, "Name": "Lib " + lib[:1]}
            for lib, collection in (
                (LIB, "movies"),
                (LIB2, "tvshows"),
            )
        ]
        self.updated = []
        self.deleted = set()

    def views(self):
        return {"Items": self.views_list}

    def all_items(self):
        for items in self.libraries.values():
            yield from items
        yield from self.boxsets

    def item(self, item_id):
        for item in self.all_items():
            if item["Id"] == item_id:
                return copy.deepcopy(item)
        raise OSError("item no longer available")

    def items(self, params):
        if params.get("Ids"):
            wanted = params["Ids"].split(",")
            rows = [copy.deepcopy(i) for i in self.all_items() if i["Id"] in wanted]
            return {"Items": rows, "TotalRecordCount": len(rows)}
        types = (params.get("IncludeItemTypes") or "").split(",")
        parent = params.get("ParentId")
        if parent in self.libraries:
            rows = [i for i in self.libraries[parent] if i["Type"] in types]
        elif parent:
            box = next(b for b in self.boxsets if b["Id"] == parent)
            rows = [{"Id": m, "Type": "Movie"} for m in box["KofinMembers"]]
        else:
            rows = [b for b in self.boxsets if "BoxSet" in types]
        if params.get("MinDateLastSaved"):
            rows = [
                r
                for r in rows
                if r.get("DateLastSaved", "") >= params["MinDateLastSaved"]
            ]
        start = params.get("StartIndex", 0)
        limit = params.get("Limit", len(rows))
        return {
            "Items": copy.deepcopy(rows[start : start + limit]),
            "TotalRecordCount": len(rows),
        }

    def ancestors(self, item_id):
        for lib, items in self.libraries.items():
            if any(i["Id"] == item_id for i in items):
                return [{"Id": lib, "Type": "CollectionFolder"}]
        return []

    def update_user_data(self, item_id, payload):
        self.updated.append((item_id, payload))


def worker(store, server, selected, monkeypatch, feed=None):
    from kofin.sync.backends.api import library

    w = Library.__new__(Library)
    w.store = store
    w.api = server
    w.player = None
    w._stop_event = SimpleNamespace(is_set=lambda: False, wait=lambda s: False)
    w._reload_owed = set()
    w._catchup_due = 0.0
    w._full_due = False
    w._repair = False
    w.changefeed = feed
    w._queue = __import__("queue").Queue()
    selections = (
        selected
        if isinstance(selected, list) and selected and isinstance(selected[0], list)
        else [list(selected)]
    )
    replies = iter(selections)
    last = [selections[-1]]

    def get_list(_):
        try:
            last[0] = next(replies)
        except StopIteration:
            pass
        return last[0]

    monkeypatch.setattr(library.settings, "get_list", get_list)
    monkeypatch.setattr(library.settings, "get_str", lambda _: "")
    monkeypatch.setattr(library.settings, "set_str", lambda *_: None)
    monkeypatch.setattr(library.private, "get_sync", lambda: {"Whitelist": []})
    monkeypatch.setattr(library.private, "save_sync", lambda state: None)
    if feed is None:
        monkeypatch.setattr(library.changefeed, "detect", lambda api: None)
    return w


@pytest.mark.parametrize(
    "pages",
    [
        [
            {"Items": [movie()], "TotalRecordCount": 2},
            {"Items": [], "TotalRecordCount": 2},
        ],
        [
            {"Items": [movie()], "TotalRecordCount": 2},
            {"Items": [movie()], "TotalRecordCount": 2},
        ],
        [
            {"Items": [movie()], "TotalRecordCount": 2},
            {"Items": [movie("b")], "TotalRecordCount": 3},
        ],
        [{"Items": []}],
        [{"Items": [movie()], "TotalRecordCount": 0}],
        [{"Items": [episode("e", SeriesId=None)], "TotalRecordCount": 1}],
    ],
)
def test_partial_enumeration_never_changes_store(store, pages):
    store.publish([movie()], library=LIB)
    before = store.records(), store.generation()
    replies = iter(pages)
    api = SimpleNamespace(user_id="u", items=lambda params: next(replies))
    kind = pages[0]["Items"][0]["Type"] if pages[0]["Items"] else "Movie"
    with pytest.raises(ValueError):
        store.publish(fetch_kind(api, LIB, kind), library=LIB)
    assert (store.records(), store.generation()) == before


def test_pagination_uses_supported_sort_keys_and_compacts():
    seen = []

    def page(params):
        seen.append(params)
        return {
            "Items": [movie(ImageBlurHashes={"Primary": {}})],
            "TotalRecordCount": 1,
        }

    rows = fetch_kind(SimpleNamespace(user_id="u", items=page), LIB, "Movie")
    assert "ImageBlurHashes" not in rows[0]
    assert seen[0]["SortBy"] == "DateCreated,SortName"
    assert seen[0]["IncludeItemTypes"] == "Movie"


def test_full_enumeration_publishes_every_kind_after_all_fetches(store, monkeypatch):
    server = Server(
        {LIB: [movie(), movie("b")], LIB2: show_bundle()},
        boxsets=[boxset("box", "Pair", ["a", "b"])],
    )
    w = worker(store, server, [LIB, LIB2], monkeypatch)
    failing = server.items

    def flaky(params):
        if (
            params.get("ParentId") == LIB2
            and params.get("IncludeItemTypes") == "Episode"
        ):
            raise OSError("offline")
        return failing(params)

    server.items = flaky
    with pytest.raises(OSError):
        w.full_sync()
    assert store.records(pinned=False) == {}
    server.items = failing
    w.full_sync()
    records = store.records(pinned=False)
    assert {r.kind for r in records.values()} == {
        "Movie",
        "Series",
        "Season",
        "Episode",
        "BoxSet",
    }
    assert records["ea11"].library == LIB2 and records["ea11"].parent_id == SHOW
    assert records["box"].library == ""
    assert store.watermark()[0] and store.watermark()[1]
    assert w._repair
    # A later complete pass that lost a show tombstones it and its children.
    server.libraries[LIB2] = show_bundle(episodes=1)
    w.full_sync()
    assert store.state("ea12").operation == "remove"
    assert store.state("ea11").operation == "upsert"


def test_enumeration_drops_a_library_deselected_meanwhile(store, monkeypatch):
    server = Server({LIB: [movie()], LIB2: show_bundle()})
    w = worker(store, server, [[LIB, LIB2], [LIB2]], monkeypatch)
    w.full_sync()
    assert set(store.records(pinned=False)) == {SHOW, SHOW[:4] + "s1", "ea11", "ea12"}


def test_enumeration_publishes_nothing_when_the_selection_emptied(store, monkeypatch):
    store.publish([movie()], library=LIB)
    before = store.records(pinned=False), store.pending()
    server = Server({LIB: [movie("b")]})
    w = worker(store, server, [[LIB], []], monkeypatch)
    w.full_sync()
    assert (store.records(pinned=False), store.pending()) == before


def test_sync_library_enumerates_only_the_named_library(store, monkeypatch):
    server = Server({LIB: [movie()], LIB2: show_bundle()})
    w = worker(store, server, [LIB, LIB2], monkeypatch)
    w.command("SyncLibrary", {"Id": LIB})
    assert set(store.records(pinned=False)) == {"a"}
    assert not w._repair
    assert store.watermark() == ("", 0.0)


class Feed:
    tier = "kofin"

    def __init__(self, records, userdata=(), server_time=1000, retention=None):
        self.records = records
        self.userdata = list(userdata)
        self.server_time = server_time
        self.retention = retention
        self.asked = []

    def changes(self, last_sync, include, libraries=None):
        from kofin.sync.changefeed import ChangeSet, Envelope

        self.asked.append((last_sync, tuple(include), tuple(libraries or ())))
        return ChangeSet(
            records=list(self.records),
            userdata=self.userdata,
            envelope=Envelope(
                server_time=self.server_time, retention_cutoff=self.retention
            ),
        )

    def server_now(self):
        return self.server_time


def test_catch_up_applies_feed_records_and_advances_the_watermark(store, monkeypatch):
    from kofin.sync.changefeed import ChangeRecord, unix_to_watermark

    server = Server({LIB: [movie(), movie("b")], LIB2: show_bundle()})
    store.publish([movie(), movie("b")], library=LIB)
    store.set_watermark(watermark=unix_to_watermark(500), enumerated=time.time())
    server.libraries[LIB][0]["Overview"] = "Edited"
    feed = Feed(
        [
            ChangeRecord("a", "Updated", item_type="Movie", library_ids=[LIB]),
            ChangeRecord("b", "Removed"),
            ChangeRecord(
                "ea11", "Added", item_type="Episode", series_id=SHOW, library_ids=[LIB2]
            ),
            ChangeRecord(SHOW, "Added", item_type="Series", library_ids=[LIB2]),
            ChangeRecord("zz", "Added", item_type="Movie", library_ids=["9" * 32]),
        ],
        userdata=[{"ItemId": "a", "Played": True, "PlayCount": 2}],
        server_time=1200,
    )
    w = worker(store, server, [LIB, LIB2], monkeypatch, feed=feed)
    w.catch_up()
    records = store.records(pinned=False)
    assert records["a"].item["Overview"] == "Edited"
    assert records["a"].item["UserData"]["PlayCount"] == 2
    assert store.state("b").operation == "remove"
    assert records["ea11"].library == LIB2 and records[SHOW].library == LIB2
    assert "zz" not in records
    assert store.watermark()[0] == unix_to_watermark(1200)
    assert feed.asked[0][1] == ("movies", "tvshows", "boxsets", "musicvideos")
    assert set(feed.asked[0][2]) == {LIB, LIB2}


def test_catch_up_retention_overrun_schedules_a_full_pass(store, monkeypatch):
    from kofin.sync.changefeed import unix_to_watermark

    server = Server({LIB: [movie()]})
    store.set_watermark(watermark=unix_to_watermark(500), enumerated=time.time())
    w = worker(
        store,
        server,
        [LIB],
        monkeypatch,
        feed=Feed([], server_time=9000, retention=8000),
    )
    w.catch_up()
    assert w._full_due


def test_catch_up_without_a_companion_uses_the_save_date(store, monkeypatch):
    from kofin.sync.changefeed import unix_to_watermark

    server = Server(
        {
            LIB: [
                movie(DateLastSaved="2026-10-09T00:00:00Z"),
                movie("b", DateLastSaved="2026-01-01T00:00:00Z"),
            ]
        }
    )
    store.set_watermark(watermark="2026-10-08T00:00:00Z", enumerated=time.time())
    w = worker(store, server, [LIB], monkeypatch)
    w.catch_up()
    assert set(store.records(pinned=False)) == {"a"}
    assert store.watermark()[0] > "2026-10-08T00:00:00Z"
    assert unix_to_watermark(0) < store.watermark()[0]


def test_websocket_changes_fetch_known_items_and_defer_unknown_ones(store, monkeypatch):
    server = Server({LIB: [movie(), movie("b")]})
    store.publish([movie()], library=LIB)
    store.set_watermark(watermark="x", enumerated=time.time())
    w = worker(store, server, [LIB], monkeypatch)
    w._catchup_due = 10**9
    server.libraries[LIB][0]["Overview"] = "Fresh"
    w.command("changed", ["a", "b"])
    assert store.records(pinned=False)["a"].item["Overview"] == "Fresh"
    assert "b" not in store.records(pinned=False)
    assert w._catchup_due == 0
    w.command("userdata", [{"ItemId": "a", "Played": True, "PlayCount": 1}])
    assert store.records(pinned=False)["a"].item["UserData"]["PlayCount"] == 1
    w.command("removed", ["a"])
    assert store.state("a").operation == "remove"


def test_failed_event_or_local_delivery_cannot_starve_enumeration(store, monkeypatch):
    from kofin.sync.backends.api import library

    clock = [0.0]
    passes = []
    store.publish([movie(), movie("b")], library=LIB)
    store.set_watermark(watermark="x", enumerated=1.0)
    server = Server({LIB: [movie("c")]})
    w = worker(store, server, [LIB], monkeypatch)
    w._queue.put(("changed", ["a"]))
    w._queue.put(("removed", ["b"]))

    def missing(*args, **kwargs):
        raise OSError("item no longer available")

    server.items = missing
    monkeypatch.setattr(w, "flush_local", missing)

    def enumerate_all(libraries=None):
        passes.append(clock[0])
        assert store.state("b").operation == "remove"
        store.publish([movie("c")], library=LIB)

    monkeypatch.setattr(w, "full_sync", enumerate_all)
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "time", lambda: 10**6)
    monkeypatch.setattr(
        library, "Native", lambda *args: SimpleNamespace(setup=lambda: None)
    )
    monkeypatch.setattr(w, "apply", lambda native: None)
    monkeypatch.setattr(library, "status", lambda value: None)
    monkeypatch.setattr(
        library.Credentials, "load", lambda: SimpleNamespace(server_id="s", user_id="u")
    )

    def wait(seconds):
        clock[0] += seconds
        assert clock[0] < 60, "worker failed to reach recovery"
        return bool(passes)

    w._stop_event = SimpleNamespace(is_set=lambda: False, wait=wait)
    w.stop_thread = False
    w.startup_done = False
    monkeypatch.setattr(
        library.xbmc,
        "Monitor",
        lambda: SimpleNamespace(abortRequested=lambda: False, waitForAbort=wait),
    )
    w.run()
    assert len(passes) == 1
    assert set(store.records(pinned=False)) == {"c"}


def test_first_content_reloads_the_skin_once_per_kind_and_waits_for_playback(
    store, monkeypatch
):
    from kofin.sync.backends.api import library

    builtins = []
    monkeypatch.setattr(library.xbmc, "executebuiltin", builtins.append)
    monkeypatch.setattr(library.xbmc, "getCondVisibility", lambda _: True)
    monkeypatch.setattr(library.xbmc, "getLocalizedString", lambda _: "Done")
    monkeypatch.setattr(library, "status", lambda value: None)
    w = worker(store, Server({}), [LIB], monkeypatch)
    store.publish([movie()] + show_bundle(), library=LIB)
    playing = [True]
    w.player = SimpleNamespace(isPlayingVideo=lambda: playing[0])

    def import_movie(repair=False):
        store.remember("a", 1, 10, {}, "Movie")

    w.apply(SimpleNamespace(reconcile=import_movie))
    assert builtins == []
    playing[0] = False
    w.flush_pending_reload()
    assert builtins == ["ReloadSkin()"]
    w.apply(
        SimpleNamespace(
            reconcile=lambda repair=False: store.remember(SHOW, 1, 11, {}, "Series")
        )
    )
    assert builtins == ["ReloadSkin()", "ReloadSkin()"]
    store.publish([movie(Overview="x")] + show_bundle(), library=LIB)
    w.apply(SimpleNamespace(reconcile=lambda repair=False: None))
    assert builtins == ["ReloadSkin()", "ReloadSkin()"]


# -- provider and identity -----------------------------------------------------------


def test_provider_lists_the_layout_and_answers_existence_offline(store, monkeypatch):
    from kofin.plugin.router import Request
    from kofin.sync.backends.api import provider

    store.publish(
        show_bundle() + [movie(), episode("ea19", season_number=0, number=0)],
        library=LIB,
    )
    store.bind(paths.library_dir(store.namespace, LIB, "tvshows"), "tvshows")
    rendered = []
    resolved = []
    content = []
    monkeypatch.setattr(provider.metadata, "listitem", lambda item, *a, **k: item["Id"])
    monkeypatch.setattr(
        provider.xbmcplugin,
        "addDirectoryItems",
        lambda h, items, n: rendered.append(list(items)),
    )
    monkeypatch.setattr(
        provider.xbmcplugin, "setContent", lambda h, c: content.append(c)
    )
    monkeypatch.setattr(
        provider.xbmcplugin, "setResolvedUrl", lambda h, ok, li: resolved.append(ok)
    )
    monkeypatch.setattr(provider.xbmcplugin, "endOfDirectory", lambda *a, **k: None)
    tv = paths.library_dir(store.namespace, LIB, "tvshows")
    provider.serve(Request(tv, 1, {}))
    assert rendered[-1] == [(paths.show_dir(store.namespace, LIB, SHOW), SHOW, True)]
    provider.serve(Request(paths.show_dir(store.namespace, LIB, SHOW), 1, {}))
    assert [e[1] for e in rendered[-1]] == ["ea11", "ea12"]
    assert rendered[-1][0][0] == paths.playback_url(
        store.namespace, "Episode", LIB, "ea11", SHOW
    )
    provider.serve(
        Request(
            paths.show_dir(store.namespace, LIB, SHOW),
            1,
            {"kodi_action": "refresh_info"},
        )
    )
    assert rendered[-1] == [(paths.show_dir(store.namespace, LIB, SHOW), SHOW, True)]
    provider.serve(
        Request(
            paths.show_dir(store.namespace, LIB, SHOW),
            1,
            {"kodi_action": "refresh_info", "id": "ea12"},
        )
    )
    assert [e[1] for e in rendered[-1]] == ["ea12"]
    provider.serve(Request(paths.library_dir(store.namespace, LIB, "movies"), 1, {}))
    assert [e[1] for e in rendered[-1]] == ["a"]
    assert content[-3:] == ["tvshows", "episodes", "movies"]
    for params in ({"id": "ea11"}, {}):
        provider.serve(
            Request(
                paths.show_dir(store.namespace, LIB, SHOW),
                1,
                dict(params, kodi_action="check_exists"),
            )
        )
    store.publish([], removed=["ea11", SHOW])
    provider.serve(
        Request(
            paths.show_dir(store.namespace, LIB, SHOW),
            1,
            {"kodi_action": "check_exists", "id": "ea11"},
        )
    )
    provider.serve(
        Request(
            paths.show_dir(store.namespace, LIB, SHOW),
            1,
            {"kodi_action": "check_exists"},
        )
    )
    provider.serve(Request(tv, 1, {"kodi_action": "check_exists"}))
    assert resolved == [True, True, False, False, True]
    provider.serve(Request(paths.root(store.namespace), 1, {}))
    assert rendered[-1][0][0] == tv


def test_identity_lookups_cover_episodes_and_music_videos(
    store, backend, kodi, monkeypatch
):
    from kofin.service import libraryclaim

    monkeypatch.setattr(native, "current_store", lambda: store)
    monkeypatch.setattr(libraryclaim.buildconfig, "BACKEND", "api")
    store.publish(show_bundle() + [musicvideo()], library=LIB)
    assert libraryclaim.library_video_path("ea11", "episode") is None
    backend.reconcile()
    episode_id = store.mapping("ea11").kodi_id
    assert native.mapped_item(episode_id, "episode") == "ea11"
    assert native.mapped_item(episode_id, "movie") is None
    assert native.native_id_for("ea11", "episode") == episode_id
    assert native.native_id_for("m", "musicvideo") == store.mapping("m").kodi_id
    assert libraryclaim.library_video_path("ea11", "episode") == paths.playback_url(
        store.namespace, "Episode", LIB, "ea11", SHOW
    )
    assert libraryclaim.library_video_path("m", "song") is None
    kodi.rows["Episode"][episode_id]["file"] = "/foreign.mkv"
    assert native.mapped_item(episode_id, "episode") is None


def test_namespace_and_urls_are_server_user_and_library_scoped():
    assert namespace("server", "a") != namespace("server", "b")
    key = namespace("server", "a")
    url = paths.playback_url(key, "Episode", LIB, "e", SHOW)
    assert url.startswith(paths.show_dir(key, LIB, SHOW))
    assert paths.parse(url) == paths.Location(key, LIB, "tvshows", SHOW)
    assert paths.parse(paths.library_dir(key, LIB, "movies")) == paths.Location(
        key, LIB, "movies"
    )
    assert paths.parse(paths.root(key)) == paths.Location(key)
    assert paths.parse("plugin://plugin.video.kofin/?mode=play") is None
    assert paths.parse(paths.library_dir(key, LIB, "movies") + SHOW + "/") is None
    assert paths.key_from_url(url) == key


def test_libraries_selected_while_the_worker_was_off_are_enumerated(store, monkeypatch):
    from kofin.sync.backends.api import library

    server = Server({LIB: [movie()], LIB2: show_bundle()})
    store.set_watermark(watermark="2026-10-08T00:00:00Z", enumerated=time.time())
    w = worker(store, server, [LIB, LIB2], monkeypatch)
    monkeypatch.setattr(library.private, "get_sync", lambda: {"Whitelist": [LIB]})
    caught = []
    monkeypatch.setattr(w, "catch_up", lambda: caught.append(True))
    w.refresh()
    assert set(store.records(pinned=False)) == {SHOW, SHOW[:4] + "s1", "ea11", "ea12"}
    assert not caught
    monkeypatch.setattr(library.private, "get_sync", lambda: {"Whitelist": [LIB, LIB2]})
    w._catchup_due = 0
    w.refresh()
    assert caught == [True]


def test_acknowledgements_and_expectations_are_written_in_batches(
    store, backend, kodi, monkeypatch
):
    opens = []
    original = private.Database.__enter__

    def counting(self):
        opens.append(1)
        return original(self)

    monkeypatch.setattr(private.Database, "__enter__", counting)
    store.publish([movie("m%03d" % i) for i in range(120)], library=LIB)
    opens.clear()
    backend.reconcile()
    assert not store.pending()
    # Well under one open per item: the pass reads the catalogue a few
    # times and commits acknowledgements by the batch.
    assert len(opens) < 40
