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
import os
import statistics
import threading
import time

import xbmcvfs

from kofin.core import kodirpc, settings, state
from kofin.core.log import Logger

#################################################################################################

LOG = Logger(__name__)

ADDON_ID = "inputstream.tempo"
# kofin's own file, never koshelf's special://temp/inputstream_tempo: an
# audiobook and a SyncPlay session would otherwise write over each other.
TEMPO_FILE = "special://temp/kofin_syncplay_tempo"

QUEUE_SETTING = "videoplayer.queuetimesize"  # Kodi 22 only, tenths of a second
SHORT_QUEUE_TENTHS = 10
QUEUE_RESTORE_SETTING = "syncPlayQueueRestore"  # hidden: the value to put back
OMEGA_QUEUE_SECS = 8.0  # Kodi 21 hard-codes it

# Pulse planning. A pulse aims to last PULSE_AIM_S, so the rate scales with the
# residual between RATE_MIN and RATE_MAX (atempo's artefact-free band), and the
# duration stretches only once the rate is capped.
RATE_MIN = 0.005
RATE_MAX = 0.03
PULSE_AIM_S = 5.0
PULSE_MAX_S = 10.0
# Below this the residual is left alone. One frame at 24 fps is 42 ms, and the
# position reads jitter by about that much on an Android box: at 50 ms the Tab
# pulsed ±50 ms against its own read noise, so the band sits above it.
DEADBAND_MS = 75.0
# Above this a pulse would take longer than PULSE_MAX_S at RATE_MAX (the
# shakedown's 300 ms budget): seek instead, at rate 1.0.
SEEK_ABOVE_MS = 300.0
SEEK_BLACKOUT_S = 30.0
# What a seek aimed at the group's current position leaves behind before it
# has been measured on a device: restart time plus landing error.
SEEK_LAG_DEFAULT_MS = 500.0
# Residual samples the decision is taken over, at the loop's 250 ms: 3 s.
WINDOW_SAMPLES = 12
# After a pulse ends (or any seek/resume): queue depth + this before measuring.
SETTLE_EXTRA_S = 1.0
# How long the add-on gets to confirm a write in its state file. Its poll is
# 250 ms, but the poll runs in DemuxRead, which only runs as the queue drains.
APPLY_TIMEOUT_S = 3.0
APPLY_POLL_S = 0.05
# Give up when this many consecutive pulses went the same way and the residual
# regrew faster than GIVEUP_PPM between them: a rate mismatch, not jitter. The
# display clock off, the rig's boxes free-run within ±550 ppm.
GIVEUP_PULSES = 3
GIVEUP_PPM = 3000.0

#################################################################################################


def plan_pulse(residual_ms):
    """The (rate, seconds) pulse that closes ``residual_ms``, or None.

    Positive residual means this member is behind the group, so the rate is
    above 1. None inside the deadband. The caller checks SEEK_ABOVE_MS first;
    here a residual past it simply saturates at RATE_MAX for PULSE_MAX_S.
    """
    magnitude = abs(residual_ms)

    if magnitude <= DEADBAND_MS:
        return None

    step = min(RATE_MAX, max(RATE_MIN, magnitude / (PULSE_AIM_S * 1000.0)))
    seconds = min(PULSE_MAX_S, magnitude / (step * 1000.0))
    rate = 1.0 + step if residual_ms > 0 else 1.0 - step
    return round(rate, 4), seconds


