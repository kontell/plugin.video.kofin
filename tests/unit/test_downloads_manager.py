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
    def __init__(self, item, streams):
        self._item = item
        self._streams = list(streams)
        self.stream_calls = []
        self.subtitle_payloads = {}
        self.downloaded_urls = []

    def item(self, item_id):
        if isinstance(self._item, Exception):
            raise self._item
        return self._item

    def download_stream(self, item_id, start=0):
        self.stream_calls.append(start)
        return self._streams.pop(0)

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
    assert not (tmp_path / "dl" / "Movies").exists() or not list(
        (tmp_path / "dl").rglob("*.part")
    )
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
    assert store.get("m1") is None
    assert not final.exists()
    assert not final.parent.exists()  # sidecar went with it, dir pruned
    assert (tmp_path / "dl").exists()  # never the root itself
    assert refreshes == [1]
