"""One-off: run plugin/actions._show_names against the real box and log the
result, to check P0.5 (titles come over JSON-RPC; the plugin opens no
MyVideos). RunScript(<file>,<series_id>,<series_id>,...)."""
import sys, xbmc, xbmcvfs
sys.path.insert(0, xbmcvfs.translatePath("special://home/addons/plugin.video.kofin/lib"))
from kofin.plugin import actions  # noqa: E402
ids = sys.argv[1:] + ["notarealshowid"]  # the trailing one exercises the id fallback
names = actions._show_names(ids)
xbmc.log("kofin-probe: _show_names(%r) = %r" % (ids, names), xbmc.LOGINFO)
