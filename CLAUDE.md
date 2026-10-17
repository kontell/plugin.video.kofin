# CLAUDE.md

## Kodi knowledge lives in kodi-drive

Shared Kodi knowledge is **not** in this file. Use the `kodi-drive:*` skills, or read
`../kodi-drive/README.md`.

Directly relevant: `kodi-database-writing`, `kodi-library-data`, `kodi-texture-cache`,
`kodi-library-nodes`, `kodi-plugin-handles`, `kodi-addon-manifest`, `kodi-addon-lifecycle`,
`kodi-announcements`, `kodi-playback-resume`, `kodi-performance`, `kodi-addon-release`, and
`jellyfin-client` under `adjacent/`.

**Do not add generally-useful Kodi findings here** — contribute them to kodi-drive. This file
holds only what is specific to *this* add-on.

## What this is

Kofin (`plugin.video.kofin`) is a Jellyfin client addon for Kodi that mirrors selected server libraries directly into Kodi's own SQLite databases (MyVideos/MyMusic) for native library UX, with playback resolved back through `plugin://` paths. It is a rewrite of `jellyfin-kodi` on the principle "rewrite the shell, transplant the organs": the shell (entry points, settings, lifecycle, playback decisions) is new code; the hard-won parts (Kodi DB writers, sync pipeline, SyncPlay) are near-verbatim ports from the Kontell fork (branch:combined/syncplay-sync). See `docs/rewrite-research.md` for the rationale; the per-phase plans in `docs/` record design decisions and deviations.

## Commands

```bash
# The checks the repo gates on (black, ruff, mypy, pytest); CI also builds the zip:
tox

# Individually (venv from requirements-dev.txt):
pytest tests/unit -q
pytest tests/unit/test_sync_writers.py::test_movie_write_full_fidelity   # single test
pytest tests/unit/test_sync_writers.py -k piers                          # one schema leg
black --check --diff .
ruff check .                                                             # rules in pyproject.toml
mypy                                                                     # config in mypy.ini

# Dev loop against the local Kodi:
tools/dev-install.sh        # rsync working tree into ~/.kodi/addons and reload
tools/build.py [OUTDIR]     # Kodi-installable zip (default ./dist)
```

Live verification runs against a real Kodi per the scenarios in `docs/testing-plan.md`; evidence
lives in `tests/live/results/` (gitignored). Configure the target in
`~/.config/kodi-drive/targets.env` — see `kodi-connect`, and never put a host or credential in
this file.

A service-only change needs an add-on disable/enable bounce; new `strings.po` ids need a **full
Kodi restart**, because Kodi caches add-on strings for the process lifetime
(`kodi-process-control`).

## CI and releases

GitHub Actions mirrors the pvr.kofin split (quality gate vs tag release):

- `.github/workflows/ci.yml` — on every PR and every push to `main` or `kofin-or`: `black`, `ruff`, `mypy`, and `pytest tests/unit` as separate Checks jobs, plus a `package` job that uploads a Kodi-installable zip artifact (`plugin.video.kofin-<ver>-prN-<sha>.zip` / `…-main-<sha>.zip`, 14-day retention). Live tests are not run in CI. All five jobs are required on `main` and `kofin-or`, on a branch that is up to date with its base, so run `tox` (the first four) before every push and on every branch of a stack.
- `.github/workflows/release.yml` — on a `v*` tag: re-runs `black`, `mypy` and the unit tests (not `ruff`, which only `ci.yml` runs), builds `plugin.video.kofin-<ver>.zip`, asserts the tag matches `addon.xml`, and opens a **draft** GitHub release whose body is the top paragraph of `changelog.txt`.
- `.github/workflows/kodi-schema-watch.yml` — every two days: runs `tools/check_kodi_schema.py` against `xbmc/xbmc` and opens one issue per database schema version newer than the gate, deduped on the issue title against open *and closed* issues labelled `kodi-schema`. It reports, it does not admit — see **The schema gate** below for what admitting one costs.

### Cutting a release

1. Bump `version="X.Y.Z"` in `addon.xml`.
2. Prepend a new top entry to `changelog.txt`:
   ```
   vX.Y.Z
   - <bullet>
   - <bullet>

   v…
   ```
   The top paragraph (up to the first blank line) becomes the release body, and on this branch the workflow adds the link to the wiki's API sync variant page beneath it.
3. Commit, merge to `main`, wait for CI green.
4. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.
5. Review the draft (`gh release view vX.Y.Z`), then **publish it yourself** —
   `kodi-addon-release` explains why a workflow cannot.

PR zips are Actions artifacts only; production install zips come from published GitHub releases.

## Architecture

**Two processes, three entry points.** `default.py` → `lib/kofin/plugin/router.py` handles every `plugin://` invocation (browse, play, settings buttons); `service.py` → `lib/kofin/service/main.py` is the long-running background service (sync, websocket, player monitoring, SyncPlay); `context_*.py` are context-menu shims. The plugin process is short-lived and must stay thin — anything stateful belongs to the service.

**Cross-process traffic is closed-world.** `core/ipc.py` is the registry of every NotifyAll message kofin sends — nothing may notify a string not declared there. `core/state.py` holds the *only* shared live state (a handful of window properties); anything else "must argue its way into this module". This replaces the fork's ~30 ad-hoc window properties.

**No module-global state.** Service restart tears down and rebuilds objects in-process (no module reload). Module-level mutable state breaks that path. The few exemptions (schema discovery cache, the shims monitor) carry in-place comments explaining why they are restart-safe; new globals need the same argument or a different design.

**SQL/API branch boundary.** Phase 1 keeps shared metadata/policy, orchestration and private storage under their existing paths while moving native operations behind `sync/backends/sql/`. `sync/private.py` is the only private database entry point for shared callers. `buildconfig.py` selects the branch's backend; `tools/package_manifest.py` controls both ZIP and development installation. The internal API profile physically omits native SQL modules and gates unfinished features. See `docs/research/kofin-or/phase1/README.md` for contracts, verification and the remaining native-ingestion work. A shared change lands on main and is ported to kofin-or with `cherry-pick -x`; OR-specific profile/version changes stay separate.

**The transplant boundary.** `lib/kofin/sync/writers/`, `lib/kofin/sync/kodidb/`, `fields.py`, `obj.py` and `lib/kofin/syncplay/` are ported fork code; `lib/kofin/sync/shims.py` provides the fork-compat helpers they import. They keep the fork's *shape* — the `obj` dict mapped from `obj_map.json`, the positional `*_obj` spec lists fed to `values()`, one writer method per media type — so a change can still be read against the fork. They do **not** keep the fork's semantics where the fork was wrong: about half the lines in `writers/movies.py`, `kodidb/movies.py` and `kodidb/music.py` post-date the port, each deviation annotated in place ("Deviation from the fork: …") and pinned by name in the L2 suite. What protects correctness is therefore `test_sync_writers.py` across every gated schema — full-fidelity rows, byte-identical idempotency dumps, zero-orphan removal — not equivalence with the fork: the A/B harness (`tests/live/ab_diff.py`) ran once, on the day of the port, for movies only, and is a historical artefact. Any restructure inside the transplant is proven by the idempotency dumps (same rows before and after), and a new deviation carries its note and its L2 test. The pipeline began as a port and is now kofin's own, split along its seams in phase 2 (`docs/sync-refactor-assessment.md`, `docs/sync-refactor-phase2-plan.md`): `library.py` is the orchestrator (queues, the tick, the command table), with the widget-refresh policy in `refresh.py`, the clocks in `clock.py` (one `Deferred` primitive), the worker threads and the pager in `workers.py`; `full_sync.py` keeps the library queue and the one walk, with the restore points (`restorepoints.py`), the update-mode plan (`prune.py`), the boxsets pass (`boxsets.py`) and the removal (`removal.py`) beside it, and the note above `Library.claim` naming what a sync needs from the Library (`tests/unit/synchost.py` is the one fake); `downloader.py` is the server-side paging; `views.py` keeps the view table and the regeneration hash, and everything it generates lives under `nodes/` (`fs.py` is the only code that deletes there) and `playlists.py`. Shell code (`core/`, `plugin/`, `service/`) follows current idioms; expect two dialects and match whichever file you are in.

