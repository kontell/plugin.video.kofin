"""Cross-process messages over Kodi's NotifyAll bus.

Every message kofin sends is declared here; nothing may notify a string that
is not in the registry. Received methods arrive prefixed by Kodi (e.g.
``Other.Restart``) — :func:`method_name` strips that.
"""

import binascii
import json
import os
import uuid
from typing import Any, Dict, Optional

import xbmc
import xbmcvfs

from kofin.core.log import Logger

LOG = Logger(__name__)

SENDER = "plugin.video.kofin"

RESTART = "Restart"
AUTH_CHANGED = "AuthChanged"

# Library-manager commands (settings buttons / picker -> RunPlugin ->
# ipc.notify -> service). Payloads carry {"Id": "<library id or csv>"}.
SYNC_LIBRARY = "SyncLibrary"
REMOVE_LIBRARY = "RemoveLibrary"
REPAIR_LIBRARY = "RepairLibrary"
UPDATE_LIBRARY = "UpdateLibrary"
REFRESH_BOXSETS = "RefreshBoxsets"
# Settings button: seed the cast-image texture cache now (service/artcache.py).
PRECACHE_ART = "PrecacheArt"

# SyncPlay (phase 4): the root entry's plugin invocation asks the service —
# the single owner of all SyncPlay state — to open the group menu on its
# worker thread. No payload.
SYNCPLAY_MENU = "SyncPlayMenu"

# Who's watching?: same shape, and for the same reason. A plugin invocation
# that blocks on a dialog cannot be reached as a library node — Kodi runs the
# node's <path> as a directory fetch, and the modal fights it. Firing this and
# exiting lets the fetch fail out cleanly while the service puts the picker up.
WHO_IS_WATCHING = "WhoIsWatching"

_REGISTRY = frozenset(
    {
        RESTART,
        AUTH_CHANGED,
        SYNC_LIBRARY,
        REMOVE_LIBRARY,
        REPAIR_LIBRARY,
        UPDATE_LIBRARY,
        REFRESH_BOXSETS,
        PRECACHE_ART,
        SYNCPLAY_MENU,
        WHO_IS_WATCHING,
    }
)

# The messages that cost something irreversible or expensive if forged: rows
# deleted, a whole library re-walked, the service bounced (repeatedly, which
# is a denial of service). Kodi's NotifyAll passes the sender string through
# verbatim from its caller — the builtin and the JSON-RPC method both — so
# "sender == kofin" proves nothing on its own, and these carry a shared secret
# as well (see nonce()).
GUARDED = frozenset({RESTART, AUTH_CHANGED, REMOVE_LIBRARY, REPAIR_LIBRARY})

# Where that secret lives. Deliberately a file in the addon's own data
# directory rather than a window property: a window property is readable over
# JSON-RPC (``XBMC.GetInfoLabels`` on ``Window(10000).Property(...)``), which
# is the very channel the guard exists to close. The bar this sets is honest
# and limited — it stops anything that can reach Kodi's JSON-RPC port or
# blind-fire a NotifyAll, not code already able to read the addon's files.
# Kodi offers no authenticated channel to do better.
NONCE_FILE = "special://profile/addon_data/plugin.video.kofin/ipc.nonce"

NONCE_KEY = "_nonce"


def _nonce_path() -> str:
    return xbmcvfs.translatePath(NONCE_FILE)


def rotate_nonce() -> str:
    """Mint this service generation's secret and write it. Service only.

    Per generation, not per install: a restart invalidates anything that
    captured the old value, and nothing needs to survive one.
    """
    value = uuid.uuid4().hex
    path = _nonce_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Written like every other secret-ish file here: owner-only, and
        # replaced atomically so a reader never sees half a token.
        temporary = path + ".tmp"
        with open(temporary, "w") as handle:
            handle.write(value)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as error:  # pragma: no cover - defensive
        LOG.warning("could not write the IPC nonce: %s", error)
        return ""
    return value


def nonce() -> str:
    """The current secret, or '' when there is none to read."""
    try:
        with open(_nonce_path()) as handle:
            return handle.read().strip()
    except OSError:
        return ""


def verify(method: str, data: Dict[str, Any], expected: str) -> bool:
    """Whether a received guarded message carries the right secret.

    Unguarded messages pass. A guarded one fails when the service has no
    secret to compare against — that is a service which never wrote one, and
    accepting on absence would be a guard anyone can disable by deleting a
    file.
    """
    if method not in GUARDED:
        return True
    if not expected:
        return False
    return str(data.get(NONCE_KEY, "")) == expected


def notify(method: str, data: Optional[Dict[str, Any]] = None) -> None:
    if method not in _REGISTRY:
        raise ValueError("unregistered IPC message: %s" % method)
    payload = dict(data or {})
    if method in GUARDED:
        payload[NONCE_KEY] = nonce()
    xbmc.executebuiltin("NotifyAll(%s, %s, %s)" % (SENDER, method, _encode(payload)))


def _encode(data: Dict[str, Any]) -> str:
    # The builtin parser re-parses its arguments, so the JSON payload is
    # wrapped in a quoted single-element list (same scheme the old addon and
    # AddonSignals use — receivers run json.loads(...)[0]).
    return '"[%s]"' % json.dumps(data).replace('"', '\\"')


def decode(data: str) -> Dict[str, Any]:
    """The payload of a received message, or {} for anything unreadable.

    Never raises: this runs on Kodi's notification thread, where the sender
    is whoever called NotifyAll — including a forger sending deliberate
    rubbish (audit finding #21).
    """
    try:
        payload = json.loads(data)
        if isinstance(payload, list) and payload:
            first = payload[0]
            if isinstance(first, str):
                # Hex-encoded signal (the Up Next wire format).
                decoded = json.loads(binascii.unhexlify(first))
                return decoded if isinstance(decoded, dict) else {}
            if isinstance(first, dict):
                return first
    except (ValueError, binascii.Error) as error:
        LOG.debug("undecodable IPC payload: %s", error)
    return {}


def method_name(method: str) -> str:
    return method.split(".", 1)[1] if "." in method else method


def encode_hex(data: Dict[str, Any]) -> str:
    """Hexlify a payload the way AddonSignals consumers expect (Up Next)."""
    return binascii.hexlify(json.dumps(data).encode()).decode()
