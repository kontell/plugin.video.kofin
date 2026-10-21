"""Fine position sync for SyncPlay: tempo pulses through Kodi's own tempo.

The actuator the drift shakedown could not have, now in Kodi itself. Stock
Kodi plays a tempo only while "Sync playback to display" is on, and that
setting slaves the media clock to the panel — ``docs/syncplay-drift-shakedown.md``
§10 measured rate errors up to 4.3 % from it — which is why fine sync went
through the inputstream.tempo add-on on Kodi 21 and 22. Kodi 23 with
kontell/xbmc #14 and #16 plays the tempo in the audio itself (a resample ratio
the engine keeps) and lets the audio be the clock, so the tempo works with the
display clock **off**, where the rig's devices free-run within a few hundred
ppm of real time. #37 is what keeps a tempo beyond the atempo threshold from
drawing a clock correction. ``docs/syncplay-fine-sync.md`` is the design.

Three parts here:

* :class:`KodiTempo` — the actuator: ``Player.SetTempo`` and the ``tempo``
  property over JSON-RPC, since Python's ``xbmc.Player`` has no tempo call,
  and ``Player.TempoEnabled`` as the test of whether the playing item can
  take one at all.
* :class:`TempoSession` — whether this group membership has fine sync: the
  setting, read at join and whenever it changes.
* :class:`PulseScheduler` — between group commands, measure the residual
  against the group estimate and close it with a *pulse*: a rate ``r`` held
  for ``T`` seconds displaces the position by ``(r − 1) × T``. One pulse,
  confirmed by reading the tempo back, then a quiet window before the
  residual is trusted again.

This is not the continuous ladder that was withdrawn. That one ran a
per-second loop against an actuator whose precondition created the error;
this one issues bounded, confirmed pulses against an actuator that works with
that precondition off, and stops — and says so — when the residual keeps
regrowing faster than pulses can reasonably close it.

The planner is Kodi-free so the arithmetic is unit testable; the scheduler
takes its player reads through the controller.
"""

import math
import statistics
import threading
import time

import xbmc

from kofin.core import kodirpc, settings, state
from kofin.core.log import Logger
from kofin.syncplay import utils

#################################################################################################

LOG = Logger(__name__)

VIDEO_PLAYER = 1  # JSON-RPC's playerid for VideoPlayer; PAPlayer is 0

# Kodi's tempo has three decimals: both players round to them, so a pulse is
# planned on that grid and its length worked out from the rate Kodi will
# really play.
RATE_STEP = 0.001
# A read within this of a rate is that rate.
RATE_MATCH = 0.0015

# Pulse planning. A pulse aims to last PULSE_AIM_S, so the rate scales with the
# residual between RATE_MIN and the user's ceiling (syncPlayMaxRate, default
# 25 %), and the duration stretches only once the rate is capped. Rates above
# RAMP_STEP are ramped in and out in RAMP_STEP increments every RAMP_DT — not
# seamless, just less of a jolt; the viewer may well notice the fast-forward.
RATE_MIN = 0.005
RATE_MAX_DEFAULT = 0.25
RATE_MAX_CEILING = 0.25
PULSE_AIM_S = 5.0
PULSE_MAX_S = 40.0
# A pulse squeezed shorter than this by the end of its item is not worth one.
PULSE_MIN_S = 1.0
RAMP_STEP = 0.05
RAMP_DT = 0.25
# Below this the residual is left alone. One frame at 24 fps is 42 ms, and the
# position reads jitter by about that much on an Android box: at 50 ms the Tab
# pulsed ±50 ms against its own read noise, so the band sits above it.
DEADBAND_MS = 75.0
# Above the budget (syncPlayPulseBudget, default 5 s) a seek closes the
# residual instead; a skip is for gross errors only, because it is both jarring
# and, on Android, inaccurate. At 25 % a 5 s residual is a 20 s pulse, a 10 s
# residual (the slider max) is a 40 s pulse — one pulse closes the budget.
BUDGET_DEFAULT_MS = 5000.0
SEEK_BLACKOUT_S = 30.0
# What a seek aimed at the group's current position leaves behind before it
# has been measured on a device: restart time plus landing error.
SEEK_LAG_DEFAULT_MS = 500.0
# Residual samples the decision is taken over, at the loop's 250 ms: 3 s.
WINDOW_SAMPLES = 12
# After a pulse ends (or any seek or resume): this long before measuring. A
# tempo change reaches the clock once the audio already queued in the engine
# and the sink has played out at the old rate (VideoPlayer times its clock
# change by that delay), a few hundred milliseconds. It is not the demux queue
# an add-on's rate had to play out through, so videoplayer.queuetimesize plays
# no part.
SETTLE_S = 1.0
# A pulse ends, settle included, at least this long before its item does: the
# item after it must start at 1.0.
END_MARGIN_S = 2.0
# A live item logs its reading on the source clock this often (debug).
LIVE_LOG_INTERVAL_S = 30.0
# How long the player gets to report a tempo kofin asked for. VideoPlayer
# takes it on its player thread within milliseconds; three seconds is a
# player that is not taking it.
APPLY_TIMEOUT_S = 3.0
# A read that disagrees with the last ask this soon after it is the player
# thread catching up, not someone else's change.
CONFIRM_GRACE_S = 1.0
# A tempo kofin did not ask for — the OSD's play speed — is put back to 1.0
# once it has held this long. Long enough that a live gate can step a member's
# position through Player.SetTempo (0.75 s at 1.2x is +150 ms), short enough
# that a viewer's speed costs a group a second or two to undo.
FOREIGN_GRACE_S = 2.0
# Give up when this many consecutive pulses went the same way and the residual
# regrew between them at a steady rate above GIVEUP_PPM: a rate mismatch, not
# jitter. The display clock off, the rig's boxes free-run within ±550 ppm; the
# display clock on imposed 0.5–4.3 %. A regrowth above GIVEUP_PPM_CEILING is
# not a rate at all but a step — a stall, a seek, an injection — and the rates
# of a real mismatch agree with each other within GIVEUP_SPREAD. Measured: a
# 1 s step on the desktop read as "−160 000 ppm" and tripped the old test.
GIVEUP_PULSES = 3
GIVEUP_PPM = 3000.0
GIVEUP_PPM_CEILING = 60000.0
GIVEUP_SPREAD = 3.0

