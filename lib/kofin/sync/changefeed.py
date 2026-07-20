# -*- coding: utf-8 -*-
"""Change-feed providers: the companion-plugin tier ladder as code
(phase 5, plan §2).

One internal record type covers every tier; each enhancement keys off
**field presence, not tier identity**, so the phase-6 plugin-free provider
is a third implementation, not a redesign. ``KofinFeed`` speaks the
KofinSyncQueue v1 protocol (typed records, query-time Etags, retention
cutoff in-band); ``LegacyFeed`` adapts the official KodiSyncQueue
``GetItems`` shape into sparse records — optional fields stay ``None`` and
the tier-1 features simply never engage.

The planner functions are pure: they turn a change set plus local lookups
into ordered work lists, so every decision (skip-before-download, parent
prefetch, image-only demotion, retention overrun) is unit-testable without
Kodi or a server.
"""

import calendar
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from kofin.core.http import JellyfinError
from kofin.core.log import Logger

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

TIER_KOFIN = "kofin"
TIER_OFFICIAL = "official"
TIER_NONE = "none"

# The one protocol version this client speaks. A mismatch falls to tier 2
# (adopt-by-probe, never raise a floor — the phase-4 stance).
PROTOCOL_VERSION = 1

WATERMARK_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Every media-type class the sync queue knows about. ``include`` lists are
# subsets of this; the legacy protocol wants the complement (exclude list).
ALL_TYPES = ("movies", "tvshows", "boxsets", "musicvideos", "music")

# Parent-first ranks: parents download ahead of children so the orphan
# re-queue dance never engages on tier 1. Unranked types sit mid-field.
_TYPE_RANK = {
    "Series": 0,
    "MusicArtist": 0,
    "AlbumArtist": 0,
    "Season": 1,
    "MusicAlbum": 1,
    "Episode": 2,
    "Audio": 2,
}
_RANK_DEFAULT = 1

# Types the artwork-only write path can apply (video art tables). Music and
# boxset image updates take the full path.
ARTWORK_ONLY_TYPES = ("Movie", "Series", "Season", "Episode", "MusicVideo")

_PLUGIN_CHECKSUM_SUFFIX = "|plugin"


@dataclass
class ChangeRecord:
    """One change-queue entry; sparse fields are ``None`` on lower tiers."""

    id: str
    status: str  # "Added" | "Updated" | "Removed"
    media_type: Optional[str] = None  # movies|tvshows|boxsets|musicvideos|music
    item_type: Optional[str] = None  # Movie|Series|Season|Episode|...
    last_modified: Optional[int] = None  # unix seconds
    update_reason: Optional[str] = None  # ItemUpdateType flags, comma string
    etag: Optional[str] = None
    series_id: Optional[str] = None
    season_id: Optional[str] = None


@dataclass
class Envelope:
    """Response envelope facts the watermark/retention logic consumes."""

    server_time: Optional[int] = None  # unix seconds
    retention_cutoff: Optional[int] = None  # unix seconds; None/0 = no cutoff


@dataclass
class ChangeSet:
    records: List[ChangeRecord] = field(default_factory=list)
    userdata: List[JsonDict] = field(default_factory=list)
    envelope: Envelope = field(default_factory=Envelope)


@dataclass
class SyncPlan:
    """Ordered work lists for one catch-up cycle (build_plan output)."""

    userdata: List[JsonDict] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    artwork: List[str] = field(default_factory=list)
    userdata_changed_ids: Set[str] = field(default_factory=set)
    skipped: int = 0


def _iso_to_unix(value: Optional[str]) -> Optional[int]:
    """Parse a server ISO timestamp to unix seconds; None when unusable.

    DateTime.MinValue-style sentinels (year 1) mean "no value" and map to
    None rather than a negative epoch.
    """
    if not value:
        return None

    try:
        parsed = time.strptime(str(value)[:19] + "Z", WATERMARK_FORMAT)
    except ValueError:
        return None

    if parsed.tm_year < 1970:
        return None

    return int(calendar.timegm(parsed))


def watermark_to_unix(watermark: str) -> int:
    """The ISO watermark setting as unix seconds; 0 = everything."""
    return _iso_to_unix(watermark) or 0


def unix_to_watermark(unix: int) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime(WATERMARK_FORMAT)


def _as_int(value: Any) -> Optional[int]:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None

    return result or None


