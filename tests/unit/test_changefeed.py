"""L1 units for the change-feed providers and planners (phase 5, plan §6):
record parsing on both tiers, the detection ladder with the protocol guard,
watermark conversions, retention-overrun decisions, and the build_plan
matrix — skip-before-download, parent prefetch, ordering, image-only
demotion, userdata dedup."""

import calendar

import pytest

from kofin.core.http import JellyfinError
from kofin.sync import changefeed
from kofin.sync.changefeed import (
    ChangeRecord,
    KofinFeed,
    LegacyFeed,
    build_plan,
    detect,
    parent_candidates,
    parse_record,
    retention_overrun,
    stored_checksum_matches,
    unix_to_watermark,
    watermark_to_unix,
)


class FakeApi:
    def __init__(self):
        self.kofin_info_result = JellyfinError("404")
        self.kofin_queue_result = {}
        self.kofin_queue_requests = []
        self.server_time_result = {"ServerDateTime": "2026-07-17T10:00:00Z"}
        self.sync_queue_result = {}
        self.sync_queue_requests = []

    def kofin_sync_info(self):
        if isinstance(self.kofin_info_result, Exception):
            raise self.kofin_info_result
        return self.kofin_info_result

    def kofin_sync_queue(self, since, types):
        self.kofin_queue_requests.append((since, types))
        return self.kofin_queue_result

    def server_time(self):
        if isinstance(self.server_time_result, Exception):
            raise self.server_time_result
        return self.server_time_result

    def sync_queue(self, last_sync, filters=""):
        self.sync_queue_requests.append((last_sync, filters))
        return self.sync_queue_result


def unix(iso):
    return calendar.timegm(
        __import__("time").strptime(iso, changefeed.WATERMARK_FORMAT)
    )


# --- watermark conversions ----------------------------------------------------


def test_watermark_round_trip():
    stamp = "2026-07-17T10:00:00Z"
    assert unix_to_watermark(watermark_to_unix(stamp)) == stamp


def test_empty_watermark_is_zero():
    assert watermark_to_unix("") == 0


def test_min_value_sentinel_parses_to_none():
    # DateTime.MinValue-style "no value" must not become a negative epoch.
    assert changefeed._iso_to_unix("0001-01-01T00:00:00Z") is None
    assert changefeed._iso_to_unix("garbage") is None
    assert changefeed._iso_to_unix(None) is None


# --- record parsing -----------------------------------------------------------


def test_parse_record_full_shape():
    record = parse_record(
        {
            "Id": "ep1",
            "Status": "Updated",
            "MediaType": "tvshows",
            "ItemType": "Episode",
            "LastModified": 1700000000,
            "UpdateReason": "ImageUpdate, MetadataEdit",
            "Etag": "abc",
            "SeriesId": "s1",
            "SeasonId": "se1",
        }
    )
    assert record.id == "ep1"
    assert record.item_type == "Episode"
    assert record.last_modified == 1700000000
    assert record.series_id == "s1"


def test_parse_record_drops_malformed():
    assert parse_record({"Status": "Added"}) is None
    assert parse_record({"Id": "x", "Status": "Exploded"}) is None


# --- detection ladder ---------------------------------------------------------


def test_detect_prefers_kofin_on_protocol_match():
    api = FakeApi()
    api.kofin_info_result = {"ProtocolVersion": 1, "ServerTime": 1700000000}
    assert isinstance(detect(api), KofinFeed)


def test_detect_falls_to_official_on_protocol_mismatch():
    api = FakeApi()
    api.kofin_info_result = {"ProtocolVersion": 2}
    assert isinstance(detect(api), LegacyFeed)


def test_detect_falls_to_official_when_kofin_absent():
    api = FakeApi()
    assert isinstance(detect(api), LegacyFeed)


def test_detect_none_when_no_companion():
    api = FakeApi()
    api.server_time_result = JellyfinError("404")
    assert detect(api) is None


# --- providers ----------------------------------------------------------------


