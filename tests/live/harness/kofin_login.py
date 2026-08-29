"""RunScript harness: sign the loaded profile's kofin into a server, or set its
library whitelist, from inside Kodi -- the same calls plugin/account.py::login
and the library picker make, so the service reacts exactly as it would to the
settings dialog. The password is read from ~/.config/kodi-drive/targets.env
inside the Kodi process and never logged.

    RunScript(<file>,login,<address>,<username>,<TARGETS_KEY>)
    RunScript(<file>,whitelist,<LibraryName>,<LibraryName>,...)
    RunScript(<file>,status)
"""

import os
import sys

import xbmc
import xbmcvfs

sys.path.insert(
    0, xbmcvfs.translatePath("special://home/addons/plugin.video.kofin/lib")
)

from kofin.core import auth, ipc, settings  # noqa: E402
from kofin.core.http import plugin_transport  # noqa: E402
from kofin.core.settings import Credentials  # noqa: E402

TARGETS = os.path.expanduser("~/.config/kodi-drive/targets.env")


def log(msg):
    xbmc.log("kofin-harness: " + msg, xbmc.LOGINFO)


def secret(key):
    with open(TARGETS) as handle:
        for line in handle:
            if line.startswith(key + "="):
                return line.strip().split("=", 1)[1].strip().strip('"').strip("'")
    raise KeyError(key)


def transport_and_header(creds, token=None):
    transport = plugin_transport(settings.get_bool("sslVerify"))
    header = (
        auth.build_auth_header(
            settings.device_name(),
            creds.device_id,
            settings.addon_version(),
            token=token,
        )
        if token
        else auth.build_auth_header(
            settings.device_name(), creds.device_id, settings.addon_version()
        )
    )
    return transport, header


def do_login(address_raw, username, key):
    creds = Credentials.load()
    if creds.is_logged_in:
        log(
            "already logged in to %s as %s; nothing done"
            % (creds.server_name, creds.display_user)
        )
        return
    address = auth.normalize_address(address_raw)
    transport, header = transport_and_header(creds)
    try:
        info = auth.public_info(transport, address)
        server_name = info.get("ServerName") or address
        result = auth.authenticate_password(
            transport, address, header, username, secret(key)
        )
    finally:
        transport.close()
    if not result.token:
        log("login FAILED: no token")
        return
    creds.server_address = address
    creds.server_name = server_name
    creds.server_id = result.server_id
    creds.user_id = result.user_id
    creds.display_user = result.user_name
    creds.token = result.token
    creds.is_logged_in = True
    creds.save()
    ipc.notify(ipc.AUTH_CHANGED)
    log(
        "signed in to %s as %s (server id %s)"
        % (server_name, result.user_name, result.server_id)
    )


def do_whitelist(names):
    creds = Credentials.load()
    if not creds.is_logged_in:
        log("whitelist: not logged in")
        return
    transport, header = transport_and_header(creds, token=creds.token)
    try:
        response = transport.request(
            "GET",
            creds.server_address + "/UserViews",
            headers={"Authorization": header},
            retries=1,
        )
        views = response.json().get("Items", [])
    finally:
        transport.close()
    by_name = {v.get("Name"): v.get("Id") for v in views}
    log("server views: %s" % sorted(by_name))
    ids = [by_name[n] for n in names if n in by_name]
    missing = [n for n in names if n not in by_name]
    if missing:
        log("whitelist: unknown library names %s" % missing)
    csv = ",".join(ids)
    settings.set_str("librarySelection", csv)
    log("librarySelection set to %s (%s)" % (csv, [n for n in names if n in by_name]))


def do_set(pairs):
    """Write add-on settings through the add-on's own setters (the applier
    reacts as it would to the settings dialog). ``key=true``/``false`` is a
    bool, anything else a string (Kodi stores ints as strings)."""
    for pair in pairs:
        key, value = pair.split("=", 1)
        if value.lower() in ("true", "false"):
            settings.set_bool(key, value.lower() == "true")
        else:
            settings.set_str(key, value)
        log("set %s=%s" % (key, value))


def do_status():
    creds = Credentials.load()
    log(
        "status: logged_in=%s server=%s user=%s selection=%s"
        % (
            creds.is_logged_in,
            creds.server_name,
            creds.display_user,
            settings.get_str("librarySelection"),
        )
    )


mode = sys.argv[1]
if mode == "login":
    do_login(sys.argv[2], sys.argv[3], sys.argv[4])
elif mode == "whitelist":
    do_whitelist(sys.argv[2:])
elif mode == "set":
    do_set(sys.argv[2:])
elif mode == "status":
    do_status()
else:
    log("unknown mode %s" % mode)
