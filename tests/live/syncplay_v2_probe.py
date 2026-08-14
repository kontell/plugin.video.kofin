"""Live M3 gate: kofin's real v2 client code against a SyncPlay v2 server.

Drives the actual ``SyncPlayManager`` / ``TimeSync`` / ``Api`` classes
(Kodistubs standing in for Kodi; ``play_item``/``schedule`` stubbed so no
media is touched) through a genuine session: join with negotiation, Hello
transport discovery, dedicated-socket time sync, queue + Ready + Unpause,
position beacons, snapshot on demand, and leave.

Run from the repo root inside the dev venv:

    .venv/bin/python tests/live/syncplay_v2_probe.py \
        --base http://127.0.0.1:8096 --user syncbot-a:sp-test
"""

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lib"))

import websocket  # noqa: E402

from kofin.core.api import Api  # noqa: E402
from kofin.core.http import plugin_transport  # noqa: E402
from kofin.syncplay.manager import SyncPlayManager  # noqa: E402

RESULTS = []


def record(ok, name, detail=""):
    RESULTS.append((ok, name, detail))
    print(
        "[%s] %s%s" % ("PASS" if ok else "FAIL", name, " — " + detail if detail else "")
    )


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


class StubPlayer:
    """The slice of the service player the manager touches; never plays."""

    def __init__(self):
        self.syncplay_group_active = False

    def isPlaying(self):
        return False

    def isPlayingAudio(self):
        return False

    def getTime(self):
        return 0.0

    def current_item(self):
        return None

    def pause(self):
        pass

    def stop(self):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8096")
    parser.add_argument("--user", required=True, help="username:password")
    args = parser.parse_args()
    username, _, password = args.user.partition(":")

    http = plugin_transport(verify_ssl=False)
    device_id = "kofin-m3-probe-%d" % int(time.time())
    login = Api(http, args.base, "kofin-probe", device_id, "0.0")
    auth = login.post(
        "/Users/AuthenticateByName", {"Username": username, "Pw": password}
    )
    api = Api(
        http,
        args.base,
        "kofin-probe",
        device_id,
        "0.0",
        token=auth["AccessToken"],
        user_id=auth["User"]["Id"],
    )

    manager = SyncPlayManager(api, StubPlayer())
    manager.enabled = lambda: True  # no Kodi settings store here
    manager.playback.play_item = lambda item, ticks: None  # never touch media
    scheduled = []
    manager.playback.schedule = scheduled.append
    beacons = []

    # The main websocket, standing in for core/ws.py + service/remote.py.
    ws_url = api.websocket_url("/socket") + "?deviceId=" + device_id
    sock = websocket.create_connection(
        ws_url, header={"Authorization": api.authorization()}
    )
    sock.send(json.dumps({"MessageType": "KeepAlive"}))

    def reader():
        while True:
            try:
                message = json.loads(sock.recv())
            except Exception:
                return
            mtype = message.get("MessageType")
            data = message.get("Data")
            if mtype == "SyncPlayGroupUpdate" and isinstance(data, dict):
                if data.get("Type") == "PositionBeacon":
                    beacons.append(data)
                manager.on_notification(mtype, data)
            elif mtype == "SyncPlayCommand" and isinstance(data, dict):
                manager.on_notification(mtype, data)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.5)  # let the session controller attach

    # --- join with negotiation + Hello ---------------------------------
    manager.new_group("kofin-m3-probe")
    record(
        wait_for(manager.in_group),
        "joined",
        "group=%s" % (manager.group or {}).get("GroupId"),
    )
    record(
        manager.protocol_version == 2,
        "negotiated-v2",
        "protocol_version=%s" % manager.protocol_version,
    )
    record(
        manager.state_version >= 1,
        "state-version-seeded",
        "v%s" % manager.state_version,
    )
    # in_group() flips early in _on_group_joined; the Hello runs later on
    # the same dispatcher pass — wait for it rather than race it.
    record(
        wait_for(lambda: manager.timesync_ws_path == "/SyncPlay/TimeSync", timeout=5),
        "hello-transport",
        repr(manager.timesync_ws_path),
    )

    # --- dedicated-socket time sync (the real TimeSync thread) ----------
    # The first greedy measurement may run over HTTP before Hello lands;
    # wait for a cycle that actually used the dedicated socket.
    ok = wait_for(
        lambda: manager.timesync is not None
        and manager.timesync._ws is not None
        and manager.timesync.rtt_ms is not None,
        timeout=10,
    )
    record(
        ok,
        "ws-timesync",
        "rtt=%.1fms offset=%.1fms via dedicated socket"
        % (manager.timesync.rtt_ms or -1, manager.timesync.offset_ms),
    )

    # --- queue -> Ready -> Unpause --------------------------------------
    movies = api.get(
        "/Items",
        {
            "IncludeItemTypes": "Movie",
            "Recursive": "true",
            "Limit": 1,
            "userId": api.user_id,
        },
    )
    movie = (movies.get("Items") or [{}])[0].get("Id")
    manager._api("syncplay_set_new_queue", [movie], 0, 0)
    record(
        wait_for(lambda: manager.current_playlist_item_id is not None),
        "queue-applied",
        "playlist item %s, phase %s"
        % (manager.current_playlist_item_id, manager.phase),
    )

    manager.post_report("syncplay_ready")
    record(
        wait_for(lambda: any(c.get("Command") == "Unpause" for c in scheduled)),
        "unpause-scheduled",
        "commands=%s" % [c.get("Command") for c in scheduled],
    )
    versions = [c.get("StateVersion") for c in scheduled]
    record(
        all(v is not None for v in versions),
        "commands-carry-stateversion",
        str(versions),
    )

    # --- beacons drive the drift reference ------------------------------
    ok = wait_for(lambda: (manager.playback.estimate_position_ms() or 0) > 0, timeout=8)
    record(
        ok and len(beacons) >= 1,
        "beacon-reference",
        "%d beacon(s), estimate=%.0fms"
        % (len(beacons), manager.playback.estimate_position_ms() or -1),
    )

    # --- snapshot on demand ---------------------------------------------
    before = time.time()
    manager.request_resync()
    record(
        wait_for(lambda: manager.last_snapshot_at >= before),
        "snapshot-on-demand",
        "state=%s members=%d" % (manager.group_state, len(manager.members)),
    )

    # --- leave ------------------------------------------------------------
    manager.leave_group()
    record(wait_for(lambda: not manager.in_group()), "left")
    remaining = api.syncplay_list()
    record(remaining == [], "group-cleaned-up", "%d group(s) remain" % len(remaining))

    try:
        sock.close()
    except Exception:
        pass
    manager.stop()

    fails = [r for r in RESULTS if not r[0]]
    print("\n===== %d PASS, %d FAIL =====" % (len(RESULTS) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
