"""L1 units for the download manager's transfer machinery (plan W1.5/W1.6).

Everything drives ``_process`` directly — no worker threads — with a fake
Api and duck-typed streams; the repoint policy is recorded, not executed
(its own L2 suite owns it)."""

import os

import pytest

from kofin.core.http import JellyfinError, Unauthorized
from kofin.downloads import manager as manager_module
from kofin.downloads import store
from kofin.downloads.manager import DownloadManager
from kofin.sync import db as sync_db
from tests.unit.fakes import FakeAddon, FakeWindow

import threading


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    FakeAddon.store = {
        "downloadsEnabled": "true",
        "downloadsNotify": "true",
        "downloadsPath": str(tmp_path / "dl"),
        "downloadsMaxParallel": "2",
    }
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcvfs.exists", lambda p: True)
    # special:// lands inside tmp_path, not in the repo: the manager writes
    # the Downloaded-music playlist and node through translatePath, and a
    # pass-through left a literal "special:" directory behind.
    monkeypatch.setattr(
        "xbmcvfs.translatePath",
        lambda p: str(p).replace("special://", str(tmp_path / "kodi") + "/"),
    )
    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    yield
    sync_db.reset_overrides()


@pytest.fixture
def repoints(monkeypatch):
    calls = {"repoint": [], "restore": [], "stamp": [], "unstamp": []}
    monkeypatch.setattr(
        manager_module.repoint,
        "repoint",
        lambda row, root: calls["repoint"].append((row.jellyfin_id, root)) or True,
    )
    monkeypatch.setattr(
        manager_module.repoint,
        "restore",
        lambda row, root: calls["restore"].append((row.jellyfin_id, root)) or True,
    )
    monkeypatch.setattr(
        manager_module.repoint,
        "stamp_tag",
        lambda row: calls["stamp"].append(row.jellyfin_id),
    )
    calls["badge"] = []
    calls["unbadge"] = []
    monkeypatch.setattr(
        manager_module.repoint,
        "stamp_badge",
        lambda row: calls["badge"].append(row.jellyfin_id),
    )
    monkeypatch.setattr(
        manager_module.repoint,
        "clear_badge",
        lambda row: calls["unbadge"].append(row.jellyfin_id),
    )
    monkeypatch.setattr(
        manager_module.repoint,
        "unstamp_tag",
        lambda row: calls["unstamp"].append(row.jellyfin_id),
    )
    return calls


class FakeStream:
    def __init__(self, status=200, chunks=(), headers=None, already_complete=False):
        self.status = status
        self._chunks = list(chunks)
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.already_complete = already_complete
        self.closed = False
        self.on_chunk = None

    def header(self, name):
        return self.headers.get(name.lower(), "")

    def chunks(self):
        for index, chunk in enumerate(self._chunks):
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk
            if self.on_chunk:
                self.on_chunk(index)

    def close(self):
        self.closed = True


class FakeManagerApi:
    server = "http://s"
    device_id = "dev1"

    def __init__(self, item, streams, playback=None, transcode_streams=()):
        self._item = item
        self._streams = list(streams)
        self._playback = playback
        self._transcode_streams = list(transcode_streams)
        self.stream_calls = []
        self.transcode_urls = []
        self.closed_transcodes = []
        self.subtitle_payloads = {}
        self.downloaded_urls = []
        self.segments = {"Items": []}

    def item(self, item_id):
        if isinstance(self._item, Exception):
            raise self._item
        return self._item

    def download_stream(self, item_id, start=0):
        self.stream_calls.append(start)
        return self._streams.pop(0)

    def playback_info(self, item_id, profile, **kwargs):
        if isinstance(self._playback, Exception):
            raise self._playback
        return self._playback or {}

    def transcode_stream(self, url):
        self.transcode_urls.append(url)
        return self._transcode_streams.pop(0)

    def close_transcode(self, device_id, play_session_id):
        self.closed_transcodes.append((device_id, play_session_id))

    def media_segments(self, item_id):
        if isinstance(self.segments, Exception):
            raise self.segments
        return self.segments

    def subtitle_stream_url(self, item_id, source_id, index, extension):
        return "http://s/subs/%s/%s/%s.%s" % (item_id, source_id, index, extension)

    def download(self, url):
        self.downloaded_urls.append(url)
        return self.subtitle_payloads.get(url, b"sub-bytes")

    def close(self):
        pass


MOVIE_DTO = {
    "Id": "m1",
    "Type": "Movie",
    "Name": "The Movie",
    "ProductionYear": 2019,
    "SeriesId": None,
    "UserData": {"Played": False},
    "MediaSources": [{"Id": "src1", "Container": "mkv", "Size": 8, "MediaStreams": []}],
}

DISPOSITION = 'attachment; filename="The Movie (2019).mkv"'


def make_manager(repoints):
    refreshes = []
    manager = DownloadManager(
        api_factory=lambda: None,
        refresh=lambda databases: refreshes.append(list(databases)),
        stopping=threading.Event(),
    )
    return manager, refreshes


def queue_row(item_id="m1", **extra):
    store.queue(store.Download(jellyfin_id=item_id, queued_at=100, **extra))
    return store.claim()


def test_happy_path_downloads_verifies_repoints_and_refreshes(tmp_path, repoints):
    manager, refreshes = make_manager(repoints)
    api = FakeManagerApi(
        MOVIE_DTO,
        [
            FakeStream(
                200,
                [b"abcd", b"efgh"],
                {"Content-Length": "8", "Content-Disposition": DISPOSITION},
            )
        ],
    )

    manager._process(api, queue_row())

    row = store.get("m1")
    assert row.state == store.DONE
    assert row.rel_path == "Movies/The Movie (2019)/The Movie (2019).mkv"
    assert row.size_actual == 8 and row.container == "mkv"
    assert row.media_type == "movie" and row.userdata == {"Played": False}
    final = tmp_path / "dl" / row.rel_path
    assert final.read_bytes() == b"abcdefgh"
    assert not os.path.exists(str(final) + ".part")
    assert repoints["repoint"] == [("m1", str(tmp_path / "dl"))]
    assert repoints["stamp"] == ["m1"]
    assert repoints["badge"] == ["m1"]  # the native-library signal
    # Deferred, not fired: a completion marks its database dirty and the
    # worker loop pays for the refresh once the pool goes quiet.
    assert refreshes == []
    manager._flush_refresh(force=True)
    assert refreshes == [["video"]]


def test_resume_appends_from_the_part_watermark(tmp_path, repoints):
    manager, _ = make_manager(repoints)
    rel = "Movies/The Movie (2019)/The Movie (2019).mkv"
    part = tmp_path / "dl" / rel
    part.parent.mkdir(parents=True)
    (part.parent / (part.name + ".part")).write_bytes(b"abcd")

    row = queue_row()
    store.record_target("m1", rel, "mkv")
    api = FakeManagerApi(
        MOVIE_DTO,
        [FakeStream(206, [b"efgh"], {"Content-Range": "bytes 4-7/8"})],
    )

    manager._process(api, store.get("m1"))

    assert api.stream_calls == [4]  # the Range start
    assert store.get("m1").state == store.DONE
    assert (tmp_path / "dl" / rel).read_bytes() == b"abcdefgh"


def test_a_server_that_ignores_the_range_rewrites_from_scratch(tmp_path, repoints):
    manager, _ = make_manager(repoints)
    rel = "Movies/The Movie (2019)/The Movie (2019).mkv"
    part = tmp_path / "dl" / rel
    part.parent.mkdir(parents=True)
    (part.parent / (part.name + ".part")).write_bytes(b"stale")

    queue_row()
    store.record_target("m1", rel, "mkv")
    api = FakeManagerApi(
        MOVIE_DTO,
        [FakeStream(200, [b"abcdefgh"], {"Content-Length": "8"})],
    )

    manager._process(api, store.get("m1"))

    assert (tmp_path / "dl" / rel).read_bytes() == b"abcdefgh"


def test_416_means_the_part_was_already_complete(tmp_path, repoints):
    manager, _ = make_manager(repoints)
    rel = "Movies/The Movie (2019)/The Movie (2019).mkv"
    part = tmp_path / "dl" / rel
    part.parent.mkdir(parents=True)
    (part.parent / (part.name + ".part")).write_bytes(b"abcdefgh")

    queue_row()
    store.record_target("m1", rel, "mkv")
    api = FakeManagerApi(
        MOVIE_DTO,
        [FakeStream(416, [], {"Content-Range": "bytes */8"}, already_complete=True)],
    )

    manager._process(api, store.get("m1"))

    assert store.get("m1").state == store.DONE
    assert (tmp_path / "dl" / rel).read_bytes() == b"abcdefgh"


