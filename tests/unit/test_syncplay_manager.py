"""Protocol behaviour tests for the SyncPlay manager, mirroring the
client requirements of SYNCPLAY.md (§5.1 command gating, §5.3 queue
idempotency, §9 membership lifecycle). Ported from the fork with the
kofin construction signature, dialog/settings fakes, and the kofin.sync.db
id mapping; plus the kofin-specific kicked-probe and group-flag tests."""

import pytest

import kofin.sync.db as database_module
import kofin.syncplay.manager as manager_module
import kofin.syncplay.playback as playback_module
from kofin.syncplay import utils
from kofin.syncplay.manager import SyncPlayManager
from tests.unit.fakes import FakeAddon, FakeWindow


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


def join(manager):
    info = {
        "GroupId": "g1",
        "GroupName": "movie night",
        "State": "Idle",
        "Participants": ["alice", "bob"],
    }

    manager._handle_group_update({"GroupId": "g1", "Type": "GroupJoined", "Data": info})
    # Don't let the real TimeSync thread run in unit tests.
    if manager.timesync is not None:
        manager.timesync.stop()
        manager.timesync = None


def make_queue(
    items=(("item-1", "pl-1"),), index=0, playing=False, last_update=None, start_ticks=0
):
    return {
        "GroupId": "g1",
        "Type": "PlayQueue",
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

    def test_participants_fallback(self, manager):
        join(manager)
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


class TestLifecycle:
    def test_group_left_cleans_up(self, manager):
        join(manager)
        manager._handle_group_update(
            {"GroupId": "g1", "Type": "GroupLeft", "Data": "g1"}
        )
        assert not manager.in_group()

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
    """Reconnect contract (report R2): after a WS drop assume kicked —
    probe GET /SyncPlay/List, rejoin if the group survives, detach with a
    toast if it is gone, and hold if the list is unavailable."""

    def test_reconnect_probes_and_rejoins(self, manager):
        join(manager)
        manager._api_raw.results["syncplay_list"] = [
            {"GroupId": "g1", "GroupName": "movie night"}
        ]

        manager._on_ws_connected()

        assert manager._api_raw.named("syncplay_list")
        assert manager._api_raw.named("syncplay_join")
        assert manager.in_group()

    def test_reconnect_group_gone_detaches(self, manager):
        join(manager)
        toasts = []
        manager._toast = lambda message, **kwargs: toasts.append(message)
        manager._api_raw.results["syncplay_list"] = [{"GroupId": "OTHER"}]

        manager._on_ws_connected()

        assert manager._api_raw.named("syncplay_join") == []
        assert not manager.in_group()
        assert len(toasts) == 1

    def test_reconnect_list_unavailable_keeps_group(self, manager):
        join(manager)
        manager.list_groups = lambda: None  # server not reachable yet

        manager._on_ws_connected()

        assert manager.in_group()
        assert manager._api_raw.named("syncplay_join") == []

    def test_reconnect_outside_group_is_noop(self, manager):
        manager._on_ws_connected()
        assert manager._api_raw.calls == []

    def test_wake_probes_group(self, manager):
        join(manager)
        manager._api_raw.results["syncplay_list"] = [{"GroupId": "g1"}]

        manager.on_wake()

        assert manager._api_raw.named("syncplay_list")
        assert manager._api_raw.named("syncplay_join")

    def test_wake_outside_group_is_noop(self, manager):
        manager.on_wake()
        assert manager._api_raw.calls == []


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
