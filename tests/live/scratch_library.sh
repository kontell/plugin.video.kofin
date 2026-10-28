#!/bin/bash
# The scratch Jellyfin libraries for the versions, extras, collection, favourite and season-plot checks:
# fake clips with a versioned movie, extras, a collection pair and a show, created on the server the
# Kodi profile syncs from and granted to that profile's user. Credentials come from
# ~/.config/kodi-drive/targets.env (never printed); the Kodi host is reached as $KOFIN_RIG_SSH or
# conor@$KODI_P1D_HOST.
# Usage: scratch_library.sh media|libraries|policy|state|ids|collection|remove-collection|favorite on|off|season-plot TEXT|rename-version|restore-policy|cleanup
set -euo pipefail
T="${TMPDIR:-/tmp}/kofin-scratch"; mkdir -p "$T"
STATE=$T/scratch-state.json
ROOT=/media/bluecon/scratch-kofin
# The three values are read as data, never evaluated: a `$(...)` or a backtick in the file is text.
target() {  # target NAME -- the value of NAME= in targets.env, one pair of surrounding quotes removed
  local line value
  line=$(grep -m1 -E "^$1=" ~/.config/kodi-drive/targets.env) || return 0
  value=${line#*=}
  case "$value" in
    \"*\") value=${value#\"}; value=${value%\"} ;;
    \'*\') value=${value#\'}; value=${value%\'} ;;
  esac
  printf '%s' "$value"
}
JELLYFIN_URL=$(target JELLYFIN_URL); JELLYFIN_API_KEY=$(target JELLYFIN_API_KEY); KODI_P1D_HOST=$(target KODI_P1D_HOST)
export JELLYFIN_URL JELLYFIN_API_KEY KODI_P1D_HOST
RIG="${KOFIN_RIG_SSH:-conor@${KODI_P1D_HOST:-p1d}}"
AUTH="Authorization: MediaBrowser Token=\"$JELLYFIN_API_KEY\", Client=\"scratch\", Device=\"dev\", DeviceId=\"scratch-dev\", Version=\"1\""
USER_ID=$(ssh "$RIG" 'grep -o "id=\"userId\"[^>]*>[^<]*" ~/.var/app/tv.kodi.Kodi/data/userdata/profiles/OR/addon_data/plugin.video.kofin/settings.xml | sed "s/.*>//"')
api() { curl -s -m 60 -H "$AUTH" -H 'Content-Type: application/json' "$@"; }
jq_() { python3 -I -c "import json,sys; d=json.load(sys.stdin); $1"; }
state_get() { python3 -I -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ''))" "$STATE" "$1" 2>/dev/null || true; }
state_set() { python3 -I - "$STATE" "$1" "$2" <<'PY'
import json, sys, os
path, key, value = sys.argv[1:4]
data = json.load(open(path)) if os.path.exists(path) else {}
data[key] = value
json.dump(data, open(path, "w"), indent=1)
PY
}
clip() {  # clip OUT [seconds] -- a test pattern with a default audio track and a forced subtitle
  local out="$1" secs="${2:-15}"
  printf '1\n00:00:01,000 --> 00:00:04,000\nScratch\n' > "$T/scratch.srt"
  ffmpeg -v error -y -f lavfi -i "testsrc2=size=320x240:rate=25" -f lavfi -i "sine=frequency=440:sample_rate=48000" -f lavfi -i "sine=frequency=660:sample_rate=48000" -i "$T/scratch.srt" \
    -t "$secs" -map 0:v -map 1:a -map 2:a -map 3:s -c:v libx264 -preset ultrafast -c:a aac -b:a 64k -c:s srt \
    -metadata:s:a:0 language=eng -metadata:s:a:1 language=fra -metadata:s:s:0 language=eng \
    -disposition:a:0 default -disposition:a:1 0 -disposition:s:0 forced "$out"
}
case "${1:-}" in
  media)
    mkdir -p "$ROOT/movies/Scratch Movie (2020)/extras" "$ROOT/movies/Scratch Pair One (2021)" "$ROOT/movies/Scratch Pair Two (2021)" "$ROOT/shows/Scratch Show/Season 1"
    clip "$ROOT/movies/Scratch Movie (2020)/Scratch Movie (2020).mkv" 20
    clip "$ROOT/movies/Scratch Movie (2020)/Scratch Movie (2020) - Extended Cut.mkv" 25
    clip "$ROOT/movies/Scratch Movie (2020)/extras/Making Of.mkv" 8
    clip "$ROOT/movies/Scratch Movie (2020)/extras/Deleted Scene.mkv" 6
    clip "$ROOT/movies/Scratch Pair One (2021)/Scratch Pair One (2021).mkv" 10
    clip "$ROOT/movies/Scratch Pair Two (2021)/Scratch Pair Two (2021).mkv" 10
    clip "$ROOT/shows/Scratch Show/Season 1/Scratch Show S01E01.mkv" 10
    clip "$ROOT/shows/Scratch Show/Season 1/Scratch Show S01E02.mkv" 10
    chmod -R a+rX "$ROOT"; find "$ROOT" -type f | sed "s#$ROOT/##" | sort ;;
  libraries)
    for spec in "Scratch Movies|movies|$ROOT/movies|Movie" "Scratch Shows|tvshows|$ROOT/shows|Series"; do
      IFS='|' read -r name ctype path itype <<< "$spec"
      body=$(python3 -I -c 'import json,sys; print(json.dumps({"LibraryOptions": {"PathInfos": [{"Path": sys.argv[1]}], "EnableChapterImageExtraction": False, "EnableRealtimeMonitor": False, "TypeOptions": [{"Type": sys.argv[2], "MetadataFetchers": [], "ImageFetchers": []}, {"Type": "Season", "MetadataFetchers": [], "ImageFetchers": []}, {"Type": "Episode", "MetadataFetchers": [], "ImageFetchers": []}]}}))' "$path" "$itype")
      code=$(api -o /dev/null -w '%{http_code}' -X POST "$JELLYFIN_URL/Library/VirtualFolders?name=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$name")&collectionType=$ctype&refreshLibrary=true" -d "$body")
      echo "create $name: HTTP $code"
    done
    sleep 3
    api "$JELLYFIN_URL/Library/VirtualFolders" | jq_ 'vals={v["Name"]: v["ItemId"] for v in d if v["Name"].startswith("Scratch")}; print(json.dumps(vals))' > "$T/scratch-libs.json"
    python3 -I - "$STATE" "$T/scratch-libs.json" <<'PY'