def test_a_size_mismatch_requeues_with_the_watermark_then_fails(
    tmp_path, repoints, monkeypatch
):
    monkeypatch.setattr(manager_module, "BACKOFF_SECONDS", 0.0)
    manager, _ = make_manager(repoints)

    def truncated_api():
        return FakeManagerApi(
            MOVIE_DTO,
            [
                FakeStream(
                    200,
                    [b"abcd"],  # four of eight bytes, then EOF
                    {"Content-Length": "8", "Content-Disposition": DISPOSITION},
                )
            ],
        )

    manager._process(truncated_api(), queue_row())
    first = store.get("m1")
    assert first.state == store.QUEUED  # attempt 1 of 3: requeued in place
    assert first.bytes_done == 4  # the Range resume watermark

    manager._process(truncated_api(), store.claim())
    manager._process(truncated_api(), store.claim())
    assert store.get("m1").state == store.FAILED
    assert "size mismatch" in store.get("m1").error


def test_unauthorized_fails_immediately_no_retry(repoints):
    manager, _ = make_manager(repoints)

    class Api403(FakeManagerApi):
        def download_stream(self, item_id, start=0):
            raise Unauthorized("GET -> 403")

    manager._process(Api403(MOVIE_DTO, []), queue_row())

    row = store.get("m1")
    assert row.state == store.FAILED
    assert "not permitted" in row.error


def test_cancel_mid_transfer_removes_everything(tmp_path, repoints):
    manager, refreshes = make_manager(repoints)
    stream = FakeStream(
        200,
        [b"abcd", b"efgh"],
        {"Content-Length": "8", "Content-Disposition": DISPOSITION},
    )
    stream.on_chunk = lambda index: manager._cancels.add("m1")
    api = FakeManagerApi(MOVIE_DTO, [stream])

    manager._process(api, queue_row())

    assert store.get("m1") is None
    assert list((tmp_path / "dl").rglob("*.part")) == []
    # The directories the transfer created go with it: a cancel used to
    # leave an empty season folder behind on disk (found live, G6a).
    assert not (tmp_path / "dl" / "Movies").exists()
    assert (tmp_path / "dl").exists()  # never the root
    assert repoints["repoint"] == []
    assert refreshes == []


def test_external_subtitles_land_as_sidecars(tmp_path, repoints):
    manager, _ = make_manager(repoints)
    dto = dict(MOVIE_DTO)
    dto["MediaSources"] = [
        {
            "Id": "src1",
            "Container": "mkv",
            "Size": 8,
            "MediaStreams": [
                {
                    "Type": "Subtitle",
                    "IsExternal": True,
                    "Index": 3,
                    "Codec": "subrip",
                    "Language": "eng",
                },
                {
                    "Type": "Subtitle",
                    "IsExternal": False,
                    "Index": 4,
                    "Codec": "subrip",
                },
                {"Type": "Audio", "Index": 1},
            ],
        }
    ]
    api = FakeManagerApi(
        dto,
        [
            FakeStream(
                200,
                [b"abcdefgh"],
                {"Content-Length": "8", "Content-Disposition": DISPOSITION},
            )
        ],
    )

    manager._process(api, queue_row())

    sidecar = tmp_path / "dl" / "Movies/The Movie (2019)/The Movie (2019).eng.srt"
    assert sidecar.read_bytes() == b"sub-bytes"
    assert len(api.downloaded_urls) == 1  # embedded and audio streams skipped


def test_unsupported_type_settles_failed(repoints):
    manager, _ = make_manager(repoints)
    api = FakeManagerApi({"Id": "m1", "Type": "Book", "MediaSources": []}, [])

    manager._process(api, queue_row())

    assert store.get("m1").state == store.FAILED


def test_no_free_space_refuses_before_touching_the_network(repoints, monkeypatch):
    manager, _ = make_manager(repoints)
    monkeypatch.setattr(manager_module.files, "free_space_ok", lambda root, size: False)
    api = FakeManagerApi(MOVIE_DTO, [])

    manager._process(api, queue_row())

    row = store.get("m1")
    assert row.state == store.FAILED and "free space" in row.error
    assert api.stream_calls == []


def test_reconcile_restores_missing_files_and_reasserts_present_ones(
    tmp_path, repoints
):
    manager, refreshes = make_manager(repoints)
    rel = "Movies/Kept (2019)/kept.mkv"
    kept = tmp_path / "dl" / rel
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"x")
    queue_row("kept")
    store.finish("kept", rel, "mkv", 1)
    queue_row("ghost")
    store.finish("ghost", "Movies/Ghost (2019)/ghost.mkv", "mkv", 1)

    manager._reconcile_once()

    assert ("kept", str(tmp_path / "dl")) in repoints["repoint"]
    assert ("ghost", str(tmp_path / "dl")) in repoints["restore"]
    # Removed, not left failed: a failed row kept the tag, the badge and the
    # leftovers, so the item went on advertising itself as downloaded.
    assert store.get("ghost") is None
    assert store.get("kept").state == store.DONE
    assert refreshes == [["video"]]


def test_remove_restores_deletes_and_prunes(tmp_path, repoints):
    manager, refreshes = make_manager(repoints)
    rel = "Movies/The Movie (2019)/The Movie (2019).mkv"
    final = tmp_path / "dl" / rel
    final.parent.mkdir(parents=True)
    final.write_bytes(b"x")
    (final.parent / "The Movie (2019).eng.srt").write_bytes(b"s")
    queue_row()
    store.finish("m1", rel, "mkv", 1)
    with sync_db.Database("kofin") as opened:
        store.set_restore_filename_on(opened.cursor, "m1", "plugin://old")

    manager._apply_remove("m1")

    assert repoints["restore"] == [("m1", str(tmp_path / "dl"))]
    assert repoints["unstamp"] == ["m1"]
    assert repoints["unbadge"] == ["m1"]
    assert store.get("m1") is None
    assert not final.exists()
    assert not final.parent.exists()  # sidecar went with it, dir pruned
    assert (tmp_path / "dl").exists()  # never the root itself
    # Immediate, unlike a completion: the row has to leave the list the user
    # is looking at.
    assert refreshes == [["video"]]


EPISODE_DTO = {
    "Id": "e1",
    "Type": "Episode",
    "Name": "Ep One",
    "SeriesId": "show1",
    "SeriesName": "The Show",
    "ParentIndexNumber": 19,
    "UserData": {},
    "MediaSources": [{"Id": "s", "Container": "avi", "Size": 4, "MediaStreams": []}],
}


def _episode_api(item_id, filename):
    dto = dict(EPISODE_DTO, Id=item_id)
    return FakeManagerApi(
        dto,
        [
            FakeStream(
                200,
                [b"abcd"],
                {
                    "Content-Length": "4",
                    "Content-Disposition": 'attachment; filename="%s"' % filename,
                },
            )
        ],
    )


def test_siblings_share_one_season_directory(tmp_path, repoints):
    """Found live: the collision suffix keyed on the *item*, so every episode
    of a season saw its siblings' rows as a clash and each landed in a
    directory of its own — four episodes across three folders. Ownership is
    the series, not the episode."""
    manager, _ = make_manager(repoints)

    manager._process(_episode_api("e1", "one.avi"), queue_row("e1"))
    manager._process(_episode_api("e2", "two.avi"), queue_row("e2"))

    assert store.get("e1").rel_path == "Shows/The Show/Season 19/one.avi"
    assert store.get("e2").rel_path == "Shows/The Show/Season 19/two.avi"
    season = tmp_path / "dl" / "Shows/The Show/Season 19"
    assert sorted(p.name for p in season.iterdir()) == ["one.avi", "two.avi"]


