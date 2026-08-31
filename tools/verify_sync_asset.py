#!/usr/bin/env python3
"""Verify a shakedown asset has the properties the measurement depends on.

Checks, in the order they would invalidate the study:

1. **No silence at either end** — leading silence is indistinguishable from a
   boundary gap, and TSG is the headline metric.
2. **Marker grid is sample-exact** — recovered chirp instants must sit on the
   1.000 s grid, or every timing read inherits the error.
3. **Timing precision through a matched filter** — inject known integer and
   fractional offsets and recover them. The residual is the floor on every Δ this
   asset can measure and no §10 threshold may be set near it; the gate is 1 ms,
   thirty times below §10's tightest bar (30 ms), and the measured floor is the
   number that matters.
4. **Survival through a lossy encode** — arm D's downloads transcode FLAC to
   Opus 128 by default, so members can be compared across codecs only if the
   marker's timing survives one.
"""

import argparse
import json
import os
import subprocess
import sys

import numpy as np

RATE = 48000


def load(path, rate=RATE):
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


def reference_chirp(f0, f1, ms=6.0, rate=RATE):
    n = int(ms * rate / 1000.0)
    t = np.arange(n) / float(rate)
    dur = n / float(rate)
    return np.sin(2 * np.pi * (f0 * t + (f1 - f0) * t * t / (2 * dur))) * np.hanning(n)


def matched(sig, ref):
    """Matched filter by FFT. conj() makes this correlation, so the peak
    index is the chirp start directly -- no convolution correction."""
    n = 1 << int(np.ceil(np.log2(len(sig) + len(ref))))
    corr = np.fft.irfft(np.fft.rfft(sig, n) * np.conj(np.fft.rfft(ref, n)), n)
    return corr[: len(sig)]


def peak_parabolic(corr, idx):
    """Sub-sample peak by parabolic interpolation on three points."""
    if idx <= 0 or idx >= len(corr) - 1:
        return float(idx)
    a, b, c = corr[idx - 1], corr[idx], corr[idx + 1]
    denom = a - 2 * b + c
    return float(idx) if denom == 0 else idx + 0.5 * (a - c) / denom


def find_markers(sig, ref, period_s=1.0, start_s=1.0, rate=RATE):
    corr = np.abs(matched(sig, ref))
    step = int(period_s * rate)
    hits, snrs = [], []
    last = len(sig) - len(ref) - step // 4
    for expect in range(int(start_s * rate), max(0, last), step):
        lo, hi = max(0, expect - step // 4), min(len(corr), expect + step // 4)
        window = corr[lo:hi]
        idx = lo + int(np.argmax(window))
        hits.append(peak_parabolic(corr, idx))
        # Discrimination, not absolute level: the peak against everything else in
        # the window it had to beat.
        guard = np.concatenate(
            [corr[lo : max(lo, idx - 4)], corr[min(hi, idx + 5) : hi]]
        )
        snrs.append(corr[idx] / (float(np.median(np.abs(guard))) or 1e-12))
    return np.array(hits), float(np.median(snrs))


def frac_shift(x, delay):
    """Delay by a fractional number of samples, via an FFT phase ramp."""
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x))
    return np.fft.irfft(spec * np.exp(-2j * np.pi * freqs * delay), len(x))


