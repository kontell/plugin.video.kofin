"""Answer Kodi's video-library existence probe for Kofin-owned paths.

The sync service owns removal of Jellyfin items from Kodi. A cleaner probe
therefore checks the shape of the stored plugin URL, without contacting the
server: a transient outage must not erase a locally synced library. Kodi only
calls this route after addon.xml has opted each video content type in through
``medialibraryscanpath``.
"""

from urllib.parse import urlsplit

import xbmcgui
import xbmcplugin

from kofin.plugin.router import Request


def check_exists(request: Request) -> None:
    """Resolve valid library folders and playable items without playing them."""
    url = urlsplit(request.base_url)
    parts = [part for part in url.path.split("/") if part]
    valid_folder = len(parts) in (1, 2) and all(_jellyfin_id(p) for p in parts)
    valid_file = (
        valid_folder
        and request.params.get("mode") == "play"
        and _jellyfin_id(request.params.get("id", ""))
    )
    valid = (
        request.params.get("kodi_action") == "check_exists"
        and url.scheme == "plugin"
        and url.netloc == "plugin.video.kofin"
        and (valid_file or (valid_folder and "mode" not in request.params))
    )
    # GetPluginResult only reads the success flag; the URL is never played.
    xbmcplugin.setResolvedUrl(
        request.handle, valid, xbmcgui.ListItem(path=request.base_url)
    )


def _jellyfin_id(value: str) -> bool:
    return len(value) == 32 and all(c in "0123456789abcdefABCDEF" for c in value)
