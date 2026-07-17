"""Representative Jellyfin DTOs for the L2 writer tests.

Shapes mirror what /Users/{id}/Items returns with the sync Fields set:
UserData always present, People with roles and image tags, MediaSources
with typed streams, ImageTags/BackdropImageTags for artwork.
"""

import copy

LIBRARY = {"Id": "lib-movies", "Name": "Movies"}
TV_LIBRARY = {"Id": "lib-shows", "Name": "Shows"}
MV_LIBRARY = {"Id": "lib-mv", "Name": "Clips"}
MUSIC_LIBRARY = {"Id": "lib-music", "Name": "Tunes"}


def _media_source(container="mkv"):
    return {
        "Id": "source1",
        "Container": container,
        "MediaStreams": [
            {
                "Type": "Video",
                "Codec": "HEVC",
                "Profile": "Main 10",
                "Width": 3840,
                "Height": 2160,
                "AspectRatio": "16:9",
                "VideoRangeType": "HDR10",
            },
            {
                "Type": "Audio",
                "Codec": "EAC3",
                "Profile": "",
                "Channels": 6,
                "Language": "eng",
            },
            {"Type": "Subtitle", "Language": "eng"},
            {"Type": "Subtitle", "Language": "swe"},
        ],
    }


MOVIE = {
    "Id": "movie1",
    "Name": "The Example",
    "SortName": "Example",
    "Type": "Movie",
    "Etag": "etag-movie1-v1",
    "Path": "/media/movies/The Example (2020)/The Example.mkv",
    "Genres": ["Drama", "Sci-Fi"],
    "ProviderIds": {"Imdb": "tt0000001", "Tmdb": "42"},
    "CommunityRating": 7.8,
    "CriticRating": 81,
    "ProductionYear": 2020,
    "PremiereDate": "2020-05-01T00:00:00.0000000Z",
    "VoteCount": 1234,
    "Overview": 'A movie with "quotes"\nand a second line.',
    "ShortOverview": "Short.",
    "Taglines": ["Nothing is real"],
    "OfficialRating": "NR",
    "ProductionLocations": ["Ireland", "France"],
    "Studios": [{"Name": "abc (us)"}, {"Name": "Example Studio"}],
    "RunTimeTicks": 72000000000,
    "LocalTrailerCount": 0,
    "RemoteTrailers": [{"Url": "https://www.youtube.com/watch?v=trailer123"}],
    "DateCreated": "2024-01-15T10:30:00.1234567Z",
    "Tags": ["4K"],
    "People": [
        {
            "Name": "Alice Actor",
            "Type": "Actor",
            "Role": "The Lead",
            "Id": "person1",
            "PrimaryImageTag": "ptag1",
        },
        {"Name": "Bob Guest", "Type": "GuestStar", "Role": "Cameo", "Id": "person2"},
        {"Name": "Carol Director", "Type": "Director", "Id": "person3"},
        {"Name": "Dave Writer", "Type": "Writer", "Id": "person4"},
    ],
    "MediaSources": [_media_source()],
    "ImageTags": {"Primary": "prim1", "Logo": "logo1"},
    "BackdropImageTags": ["back1", "back2"],
    "ParentId": "folder-movies",
    "UserData": {
        "Played": True,
        "PlayCount": 2,
        "LastPlayedDate": "2024-02-01T20:00:00.0000000Z",
        "IsFavorite": True,
        "PlaybackPositionTicks": 9000000000,
    },
}

