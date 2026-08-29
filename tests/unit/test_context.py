import sys

from kofin.plugin import context
from tests.unit.fakes import FakeApi


def _options(monkeypatch, item, enable_delete, dynamic=True):
    monkeypatch.setattr(context.settings, "get_bool", lambda key: enable_delete)
    monkeypatch.setattr(context.settings, "localized", lambda i: "L%d" % i)
    monkeypatch.setattr(context.xbmc, "getLocalizedString", lambda i: "K%d" % i)
    return context._manage_options(item, dynamic)


def test_manage_options_favorite_label_follows_state(monkeypatch):
    not_fav = _options(
        monkeypatch, {"Id": "i1", "UserData": {"IsFavorite": False}}, False
    )
    assert not_fav[0][0] == "K14076"  # Add to favourites
    assert not_fav[0][1] == {"mode": "favorite", "id": "i1"}

    fav = _options(monkeypatch, {"Id": "i1", "UserData": {"IsFavorite": True}}, False)
    assert fav[0][0] == "K14077"  # Remove from favourites
    assert fav[0][1]["mode"] == "unfavorite"


def test_manage_options_delete_gated_by_setting(monkeypatch):
    item = {"Id": "i1", "Name": "The Movie", "UserData": {}, "CanDelete": True}

    off = [params["mode"] for _, params in _options(monkeypatch, item, False)]
    assert off == ["favorite", "settings"]

    on = _options(monkeypatch, item, True)
    modes = [params["mode"] for _, params in on]
    assert modes == ["favorite", "delete", "settings"]
    delete_params = next(params for _, params in on if params["mode"] == "delete")
    assert delete_params["name"] == "The Movie"


# --- the watched toggle (moved here off the listing's own context menu) -------


def test_manage_options_watched_label_follows_state(monkeypatch):
    """Kodi pins a listing's own context entries above its Play and offers its
    own "Mark as watched" further down, so kofin's toggle -- the only one that
    reaches the server -- lives here instead, named for the server."""
    unplayed = _options(
        monkeypatch, {"Id": "i1", "Type": "Movie", "UserData": {"Played": False}}, False
    )
    assert unplayed[0] == ("L30508", {"mode": "watched", "id": "i1"})

    played = _options(
        monkeypatch, {"Id": "i1", "Type": "Movie", "UserData": {"Played": True}}, False
    )
    assert played[0] == ("L30509", {"mode": "unwatched", "id": "i1"})


def test_manage_options_watched_leads_the_menu(monkeypatch):
    item = {
        "Id": "i1",
        "Name": "The Movie",
        "Type": "Episode",
        "UserData": {},
        "CanDelete": True,
    }

    modes = [params["mode"] for _, params in _options(monkeypatch, item, True)]
    assert modes == ["watched", "favorite", "delete", "settings"]


def test_manage_options_watched_reaches_songs_and_containers(monkeypatch):
    """The listing-level toggle covered every playable type plus series,
    seasons and boxsets; moving it must not quietly narrow that."""
    for item_type in ("Audio", "Trailer", "Recording", "Series", "Season", "BoxSet"):
        options = _options(
            monkeypatch, {"Id": "i1", "Type": item_type, "UserData": {}}, False
        )
        assert options[0][1]["mode"] == "watched", item_type


def test_manage_options_omits_watched_where_there_is_no_state(monkeypatch):
    options = _options(
        monkeypatch, {"Id": "a1", "Type": "MusicArtist", "UserData": {}}, False
    )
    assert "watched" not in [params["mode"] for _, params in options]


# --- the resume reset ---------------------------------------------------------


def test_manage_options_reset_follows_the_watched_toggle_on_an_in_progress_row(
    monkeypatch,
):
    """Kodi's own "Reset resume position" deletes a MyVideos bookmark a listing
    row does not have and never speaks to the server, so the one that reaches
    the server lives here -- under Kodi's own wording, and only where there is
    a position to reset."""
    in_progress = {
        "Id": "i1",
        "Type": "Episode",
        "UserData": {"PlaybackPositionTicks": 300 * 10_000_000},
    }
    options = _options(monkeypatch, in_progress, False)
    assert options[0][1]["mode"] == "watched"
    assert options[1] == ("K38209", {"mode": "resetresume", "id": "i1"})

    fresh = {"Id": "i1", "Type": "Episode", "UserData": {}}
    modes = [params["mode"] for _, params in _options(monkeypatch, fresh, False)]
    assert "resetresume" not in modes


