#!/usr/bin/env python3
"""M1 — steady playlist, arm A (docs/syncplay-music-shakedown.md §7).

Ten tracks straight through, four members in one SyncPlay group, no interaction.
The baseline run, and the one that answers what a track boundary costs.

Three channels at once (§6): the JSON-RPC sampler on all four, the controller's
own log on each box, and — on the two P1D instances only — the sample-accurate
audio capture that is the only channel able to resolve §10's 30 ms bar.
"""

import json
import os
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from syncplay_music import MusicMember, sample, qualified_delta, straddle, \
    boundaries, hole_rate, describe, HOLE                              # noqa: E402
from syncplay_fine_sync import log                                     # noqa: E402

SERVER = "https://jelly.konell.xyz"
GROUP = "music-m1"
KP = "/sdcard/Android/data/org.xbmc.kodi/files/.kodi"
NATIVE = "/home/conor/.kodi"
# P1D runs profile "kofin-test", and a non-master profile keeps its own
# addon_data. Reading the master settings gave the harness a deviceId and
# server that belong to no running kofin, so the group was created as a
# session nobody was listening on and P1D silently never joined.
NATIVE_PROFILE = "/home/conor/.kodi/userdata/profiles/kofin-test"
FLATPAK = "/home/conor/.var/app/tv.kodi.Kodi/data"
SPECS = [
    "P1D=192.168.1.112:8080,ssh=conor@p1d,settings=%s/addon_data/plugin.video.kofin/settings.xml,log=%s/temp/kodi.log,tempo=%s/temp/kofin_syncplay_tempo" % (NATIVE_PROFILE, NATIVE, NATIVE),
    "PIERS=192.168.1.112:8081,ssh=conor@p1d,settings=%s/userdata/addon_data/plugin.video.kofin/settings.xml,log=%s/temp/kodi.log,tempo=%s/temp/kofin_syncplay_tempo" % (FLATPAK, FLATPAK, FLATPAK),
    "BRAVIA=192.168.1.198:8080,adb=192.168.1.198:34793,settings=%s/userdata/addon_data/plugin.video.kofin/settings.xml,log=%s/temp/kodi.log,tempo=%s/temp/kofin_syncplay_tempo" % (KP, KP, KP),
    "TAB=192.168.1.150:8080,adb=192.168.1.150:35177,settings=%s/userdata/addon_data/plugin.video.kofin/settings.xml,log=%s/temp/kodi.log,tempo=%s/temp/kofin_syncplay_tempo" % (KP, KP, KP),
]
# Arm label: "" for arm A (streamed), "-D" for arm D (downloaded, repointed).
ARM = sys.argv[1] if len(sys.argv) > 1 else ""
OUT = "/media/bluecon/dev/plugin.video.kofin/tests/live/results/music-A/M1" + ARM
CAP = ("/media/minipie/bluecon/dev/plugin.video.kofin/tests/live/results/music-A/M1"
       + ARM + "/capture")


def _blocking_dialogs(members):
    """Report members sitting on a modal — never dismiss one.

    Dismissing kofin's "Playback stopped" prompt is *not* neutral: a cancelled
    selection falls into the spectator branch (`manager.py:1394`, deliberately, so
    a stopped member never leaves the group blocked). The first version of this
    helper sent Input.Back and silently demoted the Bravia to spectator for a
    whole run, which was then measured as a dropout. A dialog here means a prior
    run left a member stopped inside a group: a rig fault to fix, not to click
    away.
    """
    stuck = []
    for m in members:
        try:
            if m.rpc("XBMC.GetInfoBooleans",
                     {"booleans": ["Window.IsActive(selectdialog)"]}
                     )["Window.IsActive(selectdialog)"]:
                stuck.append(m.name)
        except Exception:
            pass
    return stuck


def _all_playing(members, tries=8):
    """Every member must be visibly playing *to the server* before sampling.

    Checked against /Sessions rather than each box's own JSON-RPC: a member can
    report a player while the server sees a dormant session, which is exactly
    how a wrong deviceId cost a 30-minute run. Cheap here, expensive later.
    """
    import urllib.request
    key = open(os.path.expanduser("~/.config/kodi-drive/targets.env")).read()
    key = [l.split("=", 1)[1] for l in key.splitlines()
           if l.startswith("JELLYFIN_API_KEY=")][0]
    want = {m.device_id: m.name for m in members}
    for _ in range(tries):
        with urllib.request.urlopen(
                "http://localhost:8096/Sessions?api_key=%s" % key, timeout=15) as r:
            sessions = json.load(r)
        playing = {want[s["DeviceId"]] for s in sessions
                   if s.get("DeviceId") in want and s.get("NowPlayingItem")}
        if len(playing) == len(members):
            log("all %d members playing per the server" % len(members))
            return True
        time.sleep(5)
    log("only %s playing per the server; missing %s"
        % (sorted(playing), sorted(set(want.values()) - playing)))
    return False


