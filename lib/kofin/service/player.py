"""Playback reporting: claim resolved plays and report sessions to Jellyfin.

The player owns its progress ticker (10s cadence) — the service loop does no
playback polling. Foreign playback (anything not queued on kofin.play.json)
is ignored entirely.
"""

import json
import threading
from typing import Any, Dict, Optional

import xbmc

from kofin.core import state
from kofin.core.api import Api
from kofin.core.log import Logger

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

PROGRESS_INTERVAL_SECONDS = 10.0
CLAIM_TIMEOUT_SECONDS = 10.0


class Player(xbmc.Player):
    def __init__(self, api: Api) -> None:
        super().__init__()
        self.api = api
        self._item: Optional[JsonDict] = None
        self._ticker: Optional[_Ticker] = None
        self._lock = threading.Lock()

    # -- kodi callbacks ------------------------------------------------------

    def onPlayBackStarted(self) -> None:
        self.finalize()  # a previous kofin play that never got its stop event
        claimed = self._claim()
        if claimed is None:
            return
        with self._lock:
            self._item = claimed
        state.set_playing_id(claimed["Id"])
        LOG.info("--> play %s (%s)", claimed["Id"], claimed["PlayMethod"])
        self._report(self.api.session_playing, event=None)
        self._start_ticker()

    def onPlayBackPaused(self) -> None:
        self._set_paused(True)

    def onPlayBackResumed(self) -> None:
        self._set_paused(False)

    def onPlayBackSeek(self, time: int, seekOffset: int) -> None:
        if self._item is not None:
            self._update_position(time / 1000.0)
            self._report(self.api.session_progress, event="timeupdate")

    def onPlayBackStopped(self) -> None:
        self.finalize()

    def onPlayBackEnded(self) -> None:
        self.finalize()

    def onPlayBackError(self) -> None:
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

    def finalize(self) -> None:
        """Report the stop and release all playback state."""
        self._stop_ticker()
        with self._lock:
            item = self._item
            self._item = None
        if item is None:
            return
        LOG.info("<-- stop %s", item["Id"])
        try:
            self.api.session_stopped(
                {
                    "ItemId": item["Id"],
                    "MediaSourceId": item["MediaSourceId"],
                    "PlaySessionId": item["PlaySessionId"],
                    "PositionTicks": int(item["CurrentPosition"] * 10_000_000),
                }
            )
        except Exception as error:
            LOG.warning("stop report failed: %s", error)
        if item.get("PlayMethod") == "Transcode":
            try:
                self.api.close_transcode(item["DeviceId"], item["PlaySessionId"])
            except Exception as error:
                LOG.debug("close transcode failed: %s", error)
        state.clear_playing_id()

    def stop_threads(self) -> None:
        """Service shutdown: stop the ticker without reporting."""
        self._stop_ticker()

    # -- internals -------------------------------------------------------------

    def _claim(self) -> Optional[JsonDict]:
        monitor = xbmc.Monitor()
        waited = 0.0
        while waited < CLAIM_TIMEOUT_SECONDS:
            try:
                current_file = self.getPlayingFile()
            except RuntimeError:
                current_file = ""
            if current_file:
                claimed = state.claim_play_item(current_file)
                if claimed is not None:
                    return claimed
                # A file is playing but nothing is queued: foreign playback.
                return None
            if monitor.waitForAbort(0.5):
                return None
            waited += 0.5
        return None

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
        try:
            poster(data)
        except Exception as error:
            LOG.warning("playback report failed: %s", error)

    def _start_ticker(self) -> None:
        self._stop_ticker()
        self._ticker = _Ticker(self)
        self._ticker.start()

    def _stop_ticker(self) -> None:
        if self._ticker is not None:
            self._ticker.stop()
            self._ticker = None


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
    try:
        response = json.loads(
            xbmc.executeJSONRPC(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "Application.GetProperties",
                        "params": {"properties": ["volume", "muted"]},
                    }
                )
            )
        )
        result = response.get("result", {})
        return int(result.get("volume", 100)), bool(result.get("muted", False))
    except Exception:
        return 100, False
