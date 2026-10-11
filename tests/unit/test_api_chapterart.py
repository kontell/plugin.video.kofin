"""Chapter art through the companion: the live request publisher beside the
pass's, the stager at the play claim, and the player wiring of the API build."""

import json
import os
from types import SimpleNamespace

import pytest

from kofin.core import settings, state
from kofin.sync.backends.api import chapterart, companion
from tests.unit import contractschema
from tests.unit.apifixtures import kodi, store  # noqa: F401
from tests.unit.fakes import FakeAddon, FakeWindow

SCHEMA = contractschema.load(companion.SCHEMA_PATH)
DYNPATH = (
    "http://192.168.1.167:8096/Videos/m1/stream.mkv?static=true&mediaSourceId=m1"
    "&deviceId=device-1&playSessionId=ps1"
)


def _document():
    with open(companion.requests_path(), encoding="utf-8") as handle:
        return json.load(handle)


def _item(**values):
    item = {
        "Id": "m1",
        "Type": "Movie",
        "Path": DYNPATH,
        "PlayMethod": "DirectStream",
        "PlaySessionId": "ps1",
        "MediaSourceId": "m1",
        "DeviceId": "device-1",
    }
    item.update(values)
    return item


# -- the document, two writers ------------------------------------------------------


def test_pass_and_live_kinds_share_the_document(store, kodi, monkeypatch):
    settings.set_str("deviceId", "device-1")
    pass_request = {
        "id": "p1",
        "kind": companion.GROUP_VERSIONS,
        "library": "L",
        "item": "a",
        "owner": {"movieid": 1},
        "versions": [{"movieid": 2, "mediasourceid": "s2"}],
    }
    live_request = {
        "id": "c1",
        "kind": companion.CHAPTER_ART,
        "library": "",
        "item": "m1",
        "dynpath": DYNPATH,
        "chapters": [{"n": 1, "file": "/x/1.jpg"}],
    }
    monkeypatch.setattr(companion, "collect", lambda native: [pass_request])
    assert companion.publish(SimpleNamespace(key="ns-1")) == 1
    companion.publish_live(companion.CHAPTER_ART, [live_request])
    document = _document()
    assert document["generation"] == 2 and document["namespace"] == "ns-1"
    assert [r["id"] for r in document["requests"]] == ["p1", "c1"]
    assert document["kinds"] == ["group_versions", "chapter_art"]
    contractschema.check_document(document, SCHEMA)
    # The pass rewrites its kinds and keeps the live one; nothing changed, so
    # no new generation and no ping.
    companion.publish(SimpleNamespace(key="ns-1"))
    assert _document()["generation"] == 2 and len(kodi.notified) == 2
    # Withdrawing the live request keeps the pass's.
    companion.publish_live(companion.CHAPTER_ART, [])
    document = _document()
    assert [r["id"] for r in document["requests"]] == ["p1"]
    assert document["generation"] == 3
    assert kodi.notified[-1]["data"] == {
        "v": 2,
        "generation": 3,
        "count": 1,
        "path": companion.requests_path(),
    }


def test_a_live_request_derives_the_namespace_when_no_document_exists(store, kodi):
    settings.set_str("serverId", "server-1")
    settings.set_str("userId", "user-1")
    companion.publish_live(
        companion.CHAPTER_ART,
        [
            {
                "id": "c1",
                "kind": companion.CHAPTER_ART,
                "library": "",
                "item": "m1",
                "dynpath": DYNPATH,
                "chapters": [],
            }
        ],
    )
    document = _document()
    assert (
        document["namespace"] == companion.read()["namespace"]
        and len(document["namespace"]) == 32
    )


# -- the stager ----------------------------------------------------------------


class FakeApi:
    def __init__(self, chapters=20, images=True):
        self.calls = []
        self._chapters = [
            {
                "Name": "Chapter %d" % (i + 1),
                "ImageTag": ("tag%d" % i) if images else None,
            }
            for i in range(chapters)
        ]

    def chapters(self, item_id):
        self.calls.append(("chapters", item_id))
        return self._chapters

    def chapter_image_url(self, item_id, index, tag, max_width):
        return "http://s/Items/%s/Images/Chapter/%d?tag=%s&maxWidth=%d" % (
            item_id,
            index,
            tag,
            max_width,
        )

    def download(self, url):
        self.calls.append(("download", url))
        if "Chapter/2?" in url:  # the third chapter, 0-based on the server
            raise RuntimeError("one image failed")
        return b"\xff\xd8jpeg"


@pytest.fixture
def rig(store, kodi, monkeypatch):
    settings.set_str("deviceId", "device-1")
    monkeypatch.setattr(companion, "present", lambda: "0.1.0")
    monkeypatch.setattr(chapterart, "image_size", lambda data: (640, 360))
    monkeypatch.setattr(chapterart, "player_chapter_count", lambda: 15)
    monkeypatch.setattr(chapterart, "ACK_TIMEOUT", 0.3)
    monkeypatch.setattr(chapterart, "ACK_POLL", 0.01)
    seeded = {"count": 0}

    def count(item_id):
        # The companion seeds whatever the document names, at once.
        for request in companion.read()["requests"]:
            if request["kind"] == companion.CHAPTER_ART and request["item"] == item_id:
                return len(request["chapters"])
        return 0

    monkeypatch.setattr(chapterart, "seeded_count", count)
    lines = []
    monkeypatch.setattr(
        chapterart.LOG, "info", lambda msg, *args: lines.append(msg % args)
    )
    return SimpleNamespace(api=FakeApi(), lines=lines, seeded=seeded)


