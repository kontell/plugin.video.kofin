"""Pure helpers for SyncPlay: time conversions and protocol constants.

Kept free of Kodi imports so the protocol math is unit-testable.
Protocol reference: docs/SYNCPLAY.md in the kontell/syncplay-conformance
repository (the conformance kit is the spec's home).

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
# A group Unpause on audio resumes first and aligns while running; if the
# resume lands this far from the group position, seek (video aligns tightly
# via UNPAUSE_ALIGN_MS instead).
AUDIO_UNPAUSE_ALIGN_MS = 1500.0
AUTO_REJOIN_INTERVAL = 30.0  # min seconds between automatic re-Join attempts
SNAPSHOT_REQUEST_INTERVAL = 5.0  # min seconds between snapshot requests
# A scheduled Unpause names the exact group position; starting from anywhere
# else (the pause-time spread after a barrier, the load position on a hot join)
# is a persistent audible offset, and with no drift controller behind it the
# start is the only chance to remove it. Align by seeking at arm time — the
# seek settles inside the scheduling lead — whenever the start position is off
# by more than this.
UNPAUSE_ALIGN_MS = 100.0
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
# resuming a paused music player. The fork's comment here said VideoPlayer does
# not; on Android it does (OnPlayBackStarted fires right after the seek), which
# is how a group Unpause used to strand a member — see kodirpc.resume_player.
# So every seek that expects to stay paused re-pauses, and the resume can land
# after the seek settles, so audio watches a little longer.
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
# A group Unpause asks for playing explicitly and then confirms the clock
# moved, because the failure to start is silent otherwise: the member simply
# sits still while the group plays on, and nothing revisits it.
RESUME_VERIFY_S = 1.5  # keep asking for playing this long
RESUME_VERIFY_STEP_MS = 300  # clock sample spacing (xbmc.sleep, int ms)
PROGRAMMATIC_ECHO_GRACE = 1.0  # player events within this window of our own actions
STOP_WAIT_SECONDS = 3.0  # bound on waiting for a requested stop to take effect

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


def is_stale_version(version, highest_seen):
    """StateVersion gating (SYNCPLAY.md §6). None (v1) is never stale."""
    if version is None or highest_seen is None:
        return False

    return version < highest_seen
