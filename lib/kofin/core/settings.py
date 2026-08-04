"""Typed access to addon settings, and the hidden-settings credential store.

The settings store is the only durable state in phase 1. Hidden level-4 keys
(see resources/settings.xml) hold the credentials; :class:`Credentials` is the
sole writer of those keys.
"""

import uuid
from typing import List

import xbmc
import xbmcaddon
import xbmcvfs

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


def addon_data_path() -> str:
    """The profile's addon_data directory for kofin.

    ``sync.db`` resolves the same directory for the transplant's own files;
    this is the shell-side reader so a shell module does not have to import
    the sync stack (and inherit its failure modes) to find a scratch path.
    """
    return xbmcvfs.translatePath("special://profile/addon_data/" + ADDON_ID + "/")


def addon_name() -> str:
    return _addon().getAddonInfo("name")


def localized(string_id: int) -> str:
    return _addon().getLocalizedString(string_id)


def device_name() -> str:
    """How this device names itself to Jellyfin: Kodi's own friendly name
    (services.devicename, or "Kodi (host)" by default), matching pvr.kofin.
    Replaces the former per-addon ``deviceName`` setting."""
    return xbmc.getInfoLabel("System.FriendlyName") or "Kodi"


def resume_offset() -> float:
    """Seconds a resume point is rewound by (Advanced tab, stored negative).

    Never raises: the sync writers call this for every item they stamp a
    bookmark on, and a settings read that failed must degrade to "no offset"
    rather than take the item down with it.
    """
    try:
        return abs(float(get_int("resumeJumpBack")))
    except Exception:  # pragma: no cover - defensive
        return 0.0


def adjusted_resume(position_seconds: float) -> float:
    """``position_seconds`` pulled back by :func:`resume_offset`.

    The single rule behind three call sites that must agree — the Kodi
    bookmark the sync writes, the resume point a listing advertises, and the
    position playback actually starts at. If they disagree, Kodi's resume
    prompt names one time and playback lands on another.

    Fork ``adjust_resume`` semantics: a position shorter than the offset is
    left alone rather than clamped to zero, so a barely-started item keeps its
    in-progress bookmark instead of reverting to unwatched.
    """
    offset = resume_offset()
    if position_seconds > offset:
        return position_seconds - offset
    return position_seconds


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
        """Log out: drop the session, keep the server address and device id.

        Also clears ``whoIsWatching``: additional-user membership is tied to
        this primary user on this device. A later login as someone else must
        not re-attach the previous household's co-watchers. The shortlist is
        left alone — it is a device preference about who the picker offers.
        """
        for setting_id in (
            "serverName",
            "serverId",
            "userId",
            "displayUser",
            "accessToken",
            "whoIsWatching",
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
