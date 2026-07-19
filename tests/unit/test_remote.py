import json

import pytest

from kofin.service.remote import RemoteHandler


class FakePlaylist:
    instance = None

    def __init__(self, playlist_type=0):
        if FakePlaylist.instance is not None:
            self.__dict__ = FakePlaylist.instance.__dict__
            return
        self.items = []
        self.position = 0
        FakePlaylist.instance = self

    def clear(self):
        self.items = []

    def add(self, url, listitem=None, index=-1):
        if index < 0:
            self.items.append(url)
        else:
            self.items.insert(index, url)

    def getposition(self):
        return self.position


class FakePlayer:
    played = []
    actions = []

    def play(self, item=None, listitem=None, windowed=False, startpos=-1):
        FakePlayer.played.append(item)

    def stop(self):
        FakePlayer.actions.append("stop")

    def pause(self):
        FakePlayer.actions.append("pause")

    def playnext(self):
        FakePlayer.actions.append("next")

    def seekTime(self, seconds):
        FakePlayer.actions.append(("seek", seconds))

    def isPlaying(self):
        return True


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    FakePlaylist.instance = None
    FakePlayer.played = []
    FakePlayer.actions = []
    monkeypatch.setattr("xbmc.PlayList", FakePlaylist)
    monkeypatch.setattr("xbmc.Player", FakePlayer)
    rpc = []
    monkeypatch.setattr(
        "xbmc.executeJSONRPC", lambda q: rpc.append(json.loads(q)) or "{}"
    )
    builtins = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda c: builtins.append(c))
    return {"rpc": rpc, "builtins": builtins}


def test_play_now_respects_start_index(fakes):
    RemoteHandler().handle(
        "Play",
        {"ItemIds": ["a", "b", "c"], "StartIndex": 1, "PlayCommand": "PlayNow"},
    )
    playlist = FakePlaylist.instance
    assert len(playlist.items) == 2
    assert "id=b" in playlist.items[0] and "id=c" in playlist.items[1]
    assert FakePlayer.played  # playback started


def test_play_next_inserts_after_current(fakes):
    playlist = FakePlaylist()
    playlist.items = ["one", "two"]
    playlist.position = 0
    RemoteHandler().handle("Play", {"ItemIds": ["x"], "PlayCommand": "PlayNext"})
    assert "id=x" in playlist.items[1]
    assert not FakePlayer.played


def test_playstate_commands(fakes):
    handler = RemoteHandler()
    handler.handle("Playstate", {"Command": "Stop"})
    handler.handle("Playstate", {"Command": "Pause"})
    handler.handle("Playstate", {"Command": "Seek", "SeekPositionTicks": 300_000_000})
    assert FakePlayer.actions == ["stop", "pause", ("seek", 30.0)]


def test_general_volume_and_navigation(fakes):
    handler = RemoteHandler()
    handler.handle(
        "GeneralCommand", {"Name": "SetVolume", "Arguments": {"Volume": "55"}}
    )
    assert fakes["rpc"][0]["method"] == "Application.SetVolume"
    assert fakes["rpc"][0]["params"]["volume"] == 55
    handler.handle("GeneralCommand", {"Name": "MoveUp"})
    assert "Action(up)" in fakes["builtins"]
    handler.handle("GeneralCommand", {"Name": "GoHome"})
    assert "ActivateWindow(Home)" in fakes["builtins"]


def test_unknown_messages_are_unhandled_but_safe(fakes):
    assert RemoteHandler().handle("LibraryChanged", {}) is False
    assert RemoteHandler().handle("GeneralCommand", {"Name": "Zap"}) is True


class RecordingManager:
    """Stands in for the SyncPlay manager's on_notification entry point."""

    def __init__(self):
        self.notifications = []

    def on_notification(self, message_type, data):
        self.notifications.append((message_type, data))


def test_syncplay_messages_route_to_the_manager(fakes):
    handler = RemoteHandler()
    manager = RecordingManager()
    handler.syncplay = manager

    command = {"Command": "Unpause", "GroupId": "g1"}
    update = {"Type": "PlayQueue", "GroupId": "g1"}
    assert handler.handle("SyncPlayCommand", command) is True
    assert handler.handle("SyncPlayGroupUpdate", update) is True

    # Enqueue-only, order preserved — the websocket thread never blocks.
    assert manager.notifications == [
        ("SyncPlayCommand", command),
        ("SyncPlayGroupUpdate", update),
    ]


def test_syncplay_messages_without_manager_are_claimed_and_dropped(fakes):
    handler = RemoteHandler()
    assert handler.handle("SyncPlayCommand", {"Command": "Pause"}) is True
    assert handler.handle("SyncPlayGroupUpdate", {"Type": "GroupJoined"}) is True
