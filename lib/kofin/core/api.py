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
            settings.get_str("deviceName") or "Kodi",
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

    # -- images ---------------------------------------------------------------

    def image_url(
        self, item_id: str, image_type: str = "Primary", tag: str = ""
    ) -> str:
        url = "%s/Items/%s/Images/%s" % (self.server, item_id, image_type)
        if tag:
            url += "?tag=%s" % tag
        return url
