"""The companion add-on contract, kofin-or's side (plan, phase 6).

The official-repository variant may not touch Kodi's databases, so what
stock Piers public interfaces cannot do is left to a companion service
add-on, developed outside this repository and never part of the OR archive,
which may. kofin-or publishes what it needs done and confirms it the way it
confirms everything else, by reading the rows back; the companion is
optional, and absent it the gap stays open and is logged.

The first request is a movie's ungrouped versions. Kodi imports each version
file of a movie as a movie of its own (a plugin item's tag never reaches the
similar-video grouping), the Versions Manager groups them by hand, and the
companion can do the same from the ids below.

Wire: the open requests are one JSON document in kofin's profile, rewritten
whole whenever the set changes, and ``Companion.Requests`` goes out over
``JSONRPC.NotifyAll`` (core/contract.py) with the document's path and
generation, so a running companion wakes and one started later finds the
file. A request leaves the document when the readback no longer shows the
rows it named; there is no acknowledgement message to forge or to lose.
"""

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from kofin.core import contract, kodirpc
from kofin.core.log import Logger
from kofin.sync import private
from . import paths

LOG = Logger(__name__)

# The companion's identity is decided with the user before it exists; until
# then this is where kofin-or looks for it.
ADDON_ID = "service.kofin.companion"
VERSION = 1
DOCUMENT = os.path.join("companion", "requests.json")
GROUP_VERSIONS = "group_versions"


def requests_path() -> str:
    return os.path.join(private.addon_data_path(), DOCUMENT)


def present() -> Optional[str]:
    """The companion's version when it is installed and enabled, else None."""
    reply = kodirpc.call(
        "Addons.GetAddonDetails",
        {"addonid": ADDON_ID, "properties": ["enabled", "version"]},
    )
    addon = reply.get("addon") if isinstance(reply, dict) else None
    if not isinstance(addon, dict) or not addon.get("enabled"):
        return None
    return str(addon.get("version") or "")


def read() -> Dict[str, Any]:
    try:
        with open(requests_path(), "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return {"v": VERSION, "generation": 0, "requests": []}
    if not isinstance(document, dict) or not isinstance(document.get("requests"), list):
        return {"v": VERSION, "generation": 0, "requests": []}
    return document


def _request_id(kind: str, item_id: str, rows: List[int]) -> str:
    key = json.dumps([kind, item_id, sorted(rows)], separators=(",", ":"))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def collect(native) -> List[Dict[str, Any]]:
    """Every movie whose version files Kodi holds as movies of their own,
    with the Kodi ids the companion needs to group them."""
    requests: List[Dict[str, Any]] = []
    store = native.store
    for library in sorted(store.libraries("Movie")):
        owned = native.readback.scope("Movie", library)
        versions = native.readback.versions_in(library)
        if not versions:
            continue
        payloads = store.records(kind="Movie", item_ids=sorted(versions))
        for item_id in sorted(versions):
            owner = owned.get(item_id)
            if owner is None:
                continue
            record = payloads.get(item_id)
            names = {
                s["Id"]: s.get("Name") or ""
                for s in (record.item.get("MediaSources") or [] if record else [])
                if isinstance(s, dict) and s.get("Id")
            }
            rows = sorted(versions[item_id], key=lambda r: r["movieid"])
            requests.append(
                {
                    "id": _request_id(
                        GROUP_VERSIONS, item_id, [r["movieid"] for r in rows]
                    ),
                    "kind": GROUP_VERSIONS,
                    "library": library,
                    "item": item_id,
                    "owner": {"movieid": owner["movieid"], "file": owner.get("file")},
                    "versions": [
                        {
                            "movieid": row["movieid"],
                            "file": row.get("file"),
                            "mediasourceid": paths.source_of(row.get("file") or ""),
                            "name": names.get(
                                paths.source_of(row.get("file") or ""), ""
                            ),
                        }
                        for row in rows
                    ],
                }
            )
    return requests


def publish(native) -> int:
    """Write the open requests when the set changed and tell the companion.
    Returns how many requests are open."""
    requests = collect(native)
    current = read()
    if [r["id"] for r in requests] == [
        r.get("id") for r in current["requests"] if isinstance(r, dict)
    ]:
        if requests:
            _log_open(requests)
        return len(requests)
    generation = int(current.get("generation") or 0) + 1
    document = {
        "v": VERSION,
        "generation": generation,
        "namespace": native.key,
        "requests": requests,
    }
    target = requests_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target + ".part", "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1)
    os.replace(target + ".part", target)
    kodirpc.call(
        "JSONRPC.NotifyAll",
        {
            "sender": "plugin.video.kofin",
            "message": contract.COMPANION_REQUESTS,
            "data": {
                "v": VERSION,
                "generation": generation,
                "count": len(requests),
                "path": target,
            },
        },
    )
    if requests:
        _log_open(requests)
    else:
        LOG.info("companion: no request open (generation %d)", generation)
    return len(requests)


def _log_open(requests: List[Dict[str, Any]]) -> None:
    version = present()
    movies = sum(1 for r in requests if r["kind"] == GROUP_VERSIONS)
    if version is None:
        LOG.info(
            "companion: %d request(s) open, %d movie(s) with ungrouped versions;"
            " %s is not installed, so they stay the user's to group",
            len(requests),
            movies,
            ADDON_ID,
        )
    else:
        LOG.info(
            "companion: %d request(s) open for %s %s", len(requests), ADDON_ID, version
        )
