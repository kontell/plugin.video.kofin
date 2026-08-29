"""L2 clean suite: the migration cleaner against pristine Kodi schemas.

Invariants from docs/clean-databases-plan.md: cleaning a pristine database is
a byte-identical no-op; cleaning a fully written one lands byte-identical on
the creation-time fixture state; cleaning a database jellyfin-kodi's reset
left *below* pristine (seed rows deleted) repairs it. Plus the file-side
sweeps — prefix-owned nodes and playlists die, hand-made files survive, the
user-nodes toggle removes the whole tree — and the dialog flow's guards and
step ordering.
"""

import os
import sqlite3

import pytest
import xbmcgui

from kofin.plugin import clean as plugin_clean
from kofin.plugin.router import Request
from kofin.sync import clean
from kofin.sync import db as sync_db
from kofin.sync import schema
from kofin.sync.kodidb.kodi import Kodi
from kofin.sync.kodidb.texture import TextureCache
from kofin.sync.writers import Music, MusicVideos, TVShows
from kofin.sync.hooks import pipeline_hooks

HOOKS = pipeline_hooks()
from tests.unit import kodifixtures
from tests.unit.fakes import FakeAddon, FakeWindow
from tests.unit.sync_dtos import (
    ALBUM,
    ARTIST,
    EPISODE,
    MOVIE,
    MUSICVIDEO,
    MUSIC_LIBRARY,
    MV_LIBRARY,
    SEASON_1,
    SERIES,
    SONG,
    TV_LIBRARY,
    dto,
)
from tests.unit.test_sync_writers import (
    FakeApi,
    FakeMonitor,
    dump,
    music_dump,
    write_boxset,
    write_movie,
)


@pytest.fixture(
    params=[
        (kodifixtures.VIDEO_VERSION, kodifixtures.MUSIC_VERSION),
        (kodifixtures.PIERS_VIDEO_VERSION, kodifixtures.PIERS_MUSIC_VERSION),
        (kodifixtures.PIERS_VIDEO_VERSION_147, kodifixtures.PIERS_MUSIC_VERSION),
    ],
    ids=["omega", "piers", "piers147"],
)
def sync_env(request, monkeypatch, tmp_path):
    video_version, music_version = request.param
    FakeAddon.store = {
        "enableCoverArt": "true",
        "compressArt": "false",
        "maxArtResolution": "0",
    }
    FakeWindow.store = {"kofin.online": "true"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("kofin.sync.shims._monitor", FakeMonitor())
    monkeypatch.setattr("xbmcvfs.exists", lambda path: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: str(tmp_path))

    Kodi.reset_people_cache()
    sync_db.reset_overrides()
    schema.reset_cache()

    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    sync_db.set_path_override(
        "video",
        kodifixtures.create_video_db(
            str(tmp_path / ("MyVideos%d.db" % video_version)), video_version
        ),
    )
    sync_db.set_path_override(
        "music",
        kodifixtures.create_music_db(
            str(tmp_path / ("MyMusic%d.db" % music_version)), music_version
        ),
    )
    yield
    sync_db.reset_overrides()
    Kodi.reset_people_cache()


@pytest.fixture
def api():
    fake = FakeApi()
    fake.items_by_id = {
        "movie1": dto(MOVIE),
        "series1": dto(SERIES),
        "artist1": dto(ARTIST),
        "album1": dto(ALBUM),
    }
    fake.seasons_by_series = {"series1": [dto(SEASON_1)]}
    fake.boxset_children = {"set1": [dto(MOVIE)]}
    return fake


def _video_path():
    return str(sync_db._path_overrides["video"])


def _music_path():
    return str(sync_db._path_overrides["music"])


def _build_full_video(api):
    write_movie(api)
    write_boxset(api)
    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        shows = TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS)
        shows.tvshow(dto(SERIES))
        shows.season(dto(SEASON_1))
        shows.episode(dto(EPISODE))
        MusicVideos(api, kdb, vdb, library=MV_LIBRARY, hooks=HOOKS).musicvideo(
            dto(MUSICVIDEO)
        )


