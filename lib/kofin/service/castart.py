"""Cast images warmed through Kodi's own image cache (the API build).

The SQL build seeds Kodi's texture tables itself (service/artcache.py). This
build may not touch them, so it asks Kodi to cache each image instead:
opening ``image://<url>/`` through the VFS runs ``CTextureCache::CacheImage``
(xbmc/filesystem/ImageFile.cpp, ``CImageFile::Open``), which fetches,
resizes and records the image exactly as a dialog drawing it would. The
video scanner caches a row's cast at import while ``videolibrary.actorthumbs``
is on; this covers what it did not, at idle, a batch at a time, and the
settings button runs it to the end.

The work list is the catalogue's: every portrait of a person on a movie,
show, episode or music video of the selection, spelt as the rows carry it
(``listitems.person_thumb``). What Kodi already holds is read back once a
run through ``Textures.GetTextures`` and left alone.
"""

import threading
from typing import Any, Callable, Iterable, List, Optional, Set
from urllib.parse import quote

import xbmc
import xbmcvfs

from kofin.core import kodirpc, settings
from kofin.core.log import Logger
from kofin.plugin import listitems

LOG = Logger(__name__)

# The same knobs as the SQL seeder, for the same reasons.
SETTING = "precacheActorArt"
IDLE_SECONDS = 60
BATCH = 25
TICK_SECONDS = 15
KINDS = ("Movie", "Series", "Episode", "MusicVideo")


def wrapped(url: str) -> str:
    """The image VFS path Kodi caches a URL under (CTextureUtils::GetWrappedImageURL)."""
    return "image://" + quote(url, safe="") + "/"


def cached_urls(server: str) -> Set[str]:
    """The server's image URLs Kodi's texture cache already holds."""
    reply = kodirpc.call(
        "Textures.GetTextures",
        {
            "properties": ["url"],
            "filter": {"field": "url", "operator": "startswith", "value": server},
        },
    )
    if not isinstance(reply, dict):
        return set()
    return {
        str(t.get("url") or "")
        for t in reply.get("textures") or []
        if isinstance(t, dict) and t.get("url")
    }


def portraits(store: Any, server: str) -> List[str]:
    """Every cast portrait URL the selection's rows carry, each once."""
    from kofin.sync.backends.api.records import PayloadWindow

    seen: Set[str] = set()
    urls: List[str] = []
    records = store.records(kind=KINDS, payloads=False)
    for record in PayloadWindow(store).walk(records[i] for i in sorted(records)):
        for person in record.item.get("People") or []:
            if not isinstance(person, dict):
                continue
            url = listitems.person_thumb(server, person)
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def warm(url: str) -> bool:
    """Ask Kodi to cache one image. True when it holds the image afterwards."""
    handle = None
    try:
        handle = xbmcvfs.File(wrapped(url))
        return handle.size() > 0
    except Exception as error:
        LOG.debug("cast image not cached (%s): %s", url[:80], error)
        return False
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:  # pragma: no cover - close never matters here
                pass


class CastArt:
    """The idle-time warmer and the settings button's worker; the shape of
    service/artcache.ActorArtCache, so the service drives either."""

    def __init__(self, store_factory: Optional[Callable[[], Any]] = None) -> None:
        self._store_factory = store_factory
        self._halt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._working = threading.Lock()
        # URLs Kodi could not cache this generation (a lost or unservable
        # image; a restart retries them), and the ones it cached since the
        # last readback of its texture list.
        self._failed: Set[str] = set()
        self._done: Set[str] = set()

    def _store(self) -> Any:
        if self._store_factory is not None:
            return self._store_factory()
        from kofin.sync.backends.api.identity import current_store

        return current_store()

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._halt.clear()
        self._thread = threading.Thread(target=self._run, name="kofin-castart")
        self._thread.daemon = True
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._halt.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():  # pragma: no cover - watchdog logging only
                LOG.warning("cast image warmer did not stop within its deadline")

    # -- the idle trickle --------------------------------------------------------

    def _run(self) -> None:
        monitor = xbmc.Monitor()
        while not monitor.abortRequested() and not self._halt.is_set():
            if monitor.waitForAbort(TICK_SECONDS):
                return
            if self._halt.is_set() or not self._wanted():
                continue
            if not self._idle():
                continue
            try:
                warmed = self.seed_batch()
            except Exception:
                LOG.exception("cast image warming failed")
                return
            if warmed:
                LOG.debug("warmed %d cast image(s)", warmed)

    @staticmethod
    def _wanted() -> bool:
        try:
            return settings.get_bool(SETTING)
        except TypeError:
            return False

    @staticmethod
    def _playing() -> bool:
        return bool(xbmc.Player().isPlaying())

    def _idle(self) -> bool:
        if self._playing():
            return False
        return xbmc.getGlobalIdleTime() >= IDLE_SECONDS

    # -- the work --------------------------------------------------------------

    def pending(self) -> List[str]:
        store = self._store()
        server = store.server()
        if not server:
            return []
        held = cached_urls(server)
        return [
            url
            for url in portraits(store, server)
            if url not in held and url not in self._failed and url not in self._done
        ]

    def seed_batch(
        self, limit: int = BATCH, urls: Optional[Iterable[str]] = None
    ) -> int:
        """Warm up to ``limit`` images Kodi does not hold; how many landed."""
        with self._working:
            todo = list(urls) if urls is not None else self.pending()
            warmed = 0
            for url in todo[:limit]:
                if self._halt.is_set():
                    break
                if warm(url):
                    warmed += 1
                    self._done.add(url)
                else:
                    self._failed.add(url)
            return warmed

    def seed_all(self) -> int:
        """Everything outstanding, for the settings button; stops for playback."""
        total = 0
        todo = self.pending()
        while todo and not self._halt.is_set():
            if self._playing():
                LOG.info(
                    "cast image warming yielding to playback after %d image(s)", total
                )
                break
            batch, todo = todo[:BATCH], todo[BATCH:]
            total += self.seed_batch(urls=batch)
        LOG.info("cast image warming: %d image(s) cached", total)
        return total
