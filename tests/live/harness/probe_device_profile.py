"""Identity oracle for core/deviceprofile: build the streaming and download
DeviceProfiles from the installed add-on's own settings and write both as
canonical JSON to a file, so a before/after byte-comparison needs no
truncation-prone log line.

RunScript(<file>,<out_path>). The file's sha256 is logged under the
kofin-probe prefix.
"""

import hashlib
import json
import sys

import xbmc
import xbmcvfs

sys.path.insert(
    0, xbmcvfs.translatePath("special://home/addons/plugin.video.kofin/lib")
)
from kofin.core import deviceprofile  # noqa: E402
from kofin.core.deviceprofile import ProfileConfig  # noqa: E402

out_path = sys.argv[1]
payload = {
    "build": deviceprofile.build(ProfileConfig.from_settings()),
    "build_download": deviceprofile.build_download(ProfileConfig.for_downloads()),
}
text = json.dumps(payload, sort_keys=True, indent=1, ensure_ascii=True)
with open(out_path, "w", encoding="utf-8") as handle:
    handle.write(text + "\n")
digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
xbmc.log(
    "kofin-probe: device profile written to %s sha256=%s" % (out_path, digest),
    xbmc.LOGINFO,
)
