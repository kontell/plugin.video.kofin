"""The addon backdrop: the server's splashscreen, or the bundled artwork.

The API profile writes content-addressed artwork only in addon_data; dynamic
listings use that URL. The manifest fanart stays bundled. The remaining notes
below describe the legacy SQL profile, retained until its feature port.

``addon.xml`` names ``resources/media/fanart.webp`` as the addon's fanart, and
Kodi reads that manifest once at install/scan time — there is no runtime API to
repoint an asset. So the backdrop is changed the only way it can be: by
rewriting the file the manifest already names.

Everything here is WebP, and the extension is load-bearing: Kodi picks an image
decoder from it for local files, so WebP bytes under a ``.png`` name fail to
decode outright on Omega. ``Api.splashscreen`` records why the format is worth
the rename; ``addon.xml`` records why the manifest accepts it.

Restoring it therefore needs an unmodified copy, and that copy is *captured*
into addon_data rather than shipped beside the original. Shipping a second
identical copy would mean two sources of truth for one image, which drift the
first time somebody updates the artwork and edits only one of them. Nor can
the shipped asset simply be generated on first run instead: Kodi's addon
browser fetches ``<assets>`` straight from the repository over HTTP *before
the addon is installed*, so a manifest naming a file that is not in the zip
shows no fanart at all until someone installs it.

The capture is taken whenever the recorded state is absent or stale, because
both of those mean nobody has swapped the file since the addon was unpacked —
a fresh install, or an update that has just rewritten ``resources/``. That
also repairs the copy on every addon update, for free.

Three things make that safe to do repeatedly:

* **A content hash gates the write.** The splashscreen endpoint serves no
  ETag or Last-Modified, so there is no conditional GET to make; the fetch
  always costs its bytes. What the hash saves is the *write* and the cache
  invalidation behind it, which is the part with a visible cost — see below.
* **A daily floor gates the fetch.** ``_connect`` runs on every service start
  and every reconnect, and a flapping server would otherwise re-download a
  700KB image each time.
* **The write is atomic.** Kodi may be reading the file to cache it; a
  partial write would cache a truncated image and the hash would then say
  everything is fine.

Invalidating the texture cache is not optional. Kodi keys cached textures on
the source url, so a file rewritten underneath it keeps rendering the old
bytes until its own hash check falls due — which is on no timescale a user
would connect to having changed the setting.

The image is taken in the one encoding the endpoint can usefully be asked for;
:meth:`Api.splashscreen` records why that is WebP and why it is not Jpg.
"""

import hashlib
import json
import os
from typing import Optional

from kofin.core import settings
from kofin.core.api import Api
from kofin.core.log import Logger

LOG = Logger(__name__)

# The asset addon.xml names (shipped), and the captured copy of it as bundled
# (written to addon_data at runtime — see the module docstring).
LIVE_NAME = "fanart.webp"
DEFAULT_NAME = "fanart-default.webp"
MEDIA_DIR = os.path.join("resources", "media")

# The PNG names these replaced. An addon update rewrites resources/, so the old
# live asset goes with it, but the captured copy sits in addon_data where no
# update reaches — left alone it would outlive the format for good.
LEGACY_DEFAULT_NAME = "fanart-default.png"

# Records what the live file currently holds, so an unchanged splashscreen
# costs nothing and a restore is not repeated. In addon_data rather than a
# hidden setting: an open settings dialog discards external setting writes on
# Cancel, and this is written from a worker thread at arbitrary times.
STATE_NAME = "backdrop.json"

# Source markers for the state file.
SOURCE_SERVER = "server"
SOURCE_DEFAULT = "default"

# Floor between fetches. The splashscreen is a collage the server rebuilds as
# the library changes, so it does move — just never faster than this matters.
REFRESH_INTERVAL_SECONDS = 24 * 60 * 60


def _state_path() -> str:
    return os.path.join(settings.addon_data_path(), STATE_NAME)


def _read_state() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as handle:
            state: dict = json.load(handle)
            return state
    except (OSError, ValueError):
        return {}


def _write_state(source: str, digest: str, fetched: float) -> None:
    payload = {"source": source, "hash": digest, "fetched": fetched}
    try:
        directory = settings.addon_data_path()
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(_state_path(), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except OSError as error:
        # Only costs a redundant fetch/write next time round.
        LOG.warning("backdrop state not written: %s", error)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def apply(api: Optional[Api], now: float, force: bool = False) -> None:
    """Bring the live asset in line with the setting. Never raises.

    ``force`` skips the daily floor; it is what the settings toggle uses, so
    switching the option on acts immediately rather than at the next connect.
    """
    try:
        _apply_profile(api, now, force)
    except Exception:
        LOG.exception("backdrop update failed")


def profile_path() -> str:
    """Current dynamic-listing fanart; the installed manifest asset stays fixed."""
    if not settings.get_bool("useServerBackdrop"):
        return ""
    digest = str(_read_state().get("profile_hash") or "")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return ""
    path = os.path.join(settings.addon_data_path(), "fanart-" + digest + ".webp")
    return path if os.path.isfile(path) else ""


def _apply_profile(api: Optional[Api], now: float, force: bool) -> None:
    if api is None or not settings.get_bool("useServerBackdrop"):
        return
    previous = _read_state()
    if (
        not force
        and profile_path()
        and now - float(previous.get("fetched") or 0) < REFRESH_INTERVAL_SECONDS
    ):
        return
    if not api.branding_configuration().get("SplashscreenEnabled"):
        _write_state(SOURCE_DEFAULT, "", now)
        return
    data = api.splashscreen()
    if not data:
        return
    # Content-addressed URLs need no texture-cache mutation. Only Kofin's
    # profile is writable; addon.xml continues to name its bundled image.
    directory = settings.addon_data_path()
    os.makedirs(directory, exist_ok=True)
    digest = _digest(data)
    target = os.path.join(directory, "fanart-" + digest + ".webp")
    with open(target + ".part", "wb") as handle:
        handle.write(data)
    os.replace(target + ".part", target)
    state_path = _state_path()
    with open(state_path + ".part", "w", encoding="utf-8") as handle:
        json.dump({"profile_hash": digest, "fetched": now}, handle)
    os.replace(state_path + ".part", state_path)
