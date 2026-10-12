#!/usr/bin/env python3
"""Device-side probes for a live Kodi, standard library only.

Copy to the device (or run where Kodi runs) and call a subcommand; every
subcommand prints one JSON document. The webserver password is read from the
device's own ``guisettings.xml`` and never printed.

    kodi_probe.py rpc METHOD [PARAMS_JSON]
    kodi_probe.py listen SECONDS            notifications by method, counted
    kodi_probe.py refresh-time KIND ID      seconds for a Refresh* to land
    kodi_probe.py streams KIND [N]          stream details of N random rows
    kodi_probe.py coverage KIND             how many rows have which streams
    kodi_probe.py kofin MESSAGE [DATA_JSON] a kofin command, as its settings send it
    kodi_probe.py counts                    rows per library kind
    kodi_probe.py bounce [+ID|-ID ...]      restart kofin's service, changing its
                                            library selection while it is down
    kodi_probe.py wait-log PATTERN [SECS] [FROM_LINE]   until the log has it
    kodi_probe.py log-lines                 how many lines the log has now
    kodi_probe.py watch-total METHOD SECS   a listing's total, each time it changes
    kodi_probe.py widgets FROM_LINE         home widget re-fetches logged since a line
    kodi_probe.py bar FROM_LINE             kofin's bar since a line: what it showed
                                            and the longest it showed nothing new
    kodi_probe.py log-since LINE REGEX      matching log lines from LINE on
    kodi_probe.py wait-idle [SECS]          until nothing is playing
    kodi_probe.py play ITEM_JSON [STOP_AFTER_S] [NEXT_AFTER_S] [SEEK_PERCENT]
                                            play an item to its end (or stop it),
                                            seeking first if asked

``KODI_USERDATA`` names the userdata directory when it is not one of the
usual two (a Flatpak install, LibreELEC).
"""

import base64
import json
import os
import random
import socket
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

USERDATA = [
    os.environ.get("KODI_USERDATA", ""),
    os.path.expanduser("~/.var/app/tv.kodi.Kodi/data/userdata"),
    "/storage/.kodi/userdata",
    os.path.expanduser("~/.kodi/userdata"),
]

KINDS = {
    "movie": (
        "VideoLibrary.GetMovies",
        "movies",
        "movieid",
        "VideoLibrary.RefreshMovie",
    ),
    "episode": (
        "VideoLibrary.GetEpisodes",
        "episodes",
        "episodeid",
        "VideoLibrary.RefreshEpisode",
    ),
    "tvshow": (
        "VideoLibrary.GetTVShows",
        "tvshows",
        "tvshowid",
        "VideoLibrary.RefreshTVShow",
    ),
}


def _settings():
    for directory in USERDATA:
        path = os.path.join(directory, "guisettings.xml") if directory else ""
        if path and os.path.isfile(path):
            root = ET.parse(path).getroot()
            found = {}
            for node in root.iter("setting"):
                found[node.get("id")] = node.text or ""
            return found
    raise SystemExit("no guisettings.xml found; set KODI_USERDATA")


_SETTINGS = None


def setting(name, default=""):
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = _settings()
    return _SETTINGS.get(name) or default


def rpc(method, params=None, timeout=120):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    ).encode()
    user = setting("services.webserverusername", "kodi")
    token = base64.b64encode(
        ("%s:%s" % (user, setting("services.webserverpassword"))).encode()
    ).decode()
    request = urllib.request.Request(
        "http://127.0.0.1:%s/jsonrpc" % setting("services.webserverport", "8080"),
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Basic " + token},
    )
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        answer = json.loads(reply.read().decode("utf-8"))
    if "error" in answer:
        raise RuntimeError("%s: %s" % (method, json.dumps(answer["error"])))
    return answer.get("result")