def test_the_claim_stages_the_images_publishes_and_withdraws(rig):
    worker = chapterart.ChapterArt(rig.api, _item())
    worker._publish()
    directory = worker._directory
    assert os.path.dirname(directory) == chapterart.stage_dir()
    # 15 of the server's 20 chapters are the player's; one download failed.
    staged = sorted(os.listdir(directory), key=lambda n: int(n.split(".")[0]))
    assert len(staged) == 14 and "3.jpg" not in staged and "15.jpg" in staged
    assert all(
        c[1].endswith("maxWidth=640") for c in rig.api.calls if c[0] == "download"
    )
    document = _document()
    contractschema.check_document(document, SCHEMA)
    (request,) = document["requests"]
    assert request["kind"] == companion.CHAPTER_ART and request["item"] == "m1"
    assert request["dynpath"] == DYNPATH and request["session"] == "ps1"
    assert [c["n"] for c in request["chapters"]] == [n for n in range(1, 16) if n != 3]
    assert request["chapters"][0] == {
        "n": 1,
        "file": os.path.join(directory, "1.jpg"),
        "width": 640,
        "height": 360,
    }
    assert document["device"] == "device-1"
    assert any(
        line.startswith("--> chapter art m1: 14 of 20 seeded by the companion in")
        for line in rig.lines
    )
    worker._cleanup()
    assert _document()["requests"] == []
    assert not os.path.exists(directory)


def test_a_claims_cleanup_leaves_the_next_claim_alone(rig):
    """stop() hands the withdrawal to a janitor thread and returns, and the
    player constructs the next claim straight after. The janitor used to
    clear the whole kind and remove a directory named after the item and
    the path alone: a replay lost its request and its files, and another
    item lost its request."""
    first = chapterart.ChapterArt(rig.api, _item())
    first._publish()
    replay = chapterart.ChapterArt(rig.api, _item())
    other = chapterart.ChapterArt(rig.api, dict(_item(), Id="m2"))
    assert len({first._id, replay._id, other._id}) == 3
    assert len({first._directory, replay._directory, other._directory}) == 3
    replay._publish()
    other._publish()
    assert sorted(r["id"] for r in _document()["requests"]) == sorted(
        [first._id, replay._id, other._id]
    )
    # The first claim's janitor runs only now.
    first._cleanup()
    assert sorted(r["id"] for r in _document()["requests"]) == sorted(
        [replay._id, other._id]
    )
    assert not os.path.exists(first._directory)
    for live in (replay, other):
        (request,) = [r for r in _document()["requests"] if r["id"] == live._id]
        assert all(os.path.exists(c["file"]) for c in request["chapters"])
    replay._cleanup()
    other._cleanup()
    assert _document()["requests"] == [] and os.listdir(chapterart.stage_dir()) == []


def test_a_publish_that_lost_the_race_with_stop_withdraws_itself(rig, monkeypatch):
    """stop() can land between the worker's last look at the cancel flag
    and its publish, and a janitor that gave up waiting for the worker has
    already read "nothing published". The worker checks again after the
    publish and takes its own request and files back."""
    worker = chapterart.ChapterArt(rig.api, _item())
    publish = companion.put_live
    seen = []

    def late_cancel(request):
        publish(request)
        seen.append([r["id"] for r in _document()["requests"]])
        worker._cancel.set()

    monkeypatch.setattr(companion, "put_live", late_cancel)
    worker._publish()
    assert seen == [[worker._id]]
    assert _document()["requests"] == []
    assert not os.path.exists(worker._directory)
    # The janitor's own withdrawal afterwards finds nothing left to do.
    worker._cleanup()
    assert _document()["requests"] == []


def test_staged_files_outlive_a_request_that_could_not_be_withdrawn(rig, monkeypatch):
    """A document that cannot be written still names the request, and the
    companion reads its files from the stage: neither the claim's janitor
    nor the clear at a start removes images a standing request points at."""
    from tests.unit.fakes import FakeWindow

    FakeWindow.store = {}
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    worker = chapterart.ChapterArt(rig.api, _item())
    worker._publish()

    def unwritable(*args, **kwargs):
        raise OSError("read-only file system")

    with monkeypatch.context() as patch:
        patch.setattr(companion, "drop_live", unwritable)
        patch.setattr(companion, "publish_live", unwritable)
        worker._cleanup()
        assert chapterart.clear_at_start() is False
    (request,) = _document()["requests"]
    assert all(os.path.exists(c["file"]) for c in request["chapters"])
    # Once the document can be written both go, the request first.
    assert chapterart.clear_at_start() is True
    assert _document()["requests"] == []
    assert not os.path.exists(chapterart.stage_dir())


