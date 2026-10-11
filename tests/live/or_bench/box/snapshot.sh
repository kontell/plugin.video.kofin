#!/bin/sh
# Runs ON the device, once, before the first bench run: keep the profile and
# the add-on the device had, so restore.sh can put them back. Refuses to
# overwrite a snapshot, and keeps nothing of one that did not complete.
N="${KOFIN_BENCH_DIR:-/storage/kofin-bench}"
K="${KODI_HOME:-/storage/.kodi}"
U=$K/userdata
S=$N/snapshot-user
T=$S.partial
[ -e "$S" ] && { echo "snapshot exists at $S; remove it yourself if it is stale"; exit 1; }
systemctl stop kodi
while pgrep -x kodi.bin >/dev/null; do sleep 1; done
ok=0
# Kodi comes back whatever happened; a snapshot that stopped part way is removed.
trap 'systemctl start kodi; [ "$ok" = 1 ] || { rm -rf "$T"; echo "snapshot FAILED: nothing kept" >&2; }' EXIT
set -e
rm -rf "$T"
mkdir -p "$T/addon_data"
cp -a "$U/Database" "$T/"
cp -a "$U/addon_data/plugin.video.kofin" "$T/addon_data/"
for f in guisettings.xml sources.xml library playlists; do
  if [ -e "$U/$f" ]; then cp -a "$U/$f" "$T/"; fi
done
cp -a "$K/addons/plugin.video.kofin" "$T/addon"
mv "$T" "$S"
ok=1
du -sh "$S"
