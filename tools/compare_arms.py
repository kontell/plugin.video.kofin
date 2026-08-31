#!/usr/bin/env python3
"""Paired arm A vs arm D comparison for the music shakedown (§9).

Arm A's rows were sampled before the item key was corrected, so its stored key is
the whole playing URL; the Jellyfin item id is re-extracted from it here. Arm D's
rows already carry the corrected key (a filename stem, since a repointed song has
no item id anywhere in its path). Keys are only ever compared *within* an arm, so
the two derivations never meet.
"""
import json, os, re, statistics, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests", "live"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from syncplay_music import qualified_delta, straddle, boundaries, HOLE   # noqa: E402
from analyse_capture import load, silence_runs, delta_series, RATE       # noqa: E402

ITEM = re.compile(r"[0-9a-f]{32}")
NAMES = ["P1D", "PIERS", "BRAVIA", "TAB"]


def rows_for(path):
    out = []
    for t, row in json.load(open(path)):
        fixed = {}
        for name, snap in row.items():
            if snap is None:
                fixed[name] = HOLE
                continue
            key = snap[4][0] or ""
            m = ITEM.search(key)
            if m:
                key = m.group(0)
            elif key.startswith(("http", "/", "plugin://")):
                key = os.path.splitext(os.path.basename(key.split("?", 1)[0]))[0]
            fixed[name] = (snap[0], snap[1], snap[2], snap[3], (key, snap[4][1]))
        out.append((t, fixed))
    return out


def cluster(runs):
    out = []
    for s, d in runs:
        if out and s - out[-1][0] < 5.0:
            out[-1][1] += d
        else:
            out.append([s, d])
    return out


def tsg(capdir):
    gaps = {}
    for name, f in (("P1D", "a.raw"), ("PIERS", "b.raw")):
        sig = load(os.path.join(capdir, f))
        runs = [r for r in silence_runs(sig) if r[1] >= 50.0]
        if runs and runs[0][0] < 1.0:
            runs = runs[1:]                     # drop the lead-in
        c = cluster(runs)
        if c and c[-1][1] > 20000:
            c = c[:-1]                          # drop trailing silence after the album
        gaps[name] = [x[1] for x in c]
    return gaps


def arm(label, root):
    r = {"label": label}
    rows = rows_for(os.path.join(root, "rows.json"))
    r["rows"] = len(rows)
    r["delta_rpc"] = {}
    for other in NAMES[1:]:
        d = qualified_delta(rows, "P1D", other)
        if d:
            mags = sorted(abs(x[1]) for x in d)
            r["delta_rpc"][other] = (statistics.median([x[1] for x in d]),
                                     mags[int(0.95 * (len(mags) - 1))], len(d))
    trio = ["P1D", "PIERS", "TAB"]
    r["straddle_all"] = sum(s[2] for s in straddle(rows, NAMES))
    r["straddle_trio"] = sum(s[2] for s in straddle(rows, trio))
    r["boundaries"] = {n: len(boundaries(rows, n)) for n in NAMES}
    capdir = os.path.join(root, "capture")
    g = tsg(capdir)
    allg = g["P1D"] + g["PIERS"]
    r["tsg"] = (statistics.median(allg), min(allg), max(allg), len(g["P1D"]))
    ds = [d for d in delta_series(load(os.path.join(capdir, "a.raw")),
                                  load(os.path.join(capdir, "b.raw"))) if d[3]]
    mags = sorted(abs(d[1]) for d in ds)
    r["delta_cap"] = (statistics.median([d[1] for d in ds]),
                      mags[int(0.95 * (len(mags) - 1))], len(ds))
    return r


def main(argv):
    base = "/media/bluecon/dev/plugin.video.kofin/tests/live/results/music-A"
    a = arm("A (streamed)", os.path.join(base, "M1"))
    d = arm("D (downloaded)", os.path.join(base, "M1-D"))
    print("\n%-34s %18s %18s" % ("metric", "arm A", "arm D"))
    print("-" * 72)
    print("%-34s %18d %18d" % ("sample rows", a["rows"], d["rows"]))
    print("%-34s %15.0f ms %15.0f ms" % ("TSG median (capture)", a["tsg"][0], d["tsg"][0]))
    print("%-34s %15.0f ms %15.0f ms" % ("TSG min", a["tsg"][1], d["tsg"][1]))
    print("%-34s %15.0f ms %15.0f ms" % ("TSG max", a["tsg"][2], d["tsg"][2]))
    print("%-34s %18d %18d" % ("boundaries measured", a["tsg"][3], d["tsg"][3]))
    print("%-34s %15.0f ms %15.0f ms" % ("Δ P1D−PIERS median (capture)",
                                         a["delta_cap"][0], d["delta_cap"][0]))
    print("%-34s %15.0f ms %15.0f ms" % ("Δ P1D−PIERS p95 (capture)",
                                         a["delta_cap"][1], d["delta_cap"][1]))
    for other in NAMES[1:]:
        av = a["delta_rpc"].get(other); dv = d["delta_rpc"].get(other)
        print("%-34s %15s %15s" % ("Δ P1D−%s median (rpc)" % other,
              ("%.0f ms" % av[0]) if av else "-", ("%.0f ms" % dv[0]) if dv else "-"))
    print("%-34s %15.0f s  %15.0f s" % ("straddle, all four",
                                        a["straddle_all"], d["straddle_all"]))
    print("%-34s %15.0f s  %15.0f s" % ("straddle, P1D/PIERS/TAB",
                                        a["straddle_trio"], d["straddle_trio"]))
    print("%-34s %18s %18s" % ("boundaries per member",
          "/".join(str(a["boundaries"][n]) for n in NAMES),
          "/".join(str(d["boundaries"][n]) for n in NAMES)))
    print("\n(boundaries per member listed as %s)" % "/".join(NAMES))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
