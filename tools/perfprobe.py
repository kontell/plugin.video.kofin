#!/usr/bin/env python3
"""Latency probes against the local Kodi, for before/after numbers on perf PRs.

This is the instrument behind docs/perf-hardening-plan.md: every performance
change re-runs the relevant probe and quotes the numbers in its PR description,
so regressions are a diff in a table rather than a feeling.

Subcommands:

  dir URL     Time ``Files.GetDirectory`` on a plugin path. ``--fresh`` appends
              a unique ``_cb`` parameter per run so Kodi's directory cache
              cannot serve the listing; without it repeats measure the cached
              path. The addon ignores unknown parameters.

  play URL    Time ``Player.Open`` to ``Player.OnAVStart`` using Kodi's TCP
              notification socket (default port 9090), then stop playback.
              This is click-to-first-frame as the player announces it.

  image FILE  Push image URLs (one per line, ``-`` for stdin) through Kodi's
              own image pipeline (fetch + decode + cache) via the webserver's
              ``/image`` endpoint, twice: the first pass is cold only for URLs
              Kodi has not already cached, the second is always warm.

Kodi's webserver (8080) and "allow remote control" TCP interface (9090) must
be enabled; credentials default to kodi:kodi (``--auth`` to override).
"""

import argparse
import base64
import json
import socket
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


def rpc(
    args: argparse.Namespace,
    method: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 120.0,
) -> Tuple[float, Dict[str, Any]]:
    """One JSON-RPC call over HTTP; returns (elapsed seconds, parsed body)."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    ).encode()
    auth = base64.b64encode(args.auth.encode()).decode()
    request = urllib.request.Request(
        "http://%s:%d/jsonrpc" % (args.host, args.http_port),
        body,
        {"Content-Type": "application/json", "Authorization": "Basic " + auth},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        parsed: Dict[str, Any] = json.load(response)
    return time.monotonic() - started, parsed


def with_param(url: str, param: str) -> str:
    return url + ("&" if "?" in url else "?") + param


def probe_dir(args: argparse.Namespace) -> int:
    for run in range(1, args.runs + 1):
        url = args.url
        if args.fresh:
            url = with_param(url, "_cb=%d" % time.time_ns())
        elapsed, body = rpc(
            args,
            "Files.GetDirectory",
            {"directory": url, "media": "files", "properties": ["title"]},
        )
        result = body.get("result") or {}
        error = body.get("error")
        print(
            "dir run%d: %.3fs items=%d%s"
            % (
                run,
                elapsed,
                len(result.get("files") or []),
                " ERROR=%s" % error if error else "",
            )
        )
    return 0


def probe_play(args: argparse.Namespace) -> int:
    """Player.Open -> Player.OnAVStart wall clock, via the notification socket.

    The socket is connected before the open so the notification cannot be
    missed; Kodi frames TCP JSON-RPC as back-to-back JSON objects, so the
    stream is parsed incrementally with raw_decode.
    """
    sock = socket.create_connection((args.host, args.tcp_port), timeout=5)
    sock.settimeout(args.timeout)
    decoder = json.JSONDecoder()
    try:
        started = time.monotonic()
        rpc(args, "Player.Open", {"item": {"file": args.url}})
        buffered = ""
        while True:
            if time.monotonic() - started > args.timeout:
                print("play: no OnAVStart within %.0fs" % args.timeout)
                return 1
            buffered += sock.recv(65536).decode("utf-8", "replace")
            while buffered:
                try:
                    message, index = decoder.raw_decode(buffered)
                except ValueError:
                    break  # partial object; read more
                buffered = buffered[index:].lstrip()
                if message.get("method") == "Player.OnAVStart":
                    print("play: %.3fs to OnAVStart" % (time.monotonic() - started))
                    return 0
    finally:
        sock.close()
        for player in (0, 1, 2):
            try:
                rpc(args, "Player.Stop", {"playerid": player}, timeout=10)
            except Exception:
                pass


def probe_image(args: argparse.Namespace) -> int:
    source = sys.stdin if args.file == "-" else open(args.file)
    with source:
        urls = [line.strip() for line in source if line.strip()]
    if not urls:
        print("image: no URLs given")
        return 1
    auth = base64.b64encode(args.auth.encode()).decode()
    for label in ("first(cold)", "second(warm)"):
        timings: List[float] = []
        started = time.monotonic()
        for url in urls:
            wrapped = "image://" + urllib.parse.quote(url, safe="") + "/"
            request = urllib.request.Request(
                "http://%s:%d/image/%s"
                % (args.host, args.http_port, urllib.parse.quote(wrapped, safe="")),
                headers={"Authorization": "Basic " + auth},
            )
            single = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    response.read()
                timings.append(time.monotonic() - single)
            except Exception as error:
                timings.append(time.monotonic() - single)
                print("image: ERROR %s for %s" % (error, url[:80]))
        print(
            "image %s: total=%.2fs  %s"
            % (
                label,
                time.monotonic() - started,
                " ".join("%.0fms" % (elapsed * 1000) for elapsed in timings),
            )
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--tcp-port", type=int, default=9090)
    parser.add_argument("--auth", default="kodi:kodi", help="user:pass for JSON-RPC")
    commands = parser.add_subparsers(dest="command", required=True)

    directory = commands.add_parser("dir", help="time Files.GetDirectory")
    directory.add_argument("url")
    directory.add_argument("--runs", type=int, default=3)
    directory.add_argument(
        "--fresh",
        action="store_true",
        help="cache-bust each run so the plugin really runs",
    )
    directory.set_defaults(func=probe_dir)

    play = commands.add_parser("play", help="time Player.Open to OnAVStart")
    play.add_argument("url")
    play.add_argument("--timeout", type=float, default=60.0)
    play.set_defaults(func=probe_play)

    image = commands.add_parser("image", help="time Kodi's image pipeline")
    image.add_argument("file", help="file of image URLs, one per line, - for stdin")
    image.set_defaults(func=probe_image)

    args = parser.parse_args()
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