def test_a_different_show_with_the_same_name_still_separates(tmp_path, repoints):
    """The suffix is still there for the case it was written for: two shows
    whose names sanitize identically get one directory each, keyed on the
    series id."""
    manager, _ = make_manager(repoints)
    manager._process(_episode_api("e1", "one.avi"), queue_row("e1"))

    other = dict(EPISODE_DTO, Id="x1", SeriesId="show2")
    api = FakeManagerApi(
        other,
        [
            FakeStream(
                200,
                [b"abcd"],
                {
                    "Content-Length": "4",
                    "Content-Disposition": 'attachment; filename="one.avi"',
                },
            )
        ],
    )
    manager._process(api, queue_row("x1"))

    assert store.get("x1").rel_path == "Shows/The Show [show2]/Season 19/one.avi"


def test_a_shutdown_mid_transfer_leaves_the_row_recoverable(tmp_path, repoints):
    """A stop is not a failure: the row must stay active so the next start's
    recover_interrupted re-queues it and the .part resumes with a Range.
    Found live (G6b) — quitting Kodi mid-download settled the row as failed
    and nothing ever picked it up again."""
    from kofin.core.http import ServerUnreachable

    manager, _ = make_manager(repoints)
    stream = FakeStream(
        200,
        [b"abcd", ServerUnreachable("stream abandoned while stopping")],
        {"Content-Length": "8", "Content-Disposition": DISPOSITION},
    )
    stream.on_chunk = lambda index: manager._stop.set()
    api = FakeManagerApi(MOVIE_DTO, [stream])

    manager._process(api, queue_row())

    row = store.get("m1")
    assert row.state == store.ACTIVE
    assert row.bytes_done == 4  # the watermark survives for the resume
    assert (tmp_path / "dl" / row.rel_path).with_suffix(".mkv.part").exists()

    manager._stop.clear()
    assert store.recover_interrupted() == 1
    assert store.get("m1").state == store.QUEUED


def test_an_outage_releases_the_row_back_to_queued(tmp_path, repoints):
    """Claiming while offline would spend each item's three attempts and
    settle it failed, so a season queued offline came back as a list of
    failures instead of a list of downloads. Released to *queued*, not left
    active: recover_interrupted runs only at manager start, so an active row
    would sit stuck until the next service restart (the phase-3 fix)."""
    from tests.unit.fakes import FakeWindow

    manager, _ = make_manager(repoints)
    queue_row("m1")
    store.recover_interrupted()  # back to queued, as a real hold would leave it

    FakeWindow.store = {"kofin.online": "false"}
    row = store.claim()
    manager._retry_or_fail(row, "connection lost")

    assert store.get("m1").state == store.QUEUED  # claimable on reconnect


def test_an_outage_mid_transfer_is_not_a_failure(tmp_path, repoints):
    from kofin.core.http import ServerUnreachable
    from tests.unit.fakes import FakeWindow

    manager, _ = make_manager(repoints)
    stream = FakeStream(
        200,
        [b"abcd", ServerUnreachable("connection reset")],
        {"Content-Length": "8", "Content-Disposition": DISPOSITION},
    )
    stream.on_chunk = lambda index: FakeWindow.store.update({"kofin.online": "false"})
    api = FakeManagerApi(MOVIE_DTO, [stream])

    manager._process(api, queue_row())

    row = store.get("m1")
    assert row.state == store.QUEUED  # the offline hold re-claims it on reconnect
    assert row.bytes_done == 4


# -- the transcode path (plan W3.1) -------------------------------------------

TRANSCODE_MOVIE = dict(
    MOVIE_DTO,
    RunTimeTicks=1_000_000_000,  # 100 s
    MediaSources=[
        {
            "Id": "src1",
            "Container": "mkv",
            "Size": 8,
            "RunTimeTicks": 1_000_000_000,
            "MediaStreams": [],
        }
    ],
)

TRANSCODE_ANSWER = {
    "PlaySessionId": "ps1",
    "MediaSources": [
        {
            "SupportsDirectPlay": False,
            "TranscodingUrl": "/Videos/m1/stream.mp4?api_key=k",
            "TranscodingContainer": "mp4",
        }
    ],
}


def transcode_env(monkeypatch, probed=100.0):
    FakeAddon.store["downloadsTranscode"] = "true"
    monkeypatch.setattr(
        manager_module.probe, "duration_seconds", lambda path, container: probed
    )


def test_over_limit_transcodes_names_and_finishes(tmp_path, repoints, monkeypatch):
    transcode_env(monkeypatch)
    manager, refreshes = make_manager(repoints)
    api = FakeManagerApi(
        TRANSCODE_MOVIE,
        [],
        playback=TRANSCODE_ANSWER,
        transcode_streams=[FakeStream(200, [b"abcd", b"efgh"])],
    )

    manager._process(api, queue_row())

    row = store.get("m1")
    assert row.state == store.DONE
    assert row.quality == store.QUALITY_TRANSCODE
    assert row.container == "mp4"
    # No Content-Disposition on a transcode: the fallback names it.
    assert row.rel_path == "Movies/The Movie (2019)/The Movie.mp4"
    assert (tmp_path / "dl" / row.rel_path).read_bytes() == b"abcdefgh"
    assert api.transcode_urls == ["http://s/Videos/m1/stream.mp4?api_key=k"]
    # The job was closed by name, and the slot came back.
    assert api.closed_transcodes == [("dev1", "ps1")]
    assert manager._transcode_slot.acquire(blocking=False)
    manager._flush_refresh(force=True)
    assert repoints["repoint"] and refreshes


def test_within_limits_downloads_the_original_with_transcoding_on(
    tmp_path, repoints, monkeypatch
):
    transcode_env(monkeypatch)
    manager, _ = make_manager(repoints)
    api = FakeManagerApi(
        MOVIE_DTO,
        [
            FakeStream(
                200,
                [b"abcdefgh"],
                {"Content-Length": "8", "Content-Disposition": DISPOSITION},
            )
        ],
        playback={"MediaSources": [{"SupportsDirectPlay": True}]},
    )

    manager._process(api, queue_row())

    row = store.get("m1")
    assert row.state == store.DONE
    assert row.quality == store.QUALITY_ORIGINAL
    assert row.container == "mkv"
    assert api.transcode_urls == []


def test_a_truncated_transcode_requeues(tmp_path, repoints, monkeypatch):
    """The dead-encoder case: clean EOF, short duration — the probe is the
    only thing that can call it."""
    transcode_env(monkeypatch, probed=42.0)  # of 100 s
    manager, _ = make_manager(repoints)
    api = FakeManagerApi(
        TRANSCODE_MOVIE,
        [],
        playback=TRANSCODE_ANSWER,
        transcode_streams=[FakeStream(200, [b"abcd"])],
    )
    monkeypatch.setattr(manager_module, "BACKOFF_SECONDS", 0.0)

    manager._process(api, queue_row())

    assert store.get("m1").state == store.QUEUED  # attempt 1 of 3, re-queued
    assert api.closed_transcodes == [("dev1", "ps1")]


def test_a_transcode_retry_starts_from_a_clean_part(tmp_path, repoints, monkeypatch):
    transcode_env(monkeypatch)
    manager, _ = make_manager(repoints)
    row = queue_row()
    rel_path = "Movies/The Movie (2019)/The Movie.mp4"
    store.record_target("m1", rel_path, "mp4")
    store.record_details("m1", "movie", "", 0, "", store.QUALITY_TRANSCODE)
    part = tmp_path / "dl" / (rel_path + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"stale bytes from the dead attempt")
    row = store.get("m1")
    row.state = store.ACTIVE

    api = FakeManagerApi(
        TRANSCODE_MOVIE,
        [],
        playback=TRANSCODE_ANSWER,
        transcode_streams=[FakeStream(200, [b"fresh"])],
    )
    manager._process(api, row)

    assert (tmp_path / "dl" / rel_path).read_bytes() == b"fresh"


def test_a_kind_flip_between_attempts_unfreezes_the_target(
    tmp_path, repoints, monkeypatch
):
    """Settings moved between attempts (transcode -> original): the frozen
    .mp4 name and the resume semantics are both wrong, so the target resets
    and the original names itself from its own response."""
    manager, _ = make_manager(repoints)
    queue_row()
    stale = "Movies/The Movie (2019)/The Movie.mp4"
    store.record_target("m1", stale, "mp4")
    store.record_details("m1", "movie", "", 8, "", store.QUALITY_TRANSCODE)
    part = tmp_path / "dl" / (stale + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"half a transcode")
    row = store.get("m1")
    row.state = store.ACTIVE

    api = FakeManagerApi(
        MOVIE_DTO,
        [
            FakeStream(
                200,
                [b"abcdefgh"],
                {"Content-Length": "8", "Content-Disposition": DISPOSITION},
            )
        ],
    )
    manager._process(api, row)

    finished = store.get("m1")
    assert finished.state == store.DONE
    assert finished.rel_path == "Movies/The Movie (2019)/The Movie (2019).mkv"
    assert not part.exists()
    assert api.stream_calls == [0]  # never a Range against the stale part


