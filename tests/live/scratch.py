#!/usr/bin/env python3
"""Scratch Jellyfin libraries for live checks that change something.

Play state, cast and library removal are tested against libraries of
generated clips, never against a real one: a movie, a show of two seasons and
two albums, made with ffmpeg under ``SCRATCH_ROOT`` (a directory the server
can read) and granted to the users the rigs sync as. ``scratch_library.sh``
is the older recipe for the companion gates and shares the library names.

The server is ``JELLYFIN_URL`` with ``JELLYFIN_API_KEY``; neither is printed.

    scratch.py media                 generate the clips
    scratch.py libraries             create the three libraries, wait for the scan
    scratch.py rescan                wait for a scan to pick up changed clips
    scratch.py grant USER [USER...]  let these users see them (policy snapshot kept)
    scratch.py restore-policy        put every snapshotted policy back
    scratch.py ids                   item ids by type and name
    scratch.py cast NAME[,NAME...]   replace the show's cast
    scratch.py userdata USER [TYPE]  play state of every scratch item of a type
    scratch.py mark USER ITEM played|unplayed   as another client would
    scratch.py reset USER            every scratch item unplayed again
    scratch.py cleanup               delete the libraries and the clips
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.environ.get("SCRATCH_ROOT", "/media/bluecon/scratch-kofin")
STATE_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), "kofin-scratch")
STATE = os.path.join(STATE_DIR, "scratch-py-state.json")
LIBRARIES = {
    "movies": ("Scratch Movies", "movies", ["Movie"]),
    "shows": ("Scratch Shows", "tvshows", ["Series", "Season", "Episode"]),
    "music": ("Scratch Music", "music", ["MusicArtist", "MusicAlbum", "Audio"]),
}
EPISODES = [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)]
# Longer than the server's shortest resumable item (five minutes by
# default): anything shorter is marked played at any stop past its first
# twentieth, and a stop half-way would test nothing a real episode shows.
VIDEO_SECONDS = 330
NOISE = "Scratch Album Noise"
ALBUMS = {
    ("Scratch Artist", "Scratch Album One"): 4,
    ("Scratch Artist", "Scratch Album Two"): 4,
    ("Other Scratch Artist", "Scratch Album Three"): 3,
    # One song, so that an album can be opened to play exactly one: Kodi
    # cannot open a library song with a plugin path by its song id
    # (CFileItem::SetFromSong sets no dynamic path, and PAPlayer is handed
    # musicdb://songs/<id>, which never reaches the plugin).
    ("Scratch Artist", "Scratch Album Solo"): 1,
    # One song of noise: a sine tone is 99 kbit/s as FLAC, below every music
    # bitrate limit the settings offer, and noise is far above all of them.
    ("Scratch Artist", NOISE): 1,
}
EXPECTED = {"Movie": 1, "Episode": len(EPISODES), "Audio": sum(ALBUMS.values())}


def _auth():
    return (
        'MediaBrowser Client="scratch", Device="dev", DeviceId="kofin-scratch",'
        ' Version="1", Token="%s"' % os.environ["JELLYFIN_API_KEY"]
    )


def api(method, path, params=None, body=None):
    url = os.environ["JELLYFIN_URL"].rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": _auth(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as reply:
            raw = reply.read()
            return json.loads(raw.decode("utf-8")) if raw.strip() else None
    except urllib.error.HTTPError as error:
        raise SystemExit("%s %s: HTTP %d" % (method, path, error.code)) from error


def state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def save(data):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)


def _ffmpeg(args):
    subprocess.run(["ffmpeg", "-v", "error", "-y"] + args, check=True)


def clip(path, seconds):
    """A test pattern with two audio tracks and a subtitle. A clip that
    exists is left alone: made again it is a new file to the server, and
    every client refreshes the row for its streams."""
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    srt = os.path.join(STATE_DIR, "scratch.srt")
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(srt, "w", encoding="utf-8") as handle:
        handle.write("1\n00:00:01,000 --> 00:00:04,000\nScratch\n")
    _ffmpeg(
        [
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000",
            "-i", srt,
            "-t", str(seconds),
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "64k", "-c:s", "srt",
            "-metadata:s:a:0", "language=eng",
            "-metadata:s:a:1", "language=fra",
            "-metadata:s:s:0", "language=eng",
            "-disposition:a:0", "default", "-disposition:a:1", "0",
            path,
        ]
    )  # fmt: skip


def song(path, seconds, artist, album, title, track, total):
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    source = "sine=frequency=%d:sample_rate=44100" % (300 + 40 * track)
    if album == NOISE:
        source = "anoisesrc=color=white:sample_rate=44100:amplitude=0.05"
    _ffmpeg(
        [
            "-f", "lavfi", "-i", source,
            "-t", str(seconds),
            "-c:a", "flac",
            "-metadata", "title=" + title,
            "-metadata", "artist=" + artist,
            "-metadata", "album_artist=" + artist,
            "-metadata", "album=" + album,
            "-metadata", "track=%d/%d" % (track, total),
            "-metadata", "date=2020",
            path,
        ]
    )  # fmt: skip


def media():
    clip(
        os.path.join(ROOT, "movies/Scratch Movie (2020)/Scratch Movie (2020).mkv"),
        VIDEO_SECONDS,
    )
    for season, episode in EPISODES:
        clip(
            os.path.join(
                ROOT,
                "shows/Scratch Show/Season %d/Scratch Show S%02dE%02d.mkv"
                % (season, season, episode),
            ),
            VIDEO_SECONDS,
        )
    for (artist, album), tracks in ALBUMS.items():
        for track in range(1, tracks + 1):
            song(
                os.path.join(
                    ROOT,
                    "music",
                    artist,
                    album,
                    "%02d - Track %d.flac" % (track, track),
                ),
                25,
                artist,
                album,
                "%s Track %d" % (album.split()[-1], track),
                track,
                tracks,
            )
    subprocess.run(["chmod", "-R", "a+rX", ROOT], check=True)
    count = sum(len(files) for _, _, files in os.walk(ROOT))
    print("%d files under the scratch root" % count)


def libraries():
    data = state()
    existing = {
        folder["Name"]: folder["ItemId"]
        for folder in api("GET", "/Library/VirtualFolders")
    }
    for key, (name, collection, types) in LIBRARIES.items():
        if name not in existing:
            api(
                "POST",
                "/Library/VirtualFolders",
                {"name": name, "collectionType": collection, "refreshLibrary": "true"},
                {
                    "LibraryOptions": {
                        "PathInfos": [{"Path": os.path.join(ROOT, key)}],
                        "EnableChapterImageExtraction": False,
                        "EnableRealtimeMonitor": False,
                        "TypeOptions": [
                            {"Type": kind, "MetadataFetchers": [], "ImageFetchers": []}
                            for kind in types
                        ],
                    }
                },
            )
            print("created %s" % name)
    time.sleep(3)
    existing = {
        folder["Name"]: folder["ItemId"]
        for folder in api("GET", "/Library/VirtualFolders")
    }
    for key, (name, _, _) in LIBRARIES.items():
        data["lib_" + key] = existing.get(name, "")
    save(data)
    # A library made while another is being scanned is not always scanned
    # itself (the music one was left empty), so the scan is asked for.
    api("POST", "/Library/Refresh")
    for _ in range(60):
        found = _counts(data)
        if all(found.get(kind, 0) >= wanted for kind, wanted in EXPECTED.items()):
            break
        time.sleep(5)
    print("scanned: %s" % json.dumps(found, sort_keys=True))
    print(
        "libraries: %s"
        % json.dumps({k: v for k, v in data.items() if k.startswith("lib_")})
    )


def rescan():
    data = state()
    api("POST", "/Library/Refresh")
    for _ in range(60):
        found = _counts(data)
        if all(found.get(kind, 0) >= wanted for kind, wanted in EXPECTED.items()):
            break
        time.sleep(5)
    print("scanned: %s" % json.dumps(found, sort_keys=True))


def _items(data, kinds, user=None, fields="Path"):
    found = []
    for key in LIBRARIES:
        library = data.get("lib_" + key)
        if not library:
            continue
        params = {
            "ParentId": library,
            "Recursive": "true",
            "IncludeItemTypes": ",".join(kinds),
            "Fields": fields,
        }
        if user:
            params["userId"] = user
        found.extend(api("GET", "/Items", params).get("Items", []))
    return found


def _counts(data):
    result = {}
    for item in _items(data, list(EXPECTED)):
        result[item["Type"]] = result.get(item["Type"], 0) + 1
    return result


def grant(users):
    data = state()
    wanted = [data[k] for k in ("lib_movies", "lib_shows", "lib_music") if data.get(k)]
    for user in users:
        record = api("GET", "/Users/%s" % user)
        policy = record["Policy"]
        snapshots = data.setdefault("policies", {})
        # The first snapshot is the user's own; a second grant must not
        # overwrite it with one that already holds the scratch folders.
        snapshots.setdefault(user, policy)
        if policy.get("EnableAllFolders"):
            print("user sees every folder; nothing to change")
            continue
        folders = list(policy.get("EnabledFolders") or [])
        policy = dict(
            policy, EnabledFolders=folders + [w for w in wanted if w not in folders]
        )
        api("POST", "/Users/%s/Policy" % user, body=policy)
        print("granted %d scratch folder(s)" % len(wanted))
    save(data)


def restore_policy():
    data = state()
    for user, policy in (data.get("policies") or {}).items():
        api("POST", "/Users/%s/Policy" % user, body=policy)
        print("policy restored")
    data.pop("policies", None)
    save(data)


def ids():
    data = state()
    kinds = [
        "Movie",
        "Series",
        "Season",
        "Episode",
        "MusicAlbum",
        "Audio",
    ]
    result = {}
    for item in _items(data, kinds):
        name = item["Name"]
        if item["Type"] == "Episode":
            name = "S%02dE%02d" % (
                item.get("ParentIndexNumber", 0),
                item.get("IndexNumber", 0),
            )
        elif item["Type"] == "Audio":
            name = "%s / %s" % (item.get("Album", ""), item["Name"])
        result["%s:%s" % (item["Type"], name)] = item["Id"]
    json.dump(result, sys.stdout, indent=1, sort_keys=True)
    print()
    return result


def cast(names):
    data = state()
    series = next(
        i for i in _items(data, ["Series"]) if i["Name"].startswith("Scratch")
    )
    users = api("GET", "/Users")
    admin = next(u for u in users if u.get("Policy", {}).get("IsAdministrator"))
    item = api("GET", "/Items/%s" % series["Id"], {"userId": admin["Id"]})
    item["People"] = [
        {"Name": name, "Type": "Actor", "Role": "Role of %s" % name} for name in names
    ]
    api("POST", "/Items/%s" % series["Id"], body=item)
    print("cast set to %d name(s)" % len(names))


def userdata(user, kind="Episode"):
    data = state()
    rows = {}
    for item in _items(data, [kind], user=user):
        name = item["Name"]
        if kind == "Episode":
            name = "S%02dE%02d" % (
                item.get("ParentIndexNumber", 0),
                item.get("IndexNumber", 0),
            )
        elif kind == "Audio":
            name = "%s / %s" % (item.get("Album", ""), item["Name"])
        played = item.get("UserData") or {}
        rows[name] = {
            "id": item["Id"],
            "Played": played.get("Played"),
            "PlayCount": played.get("PlayCount"),
            "LastPlayedDate": played.get("LastPlayedDate"),
            "PositionTicks": played.get("PlaybackPositionTicks"),
        }
    json.dump(rows, sys.stdout, indent=1, sort_keys=True)
    print()
    return rows


def mark(user, item_id, value):
    method = "POST" if value == "played" else "DELETE"
    api(method, "/UserPlayedItems/%s" % item_id, {"userId": user})
    print("marked %s" % value)


def reset(user):
    data = state()
    count = 0
    for item in _items(data, ["Movie", "Episode", "Audio"], user=user):
        played = item.get("UserData") or {}
        if (
            played.get("Played")
            or played.get("PlayCount")
            or played.get("PlaybackPositionTicks")
        ):
            api("DELETE", "/UserPlayedItems/%s" % item["Id"], {"userId": user})
            api(
                "POST",
                "/UserItems/%s/UserData" % item["Id"],
                {"userId": user},
                {"PlayCount": 0, "PlaybackPositionTicks": 0, "Played": False},
            )
            count += 1
    print("%d item(s) reset" % count)


def cleanup():
    data = state()
    for key, (name, _, _) in LIBRARIES.items():
        if data.get("lib_" + key):
            api(
                "DELETE",
                "/Library/VirtualFolders",
                {"name": name, "refreshLibrary": "true"},
            )
            print("deleted %s" % name)
            data.pop("lib_" + key, None)
    save(data)
    shutil.rmtree(ROOT, ignore_errors=True)
    print("clips removed")


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    command = argv[1]
    if command == "media":
        media()
    elif command == "libraries":
        libraries()
    elif command == "rescan":
        rescan()
    elif command == "grant":
        grant(argv[2:])
    elif command == "restore-policy":
        restore_policy()
    elif command == "ids":
        ids()
    elif command == "cast":
        cast([name for name in argv[2].split(",") if name])
    elif command == "userdata":
        userdata(argv[2], argv[3] if len(argv) > 3 else "Episode")
    elif command == "mark":
        mark(argv[2], argv[3], argv[4])
    elif command == "reset":
        reset(argv[2])
    elif command == "cleanup":
        cleanup()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
