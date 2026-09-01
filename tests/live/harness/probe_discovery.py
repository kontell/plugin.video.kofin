"""RunScript harness: run kofin's LAN discovery from inside Kodi.

Usage (EventServer builtin):
    RunScript(<this file>[,<out path>])

Answers the one thing a desktop run cannot: whether a UDP broadcast sent by
Kodi's own Python on *this* device reaches the LAN and gets its reply back.
On Android that is the open question -- the reply is unicast to our own
ephemeral port, so no MulticastLock should be needed, but that is an
inference until a device says otherwise.

Runs the real ``discovery.scan`` and the real ``auth.probe_public_info``, so
a pass here is the route's substance without its dialogs; it deliberately
touches no setting and no login state. Results go to ``kodi.log`` under the
``kofin-harness:`` prefix and to the output file (default
``/sdcard/kofin-probe-discovery.txt``), which adb can read back.
"""

import sys
import time

import xbmc
import xbmcvfs

sys.path.insert(
    0, xbmcvfs.translatePath("special://home/addons/plugin.video.kofin/lib")
)

from kofin.core import auth, discovery  # noqa: E402
from kofin.core.http import plugin_transport  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "/sdcard/kofin-probe-discovery.txt"

lines = []


def say(text):
    lines.append(text)
    xbmc.log("kofin-harness: %s" % text, xbmc.LOGINFO)


arrivals = []
started = time.time()
try:
    found = discovery.scan(lambda entry: arrivals.append(time.time() - started))
except Exception as error:  # noqa: BLE001 - the whole point is to see it
    say("scan RAISED %r" % (error,))
    found = []
elapsed = time.time() - started

say("scan finished in %.2fs, %d server(s)" % (elapsed, len(found)))
for offset in arrivals:
    say("  reply landed at %.3fs" % offset)

for entry in found:
    say(
        "server %s | %s | src=%s | id=%s"
        % (entry.name, entry.address, entry.source_host, entry.server_id)
    )
    fallback = discovery.fallback_address(entry)
    say("  fallback would be %s" % fallback)
    transport = plugin_transport(True)
    try:
        for address in (entry.address, fallback):
            try:
                info = auth.probe_public_info(transport, address)
            except Exception as error:  # noqa: BLE001
                say("  probe %s FAILED %r" % (address, error))
                continue
            say(
                "  probe %s OK: %s %s"
                % (address, info.get("ServerName"), info.get("Version"))
            )
            break
    finally:
        transport.close()

# Context-managed: a bare close() still left the File object for Kodi to
# complain about ("left several classes in memory") at script teardown.
with xbmcvfs.File(OUT, "w") as handle:
    handle.write("\n".join(lines) + "\n")
xbmc.log("kofin-harness: wrote %s" % OUT, xbmc.LOGINFO)
