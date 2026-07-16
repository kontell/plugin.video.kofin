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
