#!/usr/bin/env python3
"""Create and exercise an isolated Jellyfin 12.1 fixture, then stop it.

Requires the official portable Jellyfin binary and ffmpeg. Never connects to
configured Kofin servers. The work directory must not exist; all media is
generated there. Credentials remain in that directory (mode 0700).
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "12.1.0"
NAME = "Kofin OR phase0 disposable fixture"


class Fixture:
    def __init__(self, binary, root, port, ffmpeg):
        self.binary, self.root, self.port, self.ffmpeg = binary, root, port, ffmpeg
        self.process = None
        self.log = None
        self.token = ""
        self.user = ""
        self.auth = 'MediaBrowser Client="kofin-phase0", Device="fixture", DeviceId="phase0", Version="0.90.0"'
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        (root / ".phase0-owner").write_text(NAME)
        (root / "config").mkdir()
        (root / "config/network.xml").write_text(
            "<NetworkConfiguration><InternalHttpPort>%d</InternalHttpPort>"
            "<PublicHttpPort>%d</PublicHttpPort><EnableHttps>false</EnableHttps>"
            "<LocalNetworkAddresses><string>127.0.0.1</string></LocalNetworkAddresses>"
            "<EnableIPv4>true</EnableIPv4><EnableIPv6>false</EnableIPv6>"
            "<EnableRemoteAccess>false</EnableRemoteAccess>"
            "<AutoDiscovery>false</AutoDiscovery>"
            "<EnableUPnP>false</EnableUPnP></NetworkConfiguration>" % (port, port)
        )
        self.credentials = {"Username": "phase0-admin", "Pw": secrets.token_urlsafe(24)}
        credentials_path = root / "credentials.json"
        credentials_path.write_text(json.dumps(self.credentials))
        credentials_path.chmod(0o600)

    def call(self, method, route, body=None, params=None):
        url = "http://127.0.0.1:%d%s" % (self.port, route)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        auth = self.auth + (', Token="%s"' % self.token if self.token else "")
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": auth, "Content-Type": "application/json"},
            method=method,
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def start(self):
        with socket.socket() as guard:
            guard.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            guard.bind(("127.0.0.1", self.port))
        self.log = (self.root / "console.log").open("ab")
        self.process = subprocess.Popen(
            [
                str(self.binary),
                "--nowebclient",
                "--datadir",
                str(self.root / "data"),
                "--configdir",
                str(self.root / "config"),
                "--cachedir",
                str(self.root / "cache"),
                "--logdir",
                str(self.root / "logs"),
                "--ffmpeg",
                self.ffmpeg,
            ],
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        for _ in range(90):
            if self.process.poll() is not None:
                raise RuntimeError("fixture server exited; inspect private console.log")
            try:
                info = self.call("GET", "/System/Info/Public")
                if "Version" not in info:
                    time.sleep(1)
                    continue
                if info["Version"] != VERSION:
                    raise RuntimeError("fixture requires Jellyfin " + VERSION)
                return info
            except (urllib.error.URLError, TimeoutError):
                time.sleep(1)
        raise RuntimeError("fixture server did not start")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log:
            self.log.close()

    def login(self):
        self.token = ""
        reply = self.call("POST", "/Users/AuthenticateByName", self.credentials)
        self.token, self.user = reply["AccessToken"], reply["User"]["Id"]

    def bootstrap(self):
        info = self.start()
        if info["StartupWizardCompleted"]:
            raise RuntimeError("refusing to initialize an existing server")
        self.call(
            "POST",
            "/Startup/Configuration",
            {
                "ServerName": NAME,
                "UICulture": "en-US",
                "MetadataCountryCode": "US",
                "PreferredMetadataLanguage": "en",
            },
        )
        self.call("GET", "/Startup/User")  # initializes the first administrator
        self.call(
            "POST",
            "/Startup/User",
            {
                "Name": self.credentials["Username"],
                "Password": self.credentials["Pw"],
            },
        )
        self.call(
            "POST",
            "/Startup/RemoteAccess",
            {"EnableRemoteAccess": False, "EnableAutomaticPortMapping": False},
        )
        self.call("POST", "/Startup/Complete")
        self.login()

    def make_media(self):
        seed = self.root / "seed.mkv"
        subprocess.run(
            [
                self.ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:r=1",
                "-t",
                "60",
                "-c:v",
                "mpeg4",
                str(seed),
            ],
            check=True,
        )
        self.movie_paths = []
        for index in range(2):
            path = (
                self.root
                / "media/movies"
                / ("Phase0 Movie %d (2020)" % index)
                / "movie.mkv"
            )
            path.parent.mkdir(parents=True)
            shutil.copyfile(seed, path)
            self.movie_paths.append(path)
        episode = self.root / "media/tv/Phase0 Show/Season 01/Phase0 Show S01E01.mkv"
        episode.parent.mkdir(parents=True)
        shutil.copyfile(seed, episode)
        album = self.root / "media/music/Phase0 Artist/Phase0 Album"
        album.mkdir(parents=True)
        for index in range(2):
            subprocess.run(
                [
                    self.ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=4",
                    "-metadata",
                    "artist=Phase0 Artist",
                    "-metadata",
                    "album=Phase0 Album",
                    "-metadata",
                    "title=Phase0 Song %d" % index,
                    "-metadata",
                    "track=%d" % (index + 1),
                    str(album / ("%02d.flac" % index)),
                ],
                check=True,
            )
        types = (
            "Movie",
            "Series",
            "Season",
            "Episode",
            "MusicArtist",
            "MusicAlbum",
            "Audio",
        )
        for directory, kind in (
            ("movies", "movies"),
            ("tv", "tvshows"),
            ("music", "music"),
        ):
            self.call(
                "POST",
                "/Library/VirtualFolders",
                {
                    "LibraryOptions": {
                        "PathInfos": [{"Path": str(self.root / "media" / directory)}],
                        "EnableRealtimeMonitor": False,
                        "EnableInternetProviders": False,
                        "EnableLUFSScan": False,
                        "EnableChapterImageExtraction": False,
                        "TypeOptions": [
                            {"Type": t, "MetadataFetchers": [], "ImageFetchers": []}
                            for t in types
                        ],
                    }
                },
                {
                    "name": "Phase0 " + directory,
                    "collectionType": kind,
                    "refreshLibrary": "false",
                },
            )

    def items(self):
        return self.call(
            "GET",
            "/Items",
            params={
                "userId": self.user,
                "recursive": "true",
                "includeItemTypes": "Movie,Episode,Audio",
                "fields": "Path",
                "sortBy": "SortName",
            },
        )["Items"]

    def wait_counts(self, expected):
        for _ in range(120):
            items = self.items()
            if dict(Counter(item["Type"] for item in items)) == expected:
                return items
            time.sleep(0.5)
        raise RuntimeError("fixture library did not reach expected counts")

    def exercise(self):
        self.bootstrap()
        self.make_media()
        self.call("POST", "/Library/Refresh")
        expected = {"Movie": 2, "Episode": 1, "Audio": 2}
        items = self.wait_counts(expected)
        movies = [item for item in items if item["Type"] == "Movie"]
        target, deleted = movies[0], movies[1]
        data = {
            "Played": False,
            "PlayCount": 3,
            "PlaybackPositionTicks": 120000000,
            "IsFavorite": True,
        }
        route = "/UserItems/%s/UserData" % target["Id"]
        self.call("POST", route, data, {"userId": self.user})
        readback = self.call("GET", route, params={"userId": self.user})
        assert all(readback[k] == v for k, v in data.items())
        path = Path(deleted["Path"]).resolve()
        assert path in [p.resolve() for p in self.movie_paths]
        self.call("DELETE", "/Items/" + deleted["Id"])
        self.wait_counts({"Movie": 1, "Episode": 1, "Audio": 2})
        assert not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.root / "seed.mkv", path)
        self.call("POST", "/Library/Refresh")
        self.wait_counts(expected)
        # Interrupt enumeration after one page. A caller must retain its last
        # complete snapshot rather than publishing this partial generation.
        page = self.call(
            "GET",
            "/Items",
            params={
                "userId": self.user,
                "recursive": "true",
                "includeItemTypes": "Movie",
                "startIndex": 0,
                "limit": 1,
            },
        )
        assert len(page["Items"]) == 1 and page["TotalRecordCount"] == 2
        self.stop()
        unavailable = False
        try:
            self.items()
        except (urllib.error.URLError, TimeoutError):
            unavailable = True
        assert unavailable
        self.start()
        self.login()
        self.wait_counts(expected)
        after_restart = self.call("GET", route, params={"userId": self.user})
        assert all(after_restart[k] == v for k, v in data.items())
        return {
            "version": VERSION,
            "initial_counts": expected,
            "final_counts": expected,
            "userdata_roundtrip": data,
            "userdata_survived_restart": True,
            "deleted_owned_file": True,
            "restored_movie": True,
            "enumeration_interrupted_after_items": 1,
            "enumeration_total": 2,
            "offline_request_failed": unavailable,
            "server_restarted": True,
            "scope": "fixture server behavior; Kofin sync recovery is a phase 3 gate",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--port", type=int, default=18997)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not args.ffmpeg:
        parser.error("ffmpeg is required")
    root = args.workdir or Path(tempfile.mkdtemp(prefix="kofin-or-fixture-")) / "server"
    fixture = Fixture(args.binary.resolve(), root.resolve(), args.port, args.ffmpeg)
    try:
        result = fixture.exercise()
        result["binary_sha256"] = hashlib.sha256(args.binary.read_bytes()).hexdigest()
        result["captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    finally:
        fixture.stop()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print("Fixture passed and stopped. Private working files:", root)


if __name__ == "__main__":
    main()
