"""External text subtitle attach: eligibility, labels, download, cache.

PR1 of the transcoding stream-selection design. Text tracks with
DeliveryMethod=External are materialised under addon_data with
``{Index:02d}.{lang}.{codec}`` basenames so Kodi's native subtitle dialog
shows a language code instead of a Jellyfin URL. Image-based formats are
never downloaded or attached here.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import xbmcvfs

from kofin.core.log import Logger

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# Canonical file extensions Kodi handles well via setSubtitles.
TEXT_SUB_CODECS = frozenset(
    {
        "srt",
        "ass",
        "ssa",
        "vtt",
        "webvtt",
        "smi",
        "sub",
        "txt",
    }
)

# Jellyfin Codec names → canonical extension. Real libraries report ``subrip``
# (and similar) rather than ``srt``; without this map, External text tracks are
# rejected and never attached (seen on 12 Angry Men / 3 Women).
CODEC_ALIASES = {
    "subrip": "srt",
    "srt": "srt",
    "webvtt": "vtt",
    "vtt": "vtt",
    "ass": "ass",
    "ssa": "ssa",
    "sami": "smi",
    "smi": "smi",
    "mov_text": "srt",
    "text": "srt",
    "tx3g": "srt",
    "ttml": "srt",
    "dfxp": "srt",
    "microdvd": "sub",
    "mpl2": "txt",
    "jacosub": "txt",
    "realtext": "txt",
    "subviewer": "txt",
    "raw": "txt",
}

# Never materialise via setSubtitles even if the server marks them External.
IMAGE_SUB_CODECS = frozenset(
    {
        "pgssub",
        "pgs",
        "dvdsub",
        "dvb_subtitle",
        "dvbsub",
        "xsub",
        "vobsub",
        "sup",
        "hdmv_pgs_subtitle",
    }
)

SUBS_ROOT_SPECIAL = "special://profile/addon_data/plugin.video.kofin/subs/"
MAX_SUB_BYTES = 2 * 1024 * 1024
SUB_DOWNLOAD_TIMEOUT = (6.0, 10.0)
SUB_CACHE_MAX_AGE_SECONDS = 24 * 3600

_LANG_RE = re.compile(r"^[a-z]{2,3}$")
_SESSION_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Tests point this at a real temp dir — Kodistubs' translatePath is a no-op.
_root_override: Optional[str] = None


def set_subs_root_override(path: Optional[str]) -> None:
    """Redirect the cache root (unit tests only). ``None`` clears it."""
    global _root_override
    _root_override = path


def _extension_from_url(url: str) -> str:
    path = (url or "").split("?", 1)[0]
    _, _, ext = path.rpartition(".")
    ext = ext.strip().lower()
    if ext == "webvtt":
        return "vtt"
    return CODEC_ALIASES.get(ext, ext)


def normalize_codec(stream: JsonDict) -> str:
    """Canonical subtitle extension (``srt``, ``ass``, …) for a MediaStream.

    Jellyfin's ``Codec`` is often a long name (``subrip``, ``PGSSUB``); map
    those to a file extension. Fall back to the DeliveryUrl suffix, then empty.
    """
    raw = (stream.get("Codec") or "").strip().lower()
    if raw in IMAGE_SUB_CODECS:
        return raw
    if raw in CODEC_ALIASES:
        return CODEC_ALIASES[raw]
    if raw in TEXT_SUB_CODECS:
        return "vtt" if raw == "webvtt" else raw
    # Unknown Codec string — try DeliveryUrl (…/Stream.srt) or Path (.eng.srt).
    for key in ("DeliveryUrl", "Path"):
        ext = _extension_from_url(stream.get(key) or "")
        if ext in TEXT_SUB_CODECS or ext in CODEC_ALIASES:
            return CODEC_ALIASES.get(ext, ext)
    return raw


def language_token(language: Optional[str]) -> str:
    """ISO 639-1/2 token for the filename, else ``und``."""
    token = (language or "").strip().lower()
    if _LANG_RE.match(token):
        return token
    return "und"


def _stream_index(stream: JsonDict, default: int = 0) -> int:
    raw = stream.get("Index")
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def subtitle_filename(stream: JsonDict) -> str:
    """``{Index:02d}.{lang}.{codec}`` — no DisplayTitle noise in the path."""
    index = _stream_index(stream, 0)
    codec = normalize_codec(stream) or "srt"
    if codec in IMAGE_SUB_CODECS or codec not in TEXT_SUB_CODECS:
        codec = "srt"
    if codec == "webvtt":
        codec = "vtt"
    return "%02d.%s.%s" % (index, language_token(stream.get("Language")), codec)


def is_text_subtitle_eligible(stream: JsonDict) -> bool:
    """Whether a MediaStream may be attached via setSubtitles."""
    if stream.get("Type") != "Subtitle":
        return False
    if stream.get("DeliveryMethod") != "External":
        return False
    if not stream.get("DeliveryUrl"):
        return False
    # Present and False means the server refuses external delivery.
    if stream.get("SupportsExternalStream") is False:
        return False
    codec = normalize_codec(stream)
    if codec in IMAGE_SUB_CODECS:
        return False
    if codec in TEXT_SUB_CODECS:
        return True
    # Server marked it as a text stream with External delivery — trust that
    # over an unfamiliar Codec string (better a wrong extension than silence).
    if stream.get("IsTextSubtitleStream") is True:
        return True
    return False


def eligible_text_subtitles(source: JsonDict) -> List[JsonDict]:
    """MediaStreams that qualify for text external attach, source order."""
    return [
        stream
        for stream in (source.get("MediaStreams") or [])
        if is_text_subtitle_eligible(stream)
    ]


def absolute_delivery_url(server: str, delivery_url: str) -> str:
    if delivery_url.startswith("http://") or delivery_url.startswith("https://"):
        return delivery_url
    base = server.rstrip("/")
    if not delivery_url.startswith("/"):
        delivery_url = "/" + delivery_url
    return base + delivery_url


def external_subtitle_urls(server: str, source: JsonDict) -> List[str]:
    """Absolute DeliveryUrls for eligible text tracks (URL fallback path)."""
    return [
        absolute_delivery_url(server, stream["DeliveryUrl"])
        for stream in eligible_text_subtitles(source)
    ]


def _safe_session_id(play_session_id: str) -> str:
    cleaned = _SESSION_SAFE_RE.sub("_", (play_session_id or "").strip())
    return cleaned or "unknown"


def subs_root_path() -> str:
    if _root_override is not None:
        return _root_override
    path = xbmcvfs.translatePath(SUBS_ROOT_SPECIAL)
    if not path:
        raise OSError("xbmcvfs.translatePath returned empty for subtitle cache")
    return path


def session_dir_path(play_session_id: str) -> str:
    return os.path.join(subs_root_path(), _safe_session_id(play_session_id))


def ensure_session_dir(play_session_id: str) -> str:
    if _root_override is not None:
        path = os.path.join(_root_override, _safe_session_id(play_session_id))
        os.makedirs(path, exist_ok=True)
        return path
    special = SUBS_ROOT_SPECIAL + _safe_session_id(play_session_id) + "/"
    if not xbmcvfs.exists(SUBS_ROOT_SPECIAL):
        xbmcvfs.mkdirs(SUBS_ROOT_SPECIAL)
    if not xbmcvfs.exists(special):
        xbmcvfs.mkdirs(special)
    path = xbmcvfs.translatePath(special)
    if not path:
        raise OSError("xbmcvfs.translatePath returned empty for subtitle session dir")
    return path


def cleanup_session_subs(play_session_id: Optional[str]) -> None:
    """Best-effort remove one play session's subtitle cache directory."""
    if not play_session_id:
        return
    path = session_dir_path(play_session_id)
    if not os.path.isdir(path):
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
        LOG.debug("removed subtitle cache for session %s", play_session_id)
    except Exception as error:  # pragma: no cover - defensive
        LOG.debug("subtitle session cleanup failed: %s", error)


