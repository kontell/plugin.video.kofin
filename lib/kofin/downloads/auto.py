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
from kofin.downloads import notify_allowed, store

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# The playback fraction that triggers the next-episode lookup (W4.1).
NEXT_TRIGGER_RATIO = 0.8

# A sync cycle adding more than this many items queues none of them: a
# server-side bulk import is almost never "I want all of this offline", and
# auto-pulling an arbitrary few of hundreds would be worse than asking.
# Subscribed shows are exempt on purpose — a subscription is a standing
# order, and a whole season landing at once is exactly what it is for.
#
# The numbers are the defaults behind the settings below, kept as constants
# so an unset value (a profile that predates the sliders) behaves exactly as
# it always did.
NEW_MOVIES_BATCH_LIMIT = 5
NEW_EPISODES_BATCH_LIMIT = 10
NEW_ALBUMS_BATCH_LIMIT = 5


def _limit(setting_id: str, default: int) -> int:
    """A configured bulk threshold, never zero — a zero would read as "skip
    every import", which is what turning the feature off is for."""
    return max(1, settings.get_int(setting_id) or default)


def new_movies_limit() -> int:
    return _limit("downloadsBulkMovies", NEW_MOVIES_BATCH_LIMIT)


def new_episodes_limit() -> int:
    return _limit("downloadsBulkEpisodes", NEW_EPISODES_BATCH_LIMIT)


def new_albums_limit() -> int:
    return _limit("downloadsBulkAlbums", NEW_ALBUMS_BATCH_LIMIT)


ORIGIN_NEW_MOVIES = "auto:new"

# The comma-separated Jellyfin series ids subscribed to new-episode
# downloads (W4.6): written by the Series context toggle and the settings
# manage button, read by the drain.
SHOWS_SETTING = "downloadsEpisodeShows"


def subscribed_shows() -> List[str]:
    raw = settings.get_str(SHOWS_SETTING)
    return [part for part in raw.split(",") if part]


def save_subscribed_shows(series_ids: Iterable[str]) -> None:
    unique = dict.fromkeys(sid for sid in series_ids if sid)
    settings.set_str(SHOWS_SETTING, ",".join(unique))


def toggle_show(series_id: str) -> bool:
    """Flip a show's new-episode subscription; True when now subscribed."""
    current = subscribed_shows()
    if series_id in current:
        save_subscribed_shows(sid for sid in current if sid != series_id)
        return False
    save_subscribed_shows(current + [series_id])
    return True


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


def queue_new_content(api: Any, entries: Iterable[Any]) -> None:
    """The new-content drain hook (W4.4/W4.6): movies, episodes, albums.

    Every arm reads the same ``newcontent.Entry`` rows — produced only by
    the *added* incremental writers, so initial syncs and repairs never
    reach here — and every arm ends in the guarded ``DOWNLOAD_ADD``.
    """
    if not settings.get_bool("downloadsEnabled"):
        return
    drained = list(entries)
    queue_new_movies(drained)
    queue_new_episodes(drained)
    queue_new_albums(api, drained)


