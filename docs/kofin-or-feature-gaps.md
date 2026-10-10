# Feature gaps between kofin-or and main

As of kofin-or 0.93.0 and main 0.29.1, 10 October 2026. The source for each row is `docs/kofin-or-parity.md` (the ledger, with its acceptance evidence) and `tools/package_manifest.py` (what the API profile omits); this report groups the ledger by what a user loses, what a user merely sees done differently, and what closes each gap. It is a reading of the ledger, not a replacement for it.

## How to read this

Main writes Kodi's own databases (MyVideos, MyMusic, Textures) and is therefore gated on Kodi's schema version. kofin-or writes nothing of Kodi's: it imports through Kodi's scanner from plugin listings and patches rows through JSON-RPC setters, keeping its own catalogue in `kofin.db`. Every gap below is a thing Kodi's public interfaces cannot express, a mechanism retired because it only made sense when kofin owned the tables, or a piece of phase 6 left for later.

## 1. What main does that kofin-or cannot

### 1.1 Blocked by Kodi's public API (ledger status G)

These need either a Kodi change (the phase 8 contribution programme in the plan) or the companion add-on. Rows marked closed below are closed by the companion as of its phases C0 to C4 (10 October 2026); the Kodi change stays the way to close them without a privileged helper.

| Gap | Main | kofin-or today | Closes through |
|---|---|---|---|
| Stream flags and authority (S09) | Exact `streamdetails` rows, including the Piers `iSource`/`iFlags` columns | The scanner's own stream probe plus the ListItem's streams; no authority over which wins | Flags closed by the companion (`stream_flags`); authority stays with Kodi's scanner. Found on the same day: a single-source item's audio and subtitle rows are missing in kofin-or, because `streams_and_runtime` reads the first source's streams and the compact payload drops them (one of 1,791 movies on the P1D has an audio row); the fix is the fallback to the item's streams, shared code for main first |
| Exact music scan/scrape bookkeeping (S12) | Per-row dates and scan markers | Tag-hash date, scan time for album `dateAdded` | Kodi: music metadata contract (phase 8, order 1) |
| Episode favourite tags (S14) | Episode rows tagged | No episode tag setter; episode favourites are dynamic views only | Closed by the companion (`episode_tags`); Kodi: episode tag persistence (phase 8, order 2) |
| Season plot updates (S18) | Written directly | Ingested once through `addSeason`, carried in the show refresh token; no update | Closed by the companion (`season_plot`); Kodi: season plot get/set (phase 8, order 2) |
| Precise empty-set removal (S23) | Deleted when emptied | Dropped by Kodi's clean, scoped to a cleared library root; an emptied set can linger | Closed by the companion (`remove_set`); Kodi: set removal API |
| Editing a version or extra in place (S25) | Rename, default, remove, per-asset userdata written directly | A version file the server drops leaves Kodi only with its movie; a new or renamed one is a folder scan; grouping is the user's, in the Versions Manager | Closed by the companion (`group_versions`, `retire_assets`) but for per-asset userdata; Kodi: the version and extra lifecycle (phase 8, order 4) |
| Empty artist, album and singles shells (S27) | Exact native objects | An empty album or artist is applied without a row; singles under `singles/<artist>` | Kodi: scoped music source operations (phase 8, order 3) |
| Discography and role rows (S29), artist MBIDs (S28) | Exact rows | Credits through song tags; artists matched by name (629 of 634 on the P1D); MBIDs not sent | Kodi: music source operations |
| Synthetic music source and album links (S30) | Written directly | Each library's directories are independent; a merged album row is owned by one of them | Kodi: music source operations |
| Server chapter images (S35, A03) | Texture aliases seeded per play | None; chapter startup gated off | Closed by the companion (`chapter_art`); Kodi: chapter-art association (phase 8, order 4) |
| Page atomicity (S45) | One transaction per page | Scoped readback per kind; a failed listing never announces a full apply, but there is no rollback | Kodi: job/result reporting (phase 8, order 2) |

### 1.2 Retired on purpose (ledger status R)

These exist only because main owns the tables. They are not coming to kofin-or, and the API profile removes their settings, routes and modules so nothing half-works.

