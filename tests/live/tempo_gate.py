#!/usr/bin/env python3
"""Gate 0 — does ``Player.SetTempo`` actually move the media clock on a device?

docs/syncplay-drift-shakedown.md §6, Gate 0. kofin's drift correction has
exactly one actuator, and it is unverified outside desktop Linux: Kodi gates
tempo on ``videoplayer.usedisplayasclock`` on every platform, but a
``SetTempo`` that answers OK and changes nothing would leave the whole
shakedown measuring a controller with no output. Android is the case in
question — kodi-drive records only that webOS overrides ``CanTempo()``.

The test is a slope measurement, not an API check. Play a file, measure the
media clock against the host clock, apply tempo, measure again: the second
slope must be steeper by the tempo step. Reading ``Player.PlaySpeed`` back
proves only that Kodi stored the number.

It also reports, for free:

* the device's **free-running rate error in ppm** with the tempo gate on —
  §5.3's calibration in miniature, which is what predicts the drift sawtooth;
* the **round-trip uncertainty during playback**, which is the open question on
  the Wi-Fi members (§5.2);
* any **position jump when tempo returns to 1.0** — the Omega restore-skip bug
  that `_verify_tempo_restore` exists for, and which Piers is meant to have
  fixed (R-L).

Nothing here involves kofin: it drives Kodi directly, so a failure is Kodi's.

Usage:
    ./tempo_gate.py --url http://192.168.1.112:18300/gate0.mp4 \\
        --device bravia=192.168.1.198:8080 --device pixel=192.168.1.218:8080
"""

import argparse
import json
import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from syncplay_drift import Device, local_ms, parse_device, slope_ppm, time_to_ms

TEMPO = 1.03
SETTING = "videoplayer.usedisplayasclock"

#################################################################################################