#################################################################################################


class Profile(object):
    """What a pulse may do on one player: the limits the planner and the
    scheduler read, so nothing else in them depends on the player."""

    def __init__(
        self,
        name,
        deadband_ms,
        rate_min,
        rate_max,
        aim_s,
        budget_ms,
        settle_s=SETTLE_S,
        end_margin_s=END_MARGIN_S,
    ):
        self.name = name
        self.deadband_ms = deadband_ms
        self.rate_min = rate_min
        # On Kodi's grid, so a rounded rate never lands above the ceiling.
        self.rate_max = max(
            RATE_STEP, math.floor(rate_max / RATE_STEP + 1e-6) * RATE_STEP
        )
        self.aim_s = aim_s
        self.budget_ms = budget_ms
        self.settle_s = settle_s
        self.end_margin_s = end_margin_s


VIDEO_DEFAULT = Profile(
    "video", DEADBAND_MS, RATE_MIN, RATE_MAX_DEFAULT, PULSE_AIM_S, BUDGET_DEFAULT_MS
)


def video_profile():
    """Video's limits, with the user's budget and speed ceiling."""
    budget_ms = float(settings.get_int("syncPlayPulseBudget") or BUDGET_DEFAULT_MS)
    rate_max = (settings.get_int("syncPlayMaxRate") or RATE_MAX_DEFAULT * 100) / 100.0
    return Profile(
        "video",
        DEADBAND_MS,
        RATE_MIN,
        min(RATE_MAX_CEILING, max(RATE_MIN, rate_max)),
        PULSE_AIM_S,
        budget_ms,
    )


def plan_pulse(residual_ms, profile=VIDEO_DEFAULT):
    """The (rate, seconds) pulse that closes ``residual_ms``, or None.

    Positive residual means this member is behind the group, so the rate is
    above 1. None inside the deadband. The caller checks the budget first;
    here a residual past it simply saturates at the profile's ceiling for
    PULSE_MAX_S. The rate is on Kodi's three-decimal grid and the length is
    worked out from it, so the plan moves what it says.
    """
    magnitude = abs(residual_ms)

    if magnitude <= profile.deadband_ms:
        return None

    step = min(
        profile.rate_max,
        max(profile.rate_min, magnitude / (profile.aim_s * 1000.0)),
    )
    step = max(RATE_STEP, round(step / RATE_STEP) * RATE_STEP)
    seconds = min(PULSE_MAX_S, magnitude / (step * 1000.0))
    rate = 1.0 + step if residual_ms > 0 else 1.0 - step
    return round(rate, 3), seconds


