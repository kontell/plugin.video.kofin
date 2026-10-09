# Phase 5 — native music through public Kodi APIs

Implemented 2026-10-09 on `feat/or-phase5-music` on top of phase 4 (`feat/or-phase4-video`), verified on the P1D Piers Flatpak (22.0 beta2, `20260831-e513e0ff-dirty`) in the `OR` profile with the installed Jellyfin 12.2.0 server. The internal API build now synchronizes selected music libraries — songs, albums and artists — into Kodi's native music library. Release version `0.92.0`.

## Foundation

The kind table gained `Audio`, `MusicAlbum` and `MusicArtist` with a `rescan` removal policy: the audio API has no remove call, so a song leaves Kodi only through the complete listing of its directory, and the album and artist Kodi derives from it leave when their last song does. One directory per album under the library's music root — `…/<library>/music/<album id>/`, with `…/music/singles/<artist id>/` for songs Jellyfin gives no album — is the complete-snapshot unit: a song's removal is its album directory listed without it, confirmed by one readback of the directory.

A song is a file in its directory, `<album dir>/<item id>.<container>`, not a query. The first live import used the video layout's query-style URL and Kodi stored every song's file as its bare directory: the music database splits a URL into path and file name with the options dropped (`URIUtils::Split`, called from `CMusicDatabase::SplitPath`), where the video database keeps a plugin URL whole. A directory-named song is unplayable and matches nothing on a rescan, since `RetrieveMusicInfo` reuses ids by file name. The router recognises the file-style URL and routes it to the play route; the claim helper reads the id out of the path.

The tag is marked loaded with the real file size through `ListItem.setInfo("music", {"size"})`, the one key of the legacy setter that takes the non-deprecated branch (`xbmc/interfaces/legacy/ListItem.cpp`, the `size` branch calls `setSizeRaw` and the loop sets `SetLoaded(true)`); the import logged no deprecation line. The song's date is derived from a hash of its tags (`metadata.tag_hash`, `hash_time`), so a metadata change moves the directory's hash and the scanner re-reads it, while a play on the server changes nothing the scanner sees. Album MusicBrainz ids come from the album's own record so every song in a directory carries the same ones; a Jellyfin id is never dressed as an MBID; artist MBIDs are not sent.

