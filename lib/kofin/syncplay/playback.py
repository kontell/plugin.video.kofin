"""Command execution against the Kodi player.

Ported from the fork with two substantive changes. Phase-4 plan §2: the
fork resolved a MediaSource path itself and fed it to the player; kofin's
``play_item`` builds the playlist from **kofin plugin URLs**
(``plugin://…?mode=play&id=…``) so device-profile selection, the transcode
ladder, resume, and playback reporting all stay in the existing pipeline —
SyncPlay says *which id at what position*, kofin's normal play path decides
*how*. And the fork's continuous drift controller is gone entirely: see
:class:`PlaybackController` for why, and ``docs/syncplay-drift-shakedown.md``
for the measurements that retired it. Everything else — scheduling, the
buffering watch, the audio (PAPlayer) choreography — is the fork's proven code.
"""

import threading

import xbmc

from kofin.core import kodirpc
from kofin.core.log import Logger
from kofin.syncplay import utils
from kofin.syncplay.tempo import PulseScheduler, SEEK_LAG_DEFAULT_MS

#################################################################################################

LOG = Logger(__name__)

#################################################################################################


class PlaybackController(object):
    """Executes group commands against the Kodi player and converges local
    playback on the group timeline at each command (SYNCPLAY.md §5.1).

    There is deliberately **no continuous drift controller**. One existed —
    a ``Player.SetTempo`` ladder driven by a per-second position comparison —
    and it was withdrawn after measurement showed the actuator cannot serve the
    error it was aimed at: tempo is available only while Kodi's "sync playback
    to display" is on, and that setting slaves the media clock to the panel, so
    a display rate that is not a whole multiple of the video frame rate imposes
    a fixed rate error. Measured on three Piers devices, that error ran from
    0.5 % to 4.3 % — the worst of it 14x the +/-3 % the ladder could command,
    and none of it fixable from Kodi's settings on those panels.
    ``docs/syncplay-drift-shakedown.md`` §10 has the numbers and the controls.

    Position is converged where the group tells us to converge it — on
    Unpause, Pause, Seek and item load. Between commands, a video item routed
    through inputstream.tempo is kept on the group position by the
    :class:`~kofin.syncplay.tempo.PulseScheduler`: bounded, confirmed rate
    pulses through an actuator that works with the display clock *off*, with a
    quiet window after each one — not a loop. Anything else is left alone. All
    privileged player operations run inside the manager's programmatic() guard
    so they are not echoed back to the group as user actions.
    """

    def __init__(self, manager, player):
        self.manager = manager
        self.player = player

        self._timer = None  # pending scheduled command
        self._timer_lock = threading.Lock()
        self.last_command = None

        # (media_ms, server_when_ms, playing) group position reference
        self._reference = None

        self._loop_thread = None
        self._loop_stop = threading.Event()

        # schedule() arms the fire-time timer and *then* pre-aligns on the
        # dispatcher, so both touch the player at once whenever the align
        # overruns the scheduling lead — measured on a transcoding member,
        # where a 600 ms seek beat a 437 ms lead and the align's re-pause
        # landed after the resume, stranding it. The two sequences take this
        # in turn instead.
        self._player_lock = threading.RLock()

        self._caching_since = None
        self._buffering_reported = False

        # Fine sync between commands (syncplay/tempo.py), and what a seek
        # aimed at the group's current position leaves behind on this device
        # — its restart time plus its landing error — so a corrective seek can
        # aim ahead of a moving group by exactly that. Learned from every
        # playing seek (see _seek_and_settle).
        self.tempo = PulseScheduler(self)
        self.seek_lag_ms = SEEK_LAG_DEFAULT_MS

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

        # With the timer armed, use the scheduling lead to line the player up
        # on the start position, so the fire instant starts from exactly the
        # group position (initial-start tightness for barrier restarts and
        # hot joins alike). Blocking here is fine: this runs on the dispatcher
        # like any REST round trip, and the seek settle is well inside the lead.
        if command.get("Command") == "Unpause":
            self._prealign_unpause(command)

    def _prealign_unpause(self, command):
        """Seek to a scheduled Unpause's position at arm time.

        While paused, the position at the start instant IS the command's
        PositionTicks — no extrapolation. Audio is excluded: a paused
        PAPlayer must never be seeked (it aligns after its resume instead).
        """
        if self._is_audio() or self.manager.phase not in ("waiting_ready", "synced"):
            return

        if self.manager.is_transcoding():
            # A transcode cannot seek to an arbitrary position (it snaps to a
            # segment: 47603 ms asked, 43964 ms landed) and the restart costs
            # more than the scheduling lead, so pre-aligning makes the start
            # both later and further out. Resume where we are instead.
            LOG.info("[ syncplay/align ] skipped: transcoding")
            return

        target_ms = utils.ticks_to_ms(command.get("PositionTicks") or 0)

        try:
            offset_ms = target_ms - self._position_ms()
        except Exception:
            return

        if abs(offset_ms) <= utils.UNPAUSE_ALIGN_MS:
            return

        if self._leave_to_fine_sync(offset_ms, "to the start position"):
            return

        LOG.info("[ syncplay/align ] %+.0fms to the start position", offset_ms)

        with self._player_lock, self.manager.programmatic():
            self._seek_and_settle(target_ms)

    def _leave_to_fine_sync(self, offset_ms, where):
        """Whether an unpause offset is better closed by a rate pulse than a cut.

        ``_align_after_resume`` already makes this trade -- "a skip is for gross
        errors" -- but the two aligns on the way *into* the resume did not, so
        anything past UNPAUSE_ALIGN_MS (100 ms) was seeked even when it sat well
        inside the pulse budget. That is the skip a viewer notices on an
        ordinary unpause, and it is smaller than the residual fine sync would
        have been allowed to close on the other side of the same resume.

        Only in the ``synced`` phase. A ``waiting_ready`` start -- a barrier
        restart or a hot join -- is the one chance to begin exactly on the group
        position, and its offsets are the large ones (1.0-1.7 s measured on the
        rig against a 5 s budget); handing those to the scheduler would trade a
        single cut for twenty seconds off-rate. Those keep the seek.

        ``can_close`` is already the right gate: the scheduler drops its tempo
        file when an item is not routed through inputstream.tempo, and again
        when a pulse is not confirmed, so it answers False exactly when nothing
        can be nudged.
        """
        if self.manager.phase != "synced":
            return False

        if not self.tempo.can_close(offset_ms):
            return False

        LOG.info("[ syncplay/align ] %+.0fms %s: left to fine sync", offset_ms, where)
        return True

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
            # No pulse runs across a command: the command converges position
            # itself, and a seek under a running rate lands early.
            self.tempo.cancel(name)

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

        with self._player_lock, self.manager.programmatic():
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

                if abs(behind_ms) > utils.AUDIO_UNPAUSE_ALIGN_MS:
                    self._seek_and_settle(target_ms)
            else:
                behind_ms = target_ms - self._position_ms()

                # Catch-all for starts that were not pre-aligned at arm time
                # (late commands execute immediately): the same tight band.
                # stay_paused=False because a resume follows immediately: the
                # settle's re-pause would fight it, and the pause it queues
                # lands after the resume decision is taken.
                #
                # Never on a transcode, for the same reason the arm-time align
                # is skipped: the seek snaps to a segment boundary, so aligning
                # a member that was 224 ms out left it 4.2 s out instead. An
                # offset the transport cannot close is better carried than
                # widened.
                if abs(behind_ms) > utils.UNPAUSE_ALIGN_MS:
                    if self.manager.is_transcoding():
                        LOG.info(
                            "[ syncplay/align ] %+.0fms carried: transcoding",
                            behind_ms,
                        )
                    elif not self._leave_to_fine_sync(behind_ms, "at the resume"):
                        self._seek_and_settle(target_ms, stay_paused=False)

                if self._resume_and_verify():
                    self._align_after_resume()
                    self.tempo.note_settle()

        self.manager.on_local_unpaused()

    def _align_after_resume(self):
        """Close what the resume itself opened up, once the picture is moving.

        The alignment above happens *before* the resume, and the resume then
        takes somewhere between 0.1s and 1.2s to land -- measured across four
        captures on two devices, from the same command, agreeing on the
        schedule to within 3ms. Whatever was aligned is therefore stale by the
        time anything is on screen, and nothing looked again: that is the whole
        of why a resume drifts where a seek does not. A seek names a position,
        so arriving late costs nothing; a resume names an instant, and the
        instants land a second apart.

        Here the clock has demonstrably started, so the reading is real and the
        group estimate is comparable to it. Not on a transcode, where a seek
        snaps to a segment boundary and can widen the gap it was closing, and
        not on audio, whose resume choreography aligns itself.
        """
        if self._is_audio() or self.manager.is_transcoding():
            return

        target_ms = self.estimate_position_ms()

        if target_ms is None:
            return

        behind_ms = target_ms - self._position_ms()

        # Logged whether or not it acts. A threshold that never fires and one
        # that fires and does nothing look identical from the outside, and
        # both have been believed today; the residual itself distinguishes
        # them, and it is the number the threshold should be chosen from.
        LOG.info(
            "[ syncplay/resumed ] residual %+.0fms (threshold %.0fms)",
            behind_ms,
            utils.POST_RESUME_ALIGN_MS,
        )

        if abs(behind_ms) <= utils.POST_RESUME_ALIGN_MS:
            return

        if self.tempo.can_close(behind_ms):
            # A skip is for gross errors: a residual fine sync can close is
            # left to it — a few seconds at a raised rate instead of a visible
            # cut; the scheduler measures again once the resume has played
            # through the queue.
            LOG.info(
                "[ syncplay/align ] %+.0fms after the resume: left to fine sync",
                behind_ms,
            )
            return

        LOG.info("[ syncplay/align ] %+.0fms after the resume landed", behind_ms)
        # Aimed ahead by what a seek costs here: the group keeps moving while
        # it lands, and a seek aimed at where the group *was* left +600-900 ms
        # behind on both rig members, which the fine-sync scheduler then had to
        # close with a second seek.
        self._seek_and_settle(target_ms + self.seek_lag_ms, stay_paused=False)

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

    def _resume_and_verify(self):
        """Ask for playing, and keep asking until the clock actually moves.

        ``speed`` is not proof: Kodi reports speed 1 for a player that is not
        advancing, so the only signal is the position sampled twice
        (kodi-drive: kodi-jsonrpc). Re-asking matters as much as checking —
        the failure this exists for is another thread's pause landing *after*
        our resume, and a second explicit play is what undoes that.
        """
        deadline = utils.local_ms() + utils.RESUME_VERIFY_S * 1000
        started = utils.local_ms()
        asked_at = None
        attempts = 0

        while utils.local_ms() < deadline:
            now = utils.local_ms()

            # Re-asking is what undoes another thread's pause landing after our
            # resume, so it stays -- but on its own clock. Tying it to the poll
            # cadence is what made a slow detection into a late start.
            if asked_at is None or now - asked_at >= utils.RESUME_REASK_MS:
                kodirpc.resume_player()
                asked_at = now
                attempts += 1

            before = self._position_ms()
            xbmc.sleep(utils.RESUME_VERIFY_STEP_MS)

            if self._position_ms() > before + 20:
                LOG.info(
                    "[ syncplay/unpause ] playing after %.0fms (%s ask%s)",
                    utils.local_ms() - started,
                    attempts,
                    "" if attempts == 1 else "s",
                )

                return True

        LOG.warning("Unpause did not start playback; leaving it to a resync")
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

            self.manager.post_report(
                "syncplay_ready", position_s=utils.ticks_to_seconds(ticks)
            )
            return

        if self.manager.is_transcoding():
            # A seek inside a transcoded stream cannot land where it was asked
            # to: Kodi snaps to the nearest segment boundary and says so
            # ("SeekTime - seek ended up on time 3008429" for a 3000000 ms
            # target — 8.4s past). Restarting the stream at the target is
            # exact, because the server begins encoding there, which is how
            # every kofin play already starts. The reload reports Ready through
            # the normal load flow, so nothing is reported here.
            LOG.info("[ syncplay/seek ] transcoding: reloading at the target")
            self.manager.reload_current_item()
            return

        with self.manager.programmatic():
            if not self._is_paused():
                self.player.pause()

            self._seek_and_settle(utils.ticks_to_ms(ticks))

        self.report_ready()

    def _do_stop(self):
        # Read before on_group_stopped() resets it: only media SyncPlay
        # is actually driving is stopped — a group Stop must not kill a
        # detached spectator's own playback.
        was_following = self.manager.phase != "idle"

        self.cancel_pending()
        self.manager.on_group_stopped()

        if was_following and self._has_media():
            with self.manager.programmatic():
                # Never player.stop() from here: this runs on the dispatcher,
                # and that call holds the GIL until Kodi's teardown finishes
                # (kodirpc.stop_player, issue #155). Landing asynchronously is
                # safe — on_group_stopped() has already left the phase behind,
                # so the late callback finds nothing to forward, and it is
                # inside PROGRAMMATIC_ECHO_GRACE either way.
                kodirpc.stop_player()

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

        # Always sent, and never negative. A group start names the position
        # even when that position is zero, so a falsy 0 must not be dropped —
        # omitting it lets the play route fall back to the member's own resume
        # point, which starts it minutes away from the group. And the estimate
        # can land just below zero (extrapolation across a clock offset), which
        # was measured reaching the route as startticks=-240000.
        params["startticks"] = str(max(0, int(start_ticks or 0)))

        url = plugin_url(params)
        playlist_type = (
            xbmc.PLAYLIST_MUSIC if item.get("Type") == "Audio" else xbmc.PLAYLIST_VIDEO
        )
        playlist = xbmc.PlayList(playlist_type)

        with self.manager.programmatic():
            if self.player.isPlaying():
                # Asked for and waited on rather than player.stop()'d: see
                # kodirpc.stop_player (issue #155). The wait keeps the playlist
                # rewrite below on the far side of the outgoing playback, which
                # the synchronous stop used to guarantee; Kodi orders the play
                # itself, since playPlaylist reaches the app thread behind the
                # stop message.
                kodirpc.stop_player(wait_seconds=utils.STOP_WAIT_SECONDS)

            playlist.clear()
            playlist.add(url)
            self.player.play(playlist, startpos=0)

    def prepare_ready(self):
        """After onAVStarted: settle on the group position and report Ready.

        The server compares our reported position against the group and
        answers with a private Seek if we are out of tolerance (§7).
        """
        target_ms = self.estimate_position_ms()

        if target_ms is not None:
            landed_ms = self._position_ms()
            diff = target_ms - landed_ms

            # Where a load actually lands, which is not always where it was
            # asked to. Worth a line of its own: a transcode is served from a
            # playlist the server is still writing, so the stream can begin
            # somewhere other than the requested start, and the size of that is
            # the difference between a group that looks synced and one that is.
            LOG.info(
                "[ syncplay/landed ] %.1fs, wanted %.1fs (%+.0fms)%s",
                landed_ms / 1000.0,
                target_ms / 1000.0,
                -diff,
                " transcoding" if self.manager.is_transcoding() else "",
            )

            # Audio holds are left where they paused: a paused PAPlayer
            # must never be seeked, and the group Unpause aligns the
            # position the moment playback resumes.
            if abs(diff) > 500 and not (self._is_audio() and self._is_paused()):
                with self.manager.programmatic():
                    self._seek_and_settle(target_ms)

        self.tempo.note_settle()
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
                kodirpc.stop_player()  # not player.stop() — issue #155

    # ------------------------------------------------------------------
    # Reports (SYNCPLAY.md §4)
    # ------------------------------------------------------------------

    def report_ready(self):
        self.manager.post_report("syncplay_ready")

    def report_buffering(self):
        self.manager.post_report("syncplay_buffering")

    # ------------------------------------------------------------------
    # Group position reference (SYNCPLAY.md §11)
    # ------------------------------------------------------------------

    def set_reference(self, ticks, server_when_ms, playing):
        self._reference = (utils.ticks_to_ms(ticks), server_when_ms, playing)

    def reference_is_playing(self):
        """Whether the group's last known state was playing.

        Distinct from estimate_position_ms(), which returns a position either
        way: a caller that wants to aim ahead of a moving group needs to know
        the group is actually moving.
        """
        if self._reference is None:
            return False

        return bool(self._reference[2])

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
    # Sync loop: the buffering watch
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
        self.tempo.reset()
        self._caching_since = None
        self._buffering_reported = False
        self.last_command = None
        self._reference = None

    def _loop(self):
        LOG.info("--->[ syncplay loop ]")

        while not self._loop_stop.wait(0.25):
            try:
                if not self.manager.in_group() or not self._has_media():
                    self._caching_since = None
                    continue

                if self.manager.phase not in ("waiting_ready", "synced"):
                    continue

                self._watch_buffering()

                if self.manager.watching_own_media():
                    # Blocking the group's transport commands is not enough on
                    # its own: the member is still in phase "synced", so fine
                    # sync went on measuring its private playback against the
                    # group and rate-shifting it to close a residual of whole
                    # minutes. Measured: 0.750x for 10s against a -306.6s gap.
                    # cancel() is quiet when no pulse is in flight.
                    self.tempo.cancel("spectator playing own media")
                    continue

                if self.manager.phase == "synced" and self._expecting_playback():
                    self.tempo.tick()
            except Exception as error:
                LOG.exception("SyncPlay loop error: %s", error)

        LOG.info("---<[ syncplay loop ]")

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
                self.report_buffering()
        else:
            self._caching_since = None

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

    def correct_position(self):
        """The fine-sync scheduler found a residual beyond what a pulse can
        close: seek to where the group will be once the seek has landed."""
        target_ms = self.estimate_position_ms()

        if target_ms is None:
            return

        with self._player_lock, self.manager.programmatic():
            self._seek_and_settle(target_ms + self.seek_lag_ms, stay_paused=False)

    def _clock_restart(self, target_ms, polls=30):
        """(local ms, position ms) at which the clock started advancing again
        after a seek, or None when no hold was seen (a correction too small to
        tell from playback, or a player that stayed paused).

        Kodi reports the target as soon as the seek is accepted and holds
        there while the pipeline refills; where the clock then restarts from
        is where the seek really landed. Bounded by polls, not by the wall
        clock: a test may freeze the clock.
        """
        held = None
        previous = None

        for _ in range(polls):
            position = self._position_ms()
            now = utils.local_ms()

            if previous is not None:
                if held is None:
                    if (
                        abs(position - previous) < 15
                        and abs(position - target_ms) < 1500
                    ):
                        held = position
                elif abs(position - held) > 30:
                    return now, position

            previous = position
            xbmc.sleep(50)

        return None

    def _seek_and_settle(self, target_ms, stay_paused=True):
        """Seek and wait for the position to land.

        Aligning on a server command (Unpause/Seek), a fresh item, or a
        residual beyond the pulse budget is what seeks; a residual inside it
        is closed by a tempo pulse instead, and anything under the deadband is
        left alone.

        ``stay_paused`` is the caller's intent for afterwards. It matters
        because a seek can resume a paused player by itself — PAPlayer always
        does, and Android's VideoPlayer was measured doing it too — so a caller
        that wants to stay paused has to undo that, and a caller about to
        resume must *not*, or the two fight and the player can end up stopped.
        """
        was_paused = self._is_paused()
        target_s = max(0.0, target_ms / 1000.0)
        self.tempo.before_seek()
        started = utils.local_ms()
        self.player.seekTime(target_s)

        # Give the async seek a beat before polling, or a nearby
        # pre-seek position would satisfy the check immediately.
        xbmc.sleep(150)
        deadline = started + utils.SEEK_SETTLE_TIMEOUT * 1000

        landed = False

        while utils.local_ms() < deadline:
            if abs(self._position_ms() - target_ms) < 2000:
                landed = True
                break

            xbmc.sleep(100)

        if landed and not was_paused:
            # Only a seek issued while playing can be measured: a paused
            # player's clock never restarts, and polling it for the budget
            # held the group Unpause back by 2.5 s (measured +3.3 s residual
            # after the resume, on two of three members).
            # What a seek leaves behind is measured, not assumed. Kodi reports
            # the target the moment it accepts the seek and holds there while
            # the pipeline refills; the clock then restarts from wherever the
            # seek really landed. Both matter: the Tab took ~370 ms to restart
            # and landed 350 ms *early* (MediaCodec cannot drop to the accurate
            # point), so a seek aimed at the group's current position left it
            # ~700 ms behind — and a 2 s landing test saw none of it, which is
            # how the earlier estimate decayed to 150 ms. The residual a seek
            # leaves is restart time minus landing offset; smoothed, it is what
            # the next seek aims ahead by.
            restarted = self._clock_restart(target_ms)

            if restarted is not None:
                restart_ms, restart_pos = restarted
                lag = (restart_ms - started) - (restart_pos - target_ms)
                self.seek_lag_ms = 0.5 * self.seek_lag_ms + 0.5 * lag

        self.tempo.note_settle()

        if was_paused and stay_paused:
            # PAPlayer::SeekTime() unconditionally restores playback speed,
            # silently resuming a paused music player, and Android's
            # VideoPlayer does the same (OnPlayBackStarted right after the
            # seek — the fork's comment claimed otherwise). The resume can
            # also land after the settle loop, so on audio watch a short
            # window before trusting the state.
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
