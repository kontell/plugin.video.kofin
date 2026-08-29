"""L1 units for the cast-image texture seeder.

The cache-key vectors are not invented: they were read out of the Textures13
database on the Omega test box, from rows *Kodi itself* wrote for actor art
before this module existed. If ``cached_rel_path`` ever stops reproducing
them, the seeder writes files Kodi will never look for and every tile renders
blank — silently, since a missing cache entry just means "fetch it".
"""

import struct
import threading

import pytest

from kofin.service import artcache
from kofin.sync.kodidb.texture import cached_rel_path
from tests.unit.fakes import FakeAddon

# (url as MyVideos stores it, cachedurl as Kodi wrote it) — live Omega rows.
KODI_VECTORS = [
    (
        "https://jelly.konell.xyz/Items/d551685bbf679ecf8942283ae07ad9af/Images/"
        "Primary/0?Format=original&Tag=d48d6afcff384e128fcb03914a014ddc"
        "&MaxWidth=400&MaxHeight=400&Index=0",
        "4/4f91ee51.jpg",
    ),
    (
        "https://jelly.konell.xyz/Items/e1915e01342be8f7bc2bb3dfe8a67fd8/Images/"
        "Primary/0?Format=original&Tag=56a11bb0e9bb732057c8346fa43c8be7"
        "&MaxWidth=400&MaxHeight=400&Index=0",
        "1/15ad15a7.jpg",
    ),
    (
        "https://jelly.konell.xyz/Items/b142503f51637c3d255dc3e0aa0c8b47/Images/"
        "Primary/0?Format=original&Tag=5aae6a1a3eb97a3e41478c3d58bba95a"
        "&MaxWidth=400&MaxHeight=400&Index=0",
        "1/1dc5e0ed.jpg",
    ),
]


def jpeg(width: int, height: int) -> bytes:
    """Smallest bytes that parse as a JPEG with an SOF0 of these dimensions."""
    return (
        b"\xff\xd8"
        + b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x00" * 9
    )


def png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
    )


def test_pending_urls_advances_past_what_is_already_cached(monkeypatch):
    """The work list must move: a LIMIT on the art table alone re-reads the
    same first page every batch, so once that page is cached the seeder claims
    completion with the rest of the library still missing — measured live on
    the first run (186 seeded, then "done" with ~19,000 outstanding)."""
    art_rows = [("http://s/a%d" % n,) for n in range(50)]
    cached_rows = [("http://s/a%d" % n,) for n in range(40)]

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, query, params=()):
            pass

        def fetchall(self):
            return self._rows

    class FakeDatabase:
        def __init__(self, kind):
            self.kind = kind

        def __enter__(self):
            rows = art_rows if self.kind == "video" else cached_rows
            return type("Opened", (), {"cursor": FakeCursor(rows)})()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(artcache, "Database", FakeDatabase)

    pending = artcache.pending_urls(25)
    assert pending == ["http://s/a%d" % n for n in range(40, 50)]


def test_cache_paths_match_the_rows_kodi_wrote_for_actor_art():
    for url, cachedurl in KODI_VECTORS:
        assert cached_rel_path(url) == cachedurl


def test_the_chapter_default_extension_is_unchanged():
    """cached_rel_path grew a parameter; chapter thumbs must keep asking for
    .jpg without passing one (their bench-verified contract)."""
    assert cached_rel_path("chapter://x/1").endswith(".jpg")


class FakeHttp:
    def __init__(self, body=None, fail=False):
        self.body = body if body is not None else jpeg(267, 400)
        self.fail = fail
        self.requests = []

    def request(self, method, url, timeout=None, retries=0, **kwargs):
        self.requests.append(url)
        if self.fail:
            raise OSError("server said no")
        return type("Response", (), {"content": self.body})()

    def close(self):
        pass


@pytest.fixture
def seeder(monkeypatch, tmp_path):
    FakeAddon.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    cache = artcache.ActorArtCache(thumbs_dir=str(tmp_path))
    http = FakeHttp()
    cache._http = http
    return cache, http, tmp_path