def parse_record(raw: JsonDict) -> Optional[ChangeRecord]:
    """A KofinSyncQueue response record → ChangeRecord; None when unusable."""
    item_id = raw.get("Id")
    status = raw.get("Status")

    if not item_id or status not in ("Added", "Updated", "Removed"):
        LOG.warning("dropping malformed change record: %s", raw)
        return None

    return ChangeRecord(
        id=str(item_id),
        status=str(status),
        media_type=raw.get("MediaType") or None,
        item_type=raw.get("ItemType") or None,
        last_modified=_as_int(raw.get("LastModified")),
        update_reason=raw.get("UpdateReason") or None,
        etag=raw.get("Etag") or None,
        series_id=raw.get("SeriesId") or None,
        season_id=raw.get("SeasonId") or None,
    )


class KofinFeed:
    """Tier 1: the KofinSyncQueue companion (typed records, fast path)."""

    tier = TIER_KOFIN

    def __init__(self, api: Any) -> None:
        self.api = api

    def changes(self, last_sync: str, include: Sequence[str]) -> ChangeSet:
        result = self.api.kofin_sync_queue(
            watermark_to_unix(last_sync), ",".join(include)
        )

        if not result:
            return ChangeSet()

        records = []
        for raw in result.get("Items") or []:
            record = parse_record(raw)
            if record is not None:
                records.append(record)

        return ChangeSet(
            records=records,
            userdata=list(result.get("UserData") or []),
            envelope=Envelope(
                server_time=_as_int(result.get("ServerTime")),
                retention_cutoff=_as_int(result.get("RetentionCutoff")),
            ),
        )

    def server_now(self) -> Optional[int]:
        """Server clock via the Info probe (also carries retention)."""
        return _as_int(self.api.kofin_sync_info().get("ServerTime"))


class LegacyFeed:
    """Tier 2: the official KodiSyncQueue — sparse records, fork semantics.

    The GetServerDateTime round trip the drain-time watermark save used to
    make (library.py save_last_sync) happens here at *query* time instead:
    same request count per catch-up, an earlier (safer) clock sample, and
    the RetentionDateTime field — returned today and ignored by every
    client — finally read (plan §2 retention overrun, sync-plan R5).
    """

    tier = TIER_OFFICIAL

    def __init__(self, api: Any) -> None:
        self.api = api

    def changes(self, last_sync: str, include: Sequence[str]) -> ChangeSet:
        envelope = Envelope()

        try:
            server_time = self.api.server_time()
            envelope.server_time = _iso_to_unix(server_time.get("ServerDateTime"))
            envelope.retention_cutoff = _iso_to_unix(
                server_time.get("RetentionDateTime")
            )
        except JellyfinError as error:
            # The queue call below will surface a real outage; retention
            # detection just skips this cycle.
            LOG.warning("GetServerDateTime failed: %s", error)

        # The legacy protocol takes an *exclude* list.
        excluded = [x for x in ALL_TYPES if x not in include]
        result = self.api.sync_queue(last_sync, ",".join(excluded))

        if result is None:
            return ChangeSet(envelope=envelope)

        records = []
        for status, key in (
            ("Added", "ItemsAdded"),
            ("Updated", "ItemsUpdated"),
            ("Removed", "ItemsRemoved"),
        ):
            for item_id in result.get(key) or []:
                records.append(ChangeRecord(id=str(item_id), status=status))

        return ChangeSet(
            records=records,
            userdata=list(result.get("UserDataChanged") or []),
            envelope=envelope,
        )

    def server_now(self) -> Optional[int]:
        return _iso_to_unix(self.api.server_time().get("ServerDateTime"))


def detect(api: Any) -> Optional[Any]:
    """The tier ladder, best first: KofinSyncQueue → official → None.

    Probes answer only when the respective plugin is installed and enabled;
    a KofinSyncQueue speaking an unknown protocol falls to tier 2.
    """
    try:
        info = api.kofin_sync_info()
        version = _as_int(info.get("ProtocolVersion"))

        if version == PROTOCOL_VERSION:
            LOG.info(
                "KofinSyncQueue companion detected (plugin %s, protocol %s)",
                info.get("PluginVersion", "?"),
                version,
            )
            return KofinFeed(api)

        LOG.warning(
            "KofinSyncQueue speaks protocol %s (this client: %s); "
            "falling back to the official companion path",
            version,
            PROTOCOL_VERSION,
        )
    except JellyfinError as error:
        LOG.info("no KofinSyncQueue companion detected (%s)", error)

    try:
        api.server_time()
        return LegacyFeed(api)
    except JellyfinError as error:
        LOG.info("no KodiSyncQueue companion detected (%s)", error)

    return None