def queue_new_episodes(entries: Iterable[Any]) -> int:
    """W4.6: newly added episodes — subscribed shows first, then the global
    toggle; returns how many were queued.

    Origins carry the series (``auto:<seriesId>``, the same label auto-next
    writes), so retention treats every automatic episode alike. Subscribed
    shows are uncapped — a standing order covers a whole season landing at
    once — while the global arm keeps the bulk cap: an import of a show's
    back catalog is the movies problem again.
    """
    episode_entries = [
        entry
        for entry in entries
        if getattr(entry, "type", "") == "Episode" and entry.item_id and entry.series_id
    ]
    if not episode_entries:
        return 0
    live = _live_ids()
    subscribed = set(subscribed_shows())
    queued = 0

    def push(series_id: str, wanted: List[str]) -> None:
        nonlocal queued
        ipc.notify(ipc.DOWNLOAD_ADD, {"Ids": wanted, "Origin": "auto:%s" % series_id})
        live.update(wanted)
        queued += len(wanted)

    for series_id in sorted(
        {entry.series_id for entry in episode_entries}.intersection(subscribed)
    ):
        wanted = [
            entry.item_id
            for entry in episode_entries
            if entry.series_id == series_id and entry.item_id not in live
        ]
        if wanted:
            push(series_id, wanted)
            LOG.info(
                "subscription queued %d new episode(s) of %s", len(wanted), series_id
            )

    if not settings.get_bool("downloadsAutoEpisodes"):
        return queued
    rest = [
        entry
        for entry in episode_entries
        if entry.series_id not in subscribed and entry.item_id not in live
    ]
    if not rest:
        return queued
    cap = new_episodes_limit()
    if len(rest) > cap:
        LOG.info(
            "auto-episodes skipped a bulk import of %d; the cap is %d",
            len(rest),
            cap,
        )
        _bulk_toast(30764, len(rest))
        return queued
    for series_id in sorted({entry.series_id for entry in rest}):
        wanted = [entry.item_id for entry in rest if entry.series_id == series_id]
        push(series_id, wanted)
    if queued:
        LOG.info("auto-episodes queued %d newly added episode(s)", queued)
    return queued


def queue_new_albums(api: Any, entries: Iterable[Any]) -> int:
    """W4.6: newly added albums expand to their tracks; returns how many
    tracks were queued. Albums cap by album count — an album is naturally
    bounded, a bulk import of albums is not."""
    if not settings.get_bool("downloadsAutoAlbums"):
        return 0
    albums = [
        entry
        for entry in entries
        if getattr(entry, "type", "") == "MusicAlbum" and entry.item_id
    ]
    if not albums:
        return 0
    cap = new_albums_limit()
    if len(albums) > cap:
        LOG.info(
            "auto-albums skipped a bulk import of %d; the cap is %d",
            len(albums),
            cap,
        )
        _bulk_toast(30765, len(albums))
        return 0
    live = _live_ids()
    queued = 0
    for album in albums:
        try:
            tracks = _paged_items(
                api,
                {
                    "ParentId": album.item_id,
                    "IncludeItemTypes": "Audio",
                    "Recursive": True,
                },
            )
        except Exception as error:
            LOG.warning("album expansion failed for %s: %s", album.item_id, error)
            continue
        wanted = [
            str(track.get("Id"))
            for track in tracks
            if track.get("Id")
            and str(track.get("Id")) not in live
            and track.get("CanDownload") is not False
        ]
        if not wanted:
            continue
        ipc.notify(ipc.DOWNLOAD_ADD, {"Ids": wanted, "Origin": ORIGIN_NEW_MOVIES})
        live.update(wanted)
        queued += len(wanted)
    if queued:
        LOG.info("auto-albums queued %d track(s)", queued)
    return queued


def _paged_items(api: Any, params: Dict[str, Any]) -> List[JsonDict]:
    children: List[JsonDict] = []
    start = 0
    while True:
        page = api.items(
            dict(params, StartIndex=start, Limit=200, EnableTotalRecordCount=True)
        )
        rows = page.get("Items") or []
        children.extend(rows)
        start += len(rows)
        if not rows or start >= int(page.get("TotalRecordCount") or 0):
            break
    return children


def _bulk_toast(string_id: int, count: int) -> None:
    if not notify_allowed(string_id):
        return
    try:
        toast.show(settings.localized(string_id) % count, time_ms=5000)
    except Exception:  # pragma: no cover - uncached string etc.
        pass


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
    cap = new_movies_limit()
    if len(movie_ids) > cap:
        LOG.info(
            "auto-movies skipped a bulk import of %d; the cap is %d",
            len(movie_ids),
            cap,
        )
        _bulk_toast(30751, len(movie_ids))
        return 0
    live = _live_ids()
    wanted = [movie_id for movie_id in movie_ids if movie_id not in live]
    if not wanted:
        return 0
    ipc.notify(ipc.DOWNLOAD_ADD, {"Ids": wanted, "Origin": ORIGIN_NEW_MOVIES})
    LOG.info("auto-movies queued %d newly added movie(s)", len(wanted))
    return len(wanted)
