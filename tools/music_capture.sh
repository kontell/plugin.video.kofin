#!/usr/bin/env bash
# Sample-accurate per-instance audio capture for the music shakedown (§6.1).
#
# Two Kodi instances share one host, so their outputs can be recorded against one
# clock: give each its own null sink, move its stream there, and record both
# monitors. The difference between the two files is then the true audible offset,
# including the sink latency no player reports.
#
# Streams are MOVED at runtime (pactl move-sink-input) rather than repointing
# Kodi's audiooutput.audiodevice setting. Moving needs no restart, no settings
# write on a box mid-experiment, and undoes itself exactly; a settings change
# would persist into the next run and silently become part of the rig.
#
#   music_capture.sh probe                     validate the rig, no Kodi needed
#   music_capture.sh streams                   list Kodi stream indices
#   music_capture.sh record <secs> <dir> <a> <b>   capture, streams named by the caller
#   music_capture.sh restore               undo a run killed before its trap ran
#
# module-stream-restore remembers per-application routing, so a run killed with
# SIGKILL can leave Kodi pointed at a null sink that no longer exists — silence
# on its next start, with nothing obviously wrong. "restore" is the way back.
#
# Licence: GPL-2.0-or-later
set -euo pipefail

SINK_A="${SINK_A:-kodi_a}"
SINK_B="${SINK_B:-kodi_b}"
RATE="${RATE:-48000}"
MODULES=()
MOVED=()

log() { printf '  %s\n' "$*" >&2; }

# Unloading a null sink while a Kodi stream is still attached to it destroys
# that stream's sink underneath Kodi, and its AudioEngine does NOT recover: the
# player reports a position that never advances and no audio reaches any device.
# Measured on P1D 2026-08-31 -- it wedged both instances and only a restart
# cleared it. So a module is unloaded ONLY once every stream is verified off it.
# A leftover null sink is a trivial mess; a wedged Kodi is not.
cleanup() {
    set +e
    local stuck=0
    for entry in ${MOVED[@]+"${MOVED[@]}"}; do
        local si="${entry%%:*}" home="${entry##*:}"
        if pactl move-sink-input "$si" "$home" 2>/dev/null; then
            log "restored stream $si -> $home"
        else
            log "WARNING: could not restore stream $si (gone?)"
        fi
    done
    sleep 1                       # let PipeWire settle before anything is removed
    for module in ${MODULES[@]+"${MODULES[@]}"}; do
        local sink_name
        sink_name=$(pactl -f json list modules 2>/dev/null | python3 -c '
import json, sys
want = sys.argv[1]
for m in json.load(sys.stdin):
    if str(m.get("index")) == want:
        print((m.get("argument") or "").split("sink_name=")[-1].split()[0] if "sink_name=" in (m.get("argument") or "") else "")
        break
' "$module")
        if [ -n "$sink_name" ] && pactl list short sink-inputs 2>/dev/null \
             | awk -v s="$sink_name" '$2==s{found=1} END{exit !found}'; then
            log "REFUSING to unload module $module: a stream is still on $sink_name"
            stuck=1
            continue
        fi
        pactl unload-module "$module" 2>/dev/null && log "unloaded module $module"
    done
    [ "$stuck" -eq 0 ] || log "run 'music_capture.sh restore' once Kodi is idle"
}
trap cleanup EXIT

make_sink() {
    local name=$1 id
    id=$(pactl load-module module-null-sink sink_name="$name" \
         sink_properties=device.description="$name")
    MODULES+=("$id")
    log "null sink $name (module $id)"
}

# Identifying a stream by PID does NOT work here: measured on P1D 2026-08-31,
# PipeWire's PulseAudio compatibility reports every Kodi stream as application
# name "Kodi" with application.process.id unset, and the client index a
# sink-input names does not resolve in the clients list. Both instances are
# therefore indistinguishable by property alone.
#
# Nor by appearance or cork state: an idle Kodi keeps its stream open and
# UNCORKED, feeding silence, so both exist and both look alive with nothing
# playing. And a Player.Stop/Open cycle tears the stream down and builds a new
# one with a NEW index, so an index learned while stopped is stale by the time
# playback starts -- the move then silently lands on a dead stream, audio keeps
# going to the speakers, and the null sinks stay SUSPENDED (a suspended monitor
# yields parec "Stream error: Timeout" and a zero-byte file).
#
# The working procedure, and the only one measured to work here: start playback
# FIRST, list the streams while they are live, move those, then identify which
# is which from the audio -- pause one instance briefly and see which capture
# goes quiet. Pause keeps the stream alive where stop does not.
kodi_streams() {
    pactl -f json list sink-inputs 2>/dev/null | python3 -c '
import json, sys
for si in json.load(sys.stdin):
    if (si.get("properties", {}).get("application.name") or "") == "Kodi":
        print(si["index"])
'
}

default_sink() { pactl get-default-sink; }

case "${1:-}" in
probe)
    # Validate the whole capture path without touching a running Kodi: play a
    # known file into a null sink and read it back off the monitor.
    asset="${2:-/media/bluecon/music-alt/Kofin Test Signals/Kofin Sync Test Album/01 Marker 01.flac}"
    [ -f "$asset" ] || { echo "probe needs the asset: $asset" >&2; exit 2; }
    make_sink "$SINK_A"
    out=$(mktemp -u /tmp/capture_probe_XXXX.raw)
    parec --device="${SINK_A}.monitor" --format=s16le --rate="$RATE" --channels=2 \
        > "$out" &
    rec=$!
    sleep 0.5
    ffmpeg -v error -i "$asset" -t 8 -f s16le -ar 48000 -ac 2 - \
        | paplay --device="$SINK_A" --raw --format=s16le --rate=48000 --channels=2
    sleep 0.5
    kill "$rec" 2>/dev/null || true
    wait "$rec" 2>/dev/null || true
    bytes=$(stat -c%s "$out")
    log "captured $bytes bytes ($(python3 -c "print('%.2f' % ($bytes/4/$RATE))") s)"
    echo "$out"
    ;;
