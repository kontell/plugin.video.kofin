#!/bin/sh
# Runs ON the device, once, before the first bench run: keep the profile the
# device had, so restore.sh can put it back. Refuses to overwrite a snapshot.
N="${KOFIN_BENCH_DIR:-/storage/kofin-bench}"
U=/storage/.kodi/userdata
S=$N/snapshot-user
[ -e "$S" ] && { echo "snapshot exists at $S; remove it yourself if it is stale"; exit 1; }
systemctl stop kodi
while pgrep -x kodi.bin >/dev/null; do sleep 1; done
mkdir -p "$S/addon_data"
cp -a $U/Database "$S/"
cp -a $U/addon_data/plugin.video.kofin "$S/addon_data/"
for f in guisettings.xml sources.xml library playlists; do [ -e "$U/$f" ] && cp -a "$U/$f" "$S/"; done
cp -a /storage/.kodi/addons/plugin.video.kofin "$S/addon"
systemctl start kodi
du -sh "$S"
