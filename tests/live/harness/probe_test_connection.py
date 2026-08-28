"""One-off: run plugin/account.test_connection against a local 500 stub to
check P0.4 (a 5xx becomes a toast, not an uncaught HttpError). Patches the
loaded credentials and the notifier in-process; the profile's real
serverAddress is never touched. RunScript(<file>,<stub_url>)."""
import sys, xbmc, xbmcvfs
sys.path.insert(0, xbmcvfs.translatePath("special://home/addons/plugin.video.kofin/lib"))
from kofin.plugin import account  # noqa: E402
from kofin.core.settings import Credentials  # noqa: E402
from kofin.plugin.router import Request  # noqa: E402

stub = sys.argv[1]
real = account.Credentials.load()
fake = Credentials()
fake.server_address = stub
fake.token = "t"
fake.user_id = "u"
fake.is_logged_in = True
account.Credentials.load = staticmethod(lambda: fake)
captured = []
account._notification = lambda message, level=None: captured.append((level, message))
try:
    account.test_connection(Request("plugin://plugin.video.kofin/", -1, {}))
    xbmc.log("kofin-probe: test_connection captured=%r" % (captured,), xbmc.LOGINFO)
except Exception as e:
    xbmc.log("kofin-probe: test_connection RAISED %s: %s" % (type(e).__name__, e), xbmc.LOGINFO)
finally:
    account.Credentials.load = staticmethod(lambda: real)