def test_seed_writes_the_file_before_the_row(seeder, monkeypatch):
    """A row is a promise that the file is there, so a crash between the two
    must leave an unseeded image rather than a broken cache entry."""
    cache, http, tmp_path = seeder
    url = KODI_VECTORS[0][0]
    monkeypatch.setattr(artcache, "pending_urls", lambda limit: [url])

    order = []

    class RecordingCache:
        def __init__(self, cursor):
            pass

        def add(self, url, cachedurl, width, height):
            order.append(("row", url, cachedurl, width, height))

    class FakeDatabase:
        def __init__(self, kind):
            pass

        def __enter__(self):
            return type("Opened", (), {"cursor": None})()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(artcache, "TextureCache", RecordingCache)
    monkeypatch.setattr(artcache, "Database", FakeDatabase)

    assert cache.seed_batch() == 1

    written = tmp_path / KODI_VECTORS[0][1]
    assert written.exists() and written.read_bytes() == http.body
    assert order == [("row", url, KODI_VECTORS[0][1], 267, 400)]


def test_a_failed_download_seeds_nothing_for_that_url(seeder, monkeypatch):
    cache, _http, tmp_path = seeder
    cache._http = FakeHttp(fail=True)
    monkeypatch.setattr(artcache, "pending_urls", lambda limit: ["http://s/x"])

    rows = []

    class RecordingCache:
        def __init__(self, cursor):
            pass

        def add(self, *args):
            rows.append(args)

    class FakeDatabase:
        def __init__(self, kind):
            pass

        def __enter__(self):
            return type("Opened", (), {"cursor": None})()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(artcache, "TextureCache", RecordingCache)
    monkeypatch.setattr(artcache, "Database", FakeDatabase)

    assert cache.seed_batch() == 0
    assert rows == []
    assert list(tmp_path.iterdir()) == []


def test_halt_stops_the_batch_between_images(seeder, monkeypatch):
    """stop() must be prompt: the flag is checked before every fetch, so a
    shutdown never waits for a whole batch."""
    cache, http, _tmp = seeder
    monkeypatch.setattr(
        artcache,
        "pending_urls",
        lambda limit: ["http://s/%d" % n for n in range(10)],
    )
    cache._halt.set()

    assert cache.seed_batch() == 0
    assert http.requests == []


def test_the_trickle_never_runs_during_playback(monkeypatch):
    """The seeder must not compete with a stream for the same server."""
    cache = artcache.ActorArtCache(thumbs_dir="/tmp")

    class PlayingPlayer:
        def isPlaying(self):
            return True

    monkeypatch.setattr(artcache.xbmc, "Player", PlayingPlayer)
    monkeypatch.setattr(artcache.xbmc, "getGlobalIdleTime", lambda: 9999)
    assert cache._idle() is False

    class IdlePlayer:
        def isPlaying(self):
            return False

    monkeypatch.setattr(artcache.xbmc, "Player", IdlePlayer)
    monkeypatch.setattr(artcache.xbmc, "getGlobalIdleTime", lambda: 5)
    assert cache._idle() is False  # box in use
    monkeypatch.setattr(artcache.xbmc, "getGlobalIdleTime", lambda: 120)
    assert cache._idle() is True


def _not_playing(monkeypatch):
    """Kodistubs answers isPlaying() True, so a test that means "nothing is
    playing" has to say so."""
    monkeypatch.setattr(
        artcache.xbmc,
        "Player",
        lambda: type("P", (), {"isPlaying": lambda self: False})(),
    )


def test_seed_all_runs_to_exhaustion(monkeypatch):
    """The button means "do it all"; the trickle means "a batch at a time"."""
    _not_playing(monkeypatch)
    batches = [3, 3, 1, 0]
    seen = []
    cache = artcache.ActorArtCache(thumbs_dir="/tmp")

    def fake_batch(limit=artcache.BATCH):
        value = batches[len(seen)]
        seen.append(value)
        return value

    monkeypatch.setattr(cache, "seed_batch", fake_batch)
    assert cache.seed_all() == 7


