#!/usr/bin/env python3
"""Live check for SyncPlay fine sync (docs/syncplay-fine-sync.md §6).

Two Kodi members, one group, the timecode episode. The driver speaks to the
Jellyfin server *as each member's own session* — kofin's Client name, the
member's DeviceId and token, read off its settings.xml — so it can create the
group, join, set the queue and pause without driving a single menu, and every
command reaches kofin through its own websocket exactly as a real one would.

Positions are sampled over JSON-RPC; residuals are injected by writing a
member's tempo file for a moment (inputstream.tempo applies it, kofin's
scheduler then has something to close); the scheduler's own log lines are read
back from each box.

    tests/live/syncplay_fine_sync.py \\
        --server https://jelly.konell.xyz --item <episode id> \\
        --member A=127.0.0.1:8080,settings=<path>,log=<path>,tempo=<path> \\
        --member B=192.168.1.150:8080,adb=<serial>,settings=<path>,log=<path>,tempo=<path> \\
        preflight join play steady inject-ahead inject-behind inject-seek cut leave

Credentials never leave the two boxes' own settings files; nothing here is
written to the repository.
"""

import argparse
import base64
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

VERSION = "0.21.0"
LOG_PATTERN = re.compile(
    r"syncplay/(pulse|tempo|align|resumed|landed|hold|unpause)"
    r"|play [0-9a-f]{32} via"
)


