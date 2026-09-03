"""Playback reporting, the segment engine, and everything else that hangs
off a kofin playback.

The player owns its progress ticker (10s cadence) — the service loop does no
playback polling. Foreign playback (anything not queued on kofin.play.json)
is ignored entirely, except that a library row Kodi started on its own is
claimed back from ``Player.OnPlay`` (``backfill_library_claim`` and the
module-level claim helpers) so it reports like any other.

Beside the reporter and the segment engine this file also holds: the lyrics
hand-off at playback start (``start_lyrics`` — published for a skin script,
or pushed onto the playing item's tag for a lyrics add-on); the default
audio and subtitle track selection for a transcode; the watched-to-end
offers (delete from the server, remove a download); and the wiring for late
subtitles and chapter thumbnails, which run on their own threads.

The segment engine is ``service/segments.py``'s ``SegmentEngine`` since P2.3 (one per
player, reset per playback, driven by the checker's 0.25 s tick) and the claim
helpers are ``service/libraryclaim.py``. The engine, for the record:
boundary-*crossing* detection on float positions (a coarse or late poll
cannot step over a segment), a pre-armed next boundary (one compare per
tick), recoverable dedup (seek out and back in re-offers), and a settle
window after our own skip seek so a lagging ``getTime()`` cannot re-trigger.
The overlay's lifetime is tick-driven — open at the crossing, auto-close
past the end, button actions on Kodi's GUI thread — no second monitor
thread. Play Next resolves the next episode up front and starts it through
kofin's own play path; no ``service.upnext`` anywhere.
"""

import queue
import threading
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

import xbmc
import xbmcgui

from kofin.core import lyrics as lyrics_render
from kofin.core import ipc, kodirpc, settings, state, streams, toast
from kofin.core.api import Api
from kofin.downloads import auto as downloads_auto
from kofin.core.http import JellyfinError
from kofin.core.log import Logger
from kofin.service import chapters, latesubs
from kofin.service.libraryclaim import (
    backfill_library_claim,
    downloaded_path,
    playing_jellyfin_id,
)
from kofin.service.segments import SegmentEngine
from kofin.service.ports import forward, spawn_once

if TYPE_CHECKING:
    from kofin.syncplay.manager import SyncPlayManager

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

PROGRESS_INTERVAL_SECONDS = 10.0
CLAIM_TIMEOUT_SECONDS = 10.0
# Grace for a claim that arrives from the Player.OnPlay notification instead
# of the play route (see backfill_library_claim): library-originated audio,
# and downloaded video playing as the local file its row points at (W1.7).
# Only until the notification lands.
BACKFILL_GRACE_SECONDS = 3.0

# How far into an item counts as having watched it, for the
# ``deleteAfterWatching`` offer. Jellyfin's own played threshold.
WATCHED_FRACTION = 0.9

# Item types the finished-watching delete offer applies to. A song or a music
# video reaching its end is not an invitation to delete anything.
DELETABLE_TYPES = ("Movie", "Episode")


def watched_to_end(item: JsonDict) -> bool:
    """True when this playback got close enough to the end to call it watched.

    Not "did Kodi fire onPlayBackEnded": Play Next autoplay hands over about a
    second before natural EOF, so the episode the viewer just finished ends as
    a *stopped* playback. A share of the runtime catches both, and matches the
    threshold the server itself uses to mark an item played. The last progress
    tick can be up to ``PROGRESS_INTERVAL_SECONDS`` stale, which at this
    threshold only matters for items under ~100 s.
    """
    runtime = float(item.get("Runtime") or 0) / 10_000_000
    if runtime <= 0:
        return False
    return float(item.get("CurrentPosition") or 0) >= runtime * WATCHED_FRACTION


# What lrclyrics shows as the attribution line. Setting it also tells the
# addon these lyrics did not come from one of its own scrapers.
LYRICS_SOURCE = "Jellyfin"

# The push is rejected until Kodi's current item is in place, which is not
# guaranteed at the instant the callback fires. Each miss costs a sleep, and
# the whole budget is the gap before the first frame, so this stays small.
LYRICS_PUSH_ATTEMPTS = 4
LYRICS_PUSH_RETRY_SECONDS = 0.05