def retention_overrun(last_sync: str, retention_cutoff: Optional[int]) -> bool:
    """Whether the server's queue no longer reaches back to our watermark.

    True means records between the watermark and the cutoff are gone —
    silently, unless answered with a targeted update pass (sync-plan R5).
    An empty watermark is a fresh install: nothing to overrun.
    """
    if not retention_cutoff:
        return False

    watermark = watermark_to_unix(last_sync)

    return bool(watermark) and watermark < retention_cutoff


def stored_checksum_matches(etag: Optional[str], checksum: Optional[str]) -> bool:
    """The check_unchanged predicate (fields.sync_checksum), evaluated before
    download: the stored reference checksum is ``"<etag>|plugin"``."""
    return bool(etag) and checksum == "%s%s" % (etag, _PLUGIN_CHECKSUM_SUFFIX)


def _is_image_only(record: ChangeRecord) -> bool:
    """Exactly ImageUpdate — any other flag in the reason takes the full
    path (an image change accompanied by a metadata edit must cascade)."""
    if record.update_reason is None:
        return False

    reasons = {part.strip() for part in record.update_reason.split(",") if part.strip()}

    return reasons == {"ImageUpdate"}


def _added_sort_key(record: ChangeRecord) -> Any:
    rank = _TYPE_RANK.get(record.item_type or "", _RANK_DEFAULT)

    return (rank, -(record.last_modified or 0))


def parent_candidates(records: Sequence[ChangeRecord]) -> List[str]:
    """Parent ids (series first, then seasons) that added child records
    reference — the caller checks these against kofin.db and passes the
    unknown-locally survivors to build_plan as prefetch material."""
    batch = {record.id for record in records}
    series: List[str] = []
    seasons: List[str] = []

    for record in records:
        if record.status != "Added":
            continue

        if record.series_id and record.series_id not in batch:
            if record.series_id not in series:
                series.append(record.series_id)

        if record.season_id and record.season_id not in batch:
            if record.season_id not in seasons:
                seasons.append(record.season_id)

    return series + [season for season in seasons if season not in series]


def build_plan(
    records: Sequence[ChangeRecord],
    userdata: Sequence[JsonDict],
    checksums: Dict[str, Optional[str]],
    known_parents: Callable[[str], bool],
) -> SyncPlan:
    """Turn a change set into ordered work lists.

    * Skip-before-download: records whose Etag matches the stored checksum
      are dropped here — same predicate as the post-download short-circuit,
      evaluated earlier (plan §2). Their userdata, if any, survives via the
      userdata list (the dedup below only considers records that will
      actually download).
    * Ordering: parents ahead of children, newest first within rank.
    * Parent prefetch: unknown series/seasons referenced by added children
      are prepended as their own additions.
    * Image-only demotion: reason == ImageUpdate exactly → the artwork
      class (lowest priority, artwork-only write).
    """
    plan = SyncPlan()
    plan.userdata_changed_ids = {
        str(dto.get("ItemId")) for dto in userdata if dto.get("ItemId")
    }

    added_records: List[ChangeRecord] = []

    for record in records:
        if record.status == "Removed":
            plan.removed.append(record.id)
            continue

        if stored_checksum_matches(record.etag, checksums.get(record.id)):
            plan.skipped += 1
            continue

        if record.status == "Added":
            added_records.append(record)
        elif _is_image_only(record) and record.item_type in ARTWORK_ONLY_TYPES:
            plan.artwork.append(record.id)
        else:
            plan.updated.append(record.id)

    added_records.sort(key=_added_sort_key)

    prefetch = [
        parent
        for parent in parent_candidates(added_records)
        if not known_parents(parent)
    ]
    plan.added = prefetch + [record.id for record in added_records]

    # An item can appear in the userdata list as well as among the records.
    # Whatever downloads applies its own userdata (through the write cascade
    # or the Etag short-circuit), so drop the overlap — but only against
    # records that will download: skipped and artwork-only entries never
    # apply userdata, so theirs must survive here.
    downloading = set(plan.added) | set(plan.updated)
    plan.userdata = [
        dto for dto in userdata if str(dto.get("ItemId")) not in downloading
    ]

    return plan
