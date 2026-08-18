#!/usr/bin/env python3
"""Multi-device SyncPlay drift sampler — docs/syncplay-drift-shakedown.md §5.2.

Channel 2 of the shakedown: external truth. Polls every device's player over
JSON-RPC on one host clock and reduces the samples to the numbers the plan asks
for — pairwise divergence percentiles, each device's media-clock rate error in
ppm, and the tempo duty cycle.

Three things about the measurement, since they decide whether the output means
anything:

* **Every reading is bracketed.** A sample's timestamp is the midpoint of its
  round trip and its uncertainty is half the round trip; samples wider than
  ``--max-unc`` are dropped rather than trusted. On this LAN the RPC round trip
  is a few milliseconds, well inside a 75 ms tolerance, but a Wi-Fi member can
  spike and a spike must not read as drift.
* **JSON-RPC ``speed`` cannot see tempo.** It is an int, and a 1.01x tempo
  correction leaves it at 1. The only external view of the actuator is the
  ``Player.IsTempo`` / ``Player.PlaySpeed`` infolabels, so those are sampled
  too (kodi-drive: kodi-playback-tempo).
* **Interpolation onto a common grid is legitimate, gaps are not.** Playback is
  locally linear, so a position between two samples 250 ms apart is a straight
  line; a position inside a 30 s buffer stall is not. Grid points whose
  bracketing samples are further apart than ``--max-gap`` are dropped for that
  device instead of invented, which keeps the stall scenarios (R-A, R-J)
  honest.

A run refuses to start unless every device is already playing, and prints live
pairwise divergence while it samples, so the operator can see the run working
from whichever room they are standing in.

Usage:
    ./syncplay_drift.py sample --device A=192.168.1.198:8080 \\
        --device B=192.168.1.217:8080 --seconds 3600 --label p2-tol150
    ./syncplay_drift.py sample --device A=... --hz 10 --seconds 60 --label calib-A
    ./syncplay_drift.py aggregate tests/live/results/drift/<run-id>
"""

import argparse
import base64
import bisect
import csv
import http.client
import json
import math
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "drift")
COLUMNS = [
    "host_ms",
    "unc_ms",
    "pos_ms",
    "total_ms",
    "speed",
    "cache",
    "playspeed",
    "istempo",
    "caching",
    "flags_ms",
    "err",
]
GRID_MS = 250.0

#################################################################################################


def local_ms():
    return time.time() * 1000.0


def time_to_ms(value):
    """Kodi's Global.Time object -> milliseconds."""
    if not isinstance(value, dict):
        return None

    return (
        value.get("hours", 0) * 3600000.0
        + value.get("minutes", 0) * 60000.0
        + value.get("seconds", 0) * 1000.0
        + value.get("milliseconds", 0)
    )


