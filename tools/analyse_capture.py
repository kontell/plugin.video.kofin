#!/usr/bin/env python3
"""Offline analysis of a two-member audio capture (docs/syncplay-music-shakedown.md §6.1).

Turns two recordings of two Kodi instances on one host into the §6.4 metrics that
JSON-RPC cannot reach: **Δ** including the sink latency a player never reports,
**TSG** (the silence at a track boundary) to the sample, and **dropouts** inside a
track. Both captures come off one clock, so their difference is the true audible
offset rather than two independent estimates of it.

Δ is measured by cross-correlating the two captures directly rather than by
matching markers between them. Direct correlation needs no track identity and
works through the boundary; where it fails — the members are on different tracks,
or one is silent — the peak correlation collapses, and that is reported as *not
comparable* instead of being quietly turned into a number. A window that cannot
be measured must look different from a window measured as zero.

    tools/analyse_capture.py --selftest
    tools/analyse_capture.py --a omega.raw --b piers.raw --rate 48000
"""

import argparse
import os
import subprocess
import sys

import numpy as np

RATE = 48000
WINDOW_S = 5.0
SILENCE_DBFS = -45.0  # asset floor is -19 dBFS/10 ms, so this is wide
MIN_CORRELATION = 0.30  # below this a window is not comparable


def load(path, rate=RATE):
    """Raw s16le stereo, or anything ffmpeg can open, as mono float."""
    if path.endswith(".raw"):
        data = np.fromfile(path, dtype="<i2").astype(np.float64) / 32768.0
        return data.reshape(-1, 2).mean(axis=1) if data.size % 2 == 0 else data
    out = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            str(rate),
            "-",
        ],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    return np.frombuffer(out, dtype="<f4").astype(np.float64)


def _norm_xcorr(a, b):
    """Normalised cross-correlation.

    Returns (lead_samples, coefficient) where **positive means a leads b** — the
    convention the whole report uses. The raw correlation peak is at the lag that
    aligns a onto b, which is the negative of that, so it is flipped here rather
    than at each call site.
    """
    a = a - a.mean()
    b = b - b.mean()
    denom = np.sqrt(np.dot(a, a) * np.dot(b, b))
    if denom <= 0:
        return 0.0, 0.0
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    corr = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    corr = np.concatenate([corr[-(len(b) - 1) :], corr[: len(a)]]) / denom
    idx = int(np.argmax(np.abs(corr)))
    if 0 < idx < len(corr) - 1:
        y0, y1, y2 = corr[idx - 1], corr[idx], corr[idx + 1]
        denom2 = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / denom2 if denom2 else 0.0
    else:
        frac = 0.0
    return (len(b) - 1) - (idx + frac), float(abs(corr[idx]))


def delta_series(a, b, rate=RATE, window_s=WINDOW_S):
    """[(t_s, delta_ms, coefficient, comparable)] — a ahead is positive."""
    step = int(window_s * rate)
    out = []
    for start in range(0, min(len(a), len(b)) - step, step):
        lag, coeff = _norm_xcorr(a[start : start + step], b[start : start + step])
        out.append(
            (start / float(rate), lag / rate * 1000.0, coeff, coeff >= MIN_CORRELATION)
        )
    return out


def silence_runs(sig, rate=RATE, block_ms=10.0, floor_dbfs=SILENCE_DBFS):
    """[(start_s, duration_ms)] for every run quieter than ``floor_dbfs``."""
    blk = int(block_ms * rate / 1000.0)
    nb = len(sig) // blk
    rms = np.sqrt((sig[: nb * blk].reshape(nb, blk) ** 2).mean(axis=1))
    quiet = rms < 10 ** (floor_dbfs / 20.0)
    runs, start = [], None
    for i, q in enumerate(quiet):
        if q and start is None:
            start = i
        elif not q and start is not None:
            runs.append((start * block_ms / 1000.0, (i - start) * block_ms))
            start = None
    if start is not None:
        runs.append((start * block_ms / 1000.0, (nb - start) * block_ms))
    return runs