def test_manage_options_reset_is_for_kofins_own_video_rows(monkeypatch):
    """A library row's Kodi entry works and is forwarded by the service; a song
    has no resume point Kodi could act on."""
    in_progress = {"PlaybackPositionTicks": 300 * 10_000_000}
    library_row = {"Id": "i1", "Type": "Movie", "UserData": in_progress}
    modes = [
        params["mode"]
        for _, params in _options(monkeypatch, library_row, False, dynamic=False)
    ]
    assert "resetresume" not in modes

    song = {"Id": "s1", "Type": "Audio", "UserData": in_progress}
    modes = [params["mode"] for _, params in _options(monkeypatch, song, False)]
    assert "resetresume" not in modes


# --- play all / shuffle -------------------------------------------------------


def test_manage_options_play_all_leads_music_containers(monkeypatch):
    """Kodi offers no Play on a plugin folder in either window, so the album
    row's only way to play as a whole is here -- first, because it is what the
    row is for. Core strings: 22083 "Play all", 191 "Shuffle"."""
    for item_type in ("MusicAlbum", "MusicArtist", "MusicGenre"):
        item = {"Id": "c1", "Type": item_type, "UserData": {}}
        options = _options(monkeypatch, item, False)
        assert options[0] == ("K22083", {"mode": "playall", "id": "c1"}), item_type
        assert options[1] == (
            "K191",
            {"mode": "playall", "id": "c1", "shuffle": "1"},
        ), item_type


def test_manage_options_play_all_takes_audio_playlists_only(monkeypatch):
    audio = {"Id": "p1", "Type": "Playlist", "MediaType": "Audio", "UserData": {}}
    assert _options(monkeypatch, audio, False)[0][1]["mode"] == "playall"

    video = {"Id": "p2", "Type": "Playlist", "MediaType": "Video", "UserData": {}}
    modes = [params["mode"] for _, params in _options(monkeypatch, video, False)]
    assert "playall" not in modes


def test_manage_options_play_all_never_reaches_video(monkeypatch):
    """Music only, by decision: seasons and series keep Kodi's own behaviour."""
    for item_type in ("Season", "Series", "BoxSet", "Movie", "Episode"):
        item = {"Id": "v1", "Type": item_type, "UserData": {}}
        modes = [params["mode"] for _, params in _options(monkeypatch, item, False)]
        assert "playall" not in modes, item_type


def test_manage_options_omits_watched_on_a_library_row(monkeypatch):
    """A synced row already has Kodi's own "Mark as watched", which the service
    forwards to Jellyfin, so the server-named twin is offered only where nothing
    else reaches the server: kofin's own listings."""
    item = {"Id": "i1", "Type": "Episode", "UserData": {}}
    options = _options(monkeypatch, item, False, dynamic=False)
    assert [params["mode"] for _, params in options] == ["favorite", "settings"]


# --- the transcode item's resume prompt ---------------------------------------


class _Tag:
    def __init__(self, dbid=0, media="", resume=0.0):
        self._dbid = dbid
        self._media = media
        self._resume = resume

    def getDbId(self):
        return self._dbid

    def getMediaType(self):
        return self._media

    def getResumeTime(self):
        return self._resume


class _ListItem:
    def __init__(self, tag, kofin_id=""):
        self._tag = tag
        self._kofin_id = kofin_id

    def getProperty(self, key):
        return self._kofin_id if key == "kofin.id" else ""

    def getVideoInfoTag(self):
        return self._tag


class _Dialog:
    """Records what was asked and answers with a scripted index."""

    def __init__(self, choice):
        self.choice = choice
        self.asked = []

    def contextmenu(self, labels):
        self.asked.append(labels)
        return self.choice

    def select(self, heading, labels):  # pragma: no cover - single bitrate here
        self.asked.append(labels)
        return self.choice