def test_without_a_companion_nothing_is_downloaded(rig, monkeypatch):
    monkeypatch.setattr(companion, "present", lambda: None)
    worker = chapterart.ChapterArt(rig.api, _item())
    worker._publish()
    assert rig.api.calls == []
    assert not os.path.exists(companion.requests_path())
    assert rig.lines == [
        "no service.kofin.companion: the server's chapter images stay off the chapters dialog"
    ]
    worker._cleanup()


def test_a_chapterless_or_imageless_item_publishes_nothing(rig):
    worker = chapterart.ChapterArt(FakeApi(chapters=0), _item())
    worker._publish()
    worker = chapterart.ChapterArt(FakeApi(images=False), _item())
    worker._publish()
    assert not os.path.exists(companion.requests_path())


def test_an_unacknowledged_seeding_is_said_so(rig, monkeypatch):
    monkeypatch.setattr(chapterart, "seeded_count", lambda item_id: 0)
    worker = chapterart.ChapterArt(rig.api, _item())
    worker._publish()
    assert rig.lines[-1] == "--> chapter art m1: none of 14 seeded after 0 s"
    monkeypatch.setattr(chapterart, "seeded_count", lambda item_id: None)
    worker._cleanup()
    assert _document()["requests"] == []


def test_eligibility():
    assert chapterart.eligible(_item())
    assert chapterart.eligible(_item(PlayMethod="DirectPlay", Type="Episode"))
    assert not chapterart.eligible(_item(PlayMethod="Transcode"))
    assert not chapterart.eligible(_item(Type="Audio"))
    assert not chapterart.eligible(_item(Path="/home/x/Downloads/m1.mkv"))
    assert not chapterart.eligible(_item(Id=""))


def test_clear_at_start_withdraws_and_removes_the_stage(store, kodi, monkeypatch):
    from tests.unit.fakes import FakeWindow

    FakeWindow.store = {}
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    settings.set_str("deviceId", "device-1")
    companion.publish_live(
        companion.CHAPTER_ART,
        [
            {
                "id": "c1",
                "kind": companion.CHAPTER_ART,
                "library": "",
                "item": "m1",
                "dynpath": DYNPATH,
                "chapters": [],
            }
        ],
    )
    os.makedirs(os.path.join(chapterart.stage_dir(), "old"))
    # Generations overlap: a start under a playback the other generation
    # claimed leaves its request and its tiles alone.
    state.set_playing_id("m1")
    try:
        assert chapterart.clear_at_start() is False
        assert [r["id"] for r in _document()["requests"]] == ["c1"]
        assert os.path.isdir(os.path.join(chapterart.stage_dir(), "old"))
    finally:
        state.clear_playing_id()
    assert chapterart.clear_at_start() is True
    assert _document()["requests"] == []
    assert not os.path.exists(chapterart.stage_dir())


# -- the player wiring of the API build ------------------------------------------------


class SpyArt:
    instances = []

    def __init__(self, api, item):
        self.item = item
        self.started = False
        self.stopped = False
        SpyArt.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeMonitor:
    def waitForAbort(self, seconds=0):
        return False

    def abortRequested(self):
        return False


class RecordingApi:
    def session_playing(self, data):
        pass

    def session_progress(self, data):
        pass

    def session_stopped(self, data):
        pass


@pytest.fixture
def api_player(monkeypatch):
    from kofin import buildconfig

    monkeypatch.setattr(buildconfig, "BACKEND", "api")
    FakeWindow.store = {}
    FakeAddon.store = {}
    SpyArt.instances = []
    cleared = []
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmc.Monitor", FakeMonitor)
    monkeypatch.setattr(
        "xbmc.executeJSONRPC", lambda q: '{"result": {"volume": 77, "muted": false}}'
    )
    monkeypatch.setattr(chapterart, "ChapterArt", SpyArt)
    monkeypatch.setattr(chapterart, "clear_at_start", lambda: cleared.append(True))
    return cleared


def _play(monkeypatch, method="DirectStream"):
    from kofin.service.player import Player

    api = RecordingApi()
    player = Player(api)  # type: ignore[arg-type]
    monkeypatch.setattr(player, "getPlayingFile", lambda: DYNPATH)
    monkeypatch.setattr(player, "_start_ticker", lambda: None)
    state.push_play_item(
        _item(
            PlayMethod=method,
            Runtime=0,
            AudioStreamIndex=1,
            SubtitleStreamIndex=None,
            CurrentPosition=0.0,
        )
    )
    player.onPlayBackStarted()
    return player


def test_the_api_player_stages_on_claim_and_withdraws_on_finalize(
    api_player, monkeypatch
):
    player = _play(monkeypatch)
    assert api_player == [True]
    (spy,) = SpyArt.instances
    assert spy.started and spy.item["Id"] == "m1"
    player.finalize()
    assert spy.stopped


def test_the_api_player_skips_a_transcode(api_player, monkeypatch):
    _play(monkeypatch, method="Transcode")
    assert SpyArt.instances == []
