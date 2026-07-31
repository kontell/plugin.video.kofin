import sys

from kofin.plugin import context


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
    item = {"Id": "i1", "Name": "The Movie", "UserData": {}}

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
    item = {"Id": "i1", "Name": "The Movie", "Type": "Episode", "UserData": {}}

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
    assert [params["mode"] for _, params in options] == ["favorite", "settings"]


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


class _Api:
    def __init__(self, item):
        self._item = item

    def item(self, item_id):
        return self._item


def _manage(monkeypatch, listitem, item):
    """Run manage() over a focused item; return the labels it offered."""
    dialog = _Dialog(-1)  # backs out: only the menu it built is under test
    monkeypatch.setattr(sys, "listitem", listitem, raising=False)
    monkeypatch.setattr(context, "_api", lambda: _Api(item))
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