**The schema gate.** `sync/schema.py` refuses to write any Kodi database version not in its `SUPPORTED` map (currently Omega MyVideos131/MyMusic83 and Piers MyVideos146/MyVideos147/MyVideos148/MyVideos149/MyMusic84). Supporting a new Kodi version means: dump `.schema` fixtures from a real untouched install into `tests/fixtures/`, extract creation-time seed rows, add the version to the L2 parameterization in `tests/unit/kodifixtures.py` + `test_sync_writers.py`, confirm the suite passes, then open the gate. Version-dependent constants (e.g. `EXTRA_ITEM_TYPE` — Piers renumbered Kodi's VideoAssetType enum) are keyed in `schema.py`, never inlined in writers. A bump that provably changes no DDL can reuse the previous version's fixture, but only with the upstream evidence written down — `docs/myvideos147-gate.md` is the worked example. Do not reach for that shortcut by analogy: every Piers bump since has moved DDL, each time by adding a `streamdetails` column — 148 `iSource`/`iVersion`, 149 `iFlags`. `docs/myvideos148-gate.md` is the worked example of the full path, including why a NULL `iSource` is not a regression and what writing it deliberately would buy; `docs/myvideos149-gate.md` follows it, and says why a NULL `iFlags` costs nothing at all. `test_sync_schema.py` refuses any `SUPPORTED` entry lacking a fixture and its keyed constants.

**A schema version is frozen for the life of a stable series, and moves constantly before it.** 19.0–19.5 all ship MyVideos119, 20.0–20.5 all ship 121, 21.0–21.3 all ship 131 — a point release has never bumped one. Within a development cycle it moves several times: Piers went 137 → 139 → 143 → 146 → 149 across a1…b2, with MyMusic 83 → 84 at a3 and Textures 13 → 14 at a1. So the watcher only has to follow the *development* line, which is `master` until Kodi 22 branches (there is no `Piers` branch yet — 22.0b2 is tagged on `master`); released branches are probed because it costs one HTTP request and proves the freeze, not because they are expected to move. Branch discovery is by codename pattern (`^[O-Z][a-z]+$`), so Piers and its successors are picked up without an edit. The constant itself is *not* stable: it has moved file twice (`VideoDatabase.cpp` on Omega, `VideoDatabaseMigration.cpp` on master), which is why a probe that cannot find it **fails the run** instead of reporting nothing new.

**Three databases.** Kodi's own MyVideos/MyMusic (written directly, explicit column lists everywhere so additive schema changes are harmless); `kofin.db` mapping Jellyfin ids to Kodi ids with checksums for idempotency; `sync.json` as the resumable sync queue (entries leave only on success or a server-side 404). The chapter-thumbnail feature additionally writes Kodi's texture cache (Textures13/14) at playback time, behind the same schema gate (`sync/kodidb/texture.py`, seeded and reverted per play by `service/chapters.py`).

**Tests are layered.** L1 units run against Kodistubs plus fakes in `tests/unit/fakes.py`. The L2 writer suite (`test_sync_writers.py`) runs the real writers against pristine databases built from the checked-in schema dumps, parameterized over every gated schema (`[omega]`/`[piers]`/`[piers147]`/`[piers148]`/`[piers149]` ids) — full-fidelity, idempotency (byte-identical dump), and zero-orphan removal invariants. Live gates per phase are tracked in `docs/testing-plan.md`.

## Constraints that are easy to re-break

Generic Kodi constraints that used to live here are now in kodi-drive, and are not repeated:
`<reuselanguageinvoker>` placement and plugin-handle hangs (`kodi-addon-manifest`,
`kodi-plugin-handles`), `xbmcaddon.Addon()` during an update and overlapping service generations
(`kodi-addon-lifecycle`), the string cache (`kodi-process-control`), bare-number node labels and
`folder.jpg` (`kodi-library-nodes`), texture-cache re-encoding and chapter cache keys
(`kodi-texture-cache`), never writing the library through JSON-RPC and the MyMusic trigger clock
(`kodi-database-writing`), `PlayMedia`'s resume flag and subtitle ordering
(`kodi-playback-resume`), `UserDataChanged` fan-out and the 10.9 route migration
(`jellyfin-client`), and settings `<dependencies>` on a `list[string]` (`kodi-addon-manifest`).

What remains is kofin's own:

- Generated nodes live under `special://profile/library/video/kofin/` and **every deletion path is
  gated on the `kofin` name prefix** — hand-made nodes share the tree.
- Generated playlists live in a `Kofin/` folder under Kodi's own `playlists/video/` and
  `playlists/music/` — **capital K** — with three ownership rules. Under `playlists/video/Kofin/`
  the `kofin` prefix still gates `.xsp` deletion and a foreign non-m3u8 file is spared
  (`views.py`); unmanaged `.m3u8` in that folder are Jellyfin video playlists and are pruned
  (`playlists.py`). Under `playlists/music/Kofin/` the **folder** is the boundary — the
  managed `.m3u8` files are named after the Jellyfin playlist's title, carry no prefix, and every file not in
  the managed set is removed on reconcile. Nothing of the user's may be told to live in
  either music `Kofin/` or as a `.m3u8` under video `Kofin/`.
- **A video playlist line is `CVideoDatabase::ConstructPath`, not path+filename.** Plugin
  rows store the full plugin URL in `files.strFilename`; joining it onto `path.strPath`
  doubles the URL and Kodi will not match the library row — no artwork, duration or DBTYPE
  until playback stamps a ListItem. `video_playlist_line` returns the filename unchanged
  when the path is `plugin://` (or the filename is a stack).
- **A playlist rewrite keeps the stored filename while the title still matches,**
  including a disambiguated `Name (2).m3u8`. `_unique_stem`'s taken set is collisions
  for *other* names. Seeding it with our own stem made every membership rewrite pick
  `Name (2).m3u8` and ping-pong; recomputing the stem when the base name is not yet
  reserved renames `Name (2).m3u8` onto whoever owns `Name.m3u8`. `apply_one` and
  `reconcile` both go through `assign_playlist_filename`.
- Every show's path row carries `strContent='tvshows'` + `metadata.local` **and**
  `useFolderNames=1`, and the episode object repeats the stamp.
- **A boxset pass ending with zero linked members must not stamp its reference checksum** —
  `writers/movies.py` writes it NULL on purpose, and the NULL is what drives the drift probe.
- Reference checksums have exactly one spelling: `fields.reference_checksum`. Never inline
  `"|plugin"`; the feed and prune comparators used to, and agreed with each other while both
  being wrong.
- The backdrop is `fanart.webp` and **every part of that name is load-bearing**
  (`service/backdrop.py`, `core/api.py::splashscreen`, `plugin/browse.py`).
- Extras/videoversion writes read the VERSION itemType from the seeded 40400 row and the EXTRA
  value from `schema.EXTRA_ITEM_TYPE` — both differ across gated schemas.
- **MyMusic's `versiontagscan` holds exactly one row, and the wipe puts it back.** Kodi's
  `GetMusicNeedsTagScan` returns -1 for any other count, and the music window then asks to
  "rescan tags from files" on every visit; accepting is a `SCAN_RESCAN` of every music path,
  which re-reads every directory a plugin source has (hours for 1,557 album folders on a
  tablet). Kodi's own stamp after that rescan is an UPDATE, so an empty table never heals.
  `schema.MUSIC_SEED_SQL` carries the row per version and `test_sync_schema` refuses a seed
  set that names another.
- **A single-file movie's `MediaSource.Name` is its file stem, not a label.** Jellyfin's
  `GetMediaSourceName` returns the file name without extension unless local alternate versions
  exist (then the folder prefix is stripped and a suffix label remains). `resolve_version_type`
  maps a stem-named source to the seeded Standard Edition; minting a `videoversiontype` row per
  name put 1,799 entries in Kodi's "Manage versions" picker for 1,784 films, none versioned, and
  nothing sweeps that table but kofin's own `sweep_orphan_version_types` (Kodi's only deletes are
  its v128/v131 migrations). A rewrite or removal runs the sweep; Repair converts old profiles.
