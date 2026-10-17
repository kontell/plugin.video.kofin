# Feature gaps between kofin-or and main

As of kofin-or 0.93.6 and main 0.29.2, 16 October 2026. The source for each row is `docs/kofin-or-parity.md` (the ledger, with its acceptance evidence) and `tools/package_manifest.py` (what the API profile omits). This report groups the ledger by what a user loses, what a user merely sees done differently, and what closes each gap. It is a reading of the ledger, not a replacement for it.

## How to read this

Main writes Kodi's own databases (MyVideos, MyMusic, Textures) and is therefore gated on Kodi's schema version. kofin-or writes nothing of Kodi's: it imports through Kodi's scanner from plugin listings and patches rows through JSON-RPC setters, keeping its own catalogue in `kofin.db`. Every gap below is a thing Kodi's public interfaces cannot express, or a piece of kofin-or not built yet. A row of main that Kodi would never show (an empty season, a set with no movie) or that main does not really fill (song ratings, written as 0) is not a gap and is not listed.

## 1. What main does that kofin-or does not

### 1.1 Blocked by Kodi's public API (ledger status G, and C where the companion closes it)

These need a Kodi change or the companion add-on. A kontell/xbmc PR named here is a trial, not yet upstream; the companion stays the way to close a row on Kodi 22.