def parse_state(text):
    """The add-on's ``<tempo_file>.state`` line as a dict, or None.

    One JSON object per write: seq, event (anchor/retarget/tempo), tempo,
    content_ms, output_ms, delta_ms, queue_secs, video.
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


def regrowth_ppm(residual_ms, elapsed_s):
    """How fast the residual came back, as a rate error in ppm."""
    if elapsed_s <= 0:
        return 0.0

    return residual_ms / elapsed_s * 1000.0


#################################################################################################


class TempoFile(object):
    """The tempo file the add-on polls, and the state file it answers with."""

    def __init__(self, path):
        self.path = path
        self.state_path = path + ".state"

    def write(self, rate):
        """Replace the file atomically: the add-on may read at any instant."""
        tmp = self.path + ".tmp"

        with open(tmp, "w") as handle:
            handle.write("%.4f\n" % rate)

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
    """

    def __init__(self, controller):
        self.controller = controller
        self.file = None  # TempoFile once the playing item is routed
        self.queue_secs = OMEGA_QUEUE_SECS
        self._session_id = None  # PlaySessionId the scheduler is armed for
        self._window = []
        self._pulse = None
        self._settle_until = 0.0
        self._seek_blackout_until = 0.0
        self._history = []  # (direction, regrowth ppm) per pulse
        self._gave_up = False
        self._unrouted_logged = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Arming
    # ------------------------------------------------------------------

    def _arm(self, claim):
        """Bind to the playing item's route, or note that it has none."""
        self._session_id = claim.get("PlaySessionId")
        self._window = []
        self._pulse = None
        self._history = []
        self._gave_up = False
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

        self.file = TempoFile(path)
        self.queue_secs = float(route.get("QueueSecs") or OMEGA_QUEUE_SECS)
        self._unrouted_logged = False
        LOG.info(
            "[ syncplay/tempo ] fine sync armed for %s (queue %.1fs)",
            claim.get("Id"),
            self.queue_secs,
        )

    def reset(self):
        with self._lock:
            self.cancel("stop")
            self.file = None
            self._session_id = None
            self._window = []
            self._history = []
            self._gave_up = False

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def tick(self):
        with self._lock:
            self._tick()

    def _tick(self):
        claim = self.controller.manager.current_claim()

        if not claim:
            return

        if claim.get("PlaySessionId") != self._session_id:
            self._arm(claim)

        if self.file is None or self._gave_up:
            return

        now = time.time()

        if self._pulse is not None:
            if now >= self._pulse["end_at"]:
                self._end_pulse()

            return

        controller = self.controller

        if controller._is_paused() or not controller.reference_is_playing():
            self._window = []
            return

        if now < self._settle_until:
            return

        estimate = controller.estimate_position_ms()

        if estimate is None:
            return

        self._window.append(estimate - controller._position_ms())
        del self._window[:-WINDOW_SAMPLES]

        if len(self._window) < WINDOW_SAMPLES:
            return

        residual = statistics.median(self._window)

        # Beyond the budget a seek is the tool — once. While its blackout
        # holds, a residual the seek left behind (the Tab lands a seek 350 ms
        # early; a first seek on any device carries an unmeasured lag) is
        # closed by pulses after all, saturated at RATE_MAX for PULSE_MAX_S,
        # rather than sat on for 30 s waiting for the next seek.
        if abs(residual) > SEEK_ABOVE_MS and now >= self._seek_blackout_until:
            if all(abs(sample) > SEEK_ABOVE_MS for sample in self._window):
                self._seek(residual)

            return

        plan = plan_pulse(residual)

        if plan is None:
            return

        if self._losing(residual, now):
            self._give_up()
            return

        self._start_pulse(plan[0], plan[1], residual)

    # ------------------------------------------------------------------
    # Pulses
    # ------------------------------------------------------------------

    def _start_pulse(self, rate, seconds, residual):
        before = self.file.current_seq()
        asked = time.time()
        self.file.write(rate)
        applied = self.file.wait_applied(rate, before)

        if applied is None:
            # Nothing answered: the playback is not going through the add-on
            # after all (or it is wedged). Either way do not leave a rate on
            # the file, and stop treating the item as routed.
            self.file.write(1.0)
            LOG.warning(
                "[ syncplay/pulse ] %.3fx not applied within %.0fs; "
                "command-only sync for this item",
                rate,
                APPLY_TIMEOUT_S,
            )
            self.file = None
            return

        dead_ms = (time.time() - asked) * 1000.0
        LOG.info(
            "[ syncplay/pulse ] %+.0fms: %.3fx for %.1fs (applied after %.0fms)",
            residual,
            rate,
            seconds,
            dead_ms,
        )
        self._pulse = {
            "rate": rate,
            "seconds": seconds,
            "residual": residual,
            "decided_at": asked,
            "end_at": time.time() + seconds,
            "start_delta": head_delta(applied),
        }
        self._window = []

    def _end_pulse(self):
        pulse = self._pulse
        self._pulse = None
        before = self.file.current_seq()
        self.file.write(1.0)
        landed = self.file.wait_applied(1.0, before)
        now = time.time()

        if landed is None:
            LOG.warning("[ syncplay/pulse ] return to 1.0x not confirmed")
        else:
            # Δ = content − output moves only while the rate is off 1.0, so the
            # difference between the two confirmed state lines is exactly the
            # displacement the pulse produced — the add-on's own account of it.
            moved = head_delta(landed) - pulse["start_delta"]
            LOG.info(
                "[ syncplay/pulse ] moved %+.0fms (wanted %+.0fms)",
                moved,
                pulse["residual"],
            )

        self._settle_until = now + self.queue_secs + SETTLE_EXTRA_S
        self._window = []
        self._remember(pulse, now)

    def cancel(self, reason):
        """A command or seek is about to act: no pulse may run across it."""
        with self._lock:
            self._window = []

            if self._pulse is None:
                return

            pulse = self._pulse
            self._pulse = None

            if self.file is not None:
                self.file.write(1.0)

            LOG.info("[ syncplay/pulse ] cut by %s", reason)
            self._settle_until = time.time() + self.queue_secs + SETTLE_EXTRA_S
            self._remember(pulse, time.time())

    def before_seek(self):
        """Return to 1.0x and wait for it before a seek lands.

        A seek issued while a rate is running lands early: after the flush
        Kodi resyncs to the video's first picture, which sits behind the
        audio by more at 1.03x than at 1.0x (inputstream.tempo results, item
        5). Cheap to avoid, and rare.
        """
        with self._lock:
            if self._pulse is None or self.file is None:
                return

            before = self.file.current_seq()
            self.cancel("seek")
            self.file.wait_applied(1.0, before, timeout_s=1.0)

    def note_settle(self):
        """A seek or resume just landed: measure again only once it has played
        through the queue."""
        with self._lock:
            self._window = []
            self._settle_until = max(
                self._settle_until, time.time() + self.queue_secs + SETTLE_EXTRA_S
            )

    # ------------------------------------------------------------------
    # Seeking beyond the pulse budget, and giving up
    # ------------------------------------------------------------------

    def _seek(self, residual):
        now = time.time()
        self._seek_blackout_until = now + SEEK_BLACKOUT_S
        self._window = []
        LOG.info(
            "[ syncplay/align ] %+.0fms is beyond the pulse budget: seeking",
            residual,
        )
        self.controller.correct_position()
        self.note_settle()

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
        them, this candidate included, found the residual regrown faster than
        GIVEUP_PPM since the previous pulse ended.
        """
        if len(self._history) < GIVEUP_PULSES:
            return False

        direction = 1 if residual > 0 else -1
        last = self._history[-1]
        rates = [entry["ppm"] for entry in self._history[1:]] + [
            regrowth_ppm(residual, now - last["ended_at"])
        ]

        return all(entry["direction"] == direction for entry in self._history) and all(
            abs(rate) > GIVEUP_PPM and (rate > 0) == (direction > 0) for rate in rates
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


def _queue_secs_in_force():
    """Kodi's audio/video queue depth in seconds, or the Omega constant."""
    tenths = kodirpc.kodi_setting(QUEUE_SETTING)

    if tenths is None:
        return OMEGA_QUEUE_SECS

    try:
        return int(tenths) / 10.0
    except (TypeError, ValueError):
        return OMEGA_QUEUE_SECS


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

    if kodirpc.set_kodi_setting(QUEUE_SETTING, tenths):
        LOG.info(
            "[ syncplay/tempo ] %s restored to %.1fs%s",
            QUEUE_SETTING,
            tenths / 10.0,
            reason,
        )
        settings.set_str(QUEUE_RESTORE_SETTING, "")
        return True

    LOG.warning("[ syncplay/tempo ] could not restore %s to %s", QUEUE_SETTING, saved)
    return False


