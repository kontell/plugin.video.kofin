"""Pure helpers for SyncPlay: time conversions and protocol constants.

Kept free of Kodi imports so the protocol math is unit-testable.
Protocol reference: docs/SYNCPLAY.md in the jellyfin repository.

Ported from the fork's ``jellyfin_kodi/syncplay/utils.py`` under the phase-4
transplant discipline — the math and constants are the proven parts and stay
identical.
"""

import time
from datetime import datetime

#################################################################################################

TICKS_PER_SECOND = 10000000
TICKS_PER_MS = 10000

# Client-side constants from the protocol specification (SYNCPLAY.md §12)
TIMESYNC_WINDOW = 8  # sliding measurement window, use min-RTT sample
TIMESYNC_GREEDY_COUNT = 4  # exchanges at ~1s on group join
TIMESYNC_GREEDY_INTERVAL = 1.0
TIMESYNC_INTERVAL = 30.0  # spec minimum is 60s; report recommends 30s for Kodi
# Drift correction uses hysteresis: engage tempo only once drift exceeds the
# outer band, then hold until it is back inside the small inner band. A steady
# sub-second offset (unavoidable on v1 HTTP time sync over a WAN/proxy) then
# never provokes the constant ±tempo hunting that shows on the Kodi OSD.
CORRECTION_ENGAGE_MS = 350.0  # start correcting once drift exceeds this
CORRECTION_DISENGAGE_MS = 80.0  # ...and stop once back within this
CORRECTION_RATE_CAP = 0.03  # ±3% playback rate (was ±5%; gentler and subtler)
CORRECTION_RATE_GAIN_MS = 8000.0  # ms of drift mapped to a full-cap rate step
CORRECTION_SEEK_THRESHOLD_MS = 1500.0  # drift past this: tolerate (server re-syncs)
TRANSCODE_QUANTUM_MS = 3000.0  # segment-quantized: correct only above ~a segment
TEMPO_MIN_INTERVAL_MS = 2500.0  # min gap between tempo *changes* (not restores)
# A tempo speed-up outruns the buffer of a streamed/transcoded source; if a
# correction can't converge (or provokes buffering) we must abandon it rather
# than hold a speed-up that starves the buffer forever. Then tolerate the
# residual offset for a while before trying again.
CORRECTION_MAX_ENGAGED_S = 10.0  # give up a tempo correction that won't settle
DRIFT_BLACKOUT_AFTER_GIVEUP = 30.0  # tolerate the residual offset this long
# On some players, returning tempo to 1.0 resyncs the player and lands as a
# forward seek to a keyframe (a Kodi bug fixed in v22). Detect that skip once
# and stop using tempo on this player (tolerate drift instead).
TEMPO_RESTORE_SETTLE_MS = 1000.0  # let the restore settle before measuring
TEMPO_RESTORE_SKIP_MS = 1500.0  # a jump past this after a restore = a bad player
AUTO_REJOIN_INTERVAL = 30.0  # min seconds between automatic re-Join attempts
BUFFERING_DEBOUNCE = 2.5  # Player.Caching must persist this long before reporting
# A local start that must be proposed to the group is paused ("held") the
# instant it begins, so any waiting happens before playback instead of a few
# seconds into it. The hold is released by the group's Unpause; these bound
# how long we identify the item and how long an unanswered hold may last.
FORWARD_RETRY_INTERVAL = 0.5  # poll cadence while identifying a local start
FORWARD_RETRY_LIMIT = 10  # give up identifying after this many polls
HOLD_RELEASE_TIMEOUT = 10.0  # a held start nobody adopted resumes after this
STOP_PROMPT_GRACE = 1.0  # window for a replace-play to supersede a local stop
STOP_PROMPT_POLL = 0.1  # supersession poll cadence within that window
# PAPlayer::SeekTime() unconditionally restores playback speed, silently
# resuming a paused music player (VideoPlayer does not do this). Every seek
# that expects to stay paused must therefore re-pause, and the resume can
# land after the seek settles, so audio watches a little longer.
SEEK_REPAUSE_WINDOW_MS = 600.0
# A PAPlayer paused around a gapless boundary is unreliable: state reads
# (isPlaying/getTime/Player.Paused) intermittently report no media, and a
# single pause toggle can be swallowed. The group Unpause is the one command
# that must not be lost, so on audio it retries -- nudge, then verify the
# clock actually advances -- until it demonstrably took effect.
UNPAUSE_RETRY_WINDOW_MS = 4000.0  # keep trying this long before giving up
UNPAUSE_NUDGE_INTERVAL_MS = 600.0  # min gap between pause-toggle nudges
UNPAUSE_VERIFY_STEP_MS = 300  # clock sample spacing (xbmc.sleep, int ms)
SEEK_SETTLE_TIMEOUT = 3.0  # give up waiting for a seek to land after this
DRIFT_BLACKOUT_AFTER_SEEK = 3.0  # no drift corrections right after a seek
PROGRAMMATIC_ECHO_GRACE = 1.0  # player events within this window of our own actions

