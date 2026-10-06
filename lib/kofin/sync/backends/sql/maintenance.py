"""Legacy startup, repair and playlist operations behind the SQL port."""

from typing import Dict, List
import xbmc
from kofin.core import settings
from kofin.core.log import Logger
from kofin.sync.db import Database, get_sync
from kofin.sync import kofindb as jellyfin_db, musicsources
from kofin.sync.kodidb import Movies as KodiDb, Music as MusicKodiDb

LOG = Logger(__name__)
TARGET_DB_VERSION = 1


def apply_playlist(self, data):
    """Write or prune one playlist from a websocket/FastSync id."""
    playlist_id = (data or {}).get("Id") or ""
    if not playlist_id:
        return
    from kofin.downloads import subscriptions

    kinds = self.playlist_kinds()
    if "Audio" not in kinds:
        subscriptions.reconcile_playlist_direct(self.api, playlist_id)
    if not kinds:
        return
    try:
        from kofin.sync import playlists as music_playlists
        from kofin.sync.kodidb import Music as MusicKodiDb

        item = self.api.item(playlist_id)
        memberships: Dict[str, List[str]] = {}
        with self.music_database_lock:
            with Database("kofin") as kofindb:
                mapping = jellyfin_db.JellyfinDatabase(kofindb.cursor)
                music = None
                if "Audio" in kinds:
                    with Database("music") as musicdb:
                        music = MusicKodiDb(musicdb.cursor)
                        if "Video" in kinds:
                            with Database("video") as videodb:
                                video = music_playlists.VideoPlaylistDb(videodb.cursor)
                                music_playlists.apply_one(
                                    self.api,
                                    mapping,
                                    music,
                                    video,
                                    mapping,
                                    item,
                                    kinds,
                                    audio_memberships=memberships,
                                )
                        else:
                            music_playlists.apply_one(
                                self.api,
                                mapping,
                                music,
                                None,
                                mapping,
                                item,
                                kinds,
                                audio_memberships=memberships,
                            )
                elif "Video" in kinds:
                    with Database("video") as videodb:
                        video = music_playlists.VideoPlaylistDb(videodb.cursor)
                        music_playlists.apply_one(
                            self.api, mapping, None, video, mapping, item, kinds
                        )
        subscriptions.reconcile_playlist_memberships(memberships)
    except Exception:
        LOG.exception("ApplyPlaylist failed for %s", playlist_id)


def _reconcile_playlists(self, api, kinds):
    from kofin.sync import playlists as music_playlists
    from kofin.sync.kodidb import Music as MusicKodiDb

    memberships: Dict[str, List[str]] = {}
    with Database("kofin") as kofindb, Database("music") as musicdb:
        mapping = jellyfin_db.JellyfinDatabase(kofindb.cursor)
        music = MusicKodiDb(musicdb.cursor) if "Audio" in kinds else None
        if "Video" in kinds:
            with Database("video") as videodb:
                video = music_playlists.VideoPlaylistDb(videodb.cursor)
                music_playlists.reconcile(
                    api,
                    mapping,
                    music,
                    video,
                    mapping,
                    kinds,
                    audio_memberships=memberships,
                )
        else:
            music_playlists.reconcile(
                api,
                mapping,
                music,
                None,
                mapping,
                kinds,
                audio_memberships=memberships,
            )
    return memberships


def reassert_music_sources(self):
    """Rewrite the per-library music ``source`` rows after a Kodi scan.

    Kodi's own scanner empties the source table on any run whose
    sources.xml disagrees with it, taking every ``album_source`` link
    with it (tgrDeleteSource) and leaving the per-library music nodes
    filtering on a name nothing carries. This is the in-session heal;
    ``check_version`` covers a scan that happened while Kodi was off.
    """
    try:
        with self.music_database_lock:
            with Database("kofin") as kofindb, Database("music") as musicdb:
                views = jellyfin_db.JellyfinDatabase(kofindb.cursor).get_views_by_media(
                    "music"
                )

                if not views:
                    return

                musicsources.reassert(kofindb.cursor, musicdb.cursor, views)
    except Exception:
        LOG.exception("ReassertMusicSources failed")


def repoint_ratings(self):
    """Point synced films at the rating row the user now prefers.

    The ``preferCriticRating`` flip's apply path. Both rating rows are
    written at sync time, so this fetches nothing and rewrites nothing but
    ``movie.c05`` — and only for kofin-owned films: Kodi's own scrapers
    write ``default``-typed ratings too, and which of a scraped film's
    ratings is its default is not ours to move.

    The refresh is this command's own (widget-refresh-plan D4): ratings are
    a hashed section, so it fires when a pointer actually moved and stays
    quiet when the pass was a no-op.
    """
    rating_type = "critic" if settings.get_bool("preferCriticRating") else "default"

    with Database("kofin") as kofin_db:
        db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)
        movie_ids = [kodi_id for _, kodi_id in db.get_item_ids_by_media("movie")]

    if not movie_ids:
        return

    with self.database_lock:
        with Database() as videodb:
            updated = KodiDb(videodb.cursor).repoint_ratings(movie_ids, rating_type)

    # rowcount is films considered, not films moved: the UPDATE matches
    # every id it is handed, and one already on the preferred row is a
    # no-op write.
    LOG.info("--[ ratings repointed to %s over %s film(s) ]", rating_type, updated)
    self.refresh_libraries({"video"})


