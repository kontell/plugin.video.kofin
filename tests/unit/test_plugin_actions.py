"""L1 units for the settings-button actions: which dialogs a library action
puts in front of the user, and what it notifies the service."""

import pytest

from kofin.core import ipc
from kofin.core.http import JellyfinError
from kofin.plugin import actions
from kofin.plugin.router import Request
from tests.unit.fakes import FakeApi, FakeDialog


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


@pytest.fixture
def delete_wired(wired, monkeypatch):
    dialog, _ = wired
    api = FakeApi(delete_item=None)
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
    assert api.args("delete_item") == [("i1",)]


def test_delete_declined_keeps_the_item(delete_wired):
    dialog, api, _ = delete_wired
    dialog.yesno_result = False

    actions.delete_item(_delete_request())

    assert api.args("delete_item") == []


def test_delete_without_confirmation_skips_the_prompt(delete_wired):
    """``deleteNoConfirm`` is scoped to the context-menu entry: picking Delete
    off a menu is already the deliberate act the prompt was guarding."""
    dialog, api, toggles = delete_wired
    toggles["deleteNoConfirm"] = True

    actions.delete_item(_delete_request())

    assert dialog.yesnos == []
    assert api.args("delete_item") == [("i1",)]


def test_delete_stays_off_without_the_opt_in(delete_wired):
    dialog, api, toggles = delete_wired
    toggles["enableDelete"] = False
    toggles["deleteNoConfirm"] = True

    actions.delete_item(_delete_request())

    assert dialog.yesnos == []
    assert api.args("delete_item") == []


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
    # The real strings' placeholder counts matter: 30771 (the container
    # confirmation) formats three — count, size, free space.
    monkeypatch.setattr(
        actions.settings,
        "localized",
        lambda i: {
            30716: "L30716 %s %s",
            30771: "L30771 %s %s %s",
            30806: "L30806 %s %s",
            30810: "L30810 %s %s",
        }.get(i, "L%d %%s" % i),
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

    assert notified == [(ipc.DOWNLOAD_ADD, {"Ids": ["m1"], "Types": ["Movie"]})]
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
    assert notified == [
        (ipc.DOWNLOAD_ADD, {"Ids": ["e1", "e3"], "Types": ["", ""]})
    ]  # e2 refused


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

    assert notified == [(ipc.DOWNLOAD_ADD, {"Ids": ["e2"], "Types": [""]})]


def test_cancel_and_remove_routes(download_wired, monkeypatch):
    from kofin.downloads import store as downloads_store

    notified, dialog, Request = download_wired(FakeDownloadApi({}))
    # Leaves: the store holds a row of their own.
    monkeypatch.setattr(
        downloads_store,
        "get",
        lambda item_id: downloads_store.Download(jellyfin_id=item_id),
    )

    actions.cancel_download(Request("plugin://x", -1, {"id": "c1"}))
    assert notified == [(ipc.DOWNLOAD_CANCEL, {"Id": "c1"})]

    actions.remove_download(Request("plugin://x", -1, {"id": "r1"}))
    assert notified[-1] == (ipc.DOWNLOAD_REMOVE, {"Id": "r1"})
    assert dialog.yesnos == []  # no confirmation: the download is a local copy


def test_container_remove_and_cancel_expand_from_local_state(
    download_wired, monkeypatch
):
    """A show or an album has no row of its own — the ids under it are the
    work. Expanded from kofin.db rather than the server, so both routes keep
    working offline."""
    from kofin.downloads import store as downloads_store

    notified, dialog, Request = download_wired(FakeDownloadApi({}))
    monkeypatch.setattr(downloads_store, "get", lambda item_id: None)
    monkeypatch.setattr(
        downloads_store, "container_done_ids", lambda cid: ["e1", "e2", "e3"]
    )
    monkeypatch.setattr(downloads_store, "container_pending_ids", lambda cid: ["e4"])

    actions.remove_download(Request("plugin://x", -1, {"id": "ser1"}))

    assert dialog.yesnos == []
    assert notified == [
        (ipc.DOWNLOAD_REMOVE, {"Id": "e1"}),
        (ipc.DOWNLOAD_REMOVE, {"Id": "e2"}),
        (ipc.DOWNLOAD_REMOVE, {"Id": "e3"}),
    ]

    notified.clear()
    actions.cancel_download(Request("plugin://x", -1, {"id": "ser1"}))
    assert notified == [(ipc.DOWNLOAD_CANCEL, {"Id": "e4"})]

    # Nothing downloaded under it: nothing notified.
    notified.clear()
    monkeypatch.setattr(downloads_store, "container_done_ids", lambda cid: [])
    actions.remove_download(Request("plugin://x", -1, {"id": "ser1"}))
    assert notified == []


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

    assert notified == [
        (ipc.DOWNLOAD_ADD, {"Ids": ["t1", "t2"], "Types": ["Audio", "Audio"]})
    ]
    assert len(dialog.yesnos) == 1  # music containers confirm like seasons
    assert api.item_params and api.item_params[0].get("ParentId") == "al1"
    assert api.item_params[0].get("IncludeItemTypes") == "Audio"


def test_download_artist_expands_by_artistids(download_wired):
    artist = {"Id": "ar1", "Type": "MusicArtist"}
    tracks = [{"Id": "t1", "Type": "Audio", "CanDownload": True, "MediaSources": []}]
    api = FakeDownloadApi(artist, episodes=tracks)
    notified, dialog, Request = download_wired(api)
    actions.download(Request("plugin://x", -1, {"id": "ar1"}))

    assert notified == [(ipc.DOWNLOAD_ADD, {"Ids": ["t1"], "Types": ["Audio"]})]
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

    assert notified == [
        (ipc.DOWNLOAD_ADD, {"Ids": ["t1", "m1"], "Types": ["Audio", "Movie"]})
    ]


def test_download_show_toggles_and_toasts(download_wired, monkeypatch):
    toggled = []
    monkeypatch.setattr(
        "kofin.downloads.auto.toggle_show", lambda sid: toggled.append(sid) or True
    )
    toasts = []
    monkeypatch.setattr(actions.toast, "show", lambda *a, **k: toasts.append(a[0]))
    _notified, _dialog, Request = download_wired(FakeDownloadApi({}))

    actions.download_show(Request("plugin://x", -1, {"id": "s1", "name": "Show"}))

    assert toggled == ["s1"]
    assert toasts == ["L30762 Show"]


def test_manage_download_shows_keeps_the_ticked(download_wired, monkeypatch):
    monkeypatch.setattr(
        "kofin.downloads.auto.subscribed_shows", lambda: ["s1", "s2", "s3"]
    )
    saved = []
    monkeypatch.setattr(
        "kofin.downloads.auto.save_subscribed_shows",
        lambda ids: saved.append(list(ids)),
    )
    monkeypatch.setattr(actions, "_show_names", lambda ids: list(ids))
    notified, dialog, Request = download_wired(FakeDownloadApi({}))
    dialog.multiselect_result = [0, 2]  # s2 unticked

    actions.manage_download_shows(Request("plugin://x", -1, {}))

    assert saved == [["s1", "s3"]]
    assert dialog.multiselects[0][1] == ["s1", "s2", "s3"]


def test_manage_download_shows_cancel_changes_nothing(download_wired, monkeypatch):
    monkeypatch.setattr("kofin.downloads.auto.subscribed_shows", lambda: ["s1"])
    saved = []
    monkeypatch.setattr(
        "kofin.downloads.auto.save_subscribed_shows",
        lambda ids: saved.append(list(ids)),
    )
    monkeypatch.setattr(actions, "_show_names", lambda ids: list(ids))
    _notified, dialog, Request = download_wired(FakeDownloadApi({}))
    dialog.multiselect_result = None  # cancelled

    actions.manage_download_shows(Request("plugin://x", -1, {}))
    assert saved == []


def test_a_transcoded_album_is_sized_by_its_target_not_its_flacs(
    download_wired, monkeypatch
):
    """The confirmation quoted MediaSources[0].Size — the lossless original
    — for tracks the music transcode was about to re-encode, offering a
    12-track album at its FLAC weight when it would land at a fraction of
    it."""
    album = {"Id": "al1", "Type": "MusicAlbum"}
    # A 5-minute track: 300 s at 128 kbps is ~4.8 MB, against a 50 MB FLAC.
    tracks = [
        {
            "Id": "t1",
            "Type": "Audio",
            "CanDownload": True,
            "RunTimeTicks": 300 * 10_000_000,
            "MediaSources": [{"Size": 50 * 1024**2}],
        }
    ]
    notified, dialog, Request = download_wired(FakeDownloadApi(album, episodes=tracks))
    flags = {"downloadsMusicTranscode": False}
    monkeypatch.setattr(actions.settings, "get_bool", lambda key: flags.get(key, False))
    monkeypatch.setattr(actions.settings, "get_int", lambda key: 128)

    actions.download(Request("plugin://x", -1, {"id": "al1"}))
    assert "50 MB" in dialog.yesnos[-1][1]  # transcoding off: the original

    flags["downloadsMusicTranscode"] = True
    actions.download(Request("plugin://x", -1, {"id": "al1"}))
    assert "4 MB" in dialog.yesnos[-1][1]

    # A track already under the cap downloads untouched, so the smaller of
    # the two is the honest number.
    tracks[0]["MediaSources"][0]["Size"] = 1024**2
    actions.download(Request("plugin://x", -1, {"id": "al1"}))
    assert "1 MB" in dialog.yesnos[-1][1]


def test_the_confirmation_states_the_free_space(download_wired, monkeypatch):
    """ "Download 40 items (12.0 GB)?" is not answerable without knowing what
    is left on the device."""
    from kofin.downloads import files

    album = {"Id": "al1", "Type": "MusicAlbum"}
    # 3 GB against 7 GB free: over the quarter that makes it worth asking.
    tracks = [
        {
            "Id": "t1",
            "Type": "Audio",
            "CanDownload": True,
            "MediaSources": [{"Size": 3 * 1024**3}],
        }
    ]
    notified, dialog, Request = download_wired(FakeDownloadApi(album, episodes=tracks))
    monkeypatch.setattr(files, "free_bytes", lambda root: 7 * 1024**3)

    actions.download(Request("plugin://x", -1, {"id": "al1"}))
    assert dialog.yesnos[-1][1].endswith("7.0 GB")

    # An unanswerable probe says so rather than claiming a full disk.
    monkeypatch.setattr(files, "free_bytes", lambda root: -1)
    actions.download(Request("plugin://x", -1, {"id": "al1"}))
    assert dialog.yesnos[-1][1].endswith("?")


# -- what actually triggers the confirmation ----------------------------------


def _sized_request(download_wired, size, item_type="Movie"):
    """One downloadable leaf of a stated size, wired for actions.download."""
    item = {
        "Id": "m1",
        "Type": item_type,
        "CanDownload": True,
        "MediaSources": [{"Size": size}],
    }
    return download_wired(FakeDownloadApi(item))


def test_a_small_request_queues_without_asking(download_wired, monkeypatch):
    """Whatever the count. The old rule asked about every container, so
    thirty three-minute tracks needed an answer."""
    from kofin.downloads import files

    notified, dialog, Request = _sized_request(download_wired, 1024**3)
    monkeypatch.setattr(files, "free_bytes", lambda root: 100 * 1024**3)

    actions.download(Request("plugin://x", -1, {"id": "m1"}))

    assert dialog.yesnos == []
    assert notified and notified[-1][0] == actions.ipc.DOWNLOAD_ADD


def test_a_big_single_item_asks(download_wired, monkeypatch):
    """The case the container rule missed entirely: one 40 GB film went in
    silence because a Movie is not a container."""
    from kofin.downloads import files

    notified, dialog, Request = _sized_request(download_wired, 40 * 1024**3)
    monkeypatch.setattr(files, "free_bytes", lambda root: 100 * 1024**3)

    actions.download(Request("plugin://x", -1, {"id": "m1"}))

    assert dialog.yesnos  # asked, despite being a single leaf
    assert notified and notified[-1][0] == actions.ipc.DOWNLOAD_ADD


def test_a_request_that_will_not_fit_is_refused_outright(download_wired, monkeypatch):
    """Not a question — the manager would only fail it item by item further
    down, after some of it had already been written."""
    from kofin.downloads import files

    notified, dialog, Request = _sized_request(download_wired, 9 * 1024**3)
    monkeypatch.setattr(files, "free_bytes", lambda root: 10 * 1024**3)
    shown = []
    monkeypatch.setattr(actions.toast, "show", lambda *a, **k: shown.append(a))

    actions.download(Request("plugin://x", -1, {"id": "m1"}))

    assert dialog.yesnos == []  # never asked
    assert notified == []  # and nothing queued
    assert shown and shown[-1][0].startswith("L30810")


def test_the_reserve_is_what_makes_a_tight_fit_a_refusal(download_wired, monkeypatch):
    """9 GB into 10 GB free fits arithmetically and still refuses: Kodi's own
    caches and databases usually live on the same volume, and filling it to
    the last byte breaks those first."""
    from kofin.downloads import files

    notified, dialog, Request = _sized_request(download_wired, 9 * 1024**3)

    monkeypatch.setattr(files, "free_bytes", lambda root: 10 * 1024**3)
    actions.download(Request("plugin://x", -1, {"id": "m1"}))
    assert notified == []

    # The same request with the reserve's worth of room to spare goes ahead.
    monkeypatch.setattr(files, "free_bytes", lambda root: 12 * 1024**3)
    actions.download(Request("plugin://x", -1, {"id": "m1"}))
    assert notified and notified[-1][0] == actions.ipc.DOWNLOAD_ADD


# -- delete every download (the settings button) -------------------------------


class _StoreRow:
    def __init__(self, item_id, bytes_done):
        self.jellyfin_id = item_id
        self.bytes_done = bytes_done


def test_delete_all_downloads_confirms_then_fires_one_message(
    download_wired, monkeypatch
):
    """One IPC for the whole request: the service walks the store itself,
    and a NotifyAll per row would put a library's worth of messages through
    Kodi's notification bus for one button press."""
    from kofin.downloads import store as downloads_store

    notified, dialog, Request = download_wired(FakeDownloadApi({"Id": "x"}))
    monkeypatch.setattr(
        downloads_store,
        "rows",
        lambda state=None: [_StoreRow("m1", 3 * 1024**3), _StoreRow("m2", 1024**3)],
    )

    actions.delete_all_downloads(Request("plugin://x", -1, {}))

    # Sized from what is on disk, not what was promised.
    assert dialog.yesnos[-1][1] == "L30806 2 4.0 GB"
    assert notified == [(actions.ipc.DOWNLOAD_REMOVE_ALL, None)]


def test_delete_all_downloads_respects_a_no(download_wired, monkeypatch):
    from kofin.downloads import store as downloads_store

    notified, dialog, Request = download_wired(FakeDownloadApi({"Id": "x"}))
    dialog.yesno_result = False
    monkeypatch.setattr(
        downloads_store, "rows", lambda state=None: [_StoreRow("m1", 1024**3)]
    )

    actions.delete_all_downloads(Request("plugin://x", -1, {}))

    assert notified == []


def test_delete_all_downloads_says_so_when_there_are_none(download_wired):
    """The store is empty, so there is nothing to confirm — and a yes/no
    offering to delete nothing reads as a bug."""
    notified, dialog, Request = download_wired(FakeDownloadApi({"Id": "x"}))

    actions.delete_all_downloads(Request("plugin://x", -1, {}))

    assert dialog.yesnos == []
    assert notified == []


# --- the resume reset (docs/dynamic-libraries-plan.md W2) ----------------------


@pytest.fixture
def reset_wired(monkeypatch):
    api = FakeApi(set_resume_position=None)
    cleared = []
    builtins = []
    toasts = []
    monkeypatch.setattr(actions, "_api", lambda: api)
    monkeypatch.setattr(
        actions.kodirpc,
        "clear_resume_bookmark",
        lambda path: cleared.append(path) or True,
    )
    monkeypatch.setattr(actions.xbmc, "executebuiltin", builtins.append)
    monkeypatch.setattr(actions.settings, "localized", lambda i: "L%d" % i)
    monkeypatch.setattr(
        actions.toast, "show", lambda message, *args, **kwargs: toasts.append(message)
    )
    return api, cleared, builtins, toasts


def test_reset_resume_zeroes_the_server_then_kodis_copy(reset_wired):
    """Both halves, in that order: the server position kofin stamps on the row,
    then the bookmark Kodi keeps for the row's plugin path and would fall back
    to the moment the stamp is gone."""
    api, cleared, builtins, toasts = reset_wired

    actions.reset_resume(
        Request("plugin://x", -1, {"mode": "resetresume", "id": "jf1"})
    )

    assert api.args("set_resume_position") == [("jf1", 0)]
    assert cleared == ["plugin://plugin.video.kofin/?mode=play&id=jf1"]
    assert builtins == ["Container.Refresh"]
    assert toasts == []


def test_reset_resume_leaves_kodi_alone_when_the_server_refuses(reset_wired):
    """Nothing local moves on a failed server call: the listing would otherwise
    say "reset" over a position the server still holds."""
    api, cleared, builtins, toasts = reset_wired
    api.set_response("set_resume_position", JellyfinError("down"))

    actions.reset_resume(
        Request("plugin://x", -1, {"mode": "resetresume", "id": "jf1"})
    )

    assert cleared == []
    assert builtins == []
    assert toasts == ["L30507"]


def test_reset_resume_without_an_id_does_nothing(reset_wired):
    api, cleared, builtins, _toasts = reset_wired

    actions.reset_resume(Request("plugin://x", -1, {"mode": "resetresume"}))

    assert (api.calls, cleared, builtins) == ([], [], [])


# --- _show_names: the picker's titles without a MyVideos open ----------------


def test_show_names_come_from_the_id_map_and_kodi_over_jsonrpc(monkeypatch, tmp_path):
    """The plugin process opens kofin.db for the id map and asks Kodi for the
    title; it never opens MyVideos itself (assessment P3). Unmapped shows and
    unanswered calls fall back to the id so the picker still lists them."""
    import json

    from kofin.sync import db as sync_db

    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    try:
        with sync_db.Database("kofin") as opened:
            opened.cursor.execute(
                "INSERT INTO jellyfin(jellyfin_id, media_type, kodi_id) VALUES (?, ?, ?)",
                ("ser1", "tvshow", 7),
            )
            opened.cursor.execute(
                "INSERT INTO jellyfin(jellyfin_id, media_type, kodi_id) VALUES (?, ?, ?)",
                ("ser3", "tvshow", 9),
            )
        asked = []

        def rpc(query):
            request = json.loads(query)
            asked.append((request["method"], request["params"]["tvshowid"]))
            if request["params"]["tvshowid"] == 7:
                return json.dumps({"result": {"tvshowdetails": {"title": "The Show"}}})
            return json.dumps({"error": {"code": -32602, "message": "gone"}})

        monkeypatch.setattr("xbmc.executeJSONRPC", rpc)

        assert actions._show_names(["ser1", "ser2", "ser3"]) == [
            "The Show",
            "ser2",
            "ser3",
        ]
        assert asked == [
            ("VideoLibrary.GetTVShowDetails", 7),
            ("VideoLibrary.GetTVShowDetails", 9),
        ]
    finally:
        sync_db.reset_overrides()