class TempoSession(object):
    """What a group membership arms, and disarms again."""

    def __init__(self):
        self.active = False
        self.queue_secs = None

    def begin(self):
        """Arm fine sync for the group just joined, when this member can."""
        if self.active:
            return

        if not settings.get_bool("syncPlayTempo"):
            LOG.info("[ syncplay/tempo ] fine sync is off in settings")
            return

        enabled = kodirpc.addon_enabled(ADDON_ID)

        if not enabled:
            LOG.info(
                "[ syncplay/tempo ] fine sync unavailable: %s is %s",
                ADDON_ID,
                "disabled" if enabled is False else "not installed",
            )
            return

        if kodirpc.kodi_setting(
            "audiooutput.passthrough"
        ) is True and not settings.get_bool("syncPlayTempoPassthrough"):
            LOG.info(
                "[ syncplay/tempo ] fine sync off: audio passthrough is on and "
                "sessions are not allowed to suspend it"
            )
            return

        self.queue_secs = self._shorten_queue()
        path = xbmcvfs.translatePath(TEMPO_FILE)

        try:
            TempoFile(path).reset()
        except OSError as error:
            LOG.warning("[ syncplay/tempo ] cannot write %s: %s", path, error)
            restore_queue()
            return

        state.publish_syncplay_tempo({"file": path, "queue_secs": self.queue_secs})
        self.active = True
        LOG.info(
            "[ syncplay/tempo ] fine sync armed through %s, queue %.1fs",
            ADDON_ID,
            self.queue_secs,
        )

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
        """
        current = kodirpc.kodi_setting(QUEUE_SETTING)

        if current is None:
            return OMEGA_QUEUE_SECS

        try:
            tenths = int(current)
        except (TypeError, ValueError):
            return OMEGA_QUEUE_SECS

        if not settings.get_bool("syncPlayShortQueue") or tenths <= SHORT_QUEUE_TENTHS:
            return tenths / 10.0

        if kodirpc.set_kodi_setting(QUEUE_SETTING, SHORT_QUEUE_TENTHS):
            settings.set_str(QUEUE_RESTORE_SETTING, str(tenths))
            LOG.info(
                "[ syncplay/tempo ] %s %.1fs -> %.1fs for the session",
                QUEUE_SETTING,
                tenths / 10.0,
                SHORT_QUEUE_TENTHS / 10.0,
            )
            return SHORT_QUEUE_TENTHS / 10.0

        return tenths / 10.0
