"""Media-segment parsing: /MediaSegments body -> [{Type, Start, End}].

Split out of ``service/segments.py`` (P1.5) because the plugin play route
prefetches segments too (``plugin/play.py::prefetch_segments``), and a pure
parser has no business living behind the service package. The checker that
*acts* on segments stays there.
"""

from typing import Any, Dict, List, Optional

from kofin.core.log import Logger

LOG = Logger(__name__)

# Overlap two segments of one type must exceed before the later one is taken
# for a second provider's opinion rather than a genuinely separate break. A
# Recap that ends exactly where the Intro begins is not a conflict.
OVERLAP_TOLERANCE = 1.0

# Jellyfin MediaSegmentType -> the per-type identity used by settings and
# labels (fork naming kept: Introduction/Credits/Recap/Preview/Commercial).
SEGMENT_TYPES = {
    "Intro": "Introduction",
    "Outro": "Credits",
    "Recap": "Recap",
    "Preview": "Preview",
    "Commercial": "Commercial",
}


def parse_segments(response: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sorted, de-conflicted ``[{Type, Start, End}]`` (seconds) from a
    /MediaSegments body."""
    segments: List[Dict[str, Any]] = []
    for item in (response or {}).get("Items") or []:
        segment_type = SEGMENT_TYPES.get(item.get("Type", ""))
        if not segment_type:
            continue
        start = float(item.get("StartTicks") or 0) / 10_000_000
        end = float(item.get("EndTicks") or 0) / 10_000_000
        if end <= start:
            continue
        segments.append({"Type": segment_type, "Start": start, "End": end})
    segments.sort(key=lambda segment: float(segment["Start"]))
    return deconflict(segments)


def deconflict(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One segment per overlapping run within a type — the two-provider fix.

    Two segment providers analysing the same library (Intro Skipper and the
    Chapter Segments plugin is the classic pair) each write their own rows and
    the server hands back the union: ``MediaSegmentManager.GetSegmentsAsync``
    filters by item, type and enabled-provider, then orders by start and stops
    there. ``MediaSegmentProviderOrder`` cannot help — the server consults it
    only at *analysis* time, to order which provider runs first, and
    ``MediaSegmentDto`` drops ``SegmentProviderId`` on the way out, so no
    client can attribute a segment to a provider or rank one over another.
    De-confliction is therefore on the times alone.

    First-by-start wins its run, rather than the union of it: over-skipping
    into content the viewer wanted is the worse failure, and the
    earliest-starting candidate is also the conservative one on the end.
    Within-type only, and only on real overlap, so three commercial breaks all
    survive and an abutting Recap/Intro pair is left alone.

    ``segments`` must already be sorted by start.
    """
    kept: List[Dict[str, Any]] = []
    run_end: Dict[str, float] = {}
    dropped = 0
    for segment in segments:
        segment_type = str(segment["Type"])
        previous = run_end.get(segment_type)
        if (
            previous is not None
            and float(segment["Start"]) < previous - OVERLAP_TOLERANCE
        ):
            dropped += 1
            continue
        kept.append(segment)
        run_end[segment_type] = float(segment["End"])
    if dropped:
        LOG.info(
            "dropped %d overlapping segment(s) — more than one provider is "
            "analysing this library",
            dropped,
        )
    return kept