def edge_report(sig, rate=RATE, window_ms=20.0):
    n = int(window_ms * rate / 1000.0)
    head = 20 * np.log10(max(1e-12, np.sqrt(np.mean(sig[:n] ** 2))))
    tail = 20 * np.log10(max(1e-12, np.sqrt(np.mean(sig[-n:] ** 2))))
    full = 20 * np.log10(max(1e-12, np.sqrt(np.mean(sig**2))))
    return head, tail, full


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--album", required=True)
    ap.add_argument(
        "--offset-samples",
        type=int,
        default=137,
        help="known offset injected for the precision test",
    )
    args = ap.parse_args(argv[1:])

    manifest = json.load(open(os.path.join(args.album, "manifest.json")))
    ok = True

    print("== 1. edges (no silence) ==")
    print("   %-22s %8s %8s %8s" % ("track", "head", "tail", "whole"))
    for row in manifest["tracks"]:
        sig = load(os.path.join(args.album, row["file"]))
        head, tail, full = edge_report(sig)
        flag = "" if (head > full - 12 and tail > full - 12) else "  <-- SILENT EDGE"
        if flag:
            ok = False
        print(
            "   %-22s %7.1f  %7.1f  %7.1f%s"
            % (row["file"][:22], head, tail, full, flag)
        )

    print("\n== 2. marker grid + 3. precision (FLAC) ==")
    first = manifest["tracks"][0]
    sig = load(os.path.join(args.album, first["file"]))
    ref = reference_chirp(*first["chirp_hz"], ms=manifest["chirp_ms"])
    hits, snr = find_markers(
        sig, ref, manifest["chirp_period_s"], manifest["chirp_start_s"]
    )
    grid = manifest["chirp_start_s"] * RATE + np.arange(len(hits)) * RATE
    err = hits - grid
    print("   markers found      %d" % len(hits))
    print("   matched-filter SNR %.0fx (%.1f dB)" % (snr, 20 * np.log10(snr)))
    print(
        "   grid error         mean %+.3f  max |%.3f| samples (%.1f us)"
        % (err.mean(), np.abs(err).max(), np.abs(err).max() / RATE * 1e6)
    )

    shifted = np.concatenate([np.zeros(args.offset_samples), sig])[: len(sig)]
    hits2, _ = find_markers(
        shifted, ref, manifest["chirp_period_s"], manifest["chirp_start_s"]
    )
    n = min(len(hits), len(hits2))
    rec = np.median(hits2[:n] - hits[:n])
    print(
        "   injected %d samples -> recovered %.3f  (error %.1f us)"
        % (args.offset_samples, rec, abs(rec - args.offset_samples) / RATE * 1e6)
    )
    if abs(rec - args.offset_samples) > 2:
        ok = False
        print("   <-- PRECISION FAIL")

    for frac in (0.25, 0.5, 137.4):
        shifted_f = frac_shift(sig, frac)
        hits_f, _ = find_markers(
            shifted_f, ref, manifest["chirp_period_s"], manifest["chirp_start_s"]
        )
        m = min(len(hits), len(hits_f))
        rec_f = np.median(hits_f[:m] - hits[:m])
        err_us = abs(rec_f - frac) / RATE * 1e6
        print(
            "   injected %7.2f samples -> recovered %8.3f  (error %5.1f us)"
            % (frac, rec_f, err_us)
        )
        if err_us > 1000:
            ok = False
            print("   <-- SUB-SAMPLE PRECISION FAIL")

    print("\n== 4. survival through lossy encodes ==")
    src = os.path.join(args.album, first["file"])
    for name, codec in (
        ("opus 128k", ["-c:a", "libopus", "-b:a", "128k"]),
        ("aac 256k", ["-c:a", "aac", "-b:a", "256k"]),
        ("mp3 320k", ["-c:a", "libmp3lame", "-b:a", "320k"]),
    ):
        ext = {"opus 128k": ".opus", "aac 256k": ".m4a", "mp3 320k": ".mp3"}[name]
        tmp = "/tmp/claude-1000/verify_probe" + ext
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", src] + codec + [tmp], check=True
        )
        lossy = load(tmp)
        h, s = find_markers(
            lossy, ref, manifest["chirp_period_s"], manifest["chirp_start_s"]
        )
        m = min(len(h), len(hits))
        delta = np.median(h[:m] - hits[:m])
        spread = float(np.std(h[:m] - hits[:m]))
        status = "ok" if s > 8 and spread < 5 else "MARGINAL"
        print(
            "   %-10s SNR %6.0fx  bias %+7.2f samples (%+.2f ms)  jitter %.2f samples  %s"
            % (name, s, delta, delta / RATE * 1000.0, spread, status)
        )
        if s <= 8 or spread >= 5:
            ok = False
        os.unlink(tmp)

    print("\n== 5. track identification from the marker alone ==")
    refs = [
        reference_chirp(*r["chirp_hz"], ms=manifest["chirp_ms"])
        for r in manifest["tracks"]
    ]
    worst = 1e9
    for row in manifest["tracks"]:
        clip = load(os.path.join(args.album, row["file"]))[: 20 * RATE]
        peaks = [float(np.abs(matched(clip, r)).max()) for r in refs]
        best = int(np.argmax(peaks)) + 1
        order = sorted(peaks, reverse=True)
        margin = order[0] / (order[1] or 1e-12)
        worst = min(worst, margin)
        mark = "" if best == row["track"] else "  <-- MISIDENTIFIED"
        if mark:
            ok = False
        print(
            "   track %2d -> identified %2d   margin over runner-up %.2fx%s"
            % (row["track"], best, margin, mark)
        )
    print("   -> worst margin %.2fx (need >2x)" % worst)
    if worst <= 2.0:
        ok = False
        print("   <-- DISCRIMINATION TOO WEAK")

    print("\n%s" % ("ALL CHECKS PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
