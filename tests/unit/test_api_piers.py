"""The pass on a Piers: the baseline, every Kodi 23 feature off.

What a Kodi lacks is off by name, never an error (CLAUDE.md, the capability
profile): a property Kodi does not name is never asked for, a setter field
it does not take is never sent nor compared, and the same profile moved to
a Kodi that has them compares again and patches what Piers could not.
"""

import xbmcgui

from kofin.sync.backends.api import capabilities, metadata, paths, provider
from kofin.sync.backends.api.kinds import properties
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    backend,
    boxset,
    kodi,
    methods,
    movie,
    piers,
    show_bundle,
    song,
    store,
)
from tests.unit.test_api_assets import assets_movie, folder_of, movie_rows


def _requested(kodi, method):
    return [set(params.get("properties") or []) for params in methods(kodi, method)]


def test_the_readback_properties_follow_the_feature_table():
    assert "tag" not in properties("Episode", frozenset())
    assert "tag" in properties("Episode", frozenset({"episode_tags"}))
    assert "plot" not in properties("Season", frozenset())
    assert "plot" in properties("Season", capabilities.ALL)
    assert properties("Movie", frozenset()) == properties("Movie", capabilities.ALL)
    minimal = properties("Episode", frozenset(), minimal=True)
    assert "file" in minimal and "tag" not in minimal


def _plotted_show_with_a_favourite():
    items = show_bundle(seasons=(1,), episodes=2)
    next(item for item in items if item["Type"] == "Season")["Overview"] = "An arc"
    next(item for item in items if item["Type"] == "Episode")["UserData"] = {
        "IsFavorite": True
    }
    return items


def test_a_piers_imports_a_show_without_asking_for_what_it_lacks(store, backend, piers):
    store.publish(_plotted_show_with_a_favourite(), library=LIB)
    backend.setup()
    backend.reconcile()
    assert not store.pending()
    _pending, failed, error = store.outstanding()
    assert failed == 0 and not error
    # The fake refuses what a Piers refuses; beyond that, nothing asked for
    # a season's plot or an episode's tag, and nothing sent one.
    for method in ("VideoLibrary.GetEpisodes", "VideoLibrary.GetEpisodeDetails"):
        assert all("tag" not in wanted for wanted in _requested(piers, method))
    for method in ("VideoLibrary.GetSeasons", "VideoLibrary.GetSeasonDetails"):
        assert all("plot" not in wanted for wanted in _requested(piers, method))
    assert all(
        "plot" not in sent for sent in methods(piers, "VideoLibrary.SetSeasonDetails")
    )
    assert all(
        "tag" not in sent for sent in methods(piers, "VideoLibrary.SetEpisodeDetails")
    )
    assert piers.rows["Season"] and all(
        not row["plot"] for row in piers.rows["Season"].values()
    )
    assert piers.rows["Episode"] and all(
        "tag" not in row for row in piers.rows["Episode"].values()
    )


def test_the_same_profile_on_a_kodi_23_patches_what_piers_could_not(
    store, backend, piers, monkeypatch
):
    """The feature set is an input of every acknowledgement: a Kodi upgrade
    compares the rows again, and the plot and the tag land."""
    store.publish(_plotted_show_with_a_favourite(), library=LIB)
    backend.setup()
    backend.reconcile()
    assert not store.pending()
    monkeypatch.setattr(capabilities, "features", lambda: capabilities.ALL)
    piers.calls.clear()
    backend.reconcile()
    assert not store.pending()
    assert any(
        sent.get("plot") == "An arc"
        for sent in methods(piers, "VideoLibrary.SetSeasonDetails")
    )
    assert any(
        "Favorite episodes" in (sent.get("tag") or [])
        for sent in methods(piers, "VideoLibrary.SetEpisodeDetails")
    )
    assert [row["plot"] for row in piers.rows["Season"].values()] == ["An arc"]
    tagged = [
        row
        for row in piers.rows["Episode"].values()
        if "Favorite episodes" in row["tag"]
    ]
    assert len(tagged) == 1


def test_the_scanner_format_is_the_one_this_kodi_reaches():
    assert metadata.scanner_format(frozenset()) == metadata.SCANNER_FORMAT - 1
    assert metadata.scanner_format(capabilities.ALL) == metadata.SCANNER_FORMAT


