"""'Who's watching?' — toggle additional users on this device's session.

The session's primary (logged-in) user owns the session and is permanent:
Jellyfin no-ops any attempt to add it as an additional user and offers no way
to remove it, so it is shown in the dialog title rather than the toggle list.
Everyone else is a checkbox; confirming applies the add/remove deltas.
"""

import xbmc
import xbmcgui

from kofin.core import settings
from kofin.core.api import Api
from kofin.core.http import Http, JellyfinError, Unauthorized
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.plugin.router import Request

LOG = Logger(__name__)


def who_is_watching(request: Request) -> None:
    creds = Credentials.load()
    if not creds.is_logged_in:
        return
    api = Api.from_credentials(Http(settings.get_bool("sslVerify")), creds)

    try:
        sessions = api.device_sessions(creds.device_id)
    except JellyfinError as error:
        LOG.warning("session lookup failed: %s", error)
        sessions = []
    if not sessions:
        xbmcgui.Dialog().notification(
            "Kofin", settings.localized(30045), xbmcgui.NOTIFICATION_INFO, 4000, False
        )
        return
    session = sessions[0]
    current_ids = {u.get("UserId") for u in (session.get("AdditionalUsers") or [])}

    try:
        users = api.users()
    except Unauthorized:
        users = api.public_users()
    except JellyfinError as error:
        LOG.warning("user list unavailable: %s", error)
        return

    # The primary user is permanent; it never appears in the toggle list.
    eligible = [user for user in users if user.get("Id") != api.user_id]
    if not eligible:
        return
    names = [user.get("Name", "") for user in eligible]
    preselect = [
        index for index, user in enumerate(eligible) if user.get("Id") in current_ids
    ]

    title = settings.localized(30047) % (creds.display_user or "")
    chosen = xbmcgui.Dialog().multiselect(title, names, preselect=preselect)
    if chosen is None:
        return  # cancelled; the session is left as-is

    picked_ids = {eligible[index].get("Id") for index in chosen}
    session_id = session.get("Id", "")
    changed = False
    try:
        for user in eligible:
            user_id = user.get("Id", "")
            was_on = user_id in current_ids
            now_on = user_id in picked_ids
            if now_on and not was_on:
                api.session_add_user(session_id, user_id)
                changed = True
            elif was_on and not now_on:
                api.session_remove_user(session_id, user_id)
                changed = True
    except JellyfinError as error:
        LOG.warning("session user change failed: %s", error)

    if changed:
        # Redraw the addon root so the "Who's watching?" entry re-reads the
        # session and shows the updated additional-user names.
        xbmc.executebuiltin("Container.Refresh")
