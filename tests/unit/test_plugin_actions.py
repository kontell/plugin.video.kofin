"""L1 units for the settings-button actions: which dialogs a library action
puts in front of the user, and what it notifies the service."""

import pytest

from kofin.core import ipc
from kofin.plugin import actions


class FakeDialog:
    """Records every dialog raised, and answers from a canned script."""

    def __init__(self, multiselect=None, yesno=True):
        self.multiselect_result = multiselect
        self.yesno_result = yesno
        self.multiselects = []
        self.yesnos = []

    def multiselect(self, heading, choices, **kwargs):
        self.multiselects.append((heading, list(choices)))
        return self.multiselect_result

    def yesno(self, heading, message, **kwargs):
        self.yesnos.append((heading, message))
        return self.yesno_result


@pytest.fixture
def wired(monkeypatch):
    dialog = FakeDialog()
    notified = []

    monkeypatch.setattr(actions.xbmcgui, "Dialog", lambda: dialog)
    # Every string carries a placeholder: several of these ids take a
    # substitution, and a fake without one turns "the dialog was raised at
    # all" into a formatting error — which would pass for the wrong reason.
    # Placeholder counts follow the real strings.po: 30716 ("Download %s
    # item(s) (%s)?") takes two, the rest take one.
    monkeypatch.setattr(
        actions.settings,
        "localized",
        lambda i: "L%d %%s %%s" % i if i == 30716 else "L%d %%s" % i,
    )
    monkeypatch.setattr(actions.settings, "get_list", lambda key: ["lib1", "lib2"])
    monkeypatch.setattr(actions, "_selection_names", lambda ids: list(ids))
    monkeypatch.setattr(
        actions.ipc, "notify", lambda method, data=None: notified.append((method, data))
    )
    return dialog, notified


def test_repair_asks_nothing_beyond_the_picker(wired):
    """The confirmation here borrowed the *removal* copy — "Remove %s from the
    Kodi library? The items are deleted from this device only." — so repairing
    read as though it would leave the library gone. The picker is already the
    decision and a repair rebuilds from the server, so there is nothing
    destructive to confirm."""
    dialog, notified = wired
    dialog.multiselect_result = [1]  # first real library

    actions.repair_libraries(object())

    assert dialog.yesnos == []
    assert notified == [(ipc.REPAIR_LIBRARY, {"Id": "lib1"})]


def test_repair_all_selects_the_whole_whitelist(wired):
    dialog, notified = wired
    dialog.multiselect_result = [0]  # the "All" row

    actions.repair_libraries(object())

    assert dialog.yesnos == []
    assert notified == [(ipc.REPAIR_LIBRARY, {"Id": "lib1,lib2"})]


def test_repair_cancelled_notifies_nothing(wired):
    dialog, notified = wired
    dialog.multiselect_result = None

    actions.repair_libraries(object())

    assert notified == []


def test_repair_without_a_whitelist_does_nothing(wired, monkeypatch):
    dialog, notified = wired
    monkeypatch.setattr(actions.settings, "get_list", lambda key: [])

    actions.repair_libraries(object())

    assert dialog.multiselects == []
    assert notified == []


# --- delete from server ------------------------------------------------------


class DeletingApi:
    def __init__(self):
        self.deleted = []

    def delete_item(self, item_id):
        self.deleted.append(item_id)


@pytest.fixture
def delete_wired(wired, monkeypatch):
    dialog, _ = wired
    api = DeletingApi()
    toggles = {"enableDelete": True, "deleteNoConfirm": False}
    monkeypatch.setattr(actions, "_api", lambda: api)
    monkeypatch.setattr(
        actions.settings, "get_bool", lambda key: toggles.get(key, False)
    )
    monkeypatch.setattr(actions.xbmc, "executebuiltin", lambda command: None)
    return dialog, api, toggles


def _delete_request():
    return actions.Request(
        "plugin://plugin.video.kofin/", -1, {"id": "i1", "name": "X"}
    )


def test_delete_confirms_first(delete_wired):
    dialog, api, _ = delete_wired

    actions.delete_item(_delete_request())

    assert len(dialog.yesnos) == 1
    assert api.deleted == ["i1"]


def test_delete_declined_keeps_the_item(delete_wired):
    dialog, api, _ = delete_wired
    dialog.yesno_result = False

    actions.delete_item(_delete_request())

    assert api.deleted == []