class Device(object):
    """One Kodi, addressed over HTTP JSON-RPC."""

    def __init__(self, name, host, port, auth):
        self.name = name
        self.host = host
        self.port = int(port)
        self.url = "http://%s:%s/jsonrpc" % (host, port)
        self.auth = base64.b64encode(auth.encode()).decode()
        self.playerid = None
        self.batch = True  # until a batch response proves otherwise
        self.errors = 0
        self._connection = None
        self._flags = {"playspeed": "", "istempo": "", "caching": "", "at": 0.0}

    # --- transport ----------------------------------------------------

    def _send(self, payload, timeout=4):
        """One JSON-RPC round trip on a kept-alive socket.

        Keep-alive is not an optimisation here, it is the measurement: a fresh
        TCP handshake per sample would add a round trip to every reading and
        land in the uncertainty column, which is the number deciding whether a
        75 ms tolerance is measurable at all.
        """
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Basic " + self.auth,
            "Connection": "keep-alive",
        }

        for last_attempt in (False, True):
            if self._connection is None:
                self._connection = http.client.HTTPConnection(
                    self.host, self.port, timeout=timeout
                )

            try:
                sent = local_ms()
                self._connection.request("POST", "/jsonrpc", body, headers)
                response = self._connection.getresponse()
                raw = response.read()
                received = local_ms()

                if response.status != 200:
                    raise RuntimeError("HTTP %d" % response.status)

                return sent, received, json.loads(raw)
            except Exception:
                # A kept-alive socket the peer closed fails once and works on
                # the retry; a real fault fails twice and is raised.
                self._close()

                if last_attempt:
                    raise

        raise RuntimeError("unreachable")

    def _close(self):
        connection, self._connection = self._connection, None

        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def rpc(self, method, params=None, timeout=4):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method}

        if params is not None:
            payload["params"] = params

        _, _, body = self._send(payload, timeout)

        if "error" in body:
            raise RuntimeError("%s: %s" % (method, body["error"]))

        return body.get("result")

    # --- setup --------------------------------------------------------

    def resolve_player(self):
        """Current playerid, or None when the device is not playing."""
        for entry in self.rpc("Player.GetActivePlayers") or []:
            if entry.get("type") in ("video", "audio"):
                self.playerid = entry.get("playerid")
                return self.playerid

        self.playerid = None
        return None

    def describe(self):
        """Everything worth recording about the device once, into meta.json."""
        info = {"name": self.name, "url": self.url}

        try:
            info["kodi"] = self.rpc(
                "Application.GetProperties", {"properties": ["version", "name"]}
            )
            info["playerid"] = self.playerid
            info["item"] = self.rpc(
                "Player.GetItem",
                {"playerid": self.playerid, "properties": ["file", "title", "runtime"]},
            )

            for setting in (
                "videoplayer.usedisplayasclock",
                "videoplayer.adjustrefreshrate",
            ):
                info[setting] = self.rpc(
                    "Settings.GetSettingValue", {"setting": setting}
                ).get("value")
        except Exception as error:  # a partial description still beats none
            info["describe_error"] = str(error)

        return info

    # --- sampling -----------------------------------------------------

    def sample(self, want_flags=False):
        """One row: the position now, plus the most recent tempo/caching read.

        The two are deliberately **separate requests**. Infolabels and info
        booleans are evaluated on Kodi's GUI thread, and that dispatch is
        measurable: on the Bravia `Player.GetProperties` answers in 2.5-3 ms
        while `XBMC.GetInfoLabels` takes 19-20 ms, and on the phone and tablet
        both are worse. Batching them together would put the position's
        timestamp behind an app-thread round trip and destroy the precision the
        whole channel exists for, so position is sampled alone at full rate and
        the flags are refreshed about once a second and carried forward with
        their own timestamp (``flags_ms``).
        """
        row = dict.fromkeys(COLUMNS, "")

        if self.playerid is None:
            try:
                if self.resolve_player() is None:
                    row["host_ms"] = "%.1f" % local_ms()
                    row["err"] = "noplayer"
                    self.errors += 1
                    return row
            except Exception as error:
                row["host_ms"] = "%.1f" % local_ms()
                row["err"] = type(error).__name__
                self.errors += 1
                return row

        try:
            sent, received, body = self._send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "Player.GetProperties",
                    "params": {
                        "playerid": self.playerid,
                        "properties": ["time", "totaltime", "speed", "cachepercentage"],
                    },
                }
            )
            player = body.get("result") or {}
        except Exception as error:
            row["host_ms"] = "%.1f" % local_ms()
            row["err"] = type(error).__name__
            self.errors += 1
            self.playerid = None  # re-resolve on the next round
            return row

        position = time_to_ms(player.get("time"))

        if position is None:
            row["host_ms"] = "%.1f" % ((sent + received) / 2.0)
            row["err"] = "notime"
            self.errors += 1
            self.playerid = None
            return row

        row["host_ms"] = "%.1f" % ((sent + received) / 2.0)
        row["unc_ms"] = "%.1f" % ((received - sent) / 2.0)
        row["pos_ms"] = "%.0f" % position
        row["total_ms"] = "%.0f" % (time_to_ms(player.get("totaltime")) or 0.0)
        row["speed"] = player.get("speed", "")
        row["cache"] = player.get("cachepercentage", "")

        if want_flags:
            self._read_flags()

        row["playspeed"] = self._flags["playspeed"]
        row["istempo"] = self._flags["istempo"]
        row["caching"] = self._flags["caching"]
        row["flags_ms"] = "" if not self._flags["at"] else "%.1f" % self._flags["at"]
        return row

    def _read_flags(self):
        """Refresh the GUI-thread state (tempo, caching). Never raises."""
        request = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "XBMC.GetInfoBooleans",
                "params": {"booleans": ["Player.IsTempo", "Player.Caching"]},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "XBMC.GetInfoLabels",
                "params": {"labels": ["Player.PlaySpeed"]},
            },
        ]

        try:
            sent, received, body = self._send(request if self.batch else request[0])
        except Exception:
            return

        if self.batch and not isinstance(body, list):
            # This Kodi does not batch: two requests from here on.
            self.batch = False
            return

        if isinstance(body, list):
            answers = {entry.get("id"): entry.get("result") or {} for entry in body}
            booleans, labels = answers.get(1) or {}, answers.get(2) or {}
        else:
            booleans, labels = body.get("result") or {}, {}

            try:
                _, _, second = self._send(request[1])
                labels = second.get("result") or {}
            except Exception:
                pass

        self._flags = {
            "playspeed": labels.get("Player.PlaySpeed", ""),
            "istempo": int(bool(booleans.get("Player.IsTempo"))),
            "caching": int(bool(booleans.get("Player.Caching"))),
            "at": (sent + received) / 2.0,
        }


