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
import time
from typing import Any, Dict, List, Optional, Sequence, Set

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
EPISODE_TAGS = "episode_tags"
SEASON_PLOT = "season_plot"
STREAM_FLAGS = "stream_flags"
REMOVE_SET = "remove_set"
CHAPTER_ART = "chapter_art"
# The kinds this build publishes, in the document for the companion's status:
# the pass's, collected from the catalogue on every pass, and the live ones a
# playback publishes and withdraws (chapterart.py).
PASS_KINDS = (
    GROUP_VERSIONS,
    RETIRE_ASSETS,
    EPISODE_TAGS,
    SEASON_PLOT,
    STREAM_FLAGS,
    REMOVE_SET,
)
LIVE_KINDS = (CHAPTER_ART,)
KINDS = PASS_KINDS + LIVE_KINDS
# Kinds no public read confirms: their request ids name the item, the
# companion's status says ``done`` with the request's content hash, and
# that hash is kept on the mapping (store.annotate_companion) so the request
# leaves the document until the content moves (plan, section 3.4).
ACKED_KINDS = (SEASON_PLOT, STREAM_FLAGS, REMOVE_SET)
FAVORITE_EPISODE_TAG = "Favorite episodes"
# Media type names the companion's stream_flags handler takes.
MEDIA_OF = {"Movie": "movie", "Episode": "episode", "MusicVideo": "musicvideo"}
SETS_JOURNAL = os.path.join("companion", "sets.json")
# A set removal the companion never answered is left to the user's Clean.
SETS_JOURNAL_DAYS = 7


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


def _item_request_id(kind: str, item_id: str) -> str:
    """An id the status can be read back by: the kind and the item."""
    return kind + ":" + item_id


def content_hash(request: Dict[str, Any]) -> str:
    """The companion's digest of a request: everything but its id."""
    body = {k: v for k, v in request.items() if k != "id"}
    key = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _acked(mapping, kind: str, request: Dict[str, Any]) -> bool:
    done = mapping.applied.get("companion") or {}
    return bool(done.get(kind)) and done.get(kind) == content_hash(request)


def collect(native) -> List[Dict[str, Any]]:
    """The pass's requests: every movie whose version files Kodi holds as
    movies of their own, with the Kodi ids the companion needs to group
    them; and every movie with asset rows, with the files it still has, so
    a version or extra the server renamed or dropped leaves at once instead
    of waiting for Clean (ledger S25). The second reads the acknowledged
    mappings only: no payload is loaded for it."""
    requests: List[Dict[str, Any]] = []
    store = native.store
    _absorb_acks(store)
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
    requests.extend(_metadata_requests(store))
    requests.extend(_set_requests())
    return requests


def _metadata_requests(store) -> List[Dict[str, Any]]:
    """The video metadata no public setter writes, from the acknowledged
    mappings: a favourite episode's tag, a season's plot, a file's stream
    flags (plan C4; ledger S14, S18, S09)."""
    requests: List[Dict[str, Any]] = []
    for kind in ("Movie", "Episode", "MusicVideo", "Season"):
        for library in sorted(store.libraries(kind)):
            for item_id, mapping in sorted(
                store.mappings(kind=kind, library=library).items()
            ):
                if mapping.kodi_id is None:
                    continue
                applied = mapping.applied
                if kind == "Episode" and applied.get("favorite"):
                    requests.append(
                        {
                            "id": _item_request_id(EPISODE_TAGS, item_id),
                            "kind": EPISODE_TAGS,
                            "library": library,
                            "item": item_id,
                            "owner": {"episodeid": mapping.kodi_id},
                            "tags": [FAVORITE_EPISODE_TAG],
                        }
                    )
                if kind == "Season" and applied.get("plot"):
                    request = {
                        "id": _item_request_id(SEASON_PLOT, item_id),
                        "kind": SEASON_PLOT,
                        "library": library,
                        "item": item_id,
                        "owner": {"seasonid": mapping.kodi_id},
                        "plot": applied["plot"],
                    }
                    if not _acked(mapping, SEASON_PLOT, request):
                        requests.append(request)
                flags = applied.get("flags")
                if kind in MEDIA_OF and isinstance(flags, dict):
                    request = {
                        "id": _item_request_id(STREAM_FLAGS, item_id),
                        "kind": STREAM_FLAGS,
                        "library": library,
                        "item": item_id,
                        "owner": {KINDS_ID[kind]: mapping.kodi_id},
                        "media": MEDIA_OF[kind],
                        "flags": {
                            "audio": list(flags.get("audio") or []),
                            "subtitle": list(flags.get("subtitle") or []),
                        },
                    }
                    if not _acked(mapping, STREAM_FLAGS, request):
                        requests.append(request)
    return requests