| Removed in the API build | Why it is not a gap |
|---|---|
| Clean databases, Wipe and reseed (S48, A06), the `cleandatabases` route | Nothing of Kodi's to wipe; a library leaves through owned removal per row or one clear of its root (S47) |
| Refresh boxsets (`refreshboxsets` route) | Sets are filed at import through the tag and patched through `SetMovieSetDetails` (S22) |
| Actor artwork pre-cache (S33, A02, `precacheart`, the two settings) | Kodi's scanner caches a row's cast at import with `videolibrary.actorthumbs` on and a dialog the rest; decided 10 October 2026 |
| Chapter images (`chapterImages`) | Needs texture-cache writes (S35) |
| Texture CRC/dimension rows (S34) | Kodi caches what it draws |
| Native file and song relocation for downloads (S37) | The row keeps its plugin URL and the resolver plays the local file, so identity and resume stay the row's |
| Widget SQL fingerprints (S44) | Kodi's own announcements per setter, the music scanner hold during a pass, and the first-content skin reload |
| Schema gates and seeding (S46) | No schema to gate; the package exclusion and `api-capabilities.json` take their place |
| Sync during play, `limitIndex`, `limitThreads`, the `reuseLanguageInvoker` toggle | Tuning for main's writer threads and MyVideos writes; the API pass has its own progress bar and one scan at a time |
| Save playlist to Jellyfin and Download playlist context entries (`context_playlist.py`, `context_download_playlist.py`, the `saveplaylist` route) | Their mechanics read the SQL mapping; not yet rebuilt on the catalogue |
| The music node tree (`nodes/music.py`) and the Downloaded-episodes node | Both filter on Kodi music sources or on a moved path this build never writes; a synced music library publishes Kodi's own music root |

A legacy route reached from an old favourite or widget answers with the "Not available in this preview" toast rather than failing silently.

### 1.3 Implemented in part (ledger status I partial, or D)

| Gap | kofin-or today | Left to do |
|---|---|---|
| Download badges on seasons and shows (S39) | The movie, the episode and the episode's show carry the tag; the badge is on the movie or episode row | Season and show badges need the episode tag setter (S14) |
| Downloaded music views (S40) | The Downloaded-music smart playlist is a file | The music node tree stays SQL-only |
| Music-library download subscriptions (S36) | Playlist subscriptions reconcile through the shared playlist module | Music-library subscriptions claim through the SQL mapping; not claimed in this build |
| Music browsing by server and library (S31) | Design pending | Decide with the user whether one music root per library is wanted |
| Song ratings (S16) | Play count and last played patched after import | Ratings not mapped |
| Versions and extras (S24, S26) | Files in the movie's folder; Kodi imports a version as a movie of its own and the extras under the movie | Automatic grouping is the companion add-on's job; main groups in the writer |

## 2. What a user sees done differently

Not gaps, but the behaviour differences an upgrade from main shows.

- Every movie's URL sits in a folder of its own and every folder carries a scraper binding with `noupdate`; a profile from the one-directory layout needs the Movies library deselected and reselected.
- Import runs through Kodi's scanner: Kodi's own scanning dialog for video, kofin's bar for enumeration, scans and the pass; one scan at a time; the music scanner is held while a pass writes.
- Kodi's "Ignore video extras on scan" is turned off once for the extras import, with a toast.
- One setting on the Sync tab, "Library nodes and playlists", switches the node tree, the smart playlists and the Jellyfin playlists together; main has the playlist toggle alone.
- Kodi's shipped Movies and TV shows nodes are seeded beside kofin's at the first publish; the home screen's categories rows appear after one Kodi restart.
- The download badge key is `kofindownloaded` on the row where main's writers stamp `kofin.downloaded`; a skin reads both while the branches coexist.
- A show's `dateadded` is Kodi's own (derived from the newest episode); an empty season and an unnumbered special are dynamic views only; an album and an artist with no songs have no row.
- Downloads play from disk however the item is reached, as on main, but nothing of Kodi's rows moves for them.

## 3. What kofin-or has that main does not

- No schema gate: a Kodi database bump cannot block an import, because nothing of Kodi's is written.
- The companion add-on contract: what the stock interfaces cannot do is published as a request document (`companion/requests.json`, `Companion.Requests`), the companion's presence reported in `api-capabilities.json`.
- The nodes-and-playlists switch and the per-row library tag the node tree filters on.
- A capability report of what the running Kodi exposes, read at startup.

## 4. Paths to closure, in order of leverage

1. The companion add-on, `kontell/service.kofin.companion`, built to `docs/companion-addon-plan.md`: C0 to C4 serve grouping, dropped and renamed assets, chapter images, episode tags, season plots, stream flags and emptied sets, each verified on the P1D on 10 October 2026; C5, the music family, waits for the phase 8 music contributions. (Its identity was undecided when this report was first written; the plan, phase 6 exit).
2. Kodi contributions (plan, phase 8): the music metadata contract and `releasedate` fix first, then season plot and episode tags, job reporting, scoped music source operations, plugin ingestion fields, the version and extra lifecycle, chapter art. Each is verified against a stock build before its gap is marked closed.
3. Shared changes on main, ported to kofin-or: one playlist resolver per build, a `SeasonId` column on the download row, the ListItem builders out of `metadata.py`, the badge key rename.
4. Phase 7 (0.94.x): the Piers package and whole-addon audit, the profile reset audit, the dependency audit, 32-bit ARM validation of the 0.92 and 0.93 prereleases, and the repository submission candidate.
