#!/usr/bin/env python3
"""Live driver for the SyncPlay music shakedown (docs/syncplay-music-shakedown.md).

Shares the box plumbing with ``syncplay_fine_sync.py`` — the same ``Member``
(settings, token, log grep, adb) — and adds what music needs and video did not.

**Why this is not just the video sampler with a different item.** Two things
break when the item is three minutes long instead of forty-five:

1. *A delta across a boundary is meaningless.* At a track change two members are
   on different tracks, and differencing their positions yields minus one track
   length — a number that looks like catastrophic desync and is an artefact.
   Every delta here is qualified by "both members report the same item", and the
   samples where they do not become their own series: **straddle**, which is a
   metric in its own right (§6.4) rather than something to filter away.

2. *Read holes are normal, not failure.* ``playback.py:270`` records that around
   a held music boundary ``isPlaying``/``getTime`` intermittently report no media
   for media that is right there. The sampler must survive that and count it; a
   hole rate is evidence about the boundary, and a sampler that aborted on one
   would be unable to measure the very thing the study is about.

Each sample is **one batched JSON-RPC round trip per member** (properties and
item together, against a cached player id). Three separate calls at 4 Hz across
four members is 48 requests a second, and on a box whose radio quantises to
~100 ms the read uncertainty would swamp the 30 ms bar §10 sets.

    tests/live/syncplay_music.py --selftest
    tests/live/syncplay_music.py \\
        --member A=192.168.1.112:8080,settings=<path>,log=<path>,tempo=<path> \\
        --member B=192.168.1.198:8080,adb=<serial>,settings=<path>,log=<path>,tempo=<path> \\
        sample --seconds 120
"""

import argparse
import base64
import json
import os
import re
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from syncplay_fine_sync import Member, log  # noqa: E402

# A hole is a sample where a member was expected to be playing and returned no
# usable position. Counted, never fatal.
HOLE = object()

# A Jellyfin item id as it appears in a playing URL: the first 32-hex token.
_ITEM_ID = re.compile(r"[0-9a-f]{32}")