streams)
    kodi_streams
    ;;
record)
    secs="${2:?usage: record <secs> <dir> <sink-input-a> <sink-input-b>}"
    dir="${3:?usage: record <secs> <dir> <sink-input-a> <sink-input-b>}"
    si_a="${4:?caller must identify the streams: see 'streams'}"
    si_b="${5:?caller must identify the streams: see 'streams'}"
    mkdir -p "$dir"
    make_sink "$SINK_A"; make_sink "$SINK_B"
    home=$(default_sink)
    pactl move-sink-input "$si_a" "$SINK_A"; MOVED+=("$si_a:$home")
    pactl move-sink-input "$si_b" "$SINK_B"; MOVED+=("$si_b:$home")
    log "stream $si_a -> $SINK_A ; stream $si_b -> $SINK_B"
    parec --device="${SINK_A}.monitor" --format=s16le --rate="$RATE" --channels=2 \
        > "$dir/a.raw" & pa=$!
    parec --device="${SINK_B}.monitor" --format=s16le --rate="$RATE" --channels=2 \
        > "$dir/b.raw" & pb=$!
    log "recording ${secs}s into $dir"
    # A suspended sink yields a zero-byte file and no error until the end, so
    # fail early and loudly rather than after the whole window.
    sleep 3
    for f in "$dir/a.raw" "$dir/b.raw"; do
        [ "$(stat -c%s "$f")" -gt 0 ] || {
            echo "$f is still empty after 3 s: the sink is suspended, which means"\
                 "nothing was moved into it (stale stream index?)" >&2
            kill "$pa" "$pb" 2>/dev/null; exit 1; }
    done
    log "both sinks are receiving audio"
    sleep "$((secs - 3))"
    kill "$pa" "$pb" 2>/dev/null || true
    wait "$pa" "$pb" 2>/dev/null || true
    ls -l "$dir"/*.raw >&2
    ;;
restore)
    home=$(default_sink)
    for si in $(kodi_streams); do
        pactl move-sink-input "$si" "$home" && log "stream $si -> $home"
    done
    for module in $(pactl list short modules | awk '/module-null-sink/ && /kodi_/ {print $1}'); do
        pactl unload-module "$module" && log "unloaded stale null sink $module"
    done
    ;;
*)
    sed -n '2,24p' "$0" >&2
    exit 2
    ;;
esac
