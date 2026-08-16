"""The Jellyfin API surface kofin uses (phase 1: browse, playback, sessions)."""

from typing import Any, Dict, List, Optional, Tuple

from kofin.core import auth, settings
from kofin.core.http import DEFAULT_TIMEOUT, Http, HttpError, StreamedResponse
from kofin.core.log import Logger
from kofin.core.settings import Credentials

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# Connect/read budget for the lyrics fetch. Deliberately far below the
# transport default: see Api.lyrics.
LYRICS_TIMEOUT = (2.0, 3.0)

# The splashscreen is a ~700KB WebP the server may render (and transcode) on
# demand, so it gets a longer read budget than the default. It runs on its own
# worker thread and nothing waits on it, so a slow server costs nothing but a
# late backdrop.
SPLASHSCREEN_TIMEOUT = (6.0, 60.0)

# The service's reachability probe (Service._connect): one attempt on the
# interactive connect budget and a bounded read. The backoff loop calling it
# *is* the retry policy, so the transport must not stack its own ladder on
# top — with the default budget a single offline probe held the service loop
# for ~29 s (4 x 6 s connects plus backoff), long enough to blow Kodi's
# five-second stop grace whenever a stop request landed mid-probe (measured
# 2026-08-08: a profile switch killed the script and the interrupted login
# left the profile with no kofin service and a dead webserver). The read
# stays generous for a slow-but-alive server: the endpoint is a tiny JSON
# document, and a server that cannot produce it in ten seconds is one the
# backoff should treat as down anyway.
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
        # Interactive callers — browse listings, the play route, context and
        # settings buttons — have a person watching a spinner, and the
        # transport's default 3-retry, 6 s-connect ladder reads as a hang: an
        # unreachable server took ~54 s to render the root listing. One retry
        # and a 3.05 s connect budget instead; the read timeout stays at the
        # default, because a big listing is legitimately slow. None means the
        # transport's own defaults (service and sync callers, who prefer
        # persistence over promptness).
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

    def close(self) -> None:
        """Release the transport's connection pool.

        Sync builds one Api per worker thread (each with its own session), and
        nothing closed them: the sockets survived until CPython's cyclic GC
        happened to run, which on a busy catch-up is a lot of idle connections
        against the server (audit finding #9).
        """
        self._http.close()

    # -- plumbing ----------------------------------------------------------

    def get(self, path: str, params: Optional[JsonDict] = None) -> JsonDict:
        response = self._http.request(
            "GET",
            self._url(path),
            headers=self._headers(),
            params=params,
            timeout=self._timeout,
            retries=self._retries,
        )
        body: JsonDict = response.json() if response.content else {}
        return body

    def post(
        self,
        path: str,
        body: Optional[JsonDict] = None,
        params: Optional[JsonDict] = None,
    ) -> JsonDict:
        # Interactive shortens the connect budget only; retries stay the
        # transport's per-method default (POST: none — replay double-applies).
        response = self._http.request(
            "POST",
            self._url(path),
            headers=self._headers(),
            params=params,
            json_body=body,
            timeout=self._timeout,
        )
        if not response.content:
            return {}
        parsed: JsonDict = response.json()
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

        ``format`` is the only parameter the endpoint honours: it ignores
        ``quality`` (40/70/90/100 return byte-identical output) and ignores
        ``maxWidth``/``maxHeight`` (every one of them still answers 1920x1080).

        WebP rather than the default PNG because the default is a 3.6MB RGBA
        image, and an alpha channel is what stops Kodi's texture cache from
        re-encoding it — it caches an RGBA source as a PNG of the same size,
        so the backdrop costs its full weight twice over and the cache saves
        nothing. WebP is 698KB and caches down to a 600KB JPEG.

        Not ``Jpg``, though it is the obvious lossy choice: that derivative
        answers a *different, older* collage than the endpoint's own PNG, and
        it stayed byte-identical across every cache key that could be varied
        (format alone, with quality, with maxWidth, with maxHeight), so there
        is no way to ask it for a current one. WebP tracks the PNG.

        It has to stay the server's *lossy* WebP. A lossless one is ARGB in
        the bitstream whatever the picture, so Kodi reads an alpha channel and
        the whole thing lands back on the PNG path — the bundled fallback is
        lossless and caches as a 65KB PNG, which is the same trap in miniature
        and harmless only because it is 17KB to begin with.

        One lossy generation is spent here and a second by Kodi's cache pass
        (measured at q00=8, 4:2:0). End to end that is 29.98 dB against the
        lossless PNG, with the damage confined to saturated colour edges —
        better than the Jpg route's 28.08 dB, on a poster collage which is the
        content most likely to show it.
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
        response = self._http.request(
            "GET",
            self._url("/Sessions"),
            headers=self._headers(),
            params={"deviceId": device_id},
            timeout=self._timeout,
            retries=self._retries,
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
            "GET",
            self._url("/Users"),
            headers=self._headers(),
            timeout=self._timeout,
            retries=self._retries,
        )
        listing: List[JsonDict] = response.json() if response.content else []
        return listing

    def public_users(self) -> List[JsonDict]:
        response = self._http.request(
            "GET",
            self._url("/Users/Public"),
            headers=self._headers(),
            timeout=self._timeout,
            retries=self._retries,
        )
        listing: List[JsonDict] = response.json() if response.content else []
        return listing

    def me(self) -> JsonDict:
        """The logged-in user's own UserDto: Configuration and Policy.

        ``/Users/Me`` and ``/Users/Configuration`` below are *not* the
        ``/Users/{userId}/…`` family ``_as_user`` exists to avoid — both are
        top-level routes in 10.11's OpenAPI spec, and this one carries no path
        id at all (the token names the user). Verified against 10.11.11.
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
        response = self._http.request(
            "GET",
            self._url("/Localization/Cultures"),
            headers=self._headers(),
            timeout=self._timeout,
            retries=self._retries,
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

    def album_artists(self, parent_id: str) -> JsonDict:
        """Artists credited with an album, rather than every credited artist.

        The distinction is the point of the node: on a 20,802-song library
        /Artists answers 626 and this answers 298 — the difference is guests
        and featured performers, which is noise when you are looking for a
        record.
        """
        return self.get(
            "/Artists/AlbumArtists", {"userId": self.user_id, "parentId": parent_id}
        )

    def filters(self, parent_id: str, item_type: str = "") -> JsonDict:
        """The values a library actually holds: Years, Tags, Genres, ratings.

        One call answers all four, which is what lets the Years and Tags menus
        offer the library's own values rather than a fixed range.
        """
        params: JsonDict = {"userId": self.user_id, "parentId": parent_id}
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
            {"userId": self.user_id, "searchTerm": term, "limit": limit},
        )

    def ancestors(self, item_id: str) -> List[JsonDict]:
        response = self._http.request(
            "GET",
            self._url("/Items/%s/Ancestors" % item_id),
            headers=self._headers(),
            params={"userId": self.user_id},
            timeout=self._timeout,
            retries=self._retries,
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

    def kofin_sync_queue(self, since: int, types: str, libraries: str = "") -> JsonDict:
        """Typed change records since the unix-seconds watermark. ``types``
        is an include list (the legacy exclude-list inversion dies here);
        ``libraries`` narrows that to the synced libraries, and is simply
        ignored by a server that predates the field."""
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
            timeout=self._timeout,
            retries=self._retries,
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
            "GET",
            self._url("/SyncPlay/List"),
            headers=self._headers(),
            timeout=self._timeout,
            retries=self._retries,
        )
        listing: List[JsonDict] = response.json() if response.content else []
        return listing

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
        self.post("/SyncPlay/Leave")

    def syncplay_hello(self, protocol_version: int) -> JsonDict:
        """Capability probe + negotiation in one round trip (SYNCPLAY.md §2.1):
        a 200 carries the server's protocol version and the time-sync transport
        descriptor; stock and integrated servers 404."""
        return self.post("/SyncPlay/Hello", {"ProtocolVersion": protocol_version})

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

    def update_user_data(self, item_id: str, payload: JsonDict) -> None:
        """Partial UpdateUserItemDataDto write (offline replay, plan W2.4).

        Position and played travel in one call deliberately: sent
        separately, an item finished offline arrives as "played" and then as
        a stale position, which is precisely how a watched episode comes
        back in Continue Watching (Findroid #406). Fields left out are kept
        by the server, as ``set_resume_position`` documents.
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

    @property
    def http(self) -> Http:
        """The transport, for the one caller that fetches by URL and writes
        the bytes itself: the late-subtitle chase reuses the play route's own
        naming and file handling (``plugin/subtitles``), so it needs the
        transport rather than a method here."""
        return self._http

    def download(self, url: str) -> bytes:
        """Raw bytes of a server resource (chapter thumbnail downloads)."""
        response = self._http.request("GET", url, headers=self._headers())
        return response.content or b""

    # -- offline downloads (docs/offline-downloads-plan.md) --------------------

    def download_stream(self, item_id: str, start: int = 0) -> "StreamedResponse":
        """/Items/{id}/Download as a resumable byte stream (plan W1.5).

        The original file, Range-capable, gated server-side on the user's
        EnableContentDownloading policy (feasibility V1) — a 403 here means
        the admin turned that off, and surfaces as Unauthorized.
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
        """A progressive transcode as a byte stream (plan W3.1).

        The URL is the PlaybackInfo answer's TranscodingUrl — deviceId,
        PlaySessionId and the api key already ride it. Never a Range
        request: a re-encode is not byte-stable across attempts, so there
        is nothing coherent to resume into.
        """
        return self._http.stream(url, headers=self._headers())