class Member(object):
    def __init__(self, spec):
        name, _, rest = spec.partition("=")
        fields = dict(part.split("=", 1) for part in rest.split(",")[1:])
        self.name = name
        self.host = rest.split(",")[0]
        self.adb = fields.get("adb")
        # A member on another machine that is not an Android box: P1D's native
        # Kodi and the Piers flatpak are reachable only this way, and without it
        # half the music rig cannot be driven into a group at all.
        self.ssh = fields.get("ssh")
        self.settings_path = os.path.expanduser(fields["settings"])
        self.log_path = os.path.expanduser(fields["log"])
        self.tempo_path = os.path.expanduser(fields["tempo"])
        self.user = fields.get("user", "kodi")
        self.password = fields.get("password", "kodi")
        text = self.read(self.settings_path)
        self.token = re.search(r'id="accessToken"[^>]*>([^<]*)<', text).group(1)
        self.device_id = re.search(r'id="deviceId"[^>]*>([^<]*)<', text).group(1)
        self.device_name = self.rpc(
            "XBMC.GetInfoLabels", {"labels": ["System.FriendlyName"]}
        )["System.FriendlyName"]
        self.log_start = None

    # -- files on the box ------------------------------------------------

    def read(self, path):
        if self.adb:
            return subprocess.run(
                ["adb", "-s", self.adb, "shell", "cat '%s'" % path],
                capture_output=True,
                text=True,
            ).stdout.replace("\r\n", "\n")
        if self.ssh:
            return subprocess.run(
                ["ssh", "-o", "BatchMode=yes", self.ssh, "cat '%s'" % path],
                capture_output=True,
                text=True,
            ).stdout
        with open(path) as handle:
            return handle.read()

    def write(self, path, text):
        if self.ssh:
            # Through stdin, not an argument: a tempo value is small but the
            # same path writes settings, and quoting a payload into a remote
            # shell is how a harness corrupts the box it is measuring.
            subprocess.run(
                ["ssh", "-o", "BatchMode=yes", self.ssh, "cat > '%s'" % path],
                input=text, text=True, check=True,
            )
            return
        if self.adb:
            subprocess.run(
                ["adb", "-s", self.adb, "shell", "printf '%s' > '%s'" % (text, path)],
                check=True,
            )
            return
        tmp = path + ".inject"
        with open(tmp, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)

    def tempo_file(self):
        try:
            return self.read(self.tempo_path).strip()
        except OSError:
            return "<missing>"

    def grep_log(self):
        """Matching log lines since mark_log()."""
        if self.adb:
            out = subprocess.run(
                [
                    "adb",
                    "-s",
                    self.adb,
                    "shell",
                    "grep -E 'syncplay/|kofin.plugin.play' '%s'" % self.log_path,
                ],
                capture_output=True,
                text=True,
            ).stdout.replace("\r\n", "\n")
        elif self.ssh:
            out = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", self.ssh,
                 "grep -E 'syncplay/|kofin.plugin.play' '%s'" % self.log_path],
                capture_output=True,
                text=True,
            ).stdout
        else:
            out = subprocess.run(
                ["grep", "-E", "syncplay/|kofin.plugin.play", self.log_path],
                capture_output=True,
                text=True,
            ).stdout
        lines = []
        for line in out.splitlines():
            if self.log_start and line[:23] < self.log_start:
                continue
            if LOG_PATTERN.search(line):
                lines.append(line)
        return lines

    def mark_log(self):
        self.log_start = time.strftime("%Y-%m-%d %H:%M:%S") + ".000"

    # -- Kodi --------------------------------------------------------------

    def rpc(self, method, params=None):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            payload["params"] = params
        req = urllib.request.Request(
            "http://%s/jsonrpc" % self.host,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Basic "
                + base64.b64encode(
                    ("%s:%s" % (self.user, self.password)).encode()
                ).decode(),
            },
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            answer = json.loads(response.read().decode())
        if "error" in answer:
            raise RuntimeError("%s: %s" % (method, answer["error"]))
        return answer["result"]

    def active_player(self):
        players = self.rpc("Player.GetActivePlayers")
        return players[0]["playerid"] if players else None

    def position(self):
        """(host_s, position_ms, uncertainty_ms) or None when idle."""
        player = self.active_player()
        if player is None:
            return None
        t0 = time.time()
        props = self.rpc(
            "Player.GetProperties",
            {"playerid": player, "properties": ["time", "speed"]},
        )
        t1 = time.time()
        t = props["time"]
        pos = ((t["hours"] * 60 + t["minutes"]) * 60 + t["seconds"]) * 1000 + t[
            "milliseconds"
        ]
        return ((t0 + t1) / 2.0, float(pos), (t1 - t0) * 500.0, props["speed"])

    def kodi_setting(self, name):
        try:
            return self.rpc("Settings.GetSettingValue", {"setting": name})["value"]
        except RuntimeError:
            return None

    def window_property(self, name):
        return self.rpc(
            "XBMC.GetInfoLabels", {"labels": ["Window(Home).Property(%s)" % name]}
        )["Window(Home).Property(%s)" % name]

    def stop_player(self):
        player = self.active_player()
        if player is not None:
            self.rpc("Player.Stop", {"playerid": player})

    # -- Jellyfin, as this member's session --------------------------------

    def jellyfin(self, server, method, path, body=None):
        header = (
            'MediaBrowser Client="Kofin", Device="%s", DeviceId="%s", Version="%s", Token="%s"'
            % (
                self.device_name.replace('"', "'"),
                self.device_id,
                VERSION,
                self.token,
            )
        )
        req = urllib.request.Request(
            server + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": header, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as error:
            raise RuntimeError(  # noqa: B904 - the HTTPError body is the message
                "%s %s -> %s %s" % (method, path, error.code, error.read()[:200])
            )
        return json.loads(raw) if raw else None


# ----------------------------------------------------------------------------


def log(message):
    print("%s  %s" % (time.strftime("%H:%M:%S"), message), flush=True)


def show_logs(members, label):
    for member in members:
        lines = member.grep_log()
        log("--- %s log (%s): %d lines" % (member.name, label, len(lines)))
        for line in lines[-14:]:
            print("    " + line[11:])


def sample(a, b, seconds, hz=4.0):
    """Pairwise divergence a−b (ms, positive = A ahead) over ``seconds``."""
    rows = []
    end = time.time() + seconds
    while time.time() < end:
        pa = a.position()
        pb = b.position()
        if pa and pb:
            # Playback runs at ~1x between the two reads, so the read-time gap
            # is removed from the difference.
            div = (pa[1] - pb[1]) - (pa[0] - pb[0]) * 1000.0
            rows.append((time.time(), pa[1], pb[1], div, pa[2] + pb[2]))
        time.sleep(max(0.0, 1.0 / hz))
    return rows


def sample_all(a, followers, seconds, hz=4.0):
    """Rows of (host_s, posA, {name: divergence A−follower ms}) over ``seconds``."""
    rows = []
    end = time.time() + seconds
    while time.time() < end:
        pa = a.position()
        divs = {}
        for member in followers:
            pb = member.position()
            if pa and pb:
                divs[member.name] = (pa[1] - pb[1]) - (pa[0] - pb[0]) * 1000.0
        if pa and divs:
            rows.append((time.time(), pa[1], divs))
        time.sleep(max(0.0, 1.0 / hz))
    return rows


def describe_all(rows, followers, label):
    out = {}
    for member in followers:
        series = [
            (r[0], r[1], 0.0, r[2][member.name], 0.0)
            for r in rows
            if member.name in r[2]
        ]
        out[member.name] = describe(series, "%s A−%s" % (label, member.name))
    return out


def describe(rows, label):
    if not rows:
        log("%s: no samples" % label)
        return None
    divs = [r[3] for r in rows]
    divs_sorted = sorted(abs(d) for d in divs)
    p95 = divs_sorted[int(0.95 * (len(divs_sorted) - 1))]
    first = statistics.median(divs[:4])
    last = statistics.median(divs[-4:])
    log(
        "%s: A−B first %+.0f ms, last %+.0f ms, median %+.0f, p95 |Δ| %.0f, max |Δ| %.0f, n=%d"
        % (label, first, last, statistics.median(divs), p95, divs_sorted[-1], len(rows))
    )
    return {
        "first": first,
        "last": last,
        "median": statistics.median(divs),
        "p95": p95,
        "max": divs_sorted[-1],
    }


def slope_ppm(rows, column):
    """Rate error of one member against the host clock, in ppm."""
    if len(rows) < 8:
        return None
    xs = [r[0] for r in rows]
    ys = [r[column] for r in rows]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return (sxy / sxx / 1000.0 - 1.0) * 1e6


def inject(member, rate, seconds):
    """Hold ``rate`` on a member's tempo file for ``seconds``: a displacement
    of (rate − 1) × seconds, the way the shakedown's R-B step was meant."""
    # Never on top of a running pulse: the two writes would fight over the file.
    for _ in range(40):
        if member.tempo_file().startswith("1.0"):
            break
        time.sleep(0.5)
    log(
        "inject on %s: %.2fx for %.2fs (≈ %+.0f ms)"
        % (member.name, rate, seconds, (rate - 1) * seconds * 1000)
    )
    member.write(member.tempo_path, "%.4f\n" % rate)
    time.sleep(seconds)
    member.write(member.tempo_path, "1.0000\n")


def check_moving(members):
    """Is every member's clock advancing? A frozen player answers RPC fine."""
    first = {m.name: m.position() for m in members}
    time.sleep(2.0)
    for member in members:
        before = first[member.name]
        after = member.position()
        if not before or not after:
            log("%s: NO PLAYER" % member.name)
        elif after[1] > before[1] + 500:
            log(
                "%s: clock advancing (%.1fs, speed %s)"
                % (member.name, after[1] / 1000.0, after[3])
            )
        else:
            log(
                "%s: FROZEN at %.1fs (speed %s)"
                % (member.name, after[1] / 1000.0, after[3])
            )


def wait_playing(members, timeout=60):
    deadline = time.time() + timeout
    seen = {}
    while time.time() < deadline:
        ok = True
        for member in members:
            pos = member.position()
            if not pos:
                ok = False
                continue
            last = seen.get(member.name)
            seen[member.name] = pos[1]
            if last is None or pos[1] <= last + 50:
                ok = False
        if ok:
            return True
        time.sleep(0.5)
    return False


# ----------------------------------------------------------------------------


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--item", required=True)
    ap.add_argument("--member", action="append", required=True)
    ap.add_argument("--steady", type=float, default=45.0)
    ap.add_argument("--watch", type=float, default=40.0)
    ap.add_argument("--group", default="phase-b")
    ap.add_argument("scenarios", nargs="+")
    args = ap.parse_args(argv)

    members = [Member(spec) for spec in args.member]
    a = members[0]
    followers = members[1:]
    for member in members:
        member.mark_log()
        log(
            "%s: %s, DeviceId %s…, queue %s, display clock %s"
            % (
                member.name,
                member.device_name,
                member.device_id[:8],
                member.kodi_setting("videoplayer.queuetimesize"),
                member.kodi_setting("videoplayer.usedisplayasclock"),
            )
        )
    report = {}
    group_id = None

    for scenario in args.scenarios:
        log("=== %s ===" % scenario)
        if scenario == "preflight":
            for member in members:
                member.stop_player()
                try:
                    member.jellyfin(args.server, "POST", "/SyncPlay/Leave")
                except RuntimeError:
                    pass
            time.sleep(2)
            for member in members:
                log(
                    "%s: tempo file %s, session property %r"
                    % (
                        member.name,
                        member.tempo_file(),
                        member.window_property("kofin.syncplay.tempo"),
                    )
                )

        elif scenario == "join":
            for member in members:
                member.mark_log()
            a.jellyfin(args.server, "POST", "/SyncPlay/New", {"GroupName": args.group})
            time.sleep(2)
            groups = a.jellyfin(args.server, "GET", "/SyncPlay/List")
            group_id = [
                g["GroupId"] for g in groups if g.get("GroupName") == args.group
            ][0]
            log("group %s" % group_id)
            for member in followers:
                member.jellyfin(
                    args.server, "POST", "/SyncPlay/Join", {"GroupId": group_id}
                )
                time.sleep(2)
            time.sleep(6)
            for member in members:
                log(
                    "%s: queue now %s, session property %s, tempo file %s"
                    % (
                        member.name,
                        member.kodi_setting("videoplayer.queuetimesize"),
                        member.window_property("kofin.syncplay.tempo"),
                        member.tempo_file(),
                    )
                )
            show_logs(members, "join")

        elif scenario == "play":
            for member in members:
                member.mark_log()
            a.jellyfin(
                args.server,
                "POST",
                "/SyncPlay/SetNewQueue",
                {
                    "PlayingQueue": [args.item],
                    "PlayingItemPosition": 0,
                    "StartPositionTicks": 0,
                },
            )
            time.sleep(10)
            if not wait_playing(members, timeout=30):
                log("not playing yet; asking for Unpause")
                a.jellyfin(args.server, "POST", "/SyncPlay/Unpause")
                if not wait_playing(members, timeout=30):
                    log("FAIL: playback did not start on both members")
            show_logs(members, "play")
            for member in members:
                codec = member.rpc(
                    "XBMC.GetInfoLabels",
                    {
                        "labels": [
                            "Player.Process(audiodecoder)",
                            "VideoPlayer.AudioCodec",
                        ]
                    },
                )
                log("%s: audio %s" % (member.name, codec))

        elif scenario == "steady":
            for member in members:
                member.mark_log()
            rows = sample_all(a, followers, args.steady)
            report["steady"] = describe_all(rows, followers, "steady")
            show_logs(members, "steady")

        elif scenario in ("inject-ahead", "inject-behind", "inject-seek"):
            for member in members:
                member.mark_log()
            before = describe_all(
                sample_all(a, followers, 5), followers, "%s before" % scenario
            )
            if scenario == "inject-ahead":
                inject(a, 1.2, 0.75)
            elif scenario == "inject-behind":
                inject(a, 0.8, 0.75)
            else:
                inject(a, 1.5, 2.0)
            rows = sample_all(a, followers, args.watch)
            after = describe_all(
                rows[-16:], followers, "%s after %.0fs" % (scenario, args.watch)
            )
            report[scenario] = {"before": before, "after": after}
            show_logs(members, scenario)

        elif scenario == "moving":
            check_moving(members)

        elif scenario == "cut":
            for member in members:
                member.mark_log()
            inject(a, 1.2, 0.75)
            # Wait for A's scheduler to start a pulse, then pause the group
            # from a follower while it runs.
            deadline = time.time() + 25
            started = False
            while time.time() < deadline:
                if any(
                    "syncplay/pulse ] " in line and "x for" in line
                    for line in a.grep_log()
                ):
                    started = True
                    break
                time.sleep(0.5)
            log("pulse started on A: %s" % started)
            followers[0].jellyfin(args.server, "POST", "/SyncPlay/Pause")
            time.sleep(4)
            log("A tempo file after the pause: %s" % a.tempo_file())
            followers[0].jellyfin(args.server, "POST", "/SyncPlay/Unpause")
            time.sleep(6)
            wait_playing(members, timeout=20)
            describe_all(sample_all(a, followers, 15), followers, "cut after resume")
            check_moving(members)
            show_logs(members, "cut")

        elif scenario == "seek":
            for member in members:
                member.mark_log()
            pos = a.position()
            target = int((pos[1] + 60000.0) * 10000) if pos else 600000000
            a.jellyfin(args.server, "POST", "/SyncPlay/Seek", {"PositionTicks": target})
            time.sleep(12)
            wait_playing(members, timeout=20)
            describe_all(sample_all(a, followers, 15), followers, "seek after")
            check_moving(members)
            show_logs(members, "seek")

        elif scenario == "leave":
            for member in members:
                member.mark_log()
            for member in members:
                member.jellyfin(args.server, "POST", "/SyncPlay/Leave")
            time.sleep(4)
            for member in members:
                log(
                    "%s: queue %s, session property %r, tempo file %s"
                    % (
                        member.name,
                        member.kodi_setting("videoplayer.queuetimesize"),
                        member.window_property("kofin.syncplay.tempo"),
                        member.tempo_file(),
                    )
                )
                member.stop_player()
            show_logs(members, "leave")

        else:
            log("unknown scenario %s" % scenario)

    log(
        "report: %s"
        % json.dumps(
            report, default=lambda o: round(o, 1) if isinstance(o, float) else str(o)
        )
    )


if __name__ == "__main__":
    main(sys.argv[1:])
