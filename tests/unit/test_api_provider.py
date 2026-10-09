"""Scanner callbacks, playback identity lookups and the URL layout."""

from kofin.sync.backends.api import identity, paths
from kofin.sync.backends.api.paths import Location
from kofin.sync.backends.api.store import namespace
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    LIB2,
    SHOW,
    SHOW2,
    backend,
    boxset,
    episode,
    kodi,
    methods,
    movie,
    musicvideo,
    season,
    series,
    show_bundle,
    store,
)


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
    # The movies root is folders only; the movie's folder lists the movie.
    assert [(e[0], e[2]) for e in rendered[-1]] == [
        (paths.movie_dir(store.namespace, LIB, "a"), True)
    ]
    provider.serve(Request(paths.movie_dir(store.namespace, LIB, "a"), 1, {}))
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

    monkeypatch.setattr(identity, "current_store", lambda: store)
    monkeypatch.setattr(libraryclaim.buildconfig, "BACKEND", "api")
    store.publish(show_bundle() + [musicvideo()], library=LIB)
    assert libraryclaim.library_video_path("ea11", "episode") is None
    backend.reconcile()
    episode_id = store.mapping("ea11").kodi_id
    assert identity.mapped_item(episode_id, "episode") == "ea11"
    assert identity.mapped_item(episode_id, "movie") is None
    assert identity.native_id_for("ea11", "episode") == episode_id
    assert identity.native_id_for("m", "musicvideo") == store.mapping("m").kodi_id
    assert libraryclaim.library_video_path("ea11", "episode") == paths.playback_url(
        store.namespace, "Episode", LIB, "ea11", SHOW
    )
    assert libraryclaim.library_video_path("m", "song") is None
    kodi.rows["Episode"][episode_id]["file"] = "/foreign.mkv"
    assert identity.mapped_item(episode_id, "episode") is None


def test_namespace_and_urls_are_server_user_and_library_scoped():
    assert namespace("server", "a") != namespace("server", "b")
    key = namespace("server", "a")
    url = paths.playback_url(key, "Episode", LIB, "e", SHOW)
    assert url.startswith(paths.show_dir(key, LIB, SHOW))
    assert paths.parse(url) == Location(key, LIB, "tvshows", SHOW)
    assert paths.parse(paths.library_dir(key, LIB, "movies")) == Location(
        key, LIB, "movies"
    )
    assert paths.parse(paths.root(key)) == Location(key)
    assert paths.parse("plugin://plugin.video.kofin/?mode=play") is None
    # A movie folder under a movies root is a location of its own.
    assert paths.parse(paths.library_dir(key, LIB, "movies") + SHOW + "/") == Location(
        key, LIB, "movies", movie=SHOW
    )
    assert paths.parse(paths.library_dir(key, LIB, "musicvideos") + SHOW + "/") is None
    assert paths.key_from_url(url) == key
