"""Fine position sync for SyncPlay video: tempo pulses through inputstream.tempo.

The actuator the drift shakedown could not have. Kodi's own ``Player.SetTempo``
works only while "Sync playback to display" is on, and that setting slaves the
media clock to the panel — ``docs/syncplay-drift-shakedown.md`` §10 measured
rate errors up to 4.3 % from it. inputstream.tempo rate-shifts the stream
*inside the demuxer* instead: audio goes through ``atempo`` and is stamped at
output rate, video and subtitle packets are projected through the same
content↔output map, and Kodi's clock keeps following its audio sink. So it
works with the display clock **off**, where the same three devices free-run
within a few hundred ppm of real time. ``inputstream.tempo/docs/tempo-for-video``
is the design; its rig results are in that repository's ``tests/live/results``.

Two halves here:

* :class:`TempoSession` — at group join, decide whether this member can use the
  actuator (setting on, add-on installed and enabled, passthrough guard),
  publish the per-session tempo file for the play route to stamp on every
  direct-play video item, and on Kodi 22 shorten the player's queue so a pulse
  is heard in ~2 s instead of ~5. Everything is undone at leave, and a queue
  left shortened by a crash is restored at the next service start.
* :class:`PulseScheduler` — between group commands, measure the residual
  against the group estimate and close it with a *pulse*: a rate ``r`` held
  for ``T`` seconds displaces the position by ``(r − 1) × T``. One pulse, then a
  quiet window of the queue depth plus a second before the residual is trusted
  again — a pulse is not audible until the packets already queued have played,
  and the reported position is corrected for exactly that queue (the add-on's
  ``queue_secs``), so measuring inside the window would chase the readout.

This is not the continuous ladder that was withdrawn. That one ran a
per-second loop against an actuator whose precondition created the error; this
one issues bounded, confirmed pulses against an actuator measured to move the
clock with that precondition off, and stops — and says so — when the residual
keeps regrowing faster than pulses can reasonably close it.

The planner and the state-file parser are Kodi-free so the arithmetic is unit
testable; the scheduler takes its player reads through the controller.
"""

import json
import math
import os
import statistics
import threading
import time

import xbmcvfs

from kofin.core import kodirpc, settings, state
from kofin.core.log import Logger
from kofin.syncplay import utils

#################################################################################################

LOG = Logger(__name__)

ADDON_ID = "inputstream.tempo"
# The add-on's x.4.1 (21.4.1 Omega, 22.4.1 Piers) keeps the first packets after
# a seek; x.4.0 freed them, which a Pixel 7's AV1 decoder could not take. The
# major is the Kodi version, so the floor is on minor.patch.
ADDON_MIN_PATCH = "x.4.1"
# kofin's own file, never koshelf's special://temp/inputstream_tempo: an
# audiobook and a SyncPlay session would otherwise write over each other.
TEMPO_FILE = "special://temp/kofin_syncplay_tempo"

QUEUE_SETTING = "videoplayer.queuetimesize"  # Kodi 22 only, tenths of a second
SHORT_QUEUE_TENTHS = 10
QUEUE_RESTORE_SETTING = "syncPlayQueueRestore"  # hidden: the value to put back
OMEGA_QUEUE_SECS = 8.0  # Kodi 21 hard-codes it