# musicLyricsMode. Two ways to show the same lyrics, and they must not both
# run: with a lyrics addon installed, the skin overlay and the addon's own
# window would draw the same words twice.
LYRICS_OFF = 0
LYRICS_SKIN = 1  # publish for the skin to render (see core/state.py)
LYRICS_ADDON = 2  # hand to a lyrics addon via the playing item's music tag


class _Reporter(threading.Thread):
    """One FIFO worker for every server-bound playback report.

    Kodi delivers player callbacks on its announcement thread, which every
    addon shares: a network call there stalls player and monitor callbacks
    for all of them, for the transport's whole budget, whenever the server is
    away (audit finding #2). So callbacks capture their payload at event time
    — position and volume are read when the event happens, not when the
    network gets to it — and enqueue here, and this thread posts strictly in
    order: the server reads playing/progress/stopped as one session's story,
    and the single pipe is what keeps a stop from overtaking its start.
    """

    def __init__(self) -> None:
        super().__init__(name="kofin-reporter", daemon=True)
        self._jobs: "queue.Queue[Optional[Callable[[], None]]]" = queue.Queue()

    def submit(self, job: Callable[[], None]) -> None:
        self._jobs.put(job)

    def flush(self, timeout: float = 5.0) -> bool:
        """True once everything queued before this call has run (tests)."""
        done = threading.Event()
        self._jobs.put(lambda: done.set())
        return done.wait(timeout)

    def stop(self, timeout: float = 5.0) -> None:
        self._jobs.put(None)
        if self.is_alive():
            self.join(timeout=timeout)
            if self.is_alive():  # pragma: no cover - watchdog logging only
                LOG.warning("reporter did not drain within its deadline")

    def run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                break
            try:
                job()
            except Exception as error:
                LOG.warning("playback report failed: %s", error)


