"""Protocol behaviour tests for the SyncPlay manager, mirroring the
client requirements of SYNCPLAY.md (§2 negotiation, §5.1 command gating,
§5.3 queue idempotency, §5.4 snapshots, §6 versioning, §9 membership
lifecycle). Ported from the fork with the kofin construction signature,
dialog/settings fakes, and the kofin.sync.db id mapping; plus the
kofin-specific kicked-probe, group-flag and Hello-transport tests."""

import pytest

import kofin.sync.db as database_module
import kofin.syncplay.manager as manager_module
import kofin.syncplay.playback as playback_module
from kofin.syncplay import utils
from kofin.syncplay.manager import SyncPlayManager
from tests.unit.fakes import FakeAddon, FakeWindow, player_ops_rpc


def now_iso(delta_ms=0):
    return utils.to_iso(utils.local_ms() + delta_ms)


class FakePlayer:
    def __init__(self):
        self.paused = False
        self.playing = False
        self.position = 0.0
        self.syncplay_group_active = False
        self.item = None  # the claimed play state (current_item())

    def getTime(self):
        if not self.playing:
            raise RuntimeError("not playing")

        return self.position

    def isPlaying(self):
        return self.playing

    def isPlayingAudio(self):
        return False

    def current_item(self):
        return self.item

    def pause(self):
        self.paused = not self.paused

    def seekTime(self, seconds):
        self.position = seconds

    def stop(self):
        self.playing = False

    def play(self, *args, **kwargs):
        self.playing = True


class Recorder:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __call__(self, name, *args):
        self.calls.append((name,) + args)
        return self.results.get(name)

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


@pytest.fixture(autouse=True)
def kodi_fakes(monkeypatch):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)


# --- teardown must not wait on the server (audit R4, fixes plan H7) ----------


def test_stop_leaves_the_group_when_the_server_is_reachable(manager, monkeypatch):
    join(manager)
    monkeypatch.setattr(manager_module.state, "is_offline", lambda: False)

    manager.stop()

    assert len(manager._api_raw.named("syncplay_leave")) == 1
    assert manager.group is None


def test_stop_skips_the_leave_when_the_server_is_away(manager, monkeypatch):
    """The leave is a courtesy, and against a server that vanished without
    closing the socket it cost up to 36 s on the service main thread inside
    Service.stop — past Kodi's five-second grace. Offline, it is not sent."""
    join(manager)
    monkeypatch.setattr(manager_module.state, "is_offline", lambda: True)

    manager.stop()

    assert manager._api_raw.named("syncplay_leave") == []
    assert manager.group is None  # left locally all the same


def test_stop_survives_a_refused_leave(manager, monkeypatch):
    join(manager)
    monkeypatch.setattr(manager_module.state, "is_offline", lambda: False)

    def refuse(name, *args):
        raise manager_module.JellyfinError("gone")

    manager._api_raw = refuse

    manager.stop()

    assert manager.group is None


@pytest.fixture
def manager():
    m = SyncPlayManager(None, FakePlayer())
    # Deterministic tests: run posted work inline, silence UI, stub REST.
    m._post = lambda func, *args: func(*args)
    m._toast = lambda *a, **k: None
    m.enabled = lambda: True  # the fake settings store is empty
    m._api = Recorder()
    m._api_raw = Recorder()
    m.playback.play_item = lambda item, ticks: None
    m.playback.prepare_ready = lambda: None
    m.playback.start_loop = lambda: None

    yield m

    m.timesync = None
    m._running = False
    m._inbox.put(None)
    m._dispatcher.join(timeout=2)  # P1.11: 94 leaked dispatcher threads


def join(manager, protocol_version=2, version=1):
    info = {
        "GroupId": "g1",
        "GroupName": "movie night",
        "State": "Idle",
        "Participants": ["alice", "bob"],
    }

    if protocol_version >= 2:
        info["ProtocolVersion"] = protocol_version

    manager._handle_group_update(
        {"GroupId": "g1", "Type": "GroupJoined", "Data": info, "StateVersion": version}
    )
    # Don't let the real TimeSync thread run in unit tests.
    if manager.timesync is not None:
        manager.timesync.stop()
        manager.timesync = None


def make_queue(
    version=2,
    items=(("item-1", "pl-1"),),
    index=0,
    playing=False,
    last_update=None,
    start_ticks=0,
):
    return {
        "GroupId": "g1",
        "Type": "PlayQueue",
        "StateVersion": version,
        "Data": {
            "Reason": "NewPlaylist",
            "LastUpdate": last_update or now_iso(),
            "Playlist": [{"ItemId": item, "PlaylistItemId": pl} for item, pl in items],
            "PlayingItemIndex": index,
            "StartPositionTicks": start_ticks,
            "IsPlaying": playing,
        },
    }


class TestJoin:
    def test_join(self, manager):
        join(manager)
        assert manager.in_group()
        assert manager.group["GroupName"] == "movie night"

    def test_v2_detected(self, manager):
        join(manager, protocol_version=2)
        assert manager.protocol_version == 2

    def test_v1_absence_of_field(self, manager):
        join(manager, protocol_version=1)
        assert manager.protocol_version == 1

    def test_participants_fallback(self, manager):
        join(manager, protocol_version=1)
        assert manager.members == ["alice", "bob"]

    def test_join_drives_group_flag(self, manager):
        # The phase-3 stub: Play Next is withheld while the flag is up.
        assert manager.player.syncplay_group_active is False
        join(manager)
        assert manager.player.syncplay_group_active is True

    def test_leave_clears_group_flag(self, manager):
        join(manager)
        manager._leave_locally()
        assert manager.player.syncplay_group_active is False
        assert not manager.in_group()


