"""The kind table: what Kodi's public API offers each native kind.

Everything kind-specific that is a *fact about the API* lives here, so the
reconciler can be driven by lookups rather than ``if kind ==`` branches: which
methods list, read, set, refresh and remove a kind, which properties its
readback carries, how a row of it is removed, and which Jellyfin types have a
native row at all.
"""

from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from kofin.core import kodirpc

# Independent setters travel 25 to a JSON-RPC array: the feasibility report's
# fastest measured shape (7.4), and small enough that one failed call costs
# little to retry.
BATCH = 25

# Application order: a season or episode patch needs its show's row first,
# a set needs its movies, an album or artist is found through its songs.
ORDER = (
    "Series",
    "Season",
    "Episode",
    "Movie",
    "MusicVideo",
    "BoxSet",
    "Audio",
    "MusicAlbum",
    "MusicArtist",
)

# Kinds whose rows carry a resolver URL and a namespaced unique id. A song
# has the URL but no unique id: Kodi keeps none for music, so its ownership
# is the URL alone.
FILED = ("Movie", "Series", "Episode", "MusicVideo")

# Kodi's media type for a playable kind, for playback and userdata lookups.
MEDIA = {
    "movie": "Movie",
    "episode": "Episode",
    "musicvideo": "MusicVideo",
    "song": "Audio",
}

# Kinds whose rows carry userdata the server and Kodi both edit.
PLAYABLE = ("Movie", "Episode", "MusicVideo", "Audio")

# Playable kinds with a watcher behind Kodi's announcements: their userdata
# writes are recorded as expectations so the echo is not read as a local
# edit. Nothing listens for songs (AudioLibrary.OnUpdate is not watched and
# Kodi has no watched UI for songs), so a song expectation would be a write
# for nothing.
EXPECTED = ("Movie", "Episode", "MusicVideo")

# Kinds the music scanner derives from song tags rather than files: they
# have no directory, no listing of their own and leave when their songs do.
DERIVED = ("MusicAlbum", "MusicArtist")


class Kind(NamedTuple):
    listing: str
    list_key: str
    id_param: str
    getter: str
    result_key: str
    setter: str
    refresh: Optional[str]
    remove: Optional[str]
    # How a tombstone of this kind leaves Kodi: "row" through its own remove
    # call, "parent" with the show it belongs to (no call of its own),
    # "members" once the movies that filed it have let go and Kodi's clean
    # has dropped the empty set, or "rescan" through the complete listing
    # of its directory -- the audio API has no remove call at all, and a
    # music scan replaces a directory's songs with whatever is listed.
    removal: str


KINDS: Dict[str, Kind] = {
    "Movie": Kind(
        "VideoLibrary.GetMovies",
        "movies",
        "movieid",
        "VideoLibrary.GetMovieDetails",
        "moviedetails",
        "VideoLibrary.SetMovieDetails",
        "VideoLibrary.RefreshMovie",
        "VideoLibrary.RemoveMovie",
        "row",
    ),
    "Series": Kind(
        "VideoLibrary.GetTVShows",
        "tvshows",
        "tvshowid",
        "VideoLibrary.GetTVShowDetails",
        "tvshowdetails",
        "VideoLibrary.SetTVShowDetails",
        "VideoLibrary.RefreshTVShow",
        "VideoLibrary.RemoveTVShow",
        "row",
    ),
    "Season": Kind(
        "VideoLibrary.GetSeasons",
        "seasons",
        "seasonid",
        "VideoLibrary.GetSeasonDetails",
        "seasondetails",
        "VideoLibrary.SetSeasonDetails",
        None,
        None,
        "parent",
    ),
    "Episode": Kind(
        "VideoLibrary.GetEpisodes",
        "episodes",
        "episodeid",
        "VideoLibrary.GetEpisodeDetails",
        "episodedetails",
        "VideoLibrary.SetEpisodeDetails",
        "VideoLibrary.RefreshEpisode",
        "VideoLibrary.RemoveEpisode",
        "row",
    ),
    "MusicVideo": Kind(
        "VideoLibrary.GetMusicVideos",
        "musicvideos",
        "musicvideoid",
        "VideoLibrary.GetMusicVideoDetails",
        "musicvideodetails",
        "VideoLibrary.SetMusicVideoDetails",
        "VideoLibrary.RefreshMusicVideo",
        "VideoLibrary.RemoveMusicVideo",
        "row",
    ),
    "BoxSet": Kind(
        "VideoLibrary.GetMovieSets",
        "sets",
        "setid",
        "VideoLibrary.GetMovieSetDetails",
        "setdetails",
        "VideoLibrary.SetMovieSetDetails",
        None,
        None,
        "members",
    ),
    "Audio": Kind(
        "AudioLibrary.GetSongs",
        "songs",
        "songid",
        "AudioLibrary.GetSongDetails",
        "songdetails",
        "AudioLibrary.SetSongDetails",
        None,
        None,
        "rescan",
    ),
    "MusicAlbum": Kind(
        "AudioLibrary.GetAlbums",
        "albums",
        "albumid",
        "AudioLibrary.GetAlbumDetails",
        "albumdetails",
        "AudioLibrary.SetAlbumDetails",
        None,
        None,
        "rescan",
    ),
    "MusicArtist": Kind(
        "AudioLibrary.GetArtists",
        "artists",
        "artistid",
        "AudioLibrary.GetArtistDetails",
        "artistdetails",
        "AudioLibrary.SetArtistDetails",
        None,
        None,
        "rescan",
    ),
}

