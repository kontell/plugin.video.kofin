"""Cross-process messages over Kodi's NotifyAll bus.

Every message kofin sends is declared here; nothing may notify a string that
is not in the registry. Received methods arrive prefixed by Kodi (e.g.
``Other.Restart``) — :func:`method_name` strips that.
"""

import binascii
import json
from typing import Any, Dict, Optional

import xbmc

SENDER = "plugin.video.kofin"

RESTART = "Restart"
AUTH_CHANGED = "AuthChanged"

_REGISTRY = frozenset({RESTART, AUTH_CHANGED})


def notify(method: str, data: Optional[Dict[str, Any]] = None) -> None:
    if method not in _REGISTRY:
        raise ValueError("unregistered IPC message: %s" % method)
    xbmc.executebuiltin("NotifyAll(%s, %s, %s)" % (SENDER, method, _encode(data or {})))


def _encode(data: Dict[str, Any]) -> str:
    # The builtin parser re-parses its arguments, so the JSON payload is
    # wrapped in a quoted single-element list (same scheme the old addon and
    # AddonSignals use — receivers run json.loads(...)[0]).
    return '"[%s]"' % json.dumps(data).replace('"', '\\"')


def decode(data: str) -> Dict[str, Any]:
    payload = json.loads(data)
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, str):
            # Hex-encoded signal (the Up Next wire format).
            decoded = json.loads(binascii.unhexlify(first))
            return decoded if isinstance(decoded, dict) else {}
        if isinstance(first, dict):
            return first
    return {}


def method_name(method: str) -> str:
    return method.split(".", 1)[1] if "." in method else method


def encode_hex(data: Dict[str, Any]) -> str:
    """Hexlify a payload the way AddonSignals consumers expect (Up Next)."""
    return binascii.hexlify(json.dumps(data).encode()).decode()