def test_databases(self):
    """Open the gated databases to prove the files exist and pass the
    schema gate; raises SchemaError otherwise."""
    for kind in self.required_kinds():
        with Database(kind):
            pass


def check_version(self):
    """
    Checks database version and triggers any required data migrations
    """
    with Database("kofin") as kofin_db:
        db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)
        db_version = db.get_version()

        if not db_version:
            # Make sure we always have a version in the database
            db.add_version((TARGET_DB_VERSION))

    # Video Database Migrations
    with Database("video") as videodb:
        vid_db = KodiDb(videodb.cursor)
        if vid_db.migrations():
            LOG.info("changes detected, reloading skin")
            xbmc.executebuiltin("UpdateLibrary(video)")
            xbmc.executebuiltin("ReloadSkin()")

    # Music Database Migrations. Only when a music library is synced —
    # opening MyMusic otherwise would put the schema gate in front of
    # users who never asked kofin to touch their music.
    if "music" in self.required_kinds():
        with Database("kofin") as kofindb, Database("music") as musicdb:
            music_db = MusicKodiDb(musicdb.cursor)
            music_db.ensure_blank_artist()
            pruned = musicsources.prune_orphan_paths(kofindb.cursor, musicdb.cursor)
            if pruned:
                LOG.info("pruned %s orphaned music path rows", pruned)
            singles = musicsources.prune_orphan_singles(kofindb.cursor, musicdb.cursor)
            if singles:
                LOG.info("pruned %s leftover single albums", singles)
            restored = musicsources.heal_missing_artists(kofindb.cursor, musicdb.cursor)
            if restored:
                LOG.info("restored %s artist row(s) still mapped in kofin.db", restored)
            # Kodi's own music scanner empties the source table whenever
            # it disagrees with sources.xml, which with an empty one it
            # always does — so the per-library music nodes come back from
            # any scan matching nothing until this runs. Startup covers a
            # scan that happened while Kodi was off; the
            # AudioLibrary.OnScanFinished command covers one in session.
            musicsources.reassert(
                kofindb.cursor,
                musicdb.cursor,
                jellyfin_db.JellyfinDatabase(kofindb.cursor).get_views_by_media(
                    "music"
                ),
            )


def probe_boxset_drift(self):
    """Schedule a boxsets pass when local set state disagrees with itself.

    The Etag gate cannot see local drift: a member removed and re-added
    arrives as a fresh movie row with no idSet while the set's Etag never
    moves (docs/boxsets-robustness-plan.md). This probe is the recurring
    eye that gap needs — pure-local, kofin.db's set references and
    boxset_state against one GROUP BY over MyVideos, no server traffic —
    so it can run on every startup tick alongside probe_divergence. Any
    disagreement enqueues the targeted boxsets pass, where the writer
    heals exactly the drifted sets and Etag-matched healthy sets stay
    skipped.

    Convergence: the walk ends by re-stamping every non-guarded set's
    state from measured reality (restamp_boxset_states), including both
    sides of a shared-member steal -- movie.idSet is single-valued, so
    the last set walked owns a shared member and the earlier owner's
    count moves *after* its own mid-walk stamp (V7,
    docs/healing-loops-plan.md). A guarded set keeps its stale or
    missing state deliberately: that is the designed retry, and this
    probe re-scheduling its walk is the retry's clock, not a loop bug.
    Members outside the synced libraries count into neither side. One
    walk per disturbance; a probe->walk->probe loop cannot form.
    """
    if not self.sync_allowed_now():
        return

    if get_sync()["Libraries"]:
        # An unfinished full sync owns the field, and its queue may well
        # include the boxsets pass this probe would schedule.
        return

    if self.total_updates:
        # Catch-up work in flight: boxset writes may be queued, and half
        # of them would read as drift now and heal on their own.
        return

    with Database("kofin") as kofin_db:
        db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)

        if not db.get_views_by_media("boxsets"):
            # No collections view: nothing can have synced, and the pass
            # this probe schedules would have nothing to walk.
            return

        references = list(db.get_item_ids_by_media("set"))

        if not references:
            return

        states = dict(db.get_boxset_states())

    with self.database_lock:
        with Database() as videodb:
            kodi = KodiDb(videodb.cursor)
            set_rows = set(kodi.get_boxset_ids())
            counts = kodi.get_boxset_movie_counts()

    drifted = []

    for jellyfin_id, kodi_id in references:
        stored = states.get(jellyfin_id)

        if (
            kodi_id not in set_rows
            or stored is None
            or stored != counts.get(kodi_id, 0)
        ):
            drifted.append(jellyfin_id)

    if not drifted:
        return

    LOG.warning(
        "boxset drift probe: %s of %s set(s) unhealthy (%s); "
        "scheduling a boxsets pass to heal",
        len(drifted),
        len(references),
        ", ".join(drifted[:5]),
    )
    self.enqueue_command("SyncLibrary", {"Id": "Boxsets:"})