def rpc(device, method, params=None):
    """JSON-RPC call returning (result, error)."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}

    if params is not None:
        payload["params"] = params

    try:
        _, _, body = device._send(payload, timeout=10)
    except Exception as error:
        return None, str(error)

    if not isinstance(body, dict):
        return None, "unexpected response %r" % body

    return body.get("result"), body.get("error")


def position(device):
    """(host_ms, pos_ms, unc_ms) or None."""
    try:
        sent, received, body = device._send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "Player.GetProperties",
                "params": {"playerid": device.playerid, "properties": ["time"]},
            }
        )
    except Exception:
        return None

    value = time_to_ms((body.get("result") or {}).get("time"))

    if value is None:
        return None

    return ((sent + received) / 2.0, value, (received - sent) / 2.0)


def sample_window(device, seconds, hz=10.0):
    """[(host_ms, pos_ms)], [unc_ms] over a window."""
    series, uncs = [], []
    deadline = time.time() + seconds
    interval = 1.0 / hz

    while time.time() < deadline:
        reading = position(device)

        if reading is not None:
            series.append((reading[0], reading[1]))
            uncs.append(reading[2])

        time.sleep(interval)

    return series, uncs


def wait_for_playback(device, timeout=45.0):
    """Wait until a player is active and its clock is demonstrably moving."""
    deadline = time.time() + timeout
    first = None

    while time.time() < deadline:
        try:
            if device.resolve_player() is not None:
                reading = position(device)

                if reading is not None:
                    if first is None:
                        first = reading[1]
                    elif reading[1] > first + 200:
                        return True  # two readings that differ: really playing
        except Exception:
            pass

        time.sleep(0.5)

    return False


def biggest_jump(series):
    """Largest unexplained forward step between consecutive samples, in ms.

    Meaningless on its own: Kodi's reported position jitters by tens of
    milliseconds, so this is measured in **every** window and the tempo-restore
    window is compared against the two that had no restore in them. A restore
    skip is a jump that stands out from that control, not merely a nonzero one.
    """
    worst = 0.0

    for index in range(1, len(series)):
        host_delta = series[index][0] - series[index - 1][0]
        pos_delta = series[index][1] - series[index - 1][1]
        worst = max(worst, pos_delta - host_delta)

    return worst


def slope_stderr_ppm(series, rms):
    """Standard error of the fitted rate, in ppm.

    Without it an implausible slope looks like a finding. With ~60 ms of
    position jitter over a 30 s window this lands around a few hundred ppm, so
    a reading of tens of thousands is real and one of a few hundred is not.
    """
    if not series or rms is None or len(series) < 3:
        return None

    mean_h = statistics.fmean(point[0] for point in series)
    sxx = sum((point[0] - mean_h) ** 2 for point in series)

    if sxx <= 0:
        return None

    return (rms / math.sqrt(sxx)) * 1e6


#################################################################################################


def run(device, args):
    report = {"device": device.name}
    original, error = rpc(device, "Settings.GetSettingValue", {"setting": SETTING})

    if error or original is None:
        return dict(
            report, verdict="ERROR", detail="cannot read %s: %s" % (SETTING, error)
        )

    report["setting_was"] = original.get("value")
    wanted = {"on": True, "off": False}.get(args.clock)

    if wanted is not None and original.get("value") is not wanted:
        _, error = rpc(
            device,
            "Settings.SetSettingValue",
            {"setting": SETTING, "value": wanted},
        )

        if error:
            return dict(
                report,
                verdict="ERROR",
                detail="cannot set %s=%s: %s" % (SETTING, wanted, error),
            )

        print("  %s: set %s = %s" % (device.name, SETTING, wanted))

    report["clock_gate"] = original.get("value") if wanted is None else wanted

    print("  %s: opening %s" % (device.name, args.url))
    _, error = rpc(device, "Player.Open", {"item": {"file": args.url}})

    if error:
        return dict(report, verdict="ERROR", detail="Player.Open failed: %s" % error)

    if not wait_for_playback(device):
        return dict(report, verdict="ERROR", detail="playback never started")

    # Discard the settling period: right after a start Kodi's clock resyncs
    # (mode switch, audio sync), and a one-off 150 ms correction inside a 30 s
    # window reads as a 5000 ppm rate error.
    print("  %s: settling %.0fs" % (device.name, args.settle))
    time.sleep(args.settle)

    mode, _ = rpc(device, "XBMC.GetInfoLabels", {"labels": ["System.ScreenMode"]})
    report["screenmode"] = (mode or {}).get("System.ScreenMode")
    fps, _ = rpc(device, "XBMC.GetInfoLabels", {"labels": ["Player.Process(videofps)"]})
    report["videofps"] = (fps or {}).get("Player.Process(videofps)")

    print("  %s: baseline %.0fs at 1.0x" % (device.name, args.window))
    base, base_unc = sample_window(device, args.window)

    result, error = rpc(
        device, "Player.SetTempo", {"playerid": device.playerid, "tempo": TEMPO}
    )
    report["settempo_error"] = str(error) if error else None
    report["settempo_result"] = result

    flags, _ = rpc(
        device,
        "XBMC.GetInfoLabels",
        {"labels": ["Player.PlaySpeed"]},
    )
    booleans, _ = rpc(device, "XBMC.GetInfoBooleans", {"booleans": ["Player.IsTempo"]})
    report["playspeed"] = (flags or {}).get("Player.PlaySpeed")
    report["istempo"] = (booleans or {}).get("Player.IsTempo")

    print(
        "  %s: %.0fs at %.2fx (PlaySpeed=%s IsTempo=%s)"
        % (device.name, args.window, TEMPO, report["playspeed"], report["istempo"])
    )
    fast, fast_unc = sample_window(device, args.window)

    rpc(device, "Player.SetTempo", {"playerid": device.playerid, "tempo": 1.0})
    restored, _ = sample_window(device, args.restore_window)

    if args.stop:
        rpc(device, "Player.Stop", {"playerid": device.playerid})

    if args.restore_setting and original.get("value") is not None:
        rpc(
            device,
            "Settings.SetSettingValue",
            {"setting": SETTING, "value": original["value"]},
        )
        print("  %s: put %s back to %s" % (device.name, SETTING, original["value"]))

    base_ppm, base_rms = slope_ppm(base)
    fast_ppm, fast_rms = slope_ppm(fast)
    rest_ppm, rest_rms = slope_ppm(restored)

    if base_ppm is None or fast_ppm is None:
        return dict(report, verdict="ERROR", detail="not enough samples")

    delta = fast_ppm - base_ppm
    report.update(
        {
            "base_ppm": round(base_ppm, 1),
            "base_ppm_stderr": round(slope_stderr_ppm(base, base_rms) or 0.0, 1),
            "base_fit_rms_ms": round(base_rms, 1),
            "tempo_ppm": round(fast_ppm, 1),
            "tempo_ppm_stderr": round(slope_stderr_ppm(fast, fast_rms) or 0.0, 1),
            "restored_ppm": round(rest_ppm, 1) if rest_ppm is not None else None,
            "delta_ppm": round(delta, 1),
            "expected_ppm": round((TEMPO - 1.0) * 1e6, 1),
            # Same statistic in all three windows: only the restore window had a
            # tempo->1.0 in it, so the other two are the control.
            "jump_ms": {
                "baseline": round(biggest_jump(base), 1),
                "tempo": round(biggest_jump(fast), 1),
                "restore": round(biggest_jump(restored), 1),
            },
            "samples": [len(base), len(fast), len(restored)],
            "unc_ms": {
                "min": round(min(base_unc + fast_unc), 1),
                "median": round(statistics.median(base_unc + fast_unc), 1),
                "max": round(max(base_unc + fast_unc), 1),
            },
            "series": {
                "baseline": [[round(h, 1), round(p, 1)] for h, p in base],
                "tempo": [[round(h, 1), round(p, 1)] for h, p in fast],
                "restore": [[round(h, 1), round(p, 1)] for h, p in restored],
            },
        }
    )

    expected = (TEMPO - 1.0) * 1e6

    if delta >= expected * 0.5:
        report["verdict"] = "PASS"
    elif delta >= expected * 0.1:
        report["verdict"] = "PARTIAL"
    else:
        report["verdict"] = "FAIL"

    return report


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", action="append", default=[], required=True)
    parser.add_argument("--auth", default="kodi:kodi")
    parser.add_argument(
        "--url", required=True, help="a video URL every device can fetch"
    )
    parser.add_argument("--window", type=float, default=60.0)
    parser.add_argument("--restore-window", type=float, default=20.0)
    parser.add_argument(
        "--settle", type=float, default=10.0, help="discard this long after the start"
    )
    parser.add_argument(
        "--clock",
        choices=("on", "off", "asis"),
        default="on",
        help="%s during the run; 'off' is the control that proves the display "
        "clock is what sets the rate" % SETTING,
    )
    parser.add_argument("--stop", action="store_true", default=True)
    parser.add_argument(
        "--restore-setting",
        action="store_true",
        help="put %s back to its original value afterwards" % SETTING,
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    reports = []

    for spec in args.device:
        device = parse_device(spec, args.auth)
        print("== %s" % device.name)
        report = run(device, args)
        reports.append(report)
        terse = {k: v for k, v in report.items() if k != "series"}
        print("  %s: %s" % (device.name, json.dumps(terse, sort_keys=True)))

    print(
        "\n| device | clock | verdict | free-run ppm (±SE) | tempo ppm | delta (want %+.0f) "
        "| jump base/tempo/restore | unc med | mode | fps |" % ((TEMPO - 1.0) * 1e6)
    )
    print("|---|---|---|---|---|---|---|---|---|---|")

    for report in reports:
        jumps = report.get("jump_ms") or {}
        print(
            "| %s | %s | **%s** | %s ± %s | %s | %s | %s / %s / %s ms | %s ms | %s | %s |"
            % (
                report["device"],
                report.get("clock_gate", "-"),
                report.get("verdict"),
                report.get("base_ppm", "-"),
                report.get("base_ppm_stderr", "-"),
                report.get("tempo_ppm", "-"),
                report.get("delta_ppm", report.get("detail", "-")),
                jumps.get("baseline", "-"),
                jumps.get("tempo", "-"),
                jumps.get("restore", "-"),
                (report.get("unc_ms") or {}).get("median", "-"),
                report.get("screenmode", "-"),
                report.get("videofps", "-"),
            )
        )

    if args.out:
        with open(args.out, "w") as handle:
            json.dump(reports, handle, indent=2, sort_keys=True)

    return 0 if all(r.get("verdict") == "PASS" for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
