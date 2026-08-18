"""Test doubles for the Kodi API surfaces kofin touches."""

from typing import Dict


class FakeAddon:
    """Stands in for xbmcaddon.Addon with a dict-backed settings store."""

    store: Dict[str, str] = {}

    def __init__(self, addon_id: str = "") -> None:
        self.addon_id = addon_id

    def getSetting(self, setting_id: str) -> str:
        return self.store.get(setting_id, "")

    def setSetting(self, setting_id: str, value: str) -> None:
        self.store[setting_id] = value

    def getSettingBool(self, setting_id: str) -> bool:
        return self.store.get(setting_id) == "true"

    def setSettingBool(self, setting_id: str, value: bool) -> None:
        self.store[setting_id] = "true" if value else "false"

    def getSettingInt(self, setting_id: str) -> int:
        return int(self.store.get(setting_id, "0") or "0")

    def getAddonInfo(self, info: str) -> str:
        return {"version": "0.1.0", "path": "/tmp/kofin"}.get(info, "")

    def getLocalizedString(self, string_id: int) -> str:
        return "string-%d" % string_id


class FakeWindow:
    """Stands in for xbmcgui.Window property storage."""

    store: Dict[str, str] = {}

    def __init__(self, window_id: int = 0) -> None:
        self.window_id = window_id

    def getProperty(self, key: str) -> str:
        return self.store.get(key, "")

    def setProperty(self, key: str, value: str) -> None:
        self.store[key] = value

    def clearProperty(self, key: str) -> None:
        self.store.pop(key, None)


def player_ops_rpc(get_player):
    """A stand-in for ``xbmc.executeJSONRPC`` that drives a fake player.

    kofin routes the privileged player operations through JSON-RPC rather than
    the Python bindings -- the stop because ``xbmc.Player.stop()`` deadlocks
    (issue #155), the resume because ``pause()`` is a toggle whose effect lands
    asynchronously (kodirpc.resume_player). Both seams moved out of the player
    object, so the fakes have to follow them or the tests assert against a
    player nothing is talking to.

    ``get_player`` is a callable returning the fake player, so a fixture can
    hand over one that is created later.
    """
    import json

    def rpc(query):
        payload = json.loads(query)
        method = payload.get("method")
        params = payload.get("params") or {}
        player = get_player()

        if method == "Player.GetActivePlayers":
            if player is None or not getattr(player, "playing", False):
                return json.dumps({"result": []})

            audio = getattr(player, "audio", False)
            return json.dumps({"result": [{"playerid": 0 if audio else 1}]})

        if method == "Player.GetProperties":
            paused = bool(getattr(player, "paused", False)) if player else False
            return json.dumps({"result": {"speed": 0 if paused else 1}})

        if method == "Player.Stop":
            if player is not None:
                if hasattr(player, "actions"):
                    player.actions.append("stop")
                player.playing = False

            return json.dumps({"result": "OK"})

        if method == "Player.PlayPause":
            if player is not None and "play" in params:
                player.paused = not bool(params["play"])

            return json.dumps({"result": {"speed": 1}})

        return json.dumps({"result": {}})

    return rpc
