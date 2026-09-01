"""The public SyncPlay provider contract (plan G2.1).

``core/ipc.py`` is kofin's closed world: every message *kofin* sends, one
sender, a nonce where a message is destructive. This module is the opposite
boundary, deliberately separate — the messages **other add-ons** send to the
sync service, versioned, documented in ``docs/syncplay-provider-contract.md``,
and additive-only. It is unguarded on purpose: nothing irreversible crosses
it, and a hostile local add-on can already write any window property on the
box, so a nonce would add ceremony and no security.

Wire: ``JSONRPC.NotifyAll`` from the provider's own add-on id. Kodi delivers
the method as ``Other.<message>``; payloads are JSON objects carrying
``{"v": 1, ...}``. Unknown fields are ignored (additive evolution); an
unsupported ``v`` is dropped with a log line, never guessed at. The one
outbound message (``SyncSession.State``) is kofin's and is sent from here —
this module is the second, public registry the rule in ``core/ipc.py``
points at.

The sender snippet consumers copy (never import):

    xbmc.executeJSONRPC(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "JSONRPC.NotifyAll",
        "params": {"sender": "plugin.video.example",
                   "message": "SyncProvider.Claim",
                   "data": {"v": 1, "provider": "example", "key": "..."}}}))
"""

import json
import re
from typing import Any, Dict, Optional

import xbmc

from kofin.core.log import Logger

LOG = Logger(__name__)

VERSION = 1

# provider -> service
REGISTER = "SyncProvider.Register"
CLAIM = "SyncProvider.Claim"
PROPOSE = "SyncSession.Propose"
MENU = "SyncSession.Menu"

# service -> everyone: the session state changed (the property carries the
# state; this is the ping that says "read it again"). Also the service's
# start-up announce, which is what tells providers to re-register.
STATE = "SyncSession.State"

INBOUND = frozenset({REGISTER, CLAIM, PROPOSE, MENU})

# Caps. A payload is a notification, not a document; anything bigger than
# this is malformed or hostile, and either way not worth parsing.
MAX_PAYLOAD_BYTES = 8192
PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")
MAX_KEY_LENGTH = 512
MAX_NAME_LENGTH = 256
MAX_TEMPLATE_LENGTH = 1024

_REQUIRED = {
    REGISTER: ("provider", "play"),
    CLAIM: ("provider", "key"),
    PROPOSE: ("provider", "key"),
    MENU: (),
}


def decode(name: str, data: str) -> Optional[Dict[str, Any]]:
    """The validated payload of an inbound contract message, or None.

    Never raises — this runs on Kodi's notification thread, where the
    sender is whoever called NotifyAll. Accepts a bare JSON object (the
    ``JSONRPC.NotifyAll`` path) and the quoted-list wrapping Kodi's
    *builtin* NotifyAll produces, so a provider may use either.
    """
    if name not in INBOUND:
        return None

    if not isinstance(data, str) or len(data) > MAX_PAYLOAD_BYTES:
        LOG.debug("contract %s: payload missing or oversize", name)
        return None

    try:
        payload = json.loads(data)
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            payload = payload[0]
    except ValueError:
        LOG.debug("contract %s: unparseable payload", name)
        return None

    if not isinstance(payload, dict):
        LOG.debug("contract %s: payload is not an object", name)
        return None

    if payload.get("v") != VERSION:
        LOG.info("contract %s: unsupported version %r", name, payload.get("v"))
        return None

    for field in _REQUIRED[name]:
        if field not in payload:
            LOG.info("contract %s: missing %r", name, field)
            return None

    if "provider" in payload and not (
        isinstance(payload["provider"], str)
        and PROVIDER_NAME.match(payload["provider"])
    ):
        LOG.info("contract %s: bad provider name %r", name, payload.get("provider"))
        return None

    if "key" in payload and not (
        isinstance(payload["key"], str) and 0 < len(payload["key"]) <= MAX_KEY_LENGTH
    ):
        LOG.info("contract %s: bad key", name)
        return None

    return payload


def register_template(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The validated play registration out of a REGISTER payload, or None.

    ``{"url_template": <plugin URL with {key} and optional {position_s}>,
    "audio": bool}`` — the template must name ``{key}``, or the provider
    could never be told what to start.
    """
    play = payload.get("play")

    if not isinstance(play, dict):
        return None

    template = play.get("url_template")

    if (
        not isinstance(template, str)
        or not 0 < len(template) <= MAX_TEMPLATE_LENGTH
        or "{key}" not in template
    ):
        return None

    return {"url_template": template, "audio": bool(play.get("audio"))}


def engine_claim(payload: Dict[str, Any]) -> Dict[str, Any]:
    """A CLAIM payload as the engine's claim shape (ports.Claim).

    The wire uses contract spellings; the engine keeps the claim keys the
    kofin play pipeline established. Only recognized fields cross over —
    a provider cannot smuggle arbitrary keys into the engine's view.
    """
    claim: Dict[str, Any] = {
        "Id": payload["key"],
        "Provider": payload["provider"],
        "PlayMethod": (
            payload["play_method"]
            if payload.get("play_method") in ("DirectPlay", "DirectStream", "Transcode")
            else "DirectPlay"
        ),
    }

    if isinstance(payload.get("play_session"), str):
        claim["PlaySessionId"] = payload["play_session"]

    tempo = payload.get("tempo")

    if isinstance(tempo, dict) and isinstance(tempo.get("file"), str):
        route: Dict[str, Any] = {"File": tempo["file"]}
        try:
            route["QueueSecs"] = float(tempo.get("queue_secs") or 0) or 8.0
        except (TypeError, ValueError):
            route["QueueSecs"] = 8.0
        if isinstance(tempo.get("manifest_type"), str):
            route["ManifestType"] = tempo["manifest_type"]
        claim["Tempo"] = route

    return claim


def publish_state() -> None:
    """Ping every listener that ``syncsession.state`` changed (G2.2).

    The property carries the state; this message carries nothing but the
    version, so a listener re-reads rather than trusts a payload that may
    have been overtaken. Sent over JSON-RPC, not the NotifyAll builtin —
    the builtin re-parses its arguments (see ipc._encode), and the public
    wire must stay plain JSON that any add-on can produce and read.
    """
    xbmc.executeJSONRPC(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "JSONRPC.NotifyAll",
                "params": {
                    "sender": "plugin.video.kofin",
                    "message": STATE,
                    "data": {"v": VERSION},
                },
            }
        )
    )
