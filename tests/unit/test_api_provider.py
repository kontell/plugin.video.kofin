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
    # The root lists every movie as a file under its own folder's URL, never
    # as folders (Kodi would turn them into phantom discs); the folder lists
    # its one movie.
    assert [e[1] for e in rendered[-1]] == ["a"]
    assert rendered[-1][0][0].startswith(paths.movie_dir(store.namespace, LIB, "a"))
    provider.serve(Request(paths.movie_dir(store.namespace, LIB, "a"), 1, {}))
    assert [e[1] for e in rendered[-1]] == ["a"]
    assert content[-4:] == ["tvshows", "episodes", "movies", "movies"]
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


def test_the_tvshows_root_hashes_each_show_from_the_kept_episode_rows(
    store, monkeypatch
):
    """Kodi skips a show folder whose hash matches the one it stored, so the
    hash must be the same whether the episodes came from their payloads or
    from the rows kept for them, and a second listing reads no payload."""
    from kofin.plugin.router import Request
    from kofin.sync.backends.api import metadata, provider

    store.publish(show_bundle(episodes=3) + show_bundle(SHOW2), library=LIB)
    hashes = []
    monkeypatch.setattr(
        provider.metadata,
        "listitem",
        lambda item, server, key, library, separator, seasons, episodes, **k: (
            item["Id"],
            metadata.show_hash(episodes),
        ),
    )
    monkeypatch.setattr(
        provider.xbmcplugin,
        "addDirectoryItems",
        lambda h, items, n: hashes.append({e[1][0]: e[1][1] for e in items}),
    )
    monkeypatch.setattr(provider.xbmcplugin, "setContent", lambda h, c: None)
    monkeypatch.setattr(provider.xbmcplugin, "endOfDirectory", lambda *a, **k: None)
    read = []
    payloads = store.payloads

    def counted(ids):
        ids = list(ids)
        read.extend(ids)
        return payloads(ids)

    monkeypatch.setattr(provider, "Store", lambda key: store)
    monkeypatch.setattr(store, "payloads", counted)
    root = paths.library_dir(store.namespace, LIB, "tvshows")

    provider.serve(Request(root, 1, {}))
    provider.serve(Request(root, 1, {}))

    assert len(read) == 5
    assert (
        hashes[0]
        == hashes[1]
        == {
            show: metadata.show_hash([i for i in items if i["Type"] == "Episode"])
            for show, items in (
                (SHOW, show_bundle(episodes=3)),
                (SHOW2, show_bundle(SHOW2)),
            )
        }
    )


def test_a_catalogue_that_cannot_be_read_answers_that_the_row_exists(
    store, monkeypatch
):
    """Kodi's clean deletes the row of a probe that fails: a malformed
    kofin.db is no word on a row, and the fault still propagates."""
    import os
    import sqlite3

    import pytest

    from kofin.plugin import router
    from kofin.sync import private
    from kofin.sync.backends.api import provider

    store.publish([movie()] + show_bundle(), library=LIB)
    path = private.resolve_path()
    for leftover in (path + "-wal", path + "-shm"):
        if os.path.exists(leftover):
            os.remove(leftover)
    with open(path, "wb") as broken:
        broken.write(b"not a database " * 512)
    resolved, ended = [], []
    monkeypatch.setattr(
        provider.xbmcplugin,
        "setResolvedUrl",
        lambda handle, ok, li: resolved.append((handle, ok)),
    )
    monkeypatch.setattr(
        provider.xbmcplugin,
        "endOfDirectory",
        lambda handle, succeeded=True, **_: ended.append((handle, succeeded)),
    )
    # A movie's file, and a show's folder (the stage after the files).
    probes = (
        (
            paths.movie_dir(store.namespace, LIB, "a"),
            "?mode=play&id=a&kodi_action=check_exists",
        ),
        (paths.show_dir(store.namespace, LIB, SHOW), "?kodi_action=check_exists"),
    )
    for url, query in probes:
        with pytest.raises(sqlite3.DatabaseError):
            router.dispatch([url, "7", query])
    assert resolved == [(7, True), (7, True)]
    assert ended == []


def test_identity_lookups_cover_episodes_and_music_videos(
    store, backend, kodi, monkeypatch
):
    from kofin.service import libraryclaim

    monkeypatch.setattr(identity, "current_store", lambda: store)
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