def test_seed_all_stops_when_halted(monkeypatch):
    """A service shutdown mid-button-run must end promptly."""
    _not_playing(monkeypatch)
    cache = artcache.ActorArtCache(thumbs_dir="/tmp")

    def fake_batch(limit=artcache.BATCH):
        cache._halt.set()
        return 5

    monkeypatch.setattr(cache, "seed_batch", fake_batch)
    assert cache.seed_all() == 5


def test_the_trickle_and_the_button_never_fetch_in_parallel(monkeypatch):
    """Two doors onto the same work: run at once and both compute the same
    list and fetch the same images (seen live). The instance lock is what
    makes the second caller read a list the first has already shortened."""
    cache = artcache.ActorArtCache(thumbs_dir="/tmp")
    overlap = []
    inside = threading.Event()

    def slow_batch(limit):
        overlap.append(inside.is_set())
        inside.set()
        threading.Event().wait(0.05)
        inside.clear()
        return 0

    monkeypatch.setattr(cache, "_seed_batch", slow_batch)
    threads = [threading.Thread(target=cache.seed_batch) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert overlap == [False, False, False, False]


def test_an_image_the_server_refuses_is_not_retried_all_evening(seeder, monkeypatch):
    """A library carries art rows whose image the server has since lost —
    404/500, measured live. Nothing ever caches them, so without a skip list
    the work list hands back the same dead URLs on every pass and a batch
    spends itself on them."""
    from kofin.core.http import HttpError

    cache, _http, _tmp = seeder
    attempts = []

    class GoneHttp:
        def request(self, method, url, timeout=None, retries=0, **kwargs):
            attempts.append(url)
            raise HttpError(404, "GET %s -> 404" % url)

        def close(self):
            pass

    cache._http = GoneHttp()
    monkeypatch.setattr(artcache, "pending_urls", lambda limit: ["http://s/gone"])

    assert cache.seed_batch() == 0
    assert cache.seed_batch() == 0
    assert attempts == ["http://s/gone"]  # asked once, not twice


def test_a_transport_failure_is_retried_next_batch(seeder, monkeypatch):
    """The network coming back is the normal case; only the server's own
    refusal is remembered."""
    cache, _http, _tmp = seeder
    attempts = []

    class FlakyHttp:
        def request(self, method, url, timeout=None, retries=0, **kwargs):
            attempts.append(url)
            raise OSError("connection reset")

        def close(self):
            pass

    cache._http = FlakyHttp()
    monkeypatch.setattr(artcache, "pending_urls", lambda limit: ["http://s/flaky"])

    cache.seed_batch()
    cache.seed_batch()
    assert attempts == ["http://s/flaky", "http://s/flaky"]


def test_seed_all_yields_when_playback_starts(monkeypatch):
    """The button must not go on competing with a stream for the same server.
    It stops rather than parking the thread for the length of a film — the
    run is resumable, since a seeded image is skipped next time."""
    cache = artcache.ActorArtCache(thumbs_dir="/tmp")
    playing = {"now": False}
    batches = []

    monkeypatch.setattr(
        artcache.xbmc,
        "Player",
        lambda: type("P", (), {"isPlaying": lambda self: playing["now"]})(),
    )

    def fake_batch(limit=artcache.BATCH):
        batches.append(1)
        if len(batches) == 2:
            playing["now"] = True  # a film starts mid-run
        return 5

    monkeypatch.setattr(cache, "seed_batch", fake_batch)

    assert cache.seed_all() == 10  # the two batches that ran, then it yielded
    assert len(batches) == 2


def test_the_button_does_not_wait_for_an_idle_box(monkeypatch):
    """_idle gates the trickle on idle time; the button must not use it —
    someone who just pressed it has by definition not left the box alone."""
    cache = artcache.ActorArtCache(thumbs_dir="/tmp")
    _not_playing(monkeypatch)
    monkeypatch.setattr(artcache.xbmc, "getGlobalIdleTime", lambda: 0)
    monkeypatch.setattr(cache, "seed_batch", lambda limit=artcache.BATCH: 0)

    assert cache._idle() is False  # the trickle would wait
    assert cache.seed_all() == 0  # the button ran anyway (nothing outstanding)
