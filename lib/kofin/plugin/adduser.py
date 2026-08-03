"""'Who's watching?' — toggle additional users on this device's session.

The session's primary (logged-in) user owns the session and is permanent:
Jellyfin no-ops any attempt to add it as an additional user and offers no way
to remove it, so it is shown in the dialog title rather than the toggle list.
Everyone else is a checkbox; confirming applies the add/remove deltas.

The Advanced tab's shortlist (``whoIsWatchingShortlist``) narrows that list to
the handful of people who actually watch on this device — a server with fifty
accounts otherwise makes the dialog useless.

The chosen set is also written to the hidden ``whoIsWatching`` setting so the
service can re-attach those users when a new session comes up after a Kodi
restart or websocket reconnect. Jellyfin sessions do not survive either.

The picker runs in the **service**, like SyncPlay's menu: ``who_is_watching``
is only a route handler that validates and fires ``ipc.WHO_IS_WATCHING``, and
``show_picker`` is what the service's worker thread calls. See the note on the
route handler for why blocking in the plugin process breaks node invocation.
"""

from typing import Any, Dict, List, Optional, Sequence, Set

import xbmc
import xbmcgui

from kofin.core import ipc, settings, state, toast
from kofin.core.api import Api
from kofin.core.http import Http, JellyfinError, Unauthorized
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.plugin.router import Request

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# Hidden setting holding the additional-user ids to re-apply on session start.
# Empty means nobody extra (unlike the shortlist, where empty means everyone).
WHO_IS_WATCHING = "whoIsWatching"


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


def users_to_restore(desired: Sequence[str], on_session: Set[str]) -> List[str]:
    """Ids in ``desired`` that are not already on the session.

    Order follows ``desired`` so the setting's left-to-right order is the
    restore order. Empty entries are dropped; the primary user is never in
    this list when the dialog wrote it, but a stale id is harmless (Jellyfin
    no-ops adding the session owner).
    """
    already = set(on_session)
    restored: List[str] = []
    for user_id in desired:
        if not user_id or user_id in already:
            continue
        restored.append(user_id)
        already.add(user_id)  # de-dupe within desired itself
    return restored


def persist_who_is_watching(user_ids: Sequence[Optional[str]]) -> None:
    """Write the full chosen set. Empty clears the setting ("nobody extra")."""
    cleaned = [str(user_id) for user_id in user_ids if user_id]
    settings.set_str(WHO_IS_WATCHING, ",".join(cleaned))


def restore_additional_users(api: Api, device_id: str) -> None:
    """Re-attach saved additional users to the current device session.

    Best-effort and additive only: removals happen through the picker, not
    here. A missing session or a failed add is logged and skipped so a
    websocket connect path is never taken down by this.
    """
    desired = settings.get_list(WHO_IS_WATCHING)
    if not desired:
        return

    try:
        sessions = api.device_sessions(device_id)
    except JellyfinError as error:
        LOG.warning("who's-watching restore: session lookup failed: %s", error)
        return
    if not sessions:
        LOG.debug("who's-watching restore: no device session yet")
        return

    session = sessions[0]
    session_id = session.get("Id", "")
    if not session_id:
        return

    on_session = {
        str(user.get("UserId"))
        for user in (session.get("AdditionalUsers") or [])
        if user.get("UserId")
    }
    # Never try to re-add the primary user; Jellyfin no-ops it, but a stale
    # setting from a previous primary should not spam the log either.
    primary = api.user_id or ""
    missing = [
        user_id
        for user_id in users_to_restore(desired, on_session)
        if user_id != primary
    ]
    if not missing:
        return

    for user_id in missing:
        try:
            api.session_add_user(session_id, user_id)
            LOG.info("who's-watching restored user %s", user_id)
        except JellyfinError as error:
            LOG.warning("who's-watching restore failed for %s: %s", user_id, error)


def who_is_watching(request: Request) -> None:
    """Route handler: ask the service to open the picker, then get out.

    The dialog itself runs in the service (``show_picker``) for the same
    reason SyncPlay's does — a plugin invocation that blocks on a modal cannot
    be reached as a library node, because Kodi runs the node's ``<path>`` as a
    directory fetch and the two fight. Firing and exiting lets that fetch fail
    out at once while the picker comes up over whatever is on screen.
    """
    if not Credentials.load().is_logged_in:
        return
    if not state.is_online():
        toast.show(settings.localized(30045), time_ms=4000)
        return

    LOG.debug("requesting the who's-watching picker from the service")
    ipc.notify(ipc.WHO_IS_WATCHING)


def show_picker(api: Api, creds: Credentials) -> None:
    """Toggle additional users on this device's session. Blocks on a dialog —
    the service runs it on a dedicated worker thread."""
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
        return  # cancelled; the session and the saved set are left as-is

    # Preserve dialog order so the setting is stable across confirms.
    picked_ids = [eligible[index].get("Id") for index in chosen]
    picked_set = {user_id for user_id in picked_ids if user_id}
    # Persist the intended set before the API round trips so a partial failure
    # still re-applies on the next session (service restore retries).
    persist_who_is_watching(picked_ids)

    session_id = session.get("Id", "")
    changed = False
    try:
        for user in eligible:
            user_id = user.get("Id", "")
            was_on = user_id in current_ids
            now_on = user_id in picked_set
            if now_on and not was_on:
                api.session_add_user(session_id, user_id)
                changed = True
            elif was_on and not now_on:
                api.session_remove_user(session_id, user_id)
                changed = True
    except JellyfinError as error:
        LOG.warning("session user change failed: %s", error)

    if changed and xbmc.getCondVisibility("Window.IsMedia"):
        # Redraw the addon root so the "Who's watching?" entry re-reads the
        # session and shows the updated additional-user names. Guarded because
        # this now runs in the service: with no media window up there is no
        # container for the builtin to act on (same policy as sync's widget
        # refresh in sync/library.py).
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
