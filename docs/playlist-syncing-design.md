# Playlist syncing for Kofin

| Field | Value |
|---|---|
| **Date** | 2026-09-16 |
| **Status** | Replaces `music-playlist-syncing-design.md` |
| **Addon** | `plugin.video.kofin` |
| **Companion** | `jellyfin-plugin-kofinsyncqueue` type `playlists` |

---

Kodi playlists are files, not MyVideos/MyMusic tables.

Jellyfin Audio playlists become `special://profile/playlists/music/Kofin/<Name>.m3u8`.

Jellyfin Video playlists become `special://profile/playlists/video/Kofin/<Name>.m3u8` beside the existing library `.xsp` files.

Mixed playlists (members of more than one of Audio/Video, or MediaType not Audio/Video) are ignored.

The setting id stays `syncMusicPlaylists` (retitled Sync playlists). Default false.

When it is on, music playlists materialize only if a music library is synced, and video playlists only if a movies/tvshows/musicvideos library is synced.

If neither kind is synced, playlist apply is a no-op.

Triggers are websocket `LibraryChanged` (Playlist ids) and KofinSyncQueue records with `media_type=playlists`.

There is no 15-minute poll.

A full reconcile (list + Etag skip + prune) still runs after a music or video library walk, on Repair/UpdateLibrary, and when the setting is turned on.

`playlist_state` in kofin.db stores jellyfin_id, Audio/Video, filename, and `fields.reference_checksum` of the server Etag.

Playlist ids never enter the movie/music writer queues.

KofinSyncQueue classifies `Playlist` as `playlists`, library-agnostic like boxsets, advertised in Features without a protocol bump.

Save to Jellyfin is an explicit context action on a local `.m3u`/`.m3u8` file: reverse-map lines to Jellyfin ids, refuse mixed, replace membership with a full Ids list when the file is already a managed playlist, otherwise POST /Playlists, then IPC `SyncPlaylists`.

Do not delete-by-PlaylistItemId: on Jellyfin 12 that id equals the item id, so two copies of one song share it.