def _build_full_music(api):
    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS)
        music.artist(dto(ARTIST))
        music.album(dto(ALBUM))
        music.song(dto(SONG))


# --- wipe-to-pristine invariants ---------------------------------------------


def test_clean_pristine_video_is_noop(sync_env):
    pristine = dump(_video_path())
    clean.clean_video_database()
    assert dump(_video_path()) == pristine


def test_clean_pristine_music_is_noop(sync_env):
    pristine = music_dump(_music_path())
    clean.clean_music_database()
    assert music_dump(_music_path()) == pristine


def test_clean_full_video_lands_pristine(sync_env, api):
    pristine = dump(_video_path())
    _build_full_video(api)
    assert dump(_video_path()) != pristine
    clean.clean_video_database()
    assert dump(_video_path()) == pristine


def test_clean_full_music_lands_pristine(sync_env, api):
    pristine = music_dump(_music_path())
    _build_full_music(api)
    assert music_dump(_music_path()) != pristine
    clean.clean_music_database()
    assert music_dump(_music_path()) == pristine


def test_clean_repairs_subpristine_music(sync_env):
    """jellyfin-kodi's reset deletes the creation-time seed rows (plan G2);
    cleaning such a database must land pristine, not stay damaged."""
    pristine = music_dump(_music_path())
    conn = sqlite3.connect(_music_path())
    conn.execute("DELETE FROM role")
    conn.execute("DELETE FROM artist")
    conn.commit()
    conn.close()
    clean.clean_music_database()
    assert music_dump(_music_path()) == pristine


def test_clean_removes_user_videoversiontype_rows(sync_env):
    """Seed types (owner 0) survive; user-created types are wiped-era data."""
    pristine = dump(_video_path())
    conn = sqlite3.connect(_video_path())
    conn.execute(
        "INSERT INTO videoversiontype (name, owner, itemType) VALUES (?, ?, 0)",
        ("Custom Cut", schema.VIDEO_ASSET_OWNER_USER),
    )
    conn.commit()
    conn.close()
    clean.clean_video_database()
    assert dump(_video_path()) == pristine


def test_wipe_music_refuses_unknown_version(sync_env):
    conn = sqlite3.connect(_music_path())
    conn.execute("UPDATE version SET idVersion = 999")
    conn.commit()
    conn.close()
    with pytest.raises(schema.SchemaUnsupported):
        clean.clean_music_database()


def test_music_debris_detection(sync_env):
    assert clean.music_debris_present() is False
    conn = sqlite3.connect(_music_path())
    conn.execute(
        "INSERT INTO path (idPath, strPath) VALUES (1, 'https://srv/Audio/abc/')"
    )
    conn.commit()
    conn.close()
    assert clean.music_debris_present() is True


# --- file sweeps --------------------------------------------------------------


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("x")


def test_sweep_nodes_matrix(tmp_path):
    root = tmp_path / "video"
    _touch(str(root / "kofin" / "index.xml"))
    _touch(str(root / "kofin" / "kofinmoviesabc" / "all.xml"))
    _touch(str(root / "kofinmovieslegacy" / "index.xml"))
    _touch(str(root / "kofin_Favoritemovies.xml"))
    _touch(str(root / "jellyfinmoviesabc" / "index.xml"))
    _touch(str(root / "jellyfin_Favoritemovies.xml"))
    _touch(str(root / "movies" / "index.xml"))
    _touch(str(root / "files.xml"))
    _touch(str(root / "handmade.xml"))

    removed = clean.sweep_nodes(str(root))

    survivors = sorted(os.listdir(str(root)))
    assert survivors == ["files.xml", "handmade.xml", "movies"]
    assert os.path.exists(str(root / "movies" / "index.xml"))
    assert len(removed) == 5


def test_sweep_nodes_missing_root_is_noop(tmp_path):
    assert clean.sweep_nodes(str(tmp_path / "absent")) == []


