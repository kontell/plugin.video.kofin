"""Chapter thumbnails: cache-key vectors, the texture writer, and the worker.

The key/CRC vectors are the ones bench-verified against real Kodi installs on
2026-08-03 (docs/chapter-thumbnails-feasibility.md §4): the Omega values from
this box's kofin-test profile, the Piers values from the Bravia. If one of
these assertions breaks, the seeded entries stop matching what the bookmarks
dialog asks for — silently, as blank tiles.
"""

import os
import sqlite3

import pytest

from kofin.core import state
from kofin.service import chapters
from kofin.service.player import Player
from kofin.sync import db as sync_db
from kofin.sync import schema
from kofin.sync.kodidb.texture import (
    TextureCache,
    cached_rel_path,
    chapter_art_key,
    crc32_mpeg2,
    kodi_urlencode,
)
from tests.unit import kodifixtures
from tests.unit.fakes import FakeAddon, FakeWindow

# The dialog lookup Kodi runs (TextureDatabase::GetCachedTexture): the INNER
# JOIN on sizes.size=1 is why the writer must land both rows.
KODI_LOOKUP = (
    "SELECT id, cachedurl, lasthashcheck, imagehash, width, height "
    "FROM texture JOIN sizes ON (texture.id=sizes.idtexture AND sizes.size=1) "
    "WHERE url=?"
)

OMEGA_DYNPATH = (
    "https://jelly.konell.xyz/Videos/ec40da70c171ea60b1b376bd312af9ea/stream.mkv"
    "?static=true&mediaSourceId=ec40da70c171ea60b1b376bd312af9ea"
    "&deviceId=731a0c17b0fe4428bbeadcff0be26c4c"
    "&playSessionId=43cfe963439044098e45fa59f79566db"
)

PIERS_DYNPATH = (
    "http://192.168.1.167:8096/Videos/6971670cd3807b9a28efe29104479aab/stream.mkv"
    "?static=true&mediaSourceId=6971670cd3807b9a28efe29104479aab"
)

PIERS_KEY_1 = (
    "image://video@http%3a%2f%2f192.168.1.167%3a8096%2fVideos"
    "%2f6971670cd3807b9a28efe29104479aab%2fstream.mkv%3fstatic%3dtrue"
    "%26mediaSourceId%3d6971670cd3807b9a28efe29104479aab/?chapter=1"
)


# -- bench-verified vectors ----------------------------------------------------


def test_omega_key_is_the_raw_chapter_url():
    key = chapter_art_key(OMEGA_DYNPATH, 1, wrapped=False)
    assert key == "chapter://%s/1" % OMEGA_DYNPATH
    assert cached_rel_path(key) == "b/b86d5cb1.jpg"


def test_piers_key_is_the_wrapped_image_url():
    assert chapter_art_key(PIERS_DYNPATH, 1, wrapped=True) == PIERS_KEY_1
    assert cached_rel_path(PIERS_KEY_1) == "4/4c26c20a.jpg"
    key2 = chapter_art_key(PIERS_DYNPATH, 2, wrapped=True)
    assert cached_rel_path(key2) == "4/4165e4d3.jpg"


def test_urlencode_matches_kodi():
    assert kodi_urlencode("Az09-._!()") == "Az09-._!()"
    assert kodi_urlencode("://?&=+ ") == "%3a%2f%2f%3f%26%3d%2b%20"
    assert kodi_urlencode("é") == "%c3%a9"  # per-byte UTF-8, lowercase hex
    assert kodi_urlencode("Videos") == "Videos"  # case preserved


def test_crc_is_mpeg2_of_the_lowercased_key():
    assert crc32_mpeg2(b"") == 0xFFFFFFFF
    # Case folding happens in cached_rel_path, not in the key itself.
    assert cached_rel_path("ABC") == cached_rel_path("abc")


def test_wrapped_style_is_keyed_by_texture_schema():
    assert schema.CHAPTER_ART_WRAPPED == {13: False, 14: True}
    assert schema.SUPPORTED["texture"] == {13, 14}