#################################################################################################


def local_ms():
    """Local wall clock in unix milliseconds."""
    return time.time() * 1000.0


def ticks_to_ms(ticks):
    return (ticks or 0) / TICKS_PER_MS


def ms_to_ticks(ms):
    return int(ms * TICKS_PER_MS)


def seconds_to_ticks(seconds):
    return int(seconds * TICKS_PER_SECOND)


def ticks_to_seconds(ticks):
    return (ticks or 0) / TICKS_PER_SECOND


def to_iso(unix_ms):
    """Unix milliseconds -> ISO 8601 UTC string the server parses."""
    seconds, ms = divmod(int(round(unix_ms)), 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + ".%03dZ" % ms


def parse_iso_ms(value):
    """ISO 8601 UTC string -> unix milliseconds (float).

    Handles .NET's 7-digit fractional seconds and both 'Z' and '+00:00'
    style offsets; naive timestamps are treated as UTC.
    """
    if not value:
        return None

    text = value.strip().replace("Z", "+00:00")
    head, sep, offset = text.partition("+")

    if "." in head:
        base, fraction = head.split(".")
        head = "%s.%s" % (base, (fraction + "000000")[:6])

    if not sep:  # no timezone: treat as UTC
        head += "+00:00"
        offset = ""

    try:
        parsed = datetime.fromisoformat(head + (("+" + offset) if offset else ""))
    except ValueError:
        return None

    return parsed.timestamp() * 1000.0


def ntp_sample(t0, t1, t2, t3):
    """Standard NTP offset/rtt from the four timestamps (all unix ms).

    offset = server_clock - local_clock.
    """
    rtt = (t3 - t0) - (t2 - t1)
    offset = ((t1 - t0) + (t2 - t3)) / 2.0
    return offset, rtt


def command_position_ms(command_ticks, command_when_ms, server_now_ms):
    """Extrapolated group position for a command, in ms of media time."""
    elapsed = max(0.0, server_now_ms - command_when_ms)
    return ticks_to_ms(command_ticks) + elapsed


def correction_action(
    diff_ms, can_tempo, transcoding=False, correcting=False, engage_ms=None
):
    """Continuous drift correction — playback rate (tempo) only (SYNCPLAY.md §10).

    Drift is corrected by gently nudging the playback rate, never by seeking.
    A seek from the drift loop is a visible, imprecise resync that also fights
    the server's own authoritative re-sync of out-of-position members; drift
    that a gentle nudge can't reach (too small, too large, or no rate control
    available) is tolerated and left to the server.

    diff_ms: estimated group position minus local position (positive = behind).
    correcting: whether tempo is currently engaged. The caller tracks this so
        the ladder can use a wide *engage* band but a small *release* band;
        that Schmitt-trigger removes the dead-zone limit cycle that made tempo
        hunt (0.97<->1.03) around a steady offset.
    engage_ms: override for the outer band (the ``syncPlayTolerance`` setting).

    Returns (action, value, correcting):
        (None, None, False)    -- neutral; restore 1.0x / tolerate
        ('tempo', rate, True)  -- hold/adjust tempo
    """
    magnitude = abs(diff_ms)

    engage = CORRECTION_ENGAGE_MS if engage_ms is None else engage_ms
    disengage = min(CORRECTION_DISENGAGE_MS, engage / 2.0)
    tempo_max = CORRECTION_SEEK_THRESHOLD_MS  # beyond this a nudge can't close it

    if transcoding:
        # Positions are segment-quantized: only nudge for drift past ~a segment.
        engage = max(engage, TRANSCODE_QUANTUM_MS / 2.0)
        disengage = max(disengage, TRANSCODE_QUANTUM_MS / 4.0)
        tempo_max = max(tempo_max, TRANSCODE_QUANTUM_MS)

    # No rate control, or drift too large for a gentle nudge: tolerate. The
    # server re-syncs grossly out-of-position members; the client never seeks
    # from the drift loop (that only jumps the video and pauses the group).
    if not can_tempo or magnitude >= tempo_max:
        return None, None, False

    # Tempo with hysteresis: engage past the outer band, hold until inside the
    # inner band, then release to 1.0x.
    threshold = disengage if correcting else engage

    if magnitude < threshold:
        return None, None, False

    rate = 1.0 + max(
        -CORRECTION_RATE_CAP,
        min(CORRECTION_RATE_CAP, diff_ms / CORRECTION_RATE_GAIN_MS),
    )
    return "tempo", round(rate, 2), True