def test_sweep_music_nodes_matrix(tmp_path):
    """Kodi keeps a second node tree under library/music and the video sweep
    never reaches it, so the kofin folder there walked straight through a
    "Clean databases" — one stale file when the music side was a single node,
    a whole per-library tree now."""
    root = tmp_path / "music"
    _touch(str(root / "kofin" / "index.xml"))
    _touch(str(root / "kofin" / "kofinmusiclibm" / "songs.xml"))
    _touch(str(root / "kofin" / "kofin_Downloaded" / "artists.xml"))
    _touch(str(root / "kofin_DownloadedMusic.xml"))  # the pre-folder layout
    _touch(str(root / "jellyfin_something.xml"))
    _touch(str(root / "artists.xml"))
    _touch(str(root / "handmade.xml"))

    removed = clean.sweep_music_nodes(str(root))

    assert sorted(os.listdir(str(root))) == ["artists.xml", "handmade.xml"]
    assert len(removed) == 3


def test_sweep_music_nodes_missing_root_is_noop(tmp_path):
    assert clean.sweep_music_nodes(str(tmp_path / "absent")) == []


def test_remove_all_nodes(tmp_path):
    base = tmp_path / "library"
    _touch(str(base / "video" / "movies" / "index.xml"))
    _touch(str(base / "video" / "handmade.xml"))
    _touch(str(base / "music" / "custom.xml"))

    removed = clean.remove_all_nodes(str(base))

    assert not os.path.exists(str(base / "video"))
    assert not os.path.exists(str(base / "music"))
    assert os.path.isdir(str(base))
    assert len(removed) == 2


def test_sweep_playlists(tmp_path):
    """Both managed folders and both addons' flat files, with the user's own
    left alone. The folders are named rather than swept -- "Kofin" does not
    match the lower-case prefix the flat sweep uses."""
    base = tmp_path / "playlists"
    _touch(str(base / "video" / "Kofin" / "kofinmoviesabc.xsp"))
    _touch(str(base / "video" / "kofinmoviesabc.xsp"))  # pre-folder layout
    _touch(str(base / "video" / "jellyfinmoviesabc.xsp"))
    _touch(str(base / "video" / "mylist.xsp"))
    _touch(str(base / "music" / "Kofin" / "Leo.m3u8"))
    _touch(str(base / "music" / "own.m3u8"))

    removed = clean.sweep_playlists(str(base))

    assert sorted(os.listdir(str(base / "video"))) == ["mylist.xsp"]
    assert sorted(os.listdir(str(base / "music"))) == ["own.m3u8"]
    assert len(removed) == 4


def test_remove_jellyfin_state(tmp_path):
    for name in ("jellyfin.db", "jellyfin.db-wal", "jellyfin.db-shm"):
        _touch(str(tmp_path / name))
    _touch(str(tmp_path / "MyVideos131.db"))

    removed = clean.remove_jellyfin_state(str(tmp_path))

    assert sorted(os.listdir(str(tmp_path))) == ["MyVideos131.db"]
    assert len(removed) == 3
    assert clean.remove_jellyfin_state(str(tmp_path)) == []


def test_remove_kofin_state(tmp_path, monkeypatch):
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: str(tmp_path))
    for name in ("kofin.db", "kofin.db-wal", "kofin.db-shm", "sync.json"):
        _touch(str(tmp_path / name))
    _touch(str(tmp_path / "settings.xml"))

    removed = clean.remove_kofin_state()

    assert sorted(os.listdir(str(tmp_path))) == ["settings.xml"]
    assert len(removed) == 4


def _download_row(jellyfin_id, rel_path):
    from kofin.downloads import store

    return store.Download(jellyfin_id=jellyfin_id, rel_path=rel_path)