class TestCommandGating:
    def test_stale_version_discarded(self, manager):
        join(manager, version=5)
        scheduled = []
        manager.playback.schedule = scheduled.append

        manager._handle_command(
            {
                "Command": "Unpause",
                "When": now_iso(500),
                "EmittedAt": now_iso(),
                "PositionTicks": 0,
                "PlaylistItemId": None,
                "StateVersion": 4,
            }
        )
        assert scheduled == []

    def test_pre_join_command_discarded(self, manager):
        join(manager)
        scheduled = []
        manager.playback.schedule = scheduled.append

        manager._handle_command(
            {
                "Command": "Unpause",
                "When": now_iso(500),
                "EmittedAt": now_iso(-30000),  # 30s before join
                "PositionTicks": 0,
                "PlaylistItemId": None,
            }
        )
        assert scheduled == []

    def test_item_mismatch_discarded(self, manager):
        join(manager)
        manager.current_playlist_item_id = "pl-1"
        scheduled = []
        manager.playback.schedule = scheduled.append

        manager._handle_command(
            {
                "Command": "Seek",
                "When": now_iso(500),
                "EmittedAt": now_iso(),
                "PositionTicks": 0,
                "PlaylistItemId": "pl-OTHER",
            }
        )
        assert scheduled == []
        # §6: a command for an item we don't have means a missed queue
        # update — a v2 member reconciles via a snapshot.
        assert manager._api.named("syncplay_snapshot")

    def test_stop_bypasses_item_check(self, manager):
        join(manager)
        manager.current_playlist_item_id = "pl-1"
        scheduled = []
        manager.playback.schedule = scheduled.append

        manager._handle_command(
            {
                "Command": "Stop",
                "When": now_iso(100),
                "EmittedAt": now_iso(),
                "PositionTicks": 0,
                "PlaylistItemId": "pl-OTHER",
            }
        )
        assert len(scheduled) == 1

    def test_valid_command_scheduled(self, manager):
        join(manager)
        manager.current_playlist_item_id = "pl-1"
        scheduled = []
        manager.playback.schedule = scheduled.append

        manager._handle_command(
            {
                "Command": "Pause",
                "When": now_iso(200),
                "EmittedAt": now_iso(),
                "PositionTicks": 1000,
                "PlaylistItemId": "pl-1",
            }
        )
        assert len(scheduled) == 1


class TestPlayQueue:
    def test_queue_starts_item(self, manager):
        join(manager)
        started = []
        manager._start_item = lambda i, p: started.append((i, p))

        manager._handle_group_update(make_queue())
        assert started == [("item-1", "pl-1")]
        assert manager.queue == [("item-1", "pl-1")]

    def test_stale_last_update_ignored(self, manager):
        join(manager)
        started = []
        manager._start_item = lambda i, p: started.append((i, p))

        first = make_queue(last_update=now_iso())
        manager._handle_group_update(first)
        # Identical LastUpdate (e.g. a redelivered update)
        second = make_queue(
            items=(("item-2", "pl-2"),),
            last_update=first["Data"]["LastUpdate"],
        )
        manager._handle_group_update(second)

        assert started == [("item-1", "pl-1")]

    def test_tail_only_change_does_not_restart(self, manager):
        join(manager)
        started = []

        def fake_start(item_id, playlist_item_id):
            started.append((item_id, playlist_item_id))
            manager.current_item_id = item_id
            manager.current_playlist_item_id = playlist_item_id

        manager._start_item = fake_start

        manager._handle_group_update(make_queue())
        manager.phase = "synced"  # simulate having started
        manager._handle_group_update(
            make_queue(
                items=(("item-1", "pl-1"), ("item-9", "pl-9")),
                last_update=now_iso(1000),
            )
        )
        assert started == [("item-1", "pl-1")]

    def test_empty_queue_detaches(self, manager):
        join(manager)
        manager._start_item = lambda i, p: None
        manager._handle_group_update(make_queue())
        manager.phase = "synced"

        manager._handle_group_update(
            make_queue(items=(), index=-1, last_update=now_iso(1000))
        )
        assert manager.phase == "idle"
        assert manager.current_playlist_item_id is None


class TestPlayQueueVersioning:
    def test_stale_version_ignored(self, manager):
        join(manager, version=5)
        started = []
        manager._start_item = lambda i, p: started.append((i, p))

        manager._handle_group_update(make_queue(version=3))
        assert started == []


class TestSnapshot:
    def snapshot(self, version=4, playing=False, state="Paused", when=None):
        return {
            "GroupId": "g1",
            "Type": "StateSnapshot",
            "StateVersion": version,
            "Data": {
                "GroupName": "movie night",
                "State": state,
                "PlayQueue": make_queue()["Data"],
                "PositionTicks": 50000000,
                "When": when or now_iso(),
                "IsPlaying": playing,
                "Members": [{"UserName": "alice"}],
            },
        }

    def test_snapshot_applies_queue_and_state(self, manager):
        join(manager)
        started = []
        manager._start_item = lambda i, p: started.append((i, p))

        manager._handle_group_update(self.snapshot())

        # The queue triggered a load; the synthetic command is suppressed
        # while loading (the ready flow converges instead).
        assert started == [("item-1", "pl-1")]
        assert manager.group_state == "Paused"
        assert manager.last_snapshot_at > 0

    def test_snapshot_synthetic_command_when_not_loading(self, manager):
        join(manager)
        manager._start_item = lambda i, p: None
        scheduled = []
        manager.playback.schedule = scheduled.append

        manager._handle_group_update(self.snapshot(playing=True, state="Playing"))
        # Simulate: the item is already loaded and synced.
        manager.phase = "synced"
        manager.current_playlist_item_id = "pl-1"

        snap = self.snapshot(version=5, playing=True, state="Playing")
        snap["Data"]["PlayQueue"]["LastUpdate"] = now_iso(2000)
        manager._handle_group_update(snap)

        assert scheduled
        assert scheduled[-1]["Command"] == "Unpause"

    def test_snapshot_idempotent(self, manager):
        join(manager)
        started = []
        manager._start_item = lambda i, p: started.append((i, p))

        snap = self.snapshot()
        manager._handle_group_update(snap)
        manager.phase = "synced"
        manager._handle_group_update(snap)  # replay

        assert started == [("item-1", "pl-1")]