def test_cancel_while_parked_on_the_transcode_slot(tmp_path, repoints, monkeypatch):
    transcode_env(monkeypatch)
    manager, _ = make_manager(repoints)
    manager._transcode_slot.acquire()  # another worker is transcoding
    row = queue_row()
    with manager._cancels_lock:
        manager._cancels.add("m1")
    api = FakeManagerApi(TRANSCODE_MOVIE, [], playback=TRANSCODE_ANSWER)

    manager._process(api, row)

    assert store.get("m1") is None  # cancelled cleanly, nothing written


def test_a_transcode_sidecars_embedded_text_subtitles(tmp_path, repoints, monkeypatch):
    transcode_env(monkeypatch)
    manager, _ = make_manager(repoints)
    item = dict(
        TRANSCODE_MOVIE,
        MediaSources=[
            {
                "Id": "src1",
                "Container": "mkv",
                "Size": 8,
                "RunTimeTicks": 1_000_000_000,
                "MediaStreams": [
                    {
                        "Type": "Subtitle",
                        "Index": 2,
                        "IsExternal": True,
                        "Codec": "subrip",
                        "Language": "eng",
                    },
                    {
                        "Type": "Subtitle",
                        "Index": 3,
                        "IsTextSubtitleStream": True,
                        "Codec": "ass",
                        "Language": "eng",
                    },
                    {
                        "Type": "Subtitle",
                        "Index": 4,
                        "Codec": "pgssub",
                        "Language": "eng",
                    },
                ],
            }
        ],
    )
    api = FakeManagerApi(
        item,
        [],
        playback=TRANSCODE_ANSWER,
        transcode_streams=[FakeStream(200, [b"abcdefgh"])],
    )

    manager._process(api, queue_row())

    directory = tmp_path / "dl" / "Movies/The Movie (2019)"
    assert (directory / "The Movie.eng.srt").exists()  # the external sidecar
    assert (directory / "The Movie.eng.2.srt").exists()  # the embedded track
    # The image track stays lost: nothing fetched a pgs.
    assert not any("pgs" in url for url in api.downloaded_urls)
    assert any("/3.srt" in url for url in api.downloaded_urls)


# -- music (plan W3.2) --------------------------------------------------------

SONG_DTO = {
    "Id": "a1",
    "Type": "Audio",
    "Name": "Opening Track",
    "AlbumId": "album1",
    "AlbumArtist": "The Band",
    "Album": "Greatest Hits",
    "IndexNumber": 1,
    "UserData": {"Played": False},
    "MediaSources": [{"Id": "s1", "Container": "flac", "Size": 8, "MediaStreams": []}],
}


def test_a_song_downloads_into_the_album_directory(tmp_path, repoints, monkeypatch):
    views = []
    monkeypatch.setattr(
        "kofin.sync.playlists.refresh_downloaded_music",
        lambda root=None: views.append(1) or True,
    )
    manager, _ = make_manager(repoints)
    api = FakeManagerApi(
        SONG_DTO,
        [
            FakeStream(
                200,
                [b"abcdefgh"],
                {
                    "Content-Length": "8",
                    "Content-Disposition": 'attachment; filename="01 - Opening Track.flac"',
                },
            )
        ],
    )

    manager._process(api, queue_row("a1"))

    row = store.get("a1")
    assert row.state == store.DONE
    assert row.media_type == "song"
    assert row.series_id == "album1"  # the grouping id: the album
    assert row.rel_path == "Music/The Band/Greatest Hits/01 - Opening Track.flac"
    assert (tmp_path / "dl" / row.rel_path).read_bytes() == b"abcdefgh"
    assert repoints["repoint"] == [("a1", str(tmp_path / "dl"))]
    assert views == [1]  # the Downloaded-music view exists from song one


# -- the progress bar (plan W3.4) ---------------------------------------------


class RecordingProgress:
    def __init__(self):
        self.calls = []

    def begin(self, item_id, name, total):
        self.calls.append(("begin", item_id, name, total))

    def tick(self, item_id, done):
        self.calls.append(("tick", item_id, done))

    def finish(self, item_id, completed):
        self.calls.append(("finish", item_id, completed))

    def idle(self):
        self.calls.append(("idle",))

    def close(self):
        self.calls.append(("close",))


def test_the_progress_bar_follows_a_transfer(tmp_path, repoints):
    manager, _ = make_manager(repoints)
    recorder = RecordingProgress()
    manager._progress = recorder
    api = FakeManagerApi(
        MOVIE_DTO,
        [
            FakeStream(
                200,
                [b"abcdefgh"],
                {"Content-Length": "8", "Content-Disposition": DISPOSITION},
            )
        ],
    )

    manager._process(api, queue_row())

    assert ("begin", "m1", "The Movie", 8) in recorder.calls
    assert ("finish", "m1", True) in recorder.calls

    manager.stop()
    assert ("close",) in recorder.calls


def test_a_retry_reports_finish_without_completion(tmp_path, repoints, monkeypatch):
    monkeypatch.setattr(manager_module, "BACKOFF_SECONDS", 0.0)
    manager, _ = make_manager(repoints)
    recorder = RecordingProgress()
    manager._progress = recorder
    api = FakeManagerApi(
        MOVIE_DTO,
        [
            FakeStream(
                200,
                [b"abcd"],  # short of the stated 8: a size mismatch
                {"Content-Length": "8", "Content-Disposition": DISPOSITION},
            )
        ],
    )

    manager._process(api, queue_row())

    assert ("finish", "m1", False) in recorder.calls  # requeued, not counted


