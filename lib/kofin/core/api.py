"""The Jellyfin API surface kofin uses (phase 1: browse, playback, sessions)."""

from typing import Any, Dict, List, Optional

from kofin.core import auth, settings
from kofin.core.http import Http, HttpError
from kofin.core.log import Logger
from kofin.core.settings import Credentials

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# Connect/read budget for the lyrics fetch. Deliberately far below the
# transport default: see Api.lyrics.
LYRICS_TIMEOUT = (2.0, 3.0)

# The splashscreen is a ~2.3MB PNG the server may render on demand, so it gets
# a longer read budget than the default. It runs on its own worker thread and
# nothing waits on it, so a slow server costs nothing but a late backdrop.
SPLASHSCREEN_TIMEOUT = (6.0, 60.0)


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

    def _as_user(self, params: Optional[JsonDict] = None) -> JsonDict:
        """Query params naming the logged-in user.

        Jellyfin 10.9 moved every user-scoped route off ``/Users/{userId}/…``
        onto a top-level path taking ``userId`` as a query parameter. 10.11
        still answers the old shape but has dropped it from its OpenAPI spec,
        which is how a route stops being served — so kofin asks the documented
        way. Safe without a fallback: the addon's floor is 10.11 (README,
        docs/rewrite-research.md), well past the move.

        A caller's own ``userId`` wins. Nothing passes one today; the merge
        order is stated so it cannot become a surprise.
        """
        merged: JsonDict = {"userId": self.user_id}

        if params:
            merged.update(params)

        return merged

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": self._header, "Accept": "application/json"}

    # -- system / session ---------------------------------------------------

    def public_info(self) -> JsonDict:
        return self.get("/System/Info/Public")

    def branding_configuration(self) -> JsonDict:
        return self.get("/Branding/Configuration")

    def splashscreen(self) -> bytes:
        """The server's splashscreen image, exactly as the server encodes it.

        No transcode parameters, deliberately. The endpoint ignores ``quality``
        outright (40/70/90/100 all return byte-identical output) and the image
        is already 1920x1080, so ``maxWidth``/``maxHeight`` are no-ops on the
        only dimension that could matter. That leaves ``format``, and asking
        for Jpg would only add a lossy generation ahead of the one Kodi's own
        texture cache applies — the splash is a poster collage full of fine
        text, which is exactly the content that shows it.
        """
        response = self._http.request(
            "GET",
            self._url("/Branding/Splashscreen"),
            headers={"Authorization": self._header, "Accept": "image/*"},
            timeout=SPLASHSCREEN_TIMEOUT,
        )
        return bytes(response.content)

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
        return self.get("/UserViews", self._as_user())

    def item(self, item_id: str) -> JsonDict:
        return self.get("/Items/%s" % item_id, self._as_user())

    def items(self, params: JsonDict) -> JsonDict:
        return self.get("/Items", self._as_user(params))

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

    def resume(self, fields: str = "", limit: int = 25) -> JsonDict:
        """In-progress items across every library — "Continue watching".

        The server's own endpoint rather than an /Items query filtered on
        IsResumable, so the list matches what the other clients show: which
        items count as in progress is the server's judgement (its min/max
        resume percentages decide when something enters and leaves), and so is
        the order they come back in, most recently played first.

        MediaTypes limits it to video. Audiobooks are the other thing the
        server tracks a position for, and Kodi has nowhere to resume one.
        """
        params: JsonDict = {
            "Limit": limit,
            "MediaTypes": "Video",
            "Recursive": True,
            "EnableTotalRecordCount": False,
        }
        if fields:
            params["Fields"] = fields
        return self.get("/UserItems/Resume", self._as_user(params))

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

    def lyrics(self, item_id: str) -> JsonDict:
        """Lyrics for a song, or {} when the server has none.

        Called on the playback-start callback, where the whole budget is the
        gap before Kodi renders its first frame — a lyrics addon searching
        online wins the moment we are late, and it caches that result, so a
        slow answer is worth no more than no answer. Hence the short timeout
        and no retries: forfeit rather than hold up the callback.

        404 is the ordinary "no lyrics for this track" reply and is not worth
        an exception to the caller.
        """
        try:
            response = self._http.request(
                "GET",
                self._url("/Audio/%s/Lyrics" % item_id),
                headers=self._headers(),
                timeout=LYRICS_TIMEOUT,
                retries=0,
            )
        except HttpError as error:
            if error.status == 404:
                return {}
            raise
        body: JsonDict = response.json() if response.content else {}
        return body

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
            self._url("/Items/%s/SpecialFeatures" % item_id),
            headers=self._headers(),
            params=self._as_user(),
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
        self.post("/UserPlayedItems/%s" % item_id, params=self._as_user())

    def mark_unplayed(self, item_id: str) -> None:
        self.delete("/UserPlayedItems/%s" % item_id, self._as_user())

    def set_resume_position(self, item_id: str, position_ticks: int) -> None:
        """Move an item's stored resume point (Jellyfin 10.10+ user data).

        There is no dedicated "clear the resume point" call; a user-data
        update with a zero position is it. The body is a partial
        UpdateUserItemDataDto and the server keeps every field left out, so
        this touches neither the played flag, the play count nor the
        favourite state (verified against 10.11).
        """
        self.post(
            "/UserItems/%s/UserData" % item_id,
            {"PlaybackPositionTicks": int(position_ticks)},
            self._as_user(),
        )

    def set_favorite(self, item_id: str, favorite: bool) -> None:
        path = "/UserFavoriteItems/%s" % item_id
        if favorite:
            self.post(path, params=self._as_user())
        else:
            self.delete(path, self._as_user())

    def delete_item(self, item_id: str) -> None:
        """Permanently delete an item from the server (content deletion)."""
        self.delete("/Items/%s" % item_id)

    # -- music playlists -------------------------------------------------------

    def music_playlists(self) -> List[JsonDict]:
        """User-visible music playlists (Type=Playlist, MediaType Audio or empty).

        Empty playlists often have no MediaType yet; video playlists are
        excluded. Paged so a large account does not load in one response.

        Dedupes by item id: some Jellyfin builds report a ``TotalRecordCount``
        higher than the unique set and re-emit earlier rows on later pages
        (live: count 13, eight unique, page 2 repeated Shuffle 02–07 + UHD).
        """
        results: List[JsonDict] = []
        seen: set[str] = set()
        start = 0
        page_size = 100
        while True:
            body = self.get(
                "/Items",
                self._as_user(
                    {
                        "IncludeItemTypes": "Playlist",
                        "Recursive": True,
                        "StartIndex": start,
                        "Limit": page_size,
                        "EnableTotalRecordCount": True,
                        "Fields": "MediaType,Overview",
                        "SortBy": "SortName",
                        "SortOrder": "Ascending",
                    }
                ),
            )
            items = body.get("Items") or []
            new_on_page = 0
            for item in items:
                item_id = item.get("Id") or ""
                if not item_id or item_id in seen:
                    continue
                seen.add(item_id)
                new_on_page += 1
                media_type = item.get("MediaType") or ""
                if media_type and media_type != "Audio":
                    continue
                results.append(item)
            total = int(body.get("TotalRecordCount") or 0)
            start += len(items)
            # Stop on empty page, exhausted count, or a page that added no
            # new ids (server repeating itself past the real set).
            if not items or start >= total or new_on_page == 0:
                break
        return results

    def playlist_items(
        self, playlist_id: str, start_index: int = 0, limit: int = 100
    ) -> JsonDict:
        """One page of ordered playlist entries (use ``PlaylistItemId`` if writing)."""
        return self.get(
            "/Playlists/%s/Items" % playlist_id,
            {
                "UserId": self.user_id,
                "StartIndex": start_index,
                "Limit": limit,
                "EnableTotalRecordCount": True,
                "Fields": "BasicSyncInfo",
            },
        )

    # -- images ---------------------------------------------------------------

    def image_url(
        self, item_id: str, image_type: str = "Primary", tag: str = ""
    ) -> str:
        url = "%s/Items/%s/Images/%s" % (self.server, item_id, image_type)
        if tag:
            url += "?tag=%s" % tag
        return url

    def chapters(self, item_id: str) -> List[JsonDict]:
        """The item's chapter list: name, start ticks, and — when the server
        has extracted a chapter image — its ImageTag."""
        item = self.get("/Items/%s" % item_id, self._as_user({"Fields": "Chapters"}))
        chapters = item.get("Chapters")
        return chapters if isinstance(chapters, list) else []

    def chapter_image_url(
        self, item_id: str, index: int, tag: str, max_width: int
    ) -> str:
        """The server-extracted image for chapter ``index`` (0-based, the
        server's ChapterIndex). Anonymous like every other art URL."""
        return "%s/Items/%s/Images/Chapter/%d?tag=%s&maxWidth=%d" % (
            self.server,
            item_id,
            index,
            tag,
            max_width,
        )

    def download(self, url: str) -> bytes:
        """Raw bytes of a server resource (chapter thumbnail downloads)."""
        response = self._http.request("GET", url, headers=self._headers())
        return response.content or b""