class TestBeacon:
    def beacon(self, version, item="pl-1", position_ticks=10000000, when=None):
        return {
            "GroupId": "g1",
            "Type": "PositionBeacon",
            "StateVersion": version,
            "Data": {
                "PlaylistItemId": item,
                "PositionTicks": position_ticks,
                "When": when or now_iso(),
            },
        }

    def test_beacon_updates_reference(self, manager):
        join(manager, version=3)
        manager.current_playlist_item_id = "pl-1"

        manager._handle_group_update(self.beacon(version=3))
        estimate = manager.playback.estimate_position_ms()
        assert estimate is not None
        assert abs(estimate - 1000.0) < 200

    def test_beacon_version_gap_requests_snapshot(self, manager):
        join(manager, version=3)
        manager.current_playlist_item_id = "pl-1"

        manager._handle_group_update(self.beacon(version=9))
        assert manager._api.named("syncplay_snapshot")

    def test_beacon_for_other_item_ignored(self, manager):
        join(manager, version=3)
        manager.current_playlist_item_id = "pl-1"

        manager._handle_group_update(self.beacon(version=3, item="pl-OTHER"))
        assert manager.playback.estimate_position_ms() is None

    def test_snapshot_request_rate_limited(self, manager):
        join(manager, version=3)
        manager.current_playlist_item_id = "pl-1"

        manager._handle_group_update(self.beacon(version=9))
        manager._handle_group_update(self.beacon(version=10))
        assert len(manager._api.named("syncplay_snapshot")) == 1


class TestHello:
    """The plugin-binding capability probe (Hello): learns the dedicated
    time-sync socket; its absence (stock and integrated servers) changes
    nothing."""

    def test_hello_learns_timesync_transport(self, manager):
        manager._api_raw.results["syncplay_hello"] = {
            "ProtocolVersion": 2,
            "TimeSync": {"WebSocketPath": "/SyncPlay/TimeSync"},
        }
        join(manager)
        assert manager.timesync_ws_path == "/SyncPlay/TimeSync"
        assert manager.can_ws_timesync()

    def test_hello_not_probed_on_v1(self, manager):
        join(manager, protocol_version=1)
        assert manager._api_raw.named("syncplay_hello") == []
        assert not manager.can_ws_timesync()

    def test_hello_absent_leaves_http_timesync(self, manager):
        from kofin.core.http import JellyfinError

        recorder = manager._api_raw

        def api_raw(name, *args):
            if name == "syncplay_hello":
                raise JellyfinError("404")
            return recorder(name, *args)

        manager._api_raw = api_raw
        join(manager)
        assert manager.timesync_ws_path is None
        assert not manager.can_ws_timesync()


class TestLifecycle:
    def test_group_left_cleans_up(self, manager):
        join(manager)
        manager._handle_group_update(
            {"GroupId": "g1", "Type": "GroupLeft", "Data": "g1"}
        )
        assert not manager.in_group()
        assert manager.state_version == 0
        assert manager.protocol_version == 1

    def test_join_resets_version_tracking(self, manager):
        join(manager, version=9)
        assert manager.state_version == 9
        manager._handle_group_update(
            {"GroupId": "g1", "Type": "GroupLeft", "Data": "g1"}
        )
        join(manager, version=1)
        assert manager.state_version == 1

    def test_not_in_group_triggers_rejoin(self, manager):
        join(manager)
        manager._handle_group_update(
            {"GroupId": "g1", "Type": "NotInGroup", "Data": "g1"}
        )
        assert manager._api_raw.named("syncplay_join")

    def test_rejoin_rate_limited(self, manager):
        join(manager)
        manager._handle_group_update(
            {"GroupId": "g1", "Type": "NotInGroup", "Data": "g1"}
        )
        manager._handle_group_update(
            {"GroupId": "g1", "Type": "NotInGroup", "Data": "g1"}
        )
        assert len(manager._api_raw.named("syncplay_join")) == 1

    def test_updates_for_other_groups_ignored(self, manager):
        join(manager)
        started = []
        manager._start_item = lambda i, p: started.append((i, p))

        other = make_queue()
        other["GroupId"] = "g2"
        manager._handle_group_update(other)
        assert started == []


class TestKickedProbe:
    """Reconnect contract on v1 (report R2): after a WS drop assume kicked —
    probe GET /SyncPlay/List, rejoin if the group survives, detach with a
    toast if it is gone, and hold if the list is unavailable."""

    def test_reconnect_probes_and_rejoins(self, manager):
        join(manager, protocol_version=1)
        manager._api_raw.results["syncplay_list"] = [
            {"GroupId": "g1", "GroupName": "movie night"}
        ]

        manager._on_ws_connected()

        assert manager._api_raw.named("syncplay_list")
        assert manager._api_raw.named("syncplay_join")
        assert manager.in_group()

    def test_reconnect_group_gone_detaches(self, manager):
        join(manager, protocol_version=1)
        toasts = []
        manager._toast = lambda message, **kwargs: toasts.append(message)
        manager._api_raw.results["syncplay_list"] = [{"GroupId": "OTHER"}]

        manager._on_ws_connected()

        assert manager._api_raw.named("syncplay_join") == []
        assert not manager.in_group()
        assert len(toasts) == 1

    def test_reconnect_list_unavailable_keeps_group(self, manager):
        join(manager, protocol_version=1)
        manager.list_groups = lambda: None  # server not reachable yet

        manager._on_ws_connected()

        assert manager.in_group()
        assert manager._api_raw.named("syncplay_join") == []

    def test_reconnect_outside_group_is_noop(self, manager):
        manager._on_ws_connected()
        assert manager._api_raw.calls == []

    def test_wake_probes_group(self, manager):
        join(manager, protocol_version=1)
        manager._api_raw.results["syncplay_list"] = [{"GroupId": "g1"}]

        manager.on_wake()

        assert manager._api_raw.named("syncplay_list")
        assert manager._api_raw.named("syncplay_join")

    def test_wake_outside_group_is_noop(self, manager):
        manager.on_wake()
        assert manager._api_raw.calls == []


