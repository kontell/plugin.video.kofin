"""NFO and artwork export beside downloaded files (plan W4.3).

An escape hatch, not a mirror: written once at download completion when
``downloadsExportMetadata`` is on, freshened never, and every piece is
best-effort — an export failure must never fail the download that carried
it. The NFOs carry **uniqueids** (the downloader-repo's omission that made
its NFOs a dead end), episodes get a ``tvshow.nfo`` + show-level art in the
show directory (a bare-profile TV scan is impossible without the show-level
file; one series fetch, only when it is absent), and music albums get the
``folder.jpg`` every scanner understands. Documents are hand-built strings
with escaped values, the same shape as the Downloaded-music ``.xsp``.
"""

import os
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from kofin.core.log import Logger

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

# The order that decides which provider id is the default one.
PROVIDER_PRIORITY = ("imdb", "tvdb", "tmdb")


def export_item(api: Any, item: JsonDict, media_path: str) -> None:
    """Write the item's NFO and art beside ``media_path``; never raises."""
    try:
        item_type = item.get("Type")
        if item_type == "Movie":
            _export_movie(api, item, media_path)
        elif item_type == "Episode":
            _export_episode(api, item, media_path)
        elif item_type == "Audio":
            _export_song(api, item, media_path)
    except Exception:  # pragma: no cover - the whole module is best-effort
        LOG.exception("metadata export failed for %s", item.get("Id"))


def uniqueid_lines(provider_ids: Optional[JsonDict]) -> List[str]:
    """``<uniqueid>`` elements, defaulting by provider priority."""
    pairs: List[Tuple[str, str]] = []
    for key, value in (provider_ids or {}).items():
        if value:
            pairs.append((str(key).lower(), str(value)))
    if not pairs:
        return []
    default = next(
        (
            provider
            for provider in PROVIDER_PRIORITY
            if any(kind == provider for kind, _ in pairs)
        ),
        pairs[0][0],
    )
    return [
        '<uniqueid type="%s"%s>%s</uniqueid>'
        % (escape(kind), ' default="true"' if kind == default else "", escape(value))
        for kind, value in pairs
    ]


def _document(root: str, lines: List[str]) -> str:
    body = "".join("    %s\n" % line for line in lines if line)
    return "%s<%s>\n%s</%s>\n" % (XML_DECLARATION, root, body, root)


def _field(tag: str, value: Any) -> str:
    if value is None or value == "":
        return ""
    return "<%s>%s</%s>" % (tag, escape(str(value)), tag)


def _date_part(value: Any) -> str:
    return str(value or "").split("T")[0]


def movie_nfo(item: JsonDict) -> str:
    return _document(
        "movie",
        [
            _field("title", item.get("Name")),
            _field("year", item.get("ProductionYear")),
            _field("premiered", _date_part(item.get("PremiereDate"))),
            _field("plot", item.get("Overview")),
        ]
        + uniqueid_lines(item.get("ProviderIds")),
    )


def episode_nfo(item: JsonDict) -> str:
    return _document(
        "episodedetails",
        [
            _field("title", item.get("Name")),
            _field("showtitle", item.get("SeriesName")),
            _field("season", item.get("ParentIndexNumber")),
            _field("episode", item.get("IndexNumber")),
            _field("aired", _date_part(item.get("PremiereDate"))),
            _field("plot", item.get("Overview")),
        ]
        + uniqueid_lines(item.get("ProviderIds")),
    )


def tvshow_nfo(series: JsonDict) -> str:
    return _document(
        "tvshow",
        [
            _field("title", series.get("Name")),
            _field("year", series.get("ProductionYear")),
            _field("plot", series.get("Overview")),
        ]
        + uniqueid_lines(series.get("ProviderIds")),
    )


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _write_image(api: Any, path: str, item_id: str, kind: str, tag: str) -> None:
    """Fetch one image to ``path``; absent tags and fetch failures skip.

    The name stays ``.jpg`` whatever the server returns — scanners resolve
    the conventional names, and Kodi reads image bytes by content.
    """
    if not tag or os.path.exists(path):
        return
    try:
        payload = api.download(api.image_url(item_id, kind, tag))
        if payload:
            with open(path, "wb") as handle:
                handle.write(payload)
    except Exception as error:
        LOG.debug("art export skipped for %s %s: %s", item_id, kind, error)


def _backdrop_tag(item: JsonDict) -> str:
    tags = item.get("BackdropImageTags") or []
    return str(tags[0]) if tags else ""


def _export_movie(api: Any, item: JsonDict, media_path: str) -> None:
    directory = os.path.dirname(media_path)
    base = os.path.basename(media_path).rsplit(".", 1)[0]
    _write_text(os.path.join(directory, base + ".nfo"), movie_nfo(item))
    item_id = str(item.get("Id") or "")
    tags = item.get("ImageTags") or {}
    _write_image(
        api,
        os.path.join(directory, "poster.jpg"),
        item_id,
        "Primary",
        str(tags.get("Primary") or ""),
    )
    _write_image(
        api,
        os.path.join(directory, "fanart.jpg"),
        item_id,
        "Backdrop",
        _backdrop_tag(item),
    )


def _export_episode(api: Any, item: JsonDict, media_path: str) -> None:
    directory = os.path.dirname(media_path)
    base = os.path.basename(media_path).rsplit(".", 1)[0]
    _write_text(os.path.join(directory, base + ".nfo"), episode_nfo(item))

    # The show directory: the season leaf's parent, or the media directory
    # itself when the episode has no season (mirrors files.item_dirs).
    show_dir = (
        directory
        if item.get("ParentIndexNumber") is None
        else os.path.dirname(directory)
    )
    series_id = str(item.get("SeriesId") or "")
    if not series_id:
        return
    show_nfo = os.path.join(show_dir, "tvshow.nfo")
    if not os.path.exists(show_nfo):
        # One fetch per show, and only for its first exported episode.
        series = api.item(series_id)
        _write_text(show_nfo, tvshow_nfo(series))
    _write_image(
        api,
        os.path.join(show_dir, "poster.jpg"),
        series_id,
        "Primary",
        str(item.get("SeriesPrimaryImageTag") or ""),
    )
    parent_backdrops = item.get("ParentBackdropImageTags") or []
    _write_image(
        api,
        os.path.join(show_dir, "fanart.jpg"),
        str(item.get("ParentBackdropItemId") or series_id),
        "Backdrop",
        str(parent_backdrops[0]) if parent_backdrops else "",
    )


def _export_song(api: Any, item: JsonDict, media_path: str) -> None:
    directory = os.path.dirname(media_path)
    album_id = str(item.get("AlbumId") or "")
    album_tag = str(item.get("AlbumPrimaryImageTag") or "")
    if album_id and album_tag:
        _write_image(
            api, os.path.join(directory, "folder.jpg"), album_id, "Primary", album_tag
        )
        return
    tags = item.get("ImageTags") or {}
    _write_image(
        api,
        os.path.join(directory, "folder.jpg"),
        str(item.get("Id") or ""),
        "Primary",
        str(tags.get("Primary") or ""),
    )
