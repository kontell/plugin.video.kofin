"""The SyncPlay phase table (shell refactor phase 2, P2.0).

``(phase, event) -> phase`` for every transition the manager's nine write
sites make today — join, a queue that starts an item, a queue that adopts
the item already playing, A/V up, the group unpause, the local stop, the
item ending, the group stop, leaving, being left. P2.5 replaces the
spellings with ``Phase``; this table must pass byte-identical before and
after. The thread story is unchanged and not modelled here.
"""

import pytest

import kofin.syncplay.manager as manager_module
from kofin.syncplay import utils
from kofin.syncplay.manager import SyncPlayManager
from tests.unit.fakes import FakeAddon, FakeWindow
from tests.unit.test_syncplay_manager import (
    FakePlayer,
    FakeProviders,
    Recorder,
    join,
    make_queue,
)

IDLE, LOADING, WAITING_READY, SYNCED = "idle", "loading", "waiting_ready", "synced"


@pytest.fixture(autouse=True)
def kodi_fakes(monkeypatch):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    # _start_item arms a 45 s load watchdog through utils.later; a real
    # Timer thread would outlive the test. Record instead of scheduling.
    monkeypatch.setattr(utils, "later", lambda seconds, func, *args: None)


@pytest.fixture
def manager():
    m = SyncPlayManager(None, FakePlayer())
    m._post = lambda func, *args: func(*args)
    m._toast = lambda *a, **k: None
    m.enabled = lambda: True
    m._api = Recorder()
    m._api_raw = Recorder()
    m.providers = FakeProviders()
    m.playback.play_item = lambda target: None
    m.playback.prepare_ready = lambda: None
    m.playback.start_loop = lambda: None
    m.playback.ensure_paused = lambda: None
    m.playback.stop_media = lambda: None
    m._user_stopped_prompt = lambda: None
    m._watch_hold = lambda hold: None
    join(m)
    yield m
    m.timesync = None
    m._running = False
    m._inbox.put(None)
    m._dispatcher.join(timeout=2)


def queue_starts_item(m):
    m.player.playing = False
    m._handle_group_update(make_queue(version=5))


def queue_adopts_playing_item(m):
    m.player.playing = True
    m._local_item_id = lambda: "item-1"
    m._handle_group_update(make_queue(version=5))


def queue_renames_playing_item(m):
    """The same item comes back under a fresh PlaylistItemId (a queue we
    proposed with SetNewQueue): adopted, never reloaded."""
    m.player.playing = True
    m._local_item_id = lambda: "item-1"
    m._handle_group_update(make_queue(version=5, items=(("item-1", "pl-2"),)))


def group_left(m):
    m._handle_group_update({"GroupId": "g1", "Type": "GroupLeft"})


EVENTS = {
    "queue_starts_item": queue_starts_item,
    "queue_adopts_playing_item": queue_adopts_playing_item,
    "queue_renames_playing_item": queue_renames_playing_item,
    "avstarted": lambda m: m.on_avstarted(),
    "unpaused": lambda m: m.on_local_unpaused(),
    "ended": lambda m: m.on_ended(),
    "stopped": lambda m: m.on_stopped(),
    "group_stopped": lambda m: m.on_group_stopped(),
    "leave": lambda m: m._leave_locally(),
    "group_left": group_left,
    "playback_started": lambda m: m.on_playback_started(),
}

# (phase before, event) -> phase after, as the code stands at P2.0.
TABLE = [
    (IDLE, "queue_starts_item", LOADING),
    (IDLE, "queue_adopts_playing_item", WAITING_READY),
    # The playlist item already being followed, while not idle: the queue
    # dedup at the top of _apply_play_queue ignores it — no transition.
    (SYNCED, "queue_adopts_playing_item", SYNCED),
    (WAITING_READY, "queue_adopts_playing_item", WAITING_READY),
    # A fresh PlaylistItemId for the item on screen: adopted, not reloaded.
    (SYNCED, "queue_renames_playing_item", WAITING_READY),
    (IDLE, "queue_renames_playing_item", WAITING_READY),
    (LOADING, "avstarted", WAITING_READY),
    (WAITING_READY, "avstarted", WAITING_READY),
    (SYNCED, "avstarted", SYNCED),
    (LOADING, "unpaused", SYNCED),
    (WAITING_READY, "unpaused", SYNCED),
    (SYNCED, "unpaused", SYNCED),
    (IDLE, "unpaused", IDLE),
    (SYNCED, "ended", IDLE),
    (WAITING_READY, "ended", WAITING_READY),
    (LOADING, "ended", LOADING),
    (SYNCED, "stopped", IDLE),
    (WAITING_READY, "stopped", IDLE),
    (LOADING, "stopped", LOADING),
    (IDLE, "stopped", IDLE),
    (SYNCED, "group_stopped", IDLE),
    (LOADING, "group_stopped", IDLE),
    (SYNCED, "leave", IDLE),
    (WAITING_READY, "leave", IDLE),
    (SYNCED, "group_left", IDLE),
    (LOADING, "playback_started", LOADING),
    (IDLE, "playback_started", IDLE),
    (SYNCED, "playback_started", SYNCED),
]


@pytest.mark.parametrize("before,event,after", TABLE)
def test_phase_transition(manager, before, event, after):
    manager.phase = before
    manager.current_playlist_item_id = "pl-1"
    manager.current_item_id = "item-1"

    EVENTS[event](manager)

    assert manager.phase == after, "%s --%s--> %s, expected %s" % (
        before,
        event,
        manager.phase,
        after,
    )


def test_the_table_covers_every_phase_the_machine_names():
    phases = {row[0] for row in TABLE} | {row[2] for row in TABLE}
    assert phases == {IDLE, LOADING, WAITING_READY, SYNCED}


def test_a_fresh_manager_is_idle():
    m = SyncPlayManager(None, FakePlayer())
    try:
        assert m.phase == IDLE
    finally:
        m._running = False
        m._inbox.put(None)
        m._dispatcher.join(timeout=2)


def test_in_group_is_not_a_phase(manager):
    """Membership and phase are separate axes: a joined manager is idle
    until a queue moves it (the write site in _on_group_joined does not
    exist — join leaves the phase alone)."""
    assert manager.in_group()
    assert manager.phase == IDLE
    assert manager_module.SyncPlayManager  # the module under test
