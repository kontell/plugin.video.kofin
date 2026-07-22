"""The Jellyfin API surface kofin uses (phase 1: browse, playback, sessions)."""

from typing import Any, Dict, List, Optional

from kofin.core import auth, settings
from kofin.core.http import Http
from kofin.core.log import Logger
from kofin.core.settings import Credentials

LOG = Logger(__name__)

JsonDict = Dict[str, Any]


class Api:
    def __init__(
        self,
        http: Http,
        server: str,
        device_name: str,
        device_id: str,
        version: str,
        token: str = "",
        user_id: str = "",
    ) -> None:
        self._http = http
        self.server = server
        self.user_id = user_id
        self._header = auth.build_auth_header(device_name, device_id, version, token)

    @classmethod
    def from_credentials(cls, http: Http, creds: Credentials) -> "Api":
        return cls(
            http,
            creds.server_address,
            settings.device_name(),
            creds.device_id,
            settings.addon_version(),
            creds.token,
            creds.user_id,
        )

    # -- plumbing ----------------------------------------------------------

    def get(self, path: str, params: Optional[JsonDict] = None) -> JsonDict:
        response = self._http.request(
            "GET", self._url(path), headers=self._headers(), params=params
        )
        body: JsonDict = response.json() if response.content else {}
        return body

    def post(
        self,
        path: str,
        body: Optional[JsonDict] = None,
        params: Optional[JsonDict] = None,
    ) -> JsonDict:
        response = self._http.request(
            "POST",
            self._url(path),
            headers=self._headers(),
            params=params,
            json_body=body,
        )
        if not response.content:
            return {}
        parsed: JsonDict = response.json()
        return parsed

    def delete(self, path: str, params: Optional[JsonDict] = None) -> None:
        self._http.request(
            "DELETE", self._url(path), headers=self._headers(), params=params
        )

    def _url(self, path: str) -> str:
        return self.server + (path if path.startswith("/") else "/" + path)

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": self._header, "Accept": "application/json"}

    # -- system / session ---------------------------------------------------

    def public_info(self) -> JsonDict:
        return self.get("/System/Info/Public")

    def post_capabilities(self, capabilities: JsonDict) -> None:
        self.post("/Sessions/Capabilities/Full", capabilities)

    def session_playing(self, data: JsonDict) -> None:
        self.post("/Sessions/Playing", data)

    def session_progress(self, data: JsonDict) -> None:
        self.post("/Sessions/Playing/Progress", data)

    def session_stopped(self, data: JsonDict) -> None:
        self.post("/Sessions/Playing/Stopped", data)

    def device_sessions(self, device_id: str) -> List[JsonDict]:
        response = self._http.request(
            "GET",
            self._url("/Sessions"),
            headers=self._headers(),
            params={"deviceId": device_id},
        )
        sessions: List[JsonDict] = response.json() if response.content else []
        return sessions

    def close_transcode(self, device_id: str, play_session_id: str) -> None:
        self.delete(
            "/Videos/ActiveEncodings",
            params={"deviceId": device_id, "playSessionId": play_session_id},
        )

    def users(self) -> List[JsonDict]:
        response = self._http.request(
            "GET", self._url("/Users"), headers=self._headers()
        )
        listing: List[JsonDict] = response.json() if response.content else []
        return listing

    def public_users(self) -> List[JsonDict]:
        response = self._http.request(
            "GET", self._url("/Users/Public"), headers=self._headers()
        )
        listing: List[JsonDict] = response.json() if response.content else []
        return listing

    def session_add_user(self, session_id: str, user_id: str) -> None:
        self.post("/Sessions/%s/User/%s" % (session_id, user_id))

    def session_remove_user(self, session_id: str, user_id: str) -> None:
        self.delete("/Sessions/%s/User/%s" % (session_id, user_id))

    # -- library browse ------------------------------------------------------

    def views(self) -> JsonDict:
        return self.get("/Users/%s/Views" % self.user_id)

    def item(self, item_id: str) -> JsonDict:
        return self.get("/Users/%s/Items/%s" % (self.user_id, item_id))

    def items(self, params: JsonDict) -> JsonDict:
        return self.get("/Users/%s/Items" % self.user_id, params)

    def seasons(self, series_id: str) -> JsonDict:
        return self.get(
            "/Shows/%s/Seasons" % series_id,
            {"userId": self.user_id, "Fields": "Etag,Overview"},
        )

    def episodes(self, series_id: str, season_id: str, fields: str) -> JsonDict:
        return self.get(
            "/Shows/%s/Episodes" % series_id,
            {"userId": self.user_id, "seasonId": season_id, "Fields": fields},
        )

    def genres(self, parent_id: str, include_types: Optional[str] = None) -> JsonDict:
        params: JsonDict = {"userId": self.user_id, "parentId": parent_id}
        if include_types:
            params["includeItemTypes"] = include_types
        return self.get("/Genres", params)

    def next_up(self, parent_id: str, fields: str = "") -> JsonDict:
        params: JsonDict = {"userId": self.user_id, "limit": 25}
        if parent_id:
            params["parentId"] = parent_id
        if fields:
            params["fields"] = fields
        return self.get("/Shows/NextUp", params)

    def artists(self, parent_id: str) -> JsonDict:
        return self.get("/Artists", {"userId": self.user_id, "parentId": parent_id})

    def ancestors(self, item_id: str) -> List[JsonDict]:
        response = self._http.request(
            "GET",
            self._url("/Items/%s/Ancestors" % item_id),
            headers=self._headers(),
            params={"userId": self.user_id},
        )
        listing: List[JsonDict] = response.json() if response.content else []
        return listing

    def media_folders(self) -> JsonDict:
        return self.get("/Library/MediaFolders")

    # -- KodiSyncQueue companion plugin ---------------------------------------

    def sync_queue(self, last_sync: str, filters: str = "") -> JsonDict:
        """Changes since ``last_sync`` from the KodiSyncQueue server plugin."""
        return self.get(
            "/Jellyfin.Plugin.KodiSyncQueue/%s/GetItems" % self.user_id,
            {"LastUpdateDT": last_sync, "filter": filters or "None"},
        )

    def server_time(self) -> JsonDict:
        """KodiSyncQueue server clock; also the companion-plugin tier probe
        (404 means the plugin is absent or disabled)."""
        return self.get("/Jellyfin.Plugin.KodiSyncQueue/GetServerDateTime")

    # -- KofinSyncQueue companion plugin (tier 1, phase 5) ---------------------

    def kofin_sync_info(self) -> JsonDict:
        """KofinSyncQueue probe: protocol version, server clock and retention
        cutoff in one round trip (404 means the plugin is absent)."""
        return self.get("/Kofin/SyncQueue/Info")

    def kofin_sync_queue(self, since: int, types: str) -> JsonDict:
        """Typed change records since the unix-seconds watermark. ``types``
        is an include list (the legacy exclude-list inversion dies here)."""
        return self.get("/Kofin/SyncQueue", {"since": since, "types": types})

    # -- playback -------------------------------------------------------------

    def playback_info(
        self,
        item_id: str,
        profile: JsonDict,
        start_ticks: int = 0,
        audio_index: Optional[int] = None,
        subtitle_index: Optional[int] = None,
        media_source_id: Optional[str] = None,
        max_bitrate: Optional[int] = None,
    ) -> JsonDict:
        body: JsonDict = {"DeviceProfile": profile, "UserId": self.user_id}
        params: JsonDict = {
            "UserId": self.user_id,
            "StartTimeTicks": start_ticks,
            "IsPlayback": True,
            "AutoOpenLiveStream": True,
        }
        if audio_index is not None:
            params["AudioStreamIndex"] = audio_index
        if subtitle_index is not None:
            params["SubtitleStreamIndex"] = subtitle_index
        if media_source_id:
            params["MediaSourceId"] = media_source_id
        if max_bitrate:
            params["MaxStreamingBitrate"] = max_bitrate
        return self.post("/Items/%s/PlaybackInfo" % item_id, body, params)

    # -- media segments / extras -------------------------------------------------

    def media_segments(self, item_id: str) -> JsonDict:
        """Media segments for an item (Jellyfin 10.10+ analyzed content).

        Raises :class:`JellyfinError` when the endpoint is unavailable; the
        callers treat segments as best-effort.
        """
        return self.get("/MediaSegments/%s" % item_id)

    def special_features(self, item_id: str) -> List[JsonDict]:
        """User-scoped special features (extras) of a movie/series/season."""
        response = self._http.request(
            "GET",
            self._url("/Users/%s/Items/%s/SpecialFeatures" % (self.user_id, item_id)),
            headers=self._headers(),
        )
        listing: List[JsonDict] = response.json() if response.content else []
        return listing

    def adjacent_episodes(self, series_id: str, item_id: str) -> JsonDict:
        """The episode window around ``item_id`` (next-episode resolution)."""
        return self.get(
            "/Shows/%s/Episodes" % series_id,
            {"userId": self.user_id, "adjacentTo": item_id, "Fields": "Overview"},
        )

    # -- SyncPlay (phase 4) ----------------------------------------------------

    def get_utc_time(self) -> JsonDict:
        """NTP-style timestamps: {RequestReceptionTime, ResponseTransmissionTime}."""
        return self.get("/GetUtcTime")

    def syncplay_list(self) -> List[JsonDict]:
        response = self._http.request(
            "GET", self._url("/SyncPlay/List"), headers=self._headers()
        )
        listing: List[JsonDict] = response.json() if response.content else []
        return listing

    def syncplay_new(self, group_name: str) -> None:
        self.post("/SyncPlay/New", {"GroupName": group_name})

    def syncplay_join(self, group_id: str) -> None:
        self.post("/SyncPlay/Join", {"GroupId": group_id})

    def syncplay_leave(self) -> None:
        self.post("/SyncPlay/Leave")

    def syncplay_ready(
        self, when: str, position_ticks: int, is_playing: bool, playlist_item_id: str
    ) -> None:
        self.post(
            "/SyncPlay/Ready",
            {
                "When": when,
                "PositionTicks": int(position_ticks),
                "IsPlaying": is_playing,
                "PlaylistItemId": playlist_item_id,
            },
        )

    def syncplay_buffering(
        self, when: str, position_ticks: int, is_playing: bool, playlist_item_id: str
    ) -> None:
        self.post(
            "/SyncPlay/Buffering",
            {
                "When": when,
                "PositionTicks": int(position_ticks),
                "IsPlaying": is_playing,
                "PlaylistItemId": playlist_item_id,
            },
        )

    def syncplay_ping(self, ping_ms: int) -> None:
        self.post("/SyncPlay/Ping", {"Ping": int(ping_ms)})

    def syncplay_unpause(self) -> None:
        self.post("/SyncPlay/Unpause")

    def syncplay_pause(self) -> None:
        self.post("/SyncPlay/Pause")

    def syncplay_stop(self) -> None:
        self.post("/SyncPlay/Stop")

    def syncplay_seek(self, position_ticks: int) -> None:
        self.post("/SyncPlay/Seek", {"PositionTicks": int(position_ticks)})

    def syncplay_set_new_queue(
        self,
        item_ids: List[str],
        playing_item_position: int = 0,
        start_position_ticks: int = 0,
    ) -> None:
        self.post(
            "/SyncPlay/SetNewQueue",
            {
                "PlayingQueue": item_ids,
                "PlayingItemPosition": playing_item_position,
                "StartPositionTicks": int(start_position_ticks),
            },
        )

    def syncplay_set_playlist_item(self, playlist_item_id: str) -> None:
        self.post("/SyncPlay/SetPlaylistItem", {"PlaylistItemId": playlist_item_id})

    def syncplay_queue(self, item_ids: List[str], mode: str = "Queue") -> None:
        self.post("/SyncPlay/Queue", {"ItemIds": item_ids, "Mode": mode})

    def syncplay_next_item(self, playlist_item_id: str) -> None:
        self.post("/SyncPlay/NextItem", {"PlaylistItemId": playlist_item_id})

    def syncplay_previous_item(self, playlist_item_id: str) -> None:
        self.post("/SyncPlay/PreviousItem", {"PlaylistItemId": playlist_item_id})

    def syncplay_set_ignore_wait(self, ignore_wait: bool) -> None:
        self.post("/SyncPlay/SetIgnoreWait", {"IgnoreWait": bool(ignore_wait)})

    # -- user data -------------------------------------------------------------

    def mark_played(self, item_id: str) -> None:
        self.post("/Users/%s/PlayedItems/%s" % (self.user_id, item_id))

    def mark_unplayed(self, item_id: str) -> None:
        self.delete("/Users/%s/PlayedItems/%s" % (self.user_id, item_id))

    def set_favorite(self, item_id: str, favorite: bool) -> None:
        path = "/Users/%s/FavoriteItems/%s" % (self.user_id, item_id)
        if favorite:
            self.post(path)
        else:
            self.delete(path)

    def delete_item(self, item_id: str) -> None:
        """Permanently delete an item from the server (content deletion)."""
        self.delete("/Items/%s" % item_id)

    # -- images ---------------------------------------------------------------

    def image_url(
        self, item_id: str, image_type: str = "Primary", tag: str = ""
    ) -> str:
        url = "%s/Items/%s/Images/%s" % (self.server, item_id, image_type)
        if tag:
            url += "?tag=%s" % tag
        return url