def test_a_transcode_begins_with_the_url_estimate(tmp_path, repoints, monkeypatch):
    transcode_env(monkeypatch)
    manager, _ = make_manager(repoints)
    recorder = RecordingProgress()
    manager._progress = recorder
    answer = {
        "PlaySessionId": "ps1",
        "MediaSources": [
            {
                "SupportsDirectPlay": False,
                "TranscodingUrl": "/Videos/m1/stream.mp4?VideoBitrate=800000",
                "TranscodingContainer": "mp4",
            }
        ],
    }
    api = FakeManagerApi(
        TRANSCODE_MOVIE,
        [],
        playback=answer,
        transcode_streams=[FakeStream(200, [b"abcd"])],
    )

    manager._process(api, queue_row())

    begins = [call for call in recorder.calls if call[0] == "begin"]
    assert begins == [("begin", "m1", "The Movie", 800_000 * 100 // 8)]


# -- retention (plan W4.2) and exported-metadata pruning (W4.3) ---------------


def seed_done(item_id, origin, queued_at=100):
    store.queue(
        store.Download(
            jellyfin_id=item_id, media_type="movie", origin=origin, queued_at=queued_at
        )
    )
    store.claim()
    store.record_details(item_id, "movie", "", 0, "")
    store.finish(item_id, "Movies/%s/%s.mkv" % (item_id, item_id), "mkv", 1)


def test_retention_sweeps_every_watched_download_in_the_silent_mode(
    repoints, monkeypatch
):
    """The sweep is the backstop for what the end-of-playback offer misses,
    and it no longer cares who queued the item — the origin split meant a
    download the user asked for was never collected here at all, however
    long ago they had watched it."""
    FakeAddon.store["downloadsDeleteAfterWatching"] = "true"
    FakeAddon.store["downloadsDeleteAutomatically"] = "true"
    manager, _ = make_manager(repoints)
    seed_done("auto-watched", "auto:s1", queued_at=100)
    seed_done("auto-fresh", "auto:s1", queued_at=101)
    seed_done("user-watched", store.ORIGIN_USER, queued_at=102)

    watched = {"auto-watched", "user-watched"}
    monkeypatch.setattr(
        manager_module,
        "_watched_locally",
        lambda row: row.jellyfin_id in watched,
    )
    removed = []
    monkeypatch.setattr(manager, "_apply_remove", lambda i: removed.append(i))

    manager._retention_sweep()
    assert removed == ["auto-watched", "user-watched"]  # never the unwatched


def test_retention_stands_down_when_removal_is_a_question(repoints, monkeypatch):
    """With the confirm mode chosen the sweep must do nothing: it has nobody
    to ask — what it notices may have finished hours ago — so the prompt
    belongs to the end-of-playback path alone."""
    FakeAddon.store["downloadsDeleteAfterWatching"] = "true"
    FakeAddon.store["downloadsDeleteAutomatically"] = "false"
    manager, _ = make_manager(repoints)
    seed_done("auto-watched", "auto:s1")
    monkeypatch.setattr(manager_module, "_watched_locally", lambda row: True)
    removed = []
    monkeypatch.setattr(manager, "_apply_remove", lambda i: removed.append(i))

    manager._retention_sweep()
    assert removed == []

    FakeAddon.store["downloadsDeleteAfterWatching"] = "false"
    FakeAddon.store["downloadsDeleteAutomatically"] = "true"
    manager._retention_sweep()
    assert removed == []  # the master gate


def test_retention_never_deletes_the_playing_file(repoints, monkeypatch):
    FakeAddon.store["downloadsDeleteAfterWatching"] = "true"
    FakeAddon.store["downloadsDeleteAutomatically"] = "true"
    manager, _ = make_manager(repoints)
    seed_done("auto-playing", "auto:s1")
    monkeypatch.setattr(manager_module, "_watched_locally", lambda row: True)
    monkeypatch.setattr(manager, "_playing_now", lambda row: True)
    removed = []
    monkeypatch.setattr(manager, "_apply_remove", lambda i: removed.append(i))

    manager._retention_sweep()
    assert removed == []


def test_watched_locally_reads_kodis_own_playcount(tmp_path):
    import sqlite3

    video_path = tmp_path / "MyVideos.db"
    connection = sqlite3.connect(str(video_path))
    connection.execute(
        "CREATE TABLE files (idFile INTEGER PRIMARY KEY, playCount INTEGER)"
    )
    connection.execute("INSERT INTO files VALUES (7, 2), (8, NULL)")
    connection.commit()
    connection.close()
    sync_db.set_path_override("video", str(video_path))

    with sync_db.Database("kofin") as opened:
        opened.cursor.execute(
            "INSERT INTO jellyfin (jellyfin_id, kodi_id, kodi_fileid, kodi_pathid, media_type) "
            "VALUES ('w1', 1, 7, 1, 'movie'), ('w2', 2, 8, 1, 'movie')"
        )

    watched = store.Download(jellyfin_id="w1", media_type="movie")
    fresh = store.Download(jellyfin_id="w2", media_type="movie")
    unmapped = store.Download(jellyfin_id="w3", media_type="movie")
    assert manager_module._watched_locally(watched) is True
    assert manager_module._watched_locally(fresh) is False
    assert manager_module._watched_locally(unmapped) is False


# -- the stale sweep (plan W4.8) ---------------------------------------------


DAY = manager_module.SECONDS_PER_DAY


def seed_stale(item_id, days_ago, media_type="movie"):
    """A finished download whose transfer ended ``days_ago`` days ago."""
    import time as time_module

    done_at = int(time_module.time() - days_ago * DAY)
    store.queue(
        store.Download(
            jellyfin_id=item_id,
            media_type=media_type,
            origin=store.ORIGIN_USER,
            queued_at=done_at,
        )
    )
    store.claim(None)
    store.record_details(item_id, media_type, "", 0, "")
    store.finish(
        item_id, "Movies/%s/%s.mkv" % (item_id, item_id), "mkv", 1, done_at=done_at
    )


def touches(monkeypatch, answers):
    """Stub the Kodi read: ``{id: (lastPlayed epoch, has resume point)}``,
    with anything unlisted answering None — the unmapped case."""
    monkeypatch.setattr(
        manager_module,
        "_last_touch",
        lambda row: answers.get(row.jellyfin_id),
    )


def enable_stale(days=30):
    FakeAddon.store["downloadsDeleteStale"] = "true"
    FakeAddon.store["downloadsStaleDays"] = str(days)


def test_stale_sweep_removes_what_nobody_has_touched(repoints, monkeypatch):
    enable_stale(days=30)
    manager, _ = make_manager(repoints)
    seed_stale("old", days_ago=40)
    seed_stale("recent", days_ago=10)
    touches(monkeypatch, {"old": (0.0, False), "recent": (0.0, False)})
    removed = []
    monkeypatch.setattr(manager, "_apply_remove", lambda i: removed.append(i))

    manager._stale_sweep()
    assert removed == ["old"]


def test_stale_sweep_keeps_anything_with_a_resume_point(repoints, monkeypatch):
    """The exemption the feature is sold on: a film you are part-way through
    is in progress, not abandoned, however long it has sat — and it is
    exactly what a pure age clock would take first."""
    enable_stale(days=30)
    manager, _ = make_manager(repoints)
    seed_stale("halfway", days_ago=400)
    seed_stale("untouched", days_ago=400)
    touches(monkeypatch, {"halfway": (0.0, True), "untouched": (0.0, False)})
    removed = []
    monkeypatch.setattr(manager, "_apply_remove", lambda i: removed.append(i))

    manager._stale_sweep()
    assert removed == ["untouched"]


def test_stale_clock_is_the_last_touch_not_the_download_date(repoints, monkeypatch):
    """Re-watching something resets it: a download from a year ago that was
    played last night is not stale, and one played eleven months ago is."""
    import time as time_module

    enable_stale(days=30)
    manager, _ = make_manager(repoints)
    seed_stale("rewatched", days_ago=365)
    seed_stale("watched-once", days_ago=365)
    now = time_module.time()
    touches(
        monkeypatch,
        {
            "rewatched": (now - 1 * DAY, False),
            "watched-once": (now - 330 * DAY, False),
        },
    )
    removed = []
    monkeypatch.setattr(manager, "_apply_remove", lambda i: removed.append(i))

    manager._stale_sweep()
    assert removed == ["watched-once"]


def test_stale_sweep_answers_to_its_own_setting_alone(repoints, monkeypatch):
    """Off by default, and independent of the watched pair in both
    directions: nesting it under downloadsDeleteAfterWatching would make the
    case it exists for — downloaded, never watched — unreachable."""
    manager, _ = make_manager(repoints)
    seed_stale("old", days_ago=400)
    touches(monkeypatch, {"old": (0.0, False)})
    removed = []
    monkeypatch.setattr(manager, "_apply_remove", lambda i: removed.append(i))

    manager._stale_sweep()
    assert removed == []  # nothing set: the default is to keep everything

    FakeAddon.store["downloadsDeleteAfterWatching"] = "true"
    FakeAddon.store["downloadsDeleteAutomatically"] = "true"
    manager._stale_sweep()
    assert removed == []  # the watched pair does not turn this on

    FakeAddon.store["downloadsDeleteAfterWatching"] = "false"
    FakeAddon.store["downloadsDeleteAutomatically"] = "false"
    enable_stale(days=30)
    manager._stale_sweep()
    assert removed == ["old"]  # nor off


def test_stale_sweep_skips_songs_unknown_ages_and_the_playing_file(
    repoints, monkeypatch
):
    enable_stale(days=30)
    manager, _ = make_manager(repoints)
    seed_stale("song", days_ago=400, media_type="song")
    seed_stale("unmapped", days_ago=400)
    seed_stale("playing", days_ago=400)
    seed_stale("undated", days_ago=400)
    store.finish("undated", "Movies/undated/undated.mkv", "mkv", 1, done_at=0)
    # "unmapped" is absent from the answers, so _last_touch reports None.
    touches(monkeypatch, {"playing": (0.0, False), "undated": (0.0, False)})
    monkeypatch.setattr(
        manager, "_playing_now", lambda row: row.jellyfin_id == "playing"
    )
    removed = []
    monkeypatch.setattr(manager, "_apply_remove", lambda i: removed.append(i))

    manager._stale_sweep()
    assert removed == []


def test_stale_sweep_collects_watched_downloads_too(repoints, monkeypatch):
    """With the watched pair off, this is the only thing that ever clears a
    download somebody finished — and a month later it is stale either way."""
    enable_stale(days=30)
    FakeAddon.store["downloadsDeleteAfterWatching"] = "false"
    manager, _ = make_manager(repoints)
    seed_stale("finished", days_ago=90)
    monkeypatch.setattr(manager_module, "_watched_locally", lambda row: True)
    touches(monkeypatch, {"finished": (0.0, False)})
    removed = []
    monkeypatch.setattr(manager, "_apply_remove", lambda i: removed.append(i))

    manager._stale_sweep()
    assert removed == ["finished"]


def test_last_touch_reads_kodis_own_rows(tmp_path):
    """lastPlayed and the RESUME bookmark off the repointed file row — local
    truth, which is what lets the sweep run offline."""
    import sqlite3

    video_path = tmp_path / "MyVideos.db"
    connection = sqlite3.connect(str(video_path))
    connection.execute(
        "CREATE TABLE files (idFile INTEGER PRIMARY KEY, lastPlayed TEXT)"
    )
    connection.execute(
        "CREATE TABLE bookmark (idBookmark INTEGER PRIMARY KEY, idFile INTEGER, type INTEGER)"
    )
    connection.execute(
        "INSERT INTO files VALUES (7, '2026-01-02 03:04:05'), (8, NULL), (9, NULL)"
    )
    # A resume bookmark on 8; a type-2 (episode part) one on 9, which is not
    # a resume point and must not protect it.
    connection.execute("INSERT INTO bookmark VALUES (1, 8, 1), (2, 9, 2)")
    connection.commit()
    connection.close()
    sync_db.set_path_override("video", str(video_path))

    with sync_db.Database("kofin") as opened:
        opened.cursor.execute(
            "INSERT INTO jellyfin (jellyfin_id, kodi_id, kodi_fileid, kodi_pathid, media_type) "
            "VALUES ('p1', 1, 7, 1, 'movie'), ('p2', 2, 8, 1, 'movie'), "
            "('p3', 3, 9, 1, 'movie')"
        )

    played = manager_module._last_touch(store.Download(jellyfin_id="p1"))
    assert played == (manager_module._as_epoch("2026-01-02 03:04:05"), False)
    assert played[0] > 0
    assert manager_module._last_touch(store.Download(jellyfin_id="p2")) == (0.0, True)
    assert manager_module._last_touch(store.Download(jellyfin_id="p3")) == (0.0, False)
    assert manager_module._last_touch(store.Download(jellyfin_id="p4")) is None


def test_as_epoch_reads_both_spellings_that_reach_the_column():
    """Kodi's own space-separated stamp and the ISO form the sync writers
    hand it (shims.date_played) are the same local time."""
    assert manager_module._as_epoch("2026-01-02 03:04:05") == manager_module._as_epoch(
        "2026-01-02T03:04:05"
    )
    assert manager_module._as_epoch("") == 0.0
    assert manager_module._as_epoch(None) == 0.0
    assert manager_module._as_epoch("never") == 0.0


def test_remove_sweeps_exported_metadata_with_the_directory(tmp_path, repoints):
    manager, _ = make_manager(repoints)
    seed_done("m1", store.ORIGIN_USER)
    directory = tmp_path / "dl" / "Movies" / "m1"
    directory.mkdir(parents=True)
    (directory / "m1.mkv").write_bytes(b"x")
    (directory / "m1.nfo").write_text("<movie/>", encoding="utf-8")
    (directory / "poster.jpg").write_bytes(b"img")
    (directory / "fanart.jpg").write_bytes(b"img")

    manager._apply_remove("m1")

    assert not directory.exists()  # the escape hatch left with its media


# -- the offline segment cache (plan W4.7) ------------------------------------


def test_completion_captures_the_raw_segments(tmp_path, repoints):
    manager, _ = make_manager(repoints)
    api = FakeManagerApi(
        MOVIE_DTO,
        [
            FakeStream(
                200,
                [b"abcdefgh"],
                {"Content-Length": "8", "Content-Disposition": DISPOSITION},
            )
        ],
    )
    api.segments = {"Items": [{"Type": "Intro", "StartTicks": 0, "EndTicks": 10}]}

    manager._process(api, queue_row())

    row = store.get("m1")
    assert row.state == store.DONE
    assert '"Intro"' in row.segments_json  # the raw body, parsed at claim time


def test_a_failed_segment_fetch_never_fails_the_download(tmp_path, repoints):
    manager, _ = make_manager(repoints)
    api = FakeManagerApi(
        MOVIE_DTO,
        [
            FakeStream(
                200,
                [b"abcdefgh"],
                {"Content-Length": "8", "Content-Disposition": DISPOSITION},
            )
        ],
    )
    api.segments = JellyfinError("no segments endpoint")

    manager._process(api, queue_row())

    row = store.get("m1")
    assert row.state == store.DONE
    assert row.segments_json == ""  # unknown, not known-empty


def test_songs_skip_the_segment_capture(tmp_path, repoints, monkeypatch):
    monkeypatch.setattr(
        "kofin.sync.playlists.refresh_downloaded_music", lambda root=None: True
    )
    manager, _ = make_manager(repoints)
    api = FakeManagerApi(
        SONG_DTO,
        [
            FakeStream(
                200,
                [b"abcdefgh"],
                {
                    "Content-Length": "8",
                    "Content-Disposition": 'attachment; filename="01 - Opening Track.flac"',
                },
            )
        ],
    )
    api.segments = RuntimeError("must never be asked")

    manager._process(api, queue_row("a1"))
    assert store.get("a1").state == store.DONE


def test_start_backfills_missing_segment_caches(tmp_path, repoints, monkeypatch):
    seed_done("cached", store.ORIGIN_USER, queued_at=100)
    store.set_segments("cached", '{"Items": []}')  # known-empty: kept
    seed_done("wanting", store.ORIGIN_USER, queued_at=101)

    asked = []

    class SegmentsApi:
        def media_segments(self, item_id):
            asked.append(item_id)
            return {"Items": [{"Type": "Intro", "StartTicks": 0, "EndTicks": 10}]}

        def close(self):
            pass

    manager = DownloadManager(
        api_factory=SegmentsApi,
        refresh=lambda: None,
        stopping=threading.Event(),
    )
    manager._backfill_segment_caches()

    assert asked == ["wanting"]  # never the known-empty row
    assert '"Intro"' in store.get("wanting").segments_json

    from tests.unit.fakes import FakeWindow

    FakeWindow.store["kofin.online"] = "false"
    asked.clear()
    manager._backfill_segment_caches()
    assert asked == []  # offline: nobody to ask


# -- the notification opt-out -------------------------------------------------


def test_notifications_opt_out_silences_progress_but_never_failures(
    tmp_path, repoints, monkeypatch
):
    """A per-item "Download complete" toast is the noisy one, and an album
    fires a dozen. Failures stay: an opt-out that swallowed them would turn
    "my download did nothing" into an unanswerable question — the same line
    syncPlayNotifications draws."""
    manager, _ = make_manager(repoints)
    shown = []
    monkeypatch.setattr(manager_module.toast, "show", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(manager_module.settings, "localized", lambda i: "L%d %%s" % i)

    FakeAddon.store["downloadsNotify"] = "false"
    manager._toast(30712, "The Movie")  # complete
    assert shown == []

    manager._toast(30713, "The Movie")  # failed
    manager._toast(30715, "The Movie")  # out of space
    assert [call[0] for call in shown] == ["L30713 The Movie", "L30715 The Movie"]

    shown.clear()
    FakeAddon.store["downloadsNotify"] = "true"
    manager._toast(30712, "The Movie")
    assert [call[0] for call in shown] == ["L30712 The Movie"]


def test_a_bulk_removal_refreshes_once_not_per_row(tmp_path, repoints):
    """Removing an album is one menu press and seventeen rows. Each row
    refreshing on its own meant seventeen widget passes for one answer —
    invisible while a removal could only ever be a single item, which is
    what the container remove route changed."""
    manager, refreshes = make_manager(repoints)
    for index in range(3):
        item_id = "s%d" % index
        rel = "Music/A/B/%s.opus" % item_id
        target = tmp_path / "dl" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        queue_row(item_id, media_type="song")
        store.record_details(item_id, "song", "album1", 0, "")
        store.finish(item_id, rel, "opus", 1)
        manager._ops.put(("remove", item_id, "", ""))

    manager._drain_ops()

    assert store.rows() == []
    # One refresh, fired by the last row — the only one that found the ops
    # queue drained — and against the music database, not video.
    assert refreshes == [["music"]]


def test_an_album_announces_once_not_once_per_track(tmp_path, repoints, monkeypatch):
    """Twelve tracks landing in a burst were twelve notifications naming
    songs nobody chose individually. The album is what was asked for, so
    the worker that finishes the last of it is the one that says so."""
    manager, _ = make_manager(repoints)
    shown = []
    monkeypatch.setattr(
        manager_module.toast, "show", lambda *a, **k: shown.append(a[0])
    )
    monkeypatch.setattr(manager_module.settings, "localized", lambda i: "L%d %%s" % i)

    for index in range(3):
        store.queue(store.Download(jellyfin_id="t%d" % index, media_type="song"))
        store.record_details("t%d" % index, "song", "album1", 0, "")

    track = {"Id": "t0", "Name": "Come Together", "Album": "Abbey Road"}
    manager._announce_complete("song", "album1", track)
    assert shown == []  # two still queued

    store.claim(("song",))
    store.finish("t0", "a/0.opus", "opus", 1)
    store.claim(("song",))
    store.finish("t1", "a/1.opus", "opus", 1)
    manager._announce_complete("song", "album1", track)
    assert shown == []  # one still queued

    store.claim(("song",))
    store.finish("t2", "a/2.opus", "opus", 1)
    manager._announce_complete("song", "album1", track)
    assert shown == ["L30712 Abbey Road"]  # the album, not the track

    # A second worker arriving at the same conclusion says nothing.
    manager._announce_complete("song", "album1", track)
    assert shown == ["L30712 Abbey Road"]

    # ... until something new is queued, which re-arms it.
    manager._apply_add("t9", store.ORIGIN_USER, "song")
    store.record_details("t9", "song", "album1", 0, "")
    store.claim(("song",))
    store.finish("t9", "a/9.opus", "opus", 1)
    manager._announce_complete("song", "album1", track)
    assert shown == ["L30712 Abbey Road", "L30712 Abbey Road"]


def test_video_keeps_its_per_item_completion_toast(tmp_path, repoints, monkeypatch):
    """A film or an episode is itself the thing somebody chose, and they
    finish minutes apart — nothing to coalesce."""
    manager, _ = make_manager(repoints)
    shown = []
    monkeypatch.setattr(
        manager_module.toast, "show", lambda *a, **k: shown.append(a[0])
    )
    monkeypatch.setattr(manager_module.settings, "localized", lambda i: "L%d %%s" % i)

    manager._announce_complete("episode", "show1", {"Id": "e1", "Name": "Blood Test"})
    manager._announce_complete("movie", "", {"Id": "m1", "Name": "The Movie"})
    # A stray track with no album is its own unit too.
    manager._announce_complete("song", "", {"Id": "s1", "Name": "Ringtone"})

    assert shown == ["L30712 Blood Test", "L30712 The Movie", "L30712 Ringtone"]


# -- delete every download (the settings button) -------------------------------


def _finished(item_id, rel, root):
    """A done row with a real file behind it."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    queue_row(item_id)
    store.finish(item_id, rel, rel.rsplit(".", 1)[-1], 1)
    with sync_db.Database("kofin") as opened:
        store.set_restore_filename_on(
            opened.cursor, item_id, "plugin://old-%s" % item_id
        )
    return path


def test_remove_all_clears_finished_and_unfinished_alike(tmp_path, repoints):
    manager, refreshes = make_manager(repoints)
    root = tmp_path / "dl"
    movie = _finished("m1", "Movies/The Movie (2019)/The Movie (2019).mkv", root)
    episode = _finished("e1", "Shows/The Show/Season 01/S01E01.mkv", root)
    store.queue(store.Download(jellyfin_id="q1", queued_at=100))  # never started

    manager._apply_remove_all()

    assert store.rows() == []
    assert not movie.exists()
    assert not episode.exists()
    assert not movie.parent.exists()  # directories pruned behind them
    assert root.exists()  # never the root itself
    assert sorted(repoints["restore"]) == [
        ("e1", str(root)),
        ("m1", str(root)),
    ]
    assert sorted(repoints["unstamp"]) == ["e1", "m1"]


def test_remove_all_refreshes_once_for_the_whole_request(tmp_path, repoints):
    """_apply_remove refreshes as soon as the ops queue runs dry, and during
    this walk it is dry from the first row on — so without the suppression a
    single button press became one widget pass per download."""
    manager, refreshes = make_manager(repoints)
    root = tmp_path / "dl"
    for index in range(4):
        _finished("m%d" % index, "Movies/Film %d/film.mkv" % index, root)

    manager._apply_remove_all()

    assert refreshes == [["video"]]


def test_remove_all_on_an_empty_store_does_nothing(tmp_path, repoints):
    manager, refreshes = make_manager(repoints)

    manager._apply_remove_all()

    assert refreshes == []


def test_remove_all_marks_an_active_row_cancelled(tmp_path, repoints):
    """A transfer running right now aborts at its next chunk off this flag —
    the walk cannot reach into the worker that owns it."""
    manager, _refreshes = make_manager(repoints)
    queue_row("a1")  # claim() left it active

    manager._apply_remove_all()

    assert manager._cancelled("a1")


def test_remove_all_is_one_op_not_one_per_row(tmp_path, repoints):
    """The store is read on the worker side. Queueing per row would put a
    whole library's worth of messages through Kodi's notification bus."""
    manager, _refreshes = make_manager(repoints)
    root = tmp_path / "dl"
    for index in range(3):
        _finished("m%d" % index, "Movies/Film %d/film.mkv" % index, root)

    manager.remove_all()

    assert manager._ops.qsize() == 1


# -- holding the queue while something plays -----------------------------------


class FakePlayer:
    playing = False

    def isPlaying(self):
        return FakePlayer.playing

    def getPlayingFile(self):
        return ""


@pytest.fixture
def player(monkeypatch):
    FakePlayer.playing = False
    monkeypatch.setattr(manager_module.xbmc, "Player", FakePlayer)
    return FakePlayer


def test_the_playback_gate_is_off_unless_asked_for(tmp_path, repoints, player):
    manager, _refreshes = make_manager(repoints)
    player.playing = True

    assert manager._paused_for_playback() is False


def test_the_playback_gate_holds_the_queue_while_anything_plays(
    tmp_path, repoints, player
):
    """isPlaying, not state.get_playing_id: the property only covers
    playbacks kofin claimed, and a download competing for bandwidth does not
    care who started the video."""
    manager, _refreshes = make_manager(repoints)
    FakeAddon.store["downloadsPauseDuringPlayback"] = "true"

    assert manager._paused_for_playback() is False
    player.playing = True
    assert manager._paused_for_playback() is True
    player.playing = False
    assert manager._paused_for_playback() is False


def test_a_worker_claims_nothing_while_playback_holds_it(
    tmp_path, repoints, player, monkeypatch
):
    """The gate sits in front of store.claim, so a queued row stays queued
    rather than being picked up and then stalled half-written."""
    manager, _refreshes = make_manager(repoints)
    # The worker only opens and closes its api here; nothing is fetched.
    monkeypatch.setattr(
        manager, "_api_factory", lambda: type("Api", (), {"close": lambda self: None})()
    )
    FakeAddon.store["downloadsPauseDuringPlayback"] = "true"
    store.queue(store.Download(jellyfin_id="m1", queued_at=100))

    claimed = []
    monkeypatch.setattr(
        manager_module.store, "claim", lambda kinds=None: claimed.append(kinds)
    )
    # The wait is what the gate does instead of claiming; ending the worker
    # there keeps this to a single pass of the loop body.
    wake = manager._new_wake()
    monkeypatch.setattr(wake, "wait", lambda timeout=None: manager._stop.set())

    player.playing = True
    manager._run_worker(wake=wake)
    assert claimed == []
    assert store.get("m1").state == store.QUEUED

    # And the same worker claims the moment playback ends.
    manager._stop.clear()
    player.playing = False
    manager._run_worker(wake=wake)
    assert claimed == [manager_module.VIDEO_KINDS]


def test_wake_lets_every_worker_recheck_immediately(tmp_path, repoints):
    """Player.OnStop nudges the pool rather than waiting out the poll — and
    it has to reach *every* worker, not whichever one happens to look."""
    manager, _refreshes = make_manager(repoints)
    wakes = [manager._new_wake() for _ in range(3)]

    manager.wake()

    assert all(event.is_set() for event in wakes)


# -- a download deleted by another app -----------------------------------------


@pytest.fixture
def vanished(monkeypatch):
    """The two halves of "mark it watched", recorded rather than performed:
    the local leg needs a real MyVideos (covered in the repoint L2 suite),
    and the push needs a server."""
    seen = {"local": [], "pushed": []}
    monkeypatch.setattr(
        DownloadManager,
        "_mark_local_watched",
        lambda self, row: seen["local"].append(row.jellyfin_id),
    )
    monkeypatch.setattr(
        DownloadManager,
        "_push_played",
        lambda self, row: seen["pushed"].append(row.jellyfin_id),
    )
    return seen


def test_a_vanished_file_is_cleaned_up_not_just_restored(tmp_path, repoints, vanished):
    """It used to restore the library row and leave a failed store row, so
    the leftovers stayed — sidecars, the empty directory, the tag and the
    badge — and the item went on advertising itself as downloaded."""
    manager, _refreshes = make_manager(repoints)
    root = tmp_path / "dl"
    rel = "Shows/The Show/Season 01/S01E01.mkv"
    (root / rel).parent.mkdir(parents=True)
    (root / rel).with_suffix(".eng.srt").write_bytes(b"s")  # the leftovers
    queue_row("e1", media_type="episode", series_id="show1")
    store.finish("e1", rel, "mkv", 1)

    manager._handle_vanished(store.get("e1"), str(root))

    assert store.get("e1") is None
    assert repoints["restore"] == [("e1", str(root))]
    assert repoints["unstamp"] == ["e1"]
    assert repoints["unbadge"] == ["e1"]
    assert not (root / rel).parent.exists()  # sidecar swept, directories pruned
    assert root.exists()
    assert vanished["local"] == ["e1"] and vanished["pushed"] == ["e1"]


def test_a_vanished_song_is_cleaned_up_but_not_marked_watched(
    tmp_path, repoints, vanished
):
    """The retention sweep's exclusion, for the same reason: a played track
    is not a finished one, and "watched" is not a thing a song is."""
    manager, _refreshes = make_manager(repoints)
    root = tmp_path / "dl"
    rel = "Music/The Band/Greatest Hits/01 Opening.opus"
    (root / rel).parent.mkdir(parents=True)
    queue_row("s1", media_type="song")
    store.finish("s1", rel, "opus", 1)

    manager._handle_vanished(store.get("s1"), str(root))

    assert store.get("s1") is None
    assert vanished["local"] == [] and vanished["pushed"] == []


def test_the_sweep_notices_a_file_deleted_after_the_service_started(
    tmp_path, repoints, vanished
):
    """_reconcile_once runs once per generation, so a mid-session deletion
    used to go unnoticed until the next restart — the item stayed in the
    Downloaded nodes and playing it failed in Kodi rather than falling back
    to the server."""
    manager, refreshes = make_manager(repoints)
    root = tmp_path / "dl"
    rel = "Movies/The Movie (2019)/The Movie (2019).mkv"
    (root / rel).parent.mkdir(parents=True)
    (root / rel).write_bytes(b"x")
    queue_row("m1", media_type="movie")
    store.finish("m1", rel, "mkv", 1)

    manager._sweep_vanished()
    assert store.get("m1").state == store.DONE  # still there: nothing to do

    (root / rel).unlink()  # somebody else's file manager
    manager._sweep_vanished()

    assert store.get("m1") is None
    assert vanished["pushed"] == ["m1"]
    assert refreshes == [["video"]]


def test_the_sweep_leaves_the_file_being_played_alone(
    tmp_path, repoints, vanished, monkeypatch
):
    """A share blinking is not a deletion, and tearing the row down under
    the player would turn a stutter into a lost download."""
    manager, _refreshes = make_manager(repoints)
    queue_row("m1", media_type="movie")
    store.finish("m1", "Movies/Gone/gone.mkv", "mkv", 1)  # no file on disk
    monkeypatch.setattr(DownloadManager, "_playing_now", lambda self, row: True)

    manager._sweep_vanished()

    assert store.get("m1").state == store.DONE


def test_an_offline_played_push_is_parked_for_replay(tmp_path, repoints, monkeypatch):
    """Deleting a file is exactly the thing someone does on a plane."""
    from kofin.downloads import pending

    manager, _refreshes = make_manager(repoints)
    FakeWindow.store["kofin.online"] = "false"
    queue_row("m1", media_type="movie")
    store.finish("m1", "Movies/Gone/gone.mkv", "mkv", 1)

    manager._push_played(store.get("m1"))

    assert [(row.jellyfin_id, row.played) for row in pending.rows()] == [("m1", True)]


def test_a_music_worker_cannot_swallow_a_video_workers_wake(tmp_path, repoints):
    """The claim-latency bug, pinned.

    One Event shared by the pool meant the first worker to notice cleared it
    for everybody. When that was a music worker and the row was an episode,
    it drained the op, found nothing it could claim, and went back to sleep —
    leaving the video workers, which *could* have taken the row, in a 30 s
    wait with the Event already clear. Measured on the Omega box as 31-32 s
    before a user-initiated download started.
    """
    manager, _refreshes = make_manager(repoints)
    music_wake = manager._new_wake()
    video_wake = manager._new_wake()

    manager.submit(["e1"], media_types=["Episode"])

    # A music worker gets there first: it drains the op and clears its own.
    manager._drain_ops()
    music_wake.clear()

    # The video worker's wake is untouched, so it does not sleep through the
    # row it is the only pool able to claim.
    assert video_wake.is_set()
    assert store.claim(manager_module.MUSIC_KINDS) is None
    assert store.claim(manager_module.VIDEO_KINDS).jellyfin_id == "e1"


def test_every_op_kind_reaches_every_worker(tmp_path, repoints):
    """submit/cancel/remove/remove_all all have to fan out — a cancel that
    only reached one worker is the same bug wearing a different hat."""
    manager, _refreshes = make_manager(repoints)
    wakes = [manager._new_wake() for _ in range(3)]

    for call in (
        lambda: manager.submit(["x1"]),
        lambda: manager.cancel("x1"),
        lambda: manager.remove("x1"),
        manager.remove_all,
    ):
        for event in wakes:
            event.clear()
        call()
        assert all(event.is_set() for event in wakes)


def test_stop_releases_every_worker(tmp_path, repoints):
    manager, _refreshes = make_manager(repoints)
    wakes = [manager._new_wake() for _ in range(3)]

    manager.stop()

    assert all(event.is_set() for event in wakes)


def test_draining_an_add_renotifies_the_other_workers(tmp_path, repoints):
    """The actual claim-latency bug, pinned.

    ``submit`` wakes the pool when it *enqueues*, but the row does not exist
    until a worker gets to ``_drain_ops`` and store.queue returns. The worker
    able to claim it has usually spent its wake by then — it drained nothing
    (another pool got the op first), claimed nothing (no row yet), found the
    queue empty and slept. Measured live: queued 1.6 s after the request,
    claimed 31 s after that.
    """
    manager, _refreshes = make_manager(repoints)
    drainer = manager._new_wake()
    other = manager._new_wake()
    manager.submit(["e1"], media_types=["Episode"])
    other.clear()  # the other worker already spent its wake and is asleep

    manager._drain_ops(own=drainer)

    assert other.is_set(), "the worker that can claim was never re-notified"


def test_the_draining_worker_is_not_renotified(tmp_path, repoints):
    """It is about to try to claim anyway; setting its own event would just
    buy an extra spin round the loop."""
    manager, _refreshes = make_manager(repoints)
    drainer = manager._new_wake()
    manager.submit(["e1"], media_types=["Episode"])
    drainer.clear()

    manager._drain_ops(own=drainer)

    assert not drainer.is_set()


def test_a_drain_that_queues_nothing_notifies_nobody(tmp_path, repoints):
    """A duplicate add, or a drain that only cancels, must not wake a pool
    that has nothing new to do."""
    manager, _refreshes = make_manager(repoints)
    queue_row("e1")  # already live, so the add below is a no-op
    other = manager._new_wake()
    manager.submit(["e1"], media_types=["Episode"])
    other.clear()

    manager._drain_ops(own=manager._new_wake())

    assert not other.is_set()
