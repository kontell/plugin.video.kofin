"""Small RunPlugin actions: watched/favorite toggles, settings, library
maintenance buttons (Library tab -> IPC -> service library manager)."""

from typing import List, Union

import xbmc
import xbmcgui

from kofin.core import ipc, settings
from kofin.core.api import Api
from kofin.core.http import Http, JellyfinError
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.plugin.router import Request

LOG = Logger(__name__)


def _api() -> Api:
    return Api.from_credentials(
        Http(settings.get_bool("sslVerify")), Credentials.load()
    )


def _refresh() -> None:
    xbmc.executebuiltin("Container.Refresh")


def watched(request: Request) -> None:
    item_id = request.params.get("id", "")
    try:
        _api().mark_played(item_id)
    except JellyfinError as error:
        LOG.warning("mark played failed: %s", error)
        return
    _refresh()


def unwatched(request: Request) -> None:
    item_id = request.params.get("id", "")
    try:
        _api().mark_unplayed(item_id)
    except JellyfinError as error:
        LOG.warning("mark unplayed failed: %s", error)
        return
    _refresh()


def favorite(request: Request) -> None:
    _set_favorite(request, True)


def unfavorite(request: Request) -> None:
    _set_favorite(request, False)


def _set_favorite(request: Request, value: bool) -> None:
    item_id = request.params.get("id", "")
    try:
        _api().set_favorite(item_id, value)
    except JellyfinError as error:
        LOG.warning("favorite toggle failed: %s", error)
        return
    _refresh()


def open_settings(request: Request) -> None:
    xbmc.executebuiltin("Addon.OpenSettings(plugin.video.kofin)")


# -- Library tab buttons -------------------------------------------------------


def update_libraries(request: Request) -> None:
    """Per-library (or all) fast-sync catch-up + prune pass (S2.10)."""
    whitelist = settings.get_list("librarySelection")
    if not whitelist:
        return

    names = _selection_names(whitelist)
    choices: List[Union[str, xbmcgui.ListItem]] = [settings.localized(30267)]
    choices.extend(names)  # "All" first
    picked = xbmcgui.Dialog().multiselect(settings.localized(30270), choices)

    if not picked:  # cancelled or empty
        return

    if 0 in picked:
        # Empty payload = the full-whitelist pass (keeps the retention-repair
        # release path in the service intact).
        ipc.notify(ipc.UPDATE_LIBRARY, {})
    else:
        selected = [whitelist[index - 1] for index in picked]
        ipc.notify(ipc.UPDATE_LIBRARY, {"Id": ",".join(selected)})


def refresh_boxsets(request: Request) -> None:
    ipc.notify(ipc.REFRESH_BOXSETS, {})


def repair_libraries(request: Request) -> None:
    """Per-library picker (or all) -> remove + re-add, after confirmation."""
    whitelist = settings.get_list("librarySelection")
    if not whitelist:
        return

    names = _selection_names(whitelist)
    choices: List[Union[str, xbmcgui.ListItem]] = [settings.localized(30267)]
    choices.extend(names)  # "All" first
    picked = xbmcgui.Dialog().multiselect(settings.localized(30266), choices)

    if not picked:  # cancelled or empty
        return

    if 0 in picked:
        selected = list(whitelist)
    else:
        selected = [whitelist[index - 1] for index in picked]

    if not xbmcgui.Dialog().yesno(
        settings.localized(30266),
        settings.localized(30265) % ", ".join(_selection_names(selected)),
    ):
        return

    ipc.notify(ipc.REPAIR_LIBRARY, {"Id": ",".join(selected)})


def _selection_names(library_ids: List[str]) -> List[str]:
    """View names for the picker; falls back to the raw ids offline."""
    from kofin.sync import db as sync_db
    from kofin.sync import kofindb

    names = []
    try:
        with sync_db.Database("kofin") as opened:
            db = kofindb.JellyfinDatabase(opened.cursor)
            for library_id in library_ids:
                view = db.get_view(library_id.replace("Mixed:", ""))
                names.append(view.view_name if view else library_id)
    except Exception:
        LOG.exception("view names unavailable")
        names = list(library_ids)
    return names