# Pulse planning. A pulse aims to last PULSE_AIM_S, so the rate scales with the
# residual between RATE_MIN and the user's ceiling (syncPlayMaxRate, default
# 25 %), and the duration stretches only once the rate is capped. Rates above
# RAMP_STEP are ramped in and out in RAMP_STEP increments every RAMP_DT — not
# seamless, just less of a jolt; the viewer may well notice the fast-forward.
RATE_MIN = 0.005
RATE_MAX_DEFAULT = 0.25
RATE_MAX_CEILING = 0.25
PULSE_AIM_S = 5.0
PULSE_MAX_S = 10.0
RAMP_STEP = 0.05
# Learned actuation gain: how far a pulse actually moves the content against
# how far it was asked to. A direct route measures 1.00. A segmented one
# (a transcode, HLS) overshoots — seven consecutive live pulses landed 8-24 %
# long and none short, because the rate stays in force past the scheduled
# window. Learning it rather than tabulating it keeps one rig's constant out
# of the code and costs nothing where the gain is already 1.
GAIN_ALPHA = 0.34
GAIN_MIN = 0.5
GAIN_MAX = 2.0
GAIN_MIN_ASK_MS = 150.0  # below this the measurement is mostly noise
RAMP_DT = 0.25
# Below this the residual is left alone. One frame at 24 fps is 42 ms, and the
# position reads jitter by about that much on an Android box: at 50 ms the Tab
# pulsed ±50 ms against its own read noise, so the band sits above it.
DEADBAND_MS = 75.0
# Above the budget (syncPlayPulseBudget, default 2.5 s) a seek closes the
# residual instead; a skip is for gross errors only, because it is both jarring
# and, on Android, inaccurate. At 25 % a 2.5 s residual is a 10 s pulse.
BUDGET_DEFAULT_MS = 2500.0
SEEK_BLACKOUT_S = 30.0
# What a seek aimed at the group's current position leaves behind before it
# has been measured on a device: restart time plus landing error.
SEEK_LAG_DEFAULT_MS = 500.0
# Residual samples the decision is taken over, at the loop's 250 ms: 3 s.
WINDOW_SAMPLES = 12
# After a pulse ends (or any seek/resume): queue depth + this before measuring.
SETTLE_EXTRA_S = 1.0
# A live item logs its reading on the source clock this often (debug).
LIVE_LOG_INTERVAL_S = 30.0
# How long the add-on gets to confirm a write in its state file. Its poll is
# 250 ms, but the poll runs in DemuxRead, which only runs as the queue drains.
APPLY_TIMEOUT_S = 3.0
APPLY_POLL_S = 0.05
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


def plan_pulse(residual_ms, rate_max=RATE_MAX_DEFAULT):
    """The (rate, seconds) pulse that closes ``residual_ms``, or None.

    Positive residual means this member is behind the group, so the rate is
    above 1. None inside the deadband. The caller checks the budget first;
    here a residual past it simply saturates at ``rate_max`` for PULSE_MAX_S.
    """
    magnitude = abs(residual_ms)

    if magnitude <= DEADBAND_MS:
        return None

    ceiling = min(RATE_MAX_CEILING, max(RATE_MIN, rate_max))
    step = min(ceiling, max(RATE_MIN, magnitude / (PULSE_AIM_S * 1000.0)))
    seconds = min(PULSE_MAX_S, magnitude / (step * 1000.0))
    rate = 1.0 + step if residual_ms > 0 else 1.0 - step
    return round(rate, 4), seconds