#################################################################################################
# sample


def parse_device(spec, default_auth):
    """``name=host:port`` or ``name=host:port:user:pass``."""
    if "=" not in spec:
        raise SystemExit("bad --device %r, expected name=host:port" % spec)

    name, target = spec.split("=", 1)
    parts = target.split(":")

    if len(parts) == 2:
        host, port, auth = parts[0], parts[1], default_auth
    elif len(parts) == 4:
        host, port, auth = parts[0], parts[1], "%s:%s" % (parts[2], parts[3])
    else:
        raise SystemExit("bad --device %r, expected name=host:port[:user:pass]" % spec)

    return Device(name, host, port, auth)


def sample(args):
    devices = [parse_device(spec, args.auth) for spec in args.device]

    if len(devices) < 1:
        raise SystemExit("nothing to sample")

    # Fail loudly before the run rather than producing an empty CSV: a device
    # that is not playing is the one mistake that wastes a whole soak.
    idle = []

    for device in devices:
        try:
            if device.resolve_player() is None:
                idle.append(device.name)
        except Exception as error:
            raise SystemExit("%s unreachable: %s" % (device.name, error))

    if idle:
        raise SystemExit("not playing: %s — start the group first" % ", ".join(idle))

    run_id = "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), args.label or "run")
    outdir = os.path.join(args.out, run_id)
    os.makedirs(outdir)

    meta = {
        "run_id": run_id,
        "label": args.label,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hz": args.hz,
        "seconds": args.seconds,
        "tolerance_ms": args.tolerance,
        "note": args.note,
        "devices": [device.describe() for device in devices],
    }

    with open(os.path.join(outdir, "meta.json"), "w") as handle:
        json.dump(meta, handle, indent=2, sort_keys=True)

    handles, writers = {}, {}

    for device in devices:
        handle = open(os.path.join(outdir, "%s.csv" % device.name), "w", newline="")
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        handles[device.name] = handle
        writers[device.name] = writer

    print("run %s -> %s" % (run_id, outdir))
    print(
        "sampling %s at %g Hz for %ss (ctrl-c to stop early)"
        % (", ".join(d.name for d in devices), args.hz, args.seconds)
    )

    interval = 1.0 / args.hz
    started = time.time()
    pool = ThreadPoolExecutor(max_workers=len(devices))
    rounds = 0
    last_status = 0.0

    try:
        while time.time() - started < args.seconds:
            due = started + rounds * interval
            delay = due - time.time()

            if delay > 0:
                time.sleep(delay)

            # Refresh the GUI-thread flags about once a second; the position
            # itself is sampled every round on its own fast request.
            want_flags = rounds % max(1, int(round(args.hz))) == 0
            rows = dict(
                zip(
                    [d.name for d in devices],
                    list(pool.map(lambda d: d.sample(want_flags), devices)),
                )
            )
            rounds += 1

            for name, row in rows.items():
                writers[name].writerow(row)

            if rounds % max(1, int(args.hz)) == 0:
                for handle in handles.values():
                    handle.flush()

            now = time.time()

            if now - last_status >= 10.0:
                last_status = now
                print("  %s  %s" % (time.strftime("%H:%M:%S"), live_line(rows)))
    except KeyboardInterrupt:
        print("\nstopped after %.0fs" % (time.time() - started))
    finally:
        pool.shutdown(wait=True)

        for handle in handles.values():
            handle.close()

    for device in devices:
        if device.errors:
            print("  %s: %d failed samples" % (device.name, device.errors))

    if not args.no_aggregate:
        aggregate(
            argparse.Namespace(
                rundir=outdir, max_unc=args.max_unc, max_gap=args.max_gap
            )
        )

    return outdir