| Gap | Main | kofin-or today | Closes through |
|---|---|---|---|
| Music date added (S12) | Each song's server date, each album's | A plugin song is dated at scan time and an album by its newest song, so Recently added albums follows scan order and a re-listed album goes back to the top | Kodi: `InfoTagMusic.setDateAdded()`, honoured where the file has no date (kontell/xbmc#13) |
| Episode favourite tags (S14) | Episode rows tagged | No episode tags in Kodi | Closed by the companion (`episode_tags`); Kodi: episode tags (kontell/xbmc#12) |
| Season plot updates (S18) | Written directly | Ingested once through `addSeason` | Closed by the companion (`season_plot`); Kodi: `plot` on `SetSeasonDetails` (kontell/xbmc#6) |
| Precise empty-set removal (S23) | Deleted when emptied | Dropped by Kodi's clean, scoped to a cleared library root; an emptied set can linger | Closed by the companion (`remove_set`); Kodi: `VideoLibrary.RemoveMovieSet` (kontell/xbmc#7) |
| Version grouping (S24) | Grouped by the writer | A version file imports as a movie of its own, grouped by the user in the Versions Manager | Closed by the companion (`group_versions`); Kodi: version flags on the Python `InfoTagVideo` (kontell/xbmc#8) |
| A version or extra the server drops (S25) | Removed by the writer | A dropped version stays until its movie leaves, and Clean library deletes every extra without asking. kofin-or #327 and #328 have Clean ask about both; it then removes an extra the server dropped, and offers to remove a version as an unavailable source | Closed by the companion (`retire_assets`); Kodi: clean by the add-on's answer and with the movie (kontell/xbmc#2, #10) |
| Stream flags (S09) | Exact `streamdetails` rows with `iFlags` | No default, forced or hearing-impaired bit | Closed by the companion (`stream_flags`); Kodi: `setFlags()` on the Python stream details (kontell/xbmc#9) |
| Server chapter images (S35, A03) | Texture aliases seeded per play | None | Closed by the companion (`chapter_art`); Kodi: `chapter<n>` art on the playing item (kontell/xbmc#11) |
| Import time | WAL and the name indexes set on Kodi's databases | Kodi's rollback journal and unindexed name lookups | Closed by the companion; Kodi: WAL (kontell/xbmc#4) and the name indexes (kontell/xbmc#5) |
| Featured artists' discography (S29) | A `discography` row for every artist an album's `ArtistItems` names, so a featured artist's dialog lists the album | No public interface writes `discography`; a featured artist's dialog lists only the albums they are album artist of | Kodi: a typed artist-album relationship (feasibility §9.3), with no trial PR |
| Stream rows after a play (S09) | A play's probed streams overwrite the row in both builds; main writes the server's back on the item's next update | The probed rows stay: there is no stream setter, and a play moves neither the item nor its stream token, so no refresh runs | Kodi: a provider stream-authority policy (feasibility §9.5), with no trial PR |
| Unnumbered specials (S19) | Filed with no number | Kodi's scanner files a plugin episode only with a season and an episode number; the special is a dynamic view | Kodi, if wanted: file a plugin item by its own tag where it has no number |

### 1.2 Not built yet in kofin-or (ledger status D, or I partial)

None of these needs Kodi.

| Gap | kofin-or today | To do |
|---|---|---|
| Music browsing by library (S31, S40) | A synced music library publishes Kodi's own music root; the Downloaded-music view is a smart playlist | Per-library music nodes on `path` rules, which match an album or an artist by its songs' paths; decide whether they are wanted |
| Per-version play state (S25) | Only the movie's row is patched | Jellyfin keeps a play count per version; set it on each version file through `Files.SetFileDetails` |
| Save playlist to Jellyfin, Download playlist on a playlist file (`context_playlist.py`, `context_download_playlist.py`, the `saveplaylist` route) | Omitted from the API profile; playlist and music-library download subscriptions work from kofin's own views (S36) | Rebuild on the catalogue, which the API playlist resolver already reads |
| Artist MusicBrainz ids (S28) | Album ids sent; artists matched by name (629 of 634 on the P1D) | Send them if they separate the artists name matching gets wrong |
| Downloaded-episodes node (S39) | The badge is on the episode and on its season and show | A node on the "Kofin Downloads" tag, once Kodi keeps episode tags (S14) |

### 1.3 Retired on purpose (ledger status R)

These exist only because main owns the tables, or they are Kodi's own model. They are not coming to kofin-or, and the API profile removes their settings, routes and modules so nothing half-works.

| Removed in the API build | Why it is not a gap |
|---|---|
| Clean databases, Wipe and reseed (S48, A06), the `cleandatabases` route | Nothing of Kodi's to wipe; a library leaves through owned removal per row or one clear of its root (S47) |
| Refresh boxsets (`refreshboxsets` route) | Sets are filed at import through the tag and patched through `SetMovieSetDetails` (S22) |
| Actor artwork pre-cache (S33, A02, `precacheart`, the two settings) | Kodi's scanner caches a row's cast at import with `videolibrary.actorthumbs` on, and a dialog the rest |
| Texture CRC/dimension rows (S34) | Kodi caches what it draws |
| Native file and song relocation for downloads (S37) | The row keeps its plugin URL and the resolver plays the local file, so identity and resume stay the row's |
| Widget SQL fingerprints (S44) | Kodi's own announcements per setter, the music scanner hold during a pass, and the first-content skin reload |
| Schema gates and seeding (S46) | No schema to gate; the package exclusion and `api-capabilities.json` take their place |
| Sync during play, `limitIndex`, `limitThreads`, the `reuseLanguageInvoker` toggle | Tuning for main's writer threads and MyVideos writes; the API pass has its own progress bar and one scan at a time |
| Empty artist, album and singles shells (S27) | Kodi has an artist through a credit and deletes one without (`CleanupArtists`), and an album through its songs; songs without an album file under `singles/<artist>` |
| Song artist roles (S29) | Main writes only the default role |
| Synthetic music source (S30) | Per-library browsing takes `path` rules; two libraries' directories are independent |
| Page transaction and rollback (S45) | The pass converges by readback and retry, and a failed listing never announces a full apply |
| The SQL music node tree (`nodes/music.py`) | It filters on Kodi music sources this build never writes; per-library music nodes are 1.2 |

A legacy route reached from an old favourite or widget answers with the "Not available in this preview" toast rather than failing silently.

## 2. What a user sees done differently

Not gaps, but the behaviour differences an upgrade from main shows.

- Every movie's URL sits in a folder of its own, and every folder carries a scraper binding with `noupdate`; a profile from the one-directory layout needs the Movies library deselected and reselected.
- Import runs through Kodi's scanner: Kodi's own scanning dialog for video, kofin's bar for enumeration, scans and the pass; one scan at a time; the music scanner is held while a pass writes.
- Kodi's "Ignore video extras on scan" is turned off once for the extras import, with a toast, and "Download actor thumbnails" is off for the length of a large video scan.
- One setting on the Sync tab, "Library nodes and playlists", switches the node tree, the smart playlists and the Jellyfin playlists together; main has the playlist toggle alone.
- Kodi's shipped Movies and TV shows nodes are seeded beside kofin's at the first publish; the home screen's categories rows appear after one Kodi restart.
- The download badge key is `kofindownloaded` on the row where main's writers stamp `kofin.downloaded`; a skin reads both while the branches coexist.
- An album or an artist with no songs has no row; an empty season and a set with no synced movie are dynamic views, as on main, where Kodi does not list them either.
- Downloads play from disk however the item is reached, as on main, but nothing of Kodi's rows moves for them.

## 3. What kofin-or has that main does not

- No schema gate: a Kodi database bump cannot block an import, because nothing of Kodi's is written.
- Local sources and other add-ons' library content share the library with kofin's rows, which kofin alone changes.
- No access to Kodi's databases, which the official repository requires.
- The companion add-on contract: what the stock interfaces cannot do is published as a request document (`companion/requests.json`, `Companion.Requests`), the companion's presence reported in `api-capabilities.json`.
- The nodes-and-playlists switch and the per-row library tag the node tree filters on.
- A capability report of what the running Kodi exposes, read at startup.

## 4. Paths to closure, in order of leverage

1. The companion add-on, `kontell/service.kofin.companion`, built to `docs/companion-addon-plan.md`, closes the C rows of 1.1 on Kodi 22.
2. kofin-or's own work in 1.2, none of which waits for Kodi.
3. Kodi contributions for Kodi 23: the trial PRs on kontell/xbmc named in 1.1, each verified with kofin on a master build before it goes upstream. A row closes when kofin-or verifies it on a stock build that contains the merged change, and the companion retires when its last row has.
4. Shared changes on main, ported to kofin-or: one playlist resolver per build, a `SeasonId` column on the download row, the ListItem builders out of `metadata.py`, the badge key rename.
5. Phase 7 (0.94.x): the Piers package and whole-addon audit, the profile reset audit, the dependency audit, 32-bit ARM validation of the 0.92 and 0.93 prereleases, and the repository submission candidate.
