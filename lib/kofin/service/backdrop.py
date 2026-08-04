"""The addon backdrop: the server's splashscreen, or the bundled artwork.

``addon.xml`` names ``resources/media/fanart.png`` as the addon's fanart, and
Kodi reads that manifest once at install/scan time — there is no runtime API to
repoint an asset. So the backdrop is changed the only way it can be: by
rewriting the file the manifest already names.

Restoring it therefore needs an unmodified copy, and that copy is *captured*
into addon_data rather than shipped beside the original. Shipping a second
identical PNG would mean two sources of truth for one image, which drift the
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
  2.3MB image each time.
* **The write is atomic.** Kodi may be reading the file to cache it; a
  partial write would cache a truncated image and the hash would then say
  everything is fine.

Invalidating the texture cache is not optional. Kodi keys cached textures on
the source url, so a file rewritten underneath it keeps rendering the old
bytes until its own hash check falls due — which is on no timescale a user
would connect to having changed the setting.

The image is taken exactly as the server encodes it; :meth:`Api.splashscreen`
records why no transcode parameters are asked for.
"""

import hashlib
import json
import os
from typing import Optional

from kofin.core import kodirpc, settings
from kofin.core.api import Api
from kofin.core.http import JellyfinError
from kofin.core.log import Logger

LOG = Logger(__name__)

# The asset addon.xml names (shipped), and the captured copy of it as bundled
# (written to addon_data at runtime — see the module docstring).
LIVE_NAME = "fanart.png"
DEFAULT_NAME = "fanart-default.png"
MEDIA_DIR = os.path.join("resources", "media")

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


def live_path() -> str:
    return os.path.join(settings.addon_path(), MEDIA_DIR, LIVE_NAME)


def default_path() -> str:
    return os.path.join(settings.addon_data_path(), DEFAULT_NAME)


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


def _live_digest() -> str:
    """Hash of the asset as it actually is on disk, or "" if unreadable."""
    try:
        with open(live_path(), "rb") as handle:
            return _digest(handle.read())
    except OSError:
        return ""


def _install(data: bytes, source: str, now: float) -> bool:
    """Put ``data`` in the live asset and invalidate the cached texture.

    Written to a sibling temp file and renamed, because Kodi may be reading
    the asset to cache it and a half-written PNG would be cached as-is — with
    the hash then asserting the backdrop is up to date.
    """
    if _live_digest() == _digest(data):
        # Already exactly this. Record the state so the caller's bookkeeping is
        # right, but do not touch the file or drop the texture: re-caching a
        # 2.3MB image for no visible change is the cost this avoids.
        _write_state(source, _digest(data), now)
        return True

    target = live_path()
    temp = target + ".part"
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
        os.replace(temp, target)
    except OSError as error:
        LOG.warning("backdrop write failed (%s); leaving the current image", error)
        try:
            os.remove(temp)
        except OSError:
            pass
        return False

    # Narrowed by addon id *and* filename: other addons ship a fanart.png too
    # (plugin.video.jellyfin has one on this very box), and evicting theirs is
    # not ours to do.
    kodirpc.drop_cached_texture(settings.ADDON_ID, require=LIVE_NAME)
    _write_state(source, _digest(data), now)
    LOG.info("backdrop updated from %s (%s bytes)", source, len(data))
    return True


def _capture_default() -> None:
    """Snapshot the live asset as the bundled artwork.

    Only ever called when the caller has established that nothing has swapped
    the asset since it was unpacked, so what is on disk *is* what shipped.
    Overwrites any previous capture, which is how an addon update carrying new
    artwork replaces a snapshot of the old.
    """
    try:
        with open(live_path(), "rb") as handle:
            data = handle.read()
    except OSError as error:
        LOG.warning("backdrop: bundled artwork unreadable (%s); not captured", error)
        return
    target = default_path()
    temp = target + ".part"
    try:
        directory = settings.addon_data_path()
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(temp, "wb") as handle:
            handle.write(data)
        os.replace(temp, target)
        LOG.debug("backdrop: captured bundled artwork (%s bytes)", len(data))
    except OSError as error:
        # Without it the setting cannot be turned back off until the next
        # addon update offers another chance to capture.
        LOG.warning("backdrop: bundled artwork not captured (%s)", error)


