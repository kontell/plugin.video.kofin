"""Jellyfin ↔ Kodi stream index maps for progress reporting (PR2).

Play resolve stores provisional maps on the play-state queue. After
``onAVStarted`` the service reconciles absolute Kodi subtitle indexes for
external ``setSubtitles`` tracks (embedded demux order + attachment offset or
basename match). Observation then converts the player's current stream
indexes back to Jellyfin MediaStream indexes for session progress.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

JsonDict = Dict[str, Any]
IntMap = Dict[int, int]


def _stream_index(stream: Mapping[str, Any], default: int = 0) -> int:
    raw = stream.get("Index")
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _sorted_by_index(streams: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    return sorted(streams, key=lambda s: _stream_index(s, 0))


def audio_map(streams: Sequence[Mapping[str, Any]]) -> IntMap:
    """Jellyfin audio Index → Kodi demux audio index (0..n-1)."""
    audio = _sorted_by_index(s for s in streams if s.get("Type") == "Audio")
    return {_stream_index(s): i for i, s in enumerate(audio)}


def embedded_subtitle_map(streams: Sequence[Mapping[str, Any]]) -> IntMap:
    """Jellyfin subtitle Index → provisional Kodi index for demuxed (non-External) tracks.

    External-delivery tracks are attached via ``setSubtitles`` and live in
    ``SubsAttachOrder`` / absolute ``SubsMapping`` after reconcile — not here.
    """
    embedded = _sorted_by_index(
        s
        for s in streams
        if s.get("Type") == "Subtitle" and s.get("DeliveryMethod") != "External"
    )
    return {_stream_index(s): i for i, s in enumerate(embedded)}


def reverse_map(fwd: Mapping[Any, Any]) -> IntMap:
    """Invert an int→int map; last key wins on collision."""
    out: IntMap = {}
    for key, value in int_map(fwd).items():
        out[int(value)] = int(key)
    return out


def int_map(raw: Any) -> IntMap:
    """Coerce play-state / JSON maps (string keys) to ``dict[int, int]``."""
    if not raw:
        return {}
    out: IntMap = {}
    if isinstance(raw, Mapping):
        items = raw.items()
    else:
        return {}
    for key, value in items:
        try:
            out[int(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def stringify_map(fwd: Mapping[int, int]) -> Dict[str, int]:
    """JSON-queue-safe map (string keys)."""
    return {str(k): int(v) for k, v in fwd.items()}


def provisional_external_offset_map(
    attach_order_jf: Sequence[int], embedded_count: int
) -> IntMap:
    """absolute Kodi index → Jellyfin index assuming externals follow embedded."""
    return {
        int(embedded_count) + i: int(jf_index)
        for i, jf_index in enumerate(attach_order_jf)
    }


def match_external_by_basename(
    attach_order_jf: Sequence[int],
    subs_paths: Sequence[str],
    kodi_sub_names: Sequence[str],
) -> IntMap:
    """absolute Kodi index → Jellyfin index via basename / path match.

    Kodi stream names for ``setSubtitles`` files vary by build:
    - full basename (``00.eng.srt``)
    - stem / index token with an External marker (``00 (External)`` on Omega)
    """
    mapping: IntMap = {}
    used_attach: set = set()
    basenames = [os.path.basename(p or "") for p in subs_paths]

    def _tokens(base: str) -> Tuple[str, str, str]:
        base_l = base.lower()
        stem = base_l.rsplit(".", 1)[0] if base_l else ""
        index_token = stem.split(".", 1)[0] if stem else ""
        return base_l, stem, index_token

    for kodi_i, name in enumerate(kodi_sub_names):
        name_l = (name or "").lower().strip()
        if not name_l:
            continue
        for attach_i, base in enumerate(basenames):
            if attach_i in used_attach or not base:
                continue
            base_l, stem, index_token = _tokens(base)
            if not base_l:
                continue
            matched = (
                name_l == base_l
                or name_l == stem
                or name_l.endswith(base_l)
                or base_l in name_l
                or name_l in base_l
                or (stem and (name_l == stem or name_l.startswith(stem + " ")))
                or (
                    index_token
                    and (
                        name_l == index_token
                        or name_l.startswith(index_token + " ")
                        or name_l.startswith(index_token + "(")
                        or (
                            "external" in name_l
                            and (
                                name_l.startswith(index_token)
                                or f" {index_token} " in f" {name_l} "
                            )
                        )
                    )
                )
            )
            if matched and attach_i < len(attach_order_jf):
                mapping[kodi_i] = int(attach_order_jf[attach_i])
                used_attach.add(attach_i)
                break

    # Omega often lists setSubtitles tracks first as "NN (External)" without
    # the full basename. Map remaining External-tagged slots in list order.
    if len(used_attach) < len(attach_order_jf):
        external_slots = [
            i
            for i, name in enumerate(kodi_sub_names)
            if i not in mapping and "external" in (name or "").lower()
        ]
        pending = [
            jf
            for attach_i, jf in enumerate(attach_order_jf)
            if attach_i not in used_attach
        ]
        for slot, jf in zip(external_slots, pending):
            mapping[slot] = int(jf)
    return mapping


def reconcile_subs_mapping(
    *,
    attach_order_jf: Sequence[int],
    subs_paths: Sequence[str],
    kodi_sub_names: Sequence[str],
    embedded_map_jf_to_kodi: Mapping[Any, Any],
) -> Tuple[IntMap, bool]:
    """Build absolute Kodi→Jellyfin subtitle map after streams exist.

    Returns ``(mapping, ready)``. ``ready`` is True when every attached
    external (if any) has an absolute key, or when there are no externals
    (embedded-only / no-sub sessions).

    Externals may list **before** demuxed tracks (Omega: ``00 (External)`` at
    index 0). Provisional EmbeddedSubMap kodi indexes are only ordering among
    demuxed tracks — they are re-seated onto absolute slots left after
    externals are placed.
    """
    emb = int_map(embedded_map_jf_to_kodi)
    attach_order = [int(x) for x in attach_order_jf]
    emb_jf_order = sorted(emb.keys(), key=lambda jf: emb[jf])

    if not attach_order:
        # Demux-only: provisional kodi index == absolute.
        return reverse_map(emb), True

    if not kodi_sub_names:
        # Streams not visible yet — provisional demux-then-external layout.
        mapping = reverse_map(emb)
        mapping.update(provisional_external_offset_map(attach_order, len(emb)))
        return mapping, False

    external = match_external_by_basename(attach_order, subs_paths, kodi_sub_names)
    if len(external) < len(attach_order):
        # Last resort: classic layout (externals after all demuxed).
        by_offset = provisional_external_offset_map(attach_order, len(emb))
        claimed = set(external.values())
        for abs_k, jf in by_offset.items():
            if (
                abs_k not in external
                and abs_k < len(kodi_sub_names)
                and jf not in claimed
            ):
                external[abs_k] = jf
                claimed.add(jf)

    mapping: IntMap = dict(external)
    external_slots = set(external.keys())
    free_slots = [i for i in range(len(kodi_sub_names)) if i not in external_slots]
    for kodi_i, jf in zip(free_slots, emb_jf_order):
        mapping[kodi_i] = jf

    matched_external = set(external.values())
    ready = all(jf in matched_external for jf in attach_order)
    return mapping, ready


def stream_summaries(
    streams: Sequence[Mapping[str, Any]],
) -> Tuple[List[JsonDict], List[JsonDict]]:
    """Compact AudioStreams / SubtitleStreams lists for play state."""
    audio: List[JsonDict] = []
    subs: List[JsonDict] = []
    for stream in streams:
        kind = stream.get("Type")
        if kind == "Audio":
            audio.append(
                {
                    "Index": _stream_index(stream),
                    "Language": stream.get("Language") or "",
                    "DisplayTitle": stream.get("DisplayTitle") or "",
                    "Channels": stream.get("Channels"),
                    "Codec": stream.get("Codec") or "",
                }
            )
        elif kind == "Subtitle":
            subs.append(
                {
                    "Index": _stream_index(stream),
                    "Language": stream.get("Language") or "",
                    "DisplayTitle": stream.get("DisplayTitle") or "",
                    "IsText": bool(stream.get("IsTextSubtitleStream")),
                    "DeliveryMethod": stream.get("DeliveryMethod") or "",
                    "Codec": stream.get("Codec") or "",
                    "IsForced": bool(stream.get("IsForced")),
                }
            )
    return audio, subs


def play_state_stream_fields(
    source: Mapping[str, Any],
    subtitle_fields: Optional[Mapping[str, Any]] = None,
) -> JsonDict:
    """Maps + summaries merged into the play-state dict at resolve time."""
    streams = list(source.get("MediaStreams") or [])
    audio, subs = stream_summaries(streams)
    fields: JsonDict = {
        "AudioMap": stringify_map(audio_map(streams)),
        "EmbeddedSubMap": stringify_map(embedded_subtitle_map(streams)),
        "AudioStreams": audio,
        "SubtitleStreams": subs,
    }
    if subtitle_fields:
        fields.update(dict(subtitle_fields))
    else:
        fields.setdefault("SubsAttachOrder", [])
        fields.setdefault("SubsPaths", [])
        fields.setdefault("SubsMapping", {})
        fields.setdefault("SubsMappingReady", False)
    return fields


def observe_jellyfin_indexes(
    item: Mapping[str, Any],
    *,
    kodi_audio: Optional[int],
    kodi_sub: Optional[int],
    subtitle_enabled: bool,
) -> Tuple[Optional[int], Optional[int]]:
    """Map current Kodi player indexes to Jellyfin indexes for progress.

    When subtitle is disabled, returns ``SubtitleStreamIndex=None`` (server
    "off"). When ``SubsMappingReady`` is false, keeps the existing item
    subtitle index rather than guessing from a provisional external slot.
    """
    audio_jf = _optional_int(item.get("AudioStreamIndex"))
    if kodi_audio is not None:
        rev = reverse_map(item.get("AudioMap") or {})
        if kodi_audio in rev:
            audio_jf = rev[kodi_audio]

    sub_jf = _optional_int(item.get("SubtitleStreamIndex"))
    if not subtitle_enabled:
        return audio_jf, None

    if kodi_sub is None:
        return audio_jf, sub_jf

    if item.get("SubsMappingReady"):
        abs_map = int_map(item.get("SubsMapping") or {})
        if kodi_sub in abs_map:
            return audio_jf, abs_map[kodi_sub]
        # Ready but unknown slot (e.g. user picked a demux track only in map)
        emb_rev = reverse_map(item.get("EmbeddedSubMap") or {})
        if kodi_sub in emb_rev:
            return audio_jf, emb_rev[kodi_sub]
        return audio_jf, sub_jf

    # Mapping not ready: only resolve demuxed/embedded provisional indexes.
    emb_rev = reverse_map(item.get("EmbeddedSubMap") or {})
    if kodi_sub in emb_rev:
        return audio_jf, emb_rev[kodi_sub]
    return audio_jf, sub_jf