class TestV2Reconnect:
    """Reconnect contract on v2 (§9): the server re-attaches the member and
    pushes a StateSnapshot; the client verifies and pulls one when it never
    arrives, instead of probing and re-joining."""

    class _ImmediateTimer:
        def __init__(self, interval, func, args=()):
            self._func = func
            self._args = args
            self.daemon = True

        def start(self):
            self._func(*self._args)

    def test_reconnect_requests_snapshot_when_none_pushed(self, manager, monkeypatch):
        join(manager)
        monkeypatch.setattr(manager_module.threading, "Timer", self._ImmediateTimer)

        manager._on_ws_connected()

        # No kicked-probe, no rejoin; a snapshot pull instead.
        assert manager._api_raw.named("syncplay_list") == []
        assert manager._api_raw.named("syncplay_join") == []
        assert manager._api.named("syncplay_snapshot")

    def test_reconnect_trusts_a_pushed_snapshot(self, manager, monkeypatch):
        join(manager)
        monkeypatch.setattr(manager_module.threading, "Timer", self._ImmediateTimer)
        manager.last_snapshot_at = manager_module.time.time() + 1

        manager._on_ws_connected()

        assert manager._api.named("syncplay_snapshot") == []

    def test_wake_requests_snapshot(self, manager):
        join(manager)

        manager.on_wake()

        assert manager._api.named("syncplay_snapshot")
        assert manager._api_raw.named("syncplay_list") == []

    def test_resync_menu_uses_snapshot(self, manager):
        join(manager)

        manager.request_resync()

        assert manager._api.named("syncplay_snapshot")
        assert manager._api_raw.named("syncplay_join") == []


class TestGroupWaitToast:
    def test_playing_to_waiting_toasts(self, manager):
        join(manager)
        toasts = []
        manager._toast = lambda message, **kwargs: toasts.append(message)
        manager.group_state = "Playing"

        manager._handle_group_update(
            {
                "GroupId": "g1",
                "Type": "StateUpdate",
                "Data": {"State": "Waiting", "Reason": "SetIgnoreWait"},
            }
        )

        assert manager.group_state == "Waiting"
        assert len(toasts) == 1

    def test_other_transitions_stay_quiet(self, manager):
        join(manager)
        toasts = []
        manager._toast = lambda message, **kwargs: toasts.append(message)

        for previous, new in (("Idle", "Waiting"), ("Playing", "Paused")):
            manager.group_state = previous
            manager._handle_group_update(
                {"GroupId": "g1", "Type": "StateUpdate", "Data": {"State": new}}
            )

        assert toasts == []


class TestAdoptInProgress:
    """A queue update naming the item already on screen must adopt it, not
    tear it down and reload (regression: SyncPlay reloaded the in-progress
    movie when a group was created before playback)."""

    def test_adopts_currently_playing_item(self, manager):
        join(manager)
        started = []
        manager._start_item = lambda i, p: started.append((i, p))
        prepared = []
        manager.playback.prepare_ready = lambda: prepared.append(True)
        manager.playback.ensure_paused = lambda: None

        # Already playing item-1 (e.g. we just proposed it via SetNewQueue);
        # the echo assigns a fresh PlaylistItemId. Phase is idle.
        manager.player.playing = True
        manager._local_item_id = lambda: "item-1"
        manager.phase = "idle"

        manager._handle_group_update(make_queue(items=(("item-1", "pl-new"),)))

        assert started == []  # never reloaded
        assert manager.phase == "waiting_ready"
        assert manager.current_playlist_item_id == "pl-new"
        assert prepared == [True]

    def test_reloads_when_not_already_playing(self, manager):
        join(manager)
        started = []
        manager._start_item = lambda i, p: started.append((i, p))
        manager.player.playing = False
        manager._local_item_id = lambda: None

        manager._handle_group_update(make_queue(items=(("item-9", "pl-9"),)))
        assert started == [("item-9", "pl-9")]


class TestForwardLocalPlay:
    def test_noop_when_nothing_playing(self, manager):
        # Creating a group before playback must not demote to spectator.
        join(manager)
        manager.player.playing = False

        manager._forward_local_play()

        assert manager._api.named("syncplay_set_new_queue") == []
        assert manager._api.named("syncplay_set_ignore_wait") == []
        assert manager.ignore_wait is False

    def test_proposes_when_playing(self, manager):
        join(manager)
        manager.player.playing = True
        manager.player.position = 42.0
        manager._local_item_id = lambda: "item-1"

        manager._forward_local_play()

        calls = manager._api.named("syncplay_set_new_queue")
        assert len(calls) == 1
        assert calls[0][1] == ["item-1"]

    def test_claimed_item_is_the_identity_source(self, manager):
        # The service player's claimed play state names the jellyfin id.
        join(manager)
        manager.player.playing = True
        manager.player.position = 10.0
        manager.player.item = {"Id": "item-7", "PlayMethod": "DirectStream"}

        manager._forward_local_play()

        calls = manager._api.named("syncplay_set_new_queue")
        assert len(calls) == 1
        assert calls[0][1] == ["item-7"]


@pytest.fixture
def paused_cond(manager, monkeypatch):
    """Wire the Player.Paused condition to the fake player so the
    playback controller's pause toggling behaves like the real thing."""

    def cond(condition):
        if condition == "Player.Paused":
            return manager.player.paused

        return False

    monkeypatch.setattr(playback_module.xbmc, "getCondVisibility", cond)
    monkeypatch.setattr("xbmc.executeJSONRPC", player_ops_rpc(lambda: manager.player))