def test_a_piers_lists_a_movie_s_own_file_and_its_extras_and_no_version(store, piers):
    """Without video_versions a Kodi cannot group a further file under the
    movie, so the folder lists the movie's file alone and the default
    source plays; the extras folder is Kodi 22's too, and a version file
    an earlier Kodi filed is nothing to keep."""
    store.publish([assets_movie()], library=LIB)
    record = store.records(kind="Movie", item_ids=["a"])["a"]
    entries = provider._movie_entries(
        record, "http://s", store.namespace, LIB, " / ", None, True
    )
    urls = [url for url, _item, _folder in entries]
    assert urls == [
        paths.playback_url(store.namespace, "Movie", LIB, "a"),
        paths.extras_dir(store.namespace, LIB, "a"),
    ]
    folder = folder_of(store)
    assert provider.exists(store, paths.parse(folder), "a", "s2") is False
    assert provider.exists(store, paths.parse(folder), "a", "") is True
    token = metadata.assets_token(assets_movie())
    assert token and token == metadata.assets_token(
        dict(assets_movie(), MediaSources=assets_movie()["MediaSources"][:1])
    )
    assert "s2" not in str(metadata.asset_files(assets_movie(), store.namespace, LIB))


def test_a_piers_leaves_dropped_assets_to_the_viewers_own_clean(store, backend, piers):
    """Without asset_clean (kontell/xbmc #1, #2, #10) a clean of the folder
    would ask about every bound folder of every library and delete what goes
    unanswered, so the pass cleans nothing, acknowledges the listing it
    presented, and says so once; the rows stay until the movie leaves."""
    backend.setup()
    store.publish([assets_movie()], library=LIB)
    backend.reconcile()
    assert not store.pending()
    (row,) = movie_rows(piers)
    assert len(piers.extras[row["movieid"]]) == 2
    item = assets_movie(
        SpecialFeatures=[assets_movie()["SpecialFeatures"][1]], SpecialFeatureCount=1
    )
    store.publish([item], library=LIB)
    piers.calls.clear()
    backend.reconcile()
    assert not store.pending()
    assert piers.cleaned == [] and not methods(piers, "VideoLibrary.Clean")
    assert store.mapping("a").applied["assets"] == metadata.assets_token(item)


def test_a_piers_leaves_an_emptied_set_for_kodis_own_clean(store, backend, piers):
    """Without remove_set (VideoLibrary.RemoveMovieSet, kontell/xbmc #7) the
    empty row stays: it is in no listing, and Kodi's Clean library drops it.
    The collection's removal is acknowledged all the same."""
    store.publish([movie(), movie("b"), boxset("box", "Pair", ["a", "b"])], library=LIB)
    backend.reconcile()
    assert piers.owned("Movie")["a"]["set"] == "Pair"
    piers.calls.clear()
    store.publish([], removed=["box"])
    backend.reconcile()
    assert not store.pending()
    assert piers.owned("Movie")["a"]["set"] == ""
    assert not methods(piers, "VideoLibrary.RemoveMovieSet")
    assert not methods(piers, "VideoLibrary.Clean")
    assert store.state("box").status == "applied"


def test_a_piers_listing_sets_no_stream_flags_and_no_song_date(monkeypatch):
    """The stream detail classes take no flags and the music tag no date
    added without the features, as the stubs here have none: a listing that
    asked would fail on every row."""
    streams = [
        {"Type": "Video", "Codec": "hevc"},
        {"Type": "Audio", "Codec": "aac", "Language": "eng", "IsDefault": True},
        {"Type": "Subtitle", "Language": "eng", "IsForced": True},
    ]
    flagged = movie(MediaStreams=streams)
    metadata.listitem(flagged, "http://s", "k", LIB)
    metadata.song_listitem(song("t1"))

    class Detail:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(xbmcgui, "ListItem", xbmcgui.ListItem)
    monkeypatch.setattr(metadata.xbmc, "AudioStreamDetail", Detail, raising=False)
    monkeypatch.setattr(metadata.xbmc, "SubtitleStreamDetail", Detail, raising=False)
    metadata.listitem(flagged, "http://s", "k", LIB)


def test_a_piers_answers_no_for_an_extra_the_server_dropped(store, backend, piers):
    """Kodi 22's Clean library asks the add-on about an extra's file (the
    extras folder is bound), so an extra the movie's payload no longer names
    must answer no, whatever the feature set: the only way its row leaves."""
    backend.setup()
    store.publish([assets_movie()], library=LIB)
    backend.reconcile()
    folder = folder_of(store)
    kept, dropped = assets_movie()["SpecialFeatures"]
    name_kept = "extras/%s.%s" % (kept["Name"], kept["Container"])
    name_dropped = "extras/%s.%s" % (dropped["Name"], dropped["Container"])
    assert provider.exists(store, paths.parse(folder + name_kept), "", "", frozenset())
    assert provider.exists(store, paths.parse(folder + name_dropped), "", "", frozenset())
    item = assets_movie(SpecialFeatures=[kept], SpecialFeatureCount=1)
    store.publish([item], library=LIB)
    backend.reconcile()
    assert provider.exists(store, paths.parse(folder + name_kept), "", "", frozenset())
    assert (
        provider.exists(store, paths.parse(folder + name_dropped), "", "", frozenset())
        is False
    )
