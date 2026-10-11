#!/bin/sh
# Runs ON the device: put back the profile snapshot.sh kept, and the add-on
# it kept with it (restore.sh keep-build leaves the staged build installed).
# Thumbnails the bench runs cached and the restored texture database does
# not know are removed.
N="${KOFIN_BENCH_DIR:-/storage/kofin-bench}"
U=/storage/.kodi/userdata
S=$N/snapshot-user
[ -d "$S/Database" ] || { echo "no snapshot at $S"; exit 1; }
systemctl stop kodi
while pgrep -x kodi.bin >/dev/null; do sleep 1; done
rm -f $U/Database/MyMusic*.db* $U/Database/MyVideos*.db* $U/Database/Textures*.db*
cp -a "$S"/Database/. $U/Database/
rm -rf $U/addon_data/plugin.video.kofin
cp -a "$S/addon_data/plugin.video.kofin" $U/addon_data/
for f in guisettings.xml sources.xml; do [ -e "$S/$f" ] && cp -a "$S/$f" $U/; done
for d in library playlists; do [ -d "$S/$d" ] && { rm -rf "$U/$d"; cp -a "$S/$d" $U/; }; done
if [ "$1" != "keep-build" ] && [ -d "$S/addon" ]; then
  rm -rf /storage/.kodi/addons/plugin.video.kofin
  cp -a "$S/addon" /storage/.kodi/addons/plugin.video.kofin
fi
python3 - <<'PY'
import glob, os, sqlite3
known = set()
for path in glob.glob("/storage/.kodi/userdata/Database/Textures*.db"):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    known.update(r[0] for r in c.execute("SELECT cachedurl FROM texture"))
    c.close()
root = "/storage/.kodi/userdata/Thumbnails"
removed = kept = 0
if known:  # an unreadable texture database is no licence to empty the cache
    for sub in "0123456789abcdef":
        for name in os.listdir(os.path.join(root, sub)) if os.path.isdir(os.path.join(root, sub)) else []:
            if "%s/%s" % (sub, name) in known:
                kept += 1
            else:
                os.remove(os.path.join(root, sub, name))
                removed += 1
print("thumbnails: %d kept, %d from the bench runs removed" % (kept, removed))
PY
sync
systemctl start kodi
echo restored