KINDS_ID = {"Movie": "movieid", "Episode": "episodeid", "MusicVideo": "musicvideoid"}


def _absorb_acks(store) -> int:
    """What the companion's status says is done, kept where the requests are
    built from: the mapping for an item's kind, the journal for a set."""
    document = status()
    outcomes = (document or {}).get("requests")
    if not isinstance(outcomes, dict):
        return 0
    rows = []
    done_sets = set()
    for request_id, outcome in outcomes.items():
        if not isinstance(outcome, dict) or outcome.get("state") != "done":
            continue
        kind, _sep, item_id = str(request_id).partition(":")
        if not item_id or kind not in ACKED_KINDS:
            continue
        if kind == REMOVE_SET:
            done_sets.add(item_id)
        else:
            rows.append((item_id, kind, str(outcome.get("hash") or "")))
    count = store.annotate_companion(rows) if rows else 0
    if done_sets:
        _prune_sets(done_sets)
    return count


# -- empty set rows --------------------------------------------------------------


def sets_journal_path() -> str:
    return os.path.join(private.addon_data_path(), SETS_JOURNAL)


def _read_sets() -> List[Dict[str, Any]]:
    try:
        with open(sets_journal_path(), "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return []
    entries = document.get("sets") if isinstance(document, dict) else None
    return (
        [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    )


def _write_sets(entries: List[Dict[str, Any]]) -> None:
    target = sets_journal_path()
    if not entries:
        try:
            os.remove(target)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target + ".part", "w", encoding="utf-8") as handle:
        json.dump({"sets": entries}, handle, indent=1)
    os.replace(target + ".part", target)


def set_key(title: str) -> str:
    return hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]


def journal_set(title: str) -> None:
    """A set whose last owned member let go: its empty row is the
    companion's to delete (removal.finish_sets)."""
    entries = _read_sets()
    key = set_key(title)
    if any(e.get("key") == key for e in entries):
        return
    entries.append({"key": key, "title": title, "since": int(time.time())})
    _write_sets(entries)
    LOG.info("companion: empty set %r left to the companion", title)


def _prune_sets(done: Set[str]) -> None:
    entries = _read_sets()
    kept = [e for e in entries if e.get("key") not in done]
    if len(kept) != len(entries):
        _write_sets(kept)


def _set_requests() -> List[Dict[str, Any]]:
    entries = _read_sets()
    floor = time.time() - SETS_JOURNAL_DAYS * 86400
    live = [e for e in entries if int(e.get("since") or 0) >= floor]
    if len(live) != len(entries):
        _write_sets(live)
    return [
        {
            "id": _item_request_id(REMOVE_SET, str(e["key"])),
            "kind": REMOVE_SET,
            "library": "",
            "item": str(e["key"]),
            "title": str(e.get("title") or ""),
        }
        for e in live
        if e.get("key")
    ]


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
    """Replace the requests of one live kind, keeping every other kind's."""
    _write("", (kind,), list(requests))


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
    with _LOCK:
        current = read()
        kept = [
            r
            for r in current["requests"]
            if isinstance(r, dict) and r.get("kind") not in kinds
        ]
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


def status_block() -> Dict[str, Any]:
    """The capability report's companion entry: presence and the last status."""
    return {
        "addon": ADDON_ID,
        "version": present(),
        "status": status_summary(status()),
    }


def absorb_status(seen: Set[str]) -> Dict[str, Any]:
    """A ``Companion.Status`` ping: fold the status into the capability
    report and log what it says, each refusal once (``seen`` is the
    service's memory of the refusals already logged)."""
    block = status_block()
    summary = block["status"]
    try:
        with open(report_path(), "r", encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, ValueError):
        report = None
    if isinstance(report, dict):
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
