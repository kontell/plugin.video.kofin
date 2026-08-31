#!/usr/bin/env python3
"""Build the SyncPlay music shakedown asset (docs/syncplay-music-shakedown.md §5.4).

A 10-track synthetic album whose every property exists to make a measurement
possible. Committed because the numbers in `tests/live/results/music-*` are only
reproducible against the same asset; regenerating with the same --seed gives
byte-identical WAVs.

Three properties matter, and each is a measurement requirement, not taste:

* **No silence at either end of any track.** Leading silence is indistinguishable
  from a boundary gap in a capture, and TSG is the study's headline metric. Every
  track starts and ends at full programme level; the only concession is a 3 ms
  raised-cosine ramp to stop the edge being a DC step, which is two orders of
  magnitude below the 50 ms dropout threshold.

* **A marker train on a 1.000 s grid**, sample-exact from t=1.0 s. This is what
  cross-correlation locks onto, so inter-member delta and boundary instants can
  be read from audio alone without asking a player anything.

* **The marker is a chirp, not a click.** A 6 ms linear sweep has far better
  correlation properties than an impulse (pulse compression: the matched filter
  concentrates a spread-out waveform into one sharp peak), survives a lossy
  encode better, and is less audible per unit of correlation energy. Each track
  sweeps its own band, so a capture identifies *which track* is playing from the
  marker alone — which is what makes a boundary detectable without the player.

The programme bed is deliberately rhythmic. A steady offset between two members
is inaudible on a drone and a flam on a snare (§3), so an asset with no beat
would understate exactly the error this study exists to measure.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import wave

import numpy as np

RATE = 48000
CHIRP_MS = 6.0
CHIRP_PERIOD_S = 1.0
CHIRP_START_S = 1.0  # not t=0: keeps the marker clear of the track edge
EDGE_RAMP_MS = 3.0
PROGRAMME_RMS_DBFS = -18.0
CHIRP_PEAK_DBFS = -30.0  # verify_sync_asset.py measures what survives an encode
PERC_LOWPASS_HZ = 5000.0  # keeps the programme out of the marker's band

# Track k sweeps CHIRP_F0[k] -> CHIRP_F0[k] + CHIRP_SPAN, so a capture can tell
# tracks apart from the marker alone. The band sits ABOVE the programme: the
# percussion is broadband noise an order of magnitude louder than the marker, and
# at 2-6 kHz it swamped the matched filter entirely (measured: every window
# locked onto a drum hit instead of a chirp). The bed is low-passed at
# PERC_LOWPASS_HZ and the markers live above it, so the two never compete.
CHIRP_F0_BASE = 8000.0
CHIRP_F0_STEP = 600.0
CHIRP_SPAN = 2500.0

# Deliberately not all equal: a fixed period would let a boundary-detector that
# assumes one pass by accident.
DURATIONS_S = [168, 186, 174, 192, 171, 183, 165, 189, 177, 180]

MAJOR = [0, 2, 4, 5, 7, 9, 11]
ROOTS_HZ = [110.0 * 2 ** (n / 12.0) for n in (0, 3, 7, 5, 10, 2, 8, 1, 6, 4)]
BPMS = [96, 112, 88, 120, 104, 92, 116, 100, 108, 84]


def db(x):
    return 10.0 ** (x / 20.0)


def _adsr(n, attack, decay, rate=RATE):
    a = max(1, min(int(attack * rate), n))
    d = max(1, int(decay * rate))
    env = np.ones(n, dtype=np.float64)
    env[:a] = np.linspace(0.0, 1.0, a)
    tail = np.exp(-np.linspace(0.0, 5.0, min(d, n)))
    env[-len(tail) :] *= tail
    return env


def _lowpass(x, cutoff, rate=RATE):
    spec = np.fft.rfft(x)
    spec[np.fft.rfftfreq(len(x), 1.0 / rate) > cutoff] = 0.0
    return np.fft.irfft(spec, len(x))


def _sustain(n, attack, release, rate=RATE):
    """Attack, hold, release -- unlike _adsr, this never decays to nothing, so
    consecutive segments overlap into continuous programme."""
    env = np.ones(n, dtype=np.float64)
    a = max(1, min(int(attack * rate), n))
    r = max(1, min(int(release * rate), n))
    env[:a] = 0.5 * (1 - np.cos(np.linspace(0, np.pi, a)))
    env[-r:] *= 0.5 * (1 + np.cos(np.linspace(0, np.pi, r)))
    return env


def _saw(freq, n, rate=RATE, harmonics=8):
    t = np.arange(n) / float(rate)
    out = np.zeros(n)
    for h in range(1, harmonics + 1):
        out += np.sin(2 * np.pi * freq * h * t) / h
    return out / max(1e-9, np.abs(out).max())


def synth_bed(track, duration_s, rate=RATE):
    """A rhythmic bed: bass on the beat, a pad chord per bar, a noise-burst pulse.

    Every event is generated at its *natural* length and then cut by the track
    end, never squeezed into the room left. An envelope anchored to the end of a
    shortened segment plays its whole decay inside the stub, which is how the
    first build ended each track on a 300 ms fade to -50 dBFS -- indistinguishable
    from the boundary gap the asset exists to measure.

    Deterministic given (track, duration); the only randomness is the percussion
    noise, seeded per track.
    """
    n = int(round(duration_s * rate))
    rng = np.random.default_rng(1000 + track)
    root = ROOTS_HZ[track % len(ROOTS_HZ)]
    bpm = BPMS[track % len(BPMS)]
    beat = 60.0 / bpm
    # Build two beats past the end and cut, so the track ends mid-programme.
    n_ext = n + int(round(4 * beat * rate))
    out = np.zeros(n_ext)
    total_s = n_ext / float(rate)

    def place(start, wave, env):
        room = min(len(wave), n_ext - start)
        if room > 0:
            out[start : start + room] += wave[:room] * env[:room]

    # Bass: one note per beat, walking the triad.
    degrees = [0, 4, 2, 4]
    natural = int(beat * rate * 0.9)
    for i in range(int(math.ceil(total_s / beat))):
        start = int(i * beat * rate)
        if start >= n_ext:
            break
        semis = MAJOR[degrees[i % len(degrees)] % len(MAJOR)]
        f = root * 2 ** (semis / 12.0)
        place(start, 0.55 * _saw(f, natural), _adsr(natural, 0.005, beat * 0.8))

    # Pad: a triad held for a bar, moving every four bars.
    bar = beat * 4
    overlap = 0.15
    bar_n = int(bar * rate) + int(overlap * rate)
    t = np.arange(bar_n) / float(rate)
    for i in range(int(math.ceil(total_s / bar))):
        start = int(i * bar * rate)
        if start >= n_ext:
            break
        shift = MAJOR[(i // 4) % len(MAJOR)]
        chord = np.zeros(bar_n)
        for semis in (0, 4, 7):
            f = root * 4 * 2 ** ((semis + shift) / 12.0)
            chord += np.sin(2 * np.pi * f * t) + 0.3 * np.sin(4 * np.pi * f * t)
        place(start, 0.30 * chord / 3.0, _sustain(bar_n, overlap, overlap))

    # Percussion: the reason the asset has a beat at all (section 3). Band-limited
    # so it never competes with the marker.
    hit_n = int(0.045 * rate)
    for i in range(int(math.ceil(total_s / (beat * 0.5)))):
        start = int(i * beat * 0.5 * rate)
        if start >= n_ext:
            break
        hit = _lowpass(rng.standard_normal(hit_n), PERC_LOWPASS_HZ)
        hit *= np.exp(-np.linspace(0, 9, hit_n))
        place(start, (0.5 if i % 4 == 0 else 0.18) * hit, np.ones(hit_n))

    out = out[:n]
    rms = math.sqrt(float(np.mean(out**2))) or 1.0
    out *= db(PROGRAMME_RMS_DBFS) / rms

    ramp = int(EDGE_RAMP_MS * rate / 1000.0)
    w = 0.5 * (1 - np.cos(np.linspace(0, np.pi, ramp)))
    out[:ramp] *= w
    out[-ramp:] *= w[::-1]
    return out


def chirp_train(track, n, rate=RATE):
    """The marker train, sample-exact on a 1.000 s grid."""
    # Frequency offset alone is a weak discriminator: adjacent tracks share a
    # slope, and a 600 Hz shift still scored 0.69 of the correct peak. Alternate
    # the sweep direction as well -- an up-chirp and a down-chirp decorrelate
    # almost completely, so neighbours can never be confused.
    lo = CHIRP_F0_BASE + CHIRP_F0_STEP * track
    hi = lo + CHIRP_SPAN
    f0, f1 = (lo, hi) if track % 2 == 0 else (hi, lo)
    length = int(CHIRP_MS * rate / 1000.0)
    t = np.arange(length) / float(rate)
    dur = length / float(rate)
    sweep = np.sin(2 * np.pi * (f0 * t + (f1 - f0) * t * t / (2 * dur)))
    win = np.hanning(length)
    pulse = sweep * win * db(CHIRP_PEAK_DBFS)

    out = np.zeros(n)
    step = int(CHIRP_PERIOD_S * rate)
    for start in range(int(CHIRP_START_S * rate), n - length, step):
        out[start : start + length] += pulse
    return out, f0, f1


def build_track(track, out_dir, fmt, meta):
    duration = DURATIONS_S[track % len(DURATIONS_S)]
    bed = synth_bed(track, duration)
    markers, f0, f1 = chirp_train(track, len(bed))
    mono = bed + markers

    peak = float(np.abs(mono).max())
    if peak > 0.99:  # never clip: a limiter would move the edges
        mono *= 0.99 / peak
    stereo = np.stack([mono, mono], axis=1)
    pcm = np.clip(np.round(stereo * 32767.0), -32768, 32767).astype("<i2")

    base = "%02d %s" % (track + 1, meta["titles"][track])
    wav_path = os.path.join(out_dir, base + ".wav")
    with wave.open(wav_path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(pcm.tobytes())

    tags = {
        "album": meta["album"],
        "album_artist": meta["artist"],
        "artist": meta["artist"],
        "title": meta["titles"][track],
        "track": "%d/%d" % (track + 1, len(meta["titles"])),
        "date": meta["year"],
        "genre": "Test Signal",
        "comment": "chirp %.0f-%.0f Hz @ 1.000 s grid from %.3f s"
        % (f0, f1, CHIRP_START_S),
    }
    ext = {"flac": ".flac", "aac": ".m4a", "opus": ".opus"}[fmt]
    codec = {
        "flac": ["-c:a", "flac"],
        "aac": ["-c:a", "aac", "-b:a", "256k"],
        "opus": ["-c:a", "libopus", "-b:a", "128k"],
    }[fmt]
    final = os.path.join(out_dir, base + ext)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path] + codec
    for key, value in tags.items():
        cmd += ["-metadata", "%s=%s" % (key, value)]
    cmd.append(final)
    subprocess.run(cmd, check=True)
    os.unlink(wav_path)
    return {
        "track": track + 1,
        "file": os.path.basename(final),
        "duration_s": duration,
        "chirp_hz": [f0, f1],
        "bpm": BPMS[track % len(BPMS)],
    }


def main(argv):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", required=True, help="album directory to create")
    ap.add_argument("--format", default="flac", choices=("flac", "aac", "opus"))
    ap.add_argument("--tracks", type=int, default=10)
    ap.add_argument("--album", default="Kofin Sync Test Album")
    ap.add_argument("--artist", default="Kofin Test Signals")
    ap.add_argument("--year", default="2026")
    args = ap.parse_args(argv[1:])

    titles = ["Marker %02d" % (i + 1) for i in range(args.tracks)]
    meta = {
        "album": args.album,
        "artist": args.artist,
        "year": args.year,
        "titles": titles,
    }
    os.makedirs(args.out, exist_ok=True)

    rows = []
    for track in range(args.tracks):
        row = build_track(track, args.out, args.format, meta)
        rows.append(row)
        print("  %(file)s  %(duration_s)ds  %(bpm)d bpm  chirp %(chirp_hz)s" % row)

    manifest = {
        "album": args.album,
        "artist": args.artist,
        "rate": RATE,
        "chirp_period_s": CHIRP_PERIOD_S,
        "chirp_start_s": CHIRP_START_S,
        "chirp_ms": CHIRP_MS,
        "chirp_peak_dbfs": CHIRP_PEAK_DBFS,
        "programme_rms_dbfs": PROGRAMME_RMS_DBFS,
        "edge_ramp_ms": EDGE_RAMP_MS,
        "format": args.format,
        "tracks": rows,
    }
    with open(os.path.join(args.out, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, indent=2)
    print("manifest: %s" % os.path.join(args.out, "manifest.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
