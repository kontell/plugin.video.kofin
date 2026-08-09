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
        "downloadsPath": str(tmp_path / "dl"),
        "downloadsMaxParallel": "2",
    }
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcvfs.exists", lambda p: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda p: str(p))
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
        refresh=lambda: refreshes.append(1),
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
    assert refreshes == [1]


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

    manager._run_reconcile()

    assert ("kept", str(tmp_path / "dl")) in repoints["repoint"]
    assert ("ghost", str(tmp_path / "dl")) in repoints["restore"]
    assert store.get("ghost").state == store.FAILED
    assert store.get("kept").state == store.DONE
    assert refreshes == [1]


def test_remove_restores_deletes_and_prunes(tmp_path, repoints):
    manager, refreshes = make_manager(repoints)
    rel = "Movies/The Movie (2019)/The Movie (2019).mkv"
    final = tmp_path / "dl" / rel
    final.parent.mkdir(parents=True)
    final.write_bytes(b"x")
    (final.parent / "The Movie (2019).eng.srt").write_bytes(b"s")
    queue_row()
    store.finish("m1", rel, "mkv", 1)
    store.set_restore_filename("m1", "plugin://old")

    manager._apply_remove("m1")

    assert repoints["restore"] == [("m1", str(tmp_path / "dl"))]
    assert repoints["unstamp"] == ["m1"]
    assert repoints["unbadge"] == ["m1"]
    assert store.get("m1") is None
    assert not final.exists()
    assert not final.parent.exists()  # sidecar went with it, dir pruned
    assert (tmp_path / "dl").exists()  # never the root itself
    assert refreshes == [1]


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

    assert store.get("e1").rel_path == "TV/The Show/Season 19/one.avi"
    assert store.get("e2").rel_path == "TV/The Show/Season 19/two.avi"
    season = tmp_path / "dl" / "TV/The Show/Season 19"
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

    assert store.get("x1").rel_path == "TV/The Show [show2]/Season 19/one.avi"


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
