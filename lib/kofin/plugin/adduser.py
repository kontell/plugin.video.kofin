"""Add or remove additional users on this device's session."""

from typing import List, Union

import xbmcgui

from kofin.core import settings
from kofin.core.api import Api
from kofin.core.http import Http, JellyfinError, Unauthorized
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.plugin.router import Request

LOG = Logger(__name__)

Choices = List[Union[str, xbmcgui.ListItem]]


def add_user(request: Request) -> None:
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
    current = session.get("AdditionalUsers") or []

    options: Choices = [settings.localized(30042)]
    if current:
        options.append(settings.localized(30043))
    choice = xbmcgui.Dialog().select(settings.localized(30041), options)
    if choice < 0:
        return

    try:
        if choice == 0:
            _add(api, session, current)
        else:
            _remove(api, session, current)
    except JellyfinError as error:
        LOG.warning("session user change failed: %s", error)


def _add(api: Api, session: dict, current: list) -> None:
    try:
        users = api.users()
    except Unauthorized:
        users = api.public_users()
    taken = {u.get("UserId") for u in current} | {api.user_id}
    eligible = [u for u in users if u.get("Id") not in taken]
    if not eligible:
        return
    names: Choices = [u.get("Name", "") for u in eligible]
    picked = xbmcgui.Dialog().select(settings.localized(30044), names)
    if picked < 0:
        return
    api.session_add_user(session.get("Id", ""), eligible[picked].get("Id", ""))


def _remove(api: Api, session: dict, current: list) -> None:
    names: Choices = [u.get("UserName", "") for u in current]
    picked = xbmcgui.Dialog().select(settings.localized(30044), names)
    if picked < 0:
        return
    api.session_remove_user(session.get("Id", ""), current[picked].get("UserId", ""))
