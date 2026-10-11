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
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from kofin.core import contract, kodirpc, settings
from kofin.core.log import Logger
from kofin.sync import private
from . import paths

LOG = Logger(__name__)

# The one lock on the document: the pass thread writes its kinds and the
# player thread the per-play ones. A lock carries no state across service
# generations, which is why it is exempt from the no-module-globals rule.
_LOCK = threading.Lock()

ADDON_ID = "service.kofin.companion"
VERSION = 2
DOCUMENT = os.path.join("companion", "requests.json")
REPORT = "api-capabilities.json"
STATUS = "status.json"
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "companion-schema.json")
GROUP_VERSIONS = "group_versions"
RETIRE_ASSETS = "retire_assets"
CHAPTER_ART = "chapter_art"
# The kinds this build publishes, in the document for the companion's status:
# the pass's, collected from the catalogue on every pass, and the live ones a
# playback publishes and withdraws (chapterart.py).
PASS_KINDS = (GROUP_VERSIONS, RETIRE_ASSETS)
LIVE_KINDS = (CHAPTER_ART,)
KINDS = PASS_KINDS + LIVE_KINDS


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
    """The pass's requests: every movie whose version files Kodi holds as
    movies of their own, with the Kodi ids the companion needs to group
    them; and every movie with asset rows, with the files it still has, so
    a version or extra the server renamed or dropped leaves at once instead
    of waiting for Clean (ledger S25). The second reads the acknowledged
    mappings only: no payload is loaded for it."""
    requests: List[Dict[str, Any]] = []
    store = native.store
    for library in sorted(store.libraries("Movie")):
        for item_id, mapping in sorted(
            store.mappings(kind="Movie", library=library).items()
        ):
            files = mapping.applied.get("asset_files")
            if mapping.kodi_id is None or not isinstance(files, dict):
                continue
            requests.append(
                {
                    "id": _request_id(RETIRE_ASSETS, item_id, []),
                    "kind": RETIRE_ASSETS,
                    "library": library,
                    "item": item_id,
                    "owner": {"movieid": mapping.kodi_id},
                    "versions": [
                        {"file": url, "name": name}
                        for url, name in files.get("versions") or []
                    ],
                    "extras": [{"file": url} for url in files.get("extras") or []],
                }
            )
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
    """Write the pass's requests when their set changed and tell the
    companion. Returns how many of the pass's requests are open."""
    requests = collect(native)
    _write(native.key, PASS_KINDS, requests)
    if requests:
        _log_open(requests)
    else:
        LOG.debug("companion: no pass request open")
    return len(requests)


def publish_live(kind: str, requests: Sequence[Dict[str, Any]]) -> None:
    """Replace the requests of one live kind, keeping every other kind's.
    For the kind as a whole (a generation start clears it); a claim adds
    and removes its own request with ``put_live`` and ``drop_live``."""
    _write("", (kind,), list(requests))


def put_live(request: Dict[str, Any]) -> None:
    """Add one live request, or replace the one that carries its id. Every
    other request stays, of its own kind too: a claim publishes beside
    whatever an earlier claim has not withdrawn yet."""
    wanted = str(request["id"])
    _replace("", lambda r: str(r.get("id")) == wanted, [dict(request)])


def drop_live(request_id: str) -> None:
    """Remove one live request by its id, and nothing else: a claim
    withdraws what it published. Replacing the whole kind from a claim's
    janitor thread took the next claim's request with it."""
    _replace("", lambda r: str(r.get("id")) == request_id, [])


def _namespace(current: Dict[str, Any]) -> str:
    namespace = str(current.get("namespace") or "")
    if namespace:
        return namespace
    from .store import namespace as derive

    try:
        return str(derive(settings.get_str("serverId"), settings.get_str("userId")))
    except ValueError:
        return ""


def _signature(requests: Sequence[Dict[str, Any]]) -> List[str]:
    """What a rewrite is judged by: every request's content, in id order,
    so a request whose content moved under a stable id (a desired set that
    changed) is published too."""
    return sorted(
        json.dumps(r, sort_keys=True, separators=(",", ":")) for r in requests
    )


def _write(
    namespace: str, kinds: Sequence[str], requests: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The document with ``kinds`` replaced by ``requests``; rewritten and
    announced only when a request changed or the document is of an older
    version. Returns the document's requests."""
    return _replace(namespace, lambda r: r.get("kind") in kinds, requests)


def _replace(
    namespace: str,
    gone: Callable[[Dict[str, Any]], bool],
    requests: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """The document with every request ``gone`` accepts removed and
    ``requests`` added, under the one lock both writers share."""
    with _LOCK:
        current = read()
        kept = [r for r in current["requests"] if isinstance(r, dict) and not gone(r)]
        merged = kept + requests
        if current.get("v") == VERSION and _signature(merged) == _signature(
            [r for r in current["requests"] if isinstance(r, dict)]
        ):
            return [r for r in current["requests"] if isinstance(r, dict)]
        generation = int(current.get("generation") or 0) + 1
        document = {
            "v": VERSION,
            "generation": generation,
            "namespace": namespace or _namespace(current),
            "device": settings.get_str("deviceId"),
            "kinds": list(KINDS),
            "requests": merged,
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
                "count": len(merged),
                "path": target,
            },
        },
    )
    LOG.info("companion: generation %d, %d request(s) open", generation, len(merged))
    return merged


def _log_open(requests: List[Dict[str, Any]]) -> None:
    version = present()
    movies = sum(1 for r in requests if r["kind"] == GROUP_VERSIONS)
    if version is None:
        LOG.info(
            "companion: %d pass request(s) open, %d movie(s) with ungrouped"
            " versions; %s is not installed, so they stay the user's to group",
            len(requests),
            movies,
            ADDON_ID,
        )
    else:
        LOG.info(
            "companion: %d pass request(s) open for %s %s",
            len(requests),
            ADDON_ID,
            version,
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
    request it refused or is still waiting on, by reason."""
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
    return {
        "generation": document.get("generation"),
        "updated": document.get("updated"),
        "idle": document.get("idle"),
        "kinds": list(document.get("kinds") or []),
        "gate": (
            {
                kind: {"version": entry.get("version"), "ok": bool(entry.get("ok"))}
                for kind, entry in gate.items()
                if isinstance(entry, dict)
            }
            if isinstance(gate, dict)
            else {}
        ),
        "summary": dict(document.get("summary") or {}),
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
