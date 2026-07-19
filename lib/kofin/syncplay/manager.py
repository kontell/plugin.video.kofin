"""SyncPlay group membership and state mirror, ported from the fork.

Mechanical adaptations only (phase-4 plan §2): the fork's ``Jellyfin()``
client is kofin's ``Api``; ``window()`` flags become object attributes plus
the one ``player.syncplay_group_active`` write; ``translate()`` ids are
remapped to 30550+; the played-item identity comes from the service player's
claimed play state instead of the fork's played-info pipeline; HTTP 403
detection uses kofin's ``Unauthorized`` (its transport maps 401/403 there).
The dispatcher-thread ordering, the guard-flag echo suppression, the hold
choreography, and the membership lifecycle stay recognizably identical.
"""

import threading
import time
from contextlib import contextmanager
from queue import Queue

import xbmc
import xbmcgui

from kofin.core import settings, state
from kofin.core.http import JellyfinError, Unauthorized
from kofin.core.log import Logger
from kofin.syncplay import utils
from kofin.syncplay.playback import PlaybackController
from kofin.syncplay.timesync import TimeSync

#################################################################################################

LOG = Logger(__name__)

#################################################################################################


class SyncPlayManager(object):
    """SyncPlay group membership and state mirror (SYNCPLAY.md §5, §6, §9).

    Runs in the service process. WebSocket traffic arrives through
    service/remote.py and is processed sequentially on a dispatcher thread
    so message ordering is preserved and the websocket thread is never
    blocked.
    """

    def __init__(self, api, player):
        self.api = api
        self.player = player
        self.playback = PlaybackController(self, player)
        self.timesync = None

        self.group = None  # {"GroupId", "GroupName"}
        self.group_state = None
        self.members = []
        self.ignore_wait = False

        self.queue = []  # [(ItemId, PlaylistItemId)] mirror of the group queue
        self.queue_last_update = None
        self.current_item_id = None
        self.current_playlist_item_id = None

        # idle -> loading -> waiting_ready -> synced
        self.phase = "idle"

        self.join_local_ms = 0.0
        self.last_group_id = None
        self._last_rejoin = 0.0
        self._last_ping_report = 0.0
        self._join_pending_since = 0.0
        self._pending_local_queue = False

        # A user-initiated start paused at its first instant, awaiting the
        # group echo: {"transition", "proposed", "item_id"}. "transition"
        # marks a native playlist advance (starts at 0); otherwise the
        # start position is read from the player once it settles.
        self._hold = None

        self._prog_depth = 0
        self._prog_release = 0.0
        self._prog_lock = threading.Lock()

        self._inbox = Queue()  # type: Queue
        self._running = True
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="kofin-syncplay-dispatch"
        )
        self._dispatcher.daemon = True
        self._dispatcher.start()

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def stop(self):
        if self.in_group():
            try:
                self._api_raw("syncplay_leave")
            except Exception:
                pass

        self._running = False
        self._inbox.put(None)
        self._leave_locally()
        self._dispatcher.join(timeout=5)
        if self._dispatcher.is_alive():  # pragma: no cover - watchdog only
            LOG.warning("syncplay dispatcher did not stop within deadline")

    def on_notification(self, method, data):
        """Entry point from the websocket routing; returns immediately."""
        self._inbox.put((method, data))

    def _post(self, func, *args):
        """Run a callable on the dispatcher thread."""
        self._inbox.put((func, args))

    def _dispatch_loop(self):
        LOG.info("--->[ syncplay dispatcher ]")

        while self._running:
            entry = self._inbox.get()

            if entry is None:
                break

            try:
                method, data = entry

                if callable(method):
                    method(*data)
                elif method == "SyncPlayCommand":
                    self._handle_command(data)
                elif method == "SyncPlayGroupUpdate":
                    self._handle_group_update(data)
                elif method == "WebSocketConnected":
                    self._on_ws_connected()
            except Exception as error:
                LOG.exception("SyncPlay dispatch error: %s", error)

        LOG.info("---<[ syncplay dispatcher ]")

    def get_api(self):
        return self.api

    def _api(self, name, *args):
        """SyncPlay REST with the §9 reaction to a lost session (401/403 on
        kofin's transport): one rate-limited automatic re-join before
        giving up."""
        api_client = self.get_api()

        if api_client is None:
            return None

        try:
            return getattr(api_client, name)(*args)
        except Unauthorized:
            if self.in_group():
                LOG.info("SyncPlay %s unauthorized, attempting rejoin", name)
                self._post(self._attempt_rejoin)
            else:
                LOG.warning("SyncPlay %s unauthorized", name)
        except Exception as error:
            LOG.warning("SyncPlay %s failed: %s", name, error)

        return None

    def _api_raw(self, name, *args):
        """SyncPlay REST that surfaces HTTP errors to the caller."""
        api_client = self.get_api()

        if api_client is None:
            return None

        return getattr(api_client, name)(*args)

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def in_group(self):
        return self.group is not None

    def enabled(self):
        return settings.get_bool("syncPlayEnabled")

    def offset_ms(self):
        return self.timesync.offset_ms if self.timesync else 0.0

    def server_now_ms(self):
        return utils.local_ms() + self.offset_ms()

    def server_now_iso(self):
        return utils.to_iso(self.server_now_ms())

    def join_server_ms(self):
        """Join instant on the server clock; tracks time sync refinement."""
        return self.join_local_ms + self.offset_ms()

    def get_utc_time(self):
        try:
            return self._api_raw("get_utc_time")
        except Exception as error:
            LOG.debug("GetUtcTime failed: %s", error)
            return None

    def on_timesync_update(self):
        """Report our ping after accepted measurements (SYNCPLAY.md §3)."""
        if not self.in_group() or self.timesync is None:
            return

        now = time.time()

        if now - self._last_ping_report < utils.TIMESYNC_INTERVAL - 5:
            return

        if self.timesync.ping_ms is not None:
            self._last_ping_report = now
            self._api("syncplay_ping", int(self.timesync.ping_ms))

    @contextmanager
    def programmatic(self):
        """Marks player actions we cause, so their Kodi callbacks are not
        forwarded to the group as user requests."""
        with self._prog_lock:
            self._prog_depth += 1

        try:
            yield
        finally:
            with self._prog_lock:
                self._prog_depth -= 1
                self._prog_release = time.time()

    def is_programmatic(self):
        with self._prog_lock:
            if self._prog_depth > 0:
                return True

            return (time.time() - self._prog_release) < utils.PROGRAMMATIC_ECHO_GRACE

    def _local_file_info(self):
        """The claimed play state of the current kofin playback, or None.

        kofin's service player claims every play resolved through the
        plugin (kofin.play.json), so the claim *is* the identity pipeline
        the fork read from its played-info dict.
        """
        try:
            if not self.player.isPlaying():
                return None

            return self.player.current_item()
        except Exception:
            pass

        return None

    def _local_item_id(self):
        info = self._local_file_info()

        if info:
            return info.get("Id")

        return state.get_playing_id() or None

    def is_transcoding(self):
        info = self._local_file_info()
        return bool(info) and info.get("PlayMethod") == "Transcode"

    def post_report(self, kind, position_s=None):
        """Ready/Buffering report with our actual position (SYNCPLAY.md §4).

        position_s overrides the live clock for reports that promise a
        position we are committed to landing on (a deferred audio seek).
        """
        if not self.in_group() or self.current_playlist_item_id is None:
            return

        position = position_s

        if position is None:
            try:
                position = self.player.getTime()
            except Exception:
                position = 0.0

        is_playing = self.player.isPlaying() and not xbmc.getCondVisibility(
            "Player.Paused"
        )
        self._api(
            kind,
            self.server_now_iso(),
            utils.seconds_to_ticks(position),
            bool(is_playing),
            self.current_playlist_item_id,
        )

    def _text(self, string_id, value=None):
        """localized() returns '' for unknown ids; never let formatting
        break a message handler."""
        template = settings.localized(string_id)

        if value is None:
            return template

        try:
            return template % value
        except (TypeError, ValueError):
            return "%s %s" % (template, value)

    def _toast(self, message, error=False):
        if not settings.get_bool("syncPlayNotifications") and not error:
            return

        try:
            xbmcgui.Dialog().notification(
                "SyncPlay",
                message,
                xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO,
                3000,
                False,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Group membership (menu-facing)
    # ------------------------------------------------------------------

    def list_groups(self):
        try:
            return self._api_raw("syncplay_list") or []
        except Exception as error:
            LOG.warning("SyncPlay list failed: %s", error)
            return None

    def refresh_group_info(self):
        """Refresh the member list for the menu."""
        if not self.in_group():
            return

        for group in self.list_groups() or []:
            if group.get("GroupId") == self.group["GroupId"]:
                self.members = (
                    group.get("Members") or group.get("Participants") or self.members
                )
                self.group["GroupName"] = (
                    group.get("GroupName") or self.group["GroupName"]
                )

    def join_group(self, group_id):
        self._join_pending_since = time.time()

        try:
            self._api_raw("syncplay_join", group_id)
        except JellyfinError as error:
            LOG.warning("SyncPlay join failed: %s", error)
            self._toast(settings.localized(30578), error=True)
            return False
        except Exception as error:
            LOG.warning("SyncPlay join failed: %s", error)
            self._toast(settings.localized(30578), error=True)
            return False

        self._watch_join_feedback()
        return True

    def new_group(self, group_name):
        self._join_pending_since = time.time()
        self._pending_local_queue = True

        try:
            self._api_raw("syncplay_new", group_name)
        except Exception as error:
            LOG.warning("SyncPlay new group failed: %s", error)
            self._pending_local_queue = False
            self._toast(settings.localized(30578), error=True)
            return False

        self._watch_join_feedback()
        return True

    def leave_group(self):
        try:
            self._api_raw("syncplay_leave")
        except Exception as error:
            LOG.warning("SyncPlay leave failed: %s", error)

        self._leave_locally()
        self._toast(settings.localized(30564))

    def toggle_spectator(self):
        self.ignore_wait = not self.ignore_wait
        self._api("syncplay_set_ignore_wait", self.ignore_wait)
        self._toast(
            settings.localized(30569) if self.ignore_wait else settings.localized(30583)
        )

        if not self.ignore_wait:
            # Coming back from spectator mode: re-attach so the server
            # pushes the group state again now, instead of us only
            # catching up at the group's next queue change.
            self._attempt_rejoin(force=True)

    def request_resync(self):
        """Menu 'resync': re-join re-attaches the session and the server
        pushes the group state again (§4, §9)."""
        if not self.in_group():
            return

        self._attempt_rejoin(force=True)

    def _watch_join_feedback(self):
        """The feedback plane is mandatory: joining without a working
        WebSocket would mean never receiving a single command (§1)."""

        def check():
            if self.in_group() or not self._join_pending_since:
                return

            LOG.warning("No GroupJoined update within 10s of joining")
            self._toast(settings.localized(30570), error=True)

            try:
                self._api_raw("syncplay_leave")
            except Exception:
                pass

        timer = threading.Timer(10, self._post, args=(check,))
        timer.daemon = True
        timer.start()

    # ------------------------------------------------------------------
    # WebSocket message handling (dispatcher thread)
    # ------------------------------------------------------------------

    def _handle_group_update(self, data):
        gtype = data.get("Type")
        payload = data.get("Data")

        if gtype == "GroupJoined":
            self._on_group_joined(payload or {})
            return

        if gtype in ("NotInGroup", "GroupDoesNotExist"):
            if self.in_group():
                self._attempt_rejoin()

            return

        if gtype == "LibraryAccessDenied":
            self._toast(settings.localized(30579), error=True)
            return

        if not self.in_group():
            return

        if data.get("GroupId") and data["GroupId"] != self.group["GroupId"]:
            return

        if gtype == "GroupLeft":
            self._leave_locally()
            self._toast(settings.localized(30564))
            return

        if gtype == "UserJoined":
            self._toast(self._text(30565, payload))
            return

        if gtype == "UserLeft":
            self._toast(self._text(30566, payload))
            return

        if gtype == "StateUpdate":
            previous = self.group_state
            self.group_state = (payload or {}).get("State")
            LOG.debug(
                "[ syncplay/state ] %s (%s)",
                self.group_state,
                (payload or {}).get("Reason"),
            )

            if self.group_state == "Waiting" and previous == "Playing":
                # A member dropped out or started buffering: the whole
                # group holds. Surface why playback froze (S4.5).
                self._toast(settings.localized(30585))
        elif gtype == "PlayQueue":
            self._apply_play_queue(payload or {})
        else:
            LOG.debug("Unhandled group update type: %s", gtype)

    def _handle_command(self, command):
        """SyncPlayCommand gating per SYNCPLAY.md §5.1."""
        if not self.in_group():
            return

        if command.get("GroupId") and command["GroupId"] != self.group["GroupId"]:
            LOG.info("Discarding command for another group")
            return

        emitted = utils.parse_iso_ms(command.get("EmittedAt"))

        if emitted is not None and emitted < self.join_server_ms() - 2000:
            LOG.info("Discarding command emitted before our join")
            return

        if (
            command.get("Command") != "Stop"
            and command.get("PlaylistItemId")
            and command["PlaylistItemId"] != self.current_playlist_item_id
        ):
            LOG.info(
                "Command for another queue item (%s != %s)",
                command.get("PlaylistItemId"),
                self.current_playlist_item_id,
            )
            return

        self.playback.schedule(command)

    def _on_group_joined(self, info):
        if not self.enabled():
            LOG.info("SyncPlay is disabled in settings, ignoring GroupJoined")
            return

        rejoined = self.in_group() and self.group["GroupId"] == info.get("GroupId")

        self.group = {
            "GroupId": info.get("GroupId"),
            "GroupName": info.get("GroupName") or "",
        }
        self.last_group_id = info.get("GroupId")
        self.group_state = info.get("State")
        # Members (with per-member state) is a newer server field; older
        # servers only send Participants name strings.
        self.members = info.get("Members") or info.get("Participants") or []
        self.queue_last_update = None
        self.join_local_ms = utils.local_ms()
        self._join_pending_since = 0.0
        self.ignore_wait = False

        # The phase-3 stub is now driven: while True, Play Next and the
        # cinema/near-end prompts are withheld (the group queue is
        # authoritative).
        self.player.syncplay_group_active = True

        if self.timesync is None:
            self.timesync = TimeSync(self)
            self.timesync.start()
        else:
            self.timesync.force_update()

        self.playback.start_loop()

        LOG.info("--->[ syncplay group/%s ]", self.group["GroupId"])

        if not rejoined:
            self._toast(self._text(30563, self.group["GroupName"] or "?"))

        if self._pending_local_queue:
            self._pending_local_queue = False
            self._post(self._forward_local_play)

    def _leave_locally(self):
        if not self.in_group() and self.phase == "idle":
            return

        self.playback.stop_loop()

        if self.timesync is not None:
            self.timesync.stop()
            self.timesync = None

        self.group = None
        self.group_state = None
        self.members = []
        self.queue = []
        self.queue_last_update = None
        self.current_item_id = None
        self.current_playlist_item_id = None
        self.phase = "idle"
        self.ignore_wait = False
        self._join_pending_since = 0.0
        self.player.syncplay_group_active = False
        self._release_hold()
        LOG.info("---<[ syncplay group ]")

    def _attempt_rejoin(self, force=False):
        """§9: one automatic re-join before surfacing an error."""
        if not self.last_group_id:
            self._leave_locally()
            return

        now = time.time()

        if not force and now - self._last_rejoin < utils.AUTO_REJOIN_INTERVAL:
            return

        self._last_rejoin = now
        LOG.info("Attempting SyncPlay rejoin of %s", self.last_group_id)

        try:
            self._api_raw("syncplay_join", self.last_group_id)
        except Exception as error:
            LOG.warning("SyncPlay rejoin failed: %s", error)
            self._leave_locally()
            self._toast(settings.localized(30571), error=True)

    def _on_ws_connected(self):
        """Reconnect contract (§9, report R2): after any WS drop assume we
        were kicked — current servers end the session on a socket close.
        Probe GET /SyncPlay/List: if the group still exists, re-join to
        re-attach and receive the group state again; if it is gone,
        stop pretending to be in it."""
        if not self.in_group():
            return

        if self.timesync is not None:
            self.timesync.force_update()

        self._kicked_probe()

    def _kicked_probe(self):
        groups = self.list_groups()

        if groups is None:
            # Server unreachable right now; the next reconnect re-probes.
            LOG.debug("kicked-probe skipped: group list unavailable")
            return

        group_id = self.group["GroupId"] if self.in_group() else self.last_group_id

        if any(group.get("GroupId") == group_id for group in groups):
            self._attempt_rejoin(force=True)
            return

        LOG.info("Group %s no longer exists after reconnect", group_id)
        self._leave_locally()
        self._toast(settings.localized(30571), error=True)

    # ------------------------------------------------------------------
    # Queue mirror (SYNCPLAY.md §5.3, report §9.5.1)
    # ------------------------------------------------------------------

    def _apply_play_queue(self, data):
        last_update = utils.parse_iso_ms(data.get("LastUpdate"))

        if (
            last_update is not None
            and self.queue_last_update is not None
            and last_update <= self.queue_last_update
        ):
            LOG.debug("Ignoring play queue not newer than the applied one")
            return

        self.queue_last_update = last_update
        playlist = data.get("Playlist") or []
        self.queue = [
            (entry.get("ItemId"), entry.get("PlaylistItemId")) for entry in playlist
        ]
        index = data.get("PlayingItemIndex", -1)
        is_playing = bool(data.get("IsPlaying"))
        start_ticks = data.get("StartPositionTicks") or 0

        LOG.info(
            "[ syncplay/queue ] %s items, playing %s (%s)",
            len(self.queue),
            index,
            data.get("Reason"),
        )

        if index is None or index < 0 or index >= len(self.queue):
            self._detach_playback(stop_media=True)
            return

        item_id, playlist_item_id = self.queue[index]

        if playlist_item_id == self.current_playlist_item_id and self.phase != "idle":
            return  # tail-only change; drift reference stays authoritative

        # Position reference: extrapolate from LastUpdate while playing.
        reference_ms = last_update if last_update is not None else self.server_now_ms()
        self.playback.set_reference(start_ticks, reference_ms, is_playing)

        held = self._hold
        held_match = held is not None and held.get("item_id") == item_id

        if (
            item_id
            and self.player.isPlaying()
            and (item_id == self._local_item_id() or held_match)
        ):
            # We are already playing this exact item (e.g. the queue we
            # just proposed with SetNewQueue came back with a fresh
            # PlaylistItemId): adopt it. Never tear down and reload media
            # that is already on screen, regardless of phase. A held start
            # matches on the id we proposed, since the play pipeline may
            # not have claimed the new item yet.
            LOG.info("Adopting queue identity for the playing item")
            self._hold = None
            self.current_item_id = item_id
            self.current_playlist_item_id = playlist_item_id
            self.phase = "waiting_ready"
            self.playback.ensure_paused()
            self._post(self.playback.prepare_ready)
            return

        if self.ignore_wait and self.player.isPlaying():
            # A spectator watching their own thing: the group's queue
            # must not tear it down. They re-attach via the menu (or
            # automatically once their player is idle again).
            LOG.info("Spectator playing own media; not following the queue")
            return

        self._start_item(item_id, playlist_item_id)

    def _start_item(self, item_id, playlist_item_id):
        if not self.in_group():  # left while this update was in flight
            return

        self.phase = "loading"
        self.current_item_id = item_id
        self.current_playlist_item_id = playlist_item_id

        estimate_ms = self.playback.estimate_position_ms() or 0
        LOG.info(
            "[ syncplay/play ] %s (%s) at %.1fs",
            item_id,
            playlist_item_id,
            estimate_ms / 1000.0,
        )

        try:
            # Not via _api: a 403 here is a library permission problem,
            # not lost group membership.
            item = self._api_raw("item", item_id)
        except Exception as error:
            LOG.warning("SyncPlay item lookup failed: %s", error)
            item = None

        if not item:
            self._load_failed("item lookup failed")
            return

        try:
            self.playback.play_item(item, utils.ms_to_ticks(estimate_ms))
        except Exception as error:
            LOG.exception("SyncPlay playback start failed: %s", error)
            self._load_failed(error)
            return

        expected = playlist_item_id

        def check():
            if self.phase == "loading" and self.current_playlist_item_id == expected:
                self._load_failed("no playback within 45s")

        timer = threading.Timer(45, self._post, args=(check,))
        timer.daemon = True
        timer.start()

    def _load_failed(self, reason):
        LOG.warning("SyncPlay could not start playback: %s", reason)
        self._toast(settings.localized(30576), error=True)

        try:
            self._api_raw("syncplay_leave")
        except Exception:
            pass

        self._leave_locally()

    def _detach_playback(self, stop_media=False):
        was_active = self.phase != "idle"
        self._hold = None
        self.current_item_id = None
        self.current_playlist_item_id = None
        self.phase = "idle"
        self.playback.cancel_pending()
        self.playback.last_command = None

        # Only kill media that SyncPlay itself is driving: joining an
        # idle group must not stop whatever the user was watching.
        if stop_media and was_active:
            self.playback.stop_media()

    def on_group_stopped(self):
        """A Stop command was executed."""
        self.current_item_id = None
        self.current_playlist_item_id = None
        self.phase = "idle"

    def on_local_unpaused(self):
        """An Unpause command was executed against loaded media."""
        if self.phase in ("waiting_ready", "loading"):
            self.phase = "synced"

    # ------------------------------------------------------------------
    # Player events (called from service/player.py hooks)
    # ------------------------------------------------------------------

    def on_playback_started(self):
        """Earliest signal of a local item start, before A/V rolls.

        Runs on the player callback thread the instant playback begins
        (including a native playlist advance), so a start that must wait
        for the group is paused before it plays audibly rather than a few
        seconds in when the round trip completes.
        """
        if not self.in_group():
            return

        if self.phase == "loading":
            # Our own play_item(): try to hold before the first frame;
            # on_avstarted re-ensures once the player is fully up.
            self.playback.ensure_paused()
            return

        if self.is_programmatic():
            return

        if self.ignore_wait:
            # A spectator's own plays stay local: nothing is proposed to
            # the group, so there is nothing to hold (or demote) either.
            return

        if self.phase in ("idle", "synced"):
            # Phase "synced" means no stop intervened since the synced
            # item: a native playlist advance, which starts at 0. Phase
            # "idle" is a fresh user start (possibly at a resume point).
            hold = {
                "transition": self.phase == "synced",
                "proposed": False,
                "item_id": None,
            }
            self._hold = hold
            self.playback.ensure_paused()
            self._watch_hold(hold)

    def on_kodi_play(self, data):
        """Player.OnPlay from the Kodi bus: carries the Kodi library id,
        the earliest identification of a local start (before the play
        pipeline claims the item). Called on the notification thread."""
        self._post(self._identify_held_play, data)

    def on_avstarted(self):
        if not self.in_group():
            return

        if self.phase == "loading":
            # Hold the first frame; the group start is choreographed by
            # the server once every member reports Ready.
            self.playback.ensure_paused()
            self.phase = "waiting_ready"
            self._post(self.playback.prepare_ready)
            return

        if self.ignore_wait:
            return  # a spectator's own plays are not forwarded

        hold = self._hold

        if hold is not None and not hold["proposed"]:
            # The early pause is best-effort while the player is still
            # opening; with A/V up it always lands. Our own pause above
            # must not swallow the forward via the programmatic grace.
            self.playback.ensure_paused()
            self._post(self._forward_local_play)
            return

        if not self.is_programmatic() and self.phase in ("idle", "synced"):
            self._post(self._forward_local_play)

    def on_paused(self):
        if not self.in_group() or self.is_programmatic():
            return

        if self.phase == "synced":
            LOG.info("[ syncplay/user pause ]")
            self._post(self._api, "syncplay_pause")

    def on_resumed(self):
        if not self.in_group() or self.is_programmatic():
            return

        if self.phase in ("synced", "waiting_ready"):
            LOG.info("[ syncplay/user unpause ]")
            # The group start time is the server's call: hold position
            # and ask for a group unpause instead of running ahead.
            self.playback.ensure_paused()
            self._post(self._api, "syncplay_unpause")

    def on_seek(self, seconds):
        if not self.in_group() or self.is_programmatic():
            return

        if self.phase in ("synced", "waiting_ready"):
            LOG.info("[ syncplay/user seek ] %.1fs", seconds)
            self._post(self._api, "syncplay_seek", utils.seconds_to_ticks(seconds))

    def on_stopped(self):
        self._hold = None  # whatever was held is gone

        if not self.in_group() or self.is_programmatic():
            return

        if self.phase == "loading":
            return  # load-failure timer handles it

        if self.phase in ("waiting_ready", "synced"):
            self._detach_playback()
            thread = threading.Thread(target=self._user_stopped_prompt)
            thread.daemon = True
            thread.start()

    def on_ended(self):
        self._hold = None

        if not self.in_group() or self.is_programmatic():
            return

        if self.phase != "synced":
            return

        playlist_item_id = self.current_playlist_item_id
        self.phase = "idle"

        if playlist_item_id:
            LOG.info("[ syncplay/next item ]")
            self._post(self._api, "syncplay_next_item", playlist_item_id)

    def on_error(self):
        if not self.in_group():
            return

        LOG.warning("Playback error while in a SyncPlay group")
        self._detach_playback()
        self.ignore_wait = True
        self._post(self._api, "syncplay_set_ignore_wait", True)
        self._toast(settings.localized(30576), error=True)

    def _stop_superseded(self):
        """Did another local start (or a group item) replace the stop?"""
        return self._hold is not None or self.phase != "idle" or self.player.isPlaying()

    def _user_stopped_prompt(self):
        """Local stop while synced: sort out what it means for the group.

        A stop that is immediately followed by another local start is a
        replace-play (the user picked a new item), not a departure: give
        the new start a moment to appear and skip the prompt when it does.
        """
        deadline = time.time() + utils.STOP_PROMPT_GRACE

        while time.time() < deadline:
            if not self.in_group() or self._stop_superseded():
                return

            time.sleep(utils.STOP_PROMPT_POLL)

        if not self.in_group() or self._stop_superseded():
            return

        selection = xbmcgui.Dialog().select(
            settings.localized(30568),
            [
                settings.localized(30584),
                settings.localized(30580),
                settings.localized(30562),
            ],
        )

        if selection == 2:
            self.leave_group()
            return

        if not self.in_group() or self._stop_superseded():
            # Answered against a stale premise (something started while
            # the dialog was open): only an explicit leave still applies.
            return

        if selection == 0:
            # Stop playback for the whole group. Membership is kept, so
            # whatever is played next is proposed to everyone.
            self._api("syncplay_stop")
        else:
            # Explicit spectator choice — or the dialog was dismissed:
            # never leave the group silently waiting on this member.
            self.ignore_wait = True
            self._api("syncplay_set_ignore_wait", True)
            self._toast(settings.localized(30569))

    def _forward_local_play(self, attempt=0):
        """Propose the currently-playing item to the group as its queue."""
        if not self.player.isPlaying():
            # Nothing to propose — e.g. a group created before playback
            # starts. Stay idle and wait for the user to play something;
            # do not demote to spectator.
            return

        hold = self._hold

        if hold is not None and hold["proposed"]:
            return  # the fast identification path already proposed

        if hold is not None and hold["transition"]:
            # Mid-transition the playing-id window property can still name
            # the previous track; only trust the claimed play state, which
            # is keyed by the new file.
            info = self._local_file_info()
            item_id = info.get("Id") if info else None
        else:
            item_id = self._local_item_id()

        if (
            item_id
            and item_id == self.current_item_id
            and self.phase in ("waiting_ready", "synced")
        ):
            # Already the group's current item: a late-delivered start
            # event for a proposal whose echo was adopted meanwhile.
            return

        if not item_id:
            # The plugin play worker may still be resolving the item.
            if attempt >= utils.FORWARD_RETRY_LIMIT:
                self._unmanaged_local_play()
                return

            timer = threading.Timer(
                utils.FORWARD_RETRY_INTERVAL,
                self._post,
                args=(self._forward_local_play, attempt + 1),
            )
            timer.daemon = True
            timer.start()
            return

        if hold is not None:
            hold["proposed"] = True
            hold["item_id"] = item_id

            if hold["transition"]:
                self._propose_queue(item_id, position=0.0)
                return

        self._propose_queue(item_id)

    def _identify_held_play(self, data):
        """Identify a held playlist advance from the Player.OnPlay payload.

        The Kodi library id maps straight to the jellyfin id (kofin.db), so
        a transition can be proposed within milliseconds of the boundary
        instead of waiting for the play pipeline's claim. Fresh starts keep
        waiting for the player: their start position (a resume point) is
        only trustworthy once A/V is up.
        """
        hold = self._hold

        if not self.in_group() or hold is None or hold["proposed"]:
            return

        item = data.get("item") or {}
        kodi_id = item.get("id")
        media = item.get("type")

        if not kodi_id or kodi_id == -1 or not media:
            return

        from kofin.sync import db as database  # deferred: pulls in the DB stack

        try:
            mapped = database.get_item(kodi_id, media)
        except Exception as error:
            LOG.warning("SyncPlay kodi id lookup failed: %s", error)
            return

        if not mapped:
            # A Kodi library item with no jellyfin mapping: the group
            # cannot follow it, and the play pipeline (same mapping) will
            # not identify it either. Let it play now rather than after
            # the retry window.
            self._unmanaged_local_play()
            return

        if not hold["transition"]:
            return

        hold["proposed"] = True
        hold["item_id"] = mapped[0]
        self._propose_queue(mapped[0], position=0.0)

    def _unmanaged_local_play(self):
        """Playing something the group can't follow (a local file,
        another addon): give playback back and stop pretending to be
        synchronized. Quiet when already a spectator — repeat plays of
        unmanaged media must not re-toast on every start."""
        self._release_hold()
        self._detach_playback()

        if self.ignore_wait:
            return

        LOG.info("Unmanaged playback while in a group, detaching")
        self.ignore_wait = True
        self._api("syncplay_set_ignore_wait", True)
        self._toast(settings.localized(30569))

    def _watch_hold(self, hold):
        """A held start that never gets adopted must not stay paused
        forever (e.g. the proposal failed): give playback back."""

        def check():
            if self._hold is not hold:
                return

            LOG.warning("Held start was never adopted; releasing the hold")
            self._release_hold()

        timer = threading.Timer(utils.HOLD_RELEASE_TIMEOUT, self._post, args=(check,))
        timer.daemon = True
        timer.start()

    def _release_hold(self):
        if self._hold is None:
            return

        self._hold = None
        self.playback.ensure_playing()

    def _propose_queue(self, item_id, position=None):
        """Propose a queue; a held transition proposes 0 and the player is
        aligned on it at adopt time (prepare_ready), not here.

        External players cannot reach this path: kofin has no play-with
        flow, and the SyncPlay root entry is hidden when a
        playercorefactory override is installed (plugin/browse.py).
        """
        if position is None:
            try:
                position = self.player.getTime()
            except Exception:
                position = 0.0

        LOG.info("[ syncplay/set queue ] %s at %.1fs", item_id, position)
        self._api(
            "syncplay_set_new_queue",
            [item_id],
            0,
            utils.seconds_to_ticks(position),
        )

    # ------------------------------------------------------------------
    # Lifecycle from the service
    # ------------------------------------------------------------------

    def on_sleep(self):
        self.playback.cancel_pending()

    def on_wake(self):
        """Screensaver deactivate / system wake: never trust a stale clock
        offset (report §9.5.6), and re-probe the group — after a sleep the
        socket may be a zombie the server already kicked."""
        if self.timesync is not None:
            self.timesync.force_update(reset=True)

        if self.in_group():
            self._post(self._kicked_probe)
