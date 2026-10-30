"""The Jellyfin API surface kofin uses."""

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from kofin.core import auth, settings
from kofin.core.http import (
    DEFAULT_TIMEOUT,
    Http,
    HttpError,
    StreamedResponse,
    plugin_transport,
)
from kofin.core.log import Logger
from kofin.core.settings import Credentials

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# Connect/read budget for the lyrics fetch. Deliberately far below the
# transport default: see Api.lyrics.
LYRICS_TIMEOUT = (2.0, 3.0)
# The SyncPlay leave on the teardown path: inside Kodi's five-second
# script-stop grace even when the server never answers.
LEAVE_TIMEOUT = (2.0, 3.0)

# The splashscreen is a WebP the server may render (and transcode) on demand,
# so it gets a longer read budget than the default. It runs on its own worker
# thread and nothing waits on it, so a slow server costs nothing but a late
# backdrop.
SPLASHSCREEN_TIMEOUT = (6.0, 60.0)

# The service's reachability probe (Service._connect): one attempt on the
# interactive connect budget and a bounded read. The backoff loop calling it
# is the retry policy, and a probe must fit Kodi's five-second stop grace.
PROBE_TIMEOUT = (3.05, 10.0)


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
        interactive: bool = False,
    ) -> None:
        self._http = http
        self.server = server
        self.user_id = user_id
        # Exposed for callers that must name this session to the server —
        # closing its transcodes (/Videos/ActiveEncodings) takes the deviceId
        # the auth header carries, and the downloads manager has no other
        # source for it.
        self.device_id = device_id
        self._header = auth.build_auth_header(device_name, device_id, version, token)
        # Interactive callers have a person waiting: one retry and a 3.05 s
        # connect budget. The read timeout stays at the default, because a big
        # listing is legitimately slow. None means the transport's defaults.
        self._retries: Optional[int] = 1 if interactive else None
        self._timeout: Optional[Tuple[float, float]] = (
            (3.05, DEFAULT_TIMEOUT[1]) if interactive else None
        )

    @classmethod
    def from_credentials(
        cls, http: Http, creds: Credentials, interactive: bool = False
    ) -> "Api":
        return cls(
            http,
            creds.server_address,
            settings.device_name(),
            creds.device_id,
            settings.addon_version(),
            creds.token,
            creds.user_id,
            interactive=interactive,
        )

    @classmethod
    def for_plugin(cls, creds: Credentials) -> "Api":
        """The plugin process's client: the stdlib transport, ``sslVerify``
        and the interactive retry and timeout budget."""
        return cls.from_credentials(
            plugin_transport(settings.get_bool("sslVerify")), creds, interactive=True
        )

    def close(self) -> None:
        """Release the transport's connection pool (one Api per sync worker
        thread)."""
        self._http.close()

    # -- plumbing ----------------------------------------------------------

    @staticmethod
    def _json(response: Any, empty: Any) -> Any:
        """The decoded body, or ``empty`` for none.

        A body that is not JSON (a proxy answering HTML with a 200) is an
        HttpError like any other bad answer.
        """
        if not response.content:
            return empty
        try:
            return response.json()
        except ValueError as error:
            raise HttpError(
                response.status_code,
                "%s -> %d with a body that is not JSON (%s)"
                % (getattr(response, "url", "?"), response.status_code, error),
            ) from error

    def get(self, path: str, params: Optional[JsonDict] = None) -> JsonDict:
        response = self._http.request(
            "GET",
            self._url(path),
            headers=self._headers(),
            params=params,
            timeout=self._timeout,
            retries=self._retries,
        )
        body: JsonDict = self._json(response, {})
        return body

    def post(
        self,
        path: str,
        body: Optional[JsonDict] = None,
        params: Optional[JsonDict] = None,
        timeout: Optional[Tuple[float, float]] = None,
    ) -> JsonDict:
        # Interactive shortens the connect budget only; retries stay the
        # transport's per-method default (POST: none — replay double-applies).
        # A caller's own ``timeout`` wins: the one POST made on a teardown
        # path (syncplay_leave) must not wait the transport's full budget.
        response = self._http.request(
            "POST",
            self._url(path),
            headers=self._headers(),
            params=params,
            json_body=body,
            timeout=timeout or self._timeout,
        )
        parsed: JsonDict = self._json(response, {})
        return parsed

    def delete(self, path: str, params: Optional[JsonDict] = None) -> None:
        self._http.request(
            "DELETE",
            self._url(path),
            headers=self._headers(),
            params=params,
            timeout=self._timeout,
        )

    def _url(self, path: str) -> str:
        return self.server + (path if path.startswith("/") else "/" + path)

    def _as_user(self, params: Optional[JsonDict] = None) -> JsonDict:
        """Query params naming the logged-in user.

        Jellyfin 10.9 moved every user-scoped route off ``/Users/{userId}/…``
        onto a top-level path taking ``userId`` as a query parameter, and the
        add-on's floor, 10.11, has dropped the old shape from its OpenAPI spec.
        A caller's own ``userId`` wins.
        """
        merged: JsonDict = {"userId": self.user_id}

        if params:
            merged.update(params)

        return merged

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": self._header, "Accept": "application/json"}

    # -- system / session ---------------------------------------------------

    def _get_list(self, path: str, params: Optional[JsonDict] = None) -> List[JsonDict]:
        """GET whose body is a bare JSON array rather than an Items envelope."""
        response = self._http.request(
            "GET",
            self._url(path),
            headers=self._headers(),
            params=params,
            timeout=self._timeout,
            retries=self._retries,
        )
        listing: List[JsonDict] = self._json(response, [])
        return listing

    def public_info(self) -> JsonDict:
        return self.get("/System/Info/Public")

    def probe_info(self) -> JsonDict:
        """/System/Info/Public on the probe budget — see PROBE_TIMEOUT."""
        response = self._http.request(
            "GET",
            self._url("/System/Info/Public"),
            headers=self._headers(),
            timeout=PROBE_TIMEOUT,
            retries=0,
        )
        body: JsonDict = response.json() if response.content else {}
        return body

    def branding_configuration(self) -> JsonDict:
        return self.get("/Branding/Configuration")

    def splashscreen(self) -> bytes:
        """The server's splashscreen image, as WebP.

        ``format`` is the only parameter the endpoint honours. WebP rather
        than the default PNG, which is RGBA: Kodi's texture cache keeps an
        RGBA source as an unreduced PNG. Not ``Jpg``, which answers an older
        collage than the endpoint's own PNG. It has to be the lossy WebP: a
        lossless one reads as alpha and lands back on the PNG path.
        """
        response = self._http.request(
            "GET",
            self._url("/Branding/Splashscreen"),
            headers={"Authorization": self._header, "Accept": "image/*"},
            params={"format": "Webp"},
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
        return self._get_list("/Sessions", {"deviceId": device_id})

    def close_transcode(self, device_id: str, play_session_id: str) -> None:
        self.delete(
            "/Videos/ActiveEncodings",
            params={"deviceId": device_id, "playSessionId": play_session_id},
        )

    def users(self) -> List[JsonDict]:
        return self._get_list("/Users")

    def public_users(self) -> List[JsonDict]:
        return self._get_list("/Users/Public")

    def me(self) -> JsonDict:
        """The logged-in user's own UserDto: Configuration and Policy.

        ``/Users/Me`` and ``/Users/Configuration`` below are *not* the
        ``/Users/{userId}/…`` family ``_as_user`` exists to avoid — both are
        top-level routes in 10.11's OpenAPI spec, and this one carries no path
        id at all (the token names the user).
        """
        return self.get("/Users/Me")

    def update_user_configuration(self, configuration: JsonDict) -> None:
        """Replace the user's whole UserConfiguration.

        The server takes no partial update here: whatever this posts *is* the
        configuration afterwards. Callers must send a full document read from
        :meth:`me`, or fields they never meant to touch (the home screen's
        ``OrderedViews``, ``GroupedFolders``) are cleared.
        """
        self.post("/Users/Configuration", dict(configuration), self._as_user())

    def cultures(self) -> List[JsonDict]:
        """The server's language list, for the audio/subtitle preferences.

        ``ThreeLetterISOLanguageName`` is the value a preference stores — it is
        what the web client writes, so a code set here round-trips with it.
        """
        return self._get_list("/Localization/Cultures")

    def session_add_user(self, session_id: str, user_id: str) -> None:
        self.post("/Sessions/%s/User/%s" % (session_id, user_id))

    def session_remove_user(self, session_id: str, user_id: str) -> None:
        self.delete("/Sessions/%s/User/%s" % (session_id, user_id))

    # -- library browse ------------------------------------------------------

    def views(self) -> JsonDict:
        return self.get("/UserViews", self._as_user())

    def item(self, item_id: str) -> JsonDict:
        # UserLibraryController.GetItem uses DtoOptions(allFields=True), unlike
        # the list endpoint. It has no Fields parameter (Jellyfin 10.11+).
        return self.get("/Items/%s" % item_id, self._as_user())

    def items(self, params: JsonDict) -> JsonDict:
        return self.get("/Items", self._as_user(params))

    def seasons(self, series_id: str) -> JsonDict:
        return self.get(
            "/Shows/%s/Seasons" % series_id,
            self._as_user({"Fields": "Etag,Overview"}),
        )

    def episodes(self, series_id: str, season_id: str, fields: str) -> JsonDict:
        return self.get(
            "/Shows/%s/Episodes" % series_id,
            self._as_user({"seasonId": season_id, "Fields": fields}),
        )

    def genres(self, parent_id: str, include_types: Optional[str] = None) -> JsonDict:
        params: JsonDict = self._as_user({"parentId": parent_id})
        if include_types:
            params["includeItemTypes"] = include_types
        return self.get("/Genres", params)

    def next_up(self, parent_id: str, fields: str = "") -> JsonDict:
        """Next-up episodes. ``parent_id`` scopes to one library; empty
        spans every shows library, matching the web client's home row."""
        params: JsonDict = self._as_user({"limit": 25})
        if parent_id:
            params["parentId"] = parent_id
        if fields:
            params["fields"] = fields
        return self.get("/Shows/NextUp", params)

    def resume(self, fields: str = "", limit: int = 25) -> JsonDict:
        """In-progress items across every library — "Continue watching".

        The server's own endpoint, so its resume thresholds decide which
        items count as in progress and their order. Video only, since Kodi
        cannot resume an audiobook.
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

    def latest(
        self, parent_id: str, include_types: str, fields: str = "", limit: int = 25
    ) -> List[JsonDict]:
        """The server's own "recently added" — ``/Items/Latest``.

        For music it is the only query that answers the question: an album's
        ``DateCreated`` is when the scanner last *created the album row*, and
        a rescan re-creates rows in folder order. ``Latest`` sorts the *songs*
        by ``DateCreated`` and, with ``GroupItems``, hands back each one's
        album, as the web client's Recently added section asks.

        It honours the account's ``HidePlayedInLatest`` preference. The answer
        is a bare list, not an ``Items`` envelope.
        """
        params: JsonDict = {
            "ParentId": parent_id,
            "IncludeItemTypes": include_types,
            "GroupItems": True,
            "Limit": limit,
            "ImageTypeLimit": 1,
        }
        if fields:
            params["Fields"] = fields
        return self._get_list("/Items/Latest", self._as_user(params))

    def artists(self, parent_id: str) -> JsonDict:
        return self.get("/Artists", self._as_user({"parentId": parent_id}))

    def album_artists(self, parent_id: str) -> JsonDict:
        """Artists credited with an album, without guests and featured
        performers."""
        return self.get("/Artists/AlbumArtists", self._as_user({"parentId": parent_id}))

    def filters(self, parent_id: str, item_type: str = "") -> JsonDict:
        """The values a library actually holds: Years, Tags, Genres, ratings.

        One call answers all four, which is what lets the Years and Tags menus
        offer the library's own values rather than a fixed range.
        """
        params: JsonDict = self._as_user({"parentId": parent_id})
        if item_type:
            params["includeItemTypes"] = item_type
        return self.get("/Items/Filters", params)

    def persons(self, term: str, limit: int = 100) -> JsonDict:
        """People matching a search term.

        Its own endpoint rather than /Items with IncludeItemTypes=Person:
        /Persons is the one that answers with the person rows the cast list
        links to, and it takes the same searchTerm.
        """
        return self.get(
            "/Persons",
            self._as_user({"searchTerm": term, "limit": limit}),
        )

    def ancestors(self, item_id: str) -> List[JsonDict]:
        return self._get_list("/Items/%s/Ancestors" % item_id, self._as_user())

    def media_folders(self) -> JsonDict:
        return self.get("/Library/MediaFolders")

    def lyrics(self, item_id: str) -> JsonDict:
        """Lyrics for a song, or {} when the server has none.

        Called on playback start, where an online lyrics add-on wins (and
        caches its result) once we are late: a short timeout and no retries.
        404 is no lyrics.
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

    # -- KofinSyncQueue companion plugin ---------------------------------------

    def kofin_sync_info(self) -> JsonDict:
        """KofinSyncQueue probe: protocol version, server clock and retention
        cutoff in one round trip (404 means the plugin is absent)."""
        return self.get("/Kofin/SyncQueue/Info")

    def kofin_sync_queue(self, since: int, types: str, libraries: str = "") -> JsonDict:
        """Typed change records since the unix-seconds watermark. ``types``
        is an include list; ``libraries`` narrows that to the synced
        libraries, and is ignored by a server that predates the field."""
        params: JsonDict = {"since": since, "types": types}

        if libraries:
            params["libraries"] = libraries

        return self.get("/Kofin/SyncQueue", params)

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
        params: JsonDict = self._as_user(
            {
                "StartTimeTicks": start_ticks,
                "IsPlayback": True,
                "AutoOpenLiveStream": True,
            }
        )
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
        return self._get_list("/Items/%s/SpecialFeatures" % item_id, self._as_user())

    def adjacent_episodes(self, series_id: str, item_id: str) -> JsonDict:
        """The episode window around ``item_id`` (next-episode resolution)."""
        return self.get(
            "/Shows/%s/Episodes" % series_id,
            self._as_user({"adjacentTo": item_id, "Fields": "Overview"}),
        )

    # -- SyncPlay --------------------------------------------------------------

    def get_utc_time(self) -> JsonDict:
        """NTP-style timestamps: {RequestReceptionTime, ResponseTransmissionTime}."""
        return self.get("/GetUtcTime")

    def syncplay_list(self) -> List[JsonDict]:
        return self._get_list("/SyncPlay/List")

    def syncplay_new(
        self, group_name: str, protocol_version: Optional[int] = None
    ) -> None:
        body: JsonDict = {"GroupName": group_name}

        if protocol_version:
            body["ProtocolVersion"] = protocol_version

        self.post("/SyncPlay/New", body)

    def syncplay_join(
        self, group_id: str, protocol_version: Optional[int] = None
    ) -> None:
        body: JsonDict = {"GroupId": group_id}

        if protocol_version:
            body["ProtocolVersion"] = protocol_version

        self.post("/SyncPlay/Join", body)

    def syncplay_leave(self) -> None:
        self.post("/SyncPlay/Leave", timeout=LEAVE_TIMEOUT)

    def syncplay_hello(
        self, protocol_version: int, capabilities: Optional[List[str]] = None
    ) -> JsonDict:
        """Capability probe + negotiation in one round trip (SYNCPLAY.md §2.1,
        §14.1): a 200 carries the server's protocol version, the time-sync
        transport descriptor and its capability list; stock and integrated
        servers 404. ``capabilities`` declares this device's — a promise the
        server's visibility rules rely on, so only ever sent for what the
        provider registry can actually resolve."""
        body: JsonDict = {"ProtocolVersion": protocol_version}
        if capabilities is not None:
            body["Capabilities"] = capabilities
        return self.post("/SyncPlay/Hello", body)

    def syncplay_snapshot(self) -> None:
        """Ask a protocol v2 server to push a StateSnapshot over the websocket."""
        self.post("/SyncPlay/Snapshot")

    def websocket_url(self, path: str) -> str:
        """ws(s):// URL for a server websocket path (the dedicated SyncPlay
        time-sync socket; the main /socket has its own builder in core/ws.py)."""
        base = self.server

        if base.startswith("https://"):
            base = base.replace("https://", "wss://", 1)
        else:
            base = base.replace("http://", "ws://", 1)

        return base + (path if path.startswith("/") else "/" + path)

    def authorization(self) -> str:
        """The Authorization header value, for connections made outside this
        client (the dedicated time-sync websocket)."""
        return self._header

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

    def syncplay_set_new_queue_ex(
        self,
        entries: List[JsonDict],
        playing_item_position: int = 0,
        start_position_ticks: int = 0,
    ) -> None:
        """SetNewQueue whose entries may be external content (SYNCPLAY.md
        §14.2): ``{"ItemId": guid}`` or ``{"Content": {...}}`` dicts, passed
        through verbatim. Plugin route; capability-gated server side."""
        self.post(
            "/SyncPlay/SetNewQueueEx",
            {
                "PlayingQueue": entries,
                "PlayingItemPosition": playing_item_position,
                "StartPositionTicks": int(start_position_ticks),
            },
        )

    def syncplay_next_item(self, playlist_item_id: str) -> None:
        self.post("/SyncPlay/NextItem", {"PlaylistItemId": playlist_item_id})

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
        favourite state.
        """
        self.post(
            "/UserItems/%s/UserData" % item_id,
            {"PlaybackPositionTicks": int(position_ticks)},
            self._as_user(),
        )

    def update_user_data(self, item_id: str, payload: JsonDict) -> None:
        """Partial UpdateUserItemDataDto write.

        Position and played travel in one call: sent apart, the stale
        position lands after "played" and puts a finished item back in
        Continue Watching. Fields left out are kept by the server, as
        ``set_resume_position`` documents.
        """
        self.post(
            "/UserItems/%s/UserData" % item_id,
            dict(payload),
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
        higher than the unique set and re-emit earlier rows on later pages.
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
                        "Fields": "MediaType,Overview,Etag",
                        "SortBy": "SortName",
                        "SortOrder": "Ascending",
                    }
                ),
            )
            if "Items" not in body or not isinstance(body["Items"], list):
                raise HttpError(200, "music playlists: response had no items list")
            items = body["Items"]
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
            # A first page with no items while the count says otherwise is the
            # server contradicting itself, and returning [] here reads
            # downstream as "this account has no playlists" -- which is a
            # deletion order for the managed folder. Fail loudly instead; a
            # genuinely empty account answers count 0.
            if start == 0 and not items and total > 0:
                raise HttpError(
                    200,
                    "music playlists: TotalRecordCount %d but the first page "
                    "was empty" % total,
                )
            start += len(items)
            # Stop on empty page, exhausted count, or a page that added no
            # new ids (server repeating itself past the real set). A later
            # empty page is the documented over-count, not a contradiction.
            if not items or start >= total or new_on_page == 0:
                break
        return results

    def playlists(self) -> List[JsonDict]:
        """User-visible playlists of every MediaType, with Etag.

        Same paging and dedupe as :meth:`music_playlists`; the caller decides
        Audio vs Video vs skip.
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
                        "Fields": "MediaType,Overview,Etag",
                        "SortBy": "SortName",
                        "SortOrder": "Ascending",
                    }
                ),
            )
            if "Items" not in body or not isinstance(body["Items"], list):
                raise HttpError(200, "playlists: response had no items list")
            items = body["Items"]
            new_on_page = 0
            for item in items:
                item_id = item.get("Id") or ""
                if not item_id or item_id in seen:
                    continue
                seen.add(item_id)
                new_on_page += 1
                results.append(item)
            total = int(body.get("TotalRecordCount") or 0)
            if start == 0 and not items and total > 0:
                raise HttpError(
                    200,
                    "playlists: TotalRecordCount %d but the first page "
                    "was empty" % total,
                )
            start += len(items)
            if not items or start >= total or new_on_page == 0:
                break
        return results

    def create_playlist(self, name: str, ids: List[str], media_type: str) -> JsonDict:
        return self.post(
            "/Playlists",
            {"Name": name, "Ids": ids, "MediaType": media_type, "UserId": self.user_id},
        )

    def replace_playlist_items(self, playlist_id: str, ids: List[str]) -> JsonDict:
        """Replace membership with this ordered id list (not per-entry delete)."""
        return self.post(
            "/Playlists/%s" % playlist_id,
            {"Ids": ids},
        )

    def playlist_items(
        self,
        playlist_id: str,
        start_index: int = 0,
        limit: int = 100,
        fields: str = "BasicSyncInfo",
    ) -> JsonDict:
        """One page of ordered playlist entries (use ``PlaylistItemId`` if writing).

        ``fields`` is the sync's minimal set by default; a caller building
        ListItems (plugin/playall.py) names what it reads instead.
        """
        return self.get(
            "/Playlists/%s/Items" % playlist_id,
            self._as_user(
                {
                    "StartIndex": start_index,
                    "Limit": limit,
                    "EnableTotalRecordCount": True,
                    "Fields": fields,
                }
            ),
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

    def trickplay_tile_url(
        self, item_id: str, media_source_id: str, width: int, index: int
    ) -> str:
        """Tile ``index`` of the item's trickplay thumbnails of ``width``, a
        columns × rows sheet of them. Unlike the item images the endpoint is
        authorized, and a 12.0 server refuses the token as a query parameter,
        so the URL carries the Authorization header the way Kodi's file layer
        takes one: after a ``|``, URL-encoded."""
        return "%s/Videos/%s/Trickplay/%d/%d.jpg?mediaSourceId=%s|Authorization=%s" % (
            self.server,
            item_id,
            width,
            index,
            media_source_id,
            quote(self._header, safe=""),
        )

    @property
    def http(self) -> Http:
        """The transport, for the subtitle fetches of the play route and the
        late-subtitle chase, which fetch by URL and write the bytes
        themselves."""
        return self._http

    def download(self, url: str) -> bytes:
        """Raw bytes of a server resource."""
        response = self._http.request("GET", url, headers=self._headers())
        return response.content or b""

    # -- offline downloads -----------------------------------------------------

    def download_stream(self, item_id: str, start: int = 0) -> "StreamedResponse":
        """/Items/{id}/Download as a resumable byte stream.

        The original file, Range-capable, gated server-side on the user's
        EnableContentDownloading policy: a 403 here means the admin turned
        that off, and surfaces as Unauthorized.
        """
        return self._http.stream(
            self._url("/Items/%s/Download" % item_id),
            headers=self._headers(),
            start=start,
        )

    def subtitle_stream_url(
        self, item_id: str, media_source_id: str, index: int, extension: str
    ) -> str:
        """The subtitle file for one stream of a media source. The endpoint
        serves external sidecars and extracts embedded *text* tracks alike
        (converting to the requested extension), which is what lets a
        transcoded download keep the subtitles its mp4 output drops."""
        return "%s/Videos/%s/%s/Subtitles/%d/Stream.%s" % (
            self.server,
            item_id,
            media_source_id,
            index,
            extension,
        )

    def transcode_stream(self, url: str) -> "StreamedResponse":
        """A progressive transcode as a byte stream.

        The URL is the PlaybackInfo answer's TranscodingUrl — deviceId,
        PlaySessionId and the api key already ride it. Never a Range
        request: a re-encode is not byte-stable across attempts, so there
        is nothing coherent to resume into.
        """
        return self._http.stream(url, headers=self._headers())