def test_delete_without_confirmation_skips_the_prompt(delete_wired):
    """``deleteNoConfirm`` is scoped to the context-menu entry: picking Delete
    off a menu is already the deliberate act the prompt was guarding."""
    dialog, api, toggles = delete_wired
    toggles["deleteNoConfirm"] = True

    actions.delete_item(_delete_request())

    assert dialog.yesnos == []
    assert api.deleted == ["i1"]


def test_delete_stays_off_without_the_opt_in(delete_wired):
    dialog, api, toggles = delete_wired
    toggles["enableDelete"] = False
    toggles["deleteNoConfirm"] = True

    actions.delete_item(_delete_request())

    assert dialog.yesnos == []
    assert api.deleted == []


def test_a_failed_delete_reads_as_an_error_and_stays_quiet(delete_wired, monkeypatch):
    """This toast passed neither an icon nor a sound, so Kodi's defaults made a
    failed deletion the one notification in kofin that showed the info glyph
    *and* beeped."""
    import xbmcgui

    from kofin.core.http import JellyfinError

    _dialog, api, _ = delete_wired
    raised = []
    monkeypatch.setattr(
        actions.toast,
        "show",
        lambda message, level=actions.toast.INFO, **kwargs: raised.append(
            (level, kwargs)
        ),
    )

    def boom(item_id):
        raise JellyfinError("nope")

    monkeypatch.setattr(api, "delete_item", boom)

    actions.delete_item(_delete_request())

    assert raised == [(xbmcgui.NOTIFICATION_ERROR, {})]


# --- download routes (plan W1.10) --------------------------------------------


class FakeDownloadApi:
    def __init__(self, item, episodes=(), pages=None):
        self._item = item
        self._episodes = list(episodes)
        self._pages = pages or {}
        self.item_params = []

    def item(self, item_id):
        return self._item

    def episodes(self, series_id, season_id, fields):
        return {"Items": self._episodes}

    def items(self, params):
        self.item_params.append(dict(params))
        start = params.get("StartIndex", 0)
        rows = self._episodes[start : start + params.get("Limit", 200)]
        return {"Items": rows, "TotalRecordCount": len(self._episodes)}


@pytest.fixture
def download_wired(monkeypatch):
    from kofin.plugin.router import Request

    notified = []
    dialog = FakeDialog()
    monkeypatch.setattr(
        actions.ipc, "notify", lambda m, d=None: notified.append((m, d))
    )
    monkeypatch.setattr(actions.xbmcgui, "Dialog", lambda: dialog)
    # The real strings' placeholder counts matter: 30716 formats two values.
    monkeypatch.setattr(
        actions.settings,
        "localized",
        lambda i: {30716: "L30716 %s %s"}.get(i, "L%d %%s" % i),
    )
    monkeypatch.setattr(actions.toast, "show", lambda *a, **k: None)

    from kofin.downloads import store as downloads_store

    monkeypatch.setattr(downloads_store, "rows", lambda state=None: [])

    def wire(api):
        monkeypatch.setattr(actions, "_api", lambda: api)
        return notified, dialog, Request

    return wire


def test_download_movie_notifies_without_a_confirm(download_wired):
    movie = {"Id": "m1", "Type": "Movie", "CanDownload": True, "MediaSources": []}
    notified, dialog, Request = download_wired(FakeDownloadApi(movie))

    actions.download(Request("plugin://x", -1, {"id": "m1"}))

    assert notified == [(ipc.DOWNLOAD_ADD, {"Ids": ["m1"]})]
    assert dialog.yesnos == []


def test_download_season_confirms_then_notifies_the_children(download_wired):
    season = {"Id": "sea1", "Type": "Season", "SeriesId": "ser1"}
    episodes = [
        {"Id": "e1", "CanDownload": True, "MediaSources": [{"Size": 100}]},
        {"Id": "e2", "CanDownload": False, "MediaSources": [{"Size": 100}]},
        {"Id": "e3", "CanDownload": True, "MediaSources": [{"Size": 100}]},
    ]
    notified, dialog, Request = download_wired(FakeDownloadApi(season, episodes))

    actions.download(Request("plugin://x", -1, {"id": "sea1"}))

    assert len(dialog.yesnos) == 1
    assert notified == [(ipc.DOWNLOAD_ADD, {"Ids": ["e1", "e3"]})]  # e2 refused