def test_identity_lookups_cover_shows_and_seasons(store, backend, kodi, monkeypatch):
    """The Jellyfin actions menu on a show or a season reaches the item the
    pass filed it from."""
    from kofin.service import libraryclaim

    monkeypatch.setattr(identity, "current_store", lambda: store)
    store.publish(
        [
            series(),
            season("s1", SHOW, 1),
            season("s2", SHOW, 2),
            episode("e1", SHOW, 1, 1),
            episode("e2", SHOW, 2, 1),
        ],
        library=LIB,
    )
    backend.reconcile()
    show_id = store.mapping(SHOW).kodi_id
    seasons = {
        r["season"]: r["seasonid"]
        for r in kodi.rows["Season"].values()
        if r["tvshowid"] == show_id
    }
    assert identity.mapped_item(show_id, "tvshow") == SHOW
    assert libraryclaim.mapped_jellyfin_id(show_id, "tvshow") == SHOW
    assert identity.mapped_item(seasons[1], "season") == "s1"
    assert identity.mapped_item(seasons[2], "season") == "s2"
    assert identity.mapped_item(show_id, "movie") is None
    kodi.rows["Series"][show_id]["file"] = "/foreign/"
    assert identity.mapped_item(show_id, "tvshow") is None
    assert identity.mapped_item(seasons[2], "season") is None


def test_a_set_stands_for_a_collection_acknowledged_against_it(
    store, backend, kodi, monkeypatch
):
    """Kodi keeps one set per title. Two collections of one title with
    synced members are both acknowledged against it, and the first by id
    stands for it; a collection applied without a row stands for no set,
    though one of its title is in Kodi."""
    monkeypatch.setattr(identity, "current_store", lambda: store)
    store.publish(
        [
            movie(),
            movie("b"),
            boxset("box", "Pair", ["a"]),
            boxset("box2", "Pair", ["b"]),
            # No member synced: applied without a row. "bo" sorts first.
            boxset("bo", "Pair", ["z"]),
            boxset("solo", "Solo", ["y"]),
        ],
        library=LIB,
    )
    backend.reconcile()
    (set_id,) = (r["setid"] for r in kodi.rows["BoxSet"].values())
    assert store.mapping("box").kodi_id == set_id
    assert store.mapping("box2").kodi_id == set_id
    assert identity.mapped_item(set_id, "set") == "box"
    # A movie of someone else's files a set of the other title.
    kodi.rows["Movie"][9000] = {"movieid": 9000, "file": "/films/x.mkv", "set": "Solo"}
    solo_id = kodi._set_row("Solo")["setid"]
    assert identity.mapped_item(solo_id, "set") is None
    # A collection that leaves the catalogue gives the set to the next one.
    store.publish([], removed=["box"])
    assert identity.mapped_item(set_id, "set") == "box2"


def test_syncplay_resolves_a_native_row_through_the_identity_lookup(
    store, backend, kodi, monkeypatch
):
    """SyncPlay identifies a library play from Player.OnPlay's Kodi id."""
    from kofin.syncplay import providers

    monkeypatch.setattr(identity, "current_store", lambda: store)
    store.publish(show_bundle() + [movie()], library=LIB)
    backend.reconcile()
    provider = providers.JellyfinProvider(None)
    episode_id = store.mapping("ea11").kodi_id

    assert provider.resolve_kodi_id(episode_id, "episode") == "ea11"
    assert provider.resolve_kodi_id(store.mapping("a").kodi_id, "movie") == "a"
    # A row of another kind, and one kofin does not own: not ours, so the
    # play is given back at once instead of being held for the group.
    assert provider.resolve_kodi_id(episode_id, "movie") is None
    kodi.rows["Episode"][episode_id]["file"] = "/foreign.mkv"
    assert provider.resolve_kodi_id(episode_id, "episode") is None


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


def test_a_forget_request_is_refused_and_lists_nothing(store, monkeypatch):
    """Kodi keeps no listing of a refused request, and the query is not part
    of its cache key: the refusal is what takes a cached root out."""
    from urllib.parse import parse_qsl, urlsplit

    from kofin.plugin.router import Request
    from kofin.sync.backends.api import provider

    store.publish([movie()], library=LIB)
    root = paths.library_dir(store.namespace, LIB, "movies")
    url = paths.forgetting(root)
    assert url.startswith(root + "?")
    ended, rendered = [], []
    monkeypatch.setattr(
        provider.xbmcplugin,
        "endOfDirectory",
        lambda handle, succeeded=True, **_: ended.append(succeeded),
    )
    monkeypatch.setattr(
        provider.xbmcplugin,
        "addDirectoryItems",
        lambda h, items, n: rendered.append(list(items)),
    )
    provider.serve(Request(root, 1, dict(parse_qsl(urlsplit(url).query))))
    assert ended == [False] and rendered == []