class Player(xbmc.Player):
    def __init__(self, api: Api) -> None:
        super().__init__()
        self.api = api
        self._item: Optional[JsonDict] = None
        self._ticker: Optional[_Ticker] = None
        self._lock = threading.Lock()
        # Driven by SyncPlay (phase 4); while True, Play Next is withheld —
        # the group queue is authoritative.
        self._syncplay_group_active = False
        # The SyncPlay manager, attached by the service when built.
        self.syncplay: Optional["SyncPlayManager"] = None
        # The segment engine, split out in P2.3: one per player, reset per
        # playback (service/segments.py).
        self.segments = SegmentEngine(self, api)
        # W4.1's one-shot: the item id whose 80% crossing already fired,
        # latched before the lookup so a failed resolve never retries.
        self._auto_next_latch = ""
        self._chapter_thumbs: Optional[chapters.ChapterThumbs] = None
        self._late_subs: Optional[latesubs.LateSubtitles] = None
        self._reporter = _Reporter()
        self._reporter.start()

    # A property so joining or leaving a group also withdraws or restores the
    # stream menu, without the SyncPlay manager having to know the menu exists:
    # it writes this attribute and nothing else (syncplay/manager.py is ported
    # fork code and stays that way). Changing audio means restarting playback,
    # which in a group would desync everyone else, so the entry disappears.
    @property
    def syncplay_group_active(self) -> bool:
        return self._syncplay_group_active

    @syncplay_group_active.setter
    def syncplay_group_active(self, active: bool) -> None:
        self._syncplay_group_active = active
        item = self.current_item()
        if item is not None:
            self._publish_streams(item)

    # -- syncplay forwarding ---------------------------------------------------

    def _syncplay_event(self, name: str, *args: Any) -> None:
        """Forward a player callback to SyncPlay without ever letting it
        break regular playback reporting."""
        forward(self.syncplay, name, *args)

    def current_item(self) -> Optional[JsonDict]:
        """The claimed play state of the current kofin playback (SyncPlay's
        identity source), or None for idle/foreign playback."""
        with self._lock:
            return self._item

    # -- lyrics ---------------------------------------------------------------

    def start_lyrics(self) -> None:
        """Show the playing song's Jellyfin lyrics. Never raises.

        Kodi's music database has no lyrics column, so lyrics cannot be synced
        into the library the way everything else is — they exist only on the
        playback that is running, and only while it runs. That holds whichever
        path a song was written with, so this one route covers both direct
        rows and the plugin:// rows musicTranscode writes.

        Two destinations, never both (see the LYRICS_* modes). The skin
        overlay is the seamless one; the lyrics-addon hand-off is what works
        on skins that draw nothing themselves.

        Timing only binds on the addon path: script.cu.lrclyrics searches on
        onAVStarted and memoises the result per song, and its own force
        refresh does not clear that memo, so lyrics arriving after it looked
        are ignored until the song leaves its cache. Hence this runs
        synchronously at the top of playback start, ahead of the claim, and
        the fetch behind it forfeits rather than stalls (see Api.lyrics).
        """
        mode = settings.get_int("musicLyricsMode")
        if mode == LYRICS_OFF:
            return
        try:
            self._start_lyrics(mode)
        except JellyfinError as error:
            LOG.debug("lyrics unavailable: %s", error)
        except Exception:
            LOG.exception("lyrics start failed")

    def _start_lyrics(self, mode: int) -> None:
        if not self.isPlayingAudio():
            return

        jellyfin_id = playing_jellyfin_id(self.getPlayingItem(), self.getPlayingFile())
        if jellyfin_id is None:
            return

        payload = self.api.lyrics(jellyfin_id)
        if mode == LYRICS_SKIN:
            self._publish_lyrics(payload, jellyfin_id)
        elif mode == LYRICS_ADDON:
            self._push_lyrics_to_tag(payload, jellyfin_id)

    def _publish_lyrics(self, payload: JsonDict, jellyfin_id: str) -> None:
        """Publish the lyrics and stop. Rendering them, and following the
        clock, belongs to script.kofin.lyrics -- kofin only ever knows how to
        fetch them at the right instant."""
        lines = lyrics_render.to_lines(payload)
        if not lines:
            return
        state.publish_lyrics([[start, text] for start, text in lines], jellyfin_id)
        LOG.info("--> lyrics %s (%d lines)", jellyfin_id, len(lines))

    def _reset_lyrics(self) -> None:
        """Release the overlay. Called from finalize, so lyrics never outlive
        the playback that fetched them — including the error and stale-play
        paths, which are the ones that would otherwise leave them stranded."""
        state.clear_lyrics()

    def _push_lyrics_to_tag(self, payload: JsonDict, jellyfin_id: str) -> None:
        text = lyrics_render.to_text(payload)
        if not text:
            return

        for attempt in range(LYRICS_PUSH_ATTEMPTS):
            if attempt:
                xbmc.sleep(int(LYRICS_PUSH_RETRY_SECONDS * 1000))
            # Built from the playing item so the path and the rest of the
            # music tag already match: Kodi accepts the update only for the
            # item it is playing, and applies the tag wholesale, so a partial
            # one would blank the now-playing display.
            push = self.getPlayingItem()
            # setInfo rather than InfoTagMusic.setLyrics: only setInfo marks
            # the tag loaded, and an unloaded tag gets re-read from the music
            # database on its way to the screen — which clears the lyrics,
            # there being no column to read them back from.
            push.setInfo("music", {"lyrics": text})
            push.setProperty("culrc.source", LYRICS_SOURCE)
            self.updateInfoTag(push)
            if xbmc.getInfoLabel("MusicPlayer.Lyrics").strip() == text.strip():
                LOG.info("--> lyrics %s (%d chars)", jellyfin_id, len(text))
                return

        LOG.debug("lyrics for %s did not land on the playing item", jellyfin_id)

    # -- kodi callbacks ------------------------------------------------------

    def onPlayBackStarted(self) -> None:
        # Before anything that can block this callback thread: a start that
        # must wait for a SyncPlay group is paused at its first instant, not
        # seconds later when the claim and round trip complete.
        self._syncplay_event("on_playback_started")
        self.finalize()  # a previous kofin play that never got its stop event
        # Ahead of the claim, and before anything else that blocks: the lyrics
        # addon searches on onAVStarted, so a round trip taken first has
        # already lost the race. After finalize only because that is what
        # releases the previous song's lyrics — it is a no-op on the normal
        # path, where the last playback stopped cleanly. See start_lyrics.
        self.start_lyrics()
        claimed = self._claim()
        if claimed is None:
            return
        with self._lock:
            self._item = claimed
        state.set_playing_id(claimed["Id"])
        self._publish_streams(claimed)
        LOG.info("--> play %s (%s)", claimed["Id"], claimed["PlayMethod"])
        self._report(self.api.session_playing, event=None)
        self._start_ticker()
        self._start_segment_engine(claimed)
        self._start_chapter_thumbs(claimed)
        self._start_late_subtitles(claimed)

    def onAVStarted(self) -> None:
        """First frame rendered: the SyncPlay Ready trigger, and the earliest
        moment Kodi's stream lists are populated enough to pick from."""
        self._syncplay_event("on_avstarted")
        self.apply_default_tracks()

    def onPlayBackPaused(self) -> None:
        self._syncplay_event("on_paused")
        self._set_paused(True)

    def onPlayBackResumed(self) -> None:
        self._syncplay_event("on_resumed")
        self._set_paused(False)

    def onPlayBackSeek(self, time: int, seekOffset: int) -> None:
        self._syncplay_event("on_seek", time / 1000.0)
        if self._item is not None:
            self._update_position(time / 1000.0)
            self._report(self.api.session_progress, event="timeupdate")
            self.segments.note_seek(time / 1000.0)

    def onPlayBackStopped(self) -> None:
        self._syncplay_event("on_stopped")
        self._finish()

    def onPlayBackEnded(self) -> None:
        self._syncplay_event("on_ended")
        self._finish()

    def onPlayBackError(self) -> None:
        self._syncplay_event("on_error")
        self.finalize()

    # -- reporting -----------------------------------------------------------

    def report_progress(self) -> None:
        """Ticker callback: refresh position and post progress."""
        if self._item is None:
            return
        try:
            self._update_position(self.getTime())
        except RuntimeError:  # nothing playing (race with stop)
            return
        self._report(self.api.session_progress, event="timeupdate")
        self._maybe_auto_next()

    def _maybe_auto_next(self) -> None:
        """W4.1: the 80% crossing of a downloaded episode queues the next
        keep-ahead. Runs on the ticker thread — the lookup is one bounded
        listing, and the ticker's next beat is ten seconds out anyway."""
        item = self._item
        if item is None or item.get("Type") != "Episode":
            return
        item_id = str(item.get("Id") or "")
        if not item_id or self._auto_next_latch == item_id:
            return
        runtime = float(item.get("Runtime") or 0) / 10_000_000
        position = float(item.get("CurrentPosition") or 0)
        if runtime <= 0 or position < runtime * downloads_auto.NEXT_TRIGGER_RATIO:
            return
        self._auto_next_latch = item_id
        try:
            downloads_auto.trigger_next(self.api, item)
        except Exception:  # pragma: no cover - never break the ticker
            LOG.exception("auto-next trigger failed for %s", item_id)

    def _finish(self) -> None:
        """A playback the viewer ended: report the stop, then make any
        finished-watching offer against what was playing.

        ``finalize`` clears ``_item``, so the item is captured first. Only the
        stop/end callbacks come through here — ``finalize``'s other caller
        (a stale play discovered at the next start) is cleanup, not a
        playback the viewer just finished.
        """
        item = self.current_item()
        self.finalize()
        if item is not None:
            if not self.offer_delete(item):
                # Never two stacked dialogs about one just-finished item:
                # the local-remove offer stands down when the server-delete
                # prompt ran (W4.5).
                self.offer_remove_download(item)

    def finalize(self) -> None:
        """Report the stop and release all playback state."""
        self.segments.reset()
        self._stop_ticker()
        self._stop_chapter_thumbs()
        self._stop_late_subtitles()
        self._reset_lyrics()
        self._auto_next_latch = ""
        with self._lock:
            item = self._item
            self._item = None
        if item is None:
            return
        LOG.info("<-- stop %s", item["Id"])
        stop_data = {
            "ItemId": item["Id"],
            "MediaSourceId": item["MediaSourceId"],
            "PlaySessionId": item["PlaySessionId"],
            "PositionTicks": int(item["CurrentPosition"] * 10_000_000),
        }
        close_encoding = item.get("PlayMethod") == "Transcode"
        device_id = item.get("DeviceId", "")
        play_session_id = item["PlaySessionId"]

        def post_stop() -> None:
            try:
                self.api.session_stopped(stop_data)
            except Exception as error:
                LOG.warning("stop report failed: %s", error)
                # The position is the user's viewing progress and this was
                # its only route to the server; park it for the next connect
                # rather than lose it (plan W2.4). Contained: a parking
                # failure must not skip the transcode close below.
                try:
                    from kofin.downloads import pending

                    pending.enqueue(
                        str(item["Id"]),
                        str(item.get("Type") or ""),
                        position_ticks=int(stop_data["PositionTicks"]),
                        snapshot=item.get("ServerUserData") or {},
                    )
                except Exception:  # pragma: no cover - defensive
                    LOG.exception("could not park the stop position")
            if close_encoding:
                try:
                    self.api.close_transcode(device_id, play_session_id)
                except Exception as error:
                    LOG.debug("close transcode failed: %s", error)

        self._reporter.submit(post_stop)
        state.clear_playing_id()
        state.clear_playing_streams()

    def stop_threads(self) -> None:
        """Service shutdown: stop the ticker and checker without reporting.

        Chapter thumbs are reverted too — the entries are per-play and this
        Player is their only owner, so they must not outlive it. The cost is
        a playback that survives a mid-play service restart losing its
        chapter tiles; the startup sweep would otherwise never dare touch
        them while that playback is live."""
        self.segments.reset()
        self._stop_ticker()
        self._stop_chapter_thumbs()
        self._stop_late_subtitles()
        self._reporter.stop()

    def submit_backfill(self, data: JsonDict) -> None:
        """Player.OnPlay's library-claim back-fill, off the notification
        thread: library_claim makes a server GET and opens kofin.db, and every
        song start with the server away blocked Kodi's notification dispatch
        for the transport's whole budget (audit finding #3). Rides the
        reporter so the claim lands before any report that follows it."""

        def run() -> None:
            try:
                backfill_library_claim(data, self.api)
            except Exception:
                LOG.exception("library claim back-fill failed")

        self._reporter.submit(run)

    # -- delete after watching -------------------------------------------------

    def offer_delete(self, item: JsonDict) -> bool:
        """Ask whether to delete an item the viewer just finished.

        Returns whether the prompt was raised. Both settings must be on: the
        Advanced tab's delete opt-in owns whether kofin deletes anything at
        all, and this is a sub-option of it.
        """
        if item.get("Type") not in DELETABLE_TYPES or not item.get("Id"):
            return False
        if not settings.get_bool("enableDelete"):
            return False
        if not settings.get_bool("deleteAfterWatching"):
            return False
        if not watched_to_end(item):
            return False
        if not item.get("CanDelete"):
            # The server's own answer for this account. Without it an account
            # with no EnableContentDeletion was asked "delete this?" after
            # every single episode, and told "Server request failed" every
            # time it said yes.
            return False
        # Off the Kodi callback thread: this dialog waits on a person, and
        # blocking that thread stalls the player callbacks behind it (Play
        # Next's handoff to the following episode arrives on it). Daemon, so a
        # prompt left open cannot hold up service shutdown.
        spawn_once(None, self._delete_prompt, "kofin-delete-prompt", item)
        return True

    def offer_remove_download(self, item: JsonDict) -> bool:
        """W4.5: the local sibling of ``offer_delete`` — a download watched
        to the end has its local copy removed, silently or after a prompt.

        One answer for every download, whoever queued it. It used to depend
        on origin: this path refused anything automatic and the retention
        sweep refused anything the user had asked for, so the same watched
        episode was treated two different ways depending on how it had
        arrived. The sweep still exists for what this path cannot see —
        something watched while the service was down, or outside kofin's
        player — and it only ever acts in the silent mode, because a dialog
        about an episode finished an hour ago is not a question anyone can
        answer.

        Video only, on ``DELETABLE_TYPES`` — the constant ``offer_delete``
        already gates on, whose whole reason for existing is that reaching
        the end of a song is not an invitation to delete anything. This
        path was the one of the three that never got the rule: the
        retention sweep skips a song (``store.VIDEO_MEDIA_TYPES``) and so
        does ``_mark_watched``, but a downloaded track played to its end
        raised "Remove download?" every time. The test is the *item's*
        Jellyfin type rather than the row's ``media_type`` because that is
        the vocabulary this method is handed and it is always populated —
        a row queued before the type was knowable carries ``""``, which
        would have made this refuse for video as well.
        """
        if item.get("Type") not in DELETABLE_TYPES:
            return False
        if not settings.get_bool("downloadsDeleteAfterWatching"):
            return False
        if not settings.get_bool("downloadsEnabled"):
            return False
        item_id = str(item.get("Id") or "")
        if not item_id or not watched_to_end(item):
            return False
        from kofin.downloads import store as downloads_store

        row = downloads_store.get(item_id)
        if row is None or row.state != downloads_store.DONE:
            return False
        if settings.get_bool("downloadsDeleteAutomatically"):
            LOG.info("removing watched download %s (automatic)", item_id)
            ipc.notify(ipc.DOWNLOAD_REMOVE, {"Ids": [item_id]})
            return True
        # The same thread shape as the delete prompt: a dialog waits on a
        # person, and Kodi's callback thread must never wait with it.
        spawn_once(
            None,
            self._remove_download_prompt,
            "kofin-remove-download-prompt",
            item_id,
            str(item.get("Name") or ""),
        )
        return True

    def _remove_download_prompt(self, item_id: str, name: str) -> None:
        if not xbmcgui.Dialog().yesno(
            settings.localized(30710),  # Remove download
            settings.localized(30714) % name,
        ):
            return
        ipc.notify(ipc.DOWNLOAD_REMOVE, {"Ids": [item_id]})

    def _delete_prompt(self, item: JsonDict) -> None:
        name = item.get("Name") or ""
        if not xbmcgui.Dialog().yesno(
            xbmc.getLocalizedString(117),  # Delete
            settings.localized(30506) % name,
        ):
            return
        try:
            self.api.delete_item(str(item["Id"]))
        except Exception as error:
            LOG.warning("delete after watching failed: %s", error)
            self._notify(settings.localized(30507), toast.ERROR)
            return
        # No refresh from here: the server's own library-changed event drives
        # the removal out of the Kodi database through the normal sync path.
        LOG.info("deleted %s after watching", item["Id"])

    # -- stream selection ------------------------------------------------------

    def _publish_streams(self, item: JsonDict) -> None:
        """Hand the claimed playback's streams to the context item.

        The play route resolved them; this moves them somewhere a later plugin
        invocation can read, and states in one word what the menu should
        offer so addon.xml can pick a label (see core/state.py). Never raises:
        a menu that fails to appear must not take the playback with it.
        """
        try:
            payload = dict(item.get("Streams") or {})
            if not payload:
                state.clear_playing_streams()
                return
            media_streams = payload.get("MediaStreams") or []
            attached = payload.get("Attached") or []
            method = str(item.get("PlayMethod") or "")
            payload["PlayMethod"] = method
            payload["Id"] = item.get("Id", "")
            payload["MediaSourceId"] = item.get("MediaSourceId", "")
            payload["AudioStreamIndex"] = item.get("AudioStreamIndex")
            # Both indices, not just audio: a burned-in subtitle is not a Kodi
            # track, so this is the only thing that tells the menu which row is
            # the one in the picture (core/streams.burned_subtitle).
            payload["SubtitleStreamIndex"] = item.get("SubtitleStreamIndex")
            offer = streams.menu_offer(media_streams, attached, method)
            if self._syncplay_group_active:
                offer = streams.OFFER_NONE
            state.publish_playing_streams(payload, offer)
        except Exception:
            LOG.exception("publishing playing streams failed")

    def republish_streams(self, item: JsonDict) -> None:
        """Re-publish a claimed item's streams after they changed under it.

        The late-subtitle chase extends the attached list mid-playback, and
        the context menu reads its copy off a window property — so without
        this the menu goes on mapping indices against the list the playback
        started with."""
        self._publish_streams(item)

    def apply_default_tracks(self) -> None:
        """Start on the tracks the Jellyfin *user profile* nominates.

        Kodi otherwise picks from its own language settings, so a viewer whose
        Jellyfin account asks for Japanese audio with English subtitles got
        neither. The server has already resolved both against that account and
        returns them on every MediaSource; nothing had ever applied them.

        Runs at first frame rather than at claim because Kodi's stream lists
        do not exist until then — and because running here cannot delay the
        picture, only follow it. Never raises.
        """
        if not settings.get_bool("honourJellyfinDefaultTracks"):
            return
        item = self.current_item()
        if item is None:
            return
        try:
            self._apply_default_tracks(item)
        except Exception:
            LOG.exception("applying Jellyfin default tracks failed")

    def _apply_default_tracks(self, item: JsonDict) -> None:
        payload = item.get("Streams") or {}
        media_streams = payload.get("MediaStreams") or []
        attached = payload.get("Attached") or []
        method = str(item.get("PlayMethod") or "")

        # Audio only on a direct play: a transcode carries the single track the
        # server already encoded to our request, so there is nothing to choose
        # and Kodi's only index is 0.
        if streams.is_direct(method):
            ordinal = streams.audio_ordinal(media_streams, item.get("AudioStreamIndex"))
            if ordinal is not None:
                # setAudioStream seeks the demuxer even for a no-op. On an HTTP
                # Matroska that seek can miss and land at EOF, so do not
                # re-select a track Kodi is already playing.
                if kodirpc.current_audio() == ordinal:
                    LOG.debug("audio track %s already current", ordinal)
                else:
                    self.setAudioStream(ordinal)
                    LOG.info("--> audio track %s (Jellyfin default)", ordinal)

        wanted = item.get("SubtitleStreamIndex")
        if wanted is None or int(wanted) < 0:
            # The profile asks for no subtitle. Say so rather than leaving
            # whatever Kodi auto-selected running.
            self.showSubtitles(False)
            return
        if streams.burned_subtitle(media_streams, wanted, method):
            # It is already in the picture. Anything Kodi shows on top of it is
            # a second subtitle over the first, which is what an auto-selected
            # attached track would be here.
            self.showSubtitles(False)
            LOG.info("--> subtitle %s is burned in; Kodi's own left off", wanted)
            return
        ordinal = streams.subtitle_ordinal(media_streams, wanted, attached, method)
        if ordinal is None:
            return
        if kodirpc.current_subtitle() == ordinal:
            LOG.debug("subtitle track %s already current", ordinal)
            return
        self.setSubtitleStream(ordinal)
        self.showSubtitles(True)
        LOG.info("--> subtitle track %s (Jellyfin default)", ordinal)

    # -- segment engine: lifecycle -------------------------------------------

    def _start_segment_engine(self, item: JsonDict) -> None:
        self.segments.start(item)

    def _notify(self, message: str, level: str = toast.INFO) -> None:
        toast.show(message, level, time_ms=3000)

    def _claim(self) -> Optional[JsonDict]:
        """The claim for the playback that just started, or None.

        Runs inside onPlayBackStarted, i.e. on the service's own thread
        (Kodi delivers Python player and monitor callbacks on the thread
        that created the object, from inside its Kodi API calls), so every
        beat waited here delays onAVStarted — the SyncPlay Ready trigger and
        apply_default_tracks. The ``waitForAbort`` in the grace loop is also
        what delivers the pending Player.OnPlay notification whose backfill
        this waits for, so the grace is satisfiable, and the wait is bounded
        by the backfill's server GET — or the full grace when the server is
        away. The log line at the end is the measurement audit F6 asked for
        before restructuring: outcome, elapsed and kind, one line per start.
        """
        monitor = xbmc.Monitor()
        waited = 0.0
        outcome = "timed out"
        kind = "none"
        claimed: Optional[JsonDict] = None
        while waited < CLAIM_TIMEOUT_SECONDS:
            try:
                current_file = self.getPlayingFile()
            except RuntimeError:
                current_file = ""
            if current_file:
                if self.isPlayingAudio():
                    kind = "audio"
                elif downloaded_path(current_file):
                    kind = "downloaded"
                else:
                    kind = "video"
                claimed = state.claim_play_item(current_file)
                if claimed is not None:
                    outcome = "claimed"
                    break
                # Two kinds of playback never pass through the play route
                # and get their claim back-filled from the Player.OnPlay
                # notification instead, which can land after this callback:
                # songs written as direct ``<server>/Audio/<id>/`` stream
                # URLs, and downloaded video whose row is a local file
                # (W1.7). Wait a beat for the back-fill rather than calling
                # either foreign playback.
                if (
                    kind != "video"
                    and waited < BACKFILL_GRACE_SECONDS
                    and not monitor.waitForAbort(0.25)
                ):
                    waited += 0.25
                    continue
                # A file is playing but nothing is queued: foreign playback
                # — or, for the two back-filled kinds, a back-fill that never
                # landed inside the grace.
                outcome = "foreign" if kind == "video" else "backfill missed"
                break
            if monitor.waitForAbort(0.5):
                outcome = "aborted"
                break
            waited += 0.5
        LOG.info("claim %s after %.2fs (%s)", outcome, waited, kind)
        return claimed

    def _set_paused(self, paused: bool) -> None:
        if self._item is None:
            return
        self._item["Paused"] = paused
        self._report(self.api.session_progress, event="pause" if paused else "unpause")

    def _update_position(self, seconds: float) -> None:
        if self._item is not None and seconds >= 0:
            self._item["CurrentPosition"] = seconds

    def _report(self, poster: Any, event: Optional[str]) -> None:
        item = self._item
        if item is None:
            return
        if state.is_offline():
            # Offline claims exist for the local machinery (W4.7); a session
            # post every ten seconds would ride the ladder to nowhere for a
            # session the server cannot see. The stop report is not gated —
            # its failure path parks the position for the replay (W2.4).
            return
        volume, muted = _volume_state()
        data: JsonDict = {
            "QueueableMediaTypes": "Video,Audio",
            "CanSeek": True,
            "ItemId": item["Id"],
            "MediaSourceId": item["MediaSourceId"],
            "PlayMethod": item["PlayMethod"],
            "PlaySessionId": item["PlaySessionId"],
            "PositionTicks": int(item["CurrentPosition"] * 10_000_000),
            "IsPaused": bool(item.get("Paused")),
            "IsMuted": muted,
            "VolumeLevel": volume,
            "AudioStreamIndex": item.get("AudioStreamIndex"),
            "SubtitleStreamIndex": item.get("SubtitleStreamIndex"),
        }
        if event:
            data["EventName"] = event
        # Payload is complete; the post itself must not run on this thread —
        # for callbacks it is Kodi's announcement thread (see _Reporter).
        self._reporter.submit(lambda: poster(data))

    def _start_ticker(self) -> None:
        self._stop_ticker()
        self._ticker = _Ticker(self)
        self._ticker.start()

    def _stop_ticker(self) -> None:
        if self._ticker is not None:
            self._ticker.stop()
            self._ticker = None

    def _start_chapter_thumbs(self, item: JsonDict) -> None:
        if not settings.get_bool("chapterImages"):
            return
        if not chapters.eligible(item):
            return
        self._stop_chapter_thumbs()  # belt: no owner leaks across claims
        self._chapter_thumbs = chapters.ChapterThumbs(self.api, item)
        self._chapter_thumbs.start()

    def _stop_chapter_thumbs(self) -> None:
        if self._chapter_thumbs is not None:
            self._chapter_thumbs.stop()
            self._chapter_thumbs = None

    def _start_late_subtitles(self, item: JsonDict) -> None:
        """Chase any subtitle the server had not finished extracting.

        Nothing to do on the normal path: a track the play route fetched is
        already attached, and this only exists for the cold-extraction case
        (service/latesubs.py)."""
        self._stop_late_subtitles()  # belt: no chase leaks across claims
        deferred = latesubs.deferred_of(item)
        if not deferred:
            return
        self._late_subs = latesubs.LateSubtitles(self.api.http, self, item, deferred)
        self._late_subs.start()

    def _stop_late_subtitles(self) -> None:
        if self._late_subs is not None:
            self._late_subs.stop()
            self._late_subs = None

    def fetch_subtitle(self, index: int) -> None:
        """AttachSubtitle IPC: the stream menu picked a track the transcode
        did not attach.

        Runs on Kodi's notification thread, so it must only start the chase —
        the fetch itself is an ffmpeg extraction on the server and takes tens
        of seconds (service/latesubs.py). Supersedes any chase in flight: a
        viewer who picks a second subtitle has changed their mind about the
        first, and only one of them can be on screen anyway."""
        item = self.current_item()
        if item is None:
            LOG.warning("subtitle %s requested with nothing playing", index)
            return
        attachment = latesubs.fetchable_of(item).get(index)
        if attachment is None:
            LOG.warning("subtitle %s is not one this playback can be handed", index)
            return
        self._stop_late_subtitles()
        self._late_subs = latesubs.LateSubtitles(
            self.api.http, self, item, [attachment], requested=True
        )
        self._late_subs.start()


class _Ticker(threading.Thread):
    def __init__(self, player: Player) -> None:
        super().__init__(name="kofin-progress")
        self._player = player
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()
        if self.is_alive():
            self.join(timeout=5)

    def run(self) -> None:
        while not self._halt.wait(PROGRESS_INTERVAL_SECONDS):
            try:
                self._player.report_progress()
            except Exception as error:  # pragma: no cover - defensive
                LOG.warning("progress tick failed: %s", error)


def _volume_state() -> "tuple[int, bool]":
    result = kodirpc.call(
        "Application.GetProperties", {"properties": ["volume", "muted"]}
    )
    try:
        return int(result.get("volume", 100)), bool(result.get("muted", False))
    except Exception:
        return 100, False