def _restore_default(now: float, state: dict) -> None:
    """Put the bundled artwork back, unless it is already live."""
    if state.get("source") == SOURCE_DEFAULT:
        return
    try:
        with open(default_path(), "rb") as handle:
            data = handle.read()
    except OSError as error:
        # The capture is taken on first run and refreshed on every addon
        # update, so this means addon_data was cleared while a server image
        # was live — leave that in place rather than blank the backdrop.
        LOG.warning(
            "bundled backdrop unreadable (%s); leaving the current image", error
        )
        return
    _install(data, SOURCE_DEFAULT, now)


def apply(api: Optional[Api], now: float, force: bool = False) -> None:
    """Bring the live asset in line with the setting. Never raises.

    ``force`` skips the daily floor; it is what the settings toggle uses, so
    switching the option on acts immediately rather than at the next connect.
    """
    try:
        _apply(api, now, force)
    except Exception:
        LOG.exception("backdrop update failed")


def _apply(api: Optional[Api], now: float, force: bool) -> None:
    state = _read_state()

    # An addon update replaces the whole resources/ tree, putting the bundled
    # artwork back under a state file that still claims the server's splash is
    # installed — and the daily floor would then leave it wrong for a day.
    # Kodi's texture cache makes it worse than cosmetic: it keeps serving the
    # image it cached, so disk, state and screen can disagree three ways.
    # Re-hashing the asset costs one local read of a file we would otherwise
    # be downloading, and it is the only way to notice.
    if state and state.get("hash") != _live_digest():
        LOG.info("backdrop asset changed underneath us (addon update?); reinstalling")
        state = {}

    # No recorded state means nothing has swapped the asset since it was
    # unpacked — a first run, or the drift reset just above after an update
    # rewrote resources/. Either way the file on disk is the shipped artwork,
    # and this is the only moment we can be sure of that, so it is the moment
    # to snapshot it. An update carrying new artwork refreshes the snapshot on
    # the same path.
    if not state:
        _capture_default()

    if not settings.get_bool("useServerBackdrop"):
        _restore_default(now, state)
        return

    if api is None:
        LOG.debug("backdrop: no api yet; leaving the current image")
        return

    age = now - float(state.get("fetched") or 0.0)
    if not force and state.get("source") == SOURCE_SERVER:
        if age < REFRESH_INTERVAL_SECONDS:
            LOG.debug("backdrop checked %.0fs ago; skipping", age)
            return

    try:
        branding = api.branding_configuration()
    except JellyfinError as error:
        LOG.warning("backdrop: branding config unavailable (%s)", error)
        return

    if not branding.get("SplashscreenEnabled"):
        # The server has the feature switched off, so there is nothing of its
        # own to show. Falling back beats leaving a stale splash in place.
        LOG.info("backdrop: server splashscreen disabled; using bundled artwork")
        _restore_default(now, state)
        return

    try:
        data = api.splashscreen()
    except JellyfinError as error:
        LOG.warning("backdrop: splashscreen fetch failed (%s)", error)
        return

    if not data:
        LOG.warning("backdrop: splashscreen came back empty")
        return

    if state.get("source") == SOURCE_SERVER and state.get("hash") == _digest(data):
        # Unchanged: record the check so the floor advances, but do not touch
        # the file or drop the cached texture — that would make every connect
        # re-cache a 2.3MB image for no visible change.
        LOG.debug("backdrop unchanged; skipping write")
        _write_state(SOURCE_SERVER, str(state.get("hash")), now)
        return

    _install(data, SOURCE_SERVER, now)
