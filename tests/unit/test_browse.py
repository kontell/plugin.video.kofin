from kofin.plugin.browse import _genre_types, _guess_content, _node_content, node_query


def test_node_query_movies_all():
    query = node_query("movies", "all", "v1")
    assert query["IncludeItemTypes"] == "Movie"
    assert query["ParentId"] == "v1"
    assert query["Recursive"] is True
    assert query["SortBy"] == "SortName"


def test_node_query_recent_limits_and_sorts():
    query = node_query("tvshows", "recentepisodes", "v1")
    assert query["IncludeItemTypes"] == "Episode"
    assert query["SortBy"] == "DateCreated"
    assert query["SortOrder"] == "Descending"
    assert query["Limit"] == 25


def test_node_query_genre_filter():
    query = node_query("movies", "genre-g42", "v1")
    assert query["GenreIds"] == "g42"
    assert query["IncludeItemTypes"] == "Movie"


def test_node_query_special_routes_return_none():
    assert node_query("tvshows", "nextup", "v1") is None
    assert node_query("music", "artists", "v1") is None
    assert node_query("movies", "genres", "v1") is None


def test_node_query_music_albums():
    query = node_query("music", "albums", "v1")
    assert query["IncludeItemTypes"] == "MusicAlbum"
    assert query["SortBy"] == "AlbumArtist,SortName"


def test_content_helpers():
    assert _node_content("tvshows", "nextup") == "episodes"
    assert _node_content("movies", "sets") == "movies"
    assert _node_content("music", "albums") == "albums"
    assert _genre_types("musicvideos") == "MusicVideo"
    assert _guess_content([{"Type": "Photo"}]) == "images"
    assert _guess_content([{"Type": "Unknown"}]) == "videos"