def report(a, b, rate=RATE):
    print("== Δ (a − b), %.0f s windows ==" % WINDOW_S)
    series = delta_series(a, b, rate)
    good = [d for d in series if d[3]]
    skipped = len(series) - len(good)
    if good:
        mags = sorted(abs(d[1]) for d in good)
        print(
            "   windows %d comparable, %d not (straddle or silence)"
            % (len(good), skipped)
        )
        print(
            "   median %+.1f ms   p95 |Δ| %.1f ms   max |Δ| %.1f ms"
            % (
                float(np.median([d[1] for d in good])),
                mags[int(0.95 * (len(mags) - 1))],
                mags[-1],
            )
        )
        print("   median correlation %.3f" % float(np.median([d[2] for d in good])))
    else:
        print("   no comparable windows")

    for name, sig in (("a", a), ("b", b)):
        runs = silence_runs(sig, rate)
        long_runs = [r for r in runs if r[1] >= 50.0]
        print("\n== %s: silence ==" % name)
        print("   runs >=50 ms: %d" % len(long_runs))
        for start, dur in long_runs[:12]:
            print("      %8.2f s   %7.1f ms" % (start, dur))
    return series


def _selftest():
    """Against the real asset: a known offset and a known gap, both recovered."""
    album = "/media/bluecon/music-alt/Kofin Test Signals/Kofin Sync Test Album"
    track = os.path.join(album, "01 Marker 01.flac")
    if not os.path.exists(track):
        print("  asset not found at %s" % album)
        return 1

    sig = load(track)[: 60 * RATE]
    offset_ms = 37.5
    offset = int(offset_ms * RATE / 1000.0)
    a = sig[offset:]
    b = sig[: len(a)]  # a leads b by offset_ms

    failures = []
    good = [d for d in delta_series(a, b) if d[3]]
    median = float(np.median([d[1] for d in good]))
    print(
        "  injected Δ %+.1f ms -> recovered %+.3f ms (error %.3f ms) over %d windows"
        % (offset_ms, median, abs(median - offset_ms), len(good))
    )
    if abs(median - offset_ms) > 1.0:
        failures.append(
            "Δ recovery error %.3f ms exceeds 1 ms" % abs(median - offset_ms)
        )

    # A 420 ms gap punched into b is a track boundary; it must be found to the
    # block, and it must NOT be reported as a delta.
    gapped = b.copy()
    gap_at, gap_ms = 20 * RATE, 420.0
    gapped[gap_at : gap_at + int(gap_ms * RATE / 1000.0)] = 0.0
    runs = [r for r in silence_runs(gapped) if r[1] >= 50.0]
    print(
        "  injected gap %.0f ms at %.1f s -> found %s"
        % (
            gap_ms,
            gap_at / RATE,
            ", ".join("%.0f ms at %.2f s" % (d, s) for s, d in runs) or "nothing",
        )
    )
    if len(runs) != 1 or abs(runs[0][1] - gap_ms) > 20:
        failures.append("gap not recovered within a block: %r" % (runs,))

    # A window where the two are on different tracks must be refused, not scored.
    other = load(os.path.join(album, "04 Marker 04.flac"))[: 30 * RATE]
    mixed = delta_series(a[: 30 * RATE], other)
    comparable = [d for d in mixed if d[3]]
    print(
        "  different tracks -> %d/%d windows comparable (want 0)"
        % (len(comparable), len(mixed))
    )
    if comparable:
        failures.append("different tracks scored as comparable")

    for line in failures:
        print("  FAIL: %s" % line)
    print("\n%s" % ("SELFTEST PASS" if not failures else "SELFTEST FAILED"))
    return 1 if failures else 0


def main(argv):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--a")
    ap.add_argument("--b")
    ap.add_argument("--rate", type=int, default=RATE)
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.a or not args.b:
        ap.error("--a and --b are required unless --selftest")
    report(load(args.a, args.rate), load(args.b, args.rate), args.rate)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
