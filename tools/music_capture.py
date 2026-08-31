#!/usr/bin/env python3
"""Sample-accurate per-instance audio capture (docs/syncplay-music-shakedown.md §6.1).

Runs **on the host that has both Kodi instances**. Gives each its own PipeWire
null sink, plays the asset on both, records each sink's monitor, and puts every
setting back.

Routing is done by setting each instance's ``audiooutput.audiodevice`` to its own
null sink, which Kodi enumerates as an ordinary device
(``PIPEWIRE:kodi_a|kodi_a Audio/Sink sink``). An earlier version moved the
PulseAudio streams instead, on the theory that moving is less invasive than
writing a setting. On this stack that was wrong twice over, and both failures are
why this file exists:

* **The streams cannot be told apart.** PipeWire's Pulse compatibility reports
  every Kodi stream as application name "Kodi" with no PID, the client index a
  sink-input names does not resolve, an idle Kodi keeps its stream open and
  uncorked, and a stop/open cycle renames the index. Appearance order, cork state
  and PID all fail; measured on P1D 2026-08-31, the index that sorted first was
  an idle stream and the sink it was moved to stayed silent.
* **Getting it wrong wedges Kodi.** Unloading a null sink that still has a stream
  attached destroys that stream's sink underneath Kodi, and its AudioEngine does
  not recover: the player reports a position that never advances and no audio
  reaches any device. It took a restart of both instances to clear.

A setting names its target unambiguously, is read back to confirm, and is
restored in a finally block before any module is unloaded.

    tools/music_capture.py --a 127.0.0.1:8080 --b 127.0.0.1:8081 \\
        --albumid 1593 --secs 40 --out /tmp/cap
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

SINKS = ("kodi_a", "kodi_b")


def rpc(host, method, params=None):
    req = urllib.request.Request(
        "http://%s/jsonrpc" % host,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                         "params": params or {}}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(b"kodi:kodi").decode()})
    with urllib.request.urlopen(req, timeout=15) as response:
        answer = json.loads(response.read().decode())
    if "error" in answer:
        raise RuntimeError("%s: %s" % (method, answer["error"]))
    return answer.get("result")


def pactl(*args):
    return subprocess.run(["pactl"] + list(args), check=True,
                          capture_output=True, text=True).stdout.strip()


def device_string(host, sink):
    """The device value Kodi itself offers for this sink, never one we invent."""
    for setting in rpc(host, "Settings.GetSettings", {"level": "expert"})["settings"]:
        if setting.get("id") != "audiooutput.audiodevice":
            continue
        for option in setting.get("options") or []:
            if option.get("value", "").startswith("PIPEWIRE:%s|" % sink):
                return option["value"]
    raise RuntimeError("%s does not offer sink %s" % (host, sink))


def stop(host):
    for player in rpc(host, "Player.GetActivePlayers") or []:
        rpc(host, "Player.Stop", {"playerid": player["playerid"]})


def play_first_track(host, albumid):
    songs = rpc(host, "AudioLibrary.GetSongs",
                {"filter": {"albumid": albumid}, "properties": ["track"],
                 "sort": {"method": "track"}})["songs"]
    rpc(host, "Player.Open", {"item": {"songid": songs[0]["songid"]}})


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", required=True, help="host:port of instance A")
    ap.add_argument("--b", required=True, help="host:port of instance B")
    ap.add_argument("--albumid", type=int, required=True)
    ap.add_argument("--secs", type=float, default=40.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--record-only", action="store_true",
                    help="record the two monitors and NOTHING else: no module "
                         "lifecycle, no device change, no playback. Once routing "
                         "is set for a session this is the only safe mode -- "
                         "every wedge of P1D's AudioEngine came from "
                         "reconfiguring audio under a running Kodi.")
    ap.add_argument("--no-play", action="store_true",
                    help="route and record only; playback is driven elsewhere "
                         "(a SyncPlay group owns the queue in every scenario "
                         "after M0, so the tool must not open anything itself)")
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    hosts = (args.a, args.b)
    modules, saved, recorders = [], {}, []

    if args.record_only:
        recs = []
        for sink, name in zip(SINKS, ("a", "b")):
            path = os.path.join(args.out, "%s.raw" % name)
            handle = open(path, "wb")
            recs.append((subprocess.Popen(
                ["parec", "--device=%s.monitor" % sink, "--format=s16le",
                 "--rate=%d" % args.rate, "--channels=2"], stdout=handle), handle, path))
        print("  recording %.0f s (record-only)" % args.secs)
        time.sleep(3)
        for _, _, path in recs:
            if os.path.getsize(path) == 0:
                print("  WARNING: %s empty after 3 s -- is that sink routed?" % path)
        try:
            time.sleep(max(0.0, args.secs - 3))
        finally:
            for proc, handle, path in recs:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                handle.close()
                print("  %s: %d bytes (%.2f s)" % (path, os.path.getsize(path),
                      os.path.getsize(path) / 4.0 / args.rate))
        return 0

    try:
        for sink in SINKS:
            modules.append(pactl("load-module", "module-null-sink",
                                 "sink_name=%s" % sink))
            print("  null sink %s (module %s)" % (sink, modules[-1]))

        for host, sink in zip(hosts, SINKS):
            if not args.no_play:
                stop(host)
            saved[host] = rpc(host, "Settings.GetSettingValue",
                              {"setting": "audiooutput.audiodevice"})["value"]
            target = device_string(host, sink)
            rpc(host, "Settings.SetSettingValue",
                {"setting": "audiooutput.audiodevice", "value": target})
            back = rpc(host, "Settings.GetSettingValue",
                       {"setting": "audiooutput.audiodevice"})["value"]
            if back != target:
                raise RuntimeError("%s did not take the device: %r" % (host, back))
            print("  %s -> %s" % (host, sink))
        time.sleep(3)

        if args.no_play:
            print("  waiting for playback from elsewhere")
            for _ in range(60):
                if all(rpc(h, "Player.GetActivePlayers") for h in hosts):
                    break
                time.sleep(1)
        else:
            for host in hosts:
                play_first_track(host, args.albumid)
        time.sleep(4)

        for sink, name in zip(SINKS, ("a", "b")):
            path = os.path.join(args.out, "%s.raw" % name)
            handle = open(path, "wb")
            recorders.append((subprocess.Popen(
                ["parec", "--device=%s.monitor" % sink, "--format=s16le",
                 "--rate=%d" % args.rate, "--channels=2"], stdout=handle), handle, path))
        print("  recording %.0f s" % args.secs)

        time.sleep(3)
        for _, _, path in recorders:
            if os.path.getsize(path) == 0:
                raise RuntimeError("%s empty after 3 s: nothing reached that sink" % path)
        print("  both sinks receiving audio")
        time.sleep(max(0.0, args.secs - 3))
        return 0
    finally:
        for proc, handle, path in recorders:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            handle.close()
            print("  %s: %d bytes (%.2f s)"
                  % (path, os.path.getsize(path), os.path.getsize(path) / 4.0 / args.rate))
        # Settings back BEFORE any module goes, so no stream is ever left on a
        # sink that is about to disappear.
        for host, value in saved.items():
            try:
                if not args.no_play:
                    stop(host)
                rpc(host, "Settings.SetSettingValue",
                    {"setting": "audiooutput.audiodevice", "value": value})
                print("  restored %s -> %r" % (host, value))
            except Exception as error:
                print("  WARNING: could not restore %s: %s" % (host, error))
        time.sleep(2)
        for module in modules:
            try:
                pactl("unload-module", module)
                print("  unloaded module %s" % module)
            except Exception as error:
                print("  WARNING: module %s still loaded: %s" % (module, error))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