class TestLocalStartHold:
    """A user-initiated start that has to wait for the group is paused the
    instant it begins ("held"), proposed, and released by the group's
    Unpause — instead of playing for seconds until the round trip lands."""

    def test_transition_start_pauses_immediately(self, manager, paused_cond):
        join(manager)
        manager.player.playing = True
        manager.phase = "synced"  # a native playlist advance

        manager.on_playback_started()

        assert manager.player.paused is True
        assert manager._hold is not None
        assert manager._hold["transition"] is True
        assert manager._hold["proposed"] is False

    def test_cold_start_holds(self, manager, paused_cond):
        join(manager)
        manager.player.playing = True
        manager.phase = "idle"

        manager.on_playback_started()

        assert manager.player.paused is True
        assert manager._hold is not None
        assert manager._hold["transition"] is False

    def test_loading_start_pauses_without_hold(self, manager, paused_cond):
        join(manager)
        manager.player.playing = True
        manager.phase = "loading"  # our own play_item()

        manager.on_playback_started()

        assert manager.player.paused is True
        assert manager._hold is None

    def test_programmatic_start_not_held(self, manager, paused_cond):
        join(manager)
        manager.player.playing = True
        manager.phase = "idle"

        with manager.programmatic():
            manager.on_playback_started()

        assert manager._hold is None
        assert manager.player.paused is False

    def test_not_in_group_ignored(self, manager, paused_cond):
        manager.player.playing = True

        manager.on_playback_started()

        assert manager._hold is None
        assert manager.player.paused is False

    def test_identify_transition_proposes_at_zero(
        self, manager, paused_cond, monkeypatch
    ):
        join(manager)
        manager.player.playing = True
        # Right after a gapless advance the clock may still read the
        # previous track; the proposal must pin everyone to 0 anyway.
        manager.player.position = 18.9
        manager.phase = "synced"
        manager.on_playback_started()

        monkeypatch.setattr(database_module, "get_item", lambda kid, media: ("jf-1",))
        manager.on_kodi_play({"item": {"id": 42, "type": "song"}})

        calls = manager._api.named("syncplay_set_new_queue")
        assert calls == [("syncplay_set_new_queue", ["jf-1"], 0, 0)]
        assert manager._hold["proposed"] is True
        assert manager._hold["item_id"] == "jf-1"
        assert manager.player.paused is True  # still held

    def test_identify_defers_cold_start(self, manager, paused_cond, monkeypatch):
        join(manager)
        manager.player.playing = True
        manager.phase = "idle"
        manager.on_playback_started()

        monkeypatch.setattr(database_module, "get_item", lambda kid, media: ("jf-1",))
        manager.on_kodi_play({"item": {"id": 42, "type": "movie"}})

        # A fresh start settles on its position (resume point) first; the
        # proposal happens from onAVStarted with the live clock.
        assert manager._api.named("syncplay_set_new_queue") == []
        assert manager._hold["proposed"] is False

    def test_identify_without_hold_is_noop(self, manager, paused_cond, monkeypatch):
        join(manager)
        manager.player.playing = True

        monkeypatch.setattr(database_module, "get_item", lambda kid, media: ("jf-1",))
        manager.on_kodi_play({"item": {"id": 42, "type": "song"}})

        assert manager._api.named("syncplay_set_new_queue") == []

    def test_avstarted_completes_cold_hold(self, manager, paused_cond):
        join(manager)
        manager.player.playing = True
        manager.player.position = 42.0
        manager.phase = "idle"
        manager.on_playback_started()  # enters the programmatic grace
        manager._local_item_id = lambda: "item-1"

        manager.on_avstarted()

        # Our own hold pause must not swallow the forward via the grace.
        calls = manager._api.named("syncplay_set_new_queue")
        assert calls == [
            ("syncplay_set_new_queue", ["item-1"], 0, utils.seconds_to_ticks(42.0))
        ]
        assert manager._hold["proposed"] is True
        assert manager.player.paused is True

    def test_avstarted_does_not_propose_twice(self, manager, paused_cond):
        join(manager)
        manager.player.playing = True
        manager.phase = "synced"
        manager.on_playback_started()
        manager._hold["proposed"] = True
        manager._hold["item_id"] = "item-1"

        manager.on_avstarted()

        assert manager._api.named("syncplay_set_new_queue") == []

    def test_transition_forward_distrusts_window_id(self, manager, paused_cond):
        join(manager)
        manager.player.playing = True
        manager.phase = "synced"
        manager.on_playback_started()
        # The play pipeline has not claimed the new track yet; the window
        # property still names the previous one.
        manager._local_file_info = lambda: None
        manager._local_item_id = lambda: "stale-previous-track"

        manager._forward_local_play()

        assert manager._api.named("syncplay_set_new_queue") == []
        assert manager._hold["proposed"] is False
        manager.player.playing = False  # quiesce the pending retry

    def test_forward_giveup_releases_hold_and_demotes(self, manager, paused_cond):
        join(manager)
        manager.player.playing = True
        manager.phase = "synced"
        manager.on_playback_started()
        manager._local_file_info = lambda: None

        manager._forward_local_play(attempt=utils.FORWARD_RETRY_LIMIT)

        assert manager.ignore_wait is True
        assert manager._api.named("syncplay_set_ignore_wait")
        assert manager._hold is None
        assert manager.player.paused is False  # playback given back

    def test_adopt_matches_held_proposal(self, manager, paused_cond):
        join(manager)
        started = []
        manager._start_item = lambda i, p: started.append((i, p))
        prepared = []
        manager.playback.prepare_ready = lambda: prepared.append(True)

        manager.player.playing = True
        manager.phase = "synced"
        manager.on_playback_started()
        manager._hold["proposed"] = True
        manager._hold["item_id"] = "item-1"
        # The play pipeline is still resolving the new track.
        manager._local_item_id = lambda: None

        manager._handle_group_update(make_queue(items=(("item-1", "pl-new"),)))

        assert started == []  # adopted, never reloaded
        assert manager.phase == "waiting_ready"
        assert manager.current_playlist_item_id == "pl-new"
        assert manager._hold is None
        assert prepared == [True]

    def test_forward_skips_already_adopted_item(self, manager, paused_cond):
        # A late-delivered onAVStarted for a proposal whose echo was
        # already adopted (and unpaused) must not re-propose the item.
        join(manager)
        manager.player.playing = True
        manager.phase = "synced"
        manager.current_item_id = "item-1"
        manager._local_item_id = lambda: "item-1"

        manager.on_avstarted()

        assert manager._api.named("syncplay_set_new_queue") == []

    def test_stop_clears_hold(self, manager, paused_cond):
        join(manager)
        manager.player.playing = True
        manager.phase = "idle"
        manager.on_playback_started()

        manager.on_stopped()

        assert manager._hold is None

    def test_release_hold_resumes(self, manager, paused_cond):
        join(manager)
        manager.player.playing = True
        manager.phase = "idle"
        manager.on_playback_started()
        assert manager.player.paused is True

        manager._release_hold()

        assert manager._hold is None
        assert manager.player.paused is False

    def test_release_without_hold_leaves_player_alone(self, manager, paused_cond):
        join(manager)
        manager.player.playing = True
        manager.player.paused = True  # paused by the user, not by a hold

        manager._release_hold()

        assert manager.player.paused is True