def test_kofin_feed_request_and_envelope():
    api = FakeApi()
    api.kofin_queue_result = {
        "ServerTime": 1700000100,
        "RetentionCutoff": 1699000000,
        "Items": [
            {"Id": "m1", "Status": "Added", "ItemType": "Movie", "Etag": "e1"},
            {"Id": "bad"},  # malformed -> dropped
        ],
        "UserData": [{"ItemId": "u1"}],
    }
    feed = KofinFeed(api)

    change_set = feed.changes("2026-07-17T10:00:00Z", ["movies", "boxsets"])

    assert api.kofin_queue_requests == [
        (unix("2026-07-17T10:00:00Z"), "movies,boxsets")
    ]
    assert [r.id for r in change_set.records] == ["m1"]
    assert change_set.userdata == [{"ItemId": "u1"}]
    assert change_set.envelope.server_time == 1700000100
    assert change_set.envelope.retention_cutoff == 1699000000


def test_legacy_feed_inverts_include_and_reads_retention():
    api = FakeApi()
    api.server_time_result = {
        "ServerDateTime": "2026-07-17T10:00:00Z",
        "RetentionDateTime": "2026-04-18T10:00:00Z",
    }
    api.sync_queue_result = {
        "ItemsAdded": ["a1"],
        "ItemsUpdated": ["u1"],
        "ItemsRemoved": ["r1"],
        "UserDataChanged": [{"ItemId": "w1"}],
    }
    feed = LegacyFeed(api)

    change_set = feed.changes("2026-07-16T00:00:00Z", ["movies", "boxsets"])

    # The legacy protocol takes the complement as an exclude list.
    assert api.sync_queue_requests == [
        ("2026-07-16T00:00:00Z", "tvshows,musicvideos,music")
    ]
    by_status = {(r.id, r.status) for r in change_set.records}
    assert by_status == {("a1", "Added"), ("u1", "Updated"), ("r1", "Removed")}
    # Sparse records: no tier-1 fields.
    assert all(r.etag is None and r.item_type is None for r in change_set.records)
    assert change_set.envelope.server_time == unix("2026-07-17T10:00:00Z")
    assert change_set.envelope.retention_cutoff == unix("2026-04-18T10:00:00Z")


def test_legacy_feed_none_result_is_empty_change_set():
    api = FakeApi()
    api.sync_queue_result = None
    change_set = LegacyFeed(api).changes("", [])
    assert change_set.records == [] and change_set.userdata == []
    # The clock still rode along.
    assert change_set.envelope.server_time is not None


def test_legacy_feed_survives_server_time_failure():
    api = FakeApi()
    api.server_time_result = JellyfinError("boom")
    api.sync_queue_result = {"ItemsAdded": ["a1"]}
    change_set = LegacyFeed(api).changes("", [])
    assert [r.id for r in change_set.records] == ["a1"]
    assert change_set.envelope.server_time is None


# --- retention overrun --------------------------------------------------------


def test_retention_overrun_matrix():
    old = "2026-01-01T00:00:00Z"
    cutoff = unix("2026-06-01T00:00:00Z")
    assert retention_overrun(old, cutoff) is True
    assert retention_overrun("2026-07-01T00:00:00Z", cutoff) is False
    assert retention_overrun(old, None) is False
    assert retention_overrun(old, 0) is False
    assert retention_overrun("", cutoff) is False  # fresh install


# --- skip predicate -----------------------------------------------------------


def test_stored_checksum_matches_is_the_plugin_suffix_predicate():
    assert stored_checksum_matches("e1", "e1|plugin") is True
    assert stored_checksum_matches("e1", "e2|plugin") is False
    assert stored_checksum_matches("e1", None) is False
    assert stored_checksum_matches(None, "e1|plugin") is False


# --- build_plan ---------------------------------------------------------------


def rec(id, status="Updated", **kw):
    return ChangeRecord(id=id, status=status, **kw)


def everyone_known(_id):
    return True


def nobody_known(_id):
    return False


def test_plan_skips_etag_matches_before_download():
    records = [
        rec("keep1", etag="new", item_type="Movie"),
        rec("skip1", etag="same", item_type="Movie"),
        rec("skip2", status="Added", etag="same", item_type="Movie"),
        rec("noetag", item_type="Movie"),
    ]
    checksums = {"skip1": "same|plugin", "skip2": "same|plugin", "keep1": "old|plugin"}

    plan = build_plan(records, [], checksums, everyone_known)

    assert plan.skipped == 2
    assert plan.updated == ["keep1", "noetag"]
    assert plan.added == []