def listen(seconds, port=9090):
    """Count the notifications Kodi announces over its TCP JSON-RPC port."""
    counts = {}
    samples = {}
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.settimeout(0.5)
    decoder = json.JSONDecoder()
    buffer = ""
    deadline = time.monotonic() + seconds
    began = time.monotonic()
    timeline = []
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            break
        buffer += chunk.decode("utf-8", "replace")
        while buffer:
            buffer = buffer.lstrip()
            try:
                message, end = decoder.raw_decode(buffer)
            except ValueError:
                break
            buffer = buffer[end:]
            method = message.get("method")
            if not method:
                continue
            data = (message.get("params") or {}).get("data")
            if isinstance(data, dict) and data.get("transaction"):
                method += " (transaction)"
            counts[method] = counts.get(method, 0) + 1
            samples.setdefault(method, data)
            timeline.append((round(time.monotonic() - began, 1), method))
    sock.close()
    return {
        "seconds": seconds,
        "counts": counts,
        "first": samples,
        "timeline": timeline,
    }


def refresh_time(kind, kodi_id, episodes=None, timeout=300):
    """Seconds from a Refresh* call to the row being back, and its new id."""
    listing, key, id_param, refresh = KINDS[kind]
    details = {
        "movie": ("VideoLibrary.GetMovieDetails", "moviedetails"),
        "episode": ("VideoLibrary.GetEpisodeDetails", "episodedetails"),
        "tvshow": ("VideoLibrary.GetTVShowDetails", "tvshowdetails"),
    }[kind]
    before = rpc(details[0], {id_param: kodi_id, "properties": ["title", "uniqueid"]})[
        details[1]
    ]
    wanted = before["uniqueid"].get("kofin")
    params = {id_param: kodi_id, "ignorenfo": False}
    if episodes is not None:
        params["refreshepisodes"] = episodes

    def find():
        rows = rpc(
            listing,
            {
                "properties": ["uniqueid"],
                "filter": {
                    "field": "title",
                    "operator": "is",
                    "value": before["title"],
                },
            },
        ).get(key, [])
        return [r for r in rows if r["uniqueid"].get("kofin") == wanted]

    began = time.monotonic()
    rpc(refresh, params)
    issued = time.monotonic() - began
    gone = None
    while time.monotonic() - began < timeout:
        time.sleep(0.1)
        match = find()
        if not match:
            if gone is None:
                gone = time.monotonic() - began
            continue
        if match[0][id_param] != kodi_id or gone is not None:
            return {
                "kind": kind,
                "issued_s": round(issued, 2),
                "gone_s": None if gone is None else round(gone, 2),
                "back_s": round(time.monotonic() - began, 2),
                "old_id": kodi_id,
                "new_id": match[0][id_param],
                "refreshtoken": match[0]["uniqueid"].get("kofinrefresh"),
            }
        if time.monotonic() - began > 20:
            # The id never moved and the row never left: refreshed in place.
            return {
                "kind": kind,
                "issued_s": round(issued, 2),
                "back_s": None,
                "old_id": kodi_id,
                "new_id": kodi_id,
                "kept_id": True,
            }
    return {"kind": kind, "timed_out": True, "gone_s": gone}


def streams(kind, count=20):
    listing, key, id_param, _ = KINDS[kind]
    total = rpc(listing, {"limits": {"start": 0, "end": 1}})["limits"]["total"]
    picks = sorted(random.sample(range(total), min(count, total)))
    rows = []
    for index in picks:
        row = rpc(
            listing,
            {
                "properties": ["streamdetails", "uniqueid"],
                "limits": {"start": index, "end": index + 1},
                "sort": {"method": "title"},
            },
        )[key][0]
        details = row.get("streamdetails") or {}
        rows.append(
            {
                "id": row[id_param],
                "label": row.get("label"),
                "kofin": (row.get("uniqueid") or {}).get("kofin", ""),
                "video": [
                    "%s %sx%s %s" % (v["codec"], v["width"], v["height"], v["hdrtype"])
                    for v in details.get("video", [])
                ],
                "audio": [
                    "%s %sch %s" % (a["codec"], a["channels"], a["language"])
                    for a in details.get("audio", [])
                ],
                "subtitle": [s["language"] for s in details.get("subtitle", [])],
            }
        )
    with_codec = sum(1 for r in rows if any(v.split(" ")[0] for v in r["video"]))
    with_audio = sum(1 for r in rows if r["audio"])
    return {
        "kind": kind,
        "total": total,
        "sampled": len(rows),
        "with_video_codec": with_codec,
        "with_audio": with_audio,
        "rows": rows,
    }