class TestTransitionSequence:
    """The full playlist-advance timeline as seen in the fork's field logs:
    boundary -> hold -> fast identify -> late onAVStarted -> queue echo
    adopt -> group Unpause release."""

    def test_boundary_holds_then_group_start_releases(
        self, manager, paused_cond, monkeypatch
    ):
        join(manager)
        prepared = []
        manager.playback.prepare_ready = lambda: prepared.append(True)

        # Track n is synced and playing.
        manager.player.playing = True
        manager.player.position = 18.9
        manager.phase = "synced"
        manager.current_item_id = "track-n"
        manager.current_playlist_item_id = "pl-n"

        # 1. Gapless advance: the boundary pauses the player immediately.
        manager.on_playback_started()
        assert manager.player.paused is True

        # 2. Player.OnPlay identifies the new track within milliseconds.
        monkeypatch.setattr(
            database_module, "get_item", lambda kid, media: ("track-n1",)
        )
        manager.on_kodi_play({"item": {"id": 43, "type": "song"}})
        calls = manager._api.named("syncplay_set_new_queue")
        assert calls == [("syncplay_set_new_queue", ["track-n1"], 0, 0)]
        # Position is left alone here; the adopt's prepare_ready (stubbed
        # in this test) and the Unpause's own alignment handle it.

        # 3. onAVStarted arrives late (it queues behind the play pipeline)
        # and must not propose again.
        manager.on_avstarted()
        assert len(manager._api.named("syncplay_set_new_queue")) == 1

        # 4. The queue echo adopts the held item without reloading.
        manager._handle_group_update(make_queue(items=(("track-n1", "pl-n1"),)))
        assert manager.phase == "waiting_ready"
        assert manager._hold is None
        assert manager.player.paused is True  # still held for the group
        assert prepared == [True]

        # 5. The group Unpause releases everyone together.
        manager._handle_command(
            {
                "GroupId": "g1",
                "Command": "Unpause",
                "When": now_iso(-1),  # due now: executes inline
                "EmittedAt": now_iso(),
                "PositionTicks": 0,
                "PlaylistItemId": "pl-n1",
            }
        )
        assert manager.player.paused is False
        assert manager.phase == "synced"
        assert manager.player.position < 1.0  # aligned on the group start


class TestSpectatorLocalPlayback:
    """A spectator's own plays stay local: no hold, no forwarding, no
    repeated demotions/toasts, and the group must not tear their
    playback down (regression: playing a non-jellyfin video as a
    spectator re-toasted 'spectator mode' on every start)."""

    def test_spectator_local_play_not_held(self, manager, paused_cond):
        join(manager)
        manager.ignore_wait = True
        manager.player.playing = True
        manager.phase = "idle"

        manager.on_playback_started()

        assert manager._hold is None
        assert manager.player.paused is False

    def test_spectator_avstarted_not_forwarded(self, manager, paused_cond):
        join(manager)
        manager.ignore_wait = True
        manager.player.playing = True
        manager.phase = "idle"
        manager._local_item_id = lambda: "item-1"

        manager.on_avstarted()

        assert manager._api.named("syncplay_set_new_queue") == []

    def test_unmanaged_library_item_releases_hold_quickly(
        self, manager, paused_cond, monkeypatch
    ):
        # A Kodi library item with no jellyfin mapping is identified as
        # unmanaged from Player.OnPlay, not after the retry window.
        join(manager)
        toasts = []
        manager._toast = lambda message, **kwargs: toasts.append(message)
        manager.player.playing = True
        manager.phase = "idle"
        manager.on_playback_started()
        assert manager.player.paused is True

        monkeypatch.setattr(database_module, "get_item", lambda kid, media: None)
        manager.on_kodi_play({"item": {"id": 99, "type": "movie"}})

        assert manager._hold is None
        assert manager.player.paused is False  # playback given back
        assert manager.ignore_wait is True
        assert manager._api.named("syncplay_set_ignore_wait")
        assert len(toasts) == 1

    def test_unmanaged_play_is_silent_when_already_spectator(
        self, manager, paused_cond
    ):
        join(manager)
        toasts = []
        manager._toast = lambda message, **kwargs: toasts.append(message)
        manager.ignore_wait = True
        manager.player.playing = True
        # A hold that slipped through (e.g. spectator toggled mid-hold).
        manager._hold = {"transition": True, "proposed": False, "item_id": None}
        manager._local_file_info = lambda: None

        manager._forward_local_play(attempt=utils.FORWARD_RETRY_LIMIT)

        assert manager._hold is None
        assert manager._api.named("syncplay_set_ignore_wait") == []
        assert toasts == []

    def test_queue_not_followed_over_spectators_own_media(self, manager):
        join(manager)
        started = []
        manager._start_item = lambda i, p: started.append((i, p))
        manager.ignore_wait = True
        manager.player.playing = True
        manager._local_item_id = lambda: None  # unmanaged media

        manager._handle_group_update(make_queue(items=(("item-9", "pl-9"),)))

        assert started == []
        assert manager.phase == "idle"

    def test_queue_followed_when_spectator_is_idle(self, manager):
        join(manager)
        started = []
        manager._start_item = lambda i, p: started.append((i, p))
        manager.ignore_wait = True
        manager.player.playing = False

        manager._handle_group_update(make_queue(items=(("item-9", "pl-9"),)))

        assert started == [("item-9", "pl-9")]

    def test_leaving_spectator_mode_reattaches(self, manager):
        join(manager)
        manager.ignore_wait = True

        manager.toggle_spectator()

        assert manager.ignore_wait is False
        assert manager._api_raw.named("syncplay_join")  # forced rejoin

    def test_becoming_spectator_does_not_rejoin(self, manager):
        join(manager)

        manager.toggle_spectator()

        assert manager.ignore_wait is True
        assert manager._api_raw.named("syncplay_join") == []