import json, sys, os
state = json.load(open(sys.argv[1])) if os.path.exists(sys.argv[1]) else {}
state.update({"lib_movies": json.load(open(sys.argv[2])).get("Scratch Movies", ""), "lib_shows": json.load(open(sys.argv[2])).get("Scratch Shows", "")})
json.dump(state, open(sys.argv[1], "w"), indent=1)
print("libraries:", {k: ("<id>" if v else "") for k, v in state.items() if k.startswith("lib_")})
PY
    ;;
  policy)
    api "$JELLYFIN_URL/Users/$USER_ID" > "$T/scratch-user-before.json"
    python3 -I - "$T/scratch-user-before.json" "$STATE" <<'PY' > "$T/scratch-policy.json"
import json, sys
user = json.load(open(sys.argv[1])); state = json.load(open(sys.argv[2]))
policy = user["Policy"]
print(json.dumps({"EnableAllFolders": policy.get("EnableAllFolders"), "count": len(policy.get("EnabledFolders") or [])}), file=sys.stderr)
if not policy.get("EnableAllFolders"):
    folders = list(policy.get("EnabledFolders") or [])
    for key in ("lib_movies", "lib_shows"):
        if state.get(key) and state[key] not in folders:
            folders.append(state[key])
    policy["EnabledFolders"] = folders
print(json.dumps(policy))
PY
    if grep -q '"EnableAllFolders": false' "$T/scratch-user-before.json" || python3 -I -c 'import json,sys; sys.exit(0 if not json.load(open(sys.argv[1]))["Policy"].get("EnableAllFolders") else 1)' "$T/scratch-user-before.json"; then
      code=$(api -o /dev/null -w '%{http_code}' -X POST "$JELLYFIN_URL/Users/$USER_ID/Policy" -d @"$T/scratch-policy.json"); echo "policy updated: HTTP $code (snapshot in scratch-user-before.json)"
    else echo "policy: user sees every folder; nothing to change"; fi ;;
  restore-policy)
    python3 -I -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["Policy"]))' "$T/scratch-user-before.json" > "$T/scratch-policy-restore.json"
    code=$(api -o /dev/null -w '%{http_code}' -X POST "$JELLYFIN_URL/Users/$USER_ID/Policy" -d @"$T/scratch-policy-restore.json"); echo "policy restored: HTTP $code" ;;
  state)
    api "$JELLYFIN_URL/Items?IncludeItemTypes=Movie,Series,Season,Episode,BoxSet&Recursive=true&SearchTerm=Scratch&Fields=MediaSources,SpecialFeatureCount,Overview,Path&userId=$USER_ID" | jq_ 'items=d.get("Items",[]); [print(i["Type"], "|", i["Name"], "| sources", len(i.get("MediaSources") or []), "| extras", i.get("SpecialFeatureCount", 0), "| fav", (i.get("UserData") or {}).get("IsFavorite"), "| overview", repr((i.get("Overview") or "")[:30])) for i in items]; print(len(items), "items")' ;;
  ids)
    movies=$(state_get lib_movies); shows=$(state_get lib_shows)
    { api "$JELLYFIN_URL/Items?IncludeItemTypes=Movie,BoxSet&Recursive=true&ParentId=$movies&userId=$USER_ID"; echo; api "$JELLYFIN_URL/Items?IncludeItemTypes=Series,Episode&Recursive=true&ParentId=$shows&userId=$USER_ID"; echo; api "$JELLYFIN_URL/Items?IncludeItemTypes=BoxSet&Recursive=true&SearchTerm=Scratch%20Pair&userId=$USER_ID"; echo; } > "$T/scratch-items.txt"
    python3 -I - "$T/scratch-items.txt" "$T/scratch-ids.json" <<'PY'