def test_remove_downloads_takes_the_files_and_prunes(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    season = root / "Show" / "Season 01"
    os.makedirs(str(season))
    _touch(str(season / "ep1.mkv"))
    _touch(str(season / "ep1.nfo"))  # sidecar, shares the stem
    _touch(str(season / "ep1.en.srt"))
    monkeypatch.setattr("kofin.downloads.downloads_root", lambda: str(root))
    monkeypatch.setattr(
        "kofin.downloads.manager.downloads_root", lambda: str(root), raising=False
    )
    monkeypatch.setattr(
        "kofin.downloads.store.rows",
        lambda state=None: [_download_row("a", "Show/Season 01/ep1.mkv")],
    )

    assert clean.remove_downloads() == 1
    # The tree it created goes with it, back to (not including) the root.
    assert os.path.isdir(str(root))
    assert not os.path.exists(str(root / "Show"))


def test_remove_downloads_leaves_foreign_files_alone(tmp_path, monkeypatch):
    # The downloads root is user-configurable and may be shared with other
    # media, so the sweep is per-row, never a recursive delete of the root.
    root = tmp_path / "media"
    os.makedirs(str(root / "Show"))
    _touch(str(root / "Show" / "ours.mkv"))
    _touch(str(root / "Show" / "theirs.mkv"))
    _touch(str(root / "holiday.mp4"))
    monkeypatch.setattr("kofin.downloads.downloads_root", lambda: str(root))
    monkeypatch.setattr(
        "kofin.downloads.manager.downloads_root", lambda: str(root), raising=False
    )
    monkeypatch.setattr(
        "kofin.downloads.store.rows",
        lambda state=None: [_download_row("a", "Show/ours.mkv")],
    )

    clean.remove_downloads()

    assert os.path.exists(str(root / "Show" / "theirs.mkv"))
    assert os.path.exists(str(root / "holiday.mp4"))
    assert not os.path.exists(str(root / "Show" / "ours.mkv"))


def test_downloads_present_counts_and_survives_an_unreadable_store(monkeypatch):
    monkeypatch.setattr(
        "kofin.downloads.store.rows",
        lambda state=None: [_download_row("a", "x.mkv"), _download_row("b", "y.mkv")],
    )
    assert clean.downloads_present() == 2

    def boom(state=None):
        raise RuntimeError("no kofin.db")

    monkeypatch.setattr("kofin.downloads.store.rows", boom)
    # Never blocks the clean: no store means no prompt, not a crash.
    assert clean.downloads_present() == 0


def test_remove_downloads_refuses_to_guess_at_an_unreadable_store(monkeypatch):
    # Raising aborts the clean with kofin.db still on disk. Swallowing would
    # delete the mapping and orphan every file while reporting success.
    def boom(state=None):
        raise RuntimeError("no kofin.db")

    monkeypatch.setattr("kofin.downloads.store.rows", boom)
    with pytest.raises(RuntimeError):
        clean.remove_downloads()


def test_remove_downloads_keeps_going_past_one_bad_file(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    os.makedirs(str(root))
    monkeypatch.setattr("kofin.downloads.downloads_root", lambda: str(root))
    monkeypatch.setattr(
        "kofin.downloads.store.rows",
        lambda state=None: [_download_row("a", "a.mkv"), _download_row("b", "b.mkv")],
    )

    calls = []

    def flaky(row):
        calls.append(row.jellyfin_id)
        if row.jellyfin_id == "a":
            raise OSError("read-only filesystem")

    monkeypatch.setattr("kofin.downloads.manager.delete_media_files", flaky)

    assert clean.remove_downloads() == 1
    assert calls == ["a", "b"]


def test_clear_sync_settings(monkeypatch):
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    FakeAddon.store = {name: "stale" for name in clean.SYNC_SETTINGS}

    clean.clear_sync_settings()

    assert all(FakeAddon.store[name] == "" for name in clean.SYNC_SETTINGS)


@pytest.mark.parametrize("texture_version", [13, 14])
def test_purge_server_art(tmp_path, monkeypatch, texture_version):
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: str(tmp_path))
    sync_db.reset_overrides()
    texture_path = kodifixtures.create_texture_db(
        str(tmp_path / ("Textures%d.db" % texture_version)), texture_version
    )
    sync_db.set_path_override("texture", texture_path)
    thumbs = tmp_path / "Thumbnails"

    rows = [
        ("http://srv:8096/Items/abc/Images/Primary?tag=1", "a/abc.jpg"),
        ("https://srv/Items/def/Images/Backdrop/0?tag=2", "d/def.jpg"),
        ("/home/user/.kodi/addons/some.addon/icon.png", "b/icon.png"),
    ]
    with sync_db.Database("texture") as opened:
        cache = TextureCache(opened.cursor)
        for url, cachedurl in rows:
            cache.add(url, cachedurl, 100, 100)
    for _url, cachedurl in rows:
        _touch(str(thumbs / cachedurl))

    try:
        removed = clean.purge_server_art(str(thumbs))
    finally:
        sync_db.reset_overrides()

    assert removed == 2
    assert not os.path.exists(str(thumbs / "a/abc.jpg"))
    assert not os.path.exists(str(thumbs / "d/def.jpg"))
    assert os.path.exists(str(thumbs / "b/icon.png"))
    conn = sqlite3.connect(texture_path)
    remaining = conn.execute("SELECT url FROM texture").fetchall()
    sizes = conn.execute("SELECT COUNT(*) FROM sizes").fetchone()[0]
    conn.close()
    assert remaining == [("/home/user/.kodi/addons/some.addon/icon.png",)]
    assert sizes == 1


# --- dialog flow --------------------------------------------------------------


class FlowDialog:
    answers = []
    defaults_seen = []
    oks = []

    def yesno(self, heading, message, defaultbutton=None):
        FlowDialog.defaults_seen.append(defaultbutton)
        return FlowDialog.answers.pop(0)

    def ok(self, heading, message):
        FlowDialog.oks.append(message)


class FlowProgress:
    def create(self, heading, message=""):
        pass

    def update(self, percent, message=""):
        pass

    def iscanceled(self):
        return False

    def close(self):
        pass


TEXTS = {30655: "unsupported %s", 30815: "also delete %s downloads?"}


@pytest.fixture
def flow_env(monkeypatch):
    FakeAddon.store = {}
    FlowDialog.answers = []
    FlowDialog.defaults_seen = []
    FlowDialog.oks = []
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Dialog", FlowDialog)
    monkeypatch.setattr("xbmcgui.DialogProgress", FlowProgress)
    monkeypatch.setattr(
        plugin_clean, "_text", lambda sid: TEXTS.get(sid, "msg-%d" % sid)
    )

    env = {"builtins": [], "toasts": [], "conditions": [], "ops": []}
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: env["builtins"].append(cmd))
    monkeypatch.setattr(
        "kofin.core.toast.show", lambda *a, **k: env["toasts"].append(a)
    )
    monkeypatch.setattr("kofin.core.state.is_sync_active", lambda: False)

    def cond(expression):
        env["conditions"].append(expression)
        return env.get("jellyfin_enabled", False)

    monkeypatch.setattr("xbmc.getCondVisibility", cond)

    def op(name, result=None):
        def recorded(*args, **kwargs):
            env["ops"].append(name)
            return result

        return recorded

    monkeypatch.setattr("kofin.sync.clean.preflight", op("preflight"))
    monkeypatch.setattr(
        "kofin.sync.clean.music_debris_present",
        lambda: env.get("music_debris", False),
    )
    monkeypatch.setattr("kofin.sync.clean.clean_video_database", op("video"))
    monkeypatch.setattr("kofin.sync.clean.clean_music_database", op("music"))
    monkeypatch.setattr(
        "kofin.sync.clean.downloads_present", lambda: env.get("downloads", 0)
    )
    monkeypatch.setattr("kofin.sync.clean.remove_downloads", op("downloads", 0))
    monkeypatch.setattr("kofin.sync.clean.remove_kofin_state", op("kofin_state", []))
    monkeypatch.setattr(
        "kofin.sync.clean.remove_jellyfin_state", op("jellyfin_state", [])
    )
    monkeypatch.setattr("kofin.sync.clean.clear_sync_settings", op("settings"))
    monkeypatch.setattr("kofin.sync.clean.sweep_nodes", op("sweep_nodes", []))
    monkeypatch.setattr("kofin.sync.clean.remove_all_nodes", op("all_nodes", []))
    monkeypatch.setattr("kofin.sync.clean.sweep_playlists", op("playlists", []))
    monkeypatch.setattr("kofin.sync.clean.purge_server_art", op("art", 0))
    return env