def wipe_subs_cache() -> None:
    """Remove the entire subs/ tree (auth change / user switch)."""
    path = subs_root_path()
    if not os.path.isdir(path):
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
        LOG.info("wiped subtitle cache under %s", path)
    except Exception as error:  # pragma: no cover - defensive
        LOG.warning("subtitle cache wipe failed: %s", error)


def reap_old_subs(max_age_seconds: int = SUB_CACHE_MAX_AGE_SECONDS) -> int:
    """Delete session dirs older than ``max_age_seconds``. Returns count removed."""
    root = subs_root_path()
    if not os.path.isdir(root):
        return 0
    now = time.time()
    removed = 0
    try:
        names = os.listdir(root)
    except OSError as error:
        LOG.debug("subtitle reaper list failed: %s", error)
        return 0
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        if age < max_age_seconds:
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except Exception as error:  # pragma: no cover - defensive
            LOG.debug("subtitle reaper failed for %s: %s", path, error)
    if removed:
        LOG.info("reaped %d stale subtitle session dir(s)", removed)
    return removed


def write_subtitle_file(directory: str, filename: str, body: bytes) -> str:
    path = os.path.join(directory, filename)
    with open(path, "wb") as handle:
        handle.write(body)
    return path


Downloader = Callable[[str], Optional[bytes]]


def materialize_text_subs(
    server: str,
    source: JsonDict,
    play_session_id: str,
    download: Downloader,
) -> Tuple[List[str], List[int], List[str]]:
    """Download eligible text subs into a session dir; fall back to URLs.

    Returns ``(attach_paths, jellyfin_indexes, local_paths)`` where
    ``attach_paths`` is what to pass to ``ListItem.setSubtitles`` (local file
    paths and/or URL fallbacks), ``jellyfin_indexes`` is ``SubsAttachOrder``,
    and ``local_paths`` is only the successfully materialised files (for
    play-state ``SubsPaths`` basename reconcile in PR2).
    """
    eligible = eligible_text_subtitles(source)
    if not eligible:
        return [], [], []

    directory: Optional[str] = None
    attach: List[str] = []
    indexes: List[int] = []
    local_paths: List[str] = []

    for stream in eligible:
        jf_index = _stream_index(stream, -1)
        url = absolute_delivery_url(server, stream["DeliveryUrl"])
        body = download(url)
        path: Optional[str] = None
        if body is not None:
            try:
                if directory is None:
                    directory = ensure_session_dir(play_session_id)
                path = write_subtitle_file(directory, subtitle_filename(stream), body)
            except Exception as error:
                LOG.debug(
                    "subtitle write failed for index %s: %s",
                    stream.get("Index"),
                    error,
                )
                path = None
        if path:
            attach.append(path)
            local_paths.append(path)
        else:
            attach.append(url)
            LOG.debug(
                "subtitle download failed for index %s; attaching URL",
                stream.get("Index"),
            )
        indexes.append(jf_index)

    return attach, indexes, local_paths


def play_state_subtitle_fields(
    attach_order: Sequence[int],
    paths: Sequence[str],
) -> JsonDict:
    """Provisional play-state keys for PR1 (absolute SubsMapping is PR2)."""
    return {
        "SubsAttachOrder": list(attach_order),
        "SubsPaths": list(paths),
        "SubsMapping": {},
        "SubsMappingReady": False,
    }