def test_download_confirm_declined_notifies_nothing(download_wired):
    season = {"Id": "sea1", "Type": "Season", "SeriesId": "ser1"}
    episodes = [{"Id": "e1", "CanDownload": True, "MediaSources": []}]
    notified, dialog, Request = download_wired(FakeDownloadApi(season, episodes))
    dialog.yesno_result = False

    actions.download(Request("plugin://x", -1, {"id": "sea1"}))

    assert notified == []


def test_download_skips_rows_the_store_already_holds(download_wired, monkeypatch):
    from kofin.downloads import store as downloads_store

    series = {"Id": "ser1", "Type": "Series"}
    episodes = [
        {"Id": "e1", "CanDownload": True, "MediaSources": []},
        {"Id": "e2", "CanDownload": True, "MediaSources": []},
    ]
    notified, dialog, Request = download_wired(FakeDownloadApi(series, episodes))
    monkeypatch.setattr(
        downloads_store,
        "rows",
        lambda state=None: [downloads_store.Download(jellyfin_id="e1")],
    )

    actions.download(Request("plugin://x", -1, {"id": "ser1"}))

    assert notified == [(ipc.DOWNLOAD_ADD, {"Ids": ["e2"]})]


def test_cancel_and_remove_routes(download_wired):
    notified, dialog, Request = download_wired(FakeDownloadApi({}))

    actions.cancel_download(Request("plugin://x", -1, {"id": "c1"}))
    assert notified == [(ipc.DOWNLOAD_CANCEL, {"Id": "c1"})]

    actions.remove_download(Request("plugin://x", -1, {"id": "r1", "name": "N"}))
    assert notified[-1] == (ipc.DOWNLOAD_REMOVE, {"Id": "r1"})
    assert len(dialog.yesnos) == 1

    dialog.yesno_result = False
    actions.remove_download(Request("plugin://x", -1, {"id": "r2", "name": "N"}))
    assert len(notified) == 2  # declined: nothing new


def test_download_album_confirms_and_expands(download_wired):
    album = {"Id": "al1", "Type": "MusicAlbum"}
    tracks = [
        {
            "Id": "t1",
            "Type": "Audio",
            "CanDownload": True,
            "MediaSources": [{"Size": 5}],
        },
        {
            "Id": "t2",
            "Type": "Audio",
            "CanDownload": True,
            "MediaSources": [{"Size": 5}],
        },
    ]
    api = FakeDownloadApi(album, episodes=tracks)
    notified, dialog, Request = download_wired(api)
    actions.download(Request("plugin://x", -1, {"id": "al1"}))

    assert notified == [(ipc.DOWNLOAD_ADD, {"Ids": ["t1", "t2"]})]
    assert len(dialog.yesnos) == 1  # music containers confirm like seasons
    assert api.item_params and api.item_params[0].get("ParentId") == "al1"
    assert api.item_params[0].get("IncludeItemTypes") == "Audio"


def test_download_artist_expands_by_artistids(download_wired):
    artist = {"Id": "ar1", "Type": "MusicArtist"}
    tracks = [{"Id": "t1", "Type": "Audio", "CanDownload": True, "MediaSources": []}]
    api = FakeDownloadApi(artist, episodes=tracks)
    notified, dialog, Request = download_wired(api)
    actions.download(Request("plugin://x", -1, {"id": "ar1"}))

    assert notified == [(ipc.DOWNLOAD_ADD, {"Ids": ["t1"]})]
    params = api.item_params[0]
    assert params.get("ArtistIds") == "ar1" and "ParentId" not in params


def test_download_playlist_keeps_only_the_leaves(download_wired):
    playlist = {"Id": "pl1", "Type": "Playlist"}
    entries = [
        {"Id": "t1", "Type": "Audio", "CanDownload": True, "MediaSources": []},
        {"Id": "m1", "Type": "Movie", "CanDownload": True, "MediaSources": []},
        {"Id": "x1", "Type": "TvChannel", "CanDownload": True, "MediaSources": []},
    ]
    api = FakeDownloadApi(playlist, episodes=entries)
    notified, dialog, Request = download_wired(api)
    actions.download(Request("plugin://x", -1, {"id": "pl1"}))

    assert notified == [(ipc.DOWNLOAD_ADD, {"Ids": ["t1", "m1"]})]