def test_plan_removed_never_skipped():
    records = [rec("gone1", status="Removed", etag="same", item_type="Movie")]
    plan = build_plan(records, [], {"gone1": "same|plugin"}, everyone_known)
    assert plan.removed == ["gone1"]
    assert plan.skipped == 0


def test_plan_image_only_demotion_is_exact():
    records = [
        rec("art1", update_reason="ImageUpdate", item_type="Movie"),
        rec("full1", update_reason="ImageUpdate, MetadataEdit", item_type="Movie"),
        rec("music1", update_reason="ImageUpdate", item_type="MusicAlbum"),
        rec("none1", item_type="Movie"),
    ]
    plan = build_plan(records, [], {}, everyone_known)
    assert plan.artwork == ["art1"]
    # Mixed reasons and non-video types take the full path.
    assert plan.updated == ["full1", "music1", "none1"]


def test_plan_added_orders_parents_first_then_newest():
    records = [
        rec("ep-old", "Added", item_type="Episode", last_modified=100),
        rec("movie-new", "Added", item_type="Movie", last_modified=900),
        rec("series1", "Added", item_type="Series", last_modified=50),
        rec("ep-new", "Added", item_type="Episode", last_modified=800),
        rec("season1", "Added", item_type="Season", last_modified=60),
        rec("movie-old", "Added", item_type="Movie", last_modified=200),
    ]
    plan = build_plan(records, [], {}, everyone_known)
    assert plan.added == [
        "series1",  # rank 0
        "movie-new",  # rank 1, newest first
        "movie-old",
        "season1",
        "ep-new",  # rank 2, newest first
        "ep-old",
    ]


def test_plan_prefetches_unknown_parents():
    records = [
        rec("ep1", "Added", item_type="Episode", series_id="s9", season_id="se9"),
        rec("ep2", "Added", item_type="Episode", series_id="s9", season_id="se8"),
    ]
    plan = build_plan(records, [], {}, nobody_known)
    # Series before seasons, deduped, ahead of the children.
    assert plan.added == ["s9", "se9", "se8", "ep1", "ep2"]


def test_plan_no_prefetch_when_parent_known_or_in_batch():
    records = [
        rec("s1", "Added", item_type="Series"),
        rec("ep1", "Added", item_type="Episode", series_id="s1", season_id="se1"),
        rec("ep2", "Added", item_type="Episode", series_id="s2"),
    ]
    plan = build_plan(records, [], {}, lambda i: i == "s2")
    # s1 is in the batch, s2 is known locally; only se1 needs prefetching.
    assert plan.added[0] == "se1"
    assert "s2" not in plan.added


def test_plan_tier2_records_never_prefetch():
    records = [rec("ep1", "Added")]  # sparse: no series_id
    plan = build_plan(records, [], {}, nobody_known)
    assert plan.added == ["ep1"]


def test_plan_userdata_dedup_spares_skipped_and_artwork():
    records = [
        rec("dl1", item_type="Movie"),  # downloads -> dto dropped
        rec("skip1", etag="same", item_type="Movie"),  # skipped -> dto kept
        rec("art1", update_reason="ImageUpdate", item_type="Movie"),  # kept
    ]
    userdata = [{"ItemId": "dl1"}, {"ItemId": "skip1"}, {"ItemId": "art1"}]

    plan = build_plan(records, userdata, {"skip1": "same|plugin"}, everyone_known)

    assert [d["ItemId"] for d in plan.userdata] == ["skip1", "art1"]
    # The changed-ids tag set carries the full list regardless.
    assert plan.userdata_changed_ids == {"dl1", "skip1", "art1"}


def test_parent_candidates_only_from_added():
    records = [
        rec("ep1", "Updated", item_type="Episode", series_id="s1"),
        rec("ep2", "Added", item_type="Episode", series_id="s2", season_id="se2"),
    ]
    assert parent_candidates(records) == ["s2", "se2"]