def test_jpeg_size_reads_the_sof_marker():
    sof = (
        b"\xff\xd8"  # SOI
        + b"\xff\xe0\x00\x04\x00\x00"  # APP0, length 4
        + b"\xff\xc0\x00\x11\x08"  # SOF0, length, precision
        + (360).to_bytes(2, "big")
        + (640).to_bytes(2, "big")
        + b"\x00" * 12
    )
    assert chapters.jpeg_size(sof) == (640, 360)
    assert chapters.jpeg_size(b"not a jpeg") == (0, 0)


# -- the texture writer against pristine fixture databases ---------------------


@pytest.fixture(params=[13, 14], ids=["omega", "piers"])
def texture_db(request, tmp_path):
    path = str(tmp_path / ("Textures%d.db" % request.param))
    kodifixtures.create_texture_db(path, request.param)
    return path, request.param


def _query(path, sql, args=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_add_lands_the_row_pair_kodi_looks_up(texture_db):
    path, _version = texture_db
    conn = sqlite3.connect(path)
    TextureCache(conn.cursor()).add("chapter://x/1", "a/abc.jpg", 640, 360)
    conn.commit()
    conn.close()

    rows = _query(path, KODI_LOOKUP, ("chapter://x/1",))
    assert len(rows) == 1
    _id, cachedurl, lasthashcheck, imagehash, width, height = rows[0]
    assert cachedurl == "a/abc.jpg"
    # Empty hash fields are the "trusted forever" marker: GetCachedTexture
    # only surfaces a hash (forcing revalidation) for a valid day-old
    # lasthashcheck, and needsRecaching keys on the hash being non-empty.
    assert imagehash == "" and lasthashcheck == ""
    assert (width, height) == (640, 360)


def test_add_replaces_and_the_trigger_cascades(texture_db):
    path, _version = texture_db
    conn = sqlite3.connect(path)
    cache = TextureCache(conn.cursor())
    cache.add("chapter://x/1", "a/old.jpg", 100, 100)
    cache.add("chapter://x/1", "a/new.jpg", 640, 360)
    conn.commit()
    conn.close()

    assert _query(path, "SELECT COUNT(*) FROM texture")[0][0] == 1
    # Kodi's own textureDelete trigger must have dropped the old sizes row.
    assert _query(path, "SELECT COUNT(*) FROM sizes")[0][0] == 1
    assert _query(path, KODI_LOOKUP, ("chapter://x/1",))[0][1] == "a/new.jpg"


def test_remove_returns_the_cached_path_and_cascades(texture_db):
    path, _version = texture_db
    conn = sqlite3.connect(path)
    cache = TextureCache(conn.cursor())
    cache.add("chapter://x/1", "a/abc.jpg", 640, 360)
    assert cache.remove("chapter://x/1") == "a/abc.jpg"
    assert cache.remove("chapter://x/1") is None
    conn.commit()
    conn.close()

    assert _query(path, "SELECT COUNT(*) FROM texture")[0][0] == 0
    assert _query(path, "SELECT COUNT(*) FROM sizes")[0][0] == 0


def test_remove_like_only_touches_matches(texture_db):
    path, _version = texture_db
    conn = sqlite3.connect(path)
    cache = TextureCache(conn.cursor())
    cache.add("chapter://http://s/stream?deviceId=dev1/1", "a/one.jpg", 1, 1)
    cache.add("chapter://http://s/stream?deviceId=dev1/2", "a/two.jpg", 1, 1)
    cache.add("image://foreign/", "f/foreign.jpg", 1, 1)
    removed = cache.remove_like("chapter://%dev1%")
    conn.commit()
    conn.close()

    assert sorted(rel for _url, rel in removed) == ["a/one.jpg", "a/two.jpg"]
    assert _query(path, "SELECT url FROM texture") == [("image://foreign/",)]


# -- the worker ----------------------------------------------------------------


class FakeApi:
    def __init__(self, chapter_list, image=b"\xff\xd8fake", fail_index=None):
        self.chapter_list = chapter_list
        self.image = image
        self.fail_index = fail_index
        self.downloads = []

    def chapters(self, item_id):
        return self.chapter_list

    def chapter_image_url(self, item_id, index, tag, max_width):
        return "img://%s/%d?tag=%s&w=%d" % (item_id, index, tag, max_width)

    def download(self, url):
        self.downloads.append(url)
        index = int(url.split("/")[-1].split("?")[0])
        if index == self.fail_index:
            raise RuntimeError("boom")
        return self.image


@pytest.fixture
def worker_env(texture_db, tmp_path, monkeypatch):
    path, version = texture_db
    sync_db.reset_overrides()
    sync_db.set_path_override("texture", path)
    monkeypatch.setattr(schema, "check", lambda kind: version)
    thumbs_dir = str(tmp_path / "Thumbnails")
    yield path, version, thumbs_dir
    sync_db.reset_overrides()


def _item(method="DirectStream"):
    return {
        "Id": "item1",
        "Type": "Movie",
        "Path": "http://s/Videos/item1/stream.mkv?deviceId=dev1&playSessionId=ps1",
        "PlayMethod": method,
        "DeviceId": "dev1",
    }


def test_seed_publishes_and_cleanup_reverts(worker_env):
    path, version, thumbs_dir = worker_env
    api = FakeApi(
        [
            {"Name": "One", "ImageTag": "t0"},
            {"Name": "No image"},
            {"Name": "Three", "ImageTag": "t2"},
        ]
    )
    thumbs = chapters.ChapterThumbs(api, _item(), thumbs_dir=thumbs_dir)
    thumbs._run()

    # Image indexes are positions in the full list — the imageless chapter
    # was skipped without shifting its neighbours.
    assert api.downloads == [
        "img://item1/0?tag=t0&w=640",
        "img://item1/2?tag=t2&w=640",
    ]
    wrapped = schema.CHAPTER_ART_WRAPPED[version]
    keys = [chapter_art_key(_item()["Path"], n, wrapped) for n in (1, 3)]
    for key in keys:
        rows = _query(path, KODI_LOOKUP, (key,))
        assert len(rows) == 1
        assert os.path.exists(os.path.join(thumbs_dir, rows[0][1]))

    thumbs._cancel.set()
    thumbs._cleanup()
    assert _query(path, "SELECT COUNT(*) FROM texture")[0][0] == 0
    for key in keys:
        assert not os.path.exists(os.path.join(thumbs_dir, cached_rel_path(key)))


def test_seed_survives_a_failed_download(worker_env):
    path, _version, thumbs_dir = worker_env
    api = FakeApi(
        [{"ImageTag": "t0"}, {"ImageTag": "t1"}, {"ImageTag": "t2"}], fail_index=1
    )
    thumbs = chapters.ChapterThumbs(api, _item(), thumbs_dir=thumbs_dir)
    thumbs._run()
    assert _query(path, "SELECT COUNT(*) FROM texture")[0][0] == 2


def test_cancel_before_publish_writes_no_rows(worker_env):
    path, _version, thumbs_dir = worker_env
    api = FakeApi([{"ImageTag": "t0"}])
    thumbs = chapters.ChapterThumbs(api, _item(), thumbs_dir=thumbs_dir)
    thumbs._cancel.set()
    thumbs._run()
    assert _query(path, "SELECT COUNT(*) FROM texture")[0][0] == 0
    thumbs._cleanup()  # and cleanup with nothing seeded is a no-op


def test_unsupported_texture_schema_skips_quietly(worker_env, monkeypatch):
    path, _version, thumbs_dir = worker_env

    def raise_unsupported(kind):
        raise schema.SchemaUnsupported(kind, 99)

    monkeypatch.setattr(schema, "check", raise_unsupported)
    api = FakeApi([{"ImageTag": "t0"}])
    thumbs = chapters.ChapterThumbs(api, _item(), thumbs_dir=thumbs_dir)
    thumbs._run()  # must not raise
    assert _query(path, "SELECT COUNT(*) FROM texture")[0][0] == 0


def test_eligible_gates_method_type_and_identity():
    assert chapters.eligible(_item())
    assert not chapters.eligible(_item(method="Transcode"))
    assert not chapters.eligible({**_item(), "Type": "Audio"})
    assert not chapters.eligible({**_item(), "Path": ""})
    assert chapters.eligible({**_item(), "Type": "Episode", "PlayMethod": "DirectPlay"})


def test_sweep_removes_only_this_installs_leftovers(worker_env):
    path, _version, thumbs_dir = worker_env
    conn = sqlite3.connect(path)
    cache = TextureCache(conn.cursor())
    raw_key = "chapter://http://s/stream?deviceId=dev1&playSessionId=old/3"
    wrapped_key = "image://video@http%3a%2f%2fs%2fstream%3fdeviceId%3ddev1/?chapter=3"
    cache.add(raw_key, "a/raw.jpg", 1, 1)
    cache.add(wrapped_key, "a/wrapped.jpg", 1, 1)
    cache.add("image://video@smb%3a%2f%2fnas%2ffilm.mkv/?chapter=1", "f/kodi.jpg", 1, 1)
    conn.commit()
    conn.close()
    for rel in ("a/raw.jpg", "a/wrapped.jpg", "f/kodi.jpg"):
        target = os.path.join(thumbs_dir, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as outfile:
            outfile.write(b"x")

    assert chapters.sweep("dev1", thumbs_dir=thumbs_dir) == 2
    assert _query(path, "SELECT url FROM texture") == [
        ("image://video@smb%3a%2f%2fnas%2ffilm.mkv/?chapter=1",)
    ]
    assert not os.path.exists(os.path.join(thumbs_dir, "a/raw.jpg"))
    assert not os.path.exists(os.path.join(thumbs_dir, "a/wrapped.jpg"))
    assert os.path.exists(os.path.join(thumbs_dir, "f/kodi.jpg"))


# -- player wiring -------------------------------------------------------------


class SpyThumbs:
    instances = []

    def __init__(self, api, item, thumbs_dir=None):
        self.item = item
        self.started = False
        self.stopped = False
        SpyThumbs.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class RecordingApi:
    def session_playing(self, data):
        pass

    def session_progress(self, data):
        pass

    def session_stopped(self, data):
        pass


class FakeMonitor:
    def waitForAbort(self, seconds=0):
        return False

    def abortRequested(self):
        return False


@pytest.fixture
def player_env(monkeypatch):
    FakeWindow.store = {}
    FakeAddon.store = {"chapterImages": "true"}
    SpyThumbs.instances = []
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmc.Monitor", FakeMonitor)
    monkeypatch.setattr(
        "xbmc.executeJSONRPC", lambda q: '{"result": {"volume": 77, "muted": false}}'
    )
    monkeypatch.setattr(chapters, "ChapterThumbs", SpyThumbs)


def _play(monkeypatch, method="DirectStream"):
    url = "http://s/Videos/m1/stream.mkv?playSessionId=ps1"
    api = RecordingApi()
    player = Player(api)  # type: ignore[arg-type]
    monkeypatch.setattr(player, "getPlayingFile", lambda: url)
    monkeypatch.setattr(player, "_start_ticker", lambda: None)
    state.push_play_item(
        {
            "Id": "m1",
            "Type": "Movie",
            "Path": url,
            "PlayMethod": method,
            "PlaySessionId": "ps1",
            "MediaSourceId": "src1",
            "DeviceId": "dev1",
            "Runtime": 0,
            "AudioStreamIndex": 1,
            "SubtitleStreamIndex": None,
            "CurrentPosition": 0.0,
        }
    )
    player.onPlayBackStarted()
    return player


def test_player_seeds_on_claim_and_reverts_on_finalize(player_env, monkeypatch):
    player = _play(monkeypatch)
    assert len(SpyThumbs.instances) == 1
    spy = SpyThumbs.instances[0]
    assert spy.started and spy.item["Id"] == "m1"
    player.finalize()
    assert spy.stopped


def test_player_skips_transcode_and_the_off_switch(player_env, monkeypatch):
    _play(monkeypatch, method="Transcode")
    assert SpyThumbs.instances == []

    FakeAddon.store["chapterImages"] = "false"
    _play(monkeypatch)
    assert SpyThumbs.instances == []
