"""The library-selection multiselect (settings button -> RunPlugin).

Writes the chosen view ids back to the hidden ``librarySelection`` csv; the
service-side diff engine (service/settings_apply.py) computes add/remove
sets against the synced whitelist and dispatches the work. Selection state
therefore survives failed syncs: the picker only ever records intent.
"""

from typing import Any, Dict, List

import xbmcgui

from kofin.core import settings
from kofin.core.api import Api
from kofin.core.http import Http, JellyfinError
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.plugin.router import Request

LOG = Logger(__name__)

# Library types the sync writers understand (plan §4).
SYNCABLE_TYPES = ("movies", "tvshows", "music", "musicvideos", "mixed")


def syncable_views(views: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """(Id, Name, CollectionType) of the views the writers can sync."""
    result = []
    for view in views:
        collection = view.get("CollectionType") or "mixed"
        if collection in SYNCABLE_TYPES:
            result.append(
                {
                    "Id": view.get("Id", ""),
                    "Name": view.get("Name", ""),
                    "Media": collection,
                }
            )
    return result


def select_libraries(request: Request) -> None:
    creds = Credentials.load()
    if not creds.is_logged_in:
        return

    api = Api.from_credentials(Http(settings.get_bool("sslVerify")), creds)
    try:
        views = api.views().get("Items", [])
    except JellyfinError as error:
        LOG.warning("library picker: views unavailable: %s", error)
        xbmcgui.Dialog().notification(
            "Kofin", settings.localized(30269), xbmcgui.NOTIFICATION_ERROR, 4000, False
        )
        return

    candidates = syncable_views(views)
    if not candidates:
        xbmcgui.Dialog().notification(
            "Kofin", settings.localized(30269), xbmcgui.NOTIFICATION_INFO, 4000, False
        )
        return

    selection = settings.get_list("librarySelection")
    preselect = [
        index for index, view in enumerate(candidates) if view["Id"] in selection
    ]

    chosen = xbmcgui.Dialog().multiselect(
        settings.localized(30268),
        [view["Name"] for view in candidates],
        preselect=preselect,
    )

    if chosen is None:
        return  # cancelled; nothing changes

    picked = [candidates[index]["Id"] for index in chosen]
    settings.set_str("librarySelection", ",".join(picked))
    LOG.info("library selection updated: %s ids", len(picked))