import json, sys
ids = {}
for line in open(sys.argv[1]):
    line = line.strip()
    if not line:
        continue
    for item in json.loads(line).get("Items", []):
        ids[item["Type"] + ":" + item["Name"]] = item["Id"]
json.dump(ids, open(sys.argv[2], "w"), indent=1)
print(sorted(ids))
PY
    series=$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next((v for k, v in d.items() if k.startswith("Series:")), ""))' "$T/scratch-ids.json")
    if [ -n "$series" ]; then api "$JELLYFIN_URL/Shows/$series/Seasons?userId=$USER_ID" | python3 -I -c 'import json,sys; d=json.load(sys.stdin); ids=json.load(open(sys.argv[1])); [ids.__setitem__("Season:" + i["Name"], i["Id"]) for i in d.get("Items", [])]; json.dump(ids, open(sys.argv[1], "w"), indent=1); print([k for k in ids if k.startswith("Season:")])' "$T/scratch-ids.json"; fi ;;
  collection)
    bash "$0" ids >/dev/null
    one=$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(v for k, v in d.items() if k.startswith("Movie:Scratch Pair One")))' "$T/scratch-ids.json")
    two=$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(v for k, v in d.items() if k.startswith("Movie:Scratch Pair Two")))' "$T/scratch-ids.json")
    api -X POST "$JELLYFIN_URL/Collections?Name=Scratch%20Pair&Ids=$one,$two" | jq_ 'print("collection:", "<id>" if d.get("Id") else d)' ;;
  remove-collection)
    bash "$0" ids >/dev/null
    box=$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(v for k, v in d.items() if k.startswith("BoxSet:Scratch Pair")))' "$T/scratch-ids.json")
    code=$(api -o /dev/null -w '%{http_code}' -X DELETE "$JELLYFIN_URL/Items/$box"); echo "collection deleted: HTTP $code" ;;
  favorite)
    bash "$0" ids >/dev/null
    ep=$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(v for k, v in d.items() if k.startswith("Episode:Scratch Show S01E01")))' "$T/scratch-ids.json")
    if [ "${2:-on}" = on ]; then code=$(api -o /dev/null -w '%{http_code}' -X POST "$JELLYFIN_URL/UserFavoriteItems/$ep?userId=$USER_ID"); else code=$(api -o /dev/null -w '%{http_code}' -X DELETE "$JELLYFIN_URL/UserFavoriteItems/$ep?userId=$USER_ID"); fi
    echo "favorite ${2:-on}: HTTP $code" ;;
  season-plot)
    bash "$0" ids >/dev/null
    season=$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(v for k, v in d.items() if k.startswith("Season:")))' "$T/scratch-ids.json")
    api "$JELLYFIN_URL/Items/$season?userId=$USER_ID" > "$T/scratch-season.json"
    python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); d["Overview"]=sys.argv[2]; print(json.dumps(d))' "$T/scratch-season.json" "$2" > "$T/scratch-season-new.json"
    code=$(api -o /dev/null -w '%{http_code}' -X POST "$JELLYFIN_URL/Items/$season" -d @"$T/scratch-season-new.json"); echo "season plot set: HTTP $code" ;;
  rename-version)
    mv "$ROOT/movies/Scratch Movie (2020)/Scratch Movie (2020) - Extended Cut.mkv" "$ROOT/movies/Scratch Movie (2020)/Scratch Movie (2020) - Festival Cut.mkv"
    rm -f "$ROOT/movies/Scratch Movie (2020)/extras/Deleted Scene.mkv"
    code=$(api -o /dev/null -w '%{http_code}' -X POST "$JELLYFIN_URL/Library/Refresh"); echo "renamed the version, dropped an extra; refresh: HTTP $code" ;;
  cleanup)
    for key in lib_movies lib_shows; do id=$(state_get $key); [ -n "$id" ] && echo "delete library $key: HTTP $(api -o /dev/null -w '%{http_code}' -X DELETE "$JELLYFIN_URL/Library/VirtualFolders?name=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$([ $key = lib_movies ] && echo 'Scratch Movies' || echo 'Scratch Shows')")&refreshLibrary=true")"; done
    rm -rf "$ROOT"; echo "media removed" ;;
  *) echo "usage: $0 media|libraries|policy|state|ids|collection|remove-collection|favorite on|off|season-plot TEXT|rename-version|restore-policy|cleanup" >&2; exit 2 ;;
esac
