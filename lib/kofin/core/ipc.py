"""Cross-process messages over Kodi's NotifyAll bus.

Every message kofin sends is declared here; nothing may notify a string that
is not in the registry. Received methods arrive prefixed by Kodi (e.g.
``Other.Restart``) — :func:`method_name` strips that.
"""

import binascii
import hmac
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
# ipc.notify -> service). Payloads carry {"Id": "<library id or csv>"}; an
# empty UpdateLibrary payload means the whole whitelist. The library's own
# SyncLibrary command has no message here: its two producers (the whitelist
# applier and the boxset drift probe) run in the service and enqueue it on
# the Library directly, and a message nobody sends is a message only a
# forger would.
REMOVE_LIBRARY = "RemoveLibrary"
REPAIR_LIBRARY = "RepairLibrary"
UPDATE_LIBRARY = "UpdateLibrary"
REFRESH_BOXSETS = "RefreshBoxsets"
# Settings button: seed the cast-image texture cache now (service/artcache.py).
PRECACHE_ART = "PrecacheArt"

# SyncPlay (phase 4): the root entry's plugin invocation asks the service —
# the single owner of all SyncPlay state — to open the group menu on its
# worker thread. No payload.
#
# The *public* SyncPlay provider contract is deliberately not here: its
# inbound messages are other add-ons' to send (their sender id, no nonce —
# nothing irreversible crosses that bus), and kofin's one outbound message
# (SyncSession.State) is declared and sent in core/contract.py, the second,
# public registry (plan G2.1).
SYNCPLAY_MENU = "SyncPlayMenu"

# Who's watching?: same shape, and for the same reason. A plugin invocation
# that blocks on a dialog cannot be reached as a library node — Kodi runs the
# node's <path> as a directory fetch, and the modal fights it. Firing this and
# exiting lets the fetch fail out cleanly while the service puts the picker up.
WHO_IS_WATCHING = "WhoIsWatching"

# Offline downloads (docs/offline-downloads-plan.md W1.5): the context menu
# runs in the plugin process, the download manager in the service. ADD and
# REMOVE carry {"Ids": [...]} (the sender expands seasons/series/albums),
# CANCEL carries {"Id": ...}. REMOVE_ALL carries nothing: the settings button
# confirmed against the store's own count, and one message beats a NotifyAll
# per row — which is the same reason REMOVE takes a list: a removal answers
# once, with one refresh and one toast, and cannot do that if the batch
# reaches the manager as one message per row.
DOWNLOAD_ADD = "DownloadAdd"
DOWNLOAD_CANCEL = "DownloadCancel"
DOWNLOAD_REMOVE = "DownloadRemove"
DOWNLOAD_REMOVE_ALL = "DownloadRemoveAll"

# The stream menu picking a subtitle a transcode did not attach. Carries
# {"Index": <jellyfin stream index>}. It runs in the service for the same
# reason the dialogs above do — the fetch is an ffmpeg extraction on the
# server, tens of seconds of it, and the plugin process cannot wait on that
# or reach the running playback afterwards (service/latesubs.py).
ATTACH_SUBTITLE = "AttachSubtitle"

_REGISTRY = frozenset(
    {
        RESTART,
        AUTH_CHANGED,
        REMOVE_LIBRARY,
        REPAIR_LIBRARY,
        UPDATE_LIBRARY,
        REFRESH_BOXSETS,
        PRECACHE_ART,
        SYNCPLAY_MENU,
        WHO_IS_WATCHING,
        DOWNLOAD_ADD,
        DOWNLOAD_CANCEL,
        DOWNLOAD_REMOVE,
        DOWNLOAD_REMOVE_ALL,
        ATTACH_SUBTITLE,
    }
)

# The messages that cost something irreversible or expensive if forged: rows
# deleted, a whole library re-walked, the service bounced (repeatedly, which
# is a denial of service). Kodi's NotifyAll passes the sender string through
# verbatim from its caller — the builtin and the JSON-RPC method both — so
# "sender == kofin" proves nothing on its own, and these carry a shared secret
# as well (see nonce()). The download trio is here wholesale: REMOVE deletes
# files, ADD pulls gigabytes on someone else's say-so, CANCEL wastes work.
# UPDATE_LIBRARY plans a prune that deletes rows and REFRESH_BOXSETS re-walks
# every collection: both things the first sentence names, so every library
# command is here.
GUARDED = frozenset(
    {
        RESTART,
        AUTH_CHANGED,
        REMOVE_LIBRARY,
        REPAIR_LIBRARY,
        UPDATE_LIBRARY,
        REFRESH_BOXSETS,
        DOWNLOAD_ADD,
        DOWNLOAD_CANCEL,
        DOWNLOAD_REMOVE,
        DOWNLOAD_REMOVE_ALL,
    }
)

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
        # Created with the mode rather than chmod'ed after: open() takes the
        # process umask, which left a 0644 window before the chmod (M2).
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(value)
        os.chmod(temporary, 0o600)  # an existing .tmp keeps its old mode
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
    # NotifyAll offers no useful timing channel, but a secret comparison
    # is spelled as one everywhere else in the tree (audit M2).
    return hmac.compare_digest(str(data.get(NONCE_KEY, "")), expected)


def notify(method: str, data: Optional[Dict[str, Any]] = None) -> None:
    if method not in _REGISTRY:
        raise ValueError("unregistered IPC message: %s" % method)
    payload = dict(data or {})
    if method in GUARDED:
        payload[NONCE_KEY] = nonce()
    xbmc.executebuiltin("NotifyAll(%s, %s, %s)" % (SENDER, method, _encode(payload)))


def _encode(data: Dict[str, Any]) -> str:
    # The builtin parser re-parses its arguments, so the payload is wrapped
    # in a quoted single-element list (the scheme the old addon and
    # AddonSignals use — receivers run json.loads(...)[0]). Hex rather than
    # escaped JSON: CUtil::SplitParams lets only every second character be
    # escaped, so a value containing a quote — json.dumps writes \" and the
    # old escaping made it \\" — closed the parameter early and left the
    # rest to be parsed as builtin syntax (audit M1). Hex has no quotes,
    # commas or backslashes for the parser to see, and decode() already
    # accepted it (the Up Next wire format).
    hexed = binascii.hexlify(json.dumps(data).encode("utf-8")).decode("ascii")
    return '"[\\"%s\\"]"' % hexed


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
