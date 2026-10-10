"""Skin entries and node trees from the server's views and the native selection.

The API build has no SQL view machinery, so this is where its skin-facing
presentation is built: every server view is a ``Kofin.nodes.*`` entry, a
whitelisted video library gets the generated node tree under
``special://profile/library/video/kofin/`` (``nodes/video.write_tree``) and
``library://`` paths in its properties, and everything else is a dynamic
``browse`` entry. The tree's filter nodes select by the library tag the API
backend puts on every row (``library_tag``), so the files are the SQL
build's with one rule changed. A whitelisted music library publishes Kodi's
music root: the SQL music tree filters on Kodi music *sources*, which this
build never writes.

Ownership is the ``kofin`` name prefix throughout (``nodes/fs.py``): a tree
rewrite reconciles the folder against what it wrote and never touches a
hand-made node beside it, and an empty whitelist takes the whole tree down.
"""

from typing import Dict, List, NamedTuple, Tuple

from kofin.core.log import Logger
from kofin.sync import playlists, private
from kofin.sync.nodes import props, video

LOG = Logger(__name__)

# The tag the API backend puts on every row of a library (metadata.tags),
# and the one the library's filter nodes select by.
LIBRARY_TAG_PREFIX = "kofin.library."


def library_tag(library: str) -> str:
    return LIBRARY_TAG_PREFIX + library


class View(NamedTuple):
    view_id: str
    view_name: str
    media_type: str


def node_entries(views: List[View], whitelist: List[str]) -> List[Tuple[Dict, bool]]:
    """The whitelist as the tree writer wants it: ``(view, mixed)`` pairs in
    tree order, a mixed library split into its two kinds, movies before
    shows before music videos and the server's order within a kind."""
    order = [v.view_id for v in views]
    entries = []
    for view in views:
        if view.view_id not in whitelist:
            continue
        node = {
            "Id": view.view_id,
            "Name": view.view_name,
            "Tag": library_tag(view.view_id),
            "Media": view.media_type,
        }
        if view.media_type == "mixed":
            for media in ("movies", "tvshows"):
                entries.append((dict(node, Media=media), True))
        elif view.media_type in video.NODES:
            entries.append((node, False))
    entries.sort(
        key=lambda entry: (
            video.MEDIA_RANK.get(entry[0]["Media"], len(video.MEDIA_RANK)),
            entry[0]["Media"],
            order.index(entry[0]["Id"]),
            entry[0]["Name"],
        )
    )
    return entries


def publish(items, server):
    views = [
        View(i["Id"], i.get("Name", ""), i.get("CollectionType") or "mixed")
        for i in items
        if i.get("Id") and i.get("CollectionType") != "livetv"
    ]
    whitelist = [
        x.replace("Mixed:", "") for x in (private.get_sync().get("Whitelist") or [])
    ]
    entries = node_entries(views, whitelist)
    singles: List[Dict] = []
    try:
        if entries:
            singles = video.single_nodes()
            video.write_tree(entries, singles)
            playlists.write_video_playlists(entries)
        else:
            video.delete_tree()
            playlists.remove_video_playlists()
    except Exception:
        # The tree is presentation; the properties below still publish.
        LOG.exception("library node tree not written")
        singles = []
    props.publish(
        views,
        {"Whitelist": whitelist, "SortedViews": [v.view_id for v in views]},
        singles,
        items,
        server,
    )