def _teardown(members, lead, cap):
    """Leave the group on every member; cap may be None if none was started."""
    for m in members:
        try:
            m.jellyfin(SERVER, "POST", "/SyncPlay/Leave")
        except Exception:
            pass
    if cap is None:
        return
    try:
        cap.wait(timeout=60)
    except Exception:
        cap.terminate()


def main():
    ids = json.load(open("/tmp/claude-1000/m1_ids.json"))
    os.makedirs(OUT, exist_ok=True)
    members = [MusicMember(s) for s in SPECS]
    lead = members[0]
    log("members: %s" % ", ".join(m.name for m in members))

    for m in members:
        try:
            m.jellyfin(SERVER, "POST", "/SyncPlay/Leave")
        except Exception:
            pass
        m.stop_player()
        m.mark_log()
    time.sleep(3)

    group_name = GROUP + ARM
    lead.jellyfin(SERVER, "POST", "/SyncPlay/New", {"GroupName": group_name})
    time.sleep(2)
    groups = lead.jellyfin(SERVER, "GET", "/SyncPlay/List")
    gid = [g["GroupId"] for g in groups if g.get("GroupName") == group_name][0]
    log("group %s" % gid)
    for m in members[1:]:
        m.jellyfin(SERVER, "POST", "/SyncPlay/Join", {"GroupId": gid})
        time.sleep(2)
    time.sleep(5)
    stuck = _blocking_dialogs(members)
    if stuck:
        log("ABORT: %s sitting on a modal dialog. Clear it by hand — dismissing "
            "kofin's stopped-prompt makes that member a spectator." % ", ".join(stuck))
        _teardown(members, lead, None)
        return 1

    # The capture owns the P1D pair's audio routing for the whole run.
    cap = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "conor@p1d",
         "python3 /media/minipie/bluecon/dev/plugin.video.kofin/tools/music_capture.py "
         "--a 127.0.0.1:8080 --b 127.0.0.1:8081 --albumid 1593 --record-only "
         "--secs 1900 --out %s" % CAP],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(12)

    log("queueing all %d tracks" % len(ids))
    lead.jellyfin(SERVER, "POST", "/SyncPlay/SetNewQueue",
                  {"PlayingQueue": ids, "PlayingItemPosition": 0,
                   "StartPositionTicks": 0})
    time.sleep(12)
    if not any(m.snapshot() is not HOLE for m in members):
        log("nothing playing; asking for Unpause")
        lead.jellyfin(SERVER, "POST", "/SyncPlay/Unpause")
        time.sleep(8)

    if not _all_playing(members):
        log("ABORT: not every member is playing — see above")
        _teardown(members, lead, cap)
        return 1

    log("sampling the album (~30 min)")
    try:
        rows = sample(members, 1830.0, hz=4.0)
    finally:
        try:
            lead.jellyfin(SERVER, "POST", "/SyncPlay/Stop")
        except Exception:
            pass
        _teardown(members, lead, cap)
    log("%d sample rows" % len(rows))

    names = [m.name for m in members]
    report = {"holes": hole_rate(members), "deltas": {}, "boundaries": {}}
    for m in members[1:]:
        report["deltas"][m.name] = describe(
            qualified_delta(rows, lead.name, m.name), "Δ %s−%s" % (lead.name, m.name))
    spans = straddle(rows, names)
    report["straddle"] = {"count": len(spans),
                          "median_s": statistics.median([s[2] for s in spans]) if spans else None,
                          "max_s": max((s[2] for s in spans), default=None)}
    log("straddle: %s" % report["straddle"])
    for m in members:
        marks = boundaries(rows, m.name)
        report["boundaries"][m.name] = [round(b[0], 2) for b in marks]
        log("  %s: %d boundaries" % (m.name, len(marks)))
    for m in members:
        with open(os.path.join(OUT, "log-%s.txt" % m.name), "w") as fh:
            fh.write("\n".join(m.grep_log()))
    with open(os.path.join(OUT, "rows.json"), "w") as fh:
        json.dump([(t, {k: (v if v is not HOLE else None) for k, v in r.items()})
                   for t, r in rows], fh)
    with open(os.path.join(OUT, "summary.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    log("written to %s" % OUT)
    print(cap.stderr.read()[-600:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