REQ = Request("plugin://plugin.video.kofin/", -1, {})


def test_flow_refuses_when_logged_in(flow_env):
    FakeAddon.store = {"isLoggedIn": "true"}
    plugin_clean.clean_databases(REQ)
    assert flow_env["toasts"]
    assert flow_env["ops"] == []
    assert flow_env["builtins"] == []


def test_flow_refuses_enabled_jellyfin_kodi(flow_env):
    flow_env["jellyfin_enabled"] = True
    plugin_clean.clean_databases(REQ)
    assert any("plugin.video.jellyfin" in expr for expr in flow_env["conditions"])
    assert FlowDialog.oks == ["msg-30654"]
    assert flow_env["ops"] == []


def test_flow_declined_scope_confirm_touches_nothing(flow_env):
    FlowDialog.answers = [False]
    plugin_clean.clean_databases(REQ)
    assert flow_env["ops"] == ["preflight"]
    assert flow_env["builtins"] == []


def test_flow_full_run_order_and_restart(flow_env):
    FlowDialog.answers = [True, True, False, True]  # scope, music, nodes, art
    plugin_clean.clean_databases(REQ)
    assert flow_env["ops"] == [
        "preflight",
        "video",
        "music",
        "kofin_state",
        "jellyfin_state",
        "settings",
        "sweep_nodes",
        "playlists",
        "art",
    ]
    assert FlowDialog.oks == ["msg-30665"]
    assert flow_env["builtins"] == ["RestartApp"]