def _transcode(monkeypatch, listitem, choice, bookmark=None):
    """Run play_with_transcode over a focused item; return (dialog, builtins)."""
    dialog = _Dialog(choice)
    builtins = []
    monkeypatch.setattr(sys, "listitem", listitem, raising=False)
    monkeypatch.setattr(context.xbmcgui, "Dialog", lambda: dialog)
    monkeypatch.setattr(context.xbmc, "executebuiltin", builtins.append)
    monkeypatch.setattr(context.xbmc, "getLocalizedString", lambda i: "K%d" % i)
    monkeypatch.setattr(context.settings, "get_list", lambda key: ["3"])
    monkeypatch.setattr(context.kodirpc, "resume_seconds", lambda dbid, media: bookmark)
    context.play_with_transcode()
    return dialog, builtins


def test_transcode_resume_states_the_start_position(monkeypatch):
    """PlayMedia's own resume flag cannot carry the answer -- Kodi downgrades it
    to noresume for a plugin path it can read no resume information from -- so
    the position goes in the params, where the play route acts on it."""
    item = _ListItem(_Tag(dbid=200, media="movie"), kofin_id="jf1")
    dialog, builtins = _transcode(monkeypatch, item, choice=0, bookmark=200.0)

    assert len(dialog.asked) == 1  # resume asked, bitrate not (one configured)
    assert builtins == [
        "PlayMedia(plugin://plugin.video.kofin/?mode=play&id=jf1"
        "&transcode=1&bitrate=3&dbid=200&startticks=2000000000)"
    ]


def test_transcode_from_beginning_says_so(monkeypatch):
    item = _ListItem(_Tag(dbid=200, media="movie"), kofin_id="jf1")
    _, builtins = _transcode(monkeypatch, item, choice=1, bookmark=200.0)
    assert "startticks" not in builtins[0]
    assert builtins[0].endswith("&fromstart=1)")


def test_transcode_backing_out_of_the_prompt_plays_nothing(monkeypatch):
    item = _ListItem(_Tag(dbid=200, media="movie"), kofin_id="jf1")
    dialog, builtins = _transcode(monkeypatch, item, choice=-1, bookmark=200.0)
    assert builtins == []
    assert len(dialog.asked) == 1  # and the bitrate was never asked for


def test_transcode_asks_nothing_without_a_resume_point(monkeypatch):
    item = _ListItem(_Tag(dbid=200, media="movie"), kofin_id="jf1")
    dialog, builtins = _transcode(monkeypatch, item, choice=0, bookmark=0.0)
    assert dialog.asked == []
    assert builtins[0].endswith("&fromstart=1)")


def test_transcode_resume_of_a_listing_item_comes_off_the_listitem(monkeypatch):
    """No dbid means no bookmark to read: the resume point the listing
    advertises is the one the play route would act on, so it is the one quoted
    -- and no dbid is passed on to claim otherwise."""
    item = _ListItem(_Tag(resume=200.0), kofin_id="jf1")
    dialog, builtins = _transcode(monkeypatch, item, choice=0, bookmark=None)

    assert dialog.asked == [["K12022", "K12021"]]
    assert "dbid" not in builtins[0]
    assert builtins[0].endswith("&startticks=2000000000)")


def test_transcode_ignores_an_unreadable_bookmark(monkeypatch):
    """resume_seconds answers None when the row cannot be read at all; that is
    not a resume point."""
    item = _ListItem(_Tag(dbid=200, media="movie"), kofin_id="jf1")
    dialog, builtins = _transcode(monkeypatch, item, choice=0, bookmark=None)
    assert dialog.asked == []
    assert builtins[0].endswith("&fromstart=1)")


def test_resume_label_stamps_the_time_like_kodi(monkeypatch):
    monkeypatch.setattr(
        context.xbmc, "getLocalizedString", lambda i: "Resume from {0:s}"
    )
    assert context._resume_label(200.4) == "Resume from 00:03:20"
    assert context._resume_label(3725.0) == "Resume from 01:02:05"


def test_resume_label_survives_a_template_without_a_placeholder(monkeypatch):
    monkeypatch.setattr(context.xbmc, "getLocalizedString", lambda i: "Resume {}{}")
    assert context._resume_label(65) == "00:01:05"


# --- which menu the focused item gets -----------------------------------------


