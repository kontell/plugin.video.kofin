"""Automatic download policy: auto-next episodes and newly added movies
(plan W4.1/W4.4).

Both triggers end in the same guarded ``DOWNLOAD_ADD`` the context menu
uses, carrying an ``auto`` origin so W4.2's retention sweep knows what the
system fetched on its own; neither ever touches the manager directly. The
callers own *when* — the player's 80% latch, the library's new-content
drain — and this module owns *what*: which ids, which gates, which cap.
"""

from typing import Any, Dict, Iterable, List

from kofin.core import ipc, settings, state, toast
from kofin.core.log import Logger
from kofin.downloads import store

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# The playback fraction that triggers the next-episode lookup (W4.1).
NEXT_TRIGGER_RATIO = 0.8

# A sync cycle adding more than this many movies queues none of them: a
# server-side bulk import is almost never "I want all of this offline", and
# auto-pulling an arbitrary few of hundreds would be worse than asking.
NEW_MOVIES_BATCH_LIMIT = 5

ORIGIN_NEW_MOVIES = "auto:new"


def _live_ids() -> set:
    """Ids the store already holds in any working state — nothing to re-ask."""
    return {row.jellyfin_id for row in store.rows() if row.state != store.FAILED}


def next_episode_ids(api: Any, series_id: str, current_id: str, keep: int) -> List[str]:
    """The next ``keep`` unwatched, downloadable episodes after the current.

    A paged listing in airing order — ``ParentIndexNumber,IndexNumber``,
    both SortOrders stated (the 10.11 arity rule) — walked forward from the
    current episode. NextUp was rejected: it is watch-state-driven, and at
    80% the *current* episode is still its answer.
    """
    if keep <= 0:
        return []
    episodes: List[JsonDict] = []
    start = 0
    while True:
        page = api.items(
            {
                "ParentId": series_id,
                "IncludeItemTypes": "Episode",
                "Recursive": True,
                "SortBy": "ParentIndexNumber,IndexNumber",
                "SortOrder": "Ascending,Ascending",
                "StartIndex": start,
                "Limit": 200,
                "EnableTotalRecordCount": True,
            }
        )
        rows = page.get("Items") or []
        episodes.extend(rows)
        start += len(rows)
        if not rows or start >= int(page.get("TotalRecordCount") or 0):
            break

    following = False
    live = _live_ids()
    wanted: List[str] = []
    for episode in episodes:
        episode_id = str(episode.get("Id") or "")
        if not following:
            following = episode_id == current_id
            continue
        if (episode.get("UserData") or {}).get("Played"):
            continue
        if episode.get("CanDownload") is False:
            continue
        if episode_id in live:
            continue
        wanted.append(episode_id)
        if len(wanted) >= keep:
            break
    return wanted


def trigger_next(api: Any, item: JsonDict) -> bool:
    """The 80% crossing of a downloaded episode: queue the keep-ahead.

    True when something was queued. The caller latches its one-shot before
    calling — a failed resolve must not retry every tick.
    """
    item_id = str(item.get("Id") or "")
    series_id = str(item.get("SeriesId") or "")
    if not item_id or not series_id:
        return False
    if not settings.get_bool("downloadsEnabled"):
        return False
    if not settings.get_bool("downloadsAutoNext"):
        return False
    if not store.is_done(item_id):
        # Only downloaded playbacks chain: streaming a show is not a request
        # to fill the disk with it.
        return False
    if state.is_offline():
        # Nobody to ask; the previous keep-ahead usually already covered the
        # next episode anyway.
        return False
    keep = settings.get_int("downloadsAutoNextKeep") or 2
    try:
        wanted = next_episode_ids(api, series_id, item_id, keep)
    except Exception as error:
        LOG.warning("auto-next lookup failed for %s: %s", series_id, error)
        return False
    if not wanted:
        return False
    ipc.notify(ipc.DOWNLOAD_ADD, {"Ids": wanted, "Origin": "auto:%s" % series_id})
    LOG.info("auto-next queued %d episode(s) of %s", len(wanted), series_id)
    return True


def queue_new_movies(entries: Iterable[Any]) -> int:
    """W4.4: queue this cycle's newly added movies; returns how many.

    ``entries`` are ``newcontent.Entry`` rows from the drain — produced only
    by the *added* incremental writers, so initial syncs and repairs never
    reach here, and already-watched additions were dropped at the source.
    """
    if not settings.get_bool("downloadsEnabled"):
        return 0
    if not settings.get_bool("downloadsAutoMovies"):
        return 0
    movie_ids = [
        entry.item_id
        for entry in entries
        if getattr(entry, "type", "") == "Movie" and entry.item_id
    ]
    if not movie_ids:
        return 0
    if len(movie_ids) > NEW_MOVIES_BATCH_LIMIT:
        LOG.info(
            "auto-movies skipped a bulk import of %d; the cap is %d",
            len(movie_ids),
            NEW_MOVIES_BATCH_LIMIT,
        )
        try:
            toast.show(settings.localized(30751) % len(movie_ids), time_ms=5000)
        except Exception:  # pragma: no cover - uncached string etc.
            pass
        return 0
    live = _live_ids()
    wanted = [movie_id for movie_id in movie_ids if movie_id not in live]
    if not wanted:
        return 0
    ipc.notify(ipc.DOWNLOAD_ADD, {"Ids": wanted, "Origin": ORIGIN_NEW_MOVIES})
    LOG.info("auto-movies queued %d newly added movie(s)", len(wanted))
    return len(wanted)