def pulse_schedule(rate, seconds):
    """The tempo writes that make up one pulse: (offset_s, rate) pairs,
    the last of them (…, 1.0).

    A rate within RAMP_STEP of 1.0 is one write and one return. Beyond that it
    is ramped: RAMP_STEP increments every RAMP_DT up to the rate, held, then
    stepped back down — and the hold is shortened by what the ramps already
    displace, so the whole schedule still moves (rate − 1) × seconds.
    """
    step = rate - 1.0
    sign = 1.0 if step > 0 else -1.0
    steps = int(math.ceil(abs(step) / RAMP_STEP - 1e-9))

    if steps <= 1:
        return [(0.0, rate), (seconds, 1.0)]

    intermediate = [round(1.0 + sign * RAMP_STEP * k, 3) for k in range(1, steps)]
    ramp_displacement = 2.0 * sum((value - 1.0) * RAMP_DT for value in intermediate)
    hold = max(2.0 * RAMP_DT, seconds - ramp_displacement / step)

    schedule = []
    offset = 0.0

    for value in intermediate:
        schedule.append((offset, value))
        offset += RAMP_DT

    schedule.append((offset, rate))
    offset += hold

    for value in reversed(intermediate):
        schedule.append((offset, value))
        offset += RAMP_DT

    schedule.append((offset, 1.0))
    return schedule


def regrowth_ppm(residual_ms, elapsed_s):
    """How fast the residual came back, as a rate error in ppm."""
    if elapsed_s <= 0:
        return 0.0

    return residual_ms / elapsed_s * 1000.0


#################################################################################################


def takes_correction():
    """Whether Kodi's ``Player.SetTempo`` takes ``correction`` (kontell/xbmc
    #38): a tempo the player plays as any other while the GUI shows none of
    it. Kodi refuses a parameter its schema does not name, so the schema is
    read rather than the call tried."""
    schema = kodirpc.call(
        "JSONRPC.Introspect",
        {
            "getdescriptions": False,
            "filter": {"id": "Player.SetTempo", "type": "method"},
        },
    )

    try:
        params = schema["methods"]["Player.SetTempo"]["params"]
    except (KeyError, TypeError):
        return False

    return any(
        isinstance(param, dict) and param.get("name") == "correction"
        for param in params
    )


class KodiTempo(object):
    """Kodi's own tempo on one player.

    Python's ``xbmc.Player`` has no tempo call, so the actuator is JSON-RPC:
    ``Player.SetTempo``, and the ``tempo`` property, which reads what the
    player has taken. Kodi refuses a tempo while paused and keeps the one in
    force across a pause, and refuses one for an item that cannot take it — a
    realtime stream with no time range, a bitstream track — which is what
    ``Player.TempoEnabled`` answers for the playing item.

    Every rate goes as a correction where Kodi takes the flag: no seek bar
    for the change or its end, and the OSD reads normal speed throughout,
    while the ``tempo`` property still reads the rate. Without it each change
    puts the seek bar up for three seconds, labelled with the rate.
    """

    def __init__(self, playerid):
        self.playerid = playerid
        self.correction = takes_correction()

    def set(self, rate):
        """Ask for ``rate``; True when Kodi took the call."""
        # Always a float: Kodi reads the parameter as a double, and a JSON
        # integer would be refused as invalid.
        params = {"playerid": self.playerid, "tempo": float(rate)}

        if self.correction:
            params["correction"] = True

        return isinstance(kodirpc.call("Player.SetTempo", params), dict)

    def read(self):
        """The tempo the player is taking, or None when it cannot be read."""
        result = kodirpc.call(
            "Player.GetProperties",
            {"playerid": self.playerid, "properties": ["tempo"]},
        )

        try:
            return float(result["tempo"])
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def enabled():
        """Whether the playing item can take a tempo now."""
        return bool(xbmc.getCondVisibility("Player.TempoEnabled"))


def _player_active(playerid):
    """True or False when Kodi answered; None when it could not be asked."""
    active = kodirpc.call("Player.GetActivePlayers")

    if not isinstance(active, list):
        return None

    return any(
        isinstance(player, dict) and player.get("playerid") == playerid
        for player in active
    )


def settle_left_rate(reason=""):
    """Put 1.0 back where a kofin pulse left its rate in force; True once
    nothing is left owed.

    The record (``state.syncplay_pulse``) is written when a pulse asks for a
    rate and dropped when Kodi takes 1.0 back. It outlives a refusal — a pulse
    cut while paused, since Kodi keeps a tempo across a pause and refuses a new
    one until the resume — and a service generation that ended mid-pulse, which
    would otherwise leave the item playing fast or slow to its end. It is
    dropped unasked when the player is already at 1.0 (a new item starts
    there) or no longer playing.
    """
    record = state.syncplay_pulse()

    if not record:
        return True

    playerid = record.get("player")

    if not isinstance(playerid, int):
        state.clear_syncplay_pulse()
        return True

    actuator = KodiTempo(playerid)
    current = actuator.read()

    if current is None:
        if _player_active(playerid) is False:
            state.clear_syncplay_pulse()
            return True

        return False

    if abs(current - 1.0) < RATE_MATCH:
        state.clear_syncplay_pulse()
        return True

    if not actuator.set(1.0):
        return False  # still paused: the next resume tries again

    state.clear_syncplay_pulse()
    LOG.info("[ syncplay/pulse ] %.3fx left in force%s; back to 1.0x", current, reason)
    return True


