from kofin.plugin import listitems

SERVER = "http://s:8096"


def test_path_for_playable_and_folder():
    assert (
        listitems.path_for({"Type": "Movie", "Id": "m1"})
        == "plugin://plugin.video.kofin/?mode=play&id=m1"
    )
    series_path = listitems.path_for({"Type": "Series", "Id": "s1"})
    assert "mode=browse" in series_path and "folder=s1" in series_path
    assert "type=series" in series_path


def test_is_folder():
    assert listitems.is_folder({"Type": "Series"}) is True
    assert listitems.is_folder({"Type": "Movie"}) is False
    assert listitems.is_folder({"Type": "Video", "IsFolder": True}) is False
    assert listitems.is_folder({"Type": "Unknown", "IsFolder": True}) is True


def test_resume_and_playcount():
    item = {
        "RunTimeTicks": 60 * 10_000_000,
        "UserData": {
            "PlaybackPositionTicks": 30 * 10_000_000,
            "PlayCount": 0,
            "Played": True,
        },
    }
    position, total = listitems.resume_of(item)
    assert (position, total) == (30.0, 60.0)
    assert listitems.playcount_of(item) == 1
    assert listitems.playcount_of({"UserData": {"PlayCount": 3, "Played": True}}) == 3
    assert listitems.playcount_of({}) == 0


def test_art_primary_and_backdrop():
    art = listitems.art_for(
        {
            "Id": "m1",
            "ImageTags": {"Primary": "p1", "Logo": "l1"},
            "BackdropImageTags": ["b1"],
        },
        SERVER,
    )
    assert art["poster"] == "http://s:8096/Items/m1/Images/Primary?tag=p1"
    assert art["clearlogo"].endswith("Logo?tag=l1")
    assert art["fanart"] == "http://s:8096/Items/m1/Images/Backdrop/0?tag=b1"


def test_art_parent_fallbacks_for_episode():
    art = listitems.art_for(
        {
            "Id": "e1",
            "Type": "Episode",
            "ImageTags": {"Primary": "ep"},
            "SeriesId": "s1",
            "SeriesPrimaryImageTag": "sp",
            "ParentBackdropItemId": "s1",
            "ParentBackdropImageTags": ["sb"],
        },
        SERVER,
    )
    assert art["thumb"].endswith("e1/Images/Primary?tag=ep")
    assert art["poster"].endswith("s1/Images/Primary?tag=sp")
    assert art["tvshow.poster"].endswith("s1/Images/Primary?tag=sp")
    assert art["fanart"].endswith("s1/Images/Backdrop/0?tag=sb")


def test_watched_context_toggles():
    entries = listitems.watched_context(
        {"Type": "Movie", "Id": "m1", "UserData": {"Played": True, "IsFavorite": False}}
    )
    commands = [command for _label, command in entries]
    assert any("mode=unwatched" in command for command in commands)
    assert any("mode=favorite" in command for command in commands)