def live_line(rows):
    """Pairwise divergence from one round, for the operator's benefit.

    The samples in a round are taken concurrently but not simultaneously, so
    subtract the host-clock difference as well as the position difference.
    """
    usable = {
        name: (float(row["host_ms"]), float(row["pos_ms"]))
        for name, row in rows.items()
        if row["pos_ms"] != ""
    }
    names = sorted(usable)
    parts = []

    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            host_l, pos_l = usable[left]
            host_r, pos_r = usable[right]
            parts.append(
                "%s-%s %+.0fms" % (left, right, (pos_l - pos_r) - (host_l - host_r))
            )

    for name, row in sorted(rows.items()):
        if row["pos_ms"] == "":
            parts.append("%s %s" % (name, row["err"] or "?"))
        elif str(row["caching"]) == "1":
            parts.append("%s caching" % name)
        elif str(row["istempo"]) == "1":
            parts.append("%s tempo %s" % (name, row["playspeed"]))

    return "  ".join(parts) or "no usable samples"


#################################################################################################
# aggregate


def load(path, max_unc):
    """(host_ms, pos_ms) series plus the flag series, filtered by uncertainty.

    Flags are refreshed at about 1 Hz and carried forward on the rows between
    refreshes, so they are de-duplicated by their own timestamp here: a duty
    cycle counted over carried-forward copies would be the same fraction, but
    the engagement count would depend on the sampling rate.
    """
    series, flags = [], []
    last_flags_at = None

    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            if row["err"] or row["pos_ms"] == "":
                continue

            unc = float(row["unc_ms"] or 0.0)

            if unc > max_unc:
                continue

            series.append((float(row["host_ms"]), float(row["pos_ms"])))
            flags_at = row.get("flags_ms") or ""

            if flags_at and flags_at != last_flags_at:
                last_flags_at = flags_at
                flags.append(
                    (float(flags_at), tempo_active(row), str(row["caching"]) == "1")
                )

    return series, flags


def tempo_active(row):
    """Whether the tempo actuator was engaged for this sample.

    ``Player.IsTempo`` answers it directly and is always sampled on the batch
    path. The ``Player.PlaySpeed`` fallback needs care: it reads ``0.00`` while
    nothing is playing and while paused, so "not 1.00" would count every paused
    stretch as a correction.
    """
    if str(row.get("istempo")) == "1":
        return True

    try:
        speed = float(row.get("playspeed") or 0.0)
    except ValueError:
        return False

    return speed > 0.01 and abs(speed - 1.0) >= 0.005


