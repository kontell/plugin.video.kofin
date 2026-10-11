#!/bin/sh
# Runs ON the device. Install the staged build, reset Kodi to an empty
# library with the snapshot's login and settings, select one library, start
# Kodi and sample until its first import has settled.
#
#   bench.sh <label> <library-id> <rows-expected> [timeout-s] [mode] [kind]
#
#   mode   none      Kodi makes its databases at this start (rollback journal)
#          delete    the same, after a first boot that creates them
#          wal       first boot, then every Kodi database switched to WAL
#          wal-idx   wal, and the companion's name indexes made beforehand
#          delete-idx
#   kind   songs | movies | episodes   (what <rows-expected> counts)
#
# This DELETES Kodi's music, video and texture databases and kofin's own
# state. Run snapshot.sh first; restore.sh puts the device back.
LABEL="$1"; LIB="$2"; WANT="$3"; LIMIT="${4:-3600}"; MODE="${5:-none}"; KIND="${6:-songs}"
case "$KIND" in
  movies) COUNT_METHOD=VideoLibrary.GetMovies ;;
  episodes) COUNT_METHOD=VideoLibrary.GetEpisodes ;;
  *) COUNT_METHOD=AudioLibrary.GetSongs ;;
esac
N="${KOFIN_BENCH_DIR:-/storage/kofin-bench}"
[ -d "$N/snapshot-user" ] || { echo "run snapshot.sh first"; exit 1; }
U=/storage/.kodi/userdata
A=/storage/.kodi/addons/plugin.video.kofin
D=$U/addon_data/plugin.video.kofin
L=/storage/.kodi/temp/kodi.log
R=$N/runs/$LABEL
mkdir -p $R
systemctl stop kodi
while pgrep -x kodi.bin >/dev/null; do sleep 1; done
# the build
rm -rf $A; mkdir -p $A; tar xzf $N/stage.tgz -C $A
# the profile: empty Kodi databases, no kofin store, the login kept
rm -f $U/Database/MyMusic*.db* $U/Database/MyVideos*.db* $U/Database/Textures*.db*
rm -rf $U/library/video/kofin $U/playlists/video/Kofin $U/playlists/music/Kofin
rm -rf $D; mkdir -p $D
# Kodi's own settings as the snapshot had them: a run stopped mid-scan can
# leave one the pass was holding (videolibrary.actorthumbs), and the record
# that would restore it went with kofin's state just above.
cp "$N/snapshot-user/guisettings.xml" $U/guisettings.xml
python3 - "$N/snapshot-user/addon_data/plugin.video.kofin/settings.xml" "$D/settings.xml" "$LIB" <<'PY'
import re, sys
src, dst, lib = sys.argv[1:4]
text = open(src, encoding="utf-8").read()
def put(key, value):
    global text
    pattern = re.compile(r'<setting id="%s"[^>]*?(/>|>[^<]*</setting>)' % re.escape(key))
    new = '<setting id="%s">%s</setting>' % (key, value)
    text, n = pattern.subn(new, text, count=1)
    if not n:
        text = text.replace("</settings>", "    " + new + "\n</settings>")
put("librarySelection", lib)
put("syncedLibraries", "")
put("syncStatus", "")
put("lastIncrementalSync", "")
put("viewsHash", "")
open(dst, "w", encoding="utf-8").write(text)
PY
if [ "$MODE" != "none" ]; then
  # First boot: nothing selected, Kodi creates its databases.
  cp $D/settings.xml $R/settings-selected.xml
  sed -i "s#<setting id=\"librarySelection\">[^<]*</setting>#<setting id=\"librarySelection\"></setting>#" $D/settings.xml
  systemctl start kodi
  for i in $(seq 1 90); do grep -q "kofin service" $L 2>/dev/null && [ -f $U/Database/MyMusic84.db ] && break; sleep 1; done
  sleep 15
  systemctl stop kodi
  while pgrep -x kodi.bin >/dev/null; do sleep 1; done
  python3 - "$MODE" <<'PY' > $R/journal.txt 2>&1
