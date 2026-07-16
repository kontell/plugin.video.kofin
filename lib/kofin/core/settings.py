"""Typed access to addon settings, and the hidden-settings credential store.

The settings store is the only durable state in phase 1. Hidden level-4 keys
(see resources/settings.xml) hold the credentials; :class:`Credentials` is the
sole writer of those keys.
"""

import uuid
from typing import List

import xbmcaddon

from kofin.core import log

ADDON_ID = "plugin.video.kofin"


def _addon() -> xbmcaddon.Addon:
    # A fresh Addon per call: with reuselanguageinvoker a cached instance can
    # serve stale values after another process wrote the settings.
    return xbmcaddon.Addon(ADDON_ID)


def get_str(setting_id: str) -> str:
    return _addon().getSetting(setting_id)


def set_str(setting_id: str, value: str) -> None:
    _addon().setSetting(setting_id, value)


def get_bool(setting_id: str) -> bool:
    return _addon().getSettingBool(setting_id)


def set_bool(setting_id: str, value: bool) -> None:
    _addon().setSettingBool(setting_id, value)


def get_int(setting_id: str) -> int:
    return _addon().getSettingInt(setting_id)


def get_list(setting_id: str) -> List[str]:
    raw = _addon().getSetting(setting_id)
    return [part for part in (piece.strip() for piece in raw.split(",")) if part]


def addon_version() -> str:
    return _addon().getAddonInfo("version")


def addon_path() -> str:
    return _addon().getAddonInfo("path")


def localized(string_id: int) -> str:
    return _addon().getLocalizedString(string_id)


class Credentials:
    """The hidden-settings credential record for the single server."""

    def __init__(
        self,
        server_address: str = "",
        server_name: str = "",
        server_id: str = "",
        user_id: str = "",
        display_user: str = "",
        token: str = "",
        device_id: str = "",
        is_logged_in: bool = False,
    ) -> None:
        self.server_address = server_address
        self.server_name = server_name
        self.server_id = server_id
        self.user_id = user_id
        self.display_user = display_user
        self.token = token
        self.device_id = device_id
        self.is_logged_in = is_logged_in

    @classmethod
    def load(cls) -> "Credentials":
        creds = cls(
            server_address=get_str("serverAddress"),
            server_name=get_str("serverName"),
            server_id=get_str("serverId"),
            user_id=get_str("userId"),
            display_user=get_str("displayUser"),
            token=get_str("accessToken"),
            device_id=get_str("deviceId"),
            is_logged_in=get_bool("isLoggedIn"),
        )
        if not creds.device_id:
            creds.device_id = uuid.uuid4().hex
            set_str("deviceId", creds.device_id)
        creds._register_secrets()
        return creds

    def save(self) -> None:
        set_str("serverAddress", self.server_address)
        set_str("serverName", self.server_name)
        set_str("serverId", self.server_id)
        set_str("userId", self.user_id)
        set_str("displayUser", self.display_user)
        set_str("accessToken", self.token)
        set_str("deviceId", self.device_id)
        set_bool("isLoggedIn", self.is_logged_in)
        self._register_secrets()

    @classmethod
    def clear(cls) -> None:
        """Log out: drop the session, keep the server address and device id."""
        for setting_id in (
            "serverName",
            "serverId",
            "userId",
            "displayUser",
            "accessToken",
        ):
            set_str(setting_id, "")
        set_bool("isLoggedIn", False)

    def _register_secrets(self) -> None:
        if self.token:
            log.register_secret(self.token)
        if self.user_id:
            log.register_secret(self.user_id, keep=6)
        if self.device_id:
            log.register_secret(self.device_id, keep=6)
