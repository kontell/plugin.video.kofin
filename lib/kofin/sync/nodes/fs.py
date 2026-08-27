"""The one place the node generator deletes anything.

Every file and folder kofin generates under Kodi's library and playlist
trees carries the ``kofin`` prefix, and every deletion here is gated on it:
hand-made node files and the user's own playlists share those directories
and are never ours to remove. The two prefix-less files kofin owns -- a
parent folder's ``index.xml`` and the managed playlist folder's icon -- go
only when the caller names them (``also``), on a full teardown. A folder is
only ever removed once it is empty, so a foreign entry keeps it alive.

Plain ``os`` throughout: the paths are ``special://profile`` translated to
a local directory on every platform Kodi runs on, and nothing here ever
names a VFS protocol. That is also what lets the generators run in a test
without a Kodi filesystem fake.
"""

import os
from typing import Iterable, List, Tuple

from kofin.core.log import Logger

LOG = Logger(__name__)

# The gate. Spelled once; every managed name is built from it.
PREFIX = "kofin"


def is_managed(name: str) -> bool:
    return name.startswith(PREFIX)


def listdir(root: str) -> Tuple[List[str], List[str]]:
    """``(dirs, files)`` of a directory that may not exist, sorted."""
    dirs: List[str] = []
    files: List[str] = []
    if not os.path.isdir(root):
        return dirs, files
    for entry in sorted(os.listdir(root)):
        if os.path.isdir(os.path.join(root, entry)):
            dirs.append(entry)
        else:
            files.append(entry)
    return dirs, files


def delete_file(path: str, label: str = "node") -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    LOG.info("DELETE %s %s", label, path)


def remove_empty(root: str) -> bool:
    """Remove a directory when nothing is left in it."""
    if not os.path.isdir(root):
        return False
    dirs, files = listdir(root)
    if dirs or files:
        return False
    os.rmdir(root)
    return True


def remove_folder(folder: str, label: str = "node") -> None:
    """A generated folder and the files in it.

    Managed folders only -- the caller has checked the prefix. A hand-made
    subfolder inside keeps the folder itself alive.
    """
    _, files = listdir(folder)
    for name in files:
        delete_file(os.path.join(folder, name), label)
    remove_empty(folder)


def remove_managed_entries(
    root: str,
    keep: Iterable[str] = (),
    also: Iterable[str] = (),
    label: str = "node",
) -> List[str]:
    """Every managed entry under ``root`` not named in ``keep``, plus the
    prefix-less files named in ``also``. Returns what was removed."""
    kept = set(keep)
    extra = set(also)
    removed: List[str] = []
    dirs, files = listdir(root)
    for name in dirs:
        if is_managed(name) and name not in kept:
            remove_folder(os.path.join(root, name), label)
            removed.append(name)
    for name in files:
        if (is_managed(name) and name not in kept) or name in extra:
            delete_file(os.path.join(root, name), label)
            removed.append(name)
    return removed