- **An empty listing against existing references is not a deletion order** — the rule lives in
  three places and every one is load-bearing: `boxsets.sweep_stale`, the prune's
  `get_existing_ids`, and `Views.get_views`, whose floor is gated on the whitelist (not on "no
  views", since a Live TV grant still lists one) and sits *before* the `SortedViews` stamp,
  because an empty stamp would regenerate an empty node tree. A withdrawn user gets `/UserViews`
  200 with zero items and a 403 on MediaFolders (executed on jf12, `tests/live/jf12_user_policy.py`).
  `Api._json` maps a 200 with no body to `{}`; that is **not** `{"Items":[], "TotalRecordCount":0}`.
  The prune map and the stale-id confirmation both refuse a shapeless body rather than read it as
  "none exist" (`test_get_id_etag_map_refuses_an_empty_body`, `test_get_existing_ids_refuses_a_shapeless_confirmation`).
- **`sslVerify` is one setting for every TLS socket**, HTTP and WSS. websocket-client 1.6.4 injects
  `CERT_REQUIRED` when `sslopt` is omitted, so a self-signed server with verify off listed fine and
  the websocket never came up. `WSClient` and the SyncPlay time-sync socket pass `sslopt` on `wss://`.
- **`ATTACH_SUBTITLE` is in `ipc.GUARDED`.** It is tens of seconds of server-side ffmpeg on the
  playing session; the dialogs (`PRECACHE_ART`, `SYNCPLAY_MENU`, `WHO_IS_WATCHING`) stay unguarded
  on purpose.
- **Play-queue files under addon_data are mode 0600.** The payload's `Path` is the stream URL, and a
  transcode URL carries `api_key=`. Same owner-only write as the IPC nonce.
- **A TranscodingProfile is a device statement, not a spare tyre.** Jellyfin's `StreamBuilder`
  *ranks* the transcoding profiles instead of taking the first that matches, and one whose
  `VideoCodec` list holds the source codec ranks top so the server can stream-copy into it. So a
  codec offered there is a codec the server may hand back untouched, whatever `DirectPlayProfiles`
  says — kofin's unconditional fMP4/av1 leg answered `VideoCodec=av1` + `-codec:v:0 copy` for a
  device that had just refused av1, from *second* place in the list. Every codec in
  `_transcoding_profiles` must be gated on the same list that gates direct play; reordering
  cannot substitute for withdrawing it.
- `downloader._get_items` **must fail loudly**: it submits every page up front, so a swallowed
  error yields zero pages, which reads downstream as an empty library rather than a failure.
- **A downloaded item plays from disk however it is reached, online included.** The repoint
  (`downloads/repoint.py`) only covers plays that go through Kodi's own row, so `plugin/play.py`
  is the only place that can enforce it for anything playing by *id* — a kofin listing, a widget,
  a SyncPlay group start. The preference was offline-only once, and the plan's "downloaded items
  never reach the plugin" is what SyncPlay disproved: the initiator adopts the group queue for
  the playback it is already running (`syncplay/manager.py`), so only the follower reloads, and
  it streamed media the initiator was playing locally. `resolve_downloaded` pushes the claim
  itself rather than leaving it to `backfill_library_claim`, which needs a Kodi dbid off
  `Player.OnPlay` that a group start does not carry. A request naming a source, a track or a
  bitrate still streams — a download has only the tracks it was made with.
- **A worker that dies without `is_done` is still in the list.** Drain and spawn treat that slot as live, so a crashed `GetItemWorker` (Tab: three updated-download threads, never logged `--<[ q:download`) holds the watermark still and FastSync replays the same window forever. Reap `not is_alive()` after `start()`, and hold the watermark.
- **The library tick must not wait on the GUI.** `Window.setProperty` and `DialogProgressBG.update` wait on Kodi's app thread (`CEvent::Wait` / DelayedCallGuard). Publishing `kofin.sync.active` from every writer spawn froze the Bravia manager inside one tick (bar at 76%, drain never reached). `enable_pending_refresh` is a flag; the paint is async and skipped if one is already in flight; the watermark commits *before* any of that.
- **A single's album has no kofin.db row.** `song_add` creates it on the fly (`strReleaseType=single`, empty title) when Jellyfin never modelled an album, and the song mapping's `parent_id` is the kodi album id. Song removal has to take that empty unmapped album with it — the fork's wild-id lookup ran after it had already deleted the prefix it was looking for, so the shell stayed and Kodi rendered it as "Singles" under the artist. Startup `prune_orphan_singles` heals rows already left behind. A mapped MusicAlbum is someone else's remove.
- **A downloaded song's server path row is referenced by nothing while the download lives.**
  MyMusic has one `path` row per song and the repoint moves `song.idPath` off it, so any sweep
  of unreferenced rows — Kodi's own Clean library, or kofin's startup `prune_orphan_paths`
  before it read the mapping — takes it, and a restore by the stored id then leaves the song on
  a deleted row, which `songview` drops from every listing (four empty albums on the Bravia,
  2026-08-28). The prune spares rows kofin.db still maps, the repoint captures `restore_path`
  and the restore get-or-creates from it, and `song_update` re-resolves a missing row so a
  Repair heals old damage. Never put a song back by a path id alone.
- **A mapped kodi artist id whose row is gone is not a credit.** A MusicArtist written before it
  has albums is absent from `song_artist` ∪ `album_artist`, so Kodi's `CleanupArtists` deletes
  the row; kofin.db keeps the mapping. The next song rewrite then stamped `song_artist` at the
  ghost id and `prune_song_credits` dropped every live credit that still joined — Kodi's
  listings inner-join `songartistview`, so the album opened empty (Má Vlast on the Bravia,
  2026-09-16). `song_artist_link` / `artist_link` recreate the row (the same repair `artist()`
  already does when it runs) and refuse to add a ghost to `credited`. Startup
  `heal_missing_artists` puts the row back from the credit's denormalised name so a service
  bounce lists the album again without a Repair.
- **A song's artist credits are replaced on every rewrite, and an artist that arrives after its
  content re-credits it.** Jellyfin fills `ArtistItems` by looking the tag names up against the
  entities that exist at request time (a lookup, never a create) and materialises new entities in
  the scan's post-scan pass, so a track written between the two credits the album artist by
  fallback and the artist-added event that follows names only the artist — the track's Etag
  never moves (#188). `Music.relink_content` runs for a *new* artist row on the realtime path
  only (`self.library is None`; a full sync writes artists first and must never pay for the
  listing — an L2 test counts the queries), lists the artist's content once and rewrites the
  mapped albums and songs that lack its link with `force=True` through `check_unchanged`.
  `prune_song_credits` is what lets that rewrite drop the fallback credit: the fork only ever
  added to `song_artist`, and the incidental server-side recovery left every such track with
  two credits.
- **An album's album-artist credits are replaced on every rewrite.** `artist_link` only ever
  added (`INSERT OR REPLACE` keyed on artist+album never removes a row), so a credit
  `AlbumArtists` no longer carries — a MusicBrainz conductor left on the album after the
  server went back to the file tag — outlived every Etag change. `prune_album_artists` drops
  the stale `album_artist` rows and that artist's discography row for this title when no
  other album of the same name still links them. The song leg still *adds* album artists from
  a track's `AlbumArtists` (discography for an album artist absent from `ArtistItems`); it
  prunes only a single's synthetic album, which never passes through the album writer.
- **A scanner ListItem never stamps a zero resume point** (`metadata.build` passes
  `stamp_zero_resume=False` to `listitems.build`). Kodi's scanner persists any *set* point —
  `CBookmark::IsSet` is `totalTimeInSeconds > 0` — as a type-1 bookmark, and the in-progress
  smart-playlist rule is that row's existence, so the zero stamp a browse row needs (to mask the
  bookmark Kodi saved for the plugin path) put every imported movie in the in-progress widget
  until its detail patch cleared it.
- **`VideoLibrary.SetSourceContent` deletes rows only with `clearmode: "remove"`.** `"clear"`
  unbinds the scraper and leaves every row in place; the API backend's whole-library removal
  (`backends/api/removal.py::clear_library`) relies on `"remove"` being `RemoveContentForPath`
  on the library root, under which every row of that library is filed.
- **A library is cleared in one call, and the home widgets churn while it goes.** Removed
  fifty folders to a call, Movies took 186 s against 109 s on the P1D and 1,364 s against
  803 s on the Pi, and neither way stalled a Python thread (#308's question). What costs a clear is the widgets:
  every deleted row is a `VideoLibrary.OnRemove` without `transaction`, and every visible
  video widget lists itself again for each one (2,448 refreshes on the Pi, 803 s against
  480 s with none on screen). `tests/live/or_bench/README.md` has the runs.
- **Every show folder under a `tvshows/` root carries its own binding** (`Native.bind_show`,
  `containssingleitem`). Kodi takes a plugin path's parent to be the plugin *root*
  (`URIUtils::GetParentPath`), so a folder never inherits the root's binding and the scanner
  skips it — 79 shows, one listing, nothing imported on the P1D until the bindings existed.
  The API profile's manifest keeps all three `medialibraryscanpath` content types rewritten to
  `/native/` (`tools/package_manifest.py`); with only `movies` Kodi refuses tvshows outright.
- **Three things the API backend applies without a native row, on purpose:** an unnumbered
  special (season 0, no number — Kodi's scanner cannot file it, `ProcessItemByVideoInfoTag`), a
  season with no importable episode (`season_view` joins episodes, so `GetSeasons` never
  returns it) and a collection with no synced member. A show's `dateadded` is never set:
  `tvshowcounts` derives it from the newest episode file and `SetTVShowDetails` cannot move it.
- **The API backend writes `lastplayed` and `dateadded` in local time** (`metadata._timestamp`
  through `shims.convert_to_local`), as Kodi does for its own writes; a UTC string read an
  hour off and differed from every value a playback set. Calendar dates stay as given.
- **A scanner row that matches `metadata.details` needs no patch.** The 0.91.0 imports of
  1,788 movies and 4,316 episodes applied in 36 s passes that wrote nothing, where a pass that
  patches costs about 146 ms a row; a field that *always* differs after import is a serializer
  bug, not a reason to patch. Kodi logs no announcements — `patch.Applier.flush`'s "patching N
  rows" line is the only count of what a pass wrote. Acknowledgements commit by the batch
  (`patch.Applier.commit`): one-row commits were 3,685 database opens for a pass that sent
  nothing.
- **Nothing in the API backend is forgotten on the strength of an accepted call.** `removal.py`
  is driven by the kind table (`kinds.KINDS[...].removal`): a row kind is confirmed by one
  scoped readback, a child of a show that is going is acknowledged only when the show's removal
  is confirmed, and a set is acknowledged after the apply pass has unfiled its movies and
  `GetMovieSets` — which lists only sets a movie links (`GetSetsByWhere` groups `movie_view`) —
  no longer shows it. A collection without a Kodi row (its movies unsynced, or the one a movie
  in two collections did not take) is applied without a row, never left pending.
- **A native song's URL is a file in its album directory, never a query.** Kodi's music
  database splits a URL into path and file name with the options dropped (`URIUtils::Split`
  via `CMusicDatabase::SplitPath`), where the video database keeps a plugin URL whole; the
  first live import stored every song as its bare directory — unplayable, and matched by
  nothing on a rescan. `paths.song_url` builds `<album dir>/<item id>.<container>`, the
  router routes it to the play route, and `paths.parse_item` reads the id back out.
- **Music leaves Kodi only through a complete listing.** The audio API has no remove call and
  a music scan replaces exactly one directory (`RemoveSongsFromPath` is exact), so a song's
  tombstone is its album directory listed without it, and the provider's root listing names
  every directory that still has songs to shed (`store.tombstones`) until the scan has
  emptied it. `removal.remove_music` confirms by reading the directory back; albums and
  artists are let go with their songs.
- **The music provider hands over a complete listing or none, and never fails.** Kodi scans
  whatever rows reached it before a listing failed, however it failed, and `RetrieveMusicInfo`
  removes every song on the path first: a failed listing, an exception reaching `dispatch`
  (`endOfDirectory(succeeded=False)` with nothing added) or a partial one is the directory
  deleted. So a song directory is built in full and handed over in one `addDirectoryItems`,
  a malformed song is reduced to its title rather than dropped, and when the store cannot be
  read the fallback is Kodi's own rows read back *in full*, the paged `limits.total` proving
  the count (`provider._known_entries`); until the store or that readback answers,
  `_music_folder` waits (`RETRY_DELAY`) and leaves only at shutdown.
- **The pass finds applied songs Kodi lost.** Nothing pending names a song a half-failed
  listing, a stopped scan or a user's interrupted rescan removed, so `MusicPass.directories`
  compares every acknowledged song with the readback, puts the missing ones back to pending
  and into the pass, and scans their directory. Songs whose album's payload moved join the
  same comparison, because `song_tags` takes the album's artists and MusicBrainz ids.
- **`song_tags` is total.** It is both the listing and the hash the pass compares; a track
  number that does not parse is the missing-field zero (`metadata._int`), never an error, or
  one song skips the scan of every directory in its library on every pass.
- **A played song with no server date is last played on its `DateCreated`.** Kodi's
  `UpdateSong` stamps the current time on a played song with no date, which put sixteen
  songs marked played years ago at the top of a tablet's recently played albums on import
  day. The video setters leave a missing date alone and are unchanged.
- **Kodi orders recently added albums by `album.dateAdded`, which is scan time for a plugin
  source** (`song.dateAdded` comes from the file's timestamp, which a plugin path has not;
  no setter and no music ListItem key carries it). A first import in `folder_label` order
  (the server's `DateCreated`) lands the right order; every re-listing of a directory moves
  its album to the top. A user-accepted "rescan tags from files" re-stamps the whole library.
- **The pass holds the music scanner while it writes** (`native.hold`, `provider._hold`, the
  `kofin.native.hold.*` window property). Announcements made while the scanner is busy carry
  `transaction`, which the skin's music widgets skip; without the hold every song written
  re-ran four whole-library widget queries. It does not change Kodi's per-setter cost (nine
  autocommit statements, 46 ms a song on the P1D) and the video database has no such flag.
- **A music scan is a plan scanned a part at a time: never the library root, never a directory
  by name** (`musicpass.MusicPass.scan`, `walk.py`, `paths.MUSIC_PART`, `store.plan_walk`). The pass
  plans the album directories it wants listed, in listing order under their labels, and scans
  `/native/<key>/walk/<library>/<n>/`, each part a directory that lists thirty-two of them,
  under a parent of their own whose listing names every part of the plan. Both choices answer
  Kodi's directory cache, which `kodi-library-data`'s plugin-sources page describes: a root
  walk listed every album twice (a quarter of the scan), and a part placed below the music
  root was skipped with "does not exist - skipping scan" once the root was cached, as a new
  album scanned by name can be. On the LibreELEC Pi 3 Music-Alt's 87 directories scan in
  20.6 s where the root walk took 27.2 s, 12.0 s against 21.5 s with the database in WAL
  mode; Music's 1,557 in 292.7 s against 323.4 s. `ROOT_SCAN_SHARE` is the movies' alone.
- **No thread keeps an `xbmc.Monitor` it does not wait on.** Kodi queues a callback for every
  announcement on every Monitor object alive, in one queue for the whole process, and a
  callback leaves it only when the thread that made the object calls `waitForAbort` or
  `xbmc.sleep`; every other Python thread in Kodi walks past what is left on each wait of its
  own. The websocket thread held one across `run_forever`, for hours: a 22,381-song import
  fell from 60 songs a second to 14 and was still falling, each album listing taking 0.5 s
  instead of 0.04 s, with the service threads of five add-ons spinning. It now makes one for
  the wait between attempts (`core/ws.py`). The pass's own burst of setters does the same to
  itself, so `Applier.flush` waits a millisecond on the pass's Monitor after every batch
  (`Native.pump`; a zero timeout is "for ever"). `abortRequested()` does not take callbacks
  off; a thread that only polls it needs no Monitor of its own.
- **The library thread holds one connection to kofin.db for as long as it runs**
  (`private.hold_connections`, `Library.run`). A `Database` block that opens and closes a
  connection is 26 ms and 15 ms on the Pi's card, against 0.04 ms for a statement on a
  connection already open: four of them a tick kept the idle service at 31.6 % of a core, and
  a playlist reconcile of 2,163 tracks took 4 min 37 s. The hold is the thread's and ends with
  the `with`, which is what keeps it out of the no-module-state rule; the plugin's
  process-wide pool is the other spelling and stays the plugin's.
- **The tick asks `has_pending`, never `pending()`, to test for work.** Loading every pending
  payload twice a second kept the idle service at a third of a core on 31,000 rows.
- **`forget_many` collects closed intervals once per batch.** Once per item, the "still
  pending" subquery scanned the item table: a whole-library music removal spent ten minutes in
  SQLite and held the store's write lock across a service restart.
- **The pass issues one scan at a time.** Kodi's `UpdateLibrary` builtin, which both
  `VideoLibrary.Scan` and `AudioLibrary.Scan` execute, *stops* a running scan instead of
  queuing the request. Two roots selected together on the LibreELEC box cancelled each other
  on every pass for twelve hours (`Finished scan … 17 ms`, no listing); `native.scan` waits
  for each scan before issuing the next.
- **A ListItem must outlive its InfoTag.** `getVideoInfoTag()` hands out a pointer into the
  item (`owned=false`) and the item's destructor deletes it, so a tag taken from a temporary
  (`xbmcgui.ListItem().getVideoInfoTag()`) writes freed memory on its first setter. The
  separator probe did exactly that on every API-build listing; x86_64 tolerated it, a 32-bit
  ARM Kodi segfaulted in `setGenres` twice. `test_api_metadata.py` refuses the pattern.
- **Neither a listing nor the pass holds a library's payloads.** 6,616 video payloads are
  58 MB of JSON and 200 MB of Python objects. The movies root listing, which loaded its
  1,788 before building a ListItem, grew Kodi by 100 MB in a minute on the 1 GB LibreELEC
  box and left it unable to answer ssh until it was power-cycled; the pass that followed
  would have loaded all 6,616 for planning. `store.pending_work` carries a payload only for
  a tombstone, records read theirs through `PayloadWindow` (200 ids a query, at most 512
  held), listings walk their scope through one, and the tvshows root reduces each episode to
  `metadata.episode_row` as it reads it. `store.records()` is still eager for the callers
  that need every row.
- **An empty premiere or aired date is no opinion, never a patch.** Kodi fills a missing date
  itself (its zero date on the P1D, which reads back empty; a sibling's date on the LibreELEC
  box, which does not) and `SetEpisodeDetails`/`SetTVShowDetails` ignore an empty string, so a
  desired `""` never matches the row: 187 items re-patched on every pass. `metadata.details`
  drops an empty `dateadded`, `premiered` or `firstaired` key.
- **The API build's progress bar paints once a second at most and never per item**
  (`backends/api/progress.py`). `DialogProgressBG.update` waits on Kodi's app thread; the
  applier calls `step` per item and the bar decides whether to paint. The same setting passes
  `showdialogs` to the scan calls. Both settings must stay in the API profile's
  `settings.xml` (`tools/package_manifest.py`): `getSettingBool` on a setting the build does
  not define raises `TypeError`, and that failed every pass on the box once.
- **The API build's sync status is a file, never a setting.** The library thread writes
  `sync-status.json` when its state changes (`backends/api/status.py`) and the "Library status"
  button reads the catalogue when it is opened (`plugin/actions.py::library_status`);
  `syncStatus` and `syncedLibraries` are the SQL build's and are not in the API profile. The
  state is written by a listing as well as by the pass, with the names of the libraries
  listed for the first time: the catalogue has no row of one until its listing is published,
  and a first selection read "Done" for as long as the server was being asked.
- **A profile adds to settings.xml what the file cannot carry for both builds**
  (`tools/package_manifest.py`). `VISIBLE_WHILE` hides the three direct play lists under force
  direct play in the API profile only: on Kodi 21 a `<dependencies>` block on a `list[string]`
  setting unregisters the setting it names, and the SQL build still installs there. Hidden,
  the lists keep their values, and a context-menu transcode and the download decision still
  read them. `HELP` swaps a help string the same way: "Transcode music" rewrites no path in
  the API build, where it only decides whether the music bitrate limit applies.
- **Kodi's player owns the row of what it plays.** The server counts a play when it starts
  (and marks a song played there and then), so its user data for the playing item, written
  to Kodi during the play, was counted again by Kodi's own player at the end: one play of a
  song, two counts. It is held until the play is over **however it arrives**: as an event
  (`Library.apply_userdata`) or inside a full item, which is why a listing, a catch-up, a
  changed item and the outbox's re-read publish through `Library.publish` and never through
  `store.publish`. After the stop the server is asked what the play made of the item
  (`release_userdata`); the last event heard may be a progress report. The other three
  halves of the same fix: the watched mark goes to the server and never Kodi's count
  (`flush_local`), a count that only grew is not a local edit (`patch.local_edits`), and a
  stamp of Kodi's own up to six hours later than the server's, or up to ten seconds earlier
  (the stop report that follows Kodi's own stamp of a song's end), is the same play
  (`metadata.same_play`). `tests/live/userdata_matrix.py` is the gate, on scratch libraries.
- **The play-state outbox never stops at one edit, and an edit of an item the server no
  longer has is dropped** (`Library.flush_local`). Flushed in order and stopped at the first
  failure, one edit of a deleted item (404) failed every tick for ever: three edits behind
  it never reached the server, the rows waiting on them stayed pending, and a television
  ran two 20 s passes a minute for as long as it was on, each one pinging the companion.
- **No stream flags are asked of the companion for a library filed under an older scanner
  format, or one being rebuilt** (`companion._metadata_requests`). Those rows have no
  stream rows for a flag to land on, and every request was refused on every pass: 5,632 of
  them on the same television until its libraries were repaired.
- **An unplayed song has no `lastplayed` opinion** (`metadata.userdata`). Kodi's song setter
  cannot clear the date, so a desired empty one never matched the readback and failed every
  pass for ever.
- **A show is refreshed with `refreshepisodes: false`** (`patch.Applier.refresh`). With `true`
  Kodi deletes the show and imports it again under new ids, which is what filled the
  in-progress widget with shows after a cast change. A row's acknowledgement keeps a hash per
  part of the refresh token (`metadata.refresh_parts`, built from `refresh_fields`, the one
  dict the token hashes), so the log says which part moved.
- **A library leaves with a scan of nowhere: never `UpdateLibrary(video)`, and never a clean**
  (`removal.py`, `native.library_event`). The builtin with no path lists every folder of the
  libraries that stay. A clean is worse: its `directory` narrows only the files it tests, and
  the stage after asks the add-on about **every bound folder of every library** and deletes
  the path row of one that goes unanswered. With one reusable interpreter, a second plugin
  request in the middle (kofin's own music removal, on the P1D) left one of 1,869 questions
  unasked, and a movie of a library that stayed kept its row and left every listing. The
  sets a removed library emptied go to the companion (`_release_sets`); without it they
  stay, unlisted. `finish_sets` still cleans the root for a removed collection when there
  is no companion, and has the same stage in it. Only the shows Kodi could have merged with
  a leaving one are invalidated (`_merged_shows`), not every show, and the clear runs on a
  thread of its own for the bar's sake, where a stop waits for it.
- **A single-source item's streams are on the item, not on its source** (`metadata.compact`,
  `_stream_source`): read off the first source alone, every such row was filed with a stub
  video stream and no audio or subtitle. Streams have no setter, so `SCANNER_FORMAT` numbers
  what a listing carries and a **manual** Repair rebuilds a video library filed under an
  older one (`Library.rebuild_stale`): never a pass, since the library is gone from Kodi
  until the import has run. The rebuild is on record in the store (`store.REBUILDING`) and
  no path lists the library while its old rows are still leaving.
- **Album and artist artwork is applied between the music scan and the video scans**
  (`kinds.MUSIC_FIRST`, `Native.reconcile`: `scan_music`, `plan_video`, the early kinds,
  `scan_video`). With no video scan to wait for, the kinds keep their order and the music
  hold is taken once.
- **A viewer's request has one bar from the request to the end of the work**
  (`progress.Progress.attend`, `Library.settle_progress`). `close` ends a stretch and leaves
  an attended bar up; only `finish` takes it down, and a retried command does not attend
  again. A stage that counts nothing says so with one of Kodi's own strings.
- **A library episode's own art is its thumb, and only when it has one** (`metadata.own_art`).
  Given its show's poster, a skin that asks for the poster first showed it on every episode.
  Cast pictures have a cap of their own, 400 (`listitems.person_query`).
- **The chapter image workers do not use the service's HTTP session** (`ChapterArt._downloads`):
  three at once on the one `requests` session the library thread is on is what the sync
  avoids by giving each worker a client.
- **A music scan never shows Kodi's dialog.** With `showdialogs` the music scanner starts its
  `MusicFileCounter` thread, which lists every directory a second time, concurrently, to size
  Kodi's bar: two root listings of 62 s side by side on the LibreELEC box, and every album
  listed twice for the rest of the scan. `native.scan` passes `showdialogs` for video only;
  the kofin bar covers music.
- **A listing's cost on a small device is the interpreter and the page cache, not the
  query.** On the box one album listing took 45–67 s doubled and thrashing, 0.25 s alone with
  the interpreter reused; the store's two queries were 0.01 s in isolation while a fresh
  connection's first statement waited 15 s behind the card re-reading evicted pages.
  `core/memory.py::release` hands the heap back after a publish and after a pass
  (`malloc_trim`); the provider and router log their timings at debug level
  (`native dispatch … after N s of imports`, `music folder …: N songs read in … built in …`).
- **A movie's URL sits in a folder of its own; the root never lists folders.** Kodi turns a
  plugin folder in a movies listing into a phantom `VIDEO_TS.IFO` (`ConvertDiscFoldersToFiles`
  via `CPluginFile::Exists`, unconditionally true), so the movies root lists every movie as a
  file under `paths.movie_dir`; Kodi files it on that directory's path row. A few new movies
  are scanned by folder name after `native.bind_folders` binds each folder — without
  `containssingleitem`, which would make the folder the movie — and a first import or a
  mostly-missing library scans the root once, as before. A profile from the one-directory
  layout needs the Movies library deselected and reselected: the old rows' URLs are not
  owned, and importing beside them would double the library.
- **A movie's version files and extras live in its folder, and only a scan of the folder by
  name lists them.** The root walk lists the movie's own file alone; the Kodi side of this (the
  phantom-disc probe, the NFO branch that never groups a plugin item, `AddVideoExtras`, the
  directory cache) is in `kodi-library-data`'s plugin-sources page, not here.
  `metadata.assets_token` names what the folder lists beyond that file, and the pass scans the
  folder when the token acknowledged for the movie differs (`native._asset_folders`) — a token
  that went empty included, so the token the apply pass acknowledges is always one a scan
  presented. The scan adds and never removes: a row the listing no longer names leaves through
  Clean, whose per-file `check_exists` is `provider.exists` (a version file exists while the
  movie still lists its source, an extra while the movie's payload still names its stem). The
  source is read from the request's query, never from `base_url` (Kodi's `argv[0]`, which has
  none): read from there, every version file existed. A version file is the movie's URL with
  `mediasourceid`; Kodi imports it as a movie of its own and the user groups it in the Versions
  Manager. `Readback.owns` accepts the movie's URL with
  or without the source, since the user may make a version the default; `Readback._prefer`
  lets the movie's own file win over ungrouped version rows and keeps them as `version_rows`,
  which leave with the movie on removal. Two rows by the movie's own file are still a
  duplicate identity.
- **An extra is `extras/<stem>.<container>`, and the stem is its whole identity.** Kodi names
  the extra by the path below the folder minus the extension, so the id cannot travel in the
  URL; `paths.extra_stems` gives each feature a stem unique within the movie (`Trailer`,
  `Trailer (2)`, in id order) and the listing, the token, `exists` and `resolve_extra` read
  that list and nothing else. The features travel in the movie's payload, and **every publish
  path attaches them** (`library.attach_extras`: inside `fetch_kind` for movies, and after the
  single re-fetches of `fetch_items` and `flush_local`), because a payload without them reads
  as a movie without extras and Clean would drop rows the server still has. `paths.parse`
  tells the extras directory, a file in it and a disc-probe folder below it apart: the
  directory is listed, a probe answers empty (`native._prepare_scan` lists the folder and its
  `VIDEO_TS/` and `BDMV/` through `Files.GetDirectory` right before the scan, so the directory
  cache answers Kodi's probe), and a file goes to the play route or fails as a play. The pass
  turns `videolibrary.ignorevideoextras` off once (`allow_extras`), with a toast, since Kodi
  drops the folder before looking at it while that is on. **The folder `extras/` is bound,
  `noupdate`, or Clean library deletes every extra unasked:** an extra's URL has no query, so
  Kodi files it under the add-on's root (`CVideoDatabase::SplitPath`), where a clean finds no
  binding and asks nothing. The scan that files a movie's extras binds the folder
  (`scan_video`), and `bind_movie_folders` binds those an earlier build filed once per profile
  (`apiMovieBindings` at `noupdate+extras`), so no pass reads every acknowledgement for them.
- **What stock Piers interfaces cannot do is published for the companion add-on, never done
  through SQL.** `sync/backends/api/companion.py` writes the open requests to
  `companion/requests.json` in the profile and sends `Companion.Requests` over
  `JSONRPC.NotifyAll` (`core/contract.py`) whenever the set changes; the first request is a
  movie's ungrouped version rows (`Readback.versions_in`). A request leaves the document when
  the readback no longer shows the rows it named, so there is no acknowledgement to forge or
  lose; the companion's presence is read from `Addons` into `api-capabilities.json`, and its
  absence is logged as the gap it leaves. The companion is `service.kofin.companion`
  (kontell/service.kofin.companion), built to `docs/companion-addon-plan.md`.
- **The companion contract is one document of desired state with two writers and one lock.** The pass replaces its kinds (`group_versions`, `retire_assets`, `episode_tags`, `season_plot`, `stream_flags`, `remove_set`) and a playback replaces `chapter_art`; a rewrite is judged by every request's content, not its id, so a desired set that moved under a stable id is published. A kind with no public read (`companion.ACKED_KINDS`) is acknowledged through the companion's status by content hash, kept on the mapping (`store.annotate_companion`) so the request leaves until the content moves; the acknowledgement survives a re-plan of the row (`patch.plan` carries `applied["companion"]`). What those kinds read comes from the mapping, never a payload: `patch._with_companion_fields` keeps an episode's favourite while it holds, and a season's plot and a file's stream flags beside the acknowledgement, and `asset_files` the movie's version and extra URLs with the label the listing gave each version (the empty entry once a movie that had assets has none). A plot or flags that went are not dropped like the favourite: the companion's withdrawal unlinks a tag but leaves a plot and flag bits standing, so a row that carried either keeps the cleared value (an empty plot, a zero per stream) and asks for it until that very request is acknowledged. A set's request carries its journal stamp, so the `done` of an earlier removal of the same title never closes a later one. An emptied set is journaled (`companion/sets.json`) for the companion when it is present, in place of the clean of the whole root. `companion-schema.json` is the shape both test suites check.
- **Chapter art is staged by kofin-or and written by the companion.** At the play claim of a direct video play, `chapterart.ChapterArt` downloads the server's images for the chapters the player lists into `companion/chapters/<request>/`, publishes `chapter_art` with the exact dynpath, confirms through `Textures.GetTextures` (logged with the time since the claim) and withdraws on stop; a generation start withdraws what a crash left. Kodi's own extractor refuses every http source and the dialog reads the texture database only, so a file without its row is invisible; the one scheme Kodi serves over HTTP without classing it a stream, `dav://`, does extract but makes Kodi fetch the stream's sibling files and parse the video as an EDL (probe of 10 October 2026), so it is not a route.
- **The API build's presentation is built from the catalogue, never from Kodi's tables.**
  `sync/dynamic.py` writes the node tree for whitelisted video libraries with filter rules on
  the library tag (`metadata.library_tag`, the tag `metadata.tags` puts on every row; the
  serializer owns the spelling and the presentation imports it) and the smart playlists
  beside it, publishes `library://` paths for them and `browse` paths for every other view,
  and takes the tree down by the `kofin` prefix when nothing is synced. A whitelisted music
  library gets its folder in the music tree (`kofinmusic<id>`, `nodes/music.write_music_nodes`),
  whose nodes filter on `path`, the folder its songs are filed under: a music `path` rule takes
  an album or an artist by its songs, and the SQL tree's Kodi music sources are never written
  here. The Manage menu reads a library's downloads from that folder's path. Blank login ids
  take the tree down only from a store that answered (`dynamic.music_entries`): an
  unregistered add-on and a settings document that did not load read blank too. Kodi's music nodes
  are seeded first, the skin entry stays Kodi's music root, as in the SQL build, and there is no
  Downloaded folder: a download keeps its plugin URL, so a rule on the downloads folder matches
  nothing (the Downloaded-music smart playlist has the same flaw). **Kodi's shipped video nodes are seeded into the profile
  before the tree is written** (`kodisetup.seed_default_nodes`): the moment
  `special://profile/library/video/` exists every `library://video/...` path resolves there
  and nowhere else, so a profile holding only `kofin/` listed nothing for
  `library://video/movies/` and the skin's categories rows on the P1D's Movies and Shows
  sections went empty (`kodi-library-nodes`). The seeded tree reached the home widgets only
  after a Kodi restart: `ReloadSkin()` re-listed `library://video/movies/` over JSON-RPC but
  the widget kept its empty listing (22.0b2), so an upgraded profile sees the rows after its
  next restart. A side's Jellyfin playlists are pruned, files
  and states, when its last library leaves the selection (`backends/api/playlists._drop_side`;
  `RemoveLibrary` marks the pass due), and a playlist file the resolver left short — an entry
  the pass had not filed yet — withholds its Etag checksum, because the server's Etag does not
  move when Kodi files the item. `sync/playlists.py` ships in both builds: its
  native database imports are deferred to the SQL refresh functions, and `apply_one` and
  `reconcile` take a `resolver`; the API one (`backends/api/playlists.py`) gives a line for an
  item the pass has acknowledged, the resolver URL Kodi filed the row under, and the label
  from the server's listing. Widget refresh is Kodi's own where Kodi announces: a setter that
  moves a play count, a last played or a resume point does, the music hold hides a pass's,
  and the first-content reload is the one builtin the pass fires. **A setter that changes
  only art or tags announces nothing** (22.0b2: `SetMovieDetails` writes through
  `UpdateDetailsForMovie` and `RemoveArtForItem`, and neither reaches `AnnounceUpdate`; the
  notification socket stayed silent on the P1D while an art key and a tag were added and
  taken off), and a home widget reloads on an announcement alone. So a pass that put a
  download's badge on a row or took it off ends with one scan of nowhere
  (`Native.announce_writes`, `Applier.badges_moved`): after its last setter, through `scan`
  so that a running scan is waited for and not stopped, and without Kodi's dialog. Other
  art-only patches still reach no widget until something else announces.
  **`libraryNodes` is the API build's one switch for all of it** (`dynamic.wanted`): off takes
  down the tree, the smart playlists and both `Kofin/` playlist folders, and publishes a
  synced video library as a browse entry (a synced music library keeps Kodi's music root,
  which is Kodi's). Only an explicit `false` is off — an empty read is a settings document
  that did not load, and off deletes files. `syncMusicPlaylists` stays the SQL build's; each
  profile ships one of the two (`package_manifest.DISABLED_SETTINGS`).
- **Every movie folder is bound, noupdate, or the info dialog has no cast.** Kodi's info dialog
  loads a library movie's full details, cast included, only when `GetScraperForPath` finds a
  scraper on the movie's own `strPath` (`CGUIWindowVideoBase::ShowInfo`); otherwise it shows
  the listing item's tag, which carries none, and a plugin path's parent walk never reaches the
  bound library root. A show's folder is bound, so shows were fine; every movie in a folder of
  its own (0.91.0) was not. `Native.bind_movie_folders` binds every movie folder of the catalogue
  with `noupdate`, which Kodi's own Update library skips — after every pass and, since a pass
  plans pending rows only, once per service generation when nothing is pending
  (`library.apply`), so an older profile's rows are bound without a Repair; once per profile
  every folder is re-bound with the flag (`apiMovieBindings`), for the folders an earlier
  build bound without it. A scan by name lifts the flag around the scan, which the scanner
  would otherwise skip too (`ignoreFolder` in `DoScan`), and puts it back in a `finally`.
  Observed on the P1D: Delicatessen's dialog listed no cast unbound and all 20 bound.
- **A download moves nothing of Kodi's in the API build.** `downloads/nativeport.py` is the
  manager's view of the library per build: the SQL port is the relocation and the stamps; the
  API port leaves the row's plugin URL alone (the resolver plays the local file however the
  item is reached), hands the badge and the "Kofin Downloads" tag to the pass by invalidating
  the item, its show and the show's seasons (`backends/api/downloaded.py`, the download state
  is an input of the desired state and joins `inputs_token`), reads Kodi's play state back by
  the mapped id, and leaves the local watched mark of a vanished download to the server
  round-trip that already follows it, since a setter would start the userdata echo cycle. The
  badge is on the movie or the episode and, as in the SQL build, on the episode's season and
  show; the season is the one of the episode's number (`Applier.downloaded_seasons`). The
  badge key is `kofindownloaded`: Kodi's `SetArtForItem` refuses an art type with a dot in it
  (they are the types derived from a parent, `tvshow.poster`), so the setter accepts
  `kofin.downloaded` and stores nothing. A skin therefore tests both keys, the SQL build's
  `ListItem.Art(kofin.downloaded)` and this one, or it shows no badge on one of the builds
  (skin.contuary read the first alone). The
  Downloaded-episodes node filters on a moved path and stays the SQL build's. The port's
  operations are `attached` (the file landed, or is still there at startup: SQL repoints and
  stamps and says whether the row moved; API hands the item to the pass and always says yes,
  so the startup reconcile re-plans every done file and a finish that died before its badge
  is healed) and `detached`. A season's download menu is answered through the catalogue
  (`downloads/store.py::container_states`): the catalogue files an episode under its series,
  so the season's downloads are the series' whose payload names the season.
- **A scope readback is minimal; the full row is read for an item whose state moved**
  (`kinds.MINIMAL`, `Readback.full`, `Readback.prefetch_full`). The scope carries what
  finds and owns a row and the userdata a viewer may have edited, nothing else; the plan
  reads the full row by id, or from one paged listing when at least a tenth of the scope
  (and 25 rows) is about to be compared, as after a first import.
- **A mapping that carries the payload hash and `inputs` token is acknowledged without a
  compare** (`metadata.inputs_token`: server, library, separator, set name, the seasons'
  hashes, the artwork cap and encoding, the local zone). It fires for a retried pass and a
  re-published item, never under Repair, whose point is the compare. Anything new that
  `details` reads must join the token or the short-circuit will keep a stale state: the
  art query was missing from it, so a moved cap would have frozen every old art URL.
- **The payload hash leaves out `UserData.Key`** (`store.payload_hash`). Jellyfin answers
  an album's key with the one its user data was saved under or with its release group's,
  depending on its user data cache, so the hash moved for the same 30 albums between
  enumerations and every repair rewrote them unchanged. `store.hash_matches` still accepts
  the whole-payload digest older acknowledgements carry; refusing it would have the next
  repair rewrite every row once. A hash that moves for a field no desired state reads is a
  rewrite of every such row on every repair.
- **A complete enumeration on a clock is opt-in** (`fullUpdateInterval`, days, off by
  default; `library.full_interval`). A first listing, Update library and a sync-queue
  retention overrun still list everything. Without KofinSyncQueue the catch-up reads save
  dates and sees no deletion, so on such a server a removed item leaves Kodi only through
  the schedule or Update library — the setting's help says so.
- **The first-run gate refuses what an earlier Kofin filed, and nothing else**
  (`Native.setup`, `native.LEFTOVERS`). Those rows are in no catalogue, so an import beside
  them files every item twice. They are told by where they are filed: video, and songs the SQL
  build transcodes, under the add-on's plugin URL; its other songs at the server's stream URL,
  `<server>/Audio/<id>/stream.<container>?static=true` (all 22,348 songs of the P1D's SQL
  profile and none under the plugin URL, so the path rule alone lets a music library
  through). The gate used to refuse any row at all, which shut out a profile with files of
  its own or another add-on's library for no reason: a row is ours by its URL and its unique
  id, before the first sync as after it. What such a library can still share with ours goes
  by name: a set of the same title takes our art and plot and holds a collection's removal
  pending while a movie of theirs lists it, and an artist is found by name alone.
- **A count asks for one row, never a zero-width page** (`Native._count`, `Native.setup`).
  `limits` with an end of 0 gets no row from the video library and every row from the audio
  library: the bar of a music import read its count that way every three seconds and was
  handed the library's whole song list each time (22,381 songs, 1.1 MB, 22.0b2 on the P1D).
- **A collection is planned in every pass that plans a movie filed under it, and a set is
  never acknowledged without a compare** (`Native.join_sets`, `patch.Applier.plan`). A set
  is Kodi's to make and to drop: it goes with the last movie that names it (the companion
  deletes the empty row, as a clean does) and a scan or a movie's patch makes it again from
  the name alone, bare, while the collection's payload has not moved. `sets.idSet` is a
  plain rowid, so the new row takes the number the old one had and the short-circuit above
  read it as the row it knew. 54 sets on the Bravia (a Repair's rebuild, cut short by a
  Kodi restart) and 54 on the P1D (Movies deselected and selected again) had no art and no
  plot, and only the repair pass after the next daily enumeration would have compared them.
  The collection is put back to pending before its movies are acknowledged, so a pass
  stopped between the two kinds still owes the set.
- **A first import is not a repair write.** The pass after every complete enumeration runs
  as a repair, and repair re-sends a row whose acknowledged hash moved; a row with no
  acknowledgement was just filed by the scanner from this very listing, so a matching one
  is acknowledged (`patch.Applier.plan`). Without that a tablet re-wrote all 6,200 rows of
  its first import. Music has the same rule in `plan_music`, where its absence sent a setter
  for every song of a first import (308 s of a 357 s pass for 1,353 songs on the Pi), and a
  song goes further: its desired state is its play count and last played, the readback shows
  both, and `UpdateSong` rewrites `dateAdded` to the moment of the call for a plugin path, so
  a song that matches is never written, repair or not.
- **Songs are applied last of the music kinds** (`kinds.ORDER`). An album or artist is found
  through the rows of its songs, not through their acknowledgements, and a played song is a
  setter each: with 7,873 of them first, no album had a cover until they were done.
- **A first import picks its music directories without reading a payload**
  (`MusicPass.directories`): a song with no row is missing whatever its tags say, and
  only a song with a row and an acknowledgement has a tag hash to compare. The picker and the
  applier share that hash and the album payloads for the pass (`MusicPass.tag_hash`,
  `album_items`).
- **A library is enumerated three pages at a time** (`library.fetch_kind`, `PAGERS`), each
  worker on its own client, taken in listing order with three in flight at most. What makes
  a listing complete is unchanged: every page reports the first page's total, is exactly as
  long as its place says and repeats no id, or the enumeration fails; a server that caps the
  first page is read a page at a time. 22,381 songs: 113 s to 45 s on the Pi.
- **Kodi's own databases are rollback journals unless something switches them, and this build
  may not.** The SQL build switches the databases it writes; here it is the companion's to do
  (`service.kofin.companion`, `upkeep.py`). The mode lives in the file, so a profile the SQL
  build ever wrote is already WAL, and only a database Kodi created is "stock". What the
  journal costs a write, and why every played song is a setter of its own, is in
  `kodi-performance` and `kodi-library-data`'s plugin-sources page. On the Pi's card it
  decides a music import: `SetSongDetails` 133 ms against 17 ms in WAL, a first Music-Alt
  pass 37.0 s against 21.7 s, and 7,873 played songs of 22,381 are 37 minutes of a 52-minute
  first import on a rollback journal and 9 of 18 in WAL; the library and its art are there
  after 14 and 9.
- **A video import's time is Kodi's, and what a listing carries sets it.** A first-import pass
  of 1,788 movies on the Pi is 1,490 s: 1,300 s Kodi's scan, 118 s binding every folder
  (`SetSourceContent`, 60 ms a call in either journal mode), 48 s planning and 13 s the sets,
  after 58 s of enumerating and publishing. A trace of the scanner half way through put a
  movie at 600 ms: about 300 ms waiting on journal commits and 215 ms in the name lookups
  `kodi-performance` describes, one per cast member and per tag. So a name added to every
  item's listing is a lookup on every item, quadratic over an import. The companion's WAL
  switch and its `ix_kofin_{actor,tag,studio}_nocase` indexes take the scan from 1,265 s to
  446 s; with the hold below, 259 s and a 424 s pass. Shows are the same shape: 79 shows and
  4,316 episodes scan in 1,292 s on stock Kodi and 294 s beside the companion's two changes
  (`tests/live/or_bench/README.md` has every run).
- **A season's art, name and plot ride with its show's listing; a season is sent a setter only
  for a change.** `metadata.build` offers each season's art on the show's tag under the
  season's number (`addAvailableArtwork(url, type, season=n)`) beside `addSeason`, and Kodi's
  scanner files both with the show's row (337 thumb, 337 fanart and 332 poster rows in `art`
  before the pass sent anything). A season read or write is a listing of the whole show's
  seasons inside Kodi (`kodi-performance`), so 327 setters and their readbacks were most of
  a 96–125 s season kind that is now 25 s. `apikodi`'s `files_season_art` is the artwork
  level: a season Kodi filed with less differs and is patched as before.
