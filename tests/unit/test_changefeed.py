"""L1 units for the change-feed providers: record parsing on both tiers,
the detection ladder with the protocol guard, watermark conversions and
retention-overrun decisions."""

import calendar


from kofin.core.http import JellyfinError
from kofin.sync import changefeed
from kofin.sync.changefeed import (
    KofinFeed,
    LegacyFeed,
    detect,
    parse_record,
    retention_overrun,
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

    def kofin_sync_queue(self, since, types, libraries=""):
        self.kofin_queue_requests.append((since, types, libraries))
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
        (unix("2026-07-17T10:00:00Z"), "movies,boxsets", "")
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


# --- library scope ------------------------------------------------------------
#
# The feed is scoped by media type, which cannot tell two tvshows libraries
# apart. LibraryIds lets the library thread refuse a record from a library it
# does not sync before anything is fetched.


def test_parse_record_reads_library_ids():
    record = parse_record(
        {"Id": "m1", "Status": "Added", "LibraryIds": ["libA", "libB"]}
    )

    assert record.library_ids == ["libA", "libB"]


def test_parse_record_absent_library_ids_is_unknown_not_empty():
    assert parse_record({"Id": "m1", "Status": "Added"}).library_ids is None
    assert (
        parse_record({"Id": "m1", "Status": "Added", "LibraryIds": []}).library_ids
        is None
    )


# --- checksum spelling --------------------------------------------------------


def test_direct_mode_splits_from_plugin_mode_in_one_place():
    from kofin.sync.policy import reference_checksum

    assert reference_checksum("abc") == "abc|plugin"
    assert reference_checksum("abc", direct_path=True) == "abc|direct"
    assert reference_checksum(None) is None


def test_pipeline_field_lists_carry_etag():
    """An Etag-less DTO stamps a NULL checksum that rewrites on every walk
    (kofindb warns when it happens); every field list the pipeline downloads
    with must therefore ask for the Etag."""
    from kofin.sync import downloader

    assert "Etag" in downloader.info().split(",")