def coverage(kind, page=500):
    """Every row of a kind: how many carry a video codec, an audio stream,
    a subtitle stream, and the codecs seen."""
    listing, key, _, _ = KINDS[kind]
    total = None
    start = 0
    result = {
        "rows": 0,
        "video_codec": 0,
        "audio": 0,
        "subtitle": 0,
        "audio_streams": 0,
        "subtitle_streams": 0,
    }
    codecs = {}
    bare = []
    while total is None or start < total:
        reply = rpc(
            listing,
            {
                "properties": ["streamdetails"],
                "limits": {"start": start, "end": start + page},
                "sort": {"method": "title"},
            },
        )
        total = reply["limits"]["total"]
        for row in reply.get(key, []):
            details = row.get("streamdetails") or {}
            video = [v for v in details.get("video", []) if v.get("codec")]
            result["rows"] += 1
            result["video_codec"] += bool(video)
            result["audio"] += bool(details.get("audio"))
            result["subtitle"] += bool(details.get("subtitle"))
            result["audio_streams"] += len(details.get("audio", []))
            result["subtitle_streams"] += len(details.get("subtitle", []))
            for v in video:
                codecs[v["codec"]] = codecs.get(v["codec"], 0) + 1
            if not video and len(bare) < 10:
                bare.append(row.get("label"))
        start += page
    result["codecs"] = codecs
    result["without_video_codec"] = bare
    return result


def _userdata():
    for directory in USERDATA:
        if directory and os.path.isfile(os.path.join(directory, "guisettings.xml")):
            return directory
    raise SystemExit("no userdata directory found; set KODI_USERDATA")


def kofin(message, data=None):
    """Send a kofin IPC command with the running service's nonce."""
    profile = rpc("Profiles.GetCurrentProfile")["label"]
    base = _userdata()
    if profile != "Master user":
        base = os.path.join(base, "profiles", profile)
    with open(
        os.path.join(base, "addon_data", "plugin.video.kofin", "ipc.nonce"),
        encoding="utf-8",
    ) as handle:
        nonce = handle.read().strip()
    payload = dict(data or {}, _nonce=nonce)
    return rpc(
        "JSONRPC.NotifyAll",
        {"sender": "plugin.video.kofin", "message": message, "data": [payload]},
    )


def _profile_dir():
    profile = rpc("Profiles.GetCurrentProfile")["label"]
    base = _userdata()
    if profile != "Master user":
        base = os.path.join(base, "profiles", profile)
    return base


def logfile():
    return os.path.join(os.path.dirname(_userdata().rstrip("/")), "temp", "kodi.log")


def log_lines():
    with open(logfile(), "rb") as handle:
        return sum(1 for _ in handle)