- **A small scope wanted whole is read in one listing** (`Readback.SMALL_SHARE`). The
  prefetch floor of twenty-five rows left every show's seasons, and every show of a dozen
  episodes, on the row-at-a-time path however much of the scope was about to be compared:
  761 details calls for a first import of 79 shows, at 50 ms an episode and 79 ms a season
  where one listing of the show costs as much as one of them. One changed episode of a short
  show is still read by its id.
- **A large video scan runs with Kodi's "Download actor thumbnails" off, and gives it back**
  (`Native.without_actor_thumbs`, `ACTOR_THUMBS_FLOOR`, `store.hold_setting`). What the
  setting costs the scanner per cast member, and why nothing is lost without it, is in
  `kodi-library-data`'s plugin-sources page. Off for the scan: 1,265 s to 1,078 s on stock
  Kodi, 446 s to 259 s beside the companion's indexes. The user's value is recorded **before**
  the setting moves and forgotten only after Kodi took it back, and every pass and every
  service start restores a record a stopped Kodi left; the first record wins, because the
  value read after a crash is the pass's own `false`. A refused hold leaves the setting
  alone, drops its record (Kodi kept the value, and a record left behind was given back
  over a later change of the user's) and the scan runs anyway. A refused restore keeps its
  record and fails nothing, since raised from `reconcile` it would fail every pass for as
  long as Kodi refused; `Native.settings_owed` has the library thread ask again every five
  minutes while nothing is pending (`Library.apply`), warned about once.
- **A pooled private connection leaves every block idle.** `Database.__exit__` reads
  `total_changes` as a delta from `__enter__` and commits a block that opened a transaction
  without changing a row, or the next listing on that interpreter ran inside it and a
  failure rolled both back while the write lock stayed held (`private.pool_connections`).
- **Listed artwork is capped and JPEG-encoded by default** (`listitems.art_query`,
  `maxArtResolution` 1080, `compressArt` on). Kodi decodes and re-encodes every image it
  caches whatever the source and caps the result itself, so only the server resizing first
  saves anything. Changing either setting changes every art URL, which re-caches the art and
  patches every row once.
- **An episode's desired art never carries `tvshow.*` or `season.*`.** Kodi reads those off
  the show and season rows and `SetEpisodeDetails` cannot make them true; `metadata.details`
  drops dotted keys and `patch.merge` neither sends, compares nor clears them, even when an
  older acknowledgement listed one as owned. Two episodes on the box looped on
  "readback differs: art" every 35 s after the artwork cap moved every URL but their show's.
- **A music directory Kodi skips after a stopped scan is salted** (`store.bump_salt`, once
  per directory and pass, folded into every listed song's date and the folder's root label).
  `CMusicInfoScanner::DoScan` stores the directory hash whatever `RetrieveMusicInfo` wrote,
  so a scan stopped mid-directory leaves a hash every later scan skips on.
- Widget refreshes are fingerprint-gated and command paths own their own
  (`sync/widgetstate.py`, `docs/widget-refresh-plan.md`).
- The wake-time FastSync on `GUI.OnScreensaverDeactivated` is **unconditional on purpose**: it is
  the only cover for a websocket that went half-open while asleep.
- Every property in `core/state.py` is shared across service generations — see
  `kodi-addon-lifecycle` for why two run at once, and treat that module accordingly.
- **SyncPlay converges position on commands, and between them only by confirmed tempo
  pulses through inputstream.tempo — never by a continuous loop against Kodi's own
  tempo.** The `Player.SetTempo` ladder was removed after measurement, not after taste:
  that actuator exists only while Kodi's sync-to-display is on, and the setting slaves the
  media clock to the panel, so a refresh rate that is not a whole multiple of the frame rate
  imposes a fixed rate error — 0.5 %–4.3 % across three Piers devices, the worst 14× the
  ±3 % the ladder could command (`docs/syncplay-drift-shakedown.md` §10). The actuator in use
  now is inputstream.tempo, which rate-shifts inside the demuxer and works with that setting
  **off**, where the same devices free-run within a few hundred ppm. `syncplay/tempo.py`
  issues one bounded pulse at a time, confirms it from the add-on's state file, waits a queue
  depth before measuring again, and gives up on a one-signed residual
  (`docs/syncplay-fine-sync.md`). An audio item, or a Kodi without the add-on, gets
  command-only sync, exactly as 0.19 did; a transcode has gone through inputstream.tempo since
  `165d686` (its `[ syncplay/align ] skipped: transcoding` line is the landing check, not the sync).
- **A live item's group position is on the source clock, ten days up.** The player clock of a
  live stream is session time and means nothing to another member; the engine reads a live
  member's position off inputstream.tempo's state line (`getTime() + (source_ms − player_ms)`,
  the broadcast's own PTS) and a proposer anchors the group `LIVE_DELAY_S` behind its reading,
  offset by `LIVE_PTS_EPOCH_S` on the wire so a source-clock anchor can never be mistaken for the
  session-time zero a member without the clock proposes. `claim_is_live` is the one spelling of
  live (kofin's own claim carries `Live`; a bus claim has no runtime), and a live item is never
  seeked — ffmpeg's HLS demuxer refuses a seek on a live playlist — only pulsed, and two starved
  forward pulses mean the live edge, not a fault (`docs/syncplay-fine-sync.md`).
- A sync thread that will not stop is a thread inside the HTTP retry ladder
  (`docs/library-thread-stop.md`); the two rules that follow are easy to undo.
- **Never call `xbmc.Player.stop()`** — use `core/kodirpc.py::stop_player`. Kodi's binding sends
  `TMSG_MEDIA_STOP` without a `DelayedCallGuard`, so it blocks on the app thread *holding the GIL*,
  and the app thread's stop path waits on MyVideos writes that a GIL-starved kofin thread may be
  holding. That is a deadlock with no recovery but a force-stop (issue #155, measured on Omega and
  Piers). `pause()` has the same missing guard but does not tear the player down, so it does not
  reach the wait — it is a stall risk, not a deadlock, and is deliberately left alone inside the
  SyncPlay transplant.
- `discography` has no unique index, so `INSERT OR REPLACE` never replaces
  (`kodi-database-writing` has the general shape; the repair path is kofin's).
- **"Recently added albums" asks `/Items/Latest`, never albums sorted by `DateCreated`** — an
  album's DateCreated is when the scanner last created its row, and a rescan re-creates rows in
  folder order (`docs/dynamic-libraries-plan.md` §3). The sync writes album `dateAdded` from the
  same field; that is a known, separate question.
- **"Play with transcoding" is gated on `ListItem.DBTYPE`, not on `kofin.id` alone** — every
  dynamic row carries the id, songs, albums and genres included, and the DBTYPE clause is what
  keeps the entry off them (`addon.xml`). Both context entries then need `kofin.id`-or-`DBID`
  **as well**: DBTYPE is set by whoever built the row, so any other add-on that stamps a media
  type satisfied the type clause on its own (`plugin.audio.kotome` stamps `musicvideo`, and both
  entries duly appeared all over it). A library row always has a database id and a foreign plugin
  row does not. It has to be an item-level test — `Container.FolderPath` answers empty on Home,
  where the active window is not a media window, so widget rows would escape it.
- **A listing row with no server position still stamps `setResumePoint(0, total)`** — a zero point
  with a total is "set, nothing to resume" to Kodi, which is what stops it falling back to the
  bookmark it saved for the plugin path; the *resolved* item in `plugin/play.py` must never be
  stamped with zero, for the opposite reason (`plugin/listitems.py` explains both).
- **A settings button that writes a setting has to close the dialog first.** `close=true`
  runs `SaveAndClose()` *before* the builtin, so the route writes with the dialog already
  shut; written into one that is still open, the value is reverted when the user backs out
  (`service/backdrop.py` keeps its state in addon_data for that reason). The two button
  dialects in `settings.xml` track this and are not interchangeable: every `<close>true</close>`
  button is the older `type="string"` form, and all three `type="action"` settings omit
  `<close>` on purpose, so their menus read as sub-dialogs. `plugin/serverpicker.py` closes,
  writes `serverAddress`, then reopens settings — the filled field is the confirmation.
  Observed on the Tab, both directions (`tests/live/results/S10/`): an external write into an
  **open** dialog shows the new value on screen while `settings.xml` still holds the old one,
  and Back discards it; the identical write with the dialog **closed** persists at once.
- The who's-watching shortlist packs three states into one string setting
  (`plugin/adduser.py`): `all`, an id list, or `none` for "feature off". **Empty is `all`, not
  off** — it is what every install predating the sentinels holds, and it is what an unreadable
  settings store reads back, so reading it as off would hide the root entry and strip the session
  on an add-on update. `is_enabled` is the only spelling of that test, and turning the feature off
  detaches the session's co-watchers (`detach_all`) because the picker is the only way off one.
- `kofin.menu.who` and `kofin.menu.syncplay` are the skin-facing mirrors of those two root
  entries (same gates as `plugin.browse.root`). Skins cannot read addon settings; the service
  publishes on `mark_ready` and whenever the two settings change.
- **The Advanced-tab `reuseLanguageInvoker` toggle rewrites addon.xml's
  `<reuselanguageinvoker>`.** The zip ships true; Kodi reads ExtraInfo at load, so a change
  needs a Kodi restart. An update restores the zip's true and the service reconciles a false
  setting back onto disk. Disable it when several skin widgets load `plugin://` paths at once.
- Docs in `docs/` use one line per paragraph — `tools/unwrap_md.py` fixes wrapped files.

## Translations

English lives in `resources/language/resource.language.en_gb/strings.po`; the other 26 locales are
**generated**, never hand-edited. Translations are authored into `tools/i18n/tr/<locale>.json` and
rendered by `tools/i18n/gen.py`. `tools/i18n/README.md` has the recipes and `PROMPT.md` is the
brief every translation is written against. They are machine translations, tagged
`pending native review` in each file header — leave that line until a native speaker has actually
looked.

Adding or rewording a user-visible string means touching 28 files, and the toolchain is what keeps
that mechanical: add the id to `en_gb`, add the key to all 26 JSONs (or to `PASSTHROUGH` if it is a
bare technical token), `gen.py --snapshot`, then `gen.py && validate.py && pocheck.py`, and commit
the lot together. `tr/_source.json` records the English each translation was made from, so a
**reworded** msgid fails the generator instead of silently keeping a stale translation — the drift
`pvr.kofin` had to repair by hand. `tests/unit/test_translations.py` runs the validators, so CI
catches a locale left out of step.

Help strings that quote another string's wording verbatim (`#30794` quotes `#30618`; `#30080`
quotes `#30817`; `#30826` quotes `#30052`; `#30612` quotes `#30841`) and `pocheck.py` enforces all of them —
translate such a pair together or the help names a control that is not on screen under that name.
The quoted label goes in ASCII double quotes even where the locale uses its own quotation marks
elsewhere in the same string, because that is what the check looks for. `#30624`/`#30626`/`#30631`/`#30633`/`#30635`
substitute an item *name* where their plural partners take a count; the category noun in "Dune movie
added to library" is what separates the film from the album.
