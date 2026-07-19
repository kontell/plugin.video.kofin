"""Command execution and drift control against the Kodi player.

Ported from the fork with one substantive change (phase-4 plan §2): the
fork resolved a MediaSource path itself and fed it to the player; kofin's
``play_item`` builds the playlist from **kofin plugin URLs**
(``plugin://…?mode=play&id=…``) so device-profile selection, the transcode
ladder, resume, and playback reporting all stay in the existing pipeline —
SyncPlay says *which id at what position*, kofin's normal play path decides
*how*. Everything else — scheduling, the drift controller, the buffering
watch, the audio (PAPlayer) choreography — is the fork's proven code.
"""

import json
import threading

import xbmc

from kofin.core import settings
from kofin.core.log import Logger
from kofin.syncplay import utils

#################################################################################################

LOG = Logger(__name__)

#################################################################################################


def _rpc(method, params=None):
    """Full JSON-RPC response dict ({"result": …} / {"error": …})."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}

    if params is not None:
        payload["params"] = params

    response = json.loads(xbmc.executeJSONRPC(json.dumps(payload)))
    return response if isinstance(response, dict) else {}


class PlaybackController(object):
    """Executes group commands against the Kodi player and keeps local
    playback converged on the group timeline (SYNCPLAY.md §5.1, §10).

    All privileged player operations run inside the manager's
    programmatic() guard so they are not echoed back to the group as
    user actions.
    """

    def __init__(self, manager, player):
        self.manager = manager
        self.player = player

        self._timer = None  # pending scheduled command
        self._timer_lock = threading.Lock()
        self.last_command = None

        # (media_ms, server_when_ms, playing) drift reference
        self._reference = None

        self._loop_thread = None
        self._loop_stop = threading.Event()

        self._can_tempo = False
        self._applied_tempo = 1.0
        self._correcting = False  # hysteresis: is a tempo correction engaged?
        self._correcting_since = 0.0  # when the current correction engaged
        self._last_tempo_change_ms = 0.0  # rate-limit tempo *changes*
        self._tempo_restore_watch = None  # (pos, time) to check a restore skip
        self._tempo_causes_skip = False  # this player skips on tempo restore
        self._player_id = 1  # Kodi video player

        self._drift_blackout_until = 0.0
        self._caching_since = None
        self._buffering_reported = False

    # ------------------------------------------------------------------
    # Command scheduling (SYNCPLAY.md §5.1)
    # ------------------------------------------------------------------

    def schedule(self, command):
        """Schedule a SyncPlayCommand for its server-clock instant."""
        when_ms = utils.parse_iso_ms(command.get("When"))

        if when_ms is None:
            LOG.warning("Command without a usable When: %s", command)
            return

        fire_local_ms = when_ms - self.manager.offset_ms()
        delay = (fire_local_ms - utils.local_ms()) / 1000.0

        self.cancel_pending()
        LOG.info(
            "[ syncplay/%s ] at %s (%+.0fms)",
            command.get("Command"),
            command.get("When"),
            delay * 1000,
        )

        if delay <= 0:
            self._execute(command)
            return

        with self._timer_lock:
            self._timer = threading.Timer(delay, self._execute, args=(command,))
            self._timer.daemon = True
            self._timer.start()

    def cancel_pending(self):
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _execute(self, command):
        try:
            name = command.get("Command")
            when_ms = utils.parse_iso_ms(command.get("When"))
            ticks = command.get("PositionTicks") or 0

            self.last_command = command

            if name == "Unpause":
                self._reference = (utils.ticks_to_ms(ticks), when_ms, True)
                self._do_unpause(ticks, when_ms)
            elif name == "Pause":
                self._reference = (utils.ticks_to_ms(ticks), when_ms, False)
                self._do_pause(ticks)
            elif name == "Seek":
                self._reference = (utils.ticks_to_ms(ticks), when_ms, False)
                self._do_seek(ticks)
            elif name == "Stop":
                self._reference = None
                self._do_stop()
            else:
                LOG.info("Unknown SyncPlay command: %s", name)
        except Exception as error:
            LOG.exception("SyncPlay command failed: %s", error)

    def _do_unpause(self, ticks, when_ms):
        # Gate on our own phase, not on player state reads: around a held
        # music boundary isPlaying/getTime intermittently report no media
        # for media that is right there, paused (fork field log 2026-07-10).
        phase = self.manager.phase

        if phase == "loading":
            LOG.info("Unpause while still loading, deferring to ready flow")
            return

        if phase not in ("waiting_ready", "synced"):
            LOG.info("Unpause with nothing followed, ignoring")
            return

        # Late command (When already past): jump to the extrapolated live
        # position instead of starting behind the group.
        target_ms = utils.command_position_ms(
            ticks, when_ms, self.manager.server_now_ms()
        )

        with self.manager.programmatic():
            if self._is_audio():
                # Resume first: a paused PAPlayer must never be seeked
                # (field-verified: seeks and even the pause toggle queue
                # up until user input on some builds, self-resume on
                # others). All audio alignment happens while running, and
                # the resume itself is retried until the clock moves.
                resumed = self._resume_with_retries()

                if not resumed:
                    LOG.warning("Unpause did not take effect; leaving it to a resync")
                    return

                behind_ms = target_ms - self._position_ms()

                if abs(behind_ms) > utils.CORRECTION_SEEK_THRESHOLD_MS:
                    self._seek_and_settle(target_ms)
            else:
                behind_ms = target_ms - self._position_ms()

                if abs(behind_ms) > utils.CORRECTION_SEEK_THRESHOLD_MS:
                    self._seek_and_settle(target_ms)

                if self._is_paused():
                    self.player.pause()  # toggles back to playing

        self.manager.on_local_unpaused()
        self._drift_blackout_until = (
            utils.local_ms() / 1000.0 + utils.DRIFT_BLACKOUT_AFTER_SEEK
        )

    def _resume_with_retries(self):
        """Resume paused audio and verify it, nudging until it sticks.

        Around a held gapless boundary the player's state reads are not
        trustworthy and a single toggle can be swallowed, so the only
        acceptable success signal is the clock demonstrably advancing.
        Toggling with no media loaded is ignored by Kodi, so a nudge is
        safe even when the reads claim there is nothing playing.
        """
        deadline = utils.local_ms() + utils.UNPAUSE_RETRY_WINDOW_MS
        last_nudge = 0.0
        last_pos = None
        frozen_reads = 0

        while utils.local_ms() < deadline:
            try:
                pos = self.player.getTime()
            except Exception:
                pos = None

            if pos is not None and last_pos is not None and pos > last_pos + 0.1:
                return True  # the clock is moving: playing

            if pos is not None and last_pos is not None and not self._is_paused():
                # Claims to be playing but the clock is frozen: jammed.
                frozen_reads += 1

            last_pos = pos
            now = utils.local_ms()

            if now - last_nudge > utils.UNPAUSE_NUDGE_INTERVAL_MS and (
                pos is None or self._is_paused() or frozen_reads >= 2
            ):
                LOG.info("[ syncplay/unpause ] nudging the player")
                self.player.pause()
                last_nudge = now
                frozen_reads = 0

            xbmc.sleep(utils.UNPAUSE_VERIFY_STEP_MS)

        return False

    def _do_pause(self, ticks):
        if not self._has_media():
            return

        with self.manager.programmatic():
            if not self._is_paused():
                self.player.pause()

            # Land everyone on the same frame when we drifted visibly.
            # Video only: a paused PAPlayer swallows seeks (or worse),
            # and while the group is paused nothing is audible anyway —
            # the Unpause aligns audio the moment it resumes.
            diff_ms = utils.ticks_to_ms(ticks) - self._position_ms()

            if abs(diff_ms) > 250 and not self._is_audio():
                self._seek_and_settle(utils.ticks_to_ms(ticks))

        self._restore_tempo()

    def _do_seek(self, ticks):
        if not self._has_media():
            return

        if self._is_audio():
            # Never seek a paused PAPlayer. The protocol always follows a
            # group Seek with an Unpause carrying the same position, so
            # pause here, promise the target in the ready report, and let
            # the Unpause land it on resume.
            with self.manager.programmatic():
                if not self._is_paused():
                    self.player.pause()

            self._restore_tempo()
            self.manager.post_report(
                "syncplay_ready", position_s=utils.ticks_to_seconds(ticks)
            )
            return

        with self.manager.programmatic():
            if not self._is_paused():
                self.player.pause()

            self._seek_and_settle(utils.ticks_to_ms(ticks))

        self._restore_tempo()
        self.report_ready()

    def _do_stop(self):
        # Read before on_group_stopped() resets it: only media SyncPlay
        # is actually driving is stopped — a group Stop must not kill a
        # detached spectator's own playback.
        was_following = self.manager.phase != "idle"

        self.cancel_pending()
        self._restore_tempo()
        self.manager.on_group_stopped()

        if was_following and self._has_media():
            with self.manager.programmatic():
                self.player.stop()

    # ------------------------------------------------------------------
    # Item loading (queue application handoff)
    # ------------------------------------------------------------------

    def play_item(self, item, start_ticks):
        """Start a queue item paused-on-arrival; the ready flow reports in.

        The phase-4 re-target: the playlist entry is a kofin plugin URL, so
        the plugin process resolves it (device profile, direct-play vs
        transcode, PlaybackInfo at ``startticks``) and pushes the play state
        for the service player to claim — exactly the path a user-initiated
        play takes. SyncPlay group starts are unattended by definition; the
        plugin play path has no dialogs, so nothing needs suppressing.
        """
        from kofin.plugin.listitems import plugin_url

        item_id = item.get("Id")

        if not item_id:
            raise ValueError("queue item without an Id")

        params = {"mode": "play", "id": str(item_id)}

        if start_ticks:
            params["startticks"] = str(int(start_ticks))

        url = plugin_url(params)
        playlist_type = (
            xbmc.PLAYLIST_MUSIC if item.get("Type") == "Audio" else xbmc.PLAYLIST_VIDEO
        )
        playlist = xbmc.PlayList(playlist_type)

        with self.manager.programmatic():
            if self.player.isPlaying():
                self.player.stop()

            playlist.clear()
            playlist.add(url)
            self.player.play(playlist, startpos=0)

    def prepare_ready(self):
        """After onAVStarted: settle on the group position and report Ready.

        The server compares our reported position against the group and
        answers with a private Seek if we are out of tolerance (§7).
        """
        self._detect_player_features()

        target_ms = self.estimate_position_ms()

        if target_ms is not None:
            diff = target_ms - self._position_ms()

            # Audio holds are left where they paused: a paused PAPlayer
            # must never be seeked, and the group Unpause aligns the
            # position the moment playback resumes.
            if abs(diff) > 500 and not (self._is_audio() and self._is_paused()):
                with self.manager.programmatic():
                    self._seek_and_settle(target_ms)

        self.report_ready()

    def ensure_paused(self):
        # Gate on isPlaying alone: during a gapless stream swap getTime()
        # can misbehave, and this is exactly the window a start hold must
        # be able to pause in.
        if self._player_active() and not self._is_paused():
            with self.manager.programmatic():
                self.player.pause()

    def ensure_playing(self):
        if self._player_active() and self._is_paused():
            with self.manager.programmatic():
                self.player.pause()  # toggles back to playing

    def stop_media(self):
        if self._has_media():
            with self.manager.programmatic():
                self.player.stop()

    # ------------------------------------------------------------------
    # Reports (SYNCPLAY.md §4)
    # ------------------------------------------------------------------

    def report_ready(self):
        self.manager.post_report("syncplay_ready")

    def report_buffering(self):
        self.manager.post_report("syncplay_buffering")

    # ------------------------------------------------------------------
    # Drift reference (SYNCPLAY.md §10, §11)
    # ------------------------------------------------------------------

    def set_reference(self, ticks, server_when_ms, playing):
        self._reference = (utils.ticks_to_ms(ticks), server_when_ms, playing)

    def estimate_position_ms(self):
        """Estimated group position now, from the last command/beacon."""
        if self._reference is None:
            return None

        media_ms, when_ms, playing = self._reference

        if when_ms is None:
            return media_ms

        if not playing:
            return media_ms

        return media_ms + max(0.0, self.manager.server_now_ms() - when_ms)

    # ------------------------------------------------------------------
    # Sync loop: buffering watch + drift correction
    # ------------------------------------------------------------------

    def start_loop(self):
        if self._loop_thread is not None and self._loop_thread.is_alive():
            return

        self._loop_stop.clear()
        self._loop_thread = threading.Thread(
            target=self._loop, name="kofin-syncplay-loop"
        )
        self._loop_thread.daemon = True
        self._loop_thread.start()

    def stop_loop(self):
        self._loop_stop.set()
        self.cancel_pending()
        self._restore_tempo()
        self._correcting = False
        self._tempo_restore_watch = None  # don't carry a stale check to next item
        self._caching_since = None
        self._buffering_reported = False
        self.last_command = None
        self._reference = None

    def _loop(self):
        LOG.info("--->[ syncplay loop ]")
        tick = 0

        while not self._loop_stop.wait(0.25):
            try:
                if not self.manager.in_group() or not self._has_media():
                    self._caching_since = None
                    continue

                if self.manager.phase not in ("waiting_ready", "synced"):
                    continue

                self._watch_buffering()
                self._verify_tempo_restore()

                tick += 1

                if tick % 4 == 0:  # once per second
                    self._correct_drift()
            except Exception as error:
                LOG.exception("SyncPlay loop error: %s", error)

        LOG.info("---<[ syncplay loop ]")

    def _verify_tempo_restore(self):
        """Stop using tempo on players that glitch when it returns to 1.0.

        Returning to normal speed triggers a player resync that, on some
        builds (a Kodi bug fixed in v22), lands as a visible forward seek to a
        keyframe. If the position leapt past what plain 1.0x playback
        explains after a restore, disable tempo for the rest of the session so
        the drift loop just tolerates the offset instead of glitching.
        """
        if self._tempo_restore_watch is None:
            return

        base_pos, base_time = self._tempo_restore_watch
        elapsed = utils.local_ms() - base_time

        if elapsed < utils.TEMPO_RESTORE_SETTLE_MS:
            return

        self._tempo_restore_watch = None

        if self._is_paused():
            return

        jump = self._position_ms() - (base_pos + elapsed)

        if jump > utils.TEMPO_RESTORE_SKIP_MS:
            LOG.warning(
                "Tempo restore skipped %.0fms on this player; disabling tempo",
                jump,
            )
            self._tempo_causes_skip = True
            self._can_tempo = False

    def _watch_buffering(self):
        """Debounced Player.Caching -> Buffering/Ready reports (§7)."""
        caching = bool(xbmc.getCondVisibility("Player.Caching"))

        if self._buffering_reported:
            # A recovery Ready must go out even if the group paused us in
            # the meantime, or the server keeps waiting on this member.
            if not caching:
                LOG.info("[ syncplay/recovered ]")
                self._buffering_reported = False
                self._caching_since = None
                self.report_ready()

            return

        if not self._expecting_playback():
            self._caching_since = None
            return

        now = utils.local_ms() / 1000.0

        if caching:
            if self._caching_since is None:
                self._caching_since = now

            if now - self._caching_since > utils.BUFFERING_DEBOUNCE:
                LOG.info("[ syncplay/buffering ]")
                self._buffering_reported = True
                # A tempo speed-up is the likeliest reason a streamed source
                # under-runs its buffer; drop it and pause corrections so we
                # do not re-starve the moment the group resumes.
                self._back_off_correction(now)
                self.report_buffering()
        else:
            self._caching_since = None

    def _correct_drift(self):
        if not self._expecting_playback() or self._buffering_reported:
            return

        if self._is_paused():
            return

        if not settings.get_bool("syncPlayDriftCorrection"):
            self._restore_tempo()
            self._correcting = False
            return

        if utils.local_ms() / 1000.0 < self._drift_blackout_until:
            return

        estimate = self.estimate_position_ms()

        if estimate is None:
            return

        diff_ms = estimate - self._position_ms()
        # Rate is the loop's only actuator; without it (Kodi's
        # sync-to-display setting off, or a player without tempo) drift
        # is simply tolerated (never a seek — the server re-syncs).
        was_correcting = self._correcting
        action, value, self._correcting = utils.correction_action(
            diff_ms,
            self._can_tempo,
            self.manager.is_transcoding(),
            correcting=self._correcting,
            engage_ms=self._engage_ms(),
        )

        if action is None:
            self._restore_tempo()
        elif action == "tempo":
            now = utils.local_ms()

            if not was_correcting:
                self._correcting_since = now
            elif now - self._correcting_since > utils.CORRECTION_MAX_ENGAGED_S * 1000:
                # We are holding a speed-up that won't close the gap (an
                # offset we can't correct on this transport, e.g. one that
                # would just starve a streamed buffer). Abandon it and
                # tolerate the residual offset instead of hunting forever.
                LOG.info(
                    "[ syncplay/drift ] %+.0fms won't converge, backing off", diff_ms
                )
                self._back_off_correction(now / 1000.0)
                return

            self._apply_tempo(value)

    def _engage_ms(self):
        """Outer correction band from the ``syncPlayTolerance`` setting
        (int ms; plan §4). 0/unset falls back to the fork's default band."""
        tolerance = settings.get_int("syncPlayTolerance")
        return float(tolerance) if tolerance > 0 else utils.CORRECTION_ENGAGE_MS

    def _back_off_correction(self, now_s):
        """Drop any speed-up, disengage, and tolerate the offset for a while.

        Used when a correction won't converge or when playback starts
        buffering: a sustained tempo > 1.0 outruns a streamed source's
        buffer, so holding it just starves the buffer indefinitely.
        """
        self._restore_tempo()
        self._correcting = False
        self._drift_blackout_until = now_s + utils.DRIFT_BLACKOUT_AFTER_GIVEUP

    def _expecting_playback(self):
        return (
            self.last_command is not None
            and self.last_command.get("Command") == "Unpause"
        )

    # ------------------------------------------------------------------
    # Player plumbing
    # ------------------------------------------------------------------

    def _has_media(self):
        try:
            return self.player.isPlaying() and self.player.getTime() >= 0
        except Exception:
            return False

    def _player_active(self):
        try:
            return self.player.isPlaying()
        except Exception:
            return False

    def _is_audio(self):
        try:
            return self.player.isPlayingAudio()
        except Exception:
            return False

    def _is_paused(self):
        return bool(xbmc.getCondVisibility("Player.Paused"))

    def _position_ms(self):
        try:
            return self.player.getTime() * 1000.0
        except Exception:
            return 0.0

    def _seek_and_settle(self, target_ms):
        """Seek and wait for the position to land.

        Used only to align on a server command (Unpause/Seek) or a fresh item
        — never from the drift loop, which corrects by rate alone.
        """
        was_paused = self._is_paused()
        target_s = max(0.0, target_ms / 1000.0)
        started = utils.local_ms()
        self.player.seekTime(target_s)

        # Give the async seek a beat before polling, or a nearby
        # pre-seek position would satisfy the check immediately.
        xbmc.sleep(150)
        deadline = started + utils.SEEK_SETTLE_TIMEOUT * 1000

        while utils.local_ms() < deadline:
            if abs(self._position_ms() - target_ms) < 2000:
                break

            xbmc.sleep(100)

        if was_paused:
            # PAPlayer::SeekTime() unconditionally restores playback
            # speed, silently resuming a paused music player (VideoPlayer
            # does not). The resume can also land after the settle loop,
            # so on audio watch a short window before trusting the state.
            watch_until = utils.local_ms() + (
                utils.SEEK_REPAUSE_WINDOW_MS if self._is_audio() else 0
            )

            while True:
                if not self._is_paused():
                    LOG.debug("[ syncplay/seek ] re-pausing after the seek")
                    self.player.pause()
                    break

                if utils.local_ms() >= watch_until:
                    break

                xbmc.sleep(50)

    def _jsonrpc(self, method, params=None):
        try:
            return _rpc(method, params) or {}
        except Exception as error:
            LOG.debug("JSONRPC %s failed: %s", method, error)
            return {}

    def _detect_player_features(self):
        """Decide whether Player.SetTempo can be used for drift correction.

        In Kodi, tempo is available only when "Sync playback to display"
        (videoplayer.usedisplayasclock) is enabled: CVideoPlayer::CanTempo()
        returns exactly that setting, and SetTempo fails otherwise. There is
        no JSON-RPC player property for it, so read the setting directly.
        This runs on the ready path while the player is paused, where a live
        SetTempo probe would spuriously fail; _apply_tempo() disables tempo
        as a backstop if an actual SetTempo is ever rejected (e.g. a realtime
        stream, which also blocks tempo).
        """
        self._player_id = 1
        video_player = False
        result = self._jsonrpc("Player.GetActivePlayers")

        for entry in result.get("result") or []:
            if entry.get("type") == "video":
                video_player = True
                self._player_id = entry.get("playerid", 1)

        result = self._jsonrpc(
            "Settings.GetSettingValue",
            {"setting": "videoplayer.usedisplayasclock"},
        )
        self._can_tempo = bool((result.get("result") or {}).get("value"))

        if not video_player:
            # PAPlayer has no tempo: JSON-RPC SetTempo on it succeeds
            # without effect, so the rejection backstop would never fire
            # and the drift loop would hold a correction that does
            # nothing. Music drift is tolerated (the server re-syncs
            # gross offsets).
            self._can_tempo = False

        if self._tempo_causes_skip:
            # A previous item proved this player seeks when tempo returns to
            # 1.0; don't use it again this session.
            self._can_tempo = False
        self._applied_tempo = 1.0
        LOG.info(
            "SyncPlay tempo control available: %s (%s)",
            self._can_tempo,
            "sync-playback-to-display" if video_player else "audio-only player",
        )

    def _apply_tempo(self, rate):
        if abs(rate - self._applied_tempo) < 0.01:
            return

        now = utils.local_ms()

        # Rate-limit engaging/adjusting tempo so the skin's speed indicator is
        # not re-triggered every second and corrections stay gentle. Returning
        # to 1.0x is never throttled — we always want to leave tempo promptly.
        if (
            rate != 1.0
            and now - self._last_tempo_change_ms < utils.TEMPO_MIN_INTERVAL_MS
        ):
            return

        # A restore to 1.0 makes some players seek; snapshot the position
        # *before* the call so _verify_tempo_restore can catch that skip.
        restoring = rate == 1.0 and self._applied_tempo != 1.0
        pre_pos = self._position_ms() if restoring else 0.0

        result = self._jsonrpc(
            "Player.SetTempo", {"playerid": self._player_id, "tempo": rate}
        )

        if result.get("error"):
            LOG.info("SetTempo rejected (%s), disabling tempo path", result["error"])
            self._can_tempo = False
            return

        LOG.debug("[ syncplay/tempo ] %.2f", rate)
        self._last_tempo_change_ms = now
        self._applied_tempo = rate

        if restoring:
            self._tempo_restore_watch = (pre_pos, now)

    def _restore_tempo(self):
        if self._can_tempo and abs(self._applied_tempo - 1.0) >= 0.01:
            self._apply_tempo(1.0)
