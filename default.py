import os
import sys
from urllib.parse import parse_qsl

import xbmc

# A play makes its stop check before anything else: Kodi raises a stop only on
# the Monitors alive when it stops the script (XBPython::NotifyScriptAborting),
# and a viewer can cancel the busy dialog while the imports below still run
# (0.8 s of them on a Pi 3 just after the add-on was reloaded). A play only: a
# Monitor nobody waits on costs every Python thread in Kodi a step per
# announcement while it lives, and listings are many.
_QUERY = sys.argv[2] if len(sys.argv) > 2 else ""
_STOP = (
    xbmc.Monitor().abortRequested
    if dict(parse_qsl(_QUERY.lstrip("?").rstrip("/"))).get("mode") == "play"
    else None
)

_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

# The gate first, and nothing of kofin's before it: a request it turns away
# must not have paid for the router's imports (plugin/gate.py).
from kofin.plugin import gate

_flight = gate.enter(sys.argv)
if _flight is not None:
    try:
        from kofin.plugin.router import dispatch

        dispatch(sys.argv, stop=_STOP)
    finally:
        gate.leave(_flight)