def _refreshes(store, monkeypatch):
    """A show and a movie in the catalogue, the provider's Kodi calls
    recorded, and what it tells the service."""
    from kofin.core import ipc
    from kofin.sync.backends.api import provider

    store.publish(show_bundle() + [movie()], library=LIB)
    rendered = []
    sent = []
    monkeypatch.setattr(provider.metadata, "listitem", lambda item, *a, **k: item["Id"])
    monkeypatch.setattr(
        provider.xbmcplugin,
        "addDirectoryItems",
        lambda h, items, n: rendered.append([e[1] for e in items]),
    )
    monkeypatch.setattr(provider.xbmcplugin, "setContent", lambda h, c: None)
    monkeypatch.setattr(provider.xbmcplugin, "setResolvedUrl", lambda *a: None)
    monkeypatch.setattr(provider.xbmcplugin, "endOfDirectory", lambda *a, **k: None)
    monkeypatch.setattr(
        ipc, "notify", lambda method, data=None: sent.append((method, data))
    )
    return rendered, sent


def test_a_refresh_from_outside_the_pass_is_passed_to_the_service(store, monkeypatch):
    """Kodi asks for a refreshed item the same way whoever pressed Refresh.
    A viewer's is answered from the catalogue like any listing, and the
    service is told to ask the server for the item; the pass's own, which
    names its item first, is the catalogue being filed."""
    from kofin.core import ipc
    from kofin.plugin.router import Request
    from kofin.sync.backends.api import provider

    rendered, sent = _refreshes(store, monkeypatch)
    refreshing = [""]
    monkeypatch.setattr(provider.state, "native_refresh", lambda: refreshing[0])
    show = paths.show_dir(store.namespace, LIB, SHOW)
    film = paths.movie_dir(store.namespace, LIB, "a")
    refresh = {"kodi_action": "refresh_info"}

    provider.serve(Request(show, 1, dict(refresh)))
    provider.serve(Request(show, 1, dict(refresh, id="ea12")))
    provider.serve(Request(film, 1, dict(refresh, id="a", mode="play")))
    assert rendered == [[SHOW], ["ea12"], ["a"]]
    assert sent == [
        (ipc.REFRESH_ITEMS, {"Id": SHOW}),
        (ipc.REFRESH_ITEMS, {"Id": "ea12"}),
        (ipc.REFRESH_ITEMS, {"Id": "a"}),
    ]

    # The pass's own refresh of the episode, and a viewer's of the movie
    # while the pass waits for it.
    del sent[:]
    refreshing[0] = "ea12"
    provider.serve(Request(show, 1, dict(refresh, id="ea12")))
    assert rendered[-1] == ["ea12"]
    assert sent == []
    provider.serve(Request(film, 1, dict(refresh, id="a", mode="play")))
    assert sent == [(ipc.REFRESH_ITEMS, {"Id": "a"})]


def test_only_a_refresh_that_was_answered_is_passed_on(store, monkeypatch):
    from kofin.plugin.router import Request
    from kofin.sync.backends.api import provider

    rendered, sent = _refreshes(store, monkeypatch)
    monkeypatch.setattr(provider.state, "native_refresh", lambda: "")
    show = paths.show_dir(store.namespace, LIB, SHOW)

    # A scan's listing, a clean's probe, and an item the catalogue lacks.
    provider.serve(Request(show, 1, {}))
    provider.serve(Request(show, 1, {"kodi_action": "check_exists", "id": "ea11"}))
    provider.serve(Request(show, 1, {"kodi_action": "refresh_info", "id": "gone"}))
    provider.serve(
        Request(
            paths.show_dir(store.namespace, LIB, SHOW2),
            1,
            {"kodi_action": "refresh_info"},
        )
    )
    assert sent == []


def test_a_refresh_is_answered_when_the_service_cannot_be_told(store, monkeypatch):
    """The listing is handed over first, and nothing after it can fail the
    refresh Kodi is waiting on."""
    from kofin.core import ipc
    from kofin.plugin.router import Request
    from kofin.sync.backends.api import provider

    rendered, _ = _refreshes(store, monkeypatch)

    def unreadable():
        raise RuntimeError("no window")

    def unsent(method, data=None):
        raise RuntimeError("no bus")

    film = paths.movie_dir(store.namespace, LIB, "a")
    request = {"kodi_action": "refresh_info", "id": "a", "mode": "play"}
    monkeypatch.setattr(ipc, "notify", unsent)
    provider.serve(Request(film, 1, dict(request)))
    monkeypatch.setattr(provider.state, "native_refresh", unreadable)
    provider.serve(Request(film, 1, dict(request)))
    assert rendered == [["a"], ["a"]]