def _manage(monkeypatch, listitem, item):
    """Run manage() over a focused item; return the labels it offered."""
    dialog = _Dialog(-1)  # backs out: only the menu it built is under test
    monkeypatch.setattr(sys, "listitem", listitem, raising=False)
    monkeypatch.setattr(context, "_api", lambda: FakeApi(item=item))
    monkeypatch.setattr(context, "lookup_item_id", lambda dbid, media: "jf1")
    monkeypatch.setattr(context.xbmcgui, "Dialog", lambda: dialog)
    monkeypatch.setattr(context.settings, "get_bool", lambda key: False)
    monkeypatch.setattr(context.settings, "localized", lambda i: "L%d" % i)
    monkeypatch.setattr(context.xbmc, "getLocalizedString", lambda i: "K%d" % i)
    context.manage()
    return dialog.asked[0]


def test_manage_offers_the_watched_toggle_on_a_dynamic_item(monkeypatch):
    item = {"Id": "jf1", "Type": "Episode", "UserData": {}}
    listitem = _ListItem(_Tag(), kofin_id="jf1")
    assert _manage(monkeypatch, listitem, item)[0] == "L30508"


def test_manage_drops_the_watched_toggle_on_a_library_item(monkeypatch):
    """Same item, reached through the kofin.db mapping instead of a property:
    that is what tells a synced row from one of kofin's own listings."""
    item = {"Id": "jf1", "Type": "Episode", "UserData": {}}
    listitem = _ListItem(_Tag(dbid=200, media="episode"))
    assert _manage(monkeypatch, listitem, item) == ["K14076", "L30504"]


def test_extras_is_offered_only_when_the_item_has_some(monkeypatch):
    """The reason this moved out of its own context item: a <visible>
    condition can only ask Kodi, and Kodi's ListItem.HasVideoExtras is
    movie-only by construction (hasVideoExtras is computed in movie_view with
    media_type = 'movie' hardcoded; tvshow_view has no such column), so it is
    false for every show whatever the server says. This menu is ours, built
    from an item already fetched, so it can just ask."""
    without = {"Id": "s1", "Type": "Series", "UserData": {}}
    assert "extras" not in [m["mode"] for _, m in _options(monkeypatch, without, False)]

    with_extras = dict(without, SpecialFeatureCount=2)
    options = _options(monkeypatch, with_extras, False)
    assert "extras" in [m["mode"] for _, m in options]
    params = next(m for _, m in options if m["mode"] == "extras")
    assert params["id"] == "s1"


def test_delete_is_not_offered_to_an_account_the_server_would_refuse(monkeypatch):
    """The opt-in says whether the *user* wants deletion offered; CanDelete
    says whether the *server* would allow it. Without the second, an account
    with no EnableContentDeletion got the entry, a "Delete <name>?" prompt,
    and then "Server request failed" — a 403 for something never permitted.
    Verified on 10.11: /Items/{id}?userId= carries the field with no Fields
    request, False for a normal account and True for an admin."""
    denied = {"Id": "i1", "Name": "The Movie", "UserData": {}, "CanDelete": False}
    modes = [params["mode"] for _, params in _options(monkeypatch, denied, True)]
    assert "delete" not in modes

    allowed = dict(denied, CanDelete=True)
    modes = [params["mode"] for _, params in _options(monkeypatch, allowed, True)]
    assert "delete" in modes


# --- download entries (plan W1.10) -------------------------------------------


def _download_entry_options(monkeypatch, item, downloads=True, row=None, counts=None):
    flags = {"downloadsEnabled": downloads}
    monkeypatch.setattr(context.settings, "get_bool", lambda key: flags.get(key, False))
    monkeypatch.setattr(context.settings, "localized", lambda i: "L%d" % i)
    monkeypatch.setattr(context.xbmc, "getLocalizedString", lambda i: "K%d" % i)
    from kofin.downloads import store as downloads_store

    monkeypatch.setattr(downloads_store, "get", lambda item_id: row)
    monkeypatch.setattr(
        downloads_store,
        "container_counts",
        lambda container_id: counts or {"done": 0, "pending": 0},
    )
    options = context._manage_options(item, dynamic=False)
    return [entry for entry in options if entry[1].get("mode", "").endswith("download")]


