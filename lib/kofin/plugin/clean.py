"""Clean databases: the logged-out migration cleaner (RunPlugin mode).

Dialog flow per docs/clean-databases-plan.md: guards, the scope confirm, the
music prompt (defaulted by debris detection), the user-nodes toggle, the
texture prompt, then the wipe under a progress dialog and a RestartApp.
Dialogs are fine in the plugin process (logout shows one); what this must
never become is a *directory* route — Kodi re-fetches directory paths on
window churn, which is exactly how jellyfin-kodi's directory-entry reset
re-invoked itself during shutdown in the live audit.
"""

import xbmc
import xbmcgui

from kofin.core import settings, state, toast
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.plugin.router import Request
from kofin.sync import clean, schema

LOG = Logger(__name__)


def _text(string_id: int) -> str:
    return settings.localized(string_id)


def clean_databases(request: Request) -> None:
    if not _guards_pass():
        return

    dialog = xbmcgui.Dialog()
    if not dialog.yesno(
        _text(30650), _text(30656), defaultbutton=xbmcgui.DLG_YESNO_NO_BTN
    ):
        return
    music_default = (
        xbmcgui.DLG_YESNO_YES_BTN
        if clean.music_debris_present()
        else xbmcgui.DLG_YESNO_NO_BTN
    )
    wipe_music = dialog.yesno(_text(30650), _text(30657), defaultbutton=music_default)
    all_nodes = dialog.yesno(
        _text(30650), _text(30658), defaultbutton=xbmcgui.DLG_YESNO_NO_BTN
    )
    purge_art = dialog.yesno(
        _text(30650), _text(30659), defaultbutton=xbmcgui.DLG_YESNO_NO_BTN
    )

    try:
        _run(wipe_music, all_nodes, purge_art)
    except Exception as error:
        # Deliberately broad: a partial wipe must surface, never restart
        # silently. The wipe is idempotent, so running it again is the fix.
        LOG.exception("clean databases failed")
        xbmcgui.Dialog().ok(_text(30650), str(error) or repr(error))
        return

    xbmcgui.Dialog().ok(_text(30650), _text(30665))
    LOG.info("clean databases complete; restarting Kodi")
    xbmc.executebuiltin("RestartApp")


def _guards_pass() -> bool:
    if Credentials.load().is_logged_in:
        toast.show(_text(30652), toast.WARNING)
        return False
    if state.is_sync_active():
        toast.show(_text(30653), toast.WARNING)
        return False
    if xbmc.getCondVisibility("System.AddonIsEnabled(plugin.video.jellyfin)"):
        xbmcgui.Dialog().ok(_text(30650), _text(30654))
        return False
    try:
        clean.preflight()
    except schema.SchemaError as error:
        xbmcgui.Dialog().ok(_text(30650), _text(30655) % error)
        return False
    return True


def _run(wipe_music: bool, all_nodes: bool, purge_art: bool) -> None:
    progress = xbmcgui.DialogProgress()
    progress.create(_text(30650), _text(30660))
    try:
        progress.update(10, _text(30660))
        clean.clean_video_database()
        if wipe_music:
            progress.update(30, _text(30661))
            clean.clean_music_database()
        progress.update(55, _text(30662))
        clean.remove_kofin_state()
        clean.remove_jellyfin_state()
        clean.clear_sync_settings()
        progress.update(70, _text(30663))
        if all_nodes:
            clean.remove_all_nodes()
        else:
            clean.sweep_nodes()
            clean.sweep_music_nodes()
        clean.sweep_playlists()
        if purge_art:
            progress.update(85, _text(30664))
            clean.purge_server_art()
        progress.update(100, _text(30660))
    finally:
        progress.close()