# The scanner a kind's content belongs to, for the scan call and the waits.
SCANNER = {"Audio": "music", "MusicAlbum": "music", "MusicArtist": "music"}

# What a scope readback carries: enough to find a row, own it and see the
# userdata a viewer may have changed, never the whole row. The full row is
# read by id (or as one listing when most of a scope needs it) only for the
# items whose desired state moved. A full readback of 1,788 movies with 27
# properties was the largest reply Kodi built for a pass that patched one.
MINIMAL: Dict[str, List[str]] = {
    "Movie": ["file", "uniqueid", "playcount", "lastplayed", "resume"],
    "Series": ["file", "uniqueid"],
    "Season": ["season", "tvshowid"],
    "Episode": ["file", "uniqueid", "playcount", "lastplayed", "resume", "tvshowid"],
    "MusicVideo": ["file", "uniqueid", "playcount", "lastplayed", "resume"],
    "BoxSet": ["title"],
}

PROPERTIES: Dict[str, List[str]] = {
    "Movie": [
        "file",
        "uniqueid",
        "title",
        "plot",
        "playcount",
        "lastplayed",
        "resume",
        "tag",
        "art",
        "ratings",
        "originaltitle",
        "sorttitle",
        "plotoutline",
        "tagline",
        "year",
        "premiered",
        "mpaa",
        "runtime",
        "genre",
        "studio",
        "country",
        "director",
        "writer",
        "dateadded",
        "trailer",
        "set",
    ],
    "Series": [
        "file",
        "uniqueid",
        "title",
        "originaltitle",
        "sorttitle",
        "plot",
        "premiered",
        "mpaa",
        "genre",
        "studio",
        "tag",
        "art",
        "ratings",
        "dateadded",
        "trailer",
        "status",
        "runtime",
    ],
    "Season": ["season", "title", "art", "tvshowid"],
    "Episode": [
        "file",
        "uniqueid",
        "title",
        "originaltitle",
        "plot",
        "firstaired",
        "season",
        "episode",
        "runtime",
        "director",
        "writer",
        "art",
        "ratings",
        "dateadded",
        "playcount",
        "lastplayed",
        "resume",
        "tvshowid",
    ],
    "MusicVideo": [
        "file",
        "uniqueid",
        "title",
        "plot",
        "runtime",
        "director",
        "studio",
        "year",
        "premiered",
        "genre",
        "album",
        "artist",
        "track",
        "tag",
        "art",
        "rating",
        "dateadded",
        "playcount",
        "lastplayed",
        "resume",
    ],
    "BoxSet": ["title", "plot", "art"],
    # A song's readback is one call for a whole library, so it carries only
    # what the pass compares -- its userdata -- and what maps its album.
    # Every property that joins another table (artistid, genreid) costs the
    # whole listing dear, as the schema itself warns.
    "Audio": ["file", "albumid", "playcount", "lastplayed"],
    "MusicAlbum": ["art", "description"],
    "MusicArtist": ["art", "description"],
}


def rpc(method: str, params: Optional[Dict[str, Any]] = None) -> Any:
    result = kodirpc.call(method, params or {})
    if result is None or result is kodirpc.FAILED:
        raise RuntimeError("Kodi refused " + method)
    return result


def rpc_batch(requests: Sequence[Tuple[str, Optional[Dict[str, Any]]]]) -> List[Any]:
    """Every reply, as a result or the RuntimeError that ``rpc`` would raise."""
    results = kodirpc.batch(requests)
    return [
        (
            RuntimeError("Kodi refused " + method)
            if result is None or result is kodirpc.FAILED
            else result
        )
        for (method, _), result in zip(requests, results)
    ]