class FakeDialog:
    """xbmcgui.Dialog stand-in for the stopped prompt."""

    selection = -1
    asked = []
    on_select = None

    def select(self, heading, options, *args, **kwargs):
        FakeDialog.asked.append((heading, tuple(options)))
        if FakeDialog.on_select is not None:
            return FakeDialog.on_select()
        return FakeDialog.selection

    def notification(self, *args, **kwargs):
        pass

    def ok(self, *args, **kwargs):
        pass


class TestStoppedPrompt:
    """A local stop while synced offers: stop the whole group (and stay,
    so the next play is proposed to everyone), become a spectator, or
    leave. A replace-play supersedes the prompt entirely."""

    @pytest.fixture(autouse=True)
    def _no_grace_wait(self, monkeypatch):
        monkeypatch.setattr(utils, "STOP_PROMPT_GRACE", 0.0)
        monkeypatch.setattr(manager_module.time, "sleep", lambda seconds: None)

    def _prompt(self, manager, monkeypatch, selection, on_select=None):
        FakeDialog.selection = selection
        FakeDialog.asked = []
        FakeDialog.on_select = on_select
        monkeypatch.setattr(manager_module.xbmcgui, "Dialog", FakeDialog)
        manager._user_stopped_prompt()
        return FakeDialog.asked

    def test_stop_for_everyone_keeps_membership(self, manager, monkeypatch):
        join(manager)

        asked = self._prompt(manager, monkeypatch, 0)

        assert len(asked) == 1
        assert manager._api.named("syncplay_stop")
        assert manager.in_group()
        assert manager.ignore_wait is False

    def test_spectator_choice(self, manager, monkeypatch):
        join(manager)

        self._prompt(manager, monkeypatch, 1)

        assert manager.ignore_wait is True
        assert manager._api.named("syncplay_set_ignore_wait")
        assert manager.in_group()

    def test_leave_choice(self, manager, monkeypatch):
        join(manager)

        self._prompt(manager, monkeypatch, 2)

        assert not manager.in_group()
        assert manager._api_raw.named("syncplay_leave")

    def test_dismiss_defaults_to_spectator(self, manager, monkeypatch):
        # Doing nothing would leave the group waiting on this member.
        join(manager)

        self._prompt(manager, monkeypatch, -1)

        assert manager.ignore_wait is True
        assert manager.in_group()

    def test_replace_play_suppresses_prompt(self, manager, monkeypatch):
        # The user picked a new item: its start is already held/proposed.
        join(manager)
        manager._hold = {"transition": False, "proposed": False, "item_id": None}

        asked = self._prompt(manager, monkeypatch, 0)

        assert asked == []
        assert manager._api.named("syncplay_stop") == []

    def test_group_moved_on_suppresses_prompt(self, manager, monkeypatch):
        # Another member started something; we are already loading it.
        join(manager)
        manager.phase = "loading"

        asked = self._prompt(manager, monkeypatch, 0)

        assert asked == []

    def test_stale_group_stop_answer_ignored(self, manager, monkeypatch):
        # A new item started while the dialog was open: stopping the
        # group now would kill it for everyone.
        join(manager)

        def answer():
            manager._hold = {"transition": False, "proposed": True, "item_id": "i"}
            return 0

        self._prompt(manager, monkeypatch, 0, on_select=answer)

        assert manager._api.named("syncplay_stop") == []
        assert manager.in_group()

    def test_leave_honoured_even_when_superseded(self, manager, monkeypatch):
        join(manager)

        def answer():
            manager._hold = {"transition": False, "proposed": True, "item_id": "i"}
            return 2

        self._prompt(manager, monkeypatch, 2, on_select=answer)

        assert not manager.in_group()


class TestCommandGroupGate:
    def test_command_for_another_group_discarded(self, manager):
        join(manager)
        manager.current_playlist_item_id = "pl-1"
        scheduled = []
        manager.playback.schedule = scheduled.append

        manager._handle_command(
            {
                "GroupId": "g2",  # we are in g1
                "Command": "Unpause",
                "When": now_iso(200),
                "EmittedAt": now_iso(),
                "PositionTicks": 0,
                "PlaylistItemId": "pl-1",
            }
        )
        assert scheduled == []

    def test_command_for_our_group_scheduled(self, manager):
        join(manager)
        manager.current_playlist_item_id = "pl-1"
        scheduled = []
        manager.playback.schedule = scheduled.append

        manager._handle_command(
            {
                "GroupId": "g1",
                "Command": "Unpause",
                "When": now_iso(200),
                "EmittedAt": now_iso(),
                "PositionTicks": 0,
                "PlaylistItemId": "pl-1",
            }
        )
        assert len(scheduled) == 1


