"""Loopback Jellyfin protocol fixture around the real resolver and Player."""

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

import xbmc

from kofin.core import settings, state
from kofin.core.api import Api
from kofin.core.http import Http
from kofin.core.settings import Credentials
from kofin.service.player import Player
from kofin.sync.backends.api.movies import rpc
from kofin.sync.backends.api.library import Library


def exercise(store, backend, item, addon_path):
    fixture = copy.deepcopy(item)
    fixture["RunTimeTicks"] = 120000000
    fixture["UserData"] = {"Played": False, "PlaybackPositionTicks": 0}
    fixture["MediaSources"] = [
        {
            "Id": "phase3-source",
            "Container": "mp4",
            "Protocol": "Http",
            "SupportsDirectPlay": True,
            "SupportsDirectStream": True,
            "SupportsTranscoding": False,
            "RunTimeTicks": 120000000,
            "MediaStreams": [
                {
                    "Index": 0,
                    "Type": "Video",
                    "Codec": "h264",
                    "Width": 320,
                    "Height": 180,
                },
                {
                    "Index": 1,
                    "Type": "Audio",
                    "Codec": "aac",
                    "Channels": 2,
                    "SampleRate": 48000,
                },
            ],
        }
    ]
    fixture["MediaStreams"] = fixture["MediaSources"][0]["MediaStreams"]
    reports = []
    catalogue = [fixture]
    data = (Path(addon_path) / "fixture.mp4").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path.startswith("/Videos/"):
                start = 0
                end = len(data) - 1
                if self.headers.get("Range"):
                    parts = self.headers["Range"].removeprefix("bytes=").split("-")
                    start = int(parts[0] or 0)
                    end = int(parts[1]) if parts[1] else end
                body = data[start : end + 1]
                self.send_response(206 if self.headers.get("Range") else 200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header(
                    "Content-Range", "bytes %d-%d/%d" % (start, end, len(data))
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            elif path == "/UserViews":
                self.reply(
                    {
                        "Items": [
                            {
                                "Id": "fixture",
                                "Name": "Fixture movies",
                                "CollectionType": "movies",
                            }
                        ]
                    }
                )
            elif path == "/Items":
                self.reply({"Items": catalogue, "TotalRecordCount": len(catalogue)})
            elif path.startswith("/Items/"):
                self.reply(fixture)
            elif path.startswith("/MediaSegments/"):
                self.reply({"Items": []})
            elif path.startswith("/Users/"):
                self.reply({"Configuration": {}, "Policy": {}})
            else:
                self.reply({})

        def do_POST(self):
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"
            )
            path = urlsplit(self.path).path
            if path.endswith("/PlaybackInfo"):
                self.reply(
                    {
                        "MediaSources": fixture["MediaSources"],
                        "PlaySessionId": "phase3-session",
                    }
                )
            else:
                reports.append((path, body))
                self.reply({})

        def do_DELETE(self):
            self.reply({})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    player = None
    transport = None
    monitor = xbmc.Monitor()
    try:
        address = "http://127.0.0.1:%d" % server.server_port
        Credentials(
            server_address=address,
            server_name="Phase3",
            server_id="phase3-server",
            user_id="phase3-user",
            token="synthetic-phase3-token",
            device_id="phase3-device",
            is_logged_in=True,
        ).save()
        for key in (
            "syncPlayEnabled",
            "mediaSegmentsEnabled",
            "playNextEnabled",
            "honourJellyfinDefaultTracks",
        ):
            settings.set_bool(key, False)
        store.initialize(address)
        store.publish([fixture])
        backend.reconcile()
        native = backend.read()[fixture["Id"]]["movieid"]
        transport = Http(False)
        api = Api.from_credentials(transport, Credentials.load())
        player = Player(api)

        def wait(predicate, timeout=30):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if predicate():
                    return
                if monitor.waitForAbort(0.1):
                    raise RuntimeError("Kodi stopping")
            raise TimeoutError("playback fixture timed out")

        state.set_online(True)
        browse_result = {}

        def browse_during_scan():
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not xbmc.getCondVisibility(
                    "Library.IsScanningVideo"
                ):
                    time.sleep(0.01)
                started = time.monotonic()
                response = rpc(
                    "Files.GetDirectory",
                    {
                        "directory": "plugin://plugin.video.kofin.phase3/?mode=browse&view=fixture&type=movies&folder=all",
                        "media": "video",
                    },
                )
                browse_result.update(
                    seconds=time.monotonic() - started,
                    rows=len(response.get("files", [])),
                )
            except Exception as error:
                browse_result["error"] = type(error).__name__

        for index in range(1000, 1100):
            row = copy.deepcopy(fixture)
            row.update(Id="%032x" % index, Name="Phase3 load movie %d" % index)
            catalogue.append(row)
        settings.set_str("librarySelection", "fixture")
        coordinator = Library(api, player, lambda: api)
        coordinator.full_sync()
        browser = threading.Thread(target=browse_during_scan, daemon=True)
        browser.start()
        started = time.monotonic()
        coordinator.apply(backend)
        batch_seconds = time.monotonic() - started
        browser.join(timeout=15)
        assert not browser.is_alive() and "error" not in browse_result, browse_result
        assert browse_result["rows"] == 101, browse_result
        assert len(backend.read()) == 101
        # Keep the playable fixture and remove the load-test movies through
        # the coordinator's complete-directory path before playback.
        catalogue[:] = [fixture]
        coordinator.full_sync()
        coordinator.apply(backend)
        native = backend.read()[fixture["Id"]]["movieid"]
        rpc("Player.Open", {"item": {"movieid": native}, "options": {"resume": False}})
        wait(lambda: player.current_item() is not None)
        wait(lambda: player.isPlayingVideo() and player.getTime() > 1)
        current = rpc("Player.GetItem", {"playerid": 1, "properties": ["file"]})["item"]
        assert current.get("id") == native, "native playback identity lost"
        rpc("Player.Stop", {"playerid": 1})
        wait(lambda: any(p.endswith("/Stopped") for p, _ in reports))
        assert any(
            p == "/Sessions/Playing" and b.get("ItemId") == fixture["Id"]
            for p, b in reports
        )
        first_reports = len(reports)
        rpc("Player.Open", {"item": {"movieid": native}, "options": {"resume": False}})
        wait(lambda: player.current_item() is not None)
        wait(
            lambda: any(p.endswith("/Stopped") for p, _ in reports[first_reports:]),
            timeout=35,
        )
        stopped = [body for path, body in reports if path.endswith("/Stopped")]
        assert stopped[-1].get("PositionTicks", 0) >= 100000000
        return {
            "native_playback_id": True,
            "start_stop_reported": True,
            "completion_reported": True,
            "report_count": len(reports),
            "coordinator_import_101_seconds": batch_seconds,
            "dynamic_browse_during_scan": browse_result,
        }
    finally:
        if player is not None and player.isPlayingVideo():
            rpc("Player.Stop", {"playerid": 1})
        monitor.waitForAbort(0.2)
        if player is not None:
            player.stop_threads()
        if transport is not None:
            transport.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        state.clear_all()
