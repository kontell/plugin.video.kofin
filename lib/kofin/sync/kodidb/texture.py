# -*- coding: utf-8 -*-
"""Kodi texture-cache rows for chapter thumbnails.

Not fork code: the fork never wrote ``Textures*.db``. The contract is
reproduced from Kodi's own source and bench-verified on both supported
generations (docs/chapter-thumbnails-feasibility.md §2.3, §4): a ``texture``
row whose empty ``imagehash``/``lasthashcheck`` marks the entry trusted
forever, one ``sizes`` row with ``size=1`` (the lookup INNER JOINs on it), and
the image file under ``Thumbnails/`` at a CRC-named relative path. Deleting
the ``texture`` row cascades the ``sizes`` row through Kodi's own
``textureDelete`` trigger (part of both schema fixtures).

The cache key is what the bookmarks dialog asks the GUI to render — the raw
``chapter://{dynpath}/{n}`` string on Omega, the canonical
``image://video@{encoded}/?chapter={n}`` URL on Piers — so byte-exact
reproduction of Kodi's URL encoder and CRC is what this module exists for.
"""

import sqlite3
from typing import List, Optional, Tuple

from kofin.core.log import Logger
from kofin.sync.kodidb import queries_texture as QUTEX

LOG = Logger(__name__)

# URIUtils::URLEncode keeps ASCII alphanumerics plus its RFC1738 set and
# percent-encodes every other byte with lowercase hex.
_ENCODE_KEEP = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._!()"
)

_CRC_POLY = 0x04C11DB7


def crc32_mpeg2(data: bytes) -> int:
    """Kodi's ``Crc32``: MSB-first, init ``0xFFFFFFFF``, no final xor."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ _CRC_POLY) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


def kodi_urlencode(value: str) -> str:
    """``URIUtils::URLEncode`` byte-for-byte (UTF-8 bytes, lowercase hex)."""
    parts: List[str] = []
    for char in value:
        if char in _ENCODE_KEEP:
            parts.append(char)
        else:
            parts.extend("%%%02x" % byte for byte in char.encode("utf-8"))
    return "".join(parts)


def chapter_art_key(dynpath: str, chapter: int, wrapped: bool) -> str:
    """The cache key the bookmarks dialog will look up for ``chapter``
    (1-based, matching the player's numbering) of the playback at
    ``dynpath``. ``wrapped`` comes from ``schema.CHAPTER_ART_WRAPPED``."""
    if wrapped:
        return "image://video@%s/?chapter=%d" % (kodi_urlencode(dynpath), chapter)
    return "chapter://%s/%d" % (dynpath, chapter)


def cached_rel_path(key: str) -> str:
    """Where the image file lives relative to ``Thumbnails/``:
    ``CTextureCache::GetCacheFile`` (CRC of the lowercased key) plus the
    ``.jpg`` the cache job appends — Jellyfin chapter images are always
    JPEG."""
    hexcrc = "%08x" % crc32_mpeg2(key.lower().encode("utf-8"))
    return "%s/%s.jpg" % (hexcrc[0], hexcrc)


class TextureCache(object):
    """Row-level access; file placement stays with the caller."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.cursor = cursor

    def add(self, url: str, cachedurl: str, width: int, height: int) -> None:
        """Publish a pre-cached image: replace any row for ``url``, then
        insert the texture/sizes pair. The row is the publish step — it must
        land only after the image file exists at ``cachedurl``."""
        self.cursor.execute(QUTEX.delete_cache, (url,))
        self.cursor.execute(QUTEX.add_cache, (url, cachedurl))
        self.cursor.execute(QUTEX.add_size, (self.cursor.lastrowid, width, height))

    def remove(self, url: str) -> Optional[str]:
        """Drop the row pair for ``url``; returns its ``cachedurl`` (for the
        caller to remove the file) or None when no row existed."""
        self.cursor.execute(QUTEX.get_cache, (url,))
        row = self.cursor.fetchone()
        if row is None:
            return None
        self.cursor.execute(QUTEX.delete_cache, (url,))
        cachedurl: str = row[0]
        return cachedurl

    def remove_like(self, pattern: str) -> List[Tuple[str, str]]:
        """Drop every row whose url matches the SQL LIKE ``pattern``;
        returns the removed ``(url, cachedurl)`` pairs."""
        self.cursor.execute(QUTEX.get_cache_like, (pattern,))
        rows: List[Tuple[str, str]] = [
            (row[0], row[1]) for row in self.cursor.fetchall()
        ]
        for url, _cachedurl in rows:
            self.cursor.execute(QUTEX.delete_cache, (url,))
        return rows