def wait_log(pattern, seconds=120.0, start=0):
    """Wait until a log line from ``start`` on contains ``pattern``; returns
    the line, or None at the deadline."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with open(logfile(), encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle):
                if number >= start and pattern in line:
                    return line.rstrip("\n")
        time.sleep(1)
    return None


def log_since(start, pattern):
    import re

    wanted = re.compile(pattern)
    found = []
    with open(logfile(), encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle):
            if number >= start and wanted.search(line):
                found.append(line.rstrip("\n"))
    return found


def watch_total(method, seconds, interval=2.0):
    """A listing's row count over time: one entry each time it changes,
    with the wall clock, to lay beside the log."""
    began = time.monotonic()
    changes = []
    last = None
    while time.monotonic() - began < seconds:
        try:
            total = rpc(method, {"limits": {"start": 0, "end": 1}}, timeout=30)[
                "limits"
            ]["total"]
        except Exception:  # noqa: BLE001 - a busy Kodi misses a poll
            total = None
        if total != last:
            changes.append(
                {
                    "at_s": round(time.monotonic() - began, 1),
                    "clock": time.strftime("%H:%M:%S"),
                    "total": total,
                }
            )
            last = total
        time.sleep(interval)
    return changes


def widgets(start):
    """Home widget re-fetches Kodi logged from a line on (debug logging):
    how many per source, and the last time each was fetched."""
    counts = {}
    last = {}
    for line in log_since(start, r"CDirectoryProvider\[.*\]: refreshing"):
        source = line.split("CDirectoryProvider[", 1)[1].split("]: refreshing", 1)[0]
        counts[source] = counts.get(source, 0) + 1
        last[source] = line[11:19]
    return {
        "refreshes": sum(counts.values()),
        "by_source": {
            source: {"count": counts[source], "last": last[source]}
            for source in sorted(counts, key=lambda k: -counts[k])
        },
    }


def _clock(line):
    hours, minutes, seconds = line[11:23].split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def bar(start):
    """kofin's progress bar from a log line on (debug logging): every text
    it showed with its time, how long it was down between being up, and the
    longest stretch in which what it showed did not change."""
    events = []
    for line in log_since(start, r"kofin\.sync\.backends\.api\.progress: bar"):
        text = line.split("progress: ", 1)[1]
        events.append((_clock(line), line[11:19], text))
    shown = []
    gaps = []
    down_since = None
    last_change = None
    last_text = None
    longest_same = (0.0, "", "")
    for at, clock, text in events:
        if text.startswith("bar down"):
            down_since = at
            last_text = None
            continue
        if down_since is not None:
            gaps.append({"down_s": round(at - down_since, 1), "until": clock})
            down_since = None
        if text != last_text:
            if last_change is not None and at - last_change[0] > longest_same[0]:
                longest_same = (
                    round(at - last_change[0], 1),
                    last_change[1],
                    last_change[2],
                )
            last_change = (at, clock, text)
            last_text = text
            shown.append(
                "%s %s" % (clock, text.replace("bar: ", "").replace("bar up: ", "UP "))
            )
    return {
        "paints": len(events),
        "texts": shown,
        "down_between": gaps,
        "longest_unchanged": {
            "seconds": longest_same[0],
            "from": longest_same[1],
            "text": longest_same[2],
        },
    }


def wait_idle(seconds=300.0):
    """Wait until no player is active; returns how long that took."""
    began = time.monotonic()
    while time.monotonic() - began < seconds:
        if not rpc("Player.GetActivePlayers"):
            return round(time.monotonic() - began, 1)
        time.sleep(0.5)
    return None


def play(item, stop_after=None, next_after=None, seek_percent=None, limit=600.0):
    """Open an item and stay with it: until playback has really begun, then
    to its end, or to a stop after ``stop_after`` seconds of playing, with
    a skip to the next item after ``next_after`` seconds if asked. A seek
    to ``seek_percent`` right after the start makes a long item short."""
    began = time.monotonic()
    rpc("Player.Open", {"item": item})
    started = None
    while time.monotonic() - began < 60:
        players = rpc("Player.GetActivePlayers")
        if players:
            position = rpc(
                "Player.GetProperties",
                {"playerid": players[0]["playerid"], "properties": ["time", "speed"]},
            )
            clock = position["time"]
            seconds = clock["hours"] * 3600 + clock["minutes"] * 60 + clock["seconds"]
            if position["speed"] == 1 and (seconds > 0 or clock["milliseconds"] > 0):
                started = round(time.monotonic() - began, 1)
                break
        time.sleep(0.25)
    if started is None:
        return {"started_s": None}
    if seek_percent is not None:
        time.sleep(3)
        rpc(
            "Player.Seek",
            {
                "playerid": rpc("Player.GetActivePlayers")[0]["playerid"],
                "value": {"percentage": seek_percent},
            },
        )
    playing_since = time.monotonic()
    skipped = stopped = False
    idle_since = None
    while time.monotonic() - began < limit:
        players = rpc("Player.GetActivePlayers")
        if not players:
            # Between two items of a queue Kodi has no player for a moment;
            # only a second of none is the end.
            if idle_since is None:
                idle_since = time.monotonic()
            if time.monotonic() - idle_since >= 1.5:
                break
            time.sleep(0.25)
            continue
        idle_since = None
        elapsed = time.monotonic() - playing_since
        if next_after is not None and not skipped and elapsed >= next_after:
            rpc("Player.GoTo", {"playerid": players[0]["playerid"], "to": "next"})
            skipped = True
            # The next item's own clock: wait for it to be playing.
            for _ in range(40):
                time.sleep(0.25)
                active = rpc("Player.GetActivePlayers")
                if active:
                    clock = rpc(
                        "Player.GetProperties",
                        {"playerid": active[0]["playerid"], "properties": ["time"]},
                    )["time"]
                    if clock["seconds"] < next_after and (
                        clock["seconds"] or clock["milliseconds"]
                    ):
                        break
            playing_since = time.monotonic()
            continue
        if (
            stop_after is not None
            and not stopped
            and (next_after is None or skipped)
            and elapsed >= stop_after
        ):
            rpc("Player.Stop", {"playerid": players[0]["playerid"]})
            stopped = True
        time.sleep(0.5)
    return {
        "started_s": started,
        "total_s": round(time.monotonic() - began, 1),
        "stopped": stopped,
        "skipped": skipped,
    }


def bounce(edits=()):
    """Disable kofin, wait for its service script to have exited (not just
    begun stopping), apply library selection edits to the settings file, and
    enable it again. A settings file edited under a running service is not
    read, and an enable before the old script has gone starts nothing."""
    start = log_lines()
    rpc("Addons.SetAddonEnabled", {"addonid": "plugin.video.kofin", "enabled": False})
    gone = wait_log("service.py)", 90, start)
    selected = None
    if edits:
        path = os.path.join(
            _profile_dir(), "addon_data", "plugin.video.kofin", "settings.xml"
        )
        tree = ET.parse(path)
        node = tree.getroot().find("./setting[@id='librarySelection']")
        ids = [i for i in (node.text or "").split(",") if i]
        for edit in edits:
            if edit[0] == "+" and edit[1:] not in ids:
                ids.append(edit[1:])
            if edit[0] == "-" and edit[1:] in ids:
                ids.remove(edit[1:])
        node.text = ",".join(ids)
        tree.write(path, encoding="utf-8", xml_declaration=True)
        selected = len(ids)
    mark = log_lines()
    rpc("Addons.SetAddonEnabled", {"addonid": "plugin.video.kofin", "enabled": True})
    started = wait_log("--->>> kofin service", 90, mark)
    return {
        "old_service_exited": bool(gone),
        "selected": selected,
        "started": (started or "")[-60:],
        "log_line": mark,
    }


def counts():
    result = {}
    for name, method in (
        ("movies", "VideoLibrary.GetMovies"),
        ("tvshows", "VideoLibrary.GetTVShows"),
        ("episodes", "VideoLibrary.GetEpisodes"),
        ("musicvideos", "VideoLibrary.GetMusicVideos"),
        ("songs", "AudioLibrary.GetSongs"),
        ("albums", "AudioLibrary.GetAlbums"),
    ):
        result[name] = rpc(method, {"limits": {"start": 0, "end": 1}})["limits"][
            "total"
        ]
    return result


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    command = argv[1]
    if command == "rpc":
        result = rpc(argv[2], json.loads(argv[3]) if len(argv) > 3 else None)
    elif command == "listen":
        result = listen(float(argv[2]))
    elif command == "refresh-time":
        episodes = None
        if len(argv) > 4:
            episodes = argv[4] == "episodes"
        result = refresh_time(argv[2], int(argv[3]), episodes)
    elif command == "streams":
        result = streams(argv[2], int(argv[3]) if len(argv) > 3 else 20)
    elif command == "coverage":
        result = coverage(argv[2])
    elif command == "kofin":
        result = kofin(argv[2], json.loads(argv[3]) if len(argv) > 3 else None)
    elif command == "counts":
        result = counts()
    elif command == "bounce":
        result = bounce(argv[2:])
    elif command == "log-lines":
        result = log_lines()
    elif command == "watch-total":
        result = watch_total(argv[2], float(argv[3]))
    elif command == "bar":
        result = bar(int(argv[2]))
    elif command == "widgets":
        result = widgets(int(argv[2]))
    elif command == "log-since":
        result = log_since(int(argv[2]), argv[3])
    elif command == "wait-idle":
        result = wait_idle(float(argv[2]) if len(argv) > 2 else 300.0)
    elif command == "play":
        result = play(
            json.loads(argv[2]),
            float(argv[3]) if len(argv) > 3 and argv[3] != "-" else None,
            float(argv[4]) if len(argv) > 4 and argv[4] != "-" else None,
            float(argv[5]) if len(argv) > 5 else None,
        )
    elif command == "wait-log":
        result = wait_log(
            argv[2],
            float(argv[3]) if len(argv) > 3 else 120.0,
            int(argv[4]) if len(argv) > 4 else 0,
        )
    else:
        raise SystemExit(__doc__)
    json.dump(result, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(sys.argv)