def interpolate(series, grid, max_gap_ms):
    """Positions on the common grid; None where the bracketing gap is too wide."""
    hosts = [point[0] for point in series]
    out = []

    for when in grid:
        index = bisect.bisect_left(hosts, when)

        if index == 0:
            # The grid starts at the latest first-sample across devices, so for
            # the device that set it the first point is an exact hit, not a
            # miss.
            out.append(series[0][1] if hosts and hosts[0] == when else None)
            continue

        if index >= len(hosts):
            out.append(None)
            continue

        left_h, left_p = series[index - 1]
        right_h, right_p = series[index]

        if right_h - left_h > max_gap_ms:
            out.append(None)
            continue

        span = right_h - left_h
        weight = 0.0 if span <= 0 else (when - left_h) / span
        out.append(left_p + weight * (right_p - left_p))

    return out


def slope_ppm(series):
    """Media-clock rate error against the host clock, in ppm, plus fit RMS."""
    if len(series) < 3:
        return None, None

    mean_h = statistics.fmean(point[0] for point in series)
    mean_p = statistics.fmean(point[1] for point in series)
    sxx = sum((point[0] - mean_h) ** 2 for point in series)

    if sxx <= 0:
        return None, None

    sxy = sum((point[0] - mean_h) * (point[1] - mean_p) for point in series)
    slope = sxy / sxx
    residuals = [point[1] - (mean_p + slope * (point[0] - mean_h)) for point in series]
    rms = math.sqrt(statistics.fmean(value**2 for value in residuals))
    return (slope - 1.0) * 1e6, rms


def percentile(values, fraction):
    if not values:
        return None

    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def flag_stats(flags, grid_span_s):
    """Tempo duty cycle and engagement count, plus caching episodes."""
    if not flags:
        return {}

    tempo = [entry[1] for entry in flags]
    caching = [entry[2] for entry in flags]
    engages = sum(
        1 for index in range(1, len(tempo)) if tempo[index] and not tempo[index - 1]
    )
    stalls = sum(
        1
        for index in range(1, len(caching))
        if caching[index] and not caching[index - 1]
    )
    return {
        "tempo_duty_pct": 100.0 * sum(tempo) / len(tempo),
        "tempo_engagements": engages,
        "tempo_engagements_per_hour": (
            engages / (grid_span_s / 3600.0) if grid_span_s > 0 else 0.0
        ),
        "caching_episodes": stalls,
        "caching_duty_pct": 100.0 * sum(caching) / len(caching),
    }


def aggregate(args):
    rundir = args.rundir
    names = sorted(
        os.path.splitext(name)[0]
        for name in os.listdir(rundir)
        if name.endswith(".csv")
    )

    if not names:
        raise SystemExit("no CSVs in %s" % rundir)

    loaded = {
        name: load(os.path.join(rundir, "%s.csv" % name), args.max_unc)
        for name in names
    }
    usable = {name: data for name, data in loaded.items() if len(data[0]) > 2}

    if not usable:
        raise SystemExit("no usable samples in %s" % rundir)

    start = max(data[0][0][0] for data in usable.values())
    end = min(data[0][-1][0] for data in usable.values())

    if end <= start:
        raise SystemExit("device sample windows do not overlap")

    grid = [start + step * GRID_MS for step in range(int((end - start) / GRID_MS) + 1)]
    span_s = (end - start) / 1000.0
    tracks = {
        name: interpolate(data[0], grid, args.max_gap) for name, data in usable.items()
    }

    summary = {
        "rundir": os.path.abspath(rundir),
        "span_s": round(span_s, 1),
        "grid_points": len(grid),
        "max_unc_ms": args.max_unc,
        "max_gap_ms": args.max_gap,
        "devices": {},
        "pairs": {},
    }

    for name, data in usable.items():
        ppm, rms = slope_ppm(data[0])
        summary["devices"][name] = dict(
            {
                "samples": len(data[0]),
                "dropped": len(loaded[name][0]) - len(data[0]),
                "rate_error_ppm": None if ppm is None else round(ppm, 1),
                "fit_rms_ms": None if rms is None else round(rms, 1),
                "grid_coverage_pct": round(
                    100.0
                    * sum(1 for value in tracks[name] if value is not None)
                    / len(grid),
                    1,
                ),
            },
            **flag_stats(data[1], span_s),
        )

    for index, left in enumerate(sorted(tracks)):
        for right in sorted(tracks)[index + 1 :]:
            pairs = [
                (tracks[left][step] - tracks[right][step])
                for step in range(len(grid))
                if tracks[left][step] is not None and tracks[right][step] is not None
            ]

            if not pairs:
                continue

            magnitudes = [abs(value) for value in pairs]
            summary["pairs"]["%s-%s" % (left, right)] = {
                "points": len(pairs),
                "p50_ms": round(percentile(magnitudes, 0.50), 1),
                "p95_ms": round(percentile(magnitudes, 0.95), 1),
                "max_ms": round(max(magnitudes), 1),
                "first_ms": round(pairs[0], 1),
                "last_ms": round(pairs[-1], 1),
                # The start offset is real drift too, but a constant one is a
                # different phenomenon from divergence that grows: report both.
                "growth_p95_ms": round(
                    percentile([abs(value - pairs[0]) for value in pairs], 0.95), 1
                ),
            }

    with open(os.path.join(rundir, "summary.json"), "w") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)

    report = render(summary)

    with open(os.path.join(rundir, "summary.md"), "w") as handle:
        handle.write(report)

    print(report)
    return summary