Songs are read back in one paged listing per library (5,000 to a page, filtered on the music root's path) and split by directory in memory; albums are found through the songs' `albumid`, artists by name, and when Kodi merges two of ours into one row the first by id owns its art and description. Patches to the music kinds are confirmed by re-reading the scope once per kind rather than a details call per row. Albums and artists are patched for what the scanner cannot derive — art and description — and songs for the userdata the scanner discards (`CSong::CSong` zeroes the play count of every new row), only where the readback differs. The pass scans changed directories by name and walks the library root only when more than half of them changed; the provider builds a song listing in full, hands it over in one call, never ends a music directory with `succeeded=False`, and lists Kodi's own rows back when the store cannot be read.

## What Kodi decided

Read from the e513e0ff source and confirmed on the P1D:

- **A music scan replaces exactly one directory.** `RemoveSongsFromPath` defaults to `exact = true` (`xbmc/music/MusicDatabase.h`), so a root scan with an album directory absent from the root listing leaves that album's songs in place. The root listing therefore names every directory that still has songs to shed until the scan has confirmed it empty; a whole-library removal is one root walk over tombstone directories.
- **Kodi takes no art from a plugin listing.** `CFileItem::GetUserMusicThumb` returns nothing for a plugin path, so `FindArtForAlbums` finds none and `RetrieveLocalArt` lists every added album directory again after the walk looking for folder art. Album and artist art are set with `SetAlbumDetails` and `SetArtistDetails` after import.
- **The Python interpreter is reused only when Kodi has work between two listings.** `CScriptInvocationManager::GetLanguageInvoker` reuses the last invoker thread only when its script has reached the done state; the scanner asks for the next directory the moment `endOfDirectory` returns, so back-to-back listings with no database work between them start a fresh interpreter each time: 4 ms per listing warm against 87–93 ms cold. A first import is warm (Kodi imports between listings); a walk of an unchanged library and the post-scan art pass are cold.
- **A directory scan costs 60–80 ms.** `AudioLibrary.Scan` with a directory needs no registered source and finishes in 0.06–0.08 s on an unchanged album directory, so changed directories are scanned by name; the root walk pays only when most directories have work.
- **Every setter is a burst of autocommit statements.** With Kodi's `database` log component on, one `SetSongDetails` ran nine statements (`UPDATE song …`, `DELETE FROM song_genre`, `INSERT INTO song_genre`, `UPDATE song SET strGenres`) at 3–5 ms each, with one 581 ms stall: 36–47 ms a call in a burst over HTTP, 46–66 ms from the pass, against 0.4 ms for an isolated call. Parallel calls do not scale (30 ms a call alone, 45 ms with eight workers). The music database is in WAL mode on an NVMe disk; the cost is Kodi's.
- **Announcements made while the music scanner is busy are a transaction.** `AnnounceUpdate` in `MusicDatabase.cpp` sets `transaction` when `CMusicLibraryQueue::IsScanningLibrary()`, and `CDirectoryProvider::Announce` returns early on it, so the skin's music widgets (`random_albums.xsp`, `random_artists.xsp`, `recentlyaddedalbums`, `unplayed_albums.xsp`) re-query the library on every song written outside a scan and not at all inside one. The pass holds the scanner on a listing of a hold directory while it writes (`native.hold`, `provider._hold`, a window-property token); it silenced the widget refreshes but did not change the per-statement cost above. The video database announces without the flag.
- **Kodi's music Clean never asks and never deletes a plugin row.** `CPluginFile::Exists` is unconditionally true, so `CleanupSongsByIds` keeps every song; a Clean with the add-on enabled finished in 0 s and removed nothing.
- **`Player.Open` by song id cannot play a plugin song.** Kodi opens `musicdb://songs/<id>.<ext>` through `CMusicDatabaseFile`, which translates to the stored path and opens it as a file; a plugin URL fails there (`Init: Error opening file`). Opened by its file path — what the music windows do — the song resolves through the play route, is claimed as audio and reported.

## Measurements

All on the installed server's music library (22,381 songs, 1,557 albums, 634 artists, 7,855 songs played), from the Flatpak's log, with the three video libraries (6,593 items) selected throughout.

| Step | Wall time | Notes |
|---|---:|---|
| Enumerate artists, albums, songs | 0.2 s, 0.4 s, 26–28 s | songs in pages of 500 without `RecursiveItemCount` |
| Publish 24,572 items | 3.1–3.9 s | one transaction |
| First import: root walk | 70 s / 197 s | 1,558 directories; 26 ms median between directories on the fast run, growing from 77 to 225 ms on the slow one; listings 4 ms warm |
| First import: Kodi's post-scan art listings | 141 s / 155 s | 1,539 cold listings at 87–93 ms; nothing to find |
| Read back 22,381 songs | 0.6–1.0 s | five pages |
| Song userdata patches (7,380 rows, 296 batches of 25) | 339 s | 46 ms a row, see above; 7,855 played songs in Kodi after, as on the server |
| Album patches (1,557 rows) | 40–60 s | art and description |
| Artist patches (548 rows) | 15 s | 629 of 634 artists found by name; 5 applied without a row |
| First import end to end | 643 s / 668 s | select to Done |
| Deselect the library | 63.8 s | one root walk over 1,557 tombstone directories (58.9 s), 24,572 acknowledgements in one transaction |
| Repair all (31,165 items, every row compared) | 110 s | enumeration 83 s, compare 27 s, 2 patches (a test play's userdata) |
| Pass with nothing to do, all kinds | 4.2 s for the 22,381 songs | planning only |
| One unchanged directory scanned by name | 0.06–0.08 s | the incremental path |
| Idle service | 0 % | was a third of a core before `has_pending` |

GUI round trips sampled every three seconds on the box itself (an `XBMC.GetInfoBooleans` call over HTTP) stayed between 20 and 43 ms throughout every import and removal: median 20–32 ms, 90th percentile 32–40 ms, maximum 43 ms.

The first live run cost the day two findings besides the URL layout. A whole-library removal acknowledged 24,572 tombstones with one interval collection each, whose "still pending" subquery scans the item table — ten minutes at full CPU inside SQLite, then held the store's write lock across a service restart because Kodi does not stop the previous generation's thread; one collection per batch does the same in 0.2 s. And the tick tested for pending work by loading every pending payload: with 31,000 rows the idle service sat at a third of a core until a status index and a one-row existence query replaced it.

## Verification

Unit: the fake Kodi gained a music library that keeps the scanner's rules — a scan replaces one directory's songs, a song already on the path keeps its id, play count and last played, a new song's play count is zero whatever its tag says, albums and artists derive from the songs and vanish with the last one, no art is taken from a listing, and a root scan walks the folders the root lists. `tests/unit/test_api_music.py` covers import with userdata, album and artist patches, a metadata change rescanning one directory with ids kept and a local play captured, a server userdata change patching without a rescan, a song's removal, an album's last song taking album and artist with it, a whole-library removal through tombstone folders in one root walk, an unconfirmed removal staying pending, a song moved between albums, a directory that lost songs healing, singles, two libraries sharing a merged album, paged readback with batched acknowledgements, a failed patch staying pending, the scanner hold, the provider's root and directory listings with the store-unreadable fallback, the serializer, identity lookups and the enumeration. black, ruff, mypy and the full suite (3,755 tests) pass.

Live, on the P1D: the Music library selected from the picker imported completely twice (after a deselection between), every played song carried its server play count, and a song played from the library resolved through `DirectStream`, was claimed as audio at once, played to its end and was reported; the server's `UserDataChanged` for it came back through the websocket. Kodi's own music Clean Library with the add-on enabled removed nothing. Screenshots are under `tests/live/results/or-phase5/` (gitignored).

## Known limits

Song art is the album's: Jellyfin extracts an embedded image for many songs, and patching each would be 22,000 setters for a thumbnail the music windows fall back to anyway. Album `dateadded` is the scan time (`GetMediaDateFromFile` has no plugin date); the root listing's labels sort album folders by server creation date so Kodi's recently-added albums follow the server's. An artist no live album credits, or one whose name Kodi spells differently, is applied without a row and gets no art. Empty albums and artists, the singles shell and source membership remain Kofin views, as the plan requires. A whole-library removal leaves Kodi's empty path rows behind, which its own Clean sweeps. Jellyfin's `IsFavorite` on a song has no Kodi field and is not written.