MOVIE_2 = {
    "Id": "movie2",
    "Name": "Second Feature",
    "SortName": "Second Feature",
    "Type": "Movie",
    "Etag": "etag-movie2-v1",
    "Path": "/media/movies/Second Feature (2021)/feature.mkv",
    "Genres": ["Comedy"],
    "ProviderIds": {"Imdb": "tt0000002"},
    "CommunityRating": 6.1,
    "ProductionYear": 2021,
    "PremiereDate": "2021-03-03T00:00:00.0000000Z",
    "VoteCount": 10,
    "Overview": "Another one.",
    "Taglines": [],
    "OfficialRating": "PG-13",
    "ProductionLocations": [],
    "Studios": [],
    "RunTimeTicks": 60000000000,
    "LocalTrailerCount": 0,
    "RemoteTrailers": [],
    "DateCreated": "2024-03-01T00:00:00.0000000Z",
    "Tags": [],
    "People": [],
    "MediaSources": [_media_source("mp4")],
    "ImageTags": {"Primary": "prim2"},
    "BackdropImageTags": [],
    "ParentId": "folder-movies",
    "UserData": {"Played": False, "PlayCount": 0, "IsFavorite": False},
}

BOXSET = {
    "Id": "set1",
    "Name": "Example Collection",
    "Type": "BoxSet",
    "Etag": "etag-set1-v1",
    "Overview": "Both of them.",
    "ImageTags": {"Primary": "setprim"},
    "BackdropImageTags": [],
    "UserData": {},
}

SERIES = {
    "Id": "series1",
    "Name": "The Show",
    "SortName": "Show",
    "Type": "Series",
    "Etag": "etag-series1-v1",
    "Path": "/media/shows/The Show",
    "Genres": ["Drama"],
    "ProviderIds": {"Tvdb": "5555"},
    "CommunityRating": 8.5,
    "ProductionYear": 2019,
    "PremiereDate": "2019-09-01T00:00:00.0000000Z",
    "VoteCount": 500,
    "Overview": "A show.",
    "OfficialRating": "TV-MA",
    "Studios": [{"Name": "HBO"}],
    "Tags": [],
    "Status": "Continuing",
    "LocalTrailerCount": 0,
    "RemoteTrailers": [],
    "RecursiveItemCount": 2,
    "People": [
        {"Name": "Alice Actor", "Type": "Actor", "Role": "Lead", "Id": "person1"}
    ],
    "ImageTags": {"Primary": "sprim", "Banner": "sban"},
    "BackdropImageTags": ["sback"],
    "ParentId": "folder-shows",
    "UserData": {"Played": False, "PlayCount": 0, "IsFavorite": False},
}

SEASON_1 = {
    "Id": "season1",
    "Name": "Season 1",
    "Type": "Season",
    "IndexNumber": 1,
    "SeriesId": "series1",
    "LocationType": "FileSystem",
    "ImageTags": {"Primary": "seasprim"},
    "BackdropImageTags": [],
    "UserData": {},
}

EPISODE = {
    "Id": "episode1",
    "Name": "Pilot",
    "Type": "Episode",
    "Etag": "etag-episode1-v1",
    "Path": "/media/shows/The Show/Season 1/S01E01.mkv",
    "Overview": "It begins.",
    "CommunityRating": 8.0,
    "VoteCount": 100,
    "ProviderIds": {"Tvdb": "9999"},
    "SeriesId": "series1",
    "SeriesName": "The Show",
    "ParentIndexNumber": 1,
    "IndexNumber": 1,
    "RunTimeTicks": 30000000000,
    "PremiereDate": "2019-09-01T00:00:00.0000000Z",
    "DateCreated": "2024-02-10T08:00:00.0000000Z",
    "LocationType": "FileSystem",
    "People": [
        {"Name": "Eve Episodic", "Type": "GuestStar", "Role": "Guest", "Id": "person5"},
        {"Name": "Carol Director", "Type": "Director", "Id": "person3"},
    ],
    "MediaSources": [_media_source()],
    "ImageTags": {"Primary": "eprim"},
    "BackdropImageTags": [],
    "UserData": {
        "Played": False,
        "PlayCount": 0,
        "IsFavorite": False,
        "PlaybackPositionTicks": 3000000000,
    },
}