def pulse_schedule(rate, seconds):
    """The tempo-file writes that make up one pulse: (offset_s, rate) pairs,
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

    intermediate = [round(1.0 + sign * RAMP_STEP * k, 4) for k in range(1, steps)]
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


def parse_state(text):
    """The add-on's ``<tempo_file>.state`` line as a dict, or None.

    One JSON object per write: seq, event (anchor/retarget/tempo), tempo,
    content_ms, output_ms, delta_ms, queue_secs, video, and since 22.4.6 /
    21.4.6 source_ms and player_ms (see source_offset_ms).
    """
    if not text:
        return None

    try:
        parsed = json.loads(text.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None

    return parsed if isinstance(parsed, dict) and "seq" in parsed else None


def head_delta(state_line):
    """Δ = content − output at the demux head, from a state line.

    Not its ``delta_ms``: that is the Δ the add-on last *reported*, which sits
    a queue depth behind the head, so read at the end of a pulse it is short by
    (r − 1) × queue — measured at 15 % on a 1 s queue. The two counters are the
    head itself.
    """
    content = float(state_line.get("content_ms") or -1.0)
    output = float(state_line.get("output_ms") or -1.0)

    if content < 0 or output < 0:
        return float(state_line.get("delta_ms") or 0.0)

    return content - output


def source_offset_ms(state_line):
    """What to add to the player clock to read the source's own clock, from a
    state line — or None when the add-on has not reported it (an older
    add-on, or a pipeline not yet anchored).

    ``source_ms`` is the demux head on the container's clock (a broadcast's
    PTS) and ``player_ms`` what Kodi's player clock reads for that same
    head, so the difference is the constant that maps one onto the other; it
    shifts only by what a pulse moves, and the scheduler never measures
    across a pulse. A clock that began less than a minute ago is not a
    broadcast's but a transcode job's, and is refused: two members on two
    jobs would each be sure of a position the other never had.
    """
    if not state_line:
        return None

    try:
        source = float(state_line.get("source_ms"))
        player = float(state_line.get("player_ms"))
        content = float(state_line.get("content_ms"))
    except (TypeError, ValueError):
        return None

    if source < 0 or player < 0 or content < 0:
        return None

    if source - content < utils.LIVE_CLOCK_MIN_START_S * 1000.0:
        # The stream's clock began seconds ago: a transcode job's own
        # timeline, shared with nobody (utils.LIVE_CLOCK_MIN_START_S).
        return None

    return source - player


def addon_is_recent(version):
    """Whether an inputstream.tempo version is at least x.4.1 on its channel."""
    try:
        parts = [int(part) for part in str(version).split(".")[:3]]
    except ValueError:
        return False

    return len(parts) == 3 and (parts[1], parts[2]) >= (4, 1)


def regrowth_ppm(residual_ms, elapsed_s):
    """How fast the residual came back, as a rate error in ppm."""
    if elapsed_s <= 0:
        return 0.0

    return residual_ms / elapsed_s * 1000.0


#################################################################################################


class TempoFile(object):
    """The tempo file the add-on polls, and the state file it answers with.

    The first line is the rate. A second line, ``queue_secs=<s>``, tells the
    add-on the depth of Kodi's demux queue for this play, which is where it
    reads the position it reports (the OSD, getTime()) relative to its head:
    a stream whose properties did not carry ``queue_secs`` (a PVR add-on's
    cannot know a setting a kofin session shortens) would otherwise report
    a pulse's shift up to 8 s late. Add-ons before 22.4.6 / 21.4.6 read the
    rate and ignore the rest.
    """

    def __init__(self, path, queue_secs=None):
        self.path = path
        self.state_path = path + ".state"
        self.queue_secs = queue_secs

    def write(self, rate):
        """Replace the file atomically: the add-on may read at any instant."""
        tmp = self.path + ".tmp"

        with open(tmp, "w") as handle:
            handle.write("%.4f\n" % rate)

            if self.queue_secs:
                handle.write("queue_secs=%.2f\n" % self.queue_secs)

        os.replace(tmp, self.path)

    def reset(self):
        """1.0, and no stale answer from a previous playback."""
        self.write(1.0)

        try:
            os.remove(self.state_path)
        except OSError:
            pass

    def read_state(self):
        try:
            with open(self.state_path) as handle:
                return parse_state(handle.read())
        except OSError:
            return None

    def current_seq(self):
        current = self.read_state()
        return int(current.get("seq") or 0) if current else 0

    def wait_applied(self, rate, after_seq, timeout_s=APPLY_TIMEOUT_S):
        """The state line confirming ``rate`` landed, or None on timeout.

        Confirmation is a state write with a later seq carrying the rate: the
        add-on writes one for every applied change, anchor and re-target.
        """
        deadline = time.time() + timeout_s

        while True:
            current = self.read_state()

            if (
                current
                and int(current.get("seq") or 0) > after_seq
                and abs(float(current.get("tempo") or 0.0) - rate) < 0.0015
            ):
                return current

            if time.time() >= deadline:
                return None

            time.sleep(APPLY_POLL_S)


#################################################################################################


class PulseScheduler(object):
    """Closes the residual between commands with confirmed tempo pulses.

    Driven by the controller's 250 ms loop through :meth:`tick`; told about
    every command and seek so a pulse is never left running across one.

    Nothing here blocks under ``_lock``. A write to the tempo file is
    confirmed by a later tick finding the add-on's state line, not by
    sleeping on it: ``cancel()`` and ``before_seek()`` take the same lock on
    the command thread, and a scheduled Pause must not wait on the actuator.
    The one corrective seek goes through the controller with the lock
    dropped, because the command path holds the player lock before it asks
    for this one (``_seek_and_settle`` → ``before_seek``).
    """

    def __init__(self, controller):
        self.controller = controller
        self.file = None  # TempoFile once the playing item is routed
        self.queue_secs = OMEGA_QUEUE_SECS
        self.budget_ms = BUDGET_DEFAULT_MS
        self.rate_max = RATE_MAX_DEFAULT
        self._session_id = None  # PlaySessionId the scheduler is armed for
        self._window = []
        self._pulse = None  # the running pulse: its remaining writes
        self._awaiting = None  # a write waiting for the add-on's state line
        self._settle_until = 0.0
        self._seek_blackout_until = 0.0
        self._history = []  # (direction, regrowth ppm) per pulse
        self._gain = 1.0  # measured displacement / displacement asked for
        self._gave_up = False
        self._unrouted_logged = False
        # A live item (utils.claim_is_live): never seeked, and a forward
        # pulse the feed cannot supply ends fine sync (the live edge).
        self._live = False
        self._edge_pulses = 0
        self._apply_misses = 0
        self._live_logged_at = 0.0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Arming
    # ------------------------------------------------------------------

    def _arm(self, claim):
        """Bind to the playing item's route, or note that it has none."""
        self._session_id = claim.get("PlaySessionId")
        self._window = []
        self._pulse = None
        self._awaiting = None
        self._history = []
        self._gave_up = False
        self._live = utils.claim_is_live(claim)
        self._edge_pulses = 0
        self._apply_misses = 0
        # The gain is deliberately NOT reset here. It is a property of the
        # transport, not of the item, and a transcode re-arms on every reload
        # — which a group seek forces, since the stream restarts with a new
        # PlaySessionId. Resetting on re-arm threw the measurement away on
        # exactly the route that needs it: measured live, the gain never got
        # past 1.05 while pulses kept landing 14 % long. If the transport
        # really does change, the EMA re-converges within about three pulses.
        route = claim.get("Tempo") or {}
        path = route.get("File")

        if not path:
            self.file = None

            if not self._unrouted_logged:
                self._unrouted_logged = True
                LOG.info(
                    "[ syncplay/tempo ] %s is not routed through %s; "
                    "command-only sync for this item",
                    claim.get("Id"),
                    ADDON_ID,
                )

            return

        self.queue_secs = float(route.get("QueueSecs") or OMEGA_QUEUE_SECS)
        self.file = TempoFile(path, queue_secs=self.queue_secs)
        self.budget_ms = float(
            settings.get_int("syncPlayPulseBudget") or BUDGET_DEFAULT_MS
        )
        self.rate_max = (
            settings.get_int("syncPlayMaxRate") or RATE_MAX_DEFAULT * 100
        ) / 100.0
        self._unrouted_logged = False
        LOG.info(
            "[ syncplay/tempo ] fine sync armed for %s "
            "(queue %.1fs, budget %.0fms, up to %.0f%%%s)",
            claim.get("Id"),
            self.queue_secs,
            self.budget_ms,
            self.rate_max * 100.0,
            ", live: on the source clock, pulses only" if self._live else "",
        )

    def can_close(self, residual_ms):
        """Whether fine sync will take this residual, so a seek is not needed.

        A live item has no seek to fall back on, so every residual is fine
        sync's — saturated pulses close a large one over time."""
        if self.file is None or self._gave_up:
            return False

        return self._live or abs(residual_ms) <= self.budget_ms

    def reset(self):
        with self._lock:
            self.cancel("stop")
            self.file = None
            self._session_id = None
            self._window = []
            self._history = []
            self._gain = 1.0
            self._gave_up = False

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
        claim = self.controller.manager.current_claim()

        if not claim:
            return None

        if claim.get("PlaySessionId") != self._session_id:
            self._arm(claim)

        if self.file is None or self._gave_up:
            return None

        now = time.time()

        if self._awaiting is not None:
            self._check_awaiting(now)
            return None

        if self._pulse is not None:
            self._advance_pulse(now)
            return None

        controller = self.controller

        if controller._is_paused() or not controller.reference_is_playing():
            self._window = []
            return None

        if now < self._settle_until:
            return None

        estimate = controller.estimate_position_ms()

        if estimate is None:
            return None

        if self._live and not controller.live_on_source_clock():
            # The group is anchored on session time (a proposer without
            # the clock) or this member cannot read its own: nothing to
            # converge on — the commands keep it tuned together (P2).
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

        # Beyond the budget a seek is the tool — once, for gross errors, and
        # only when every sample in the window agrees. Otherwise — the seek's
        # blackout holds, or the residual straddles the budget — pulses close
        # it after all, saturated at the rate ceiling for PULSE_MAX_S. The
        # Bravia sat at +2.5 s for 25 s after a resume when a median beyond
        # the budget and a window not all beyond it meant neither.
        # A live stream has no seek (ffmpeg's HLS demuxer refuses one on a
        # live playlist), so a live residual beyond the budget takes the
        # saturated pulses instead, as many as it needs.
        if (
            abs(residual) > self.budget_ms
            and not self._live
            and now >= self._seek_blackout_until
            and all(abs(sample) > self.budget_ms for sample in self._window)
        ):
            self._seek_blackout_until = now + SEEK_BLACKOUT_S
            self._window = []
            LOG.info(
                "[ syncplay/align ] %+.0fms is beyond the pulse budget: seeking",
                residual,
            )
            return residual

        # Ask for what will land on the residual, not for the residual: an
        # actuator that overshoots by 15 % is corrected by asking for 15 %
        # less. The give-up history keeps the true residual.
        plan = plan_pulse(residual / self._gain, self.rate_max)

        if plan is None:
            return None

        if self._losing(residual, now):
            self._give_up()
            return None

        self._start_pulse(plan[0], plan[1], residual, residual / self._gain)
        return None

    # ------------------------------------------------------------------
    # Pulses
    # ------------------------------------------------------------------

    def _start_pulse(self, rate, seconds, residual, ask_ms=None):
        tempo_file = self.file
        assert tempo_file is not None  # only called armed (_tick)
        schedule = pulse_schedule(rate, seconds)
        first = schedule[0][1]
        asked = time.time()
        self._awaiting = {
            "kind": "start",
            "rate": first,
            "after_seq": tempo_file.current_seq(),
            "asked": asked,
            "deadline": asked + APPLY_TIMEOUT_S,
            "plan": (
                rate,
                seconds,
                residual,
                schedule,
                residual if ask_ms is None else ask_ms,
            ),
        }
        tempo_file.write(first)
        self._window = []

    def _check_awaiting(self, now):
        """A write is out: has the add-on's state line confirmed it yet?"""
        waiting = self._awaiting
        tempo_file = self.file
        assert waiting is not None and tempo_file is not None  # armed, awaiting
        line = tempo_file.read_state()
        confirmed = (
            line
            and int(line.get("seq") or 0) > waiting["after_seq"]
            and abs(float(line.get("tempo") or 0.0) - waiting["rate"]) < 0.0015
        )

        if not confirmed and now < waiting["deadline"]:
            return

        self._awaiting = None

        if waiting["kind"] == "start":
            self._pulse_started(waiting, line if confirmed else None, now)
        else:
            self._pulse_ended(waiting, line if confirmed else None, now)

    def _pulse_started(self, waiting, applied, now):
        tempo_file = self.file
        assert tempo_file is not None  # only called armed (_check_awaiting)
        rate, seconds, residual, schedule, ask_ms = waiting["plan"]

        if applied is None:
            # Nothing answered: the playback is not going through the add-on
            # after all (or it is wedged). Either way do not leave a rate on
            # the file, and stop treating the item as routed — except a live
            # item at its edge, whose demuxer polls only as segments arrive:
            # that one gets a few more tries before it is written off.
            tempo_file.write(1.0)

            if self._live and self._apply_misses < utils.LIVE_APPLY_MISSES:
                self._apply_misses += 1
                self._settle_until = now + self.queue_secs + SETTLE_EXTRA_S
                LOG.warning(
                    "[ syncplay/pulse ] %.3fx not applied within %.0fs; "
                    "live item, retrying (%d of %d)",
                    waiting["rate"],
                    APPLY_TIMEOUT_S,
                    self._apply_misses,
                    utils.LIVE_APPLY_MISSES,
                )
                return

            LOG.warning(
                "[ syncplay/pulse ] %.3fx not applied within %.0fs; "
                "command-only sync for this item",
                waiting["rate"],
                APPLY_TIMEOUT_S,
            )
            self.file = None
            return

        self._apply_misses = 0

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
            "ask_ms": ask_ms,
            "decided_at": waiting["asked"],
            "start_delta": head_delta(applied),
            # The remaining writes, at absolute times; the last one is 1.0.
            "writes": [(now + offset, value) for offset, value in schedule[1:]],
        }

    def _advance_pulse(self, now):
        """Write whatever the schedule has due. Several overdue at once — a
        late tick — collapse to the latest, which is all the add-on's 250 ms
        poll would see anyway; the final 1.0 ends the pulse."""
        pulse = self._pulse
        tempo_file = self.file
        assert pulse is not None and tempo_file is not None  # armed, pulsing
        writes = pulse["writes"]
        due = None

        while writes and now >= writes[0][0]:
            due = writes.pop(0)

        if due is None:
            return

        if not writes:
            self._end_pulse(now)
        else:
            tempo_file.write(due[1])

    def _end_pulse(self, now):
        tempo_file = self.file
        assert tempo_file is not None  # only called armed (_advance_pulse)
        pulse = self._pulse
        self._pulse = None
        self._awaiting = {
            "kind": "end",
            "rate": 1.0,
            "after_seq": tempo_file.current_seq(),
            "asked": now,
            "deadline": now + APPLY_TIMEOUT_S,
            "pulse": pulse,
        }
        tempo_file.write(1.0)

    def _pulse_ended(self, waiting, landed, now):
        pulse = waiting["pulse"]

        if landed is None:
            LOG.warning("[ syncplay/pulse ] return to 1.0x not confirmed")
        else:
            # Δ = content − output moves only while the rate is off 1.0, so the
            # difference between the two confirmed state lines is exactly the
            # displacement the pulse produced — the add-on's own account of it.
            moved = head_delta(landed) - pulse["start_delta"]
            starved = self._starved(pulse, moved)

            if not starved:
                self._learn_gain(pulse, moved)

            LOG.info(
                "[ syncplay/pulse ] moved %+.0fms (wanted %+.0fms, gain %.2f)%s",
                moved,
                pulse["residual"],
                self._gain,
                " starved: at the live edge" if starved else "",
            )

            if starved:
                self._edge_pulses += 1

                if self._edge_pulses >= utils.LIVE_EDGE_PULSES:
                    self._give_up_edge()
            else:
                self._edge_pulses = 0

        self._settle_until = now + self.queue_secs + SETTLE_EXTRA_S
        self._window = []
        self._remember(pulse, now)

    def _starved(self, pulse, moved):
        """A forward pulse on a live item that moved well short of its ask:
        the demuxer ran out of feed, which is the live edge. A pulse back
        is never starved — the buffer behind is the stream's own."""
        if not self._live or pulse["rate"] <= 1.0:
            return False

        asked = abs(self._asked_ms(pulse))
        return (
            asked >= GAIN_MIN_ASK_MS and moved < utils.LIVE_EDGE_MOVE_FRACTION * asked
        )

    def _give_up_edge(self):
        self._gave_up = True
        LOG.warning(
            "[ syncplay/pulse ] giving up: at the live edge — %d forward pulses "
            "in a row were starved; this member follows the feed",
            self._edge_pulses,
        )

    @staticmethod
    def _deliverable_ms(pulse):
        """What the pulse could move at most: its rate over its length. A
        saturated pulse (the rate ceiling for PULSE_MAX_S) asks for the whole
        residual and delivers this much of it — the measure a saturated
        pulse's outcome is judged against."""
        return (pulse["rate"] - 1.0) * pulse["seconds"] * 1000.0

    def _asked_ms(self, pulse):
        asked = pulse.get("ask_ms") or pulse["residual"]
        deliverable = self._deliverable_ms(pulse)

        if abs(deliverable) < abs(asked):
            return deliverable

        return asked

    def _learn_gain(self, pulse, moved):
        """Fold one pulse's outcome into the actuation gain.

        Compared against what the actuator was *asked* for, not against the
        residual — those differ once the gain is off 1, and comparing with the
        residual would make the correction chase its own tail — and capped at
        what the pulse could deliver, or a saturated pulse would read as a
        weak actuator. Small asks are skipped: at 80ms the confirmation
        quantisation is a large fraction of the measurement.
        """
        asked = self._asked_ms(pulse)

        if abs(asked) < GAIN_MIN_ASK_MS or moved * asked <= 0:
            return

        observed = moved / asked
        self._gain = min(
            GAIN_MAX,
            max(GAIN_MIN, (1.0 - GAIN_ALPHA) * self._gain + GAIN_ALPHA * observed),
        )

    def cancel(self, reason):
        """A command or seek is about to act: no pulse may run across it.

        Returns at once — it writes 1.0 and waits for nothing. It is also a
        fresh start for the give-up test (that is about the residual
        regrowing between pulses on its own, and a command moves the position
        by hand) and for a member that had given up: a real rate mismatch
        will earn the verdict again within three pulses, and the user is told
        only once per group either way.
        """
        with self._lock:
            self._window = []
            self._history = []
            self._gave_up = False
            pulse = self._pulse
            waiting = self._awaiting
            self._pulse = None
            self._awaiting = None

            if pulse is None and waiting is None:
                return

            if self.file is not None:
                self.file.write(1.0)

            LOG.info("[ syncplay/pulse ] cut by %s", reason)
            self._settle_until = time.time() + self.queue_secs + SETTLE_EXTRA_S

            if pulse is not None:
                self._remember(pulse, time.time())

    def before_seek(self):
        """Return to 1.0x, and wait for it, before a seek lands.

        A seek issued while a rate is running lands early: after the flush
        Kodi resyncs to the video's first picture, which sits behind the
        audio by more at 1.03x than at 1.0x (inputstream.tempo results, item
        5). The wait is on the command thread that is about to seek, outside
        the scheduler lock, and bounded.
        """
        with self._lock:
            tempo_file = self.file
            if tempo_file is not None and (
                self._pulse is not None or self._awaiting is not None
            ):
                before = tempo_file.current_seq()
            else:
                tempo_file = None
                before = 0
            self.cancel("seek")

        if tempo_file is not None:
            tempo_file.wait_applied(1.0, before, timeout_s=1.0)

    def note_settle(self):
        """A seek or resume just landed: measure again only once it has played
        through the queue — and, as with a command, regrowth is counted from
        here, not across the seek."""
        with self._lock:
            self._window = []
            self._history = []
            self._settle_until = max(
                self._settle_until, time.time() + self.queue_secs + SETTLE_EXTRA_S
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


def restore_queue(reason=""):
    """Put back a queue size shortened for a session, if one is recorded."""
    saved = settings.get_str(QUEUE_RESTORE_SETTING)

    if not saved:
        return False

    try:
        tenths = int(saved)
    except ValueError:
        settings.set_str(QUEUE_RESTORE_SETTING, "")
        return False

    current = kodirpc.kodi_setting(QUEUE_SETTING)

    try:
        already_back = current is not None and int(current) != SHORT_QUEUE_TENTHS
    except (TypeError, ValueError):
        already_back = False

    if already_back:
        # The value really is back — an earlier restore reached the disk, or
        # the user has set it themselves. Only now is the record safe to drop.
        settings.set_str(QUEUE_RESTORE_SETTING, "")
        return False

    if kodirpc.set_kodi_setting(QUEUE_SETTING, tenths):
        LOG.info(
            "[ syncplay/tempo ] %s restored to %.1fs%s",
            QUEUE_SETTING,
            tenths / 10.0,
            reason,
        )
        # The record is deliberately kept. A JSON-RPC settings write lives in
        # Kodi's memory and only reaches guisettings.xml when Kodi saves, so a
        # Kodi that is killed between the two leaves the shortened value on
        # disk with no record left to undo it — which is how two devices in
        # the shakedown were found stuck at 1.0s, one of them stuttering at
        # every 3s HLS segment boundary because of it. The next start clears
        # the record once it can see the value came back.
        return True

    LOG.warning("[ syncplay/tempo ] could not restore %s to %s", QUEUE_SETTING, saved)
    return False


class TempoSession(object):
    """What a group membership arms, and disarms again."""

    def __init__(self, notify=None):
        self.active = False
        self.queue_secs = None
        self.queue_full_tenths = None
        # Tells the user, once per group, that fine sync wanted the add-on
        # and did not get it — a silent fallback to command-only sync reads
        # as the feature not working.
        self._notify = notify

    def begin(self):
        """Arm fine sync for the group just joined, when this member can."""
        if self.active:
            return

        if not settings.get_bool("syncPlayTempo"):
            LOG.info("[ syncplay/tempo ] fine sync is off in settings")
            return

        details = kodirpc.addon_details(ADDON_ID)

        if not details or not details["enabled"]:
            LOG.info(
                "[ syncplay/tempo ] fine sync unavailable: %s is %s",
                ADDON_ID,
                "disabled" if details else "not installed",
            )
            self._tell()
            return

        if not addon_is_recent(details["version"]):
            LOG.info(
                "[ syncplay/tempo ] fine sync unavailable: %s %s is older than "
                "%s; it drops the first packets after a seek",
                ADDON_ID,
                details["version"],
                ADDON_MIN_PATCH,
            )
            self._tell()
            return

        self.queue_secs = self._shorten_queue()
        path = xbmcvfs.translatePath(TEMPO_FILE)

        try:
            TempoFile(path).reset()
        except OSError as error:
            LOG.warning("[ syncplay/tempo ] cannot write %s: %s", path, error)
            restore_queue()
            return

        state.publish_syncplay_tempo(
            {
                "file": path,
                "queue_secs": self.queue_secs,
                # Both sizes, so the play route can pick per item: a segmented
                # stream cannot live on the shortened one.
                "queue_short_tenths": (
                    SHORT_QUEUE_TENTHS if self.queue_full_tenths else None
                ),
                "queue_full_tenths": self.queue_full_tenths,
            }
        )
        self.active = True
        LOG.info(
            "[ syncplay/tempo ] fine sync armed through %s, queue %.1fs",
            ADDON_ID,
            self.queue_secs,
        )

    def _tell(self):
        if self._notify is not None:
            self._notify(settings.localized(30599))

    def end(self):
        if not self.active:
            return

        self.active = False
        state.clear_syncplay_tempo()

        try:
            TempoFile(xbmcvfs.translatePath(TEMPO_FILE)).write(1.0)
        except OSError:
            pass

        restore_queue()
        LOG.info("[ syncplay/tempo ] fine sync disarmed")

    def _shorten_queue(self):
        """Kodi 22: a 1 s queue while in the group, so a pulse lands in ~2 s.

        Read when the player object is constructed, so it takes effect on the
        next playback — every group play starts after the join. The original
        value is recorded in a hidden setting, which is what a restore after
        a crash reads.

        Also records ``queue_full_tenths``: the value a route that cannot live
        on 1 s has to be given back. Because the queue is read per player
        construction rather than per session, the play route can choose between
        the two for the item it is about to resolve.
        """
        current = kodirpc.kodi_setting(QUEUE_SETTING)

        if current is kodirpc.FAILED:
            # C3: a blip is not "this Kodi lacks the setting". Leave the
            # queue untouched — no shorten, no record — and use the
            # conservative window for this arm only.
            LOG.warning(
                "[ syncplay/tempo ] %s read failed; queue left untouched",
                QUEUE_SETTING,
            )
            self.queue_full_tenths = None
            return OMEGA_QUEUE_SECS

        if current is None:
            self.queue_full_tenths = None  # Kodi 21: fixed queue, not ours
            return OMEGA_QUEUE_SECS

        try:
            tenths = int(current)
        except (TypeError, ValueError):
            self.queue_full_tenths = None
            return OMEGA_QUEUE_SECS

        # The user's own value, which is the record when a previous session
        # left one — the live setting may still be a shortened leftover.
        self.queue_full_tenths = tenths
        try:
            recorded = int(settings.get_str(QUEUE_RESTORE_SETTING) or 0)
        except ValueError:
            recorded = 0
        if recorded > tenths:
            self.queue_full_tenths = recorded

        if not settings.get_bool("syncPlayShortQueue") or tenths <= SHORT_QUEUE_TENTHS:
            return tenths / 10.0

        # The record first, and only if it stuck: a shortening nobody can undo
        # is worse than none. JSON-RPC setting writes are not saved to disk by
        # Kodi (SettingsOperations never calls Save), so the value on disk is
        # whatever Kodi last exited with — the Pixel was found at 1.0 s with
        # no record, a session whose record write had been dropped.
        settings.set_str(QUEUE_RESTORE_SETTING, str(tenths))

        if settings.get_str(QUEUE_RESTORE_SETTING) != str(tenths):
            LOG.warning(
                "[ syncplay/tempo ] queue left alone: restore record not stored"
            )
            return tenths / 10.0

        if kodirpc.set_kodi_setting(QUEUE_SETTING, SHORT_QUEUE_TENTHS):
            LOG.info(
                "[ syncplay/tempo ] %s %.1fs -> %.1fs for the session",
                QUEUE_SETTING,
                tenths / 10.0,
                SHORT_QUEUE_TENTHS / 10.0,
            )
            return SHORT_QUEUE_TENTHS / 10.0

        settings.set_str(QUEUE_RESTORE_SETTING, "")
        return tenths / 10.0