def _row(state):
    from kofin.downloads import store as downloads_store

    return downloads_store.Download(jellyfin_id="i1", state=state)


def test_download_offered_only_when_the_server_allows(monkeypatch):

    movie = {"Id": "i1", "Type": "Movie", "Name": "M", "CanDownload": True}
    offered = _download_entry_options(monkeypatch, movie)
    assert offered == [("L30708", {"mode": "download", "id": "i1"})]

    refused = _download_entry_options(
        monkeypatch, {"Id": "i1", "Type": "Movie", "CanDownload": False}
    )
    assert refused == []  # the server would 403; never offer it

    disabled = _download_entry_options(monkeypatch, movie, downloads=False)
    assert disabled == []


def test_download_entry_follows_the_store_state(monkeypatch):
    from kofin.downloads import store as downloads_store

    movie = {"Id": "i1", "Type": "Movie", "Name": "M", "CanDownload": True}

    queued = _download_entry_options(
        monkeypatch, movie, row=_row(downloads_store.QUEUED)
    )
    assert queued == [("L30709", {"mode": "canceldownload", "id": "i1"})]

    done = _download_entry_options(monkeypatch, movie, row=_row(downloads_store.DONE))
    assert done == [("L30710", {"mode": "removedownload", "id": "i1"})]

    failed = _download_entry_options(
        monkeypatch, movie, row=_row(downloads_store.FAILED)
    )
    assert failed == [("L30708", {"mode": "download", "id": "i1"})]  # retry


def test_containers_offer_download_without_the_per_item_gate(monkeypatch):
    """Folders always answer CanDownload false server-side; the route's own
    filter drops refused children instead."""
    series = {"Id": "s1", "Type": "Series", "CanDownload": False}
    assert _download_entry_options(monkeypatch, series) == [
        ("L30708", {"mode": "download", "id": "s1"})
    ]


def test_a_downloaded_container_offers_remove_not_just_download(monkeypatch):
    """A fully downloaded show offered "Download" and nothing else — the one
    entry that had nothing left to do — with no way to remove what it had
    and no sign it held anything. The counts come from kofin.db, so this
    answers offline too."""
    series = {"Id": "s1", "Type": "Series", "Name": "Show", "CanDownload": False}

    done = _download_entry_options(
        monkeypatch, series, counts={"done": 3, "pending": 0}
    )
    assert done == [("L30710", {"mode": "removedownload", "id": "s1"})]

    # Partly downloaded: both, because the rest is still worth fetching.
    partial = _download_entry_options(
        monkeypatch, series, counts={"done": 2, "pending": 1}
    )
    assert [params["mode"] for _, params in partial] == [
        "download",
        "removedownload",
        "canceldownload",
    ]


def test_music_types_join_the_download_gates(monkeypatch):
    song = {"Id": "i1", "Type": "Audio", "Name": "T", "CanDownload": True}
    assert _download_entry_options(monkeypatch, song) == [
        ("L30708", {"mode": "download", "id": "i1"})
    ]
    refused = dict(song, CanDownload=False)
    assert _download_entry_options(monkeypatch, refused) == []

    for container_type in ("MusicAlbum", "MusicArtist", "Playlist"):
        container = {"Id": "c1", "Type": container_type, "CanDownload": False}
        assert _download_entry_options(monkeypatch, container) == [
            ("L30708", {"mode": "download", "id": "c1"})
        ], container_type


def test_series_offers_the_subscription_toggle(monkeypatch):
    monkeypatch.setattr("kofin.downloads.auto.subscribed_shows", lambda: ["subbed"])
    monkeypatch.setattr(context.settings, "localized", lambda i: "L%d" % i)

    fresh = {"Id": "s1", "Type": "Series", "Name": "Show", "CanDownload": False}
    options = context._download_options(fresh)
    assert options[1] == (
        "L30760",
        {"mode": "downloadshow", "id": "s1", "name": "Show"},
    )

    subscribed = dict(fresh, Id="subbed")
    assert context._download_options(subscribed)[1][0] == "L30761"  # stop label

    album = {"Id": "al1", "Type": "MusicAlbum", "CanDownload": False}
    assert len(context._download_options(album)) == 1  # shows only