MUSICVIDEO = {
    "Id": "mvideo1",
    "Name": "Hit Single",
    "SortName": "01 Hit Single",
    "Type": "MusicVideo",
    "Etag": "etag-mvideo1-v1",
    "Path": "/media/musicvideos/Hit Single.mkv",
    "Genres": ["Pop"],
    "ProductionYear": 2018,
    "PremiereDate": "2018-06-06T00:00:00.0000000Z",
    "Overview": "A video.",
    "Studios": [{"Name": "Vevo"}],
    "RunTimeTicks": 2400000000,
    "Album": "Hits",
    "Artists": ["The Band"],
    "ArtistItems": [{"Name": "The Band", "Id": "artist1"}],
    "Tags": [],
    "DateCreated": "2024-01-20T12:00:00.0000000Z",
    "People": [{"Name": "Frank Filmer", "Type": "Director", "Id": "person6"}],
    "MediaSources": [_media_source("mp4")],
    "ImageTags": {"Primary": "mvprim"},
    "BackdropImageTags": [],
    "ParentId": "folder-mv",
    "UserData": {"Played": False, "PlayCount": 0, "IsFavorite": False},
}

ARTIST = {
    "Id": "artist1",
    "Name": "The Band",
    "Type": "MusicArtist",
    "Etag": "etag-artist1-v1",
    "Genres": ["Rock"],
    "Overview": "A band.",
    "ProviderIds": {"MusicBrainzArtist": "mbid-artist-1"},
    "ImageTags": {"Primary": "artprim"},
    "BackdropImageTags": ["artback"],
    "ParentId": "folder-music",
    "UserData": {"Played": False, "IsFavorite": False},
}

ALBUM = {
    "Id": "album1",
    "Name": "Greatest Hits",
    "Type": "MusicAlbum",
    "Etag": "etag-album1-v1",
    "ProductionYear": 2017,
    "Genres": ["Rock"],
    "Overview": "The hits.",
    "RunTimeTicks": 24000000000,
    "ProviderIds": {"MusicBrainzAlbum": "mbid-album-1"},
    "AlbumArtists": [{"Name": "The Band", "Id": "artist1"}],
    "ArtistItems": [{"Name": "The Band", "Id": "artist1"}],
    "DateCreated": "2024-01-05T00:00:00.0000000Z",
    "ImageTags": {"Primary": "albprim"},
    "BackdropImageTags": [],
    "ParentId": "folder-music",
    "UserData": {"Played": False, "IsFavorite": False},
}

SONG = {
    "Id": "song1",
    "Name": "Opening Track",
    "Type": "Audio",
    "Etag": "etag-song1-v1",
    "Path": "/media/music/The Band/Greatest Hits/01 - Opening Track.flac",
    "Genres": ["Rock"],
    "Artists": ["The Band"],
    "ArtistItems": [{"Name": "The Band", "Id": "artist1"}],
    "AlbumArtists": [{"Name": "The Band", "Id": "artist1"}],
    "Album": "Greatest Hits",
    "AlbumId": "album1",
    "IndexNumber": 1,
    "ParentIndexNumber": 1,
    "ProductionYear": 2017,
    "RunTimeTicks": 1800000000,
    "Overview": "Track one.",
    "ProviderIds": {"MusicBrainzTrackId": "mbid-track-1"},
    "DateCreated": "2024-01-05T00:00:00.0000000Z",
    "MediaSources": [{"Id": "songsource", "Container": "flac", "MediaStreams": []}],
    "ImageTags": {"Primary": "songprim"},
    "BackdropImageTags": [],
    "ParentId": "album1",
    "UserData": {
        "Played": True,
        "PlayCount": 5,
        "LastPlayedDate": "2024-02-02T00:00:00.0000000Z",
        "IsFavorite": False,
    },
}


def dto(source):
    """A fresh copy per write: the writers mutate their input dicts."""
    return copy.deepcopy(source)