#################################################################################################


class PulseScheduler(object):
    """Closes the residual between commands with confirmed tempo pulses.

    Driven by the controller's 250 ms loop through :meth:`tick`; told about
    every command and seek so a pulse is never left running across one.

    Nothing here waits under ``_lock``: a write is one JSON-RPC call, and its
    confirmation is a later tick finding the tempo read back, not a sleep.
    ``cancel()`` and ``before_seek()`` take the same lock on the command
    thread, and a scheduled Pause must not wait on the actuator. The one
    corrective seek goes through the controller with the lock dropped,
    because the command path holds the player lock before it asks for this
    one (``_seek_and_settle`` → ``before_seek``).

    **A pulse never outlives a pause.** Kodi refuses a tempo while paused and
    keeps the one in force across a pause, so a pulse cut while paused — by a
    viewer's pause, or a command arriving on a paused player — leaves its
    rate owed: the next playing tick, and every resume
    (``SyncPlayManager.on_resumed``), put 1.0 back (:meth:`settle_owed`).
    """

    def __init__(self, controller):
        self.controller = controller
        self.profile = None  # the playing item's limits once it is armed
        self.actuator = None
        self._session_id = None  # PlaySessionId the scheduler is armed for
        self._window = []
        self._pulse = None  # the running pulse: its remaining writes
        self._awaiting = None  # a write waiting to be read back
        self._measure = None  # an ended pulse, moved measured after the settle
        self._asked = 1.0  # the last tempo asked for
        self._asked_at = 0.0
        self._marked = False  # the left-rate record is ours to clear
        self._settle_until = 0.0
        self._seek_blackout_until = 0.0
        self._history = []  # (direction, regrowth ppm) per pulse
        self._gave_up = False
        self._unarmed_logged = False
        self._disabled_logged = False
        self._foreign_logged = False
        self._foreign_since = None  # when a tempo kofin did not ask for was seen
        self._session_time_logged = False
        # A live item (utils.claim_is_live): never seeked, and a forward
        # pulse Kodi ends at the live edge ends fine sync (LIVE_EDGE_PULSES).
        self._live = False
        self._edge_pulses = 0
        self._live_logged_at = 0.0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Arming
    # ------------------------------------------------------------------

    def _session_active(self):
        active = getattr(self.controller.manager, "fine_sync_active", None)
        return bool(active()) if active is not None else False

    def _arm_for_current(self):
        """Arm for the playing item if it is not the one armed. Under the
        lock; returns the claim, or None outside a fine-sync session."""
        claim = self.controller.manager.current_claim()

        if not claim or not self._session_active():
            return None

        if claim.get("PlaySessionId") != self._session_id:
            self._arm(claim)

        return claim

    def _arm(self, claim):
        """Bind to the playing item, or note that it has no fine sync."""
        self._session_id = claim.get("PlaySessionId")
        self._window = []
        self._pulse = None
        self._awaiting = None
        self._measure = None
        self._history = []
        self._gave_up = False
        self._live = utils.claim_is_live(claim)
        self._edge_pulses = 0
        self._disabled_logged = False
        self._foreign_logged = False
        self._foreign_since = None

        if self.controller._is_audio():
            self.profile = None
            self.actuator = None

            if not self._unarmed_logged:
                self._unarmed_logged = True
                LOG.info(
                    "[ syncplay/tempo ] %s is audio; command-only sync for this item",
                    claim.get("Id"),
                )

            return

        self._unarmed_logged = False
        self.profile = video_profile()
        self.actuator = KodiTempo(VIDEO_PLAYER)
        LOG.info(
            "[ syncplay/tempo ] fine sync armed for %s "
            "(%s: budget %.0fms, up to %.1f%%%s%s)",
            claim.get("Id"),
            self.profile.name,
            self.profile.budget_ms,
            self.profile.rate_max * 100.0,
            ", live: on the source clock, pulses only" if self._live else "",
            "" if self.actuator.correction else ", shown on the OSD",
        )

    def _write_off(self, why):
        """This item takes no pulses after all: command-only until the next."""
        LOG.warning("[ syncplay/pulse ] %s; command-only sync for this item", why)
        self.profile = None

    def can_close(self, residual_ms):
        """Whether fine sync will take this residual, so a seek is not needed.

        A live item has no seek to fall back on, so every residual is fine
        sync's — saturated pulses close a large one over time. An item that
        cannot take a tempo now (``Player.TempoEnabled``) leaves it to a seek.
        """
        with self._lock:
            self._arm_for_current()  # a command can ask before the first tick

            if self.profile is None or self._gave_up:
                return False

            live = self._live
            budget_ms = self.profile.budget_ms

        if not KodiTempo.enabled():
            return False

        return live or abs(residual_ms) <= budget_ms

    def reset(self):
        with self._lock:
            self.cancel("stop")
            self.profile = None
            self.actuator = None
            self._session_id = None
            self._window = []
            self._history = []
            self._gave_up = False
            self._session_time_logged = False

    # ------------------------------------------------------------------
    # The actuator
    # ------------------------------------------------------------------

    def _set(self, rate, now):
        """Ask for ``rate``; False when Kodi refused it."""
        actuator = self.actuator

        if actuator is None or not actuator.set(rate):
            return False

        self._asked = rate
        self._asked_at = now

        if abs(rate - 1.0) >= RATE_MATCH and not self._marked:
            state.publish_syncplay_pulse({"player": actuator.playerid})
            self._marked = True

        return True

    def _return_to_one(self, now):
        """Ask for 1.0; while paused Kodi refuses it, and it stays owed."""
        if self.actuator is None or not self.actuator.set(1.0):
            return False

        self._back_at_one(now)
        return True

    def settle_owed(self):
        """Put back a rate a cut pulse left in force, when nothing of ours is
        running: on a resume, in a group or not, and on the playing tick."""
        with self._lock:
            if self._pulse is not None or self._awaiting is not None:
                return

            if not self._marked and not state.syncplay_pulse():
                return

            self._settle_left(time.time())

    def _settle_left(self, now):
        if settle_left_rate(" after a pause"):
            self._marked = False
            self._asked = 1.0
            self._asked_at = now

    def _back_at_one(self, now):
        """Kodi reads 1.0 where kofin last asked for a rate: a new item starts
        there, and an owed 1.0 may have been put back from a resume."""
        self._asked = 1.0
        self._asked_at = now

        if self._marked:
            state.clear_syncplay_pulse()
            self._marked = False

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def tick(self):
        with self._lock:
            seek = self._tick()

        if seek is not None:
            # Outside the lock: the controller takes the player lock, and the
            # command path takes that one first before asking for ours.
            self.controller.correct_position()
            self.note_settle()

    def _tick(self):
        """One scheduling step under the lock. Returns the residual to seek
        away, or None."""
        if self._arm_for_current() is None or self.profile is None:
            return None

        controller = self.controller
        now = time.time()

        if controller._is_paused():
            if self._pulse is not None or self._awaiting is not None:
                self._cut("pause", now)

            self._window = []
            return None

        if self._marked and self._pulse is None and self._awaiting is None:
            # A rate of ours in force with no pulse running is one a pause
            # refused to take back: owed, and the player is playing again.
            self._settle_left(now)

        current = self.actuator.read() if self.actuator is not None else None

        if self._awaiting is not None:
            self._check_awaiting(current, now)
            return None

        if self._pulse is not None:
            self._advance_pulse(current, now)
            return None

        if current is not None and self._changed_elsewhere(current, now):
            if abs(current - 1.0) < RATE_MATCH:
                self._back_at_one(now)
            else:
                # Someone else's tempo: measure nothing while it holds, and put
                # 1.0 back once it has held FOREIGN_GRACE_S.
                self._window = []

                if self._foreign_since is None:
                    self._foreign_since = now
                elif now - self._foreign_since >= FOREIGN_GRACE_S:
                    self._foreign(current, now)

                return None
        elif self._foreign_since is not None:
            # It was taken back by whoever set it: let the clock catch up.
            self._foreign_since = None
            self._settle_until = max(self._settle_until, now + self.profile.settle_s)

        if self._gave_up or not controller.reference_is_playing():
            self._window = []
            return None

        if now < self._settle_until:
            return None

        if self._measure is not None:
            self._report_moved(now)

        estimate = controller.estimate_position_ms()

        if estimate is None:
            return None

        if self._live and not controller.live_on_source_clock():
            # The group is anchored on session time (a proposer without
            # the clock) or this member cannot read its own: nothing to
            # converge on — the commands keep it tuned together (P2).
            if not self._session_time_logged:
                self._session_time_logged = True
                LOG.info(
                    "[ syncplay/tempo ] live group is on session time; "
                    "no pulses until a source clock is shared"
                )
            self._window = []
            return None

        position = controller._position_ms()
        self._window.append(estimate - position)

        if self._live and now - self._live_logged_at >= LIVE_LOG_INTERVAL_S:
            # The ruler for a live gate: every member's reading on the one
            # clock, against the group, at a cadence a log can be read at.
            self._live_logged_at = now
            LOG.debug(
                "[ syncplay/live ] %.1fs on the source clock, %+.0fms off the group",
                (position - utils.LIVE_PTS_EPOCH_S * 1000.0) / 1000.0,
                estimate - position,
            )

        del self._window[:-WINDOW_SAMPLES]

        if len(self._window) < WINDOW_SAMPLES:
            return None

        residual = statistics.median(self._window)
        # The window's spread is this member's own read noise between pulses,
        # which is what a deadband has to sit above.
        LOG.debug(
            "[ syncplay/window ] %+.0fms, spread %.0fms",
            residual,
            max(self._window) - min(self._window),
        )

        # Beyond the budget a seek is the tool — once, for gross errors, and
        # only when every sample in the window agrees. Otherwise — the seek's
        # blackout holds, or the residual straddles the budget — pulses close
        # it after all, saturated at the rate ceiling for PULSE_MAX_S. The
        # Bravia sat at +2.5 s for 25 s after a resume when a median beyond
        # the budget and a window not all beyond it meant neither.
        # A live stream has no seek (ffmpeg's HLS demuxer refuses one on a
        # live playlist), so a live residual beyond the budget takes the
        # saturated pulses instead, as many as it needs.
        budget_ms = self.profile.budget_ms

        if (
            abs(residual) > budget_ms
            and not self._live
            and now >= self._seek_blackout_until
            and all(abs(sample) > budget_ms for sample in self._window)
        ):
            self._seek_blackout_until = now + SEEK_BLACKOUT_S
            self._window = []
            LOG.info(
                "[ syncplay/align ] %+.0fms is beyond the pulse budget: seeking",
                residual,
            )
            return residual

        plan = plan_pulse(residual, self.profile)

        if plan is None:
            return None

        if self._losing(residual, now):
            self._give_up()
            return None

        self._start_pulse(plan[0], plan[1], residual, now)
        return None

    # ------------------------------------------------------------------
    # Pulses
    # ------------------------------------------------------------------

    def _fit_to_item(self, seconds):
        """The pulse's length once the end of its item is allowed for, or None
        when there is no room for one. A live stream has no end."""
        if self._live:
            return seconds

        remaining_ms = self.controller.remaining_ms()

        if remaining_ms is None:
            return seconds

        profile = self.profile
        assert profile is not None  # only called armed (_start_pulse)
        room = remaining_ms / 1000.0 - profile.settle_s - profile.end_margin_s

        if room >= seconds:
            return seconds

        return room if room >= PULSE_MIN_S else None

    def _start_pulse(self, rate, seconds, residual, now):
        self._window = []

        if not KodiTempo.enabled():
            if not self._disabled_logged:
                self._disabled_logged = True
                LOG.info(
                    "[ syncplay/pulse ] %+.0fms left: the item cannot take a tempo now",
                    residual,
                )
            return

        fitted = self._fit_to_item(seconds)

        if fitted is None:
            LOG.info(
                "[ syncplay/pulse ] %+.0fms left: too close to the end for a pulse",
                residual,
            )
            return

        schedule = pulse_schedule(rate, fitted)
        first = schedule[0][1]

        if not self._set(first, now):
            self._write_off("%.3fx refused" % first)
            return

        self._awaiting = {
            "kind": "start",
            "rate": first,
            "asked": now,
            "deadline": now + APPLY_TIMEOUT_S,
            "plan": (rate, fitted, residual, schedule),
        }

    def _check_awaiting(self, current, now):
        """A write is out: has the player taken it yet?"""
        waiting = self._awaiting
        assert waiting is not None  # only called awaiting (_tick)
        confirmed = current is not None and abs(current - waiting["rate"]) < RATE_MATCH

        if not confirmed and now < waiting["deadline"]:
            return

        self._awaiting = None

        if waiting["kind"] == "start":
            self._pulse_started(waiting, confirmed, now)
        else:
            self._pulse_ended(waiting, confirmed, now)

    def _pulse_started(self, waiting, confirmed, now):
        rate, seconds, residual, schedule = waiting["plan"]

        if not confirmed:
            # The player took the call and never played the rate. Do not leave
            # it asked for, and stop treating the item as one that can be
            # nudged.
            self._return_to_one(now)
            self._write_off(
                "%.3fx not applied within %.0fs" % (waiting["rate"], APPLY_TIMEOUT_S)
            )
            return

        LOG.info(
            "[ syncplay/pulse ] %+.0fms: %.3fx for %.1fs%s (applied after %.0fms)",
            residual,
            rate,
            seconds,
            " ramped" if len(schedule) > 2 else "",
            (now - waiting["asked"]) * 1000.0,
        )
        self._pulse = {
            "rate": rate,
            "seconds": seconds,
            "residual": residual,
            "decided_at": waiting["asked"],
            # Where the player clock stood when the rate took: what the pulse
            # moved is read against it once the pulse has played out.
            "start": (now, self.controller._position_ms()),
            # The remaining writes, at absolute times; the last one is 1.0.
            "writes": [(now + offset, value) for offset, value in schedule[1:]],
        }

    def _changed_elsewhere(self, current, now):
        """Whether the tempo read is not the one kofin last asked for, and the
        player has had time to take the ask."""
        return (
            abs(current - self._asked) >= RATE_MATCH
            and now - self._asked_at >= CONFIRM_GRACE_S
        )

    def _advance_pulse(self, current, now):
        """Write whatever the schedule has due. Several overdue at once — a
        late tick — collapse to the latest; the final 1.0 ends the pulse."""
        pulse = self._pulse
        assert pulse is not None  # only called pulsing (_tick)

        if current is not None and self._changed_elsewhere(current, now):
            if abs(current - 1.0) < RATE_MATCH:
                self._ended_by_kodi(pulse, now)
            else:
                self._foreign(current, now)
            return

        writes = pulse["writes"]
        due = None

        while writes and now >= writes[0][0]:
            due = writes.pop(0)

        if due is None:
            return

        if not writes:
            self._end_pulse(now)
        elif not self._set(due[1], now):
            self._cut("a refused step", now)

    def _end_pulse(self, now):
        pulse = self._pulse
        self._pulse = None

        if not self._return_to_one(now):
            LOG.warning("[ syncplay/pulse ] return to 1.0x refused; owed")
            self._settle(pulse, now, measured=False)
            return

        self._awaiting = {
            "kind": "end",
            "rate": 1.0,
            "asked": now,
            "deadline": now + APPLY_TIMEOUT_S,
            "pulse": pulse,
        }

    def _pulse_ended(self, waiting, confirmed, now):
        pulse = waiting["pulse"]

        if not confirmed:
            LOG.warning("[ syncplay/pulse ] return to 1.0x not confirmed")

        if self._live and pulse["rate"] > 1.0:
            self._edge_pulses = 0  # fed to the end: not the edge

        self._settle(pulse, now, measured=confirmed)

    def _ended_by_kodi(self, pulse, now):
        """Kodi put the tempo back to 1.0 during the pulse. On a live stream
        playing faster than its source that is the live edge (kontell/xbmc
        #25: half a second with nothing to read)."""
        self._pulse = None
        self._back_at_one(now)

        if self._live and pulse["rate"] > 1.0:
            self._edge_pulses += 1
            LOG.info(
                "[ syncplay/pulse ] Kodi returned to 1.0x at the live edge (%d of %d)",
                self._edge_pulses,
                utils.LIVE_EDGE_PULSES,
            )

            if self._edge_pulses >= utils.LIVE_EDGE_PULSES:
                self._give_up_edge()
        else:
            LOG.info("[ syncplay/pulse ] Kodi returned to 1.0x; pulse over")

        self._settle(pulse, now, measured=False)

    def _foreign(self, current, now):
        """A tempo kofin did not ask for — a viewer's play speed in the OSD.
        A group's speed is 1.0: put it back."""
        if not self._foreign_logged:
            self._foreign_logged = True
            LOG.info(
                "[ syncplay/tempo ] %.3fx set outside fine sync; back to 1.0x", current
            )

        pulse = self._pulse
        self._pulse = None
        self._awaiting = None
        self._foreign_since = None
        self._return_to_one(now)
        self._settle(pulse, now, measured=False)

    def _settle(self, pulse, now, measured):
        """A pulse is over: a quiet window before measuring again, and its
        outcome read once the window has passed."""
        profile = self.profile
        self._settle_until = now + (profile.settle_s if profile else SETTLE_S)
        self._window = []
        self._measure = pulse if measured else None

        if pulse is not None:
            self._remember(pulse, now)

    def _report_moved(self, now):
        """What the pulse moved, read off the player clock: the change in
        position less the time that passed. Logged against what it was asked
        to move, which is the actuator's accuracy."""
        pulse = self._measure
        assert pulse is not None  # only called measuring (_tick)
        self._measure = None
        started, start_ms = pulse["start"]
        moved = (self.controller._position_ms() - start_ms) - (now - started) * 1000.0
        LOG.info(
            "[ syncplay/pulse ] moved %+.0fms (wanted %+.0fms)",
            moved,
            (pulse["rate"] - 1.0) * pulse["seconds"] * 1000.0,
        )

    def _give_up_edge(self):
        self._gave_up = True
        LOG.warning(
            "[ syncplay/pulse ] giving up: at the live edge — Kodi ended %d forward "
            "pulses in a row; this member follows the feed",
            self._edge_pulses,
        )

    def _cut(self, reason, now):
        """Under the lock: no pulse runs on. Returns whether one was."""
        pulse = self._pulse
        waiting = self._awaiting
        self._pulse = None
        self._awaiting = None
        self._measure = None

        if pulse is None and waiting is None:
            return False

        self._return_to_one(now)
        LOG.info("[ syncplay/pulse ] cut by %s", reason)
        profile = self.profile
        self._settle_until = now + (profile.settle_s if profile else SETTLE_S)

        if pulse is not None:
            self._remember(pulse, now)

        return True

    def cancel(self, reason):
        """A command or seek is about to act: no pulse may run across it.

        Returns at once — it asks for 1.0 and waits for nothing; a paused
        player refuses it, and the 1.0 is owed to the resume. It is also a
        fresh start for the give-up test (that is about the residual regrowing
        between pulses on its own, and a command moves the position by hand)
        and for a member that had given up: a real rate mismatch will earn the
        verdict again within three pulses, and the user is told only once per
        group either way.
        """
        with self._lock:
            self._window = []
            self._history = []
            self._gave_up = False
            self._cut(reason, time.time())

    def before_seek(self):
        """Back to 1.0 before a seek lands: no pulse runs across a seek. Kodi
        keeps a tempo across one, and the speed message and the seek take the
        player's one queue in order, so nothing has to be waited for."""
        self.cancel("seek")

    def note_settle(self):
        """A seek or resume just landed: measure again only once it has played
        out — and, as with a command, regrowth is counted from here, not
        across the seek."""
        with self._lock:
            self._window = []
            self._history = []
            self._measure = None
            profile = self.profile
            self._settle_until = max(
                self._settle_until,
                time.time() + (profile.settle_s if profile else SETTLE_S),
            )

    # ------------------------------------------------------------------
    # Giving up
    # ------------------------------------------------------------------

    def _remember(self, pulse, ended_at):
        """Fold a finished pulse into the give-up history."""
        direction = 1 if pulse["rate"] > 1.0 else -1
        previous = self._history[-1] if self._history else None
        elapsed = (
            pulse["decided_at"] - previous["ended_at"] if previous is not None else 0.0
        )
        self._history.append(
            {
                "direction": direction,
                "ppm": regrowth_ppm(pulse["residual"], elapsed) if previous else 0.0,
                "ended_at": ended_at,
            }
        )
        del self._history[:-GIVEUP_PULSES]

    def _losing(self, residual, now):
        """Whether the residual is a rate mismatch pulses cannot keep up with.

        True once GIVEUP_PULSES pulses have gone the same way and every one of
        them, this candidate included, found the residual regrown at a
        steady, plausible rate above GIVEUP_PPM since the previous pulse
        ended.
        """
        if len(self._history) < GIVEUP_PULSES:
            return False

        direction = 1 if residual > 0 else -1
        last = self._history[-1]
        rates = [entry["ppm"] for entry in self._history[1:]] + [
            regrowth_ppm(residual, now - last["ended_at"])
        ]

        if not all(entry["direction"] == direction for entry in self._history):
            return False

        magnitudes = [abs(rate) for rate in rates]

        return (
            all((rate > 0) == (direction > 0) for rate in rates)
            and min(magnitudes) > GIVEUP_PPM
            and max(magnitudes) < GIVEUP_PPM_CEILING
            and max(magnitudes) <= GIVEUP_SPREAD * min(magnitudes)
        )

    def _give_up(self):
        self._gave_up = True
        LOG.warning(
            "[ syncplay/pulse ] giving up: the residual regrows faster than "
            "%.0f ppm across %s pulses — a rate mismatch, not drift. Is "
            "'Sync playback to display' on?",
            GIVEUP_PPM,
            GIVEUP_PULSES,
        )
        self.controller.manager.notify_rate_mismatch()


#################################################################################################


class TempoSession(object):
    """Whether this group membership has fine sync: the setting, read at join
    and again when it changes.

    There is nothing to install and nothing to undo. The actuator is the
    player's own, and an item that cannot take a tempo says so through
    ``Player.TempoEnabled`` when it plays (:class:`PulseScheduler`).
    """

    def __init__(self):
        self.active = False

    def begin(self):
        """Arm fine sync for the group just joined, when the setting allows."""
        if self.active:
            return

        if not settings.get_bool("syncPlayTempo"):
            LOG.info("[ syncplay/tempo ] fine sync is off in settings")
            return

        self.active = True
        LOG.info("[ syncplay/tempo ] fine sync armed (Kodi's tempo)")

    def end(self):
        if not self.active:
            return

        self.active = False
        LOG.info("[ syncplay/tempo ] fine sync disarmed")
