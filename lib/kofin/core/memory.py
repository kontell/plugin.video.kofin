"""Hand freed heap back to the operating system where the C library allows.

CPython returns empty pymalloc arenas, but anything over 512 bytes -- a
payload string, a large dict -- is malloc'd, and glibc keeps a freed block
in the heap unless asked. After an enumeration held 22,000 items, Kodi's
resident set on the LibreELEC box stayed 150 MB above where it started,
and the page cache that every cold plugin interpreter and every store open
depends on was squeezed out: the card then re-read hundreds of megabytes a
minute and one album listing took 20 s. ``release`` is a no-op where there
is no glibc ``malloc_trim``.
"""

import gc

from kofin.core.log import Logger

LOG = Logger(__name__)


def release() -> bool:
    """Collect garbage and trim the heap; True when the C library trimmed."""
    gc.collect()
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        trimmed = bool(libc.malloc_trim(0))
    except Exception as error:
        LOG.debug("heap trim unavailable: %s", error)
        return False
    return trimmed