class MusicMember(Member):
    """A Member that reports *what* it is playing as well as where."""

    def __init__(self, spec):
        Member.__init__(self, spec)
        self._playerid = None
        self.holes = 0
        self.samples = 0

    def batch(self, calls):
        payload = [
            {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
            for i, (method, params) in enumerate(calls)
        ]
        req = urllib.request.Request(
            "http://%s/jsonrpc" % self.host,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Basic "
                + base64.b64encode(
                    ("%s:%s" % (self.user, self.password)).encode()
                ).decode(),
            },
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            answers = json.loads(response.read().decode())
        out = [None] * len(calls)
        for answer in answers:
            if "error" not in answer:
                out[answer["id"]] = answer["result"]
        return out

    def snapshot(self):
        """One round trip: (host_s, pos_ms, unc_ms, speed, item_key) or HOLE.

        ``item_key`` is the **Jellyfin item id**, pulled out of the playing URL.

        Neither obvious choice works. The Kodi library id differs per box (the
        Tab built its library separately and numbers the same album differently),
        and the whole file path differs per *member and per play*: inside a group
        kofin resolves to
        ``…/Audio/<itemId>/stream.flac?static=true&mediaSourceId=…&deviceId=…&playSessionId=…``
        so two members never share a URL. Measured 2026-08-31 — a whole-path key
        qualified 0 of 3545 paired samples in M1, which reads as total desync and
        is purely an artefact of the key.

        The item id is the one identifier every member agrees on, and it appears
        in both URL forms (a direct library URL and a plugin route). It is the
        first 32-hex token in the path; ``mediaSourceId`` and ``playSessionId``
        are also 32-hex but come later in the query, so first-match is correct.

        **Arm D carries no item id at all.** A downloaded song is repointed to a
        local file under each member's own downloads root, and the roots differ
        (``/home/conor/kofin-downloads`` vs the flatpak's sandbox vs
        ``/storage/…`` on Android), as does the container — FLAC where
        ``downloadsMusicTranscode`` is off, Opus where it is on. What survives all
        of that is the **filename stem**: ``01 Marker 01``. So the fallback is the
        basename without its extension, which is identical on every member in
        both arms.
        """
        self.samples += 1
        if self._playerid is None:
            try:
                players = self.rpc("Player.GetActivePlayers")
            except Exception:
                self.holes += 1
                return HOLE
            if not players:
                self.holes += 1
                return HOLE
            self._playerid = players[0]["playerid"]

        t0 = time.time()
        try:
            props, item = self.batch(
                [
                    (
                        "Player.GetProperties",
                        {"playerid": self._playerid, "properties": ["time", "speed"]},
                    ),
                    (
                        "Player.GetItem",
                        {
                            "playerid": self._playerid,
                            "properties": ["title", "album", "file"],
                        },
                    ),
                ]
            )
        except Exception:
            self._playerid = None  # player may have gone; re-probe next time
            self.holes += 1
            return HOLE
        t1 = time.time()

        if not props or not props.get("time") or not item:
            self._playerid = None
            self.holes += 1
            return HOLE

        clock = props["time"]
        pos = (
            (clock["hours"] * 60 + clock["minutes"]) * 60 + clock["seconds"]
        ) * 1000 + clock["milliseconds"]
        detail = item.get("item") or {}
        key = None
        path = detail.get("file") or ""
        found = _ITEM_ID.search(path)
        if found:
            key = found.group(0)
        elif path:
            # Arm D: a local file. Root and container differ per member; the
            # filename stem does not.
            key = os.path.splitext(os.path.basename(path.split("?", 1)[0]))[0]
        if not key:
            key = detail.get("id")
            if not key or key == -1:
                key = detail.get("label")
        if not key:
            # Nothing identifies what is playing: a hole, not a made-up key.
            self.holes += 1
            return HOLE
        return (
            (t0 + t1) / 2.0,
            float(pos),
            (t1 - t0) * 500.0,
            props.get("speed", 0),
            ("%s" % key, detail.get("label") or ""),
        )


def sample(members, seconds, hz=4.0):
    """Rows of {name: snapshot} at ``hz`` for ``seconds``. Holes are kept."""
    rows = []
    end = time.time() + seconds
    while time.time() < end:
        row = {}
        for member in members:
            row[member.name] = member.snapshot()
        rows.append((time.time(), row))
        time.sleep(max(0.0, 1.0 / hz))
    return rows


# ---------------------------------------------------------------------------
# metrics (§6.4). Pure functions of the rows, so they self-test without a box.
# ---------------------------------------------------------------------------


def qualified_delta(rows, a, b):
    """Δ in ms (a − b, positive = a ahead), only where both play the same item.

    The read-time gap is removed the way the video sampler does it: playback runs
    at ~1x between the two reads, so the difference is corrected by how far apart
    they were taken.
    """
    out = []
    for host_s, row in rows:
        pa, pb = row.get(a), row.get(b)
        if pa is HOLE or pb is HOLE or pa is None or pb is None:
            continue
        if pa[4][0] != pb[4][0]:
            continue
        div = (pa[1] - pb[1]) - (pa[0] - pb[0]) * 1000.0
        out.append((host_s, div, pa[2] + pb[2]))
    return out


def straddle(rows, names):
    """Spans (start_s, end_s, secs) where members disagree about the item.

    This is the boundary cost seen from outside: how long the group is split
    across two tracks. It is not an error in the sampler and must never be
    silently dropped — §2 predicts it is where music's whole penalty lives.
    """
    spans, start = [], None
    for host_s, row in rows:
        keys = {
            row[n][4][0]
            for n in names
            if row.get(n) is not HOLE and row.get(n) is not None
        }
        split = len(keys) > 1
        if split and start is None:
            start = host_s
        elif not split and start is not None:
            spans.append((start, host_s, host_s - start))
            start = None
    if start is not None:
        spans.append((start, rows[-1][0], rows[-1][0] - start))
    return spans


def boundaries(rows, name):
    """(host_s, from_key, to_key) each time one member's item changes.

    Resolution is the sample interval, so this locates a boundary; §6.1's capture
    times it. The two channels answer different questions and neither replaces
    the other.
    """
    out, prev = [], None
    for host_s, row in rows:
        snap = row.get(name)
        if snap is HOLE or snap is None:
            continue
        key = snap[4][0]
        if prev is not None and key != prev:
            out.append((host_s, prev, key))
        prev = key
    return out


def hole_rate(members):
    return {
        m.name: (m.holes, m.samples, 100.0 * m.holes / m.samples if m.samples else 0.0)
        for m in members
    }


def describe(deltas, label):
    if not deltas:
        log("%s: no qualified samples" % label)
        return None
    divs = [d[1] for d in deltas]
    mags = sorted(abs(d) for d in divs)
    p95 = mags[int(0.95 * (len(mags) - 1))]
    unc = statistics.median([d[2] for d in deltas])
    log(
        "%s: median %+.0f ms, p95 |Δ| %.0f, max |Δ| %.0f, read unc ±%.0f, n=%d"
        % (label, statistics.median(divs), p95, mags[-1], unc, len(divs))
    )
    return {
        "median": statistics.median(divs),
        "p95": p95,
        "max": mags[-1],
        "uncertainty": unc,
        "n": len(divs),
    }


# ---------------------------------------------------------------------------


def _selftest():
    """Verify the metrics against rows with known answers, no devices needed."""

    def snap(t, pos, key):
        return (t, float(pos), 2.0, 1, (key, key))

    rows = []
    # 0-2 s both on track 1, B is 40 ms behind.
    for i in range(8):
        t = i * 0.25
        rows.append(
            (t, {"A": snap(t, 1000 + i * 250, "t1"), "B": snap(t, 960 + i * 250, "t1")})
        )
    # 2-3 s A has advanced to track 2, B has not: a straddle.
    for i in range(8, 12):
        t = i * 0.25
        rows.append(
            (t, {"A": snap(t, (i - 8) * 250, "t2"), "B": snap(t, 960 + i * 250, "t1")})
        )
    # 3-4 s both on track 2, B 40 ms behind again.
    for i in range(12, 16):
        t = i * 0.25
        rows.append(
            (
                t,
                {
                    "A": snap(t, (i - 8) * 250, "t2"),
                    "B": snap(t, (i - 8) * 250 - 40, "t2"),
                },
            )
        )
    # one hole on B
    rows.append((4.0, {"A": snap(4.0, 2000, "t2"), "B": HOLE}))

    failures = []

    deltas = qualified_delta(rows, "A", "B")
    if len(deltas) != 12:
        failures.append(
            "qualified_delta kept %d rows, want 12 (4 straddle + 1 "
            "hole excluded)" % len(deltas)
        )
    median = statistics.median([d[1] for d in deltas])
    if abs(median - 40.0) > 1e-6:
        failures.append("median Δ %.3f, want +40.000" % median)

    # The artefact this exists to prevent: unqualified differencing across the
    # boundary would report a delta of about minus one track length.
    naive = []
    for _host_s, row in rows:
        pa, pb = row.get("A"), row.get("B")
        if pa is HOLE or pb is HOLE:
            continue
        naive.append((pa[1] - pb[1]) - (pa[0] - pb[0]) * 1000.0)
    if abs(min(naive)) < 1000:
        failures.append("selftest fixture does not exercise the boundary artefact")

    spans = straddle(rows, ["A", "B"])
    if len(spans) != 1 or abs(spans[0][2] - 1.0) > 1e-6:
        failures.append("straddle %r, want one span of 1.000 s" % (spans,))

    bounds = boundaries(rows, "A")
    if len(bounds) != 1 or bounds[0][1] != "t1" or bounds[0][2] != "t2":
        failures.append("boundaries %r, want one t1->t2" % (bounds,))
    if len(boundaries(rows, "B")) != 1:
        failures.append("B should also show exactly one boundary")

    print("  qualified samples   %d (straddle and hole excluded)" % len(deltas))
    print("  median Δ            %+.1f ms" % median)
    print(
        "  naive Δ at boundary %+.0f ms  <- the artefact, correctly excluded"
        % min(naive)
    )
    print("  straddle spans      %s" % [round(s[2], 3) for s in spans])
    print("  boundaries A        %s" % [(b[1], b[2]) for b in bounds])
    for line in failures:
        print("  FAIL: %s" % line)
    print("\n%s" % ("SELFTEST PASS" if not failures else "SELFTEST FAILED"))
    return 1 if failures else 0


def main(argv):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--member", action="append", default=[])
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--hz", type=float, default=4.0)
    ap.add_argument("--out", help="write raw rows here as JSON")
    ap.add_argument("scenario", nargs="?", default="sample")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if not args.member:
        ap.error("--member is required unless --selftest")

    members = [MusicMember(spec) for spec in args.member]
    log("members: %s" % ", ".join("%s (%s)" % (m.name, m.device_name) for m in members))
    for member in members:
        member.mark_log()

    rows = sample(members, args.seconds, args.hz)
    log("%d samples over %.0f s" % (len(rows), args.seconds))

    for name, (holes, total, pct) in hole_rate(members).items():
        log("  %s read holes %d/%d (%.1f%%)" % (name, holes, total, pct))

    lead = members[0]
    for member in members[1:]:
        describe(
            qualified_delta(rows, lead.name, member.name),
            "Δ %s−%s" % (lead.name, member.name),
        )

    spans = straddle(rows, [m.name for m in members])
    if spans:
        log(
            "straddle: %d spans, median %.2f s, max %.2f s"
            % (
                len(spans),
                statistics.median([s[2] for s in spans]),
                max(s[2] for s in spans),
            )
        )
    for member in members:
        marks = boundaries(rows, member.name)
        if marks:
            log(
                "  %s boundaries at %s"
                % (member.name, ", ".join("%.1f" % b[0] for b in marks))
            )

    if args.out:
        with open(args.out, "w") as handle:
            json.dump(
                [
                    (t, {k: (v if v is not HOLE else None) for k, v in r.items()})
                    for t, r in rows
                ],
                handle,
            )
        log("rows written to %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