def test_flow_user_nodes_toggle_swaps_the_sweep(flow_env):
    FlowDialog.answers = [True, False, True, False]
    plugin_clean.clean_databases(REQ)
    assert "all_nodes" in flow_env["ops"]
    assert "sweep_nodes" not in flow_env["ops"]
    assert "music" not in flow_env["ops"]
    assert "art" not in flow_env["ops"]


def test_flow_music_prompt_default_follows_detection(flow_env):
    flow_env["music_debris"] = True
    FlowDialog.answers = [True, False, False, False]
    plugin_clean.clean_databases(REQ)
    assert FlowDialog.defaults_seen[1] == xbmcgui.DLG_YESNO_YES_BTN


def test_flow_no_downloads_asks_nothing_about_them(flow_env):
    # Four prompts, not five: an empty store has nothing to offer.
    FlowDialog.answers = [True, False, False, False]
    plugin_clean.clean_databases(REQ)
    assert len(FlowDialog.defaults_seen) == 4
    assert "downloads" not in flow_env["ops"]


def test_flow_downloads_deleted_before_the_mapping_dies(flow_env):
    # The ordering is the whole point: the download table lives in kofin.db,
    # so once remove_kofin_state runs nothing knows which files were ours.
    flow_env["downloads"] = 3
    FlowDialog.answers = [True, False, False, False, True]
    plugin_clean.clean_databases(REQ)
    assert flow_env["ops"].index("downloads") < flow_env["ops"].index("kofin_state")
    assert flow_env["ops"].index("video") < flow_env["ops"].index("downloads")


def test_flow_downloads_prompt_defaults_to_yes(flow_env):
    # Keeping them is the answer that leaves unmanaged litter behind.
    flow_env["downloads"] = 2
    FlowDialog.answers = [True, False, False, False, True]
    plugin_clean.clean_databases(REQ)
    assert FlowDialog.defaults_seen[4] == xbmcgui.DLG_YESNO_YES_BTN


def test_flow_declined_downloads_are_left_on_disk(flow_env):
    flow_env["downloads"] = 2
    FlowDialog.answers = [True, False, False, False, False]
    plugin_clean.clean_databases(REQ)
    assert "downloads" not in flow_env["ops"]
    assert "kofin_state" in flow_env["ops"]  # the rest of the clean still runs