import glob, sqlite3, sys
# <journal mode>[-idx]: -idx also makes the companion's actor index.
mode, _, index = sys.argv[1].partition("-")
for path in sorted(glob.glob("/storage/.kodi/userdata/Database/My*.db") + glob.glob("/storage/.kodi/userdata/Database/Textures*.db")):
    c = sqlite3.connect(path)
    print(path.rsplit("/", 1)[-1], "was", c.execute("PRAGMA journal_mode").fetchone()[0], "now", c.execute("PRAGMA journal_mode=%s" % mode).fetchone()[0])
    if index == "idx" and "MyVideos" in path:
        for table in ("actor", "tag", "studio"):
            c.execute("CREATE INDEX IF NOT EXISTS ix_kofin_%s_nocase ON %s (name COLLATE NOCASE)" % (table, table))
        c.commit()
        print(path.rsplit("/", 1)[-1], "kofin indexes:", [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_kofin%'")])
    c.close()
PY
  cat $R/journal.txt
  cp $R/settings-selected.xml $D/settings.xml
  rm -f $D/kofin.db* $D/sync.json
fi
sync
echo 3 > /proc/sys/vm/drop_caches
date +%s > $R/t0
systemctl start kodi
PASS=$(sed -n "s/.*services.webserverpassword[^>]*>\([^<]*\)<.*/\1/p" $U/guisettings.xml)
songs() { curl -s -m 10 -u "kodi:$PASS" -H "Content-Type: application/json" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$COUNT_METHOD\",\"params\":{\"limits\":{\"start\":0,\"end\":0}}}" http://localhost:8080/jsonrpc | sed -n 's/.*"total":\([0-9]*\).*/\1/p'; }
pending() { python3 - <<'PY' 2>/dev/null
import sqlite3
try:
    c = sqlite3.connect("file:/storage/.kodi/userdata/addon_data/plugin.video.kofin/kofin.db?mode=ro", uri=True, timeout=5)
    print(c.execute("select count(*) from api_item where status='pending'").fetchone()[0], c.execute("select count(*) from api_item").fetchone()[0])
except Exception as e:
    print("? ?")
PY
}
T0=$(cat $R/t0); QUIET=0
: > $R/samples.txt
while true; do
  sleep 10
  NOW=$(date +%s); E=$((NOW-T0))
  P=$(pgrep -x kodi.bin | head -1)
  RSS=$(awk '/VmRSS/{print $2}' /proc/$P/status 2>/dev/null)
  AV=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
  CPU=$(awk '{print $14+$15}' /proc/$P/stat 2>/dev/null)
  S=$(songs); PD=$(pending)
  echo "$E songs=$S pending/total=$PD rss=$RSS avail=$AV cpu=$CPU load=$(cut -d' ' -f1 /proc/loadavg) $(vcgencmd measure_temp 2>/dev/null) $(vcgencmd get_throttled 2>/dev/null)" >> $R/samples.txt
  # the four busiest threads since the last sample, by name
  for t in /proc/$P/task/*; do echo "$(awk '{print $14+$15}' $t/stat 2>/dev/null) ${t##*/} $(cat $t/comm 2>/dev/null)"; done > $R/threads.new 2>/dev/null
  if [ -f $R/threads.old ]; then
    echo "$E $(awk 'NR==FNR{old[$2]=$1; next} {d=$1-old[$2]; if (d>20) printf "%s:%d ", $3, d}' $R/threads.old $R/threads.new)" >> $R/threads.txt
  fi
  mv $R/threads.new $R/threads.old
  set -- $PD
  if [ "${S:-0}" -ge "$WANT" ] && [ "$1" = "0" ] && [ "$2" != "0" ] && grep -q "native pass done" $L; then QUIET=$((QUIET+1)); else QUIET=0; fi
  [ $QUIET -ge 3 ] && { echo settled > $R/outcome; break; }
  [ $E -ge $LIMIT ] && { echo timeout > $R/outcome; break; }
done
date +%s > $R/t1
cp $L $R/kodi.log
echo "outcome=$(cat $R/outcome) elapsed=$(( $(cat $R/t1) - T0 )) s (includes 30 s settle) songs=$S pending/total=$PD"
