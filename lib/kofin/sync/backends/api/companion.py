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

The document is version 2 (``companion-schema.json`` beside this module is
the contract both sides test against): version 1 carried the requests,
version 2 adds the device id every chapter key will carry and the kinds this
build publishes, so the companion's status can say which it cannot serve.
The companion writes its own status document and pings ``Companion.Status``
(docs/companion-addon-plan.md, section 3.3); kofin-or folds it into the
capability report and logs each refusal once.
"""

import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Set

from kofin.core import contract, kodirpc, settings
from kofin.core.log import Logger
from kofin.sync import private
from . import paths

LOG = Logger(__name__)

ADDON_ID = "service.kofin.companion"
VERSION = 2
DOCUMENT = os.path.join("companion", "requests.json")
REPORT = "api-capabilities.json"
STATUS = "status.json"
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "companion-schema.json")
GROUP_VERSIONS = "group_versions"
# The kinds this build publishes, in the document for the companion's status.
KINDS = (GROUP_VERSIONS,)


def requests_path() -> str:
    return os.path.join(private.addon_data_path(), DOCUMENT)


def report_path() -> str:
    return os.path.join(private.addon_data_path(), REPORT)


def status_path() -> str:
    """The companion's status document, beside kofin's profile data."""
    return os.path.join(
        os.path.dirname(private.addon_data_path().rstrip("/")), ADDON_ID, STATUS
    )


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
    if current.get("v") == VERSION and [r["id"] for r in requests] == [
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
        "device": settings.get_str("deviceId"),
        "kinds": list(KINDS),
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


# -- the companion's status ------------------------------------------------------


def status() -> Optional[Dict[str, Any]]:
    """The companion's last status document, or None when it never wrote one."""
    try:
        with open(status_path(), "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def status_summary(document: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """What the capability report keeps of a status: the generation it
    answered, the kinds it serves, the gate it found, the counts, and every
    request it refused or is still waiting on, by reason.

    Total: the status is another add-on's file, and this is read while the
    backend is prepared at start, where an exception leaves native sync
    off. A field of the wrong type is read as the field left out."""
    if not document:
        return {}
    outcomes = document.get("requests")
    if not isinstance(outcomes, dict):
        outcomes = {}
    by_state: Dict[str, Dict[str, str]] = {"refused": {}, "waiting": {}}
    for request_id, outcome in outcomes.items():
        if not isinstance(outcome, dict):
            continue
        state = str(outcome.get("state") or "")
        if state in by_state:
            by_state[state][str(request_id)] = str(outcome.get("reason") or "")
    gate = document.get("gate")
    kinds = document.get("kinds")
    counts = document.get("summary")
    return {
        "generation": document.get("generation"),
        "updated": document.get("updated"),
        "idle": document.get("idle"),
        "kinds": [str(k) for k in kinds] if isinstance(kinds, list) else [],
        "gate": (
            {
                kind: {"version": entry.get("version"), "ok": bool(entry.get("ok"))}
                for kind, entry in gate.items()
                if isinstance(entry, dict)
            }
            if isinstance(gate, dict)
            else {}
        ),
        "summary": (
            {
                str(state): count
                for state, count in counts.items()
                if isinstance(count, int) and not isinstance(count, bool)
            }
            if isinstance(counts, dict)
            else {}
        ),
        "refused": by_state["refused"],
        "waiting": by_state["waiting"],
    }


def current_status(version: Optional[str]) -> Optional[Dict[str, Any]]:
    """The companion's status, as far as it is this run's (plan section 8,
    "A stale status").

    None without the add-on: the status file outlives an uninstall, and a
    report that named the kinds an absent add-on serves would be wrong.
    None for a status that answered an older generation than the request
    document carries now: until the companion answers the new one, what it
    said about the old is not what is being served. An idle status names
    no generation and is kept."""
    if version is None:
        return None
    document = status()
    if document is None:
        return None
    answered = document.get("generation")
    generation = read().get("generation")
    if (
        isinstance(answered, int)
        and isinstance(generation, int)
        and answered < generation
    ):
        return None
    return document


def status_block() -> Dict[str, Any]:
    """The capability report's companion entry: presence and the status
    that is current (``current_status``)."""
    version = present()
    return {
        "addon": ADDON_ID,
        "version": version,
        "status": status_summary(current_status(version)),
    }


def absorb_status(
    seen: Set[str], report: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """A ``Companion.Status`` ping: fold the status into the capability
    report and log what it says, each refusal once (``seen`` is the
    service's memory of the refusals already logged).

    ``report`` is the capability report this process wrote at its start
    (``Service._prepare_api_backend``), and the file is rewritten from it.
    The file is never read back to be rewritten: a ping handled around the
    start-up write read the previous run's report and put it back over the
    new one, ``native_sync_enabled`` and ``private_state`` with it, and
    every later ping then read its own stale write. Without a report yet,
    nothing is written; the start-up write carries the block itself."""
    block = status_block()
    summary = block["status"]
    if report is not None:
        report["companion"] = block
        target = report_path()
        with open(target + ".part", "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        os.replace(target + ".part", target)
    if not summary:
        return block
    if summary.get("idle"):
        key = "idle:%s" % summary["idle"]
        if key not in seen:
            seen.add(key)
            LOG.info("companion idle: %s", summary["idle"])
        return block
    counts = summary.get("summary") or {}
    LOG.info(
        "companion status: generation %s, %d done, %d waiting, %d refused",
        summary.get("generation"),
        int(counts.get("done") or 0),
        int(counts.get("waiting") or 0),
        int(counts.get("refused") or 0),
    )
    for request_id, reason in sorted(summary.get("refused", {}).items()):
        key = "%s:%s" % (request_id, reason)
        if key in seen:
            continue
        seen.add(key)
        LOG.info("companion refused request %s: %s", request_id, reason)
    return block