def render(summary):
    lines = [
        "# drift summary — %s" % os.path.basename(summary["rundir"]),
        "",
        "%.0fs of overlap, %d grid points, samples wider than %g ms dropped."
        % (summary["span_s"], summary["grid_points"], summary["max_unc_ms"]),
        "",
        "| device | samples | dropped | rate err (ppm) | fit RMS (ms) | tempo duty | engages/h | caching eps | grid cov |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for name, device in sorted(summary["devices"].items()):
        lines.append(
            "| %s | %d | %d | %s | %s | %s | %s | %s | %s%% |"
            % (
                name,
                device["samples"],
                device["dropped"],
                device["rate_error_ppm"],
                device["fit_rms_ms"],
                "%.1f%%" % device.get("tempo_duty_pct", 0.0),
                "%.0f" % device.get("tempo_engagements_per_hour", 0.0),
                device.get("caching_episodes", 0),
                device["grid_coverage_pct"],
            )
        )

    lines += [
        "",
        "| pair | p50 | p95 | max | first | last | growth p95 |",
        "|---|---|---|---|---|---|---|",
    ]

    for name, pair in sorted(summary["pairs"].items()):
        lines.append(
            "| %s | %.0f | %.0f | %.0f | %+.0f | %+.0f | %.0f |"
            % (
                name,
                pair["p50_ms"],
                pair["p95_ms"],
                pair["max_ms"],
                pair["first_ms"],
                pair["last_ms"],
                pair["growth_p95_ms"],
            )
        )

    return "\n".join(lines) + "\n"


#################################################################################################


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("sample", help="poll devices and write CSVs")
    run.add_argument(
        "--device",
        action="append",
        default=[],
        required=True,
        help="name=host:port[:user:pass], repeatable",
    )
    run.add_argument("--auth", default="kodi:kodi", help="default RPC credentials")
    run.add_argument("--hz", type=float, default=4.0)
    run.add_argument("--seconds", type=float, default=600.0)
    run.add_argument("--label", default="", help="run label, used in the run id")
    run.add_argument("--note", default="", help="free text into meta.json")
    run.add_argument(
        "--tolerance",
        type=int,
        default=0,
        help="syncPlayTolerance under test, recorded",
    )
    run.add_argument("--out", default=RESULTS)
    run.add_argument("--max-unc", type=float, default=15.0)
    run.add_argument("--max-gap", type=float, default=2000.0)
    run.add_argument("--no-aggregate", action="store_true")

    report = sub.add_parser("aggregate", help="reduce a run directory")
    report.add_argument("rundir")
    report.add_argument("--max-unc", type=float, default=15.0)
    report.add_argument("--max-gap", type=float, default=2000.0)

    args = parser.parse_args(argv)

    if args.command == "sample":
        sample(args)
    else:
        aggregate(args)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
