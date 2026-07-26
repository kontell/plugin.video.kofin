"""'Who's watching?' — toggle additional users on this device's session.

The session's primary (logged-in) user owns the session and is permanent:
Jellyfin no-ops any attempt to add it as an additional user and offers no way
to remove it, so it is shown in the dialog title rather than the toggle list.
Everyone else is a checkbox; confirming applies the add/remove deltas.

The Advanced tab's shortlist (``whoIsWatchingShortlist``) narrows that list to
the handful of people who actually watch on this device — a server with fifty
accounts otherwise makes the dialog useless.
"""

from typing import Any, Dict, List, Optional, Sequence, Set

import xbmc
import xbmcgui

from kofin.core import settings, toast
from kofin.core.api import Api
from kofin.core.http import Http, JellyfinError, Unauthorized
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.plugin.router import Request

LOG = Logger(__name__)

JsonDict = Dict[str, Any]


def offerable(
    users: Sequence[JsonDict],
    primary_id: str,
    shortlist: Sequence[str],
    on_session: Set[Optional[str]],
) -> List[JsonDict]:
    """The users the toggle dialog lists.

    The primary user is never offered (it owns the session and cannot be
    added or removed). An empty shortlist means "everyone". A user already on
    the session is always listed even when the shortlist has since dropped
    them — otherwise the only way to take them back off would be to put them
    back on the shortlist first.
    """
    eligible = [user for user in users if user.get("Id") != primary_id]
    if not shortlist:
        return eligible
    allowed = set(shortlist)
    return [
        user
        for user in eligible
        if user.get("Id") in allowed or user.get("Id") in on_session
    ]


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
        toast.show(settings.localized(30045), time_ms=4000)
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

    eligible = offerable(
        users, api.user_id, settings.get_list("whoIsWatchingShortlist"), current_ids
    )
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


def select_shortlist(request: Request) -> None:
    """Advanced-tab button: pick which users "Who's watching?" offers.

    Stores ids, not names: a rename on the server must not silently empty the
    shortlist. Selecting nobody clears it, which reads as "offer everyone" —
    the same as never having set one.
    """
    creds = Credentials.load()
    if not creds.is_logged_in:
        return
    api = Api.from_credentials(Http(settings.get_bool("sslVerify")), creds)

    try:
        users = api.users()
    except Unauthorized:
        users = api.public_users()
    except JellyfinError as error:
        LOG.warning("shortlist: user list unavailable: %s", error)
        toast.show(settings.localized(30507), toast.ERROR, time_ms=4000)
        return

    # The primary user is on every session by definition, so it is no more
    # selectable here than it is in the toggle dialog.
    candidates = [user for user in users if user.get("Id") != api.user_id]
    if not candidates:
        return

    shortlist = settings.get_list("whoIsWatchingShortlist")
    preselect = [
        index for index, user in enumerate(candidates) if user.get("Id") in shortlist
    ]

    chosen = xbmcgui.Dialog().multiselect(
        settings.localized(30048),
        [user.get("Name", "") for user in candidates],
        preselect=preselect,
    )
    if chosen is None:
        return  # cancelled; the shortlist is left as-is

    picked = [str(candidates[index].get("Id", "")) for index in chosen]
    settings.set_str("whoIsWatchingShortlist", ",".join(picked))
    LOG.info("who's-watching shortlist updated: %s users", len(picked))