class TestLoadAllowance:
    """Aim a load ahead of a playing group by however long loading takes.

    A load is not instant and a playing group does not wait for it, so a load
    aimed at the position the group is at *now* starts wherever the group has
    reached by the first frame. On a transcode that was ~9s, which is what made
    a member look permanently adrift after a group Seek when it had in fact
    loaded exactly where it was told.
    """

    @staticmethod
    def _playing_at(manager, media_ms):
        # A reference the group is moving from, which is what makes aiming
        # ahead meaningful at all.
        manager.playback.set_reference(
            utils.ms_to_ticks(media_ms), manager.server_now_ms(), True
        )

    @staticmethod
    def _load(manager, elapsed_ms, clock):
        """One timed load: start it, let `elapsed_ms` pass, finish it."""
        manager._load_started_ms = clock[0]
        clock[0] += elapsed_ms
        manager._note_load_completed()

    def test_first_load_has_no_allowance(self, manager):
        self._playing_at(manager, 60000)
        # Nothing has been timed yet, so there is nothing to aim off by, and
        # guessing would be worse than not.
        assert manager._load_allowance_ms() == 0.0

    def test_a_timed_load_becomes_the_next_load_s_allowance(self, manager, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(utils, "local_ms", lambda: clock[0])
        self._playing_at(manager, 60000)

        self._load(manager, 9000.0, clock)

        assert manager._load_allowance_ms() == pytest.approx(9000.0)

    def test_a_paused_group_gets_no_allowance(self, manager, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(utils, "local_ms", lambda: clock[0])
        self._load(manager, 9000.0, clock)

        # The measurement stands, but a paused group is not going anywhere:
        # aiming ahead would land the member past a position that will not move.
        manager.playback.set_reference(
            utils.ms_to_ticks(60000), manager.server_now_ms(), False
        )

        assert manager._load_allowance_ms() == 0.0

    def test_a_fast_load_is_not_worth_aiming_off_for(self, manager, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(utils, "local_ms", lambda: clock[0])
        self._playing_at(manager, 60000)

        self._load(manager, utils.LOAD_ALLOWANCE_MIN_MS - 1, clock)

        assert manager._load_allowance_ms() == 0.0

    def test_an_absurd_load_does_not_poison_the_estimate(self, manager, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(utils, "local_ms", lambda: clock[0])
        self._playing_at(manager, 60000)

        self._load(manager, 6000.0, clock)
        # A dialog, a stall, a device asleep — not the load, and folding it in
        # would aim the next load minutes past the group.
        self._load(manager, utils.LOAD_ALLOWANCE_MAX_MS + 1, clock)

        assert manager._load_allowance_ms() == pytest.approx(6000.0)

    def test_successive_loads_are_smoothed(self, manager, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(utils, "local_ms", lambda: clock[0])
        self._playing_at(manager, 60000)

        self._load(manager, 8000.0, clock)
        self._load(manager, 4000.0, clock)

        # One slow load among fast ones must not hold the aim high for ever,
        # and one fast load must not drop it instantly either.
        assert manager._load_allowance_ms() == pytest.approx(6000.0)

    def test_a_load_nobody_started_is_ignored(self, manager, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(utils, "local_ms", lambda: clock[0])
        self._playing_at(manager, 60000)

        # onAVStarted for a play the group did not initiate: there is no start
        # stamp, so there is nothing to measure.
        manager._note_load_completed()

        assert manager._load_allowance_ms() == 0.0

    def test_the_reload_actually_starts_ahead_of_the_group(self, manager, monkeypatch):
        """The whole point, exercised through _start_item rather than the helper.

        A group Seek on a transcoding member reloads the stream, and the reload
        must ask the server to begin encoding where the group *will be*, not
        where it was when the command arrived.
        """
        clock = [1000.0]
        monkeypatch.setattr(utils, "local_ms", lambda: clock[0])
        join(manager)

        started = []
        manager._api_raw = lambda kind, *a, **k: {"Id": "item-1", "Type": "Movie"}
        manager.playback.play_item = lambda item, ticks: started.append(ticks)

        # Time the load first, then plant the reference, so the expected value
        # is the group position plus the allowance and nothing else — the clock
        # this test drives also moves the group's own extrapolation.
        self._load(manager, 9000.0, clock)
        self._playing_at(manager, 60000)

        manager.current_item_id = "item-1"
        manager.current_playlist_item_id = "pli-1"
        manager.reload_current_item()

        assert len(started) == 1
        # 60s group position + the 9s this device's last load took.
        assert utils.ticks_to_ms(started[0]) == pytest.approx(69000.0, abs=50)


class TestLoadWatchdogGeneration:
    """A load's watchdog must only be able to fail *that* load.

    It used to key on the playlist item id, which a transcode-seek reload does
    not change: the watchdog armed by a load that succeeded 45s earlier found
    "phase == loading" true again for the reload and declared it failed, and
    the client left the group mid-seek. Measured live 2026-08-18 — a group
    start at 12:59:37 killed the reload from a seek at 13:00:19.
    """

    @staticmethod
    def _arm(manager, item_id="item-1", playlist_item_id="pli-1"):
        """Run _start_item and hand back the watchdog it armed."""
        armed = []
        import threading as _t

        class FakeTimer:
            def __init__(self, delay, func, args=()):
                armed.append((func, args))

            def start(self):
                pass

            @property
            def daemon(self):
                return True

            @daemon.setter
            def daemon(self, v):
                pass

        real = manager_module.threading.Timer
        manager_module.threading.Timer = FakeTimer
        try:
            manager._start_item(item_id, playlist_item_id)
        finally:
            manager_module.threading.Timer = real
        return armed[-1] if armed else None

    def test_a_superseded_load_s_watchdog_does_not_fire(self, manager):
        join(manager)
        manager._api_raw = lambda kind, *a, **k: {"Id": "item-1", "Type": "Movie"}
        manager.playback.play_item = lambda item, ticks: None

        failures = []
        manager._load_failed = lambda reason: failures.append(reason)

        first = self._arm(manager)
        # The same item reloaded — a transcode seek, which is the whole point.
        self._arm(manager)

        # The first load's watchdog fires while the second is still loading.
        func, args = first
        func(*args)

        assert failures == [], "a superseded load's watchdog killed a healthy reload"

    def test_the_current_load_s_watchdog_still_fires(self, manager):
        join(manager)
        manager._api_raw = lambda kind, *a, **k: {"Id": "item-1", "Type": "Movie"}
        manager.playback.play_item = lambda item, ticks: None

        failures = []
        manager._load_failed = lambda reason: failures.append(reason)

        func, args = self._arm(manager)
        func(*args)

        # Nothing superseded it and the phase never left loading: this one is
        # genuinely stuck, and the watchdog is the only thing that notices.
        assert failures == ["no playback within 45s"]
