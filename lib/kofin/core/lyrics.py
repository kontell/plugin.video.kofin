"""Rendering Jellyfin's lyric payload into what Kodi lyrics addons expect.

Jellyfin returns lyrics as a list of lines, each optionally carrying a start
time in .NET ticks (100 ns units)::

    {"Metadata": {}, "Lyrics": [{"Text": "Tonight", "Start": 5800000}]}

``Metadata.IsSynced`` looks like the field to branch on but is never set by
Jellyfin core — every response for a locally stored lyric carries an empty
``Metadata``. Whether a lyric is timed is decided by the presence of ``Start``
on the first line, which is what jellyfin-web does too.

Timed lyrics are rendered as LRC (``[mm:ss.xx]Text``) because that is the only
form script.cu.lrclyrics accepts ahead of its own online scrapers: it sniffs
for a ``[mm:ss]`` stamp and runs the matching pass before every scraper, while
the plain-text pass runs after all of them.
"""

from typing import Any, Dict, List, Optional, Tuple

JsonDict = Dict[str, Any]

# One rendered line: its start time in seconds, or None when the payload
# carries no timings at all (or this particular line was left unstamped).
LyricLine = Tuple[Optional[float], str]

TICKS_PER_SECOND = 10_000_000

# Jellyfin timestamps are .NET ticks; one centisecond is 100_000 of them.
# Rounding to centiseconds *before* splitting into minutes and seconds keeps
# "[00:60.00]" from ever being emitted for a time that rounds up to a minute.
TICKS_PER_CENTISECOND = 100_000
CENTISECONDS_PER_MINUTE = 6000


def _stamp(start: int) -> str:
    centiseconds = max(0, int(round(start / TICKS_PER_CENTISECOND)))
    minutes, remainder = divmod(centiseconds, CENTISECONDS_PER_MINUTE)
    return "[%02d:%05.2f]" % (minutes, remainder / 100.0)


def is_synced(payload: Optional[JsonDict]) -> bool:
    """Whether the payload carries line timings.

    Decided on the first line alone: Jellyfin sorts timed lyrics by start time,
    so a timed set always stamps line zero. A lyric file that failed LRC
    parsing falls back to the plain-text parser server-side and arrives with no
    stamps at all, which lands here as False.
    """
    lines = (payload or {}).get("Lyrics") or []
    return bool(lines) and lines[0].get("Start") is not None


def to_text(payload: Optional[JsonDict]) -> Optional[str]:
    """The payload as lyric text, or None when there is nothing to show.

    Timed payloads come back as LRC, untimed ones as plain lines. A line
    without a stamp inside an otherwise timed payload is emitted bare rather
    than guessed at — lrclyrics only needs one stamp to treat the whole body
    as timed.
    """
    lines: List[JsonDict] = (payload or {}).get("Lyrics") or []
    if not lines:
        return None

    timed = is_synced(payload)
    rendered: List[str] = []
    for line in lines:
        text = line.get("Text") or ""
        start = line.get("Start")
        if timed and start is not None:
            rendered.append("%s%s" % (_stamp(int(start)), text))
        else:
            rendered.append(text)

    body = "\n".join(rendered)
    return body if body.strip() else None


def to_lines(payload: Optional[JsonDict]) -> List[LyricLine]:
    """The payload as ``(start_seconds, text)`` pairs for the skin overlay.

    The counterpart to :func:`to_text`: that renders LRC for a lyrics addon to
    re-parse, this keeps the timings we already have so nothing has to parse
    a timestamp back out of a string we just formatted.
    """
    lines: List[JsonDict] = (payload or {}).get("Lyrics") or []
    if not lines:
        return []

    timed = is_synced(payload)
    out: List[LyricLine] = []
    for line in lines:
        text = line.get("Text") or ""
        start = line.get("Start")
        if timed and start is not None:
            out.append((max(0.0, int(start) / TICKS_PER_SECOND), text))
        else:
            out.append((None, text))
    return out
