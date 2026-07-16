"""Remote control: Play / Playstate / GeneralCommand from the websocket.

Runs on the websocket thread; every handler is a quick JSON-RPC call or
builtin. Unknown commands log and return — never raise.
"""

import json
from typing import Any, Dict, List

import xbmc
import xbmcgui

from kofin.core.log import Logger
from kofin.plugin.listitems import plugin_url

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# GeneralCommand names that map straight to a Kodi input action.
INPUT_ACTIONS = {
    "MoveUp": "up",
    "MoveDown": "down",
    "MoveLeft": "left",
    "MoveRight": "right",
    "Select": "select",
    "Back": "back",
    "ToggleContextMenu": "contextmenu",
    "ToggleOsdMenu": "osd",
    "PageUp": "pageup",
    "PageDown": "pagedown",
    "NextLetter": "nextletter",
    "PreviousLetter": "prevletter",
    "TakeScreenshot": "screenshot",
    "ToggleFullscreen": "togglefullscreen",
}


class RemoteHandler:
    def handle(self, message_type: str, data: JsonDict) -> bool:
        """Dispatch a websocket message; returns True when handled."""
        if message_type == "Play":
            self._play(data)
        elif message_type == "Playstate":
            self._playstate(data)
        elif message_type == "GeneralCommand":
            self._general(data)
        else:
            return False
        return True

    # -- Play ------------------------------------------------------------------

    def _play(self, data: JsonDict) -> None:
        item_ids = data.get("ItemIds") or []
        if isinstance(item_ids, str):
            item_ids = item_ids.split(",")
        start_index = int(data.get("StartIndex") or 0)
        ordered: List[str] = list(item_ids[start_index:])
        if not ordered:
            return
        command = data.get("PlayCommand", "PlayNow")
        LOG.info("remote %s of %d item(s)", command, len(ordered))

        playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
        urls = [plugin_url({"mode": "play", "id": item_id}) for item_id in ordered]

        if command == "PlayNow":
            playlist.clear()
            for url in urls:
                playlist.add(url)
            xbmc.Player().play(playlist)
            position_ticks = int(data.get("StartPositionTicks") or 0)
            if position_ticks:
                self._seek_when_playing(position_ticks / 10_000_000)
        elif command == "PlayNext":
            insert_at = playlist.getposition() + 1
            for offset, url in enumerate(urls):
                playlist.add(url, index=insert_at + offset)
        else:  # PlayLast
            for url in urls:
                playlist.add(url)

    def _seek_when_playing(self, seconds: float) -> None:
        monitor = xbmc.Monitor()
        player = xbmc.Player()
        for _ in range(20):
            if monitor.waitForAbort(0.5):
                return
            if player.isPlaying():
                try:
                    player.seekTime(seconds)
                except RuntimeError:
                    pass
                return

    # -- Playstate ----------------------------------------------------------------

    def _playstate(self, data: JsonDict) -> None:
        command = data.get("Command", "")
        player = xbmc.Player()
        if command == "Stop":
            player.stop()
        elif command in ("Pause", "Unpause", "PlayPause"):
            player.pause()  # Kodi's pause() toggles
        elif command == "NextTrack":
            player.playnext()
        elif command == "PreviousTrack":
            player.playprevious()
        elif command == "Seek":
            ticks = int(data.get("SeekPositionTicks") or 0)
            try:
                player.seekTime(ticks / 10_000_000)
            except RuntimeError:
                LOG.debug("seek with nothing playing")
        else:
            LOG.info("unhandled playstate command %s", command)

    # -- GeneralCommand -----------------------------------------------------------

    def _general(self, data: JsonDict) -> None:
        name = data.get("Name", "")
        arguments = data.get("Arguments") or {}

        if name in INPUT_ACTIONS:
            xbmc.executebuiltin("Action(%s)" % INPUT_ACTIONS[name])
        elif name == "GoHome":
            xbmc.executebuiltin("ActivateWindow(Home)")
        elif name == "GoToSettings":
            xbmc.executebuiltin("ActivateWindow(Settings)")
        elif name == "GoToSearch":
            xbmc.executebuiltin("ActivateWindow(Home)")
            xbmc.executebuiltin("SendClick(600)")
        elif name == "SetVolume":
            self._rpc(
                "Application.SetVolume",
                {"volume": int(arguments.get("Volume") or 0)},
            )
        elif name == "VolumeUp":
            self._rpc("Application.SetVolume", {"volume": "increment"})
        elif name == "VolumeDown":
            self._rpc("Application.SetVolume", {"volume": "decrement"})
        elif name in ("Mute", "Unmute", "ToggleMute"):
            mute = {"Mute": True, "Unmute": False, "ToggleMute": "toggle"}[name]
            self._rpc("Application.SetMute", {"mute": mute})
        elif name == "DisplayMessage":
            xbmcgui.Dialog().notification(
                arguments.get("Header") or "Jellyfin",
                arguments.get("Text") or "",
                xbmcgui.NOTIFICATION_INFO,
                int(arguments.get("TimeoutMs") or 5000),
                False,
            )
        elif name == "SendString":
            self._rpc(
                "Input.SendText",
                {"text": arguments.get("String") or "", "done": False},
            )
        elif name in ("SetAudioStreamIndex", "SetSubtitleStreamIndex"):
            # Jellyfin stream indexes need source-mapping; deferred.
            LOG.info("%s not yet mapped", name)
        else:
            LOG.info("unhandled general command %s", name)

    def _rpc(self, method: str, params: JsonDict) -> None:
        xbmc.executeJSONRPC(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        )
