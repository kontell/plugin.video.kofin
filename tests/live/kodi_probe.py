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

    # Rows that share the title and the item (a movie's version rows) were
    # there before the refresh and are not the row coming back.
    others = {row[id_param] for row in find()} - {kodi_id}
    began = time.monotonic()
    rpc(refresh, params)
    issued = time.monotonic() - began
    gone = None
    while time.monotonic() - began < timeout:
        time.sleep(0.1)
        match = [row for row in find() if row[id_param] not in others]
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


def _profile_dir():
    """The current profile's directory, from profiles.xml: a profile's
    directory need not be its name, and the master profile's name is in
    whatever language Kodi first started in."""
    base = _userdata()
    current = rpc("Profiles.GetCurrentProfile")["label"]
    listing = os.path.join(base, "profiles.xml")
    if os.path.isfile(listing):
        for node in ET.parse(listing).getroot().iter("profile"):
            if node.findtext("name") == current:
                directory = node.findtext("directory") or ""
                if directory.startswith("special://masterprofile/"):
                    directory = directory[len("special://masterprofile/") :]
                return os.path.join(base, directory) if directory.strip("/") else base
    return base


def kofin(message, data=None):
    """Send a kofin IPC command with the running service's nonce."""
    with open(
        os.path.join(_profile_dir(), "addon_data", "plugin.video.kofin", "ipc.nonce"),
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
