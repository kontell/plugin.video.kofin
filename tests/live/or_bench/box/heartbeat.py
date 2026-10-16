"""Heartbeat: how late a Python thread in Kodi wakes from 100 ms sleeps.
Args: max seconds, output json path, stop-file path. Holds no xbmc.Monitor."""

import json, os, sys, time

limit, out, stop = float(sys.argv[1]), sys.argv[2], sys.argv[3]
start = time.monotonic()
t = start
gaps = []
big = []
while t - start < limit and not os.path.exists(stop):
    time.sleep(0.1)
    now = time.monotonic()
    gap = now - t - 0.1
    t = now
    gaps.append(gap)
    if gap > 1.0:
        big.append([round(now - start, 1), round(gap, 2)])
gaps.sort()
with open(out, "w") as f:
    json.dump(
        {
            "seconds": round(t - start, 1),
            "samples": len(gaps),
            "max": round(gaps[-1], 2) if gaps else 0,
            "p99": round(gaps[int(len(gaps) * 0.99)], 3) if gaps else 0,
            "over_1s": sum(1 for g in gaps if g > 1),
            "over_5s": sum(1 for g in gaps if g > 5),
            "stalled_s": round(sum(g for g in gaps if g > 1), 1),
            "big": big[:40],
        },
        f,
    )
