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

Kofin (`plugin.video.kofin`) is a Jellyfin client add-on for Kodi that files selected server libraries into Kodi's own library through Kodi's public interfaces: it is the scanner's source for every item (the `/native/` provider) and patches what a listing cannot carry over JSON-RPC, so Kodi's databases are never opened. Playback resolves back through `plugin://` paths. The shell (entry points, settings, lifecycle, playback decisions) is kofin's own; SyncPlay is a port from the Kontell fork of `jellyfin-kodi`; the catalogue-and-pass pipeline under `sync/backends/api/` replaced the fork's database writers in 0.9x (`docs/kodi-api-sync-feasibility.md`, `docs/kofin-or-implementation-plan.md`). Until 0.95 the add-on also shipped a build that wrote those databases directly (the `sql` branch, frozen at 0.29.2 for Omega); nothing of it remains in this tree. The per-phase plans in `docs/` record design decisions and deviations.

## Commands

```bash
# The checks the repo gates on (black, ruff, mypy, pytest); CI also builds the zip:
tox

# Individually (venv from requirements-dev.txt):
pytest tests/unit -q
pytest tests/unit/test_api_lifecycle.py -k first_import                  # one test
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

### Branches

`master` is the only developed branch; its versions run from 0.95. Two frozen branches share the add-on id for older Kodis, and the Kontell repositories serve each Kodi version its branch (`addons.toml` in repository.kontell):

- `sql` — the SQL build, which wrote Kodi's databases directly: Omega onwards, versions below 0.90, frozen at 0.29.2 and no longer maintained.
- `api` — the API build with the companion add-on, Piers only, 0.90 to 0.94.x, archived.

**A version never leaves its branch's range.** After a Kodi upgrade a device takes the new Kodi's build only if its version is higher than the one installed, so sql < api < master holds for good, and the repository's publisher fails a release that falls in no range. Nothing is cherry-picked between the branches any more.

GitHub Actions mirrors the pvr.kofin split (quality gate vs tag release):

- `.github/workflows/ci.yml` — on every PR and every push to `master`: `black`, `ruff`, `mypy`, and `pytest tests/unit` as separate Checks jobs, plus a `package` job that uploads a Kodi-installable zip artifact (`plugin.video.kofin-<ver>-prN-<sha>.zip` / `…-<branch>-<sha>.zip`, 14-day retention). Live tests are not run in CI. All five jobs are required on `master`, on a branch that is up to date with its base, so run `tox` (the first four) before every push and on every branch of a stack.
- `.github/workflows/release.yml` — on a `v*` tag: re-runs `black`, `mypy` and the unit tests (not `ruff`, which only `ci.yml` runs), builds `plugin.video.kofin-<ver>.zip`, asserts the tag matches `addon.xml`, and opens a **draft** GitHub release whose body is the top paragraph of `changelog.txt`.
- `.github/workflows/kodi-schema-watch.yml` — every two days: checks out `sql` and runs its `tools/check_kodi_schema.py` against xbmc's Piers branch, opening a `kodi-schema` issue when Piers bumps a database past the sql gate. It lives on `master` only because a schedule fires from the default branch; delete it once Piers 22.0 ships.

### Cutting a release

1. Bump `version="X.Y.Z"` in `addon.xml`.
2. Prepend a new top entry to `changelog.txt`:
   ```
   vX.Y.Z
   - <bullet>
   - <bullet>

   v…
   ```
   The top paragraph (up to the first blank line) becomes the release body, and the workflow adds the link to the wiki's Kodi versions page beneath it.
3. Commit, merge to the branch being released, wait for CI green.
4. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.
5. Review the draft (`gh release view vX.Y.Z`), then **publish it yourself** —
   `kodi-addon-release` explains why a workflow cannot.

PR zips are Actions artifacts only; production install zips come from published GitHub releases.

## Architecture

**Two processes, three entry points.** `default.py` → `lib/kofin/plugin/router.py` handles every `plugin://` invocation (browse, play, settings buttons); `service.py` → `lib/kofin/service/main.py` is the long-running background service (sync, websocket, player monitoring, SyncPlay); `context_*.py` are context-menu shims. The plugin process is short-lived and must stay thin — anything stateful belongs to the service.

**Cross-process traffic is closed-world.** `core/ipc.py` is the registry of every NotifyAll message kofin sends — nothing may notify a string not declared there. `core/state.py` holds the *only* shared live state (a handful of window properties); anything else "must argue its way into this module". This replaces the fork's ~30 ad-hoc window properties.

**No module-global state.** Service restart tears down and rebuilds objects in-process (no module reload). Module-level mutable state breaks that path. The few exemptions carry in-place comments explaining why they are restart-safe; new globals need the same argument or a different design.

**One backend.** `sync/backends/api/` is the library pipeline: `store.py` the catalogue (kofin.db), `library.py` the thread, `native.py` the pass, `provider.py` the scanner's source, `metadata.py` the serializer, `patch.py` the comparator, `readback.py`, `removal.py` and `kinds.py` the kind table. `sync/private.py` is the only entry point to kofin.db. `tools/package_manifest.py` lists what the zip and the development install ship, and both carry the whole tree. `docs/research/kofin-or/phase1/README.md` records the contracts the pipeline was built to.

**What is still a port.** `lib/kofin/syncplay/` is ported fork code and keeps the fork's shape so a change can still be read against it; `sync/shims.py` is what remains of the compat layer the fork's writers imported, three helpers. The database writers, the sync orchestrator and the L2 schema suite that proved them went with the SQL build. Shell code (`core/`, `plugin/`, `service/`) and the pipeline follow current idioms; match whichever file you are in.

**Two stores, and none of Kodi's.** `kofin.db` holds the catalogue (payloads, placements, acknowledgements and the mapping to Kodi's ids), the playlist state and the download table; `sync.json` holds the whitelist and the sync state. Kodi's MyVideos and MyMusic are never opened: `tests/package/probe.py` runs the shipped tree with every such open refused.

**Tests are layered.** L1 units run against Kodistubs plus fakes in `tests/unit/fakes.py`. The pipeline's suite (`test_api_*.py`) drives the real store, pass and provider against the fake Kodi in `tests/unit/apikodi.py` and the fixtures in `apifixtures.py`. `test_package.py` builds the zip, checks what it ships and runs the browser contracts against the staged tree. Live gates per phase are tracked in `docs/testing-plan.md`.

## Constraints that are easy to re-break

Generic Kodi constraints that used to live here are now in kodi-drive, and are not repeated:
`<reuselanguageinvoker>` placement and plugin-handle hangs (`kodi-addon-manifest`,
`kodi-plugin-handles`), `xbmcaddon.Addon()` during an update and overlapping service generations
(`kodi-addon-lifecycle`), the string cache (`kodi-process-control`), bare-number node labels and
`folder.jpg` (`kodi-library-nodes`), texture-cache re-encoding and chapter cache keys
(`kodi-texture-cache`), `PlayMedia`'s resume flag and subtitle ordering
(`kodi-playback-resume`), `UserDataChanged` fan-out and the 10.9 route migration
(`jellyfin-client`), and settings `<dependencies>` on a `list[string]` (`kodi-addon-manifest`).

What remains is kofin's own:

- **Kodi's databases are never opened.** Every row kofin files reaches Kodi through the scanner
  or JSON-RPC, and `tests/package/probe.py` runs the shipped tree with every `MyVideos*`,
  `MyMusic*` and `Textures*` open refused. The retired SQL build's rows are told apart by URL
  (the first-run gate below); nothing here reads or writes them.
- Generated nodes live under `special://profile/library/video/kofin/` and **every deletion path is
  gated on the `kofin` name prefix** — hand-made nodes share the tree.
- Generated playlists live in a `Kofin/` folder under Kodi's own `playlists/video/` and
  `playlists/music/` — **capital K** — with three ownership rules. Under `playlists/video/Kofin/`
  the `kofin` prefix still gates `.xsp` deletion and a foreign non-m3u8 file is spared
  (`nodes/fs.py`); unmanaged `.m3u8` in that folder are Jellyfin video playlists and are pruned
  (`playlists.py`). Under `playlists/music/Kofin/` the **folder** is the boundary — the
  managed `.m3u8` files are named after the Jellyfin playlist's title, carry no prefix, and every file not in
  the managed set is removed on reconcile. Nothing of the user's may be told to live in
  either music `Kofin/` or as a `.m3u8` under video `Kofin/`.
- **A playlist rewrite keeps the stored filename while the title still matches,**
  including a disambiguated `Name (2).m3u8`. `_unique_stem`'s taken set is collisions
  for *other* names. Seeding it with our own stem made every membership rewrite pick
  `Name (2).m3u8` and ping-pong; recomputing the stem when the base name is not yet
  reserved renames `Name (2).m3u8` onto whoever owns `Name.m3u8`. `apply_one` and
  `reconcile` both go through `assign_playlist_filename`.
- Reference checksums have exactly one spelling: `policy.reference_checksum`. Never inline
  `"|plugin"`; the playlist state compares through it, and two comparators once agreed with
  each other while both being wrong.
- The bundled backdrop is `resources/media/fanart.jpeg`, named in `addon.xml` and
  `plugin/browse.py` (`BACKDROP_IMAGE`), and nothing rewrites it. The server's splashscreen
  is fetched as WebP and written content-addressed into addon_data (`service/backdrop.py`,
  `core/api.py::splashscreen`); `profile_path` is what a listing shows while the setting is on.
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
- **A downloaded item plays from disk however it is reached, online included.** A download leaves
  Kodi's row on its plugin URL, so `plugin/play.py` is the one place that can enforce it for
  anything playing by *id* — a kofin listing, a widget, a SyncPlay group start. The preference was offline-only once, and the plan's "downloaded items
  never reach the plugin" is what SyncPlay disproved: the initiator adopts the group queue for
  the playback it is already running (`syncplay/manager.py`), so only the follower reloads, and
  it streamed media the initiator was playing locally. `resolve_downloaded` queues the play
  state as the stream path does, so the service reports the play. A request naming a
  source, a track or a bitrate still streams — a download has only the tracks it was made with.
- **A scanner ListItem never stamps a zero resume point** (`metadata.listitem` stamps one only
  for a position above zero). Kodi's scanner persists any *set* point —
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
- **Every show folder under a `tvshows/` root carries its own binding** (`Native.bind_folders`,
  `containssingleitem`). Kodi takes a plugin path's parent to be the plugin *root*
  (`URIUtils::GetParentPath`), so a folder never inherits the root's binding and the scanner
  skips it — 79 shows, one listing, nothing imported on the P1D until the bindings existed.
  `addon.xml` binds all three `medialibraryscanpath` content types to `/native/`; with only
  `movies` Kodi refuses tvshows outright.
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
  no longer shows it; the empty row Kodi keeps is removed by id with the `remove_set`
  feature (`RemoveMovieSet`, kontell/xbmc #7), and without it stays, in no listing, for
  Kodi's own Clean library. A collection without a Kodi row (its movies unsynced, or the one a movie
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
- **A song's date added is the server's `DateCreated`** (`metadata.song_listitem`,
  `InfoTagMusic.setDateAdded`, kontell/xbmc #13), and an album takes its newest song's, so
  recently added albums is the server's order. Without #13 Kodi took the time of the scan
  for a plugin path, which has no file date. The date is not one of the tags: it moves no
  directory's hash, so a library filed by an earlier build takes it as its directories are
  listed again (a Repair lists every one). `folder_label` still lists albums oldest first.
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
- **The progress bar paints once a second at most and never per item**
  (`backends/api/progress.py`). `DialogProgressBG.update` waits on Kodi's app thread; the
  applier calls `step` per item and the bar decides whether to paint. The same setting passes
  `showdialogs` to the scan calls. Both settings stay in `settings.xml`: `getSettingBool` on
  a setting the build does not define raises `TypeError`, and that failed every pass on the
  box once.
- **The sync status is a file, never a setting.** The library thread writes
  `sync-status.json` when its state changes (`backends/api/status.py`) and the "Library status"
  button reads the catalogue when it is opened (`plugin/actions.py::library_status`); the
  SQL build's two read-only lines, `syncStatus` and `syncedLibraries`, are gone. The
  state is written by a listing as well as by the pass, with the names of the libraries
  listed for the first time: the catalogue has no row of one until its listing is published,
  and a first selection read "Done" for as long as the server was being asked.
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
  ran two 20 s passes a minute for as long as it was on.
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
  sets a removed library emptied go with the remove itself (kontell/xbmc #3), and a
  collection the server removed leaves by its id once no movie files it (`finish_sets`,
  `VideoLibrary.RemoveMovieSet`, #7). An emptied set is in no listing (`GetMovieSets` and
  `GetMovieSetDetails` both read sets through their movies), so the id is removed only while
  this pass's set readback shows no set holding it: a set with movies is never touched, even
  one Kodi gave a reused id. A Kodi that could not be asked keeps the collection pending.
  Only the shows Kodi could have merged with
  a leaving one are invalidated (`_merged_shows`), not every show, and the clear runs on a
  thread of its own for the bar's sake, where a stop waits for it.
- **A single-source item's streams are on the item, not on its source** (`metadata.compact`,
  `_stream_source`): read off the first source alone, every such row was filed with a stub
  video stream and no audio or subtitle. A stream's default, forced and hearing-impaired
  flags are listed with it by the names an NFO's `<flag>` takes (`metadata._streams`,
  kontell/xbmc #9, the `stream_flags` feature; without it no flag is set). Streams have no
  setter, so the scanner format numbers what a listing carries (`metadata.scanner_format`:
  3 with the flags, 2 without) and a **manual** Repair rebuilds a video library filed under
  a lower one than this Kodi reaches (`Library.rebuild_stale`; a library a Piers filed is
  not stale on that Piers, and is on the Kodi 23 the profile moves to): never a pass, since the library is gone from Kodi
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
  a clean, whose per-file `check_exists` is `provider.exists` (a version file exists while
  the movie still lists its source, an extra while the movie's payload still names its stem).
  The pass runs that clean itself, of the one movie's folder, when the movie's acknowledged
  file list (`asset_files`) lost an entry (`patch.assets_dropped`, `Native.retire_assets`),
  and only with the `asset_clean` feature: Kodi 23 with kontell/xbmc #1, #2 and #10 asks the
  add-on about the folder's versions and extras and keeps what it cannot answer for, where
  a Kodi without them asks about every bound folder of every library and deletes the path
  row of one left unanswered, so the rows are left for the viewer's own Clean library, said
  once in the log per pass. What that clean does with them is Kodi's version: Kodi 23's
  clean asks the add-on about a version's or an extra's file row and takes one answered no
  (a dropped extra left stock master's library that way, rig run of 23 October 2026);
  Kodi 22's `CleanDatabase` deletes only the files of the movies and episodes it removes,
  so there the rows stay until the movie leaves, whatever the add-on answers (22.0b2, the
  same extra survived a full clean, asked and answered no). Without the
  `video_versions` feature (#8) the folder lists the movie's own file and its extras alone:
  the default source plays, the assets token and file list name the extras only, and
  `provider.exists` answers no for a version file, so a row an earlier Kodi filed leaves
  the day something asks (a Kodi with #10). The source is read from the request's query, never
  from `base_url` (Kodi's `argv[0]`, which has none): read from there, every version file
  existed. A version file is the movie's URL with `mediasourceid`. Every file of a movie with
  several sources says it has versions, the movie's own as the default, under the movie's
  unique ids (`metadata.Version`, kontell/xbmc #8), so the scan files one movie with a version
  per file. A further version's ids leave `kofinrefresh` out (`Version.of`): Kodi groups a
  file under a movie only while no unique id type of the two differs
  (`HasConflictingUniqueID`), and a Director's Cut put back on the server carried a token the
  movie's row did not, became a movie of its own, and the refresh after it lost the extra. A
  movie's refresh token takes its own source alone, without the source's name or streams
  (Jellyfin labels the first source while there are versions, and the payload keeps a
  source's streams only while there are several), so a version that comes or goes refreshes
  nothing. Kodi 23's `RefreshMovie` keeps a movie's versions and extras and files it under a
  new id (seen on the rig). `Readback.owns` accepts the
  movie's URL with or without the source, since the user may make a version the default;
  `Readback._prefer` lets the movie's own file win over a version row Kodi filed apart and
  keeps it as `version_rows`, which leave with the movie on removal. Two rows by the movie's
  own file are still a duplicate identity.
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
  cache answers Kodi's probe), and a file goes to the play route or fails as a play. Kodi
  drops the folder before looking at it while `videolibrary.ignorevideoextras` is on (its
  default), and that setting is the viewer's: the pass never changes it, and the folder is
  scanned once either way. **The folder `extras/` is bound,
  `noupdate`, or Clean library deletes every extra unasked:** an extra's URL has no query, so
  Kodi files it under the add-on's root (`CVideoDatabase::SplitPath`), where a clean finds no
  binding and asks nothing. The scan that files a movie's extras binds the folder
  (`scan_video`), and `bind_movie_folders` binds those an earlier build filed once per profile
  (`apiMovieBindings` at `noupdate+extras`), so no pass reads every acknowledgement for them.
- **One build for Piers and Kodi 23: what a Kodi lacks is off by name, never an error.**
  `requirements.json` has a *baseline* (the Piers floor: `minimum_kodi_major` 22,
  `xbmc.addon` 21.90.802, `xbmc.python` 3.1.0, and the interfaces every Kodi the build
  installs on offers) and a *feature table*; `capabilities.inspect` probes both at every
  service start from one capture (`JSONRPC.Introspect`'s methods and types, the system
  add-ons' versions, `hasattr` on the info tags and stream detail classes), writes
  `api-capabilities.json`, and logs one line. A Kodi short of the baseline keeps native
  sync off with the reasons in Library status; a feature is on only while every one of
  its probes passes, and `capabilities.features()` / `has()` / `table()` are how the pass,
  the provider (plugin process), the play route and SyncPlay read it, by name, from the
  report. Three rules follow. **R1:** Kodi's schema shapes the kind table and every
  desired state: a setter field or a readback property Kodi does not name is neither
  sent, compared nor requested (on Piers `tag` in `Video.Fields.Episode` is absent and a
  `GetEpisodes` that asks for it fails whole; `kinds.FEATURE_FIELDS` names the gated
  fields, `kinds.properties` applies them to every readback, and `metadata.details` leaves
  them out of the desired state). **R2:** the feature set is an input of every
  acknowledgement (`inputs_token`, `SCANNER_FORMAT`, `assets_token`), and the pass stamps
  the set it ran with (`store.features_applied`): a set that moved since the last pass, a
  Kodi upgrade, runs the next pass as a repair, the compare of every row, where nothing is
  written that already matches, and a Kodi that lacks a feature is never told its library
  is stale for it. The stamp carries `metadata.SERIALIZER` too (`store.serializer_applied`),
  and a number that moved runs the same repair: a bump otherwise reaches only the rows a
  pass is handed, and a row that never changes keeps the old build's state for good. **R3:** a feature that is off says so once, in the start log and under
  "Not on this Kodi" in Library status, never per item or per pass. Two features are
  proxies for behaviour Kodi cannot be asked about: `asset_clean` (kontell/xbmc #1, #2,
  #10; the folder clean is unsafe without them) is `xbmc.python` ≥ 3.2.0, the version the
  change set raises it to, until the three are upstream and Kodi's major can stand for
  them; `tempo` (the tempo set, #14–#19 and #37–#39) is the `correction` flag #38 adds to
  `Player.SetTempo`. The first start still deletes the `companion/` folder an earlier build
  left in the profile (`backends.api.forget_companion`).
- **A season's plot and an episode's tags are patched like any other field where the Kodi has
  the feature** (kontell/xbmc #6, #12: `season_plot`, `episode_tags`; elsewhere neither is in
  the desired state nor in a readback). An empty server plot is no opinion: `SetDetailsForSeason` writes no empty plot, so a
  plot the server cleared stays in Kodi. A show's refresh token carries its seasons' numbers
  only (`metadata.refresh_fields`), so a season plot no longer refreshes its show.
- **Chapter art is the resolved item's `chapter<n>` art** (`plugin/play.py::chapter_art`,
  kontell/xbmc #11): the server's image for each chapter it has one for, numbered from 1 as
  Kodi numbers chapters, on a direct play of the item's own file. Kodi's own extractor
  refuses every http source, and a transcode's stream has no chapters. A Kodi without #11
  ignores the keys and fetches nothing for them, so they are set on every Kodi.
- **The presentation is built from the catalogue, never from Kodi's tables.**
  `sync/dynamic.py` writes the node tree for whitelisted video libraries with `path`
  rules on the folder the library's rows are filed under (`paths.library_dir`: a movie's
  URL and an episode's carry a query, so Kodi files each on its movie or show folder,
  `CVideoDatabase::SplitPath`) and the smart playlists beside it, so no row carries a tag
  of Kofin's for it. A `kofin.library.<id>` tag, which showed in the info dialog and
  Kodi's Tags list, did this until serializer 6, and `patch.merge` takes one off any row
  that still has it. Both trees need the login's key (`dynamic.login_key`) and are left
  alone while the settings cannot say who is logged in. `dynamic.py` publishes `library://`
  paths for them and `browse` paths for every other view, and takes the tree down by the `kofin` prefix when nothing is synced. A whitelisted music
  library gets its folder in the music tree (`kofinmusic<id>`, `nodes/music.write_music_nodes`),
  whose nodes filter on `path`, the folder its songs are filed under: a music `path` rule takes
  an album or an artist by its songs. The Manage menu reads a library's downloads from that folder's path. Blank login ids
  take the tree down only from a store that answered (`dynamic.music_entries`): an
  unregistered add-on and a settings document that did not load read blank too. Kodi's music nodes
  are seeded first, the skin entry stays Kodi's music root, and there is no Downloaded folder: a
  download keeps its plugin URL, so a rule on the downloads folder matches nothing. For the same
  reason the Downloaded-music view is not a smart playlist but `Downloaded music.m3u8`, the
  finished songs listed from the download store (`backends/api/playlists.write_downloaded_music`),
  rewritten when a download finishes or leaves (`downloads/nativeport.py`); its stem is reserved in the managed folder (`playlists.RESERVED_STEMS`). A server
  playlist stored under that name earlier moves to `(2)` on its next pass, Etag or not, and
  the old name is removed only while it is still that playlist's copy (`_remove_stored`).
  **Kodi's shipped video nodes are seeded into the profile
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
  move when Kodi files the item. `sync/playlists.py` keeps the files, the naming, the state and
  the prune rules; `apply_one` and `reconcile` take a `resolver` (`backends/api/playlists.py`),
  which gives a line for an item the pass has acknowledged, the resolver URL Kodi filed the row under, and the label
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
  **`libraryNodes` is the one switch for all of it** (`dynamic.wanted`): off takes
  down the tree, the smart playlists and both `Kofin/` playlist folders, and publishes a
  synced video library as a browse entry (a synced music library keeps Kodi's music root,
  which is Kodi's). It is off by default, since everything it writes is outside
  addon_data, and only an explicit value from a store that answered counts: a settings
  document that did not load reads a boolean as its default, which is off, so `wanted` reads
  through the `deviceId` canary (`dynamic._answering`). On None nothing is written or taken
  down, and the skin entries follow the tree on disk, its singles included. The Jellyfin
  playlists follow the switch in the playlists pass, never in the command that queued it: the
  pass stays due while the switch cannot be read, a named playlist's update falls to it while
  it cannot, and a service that starts with the switch off and playlist
  states on record runs it (`api_playlists.owed`), which is how a profile whose stored
  default moved loses them.
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
- **A download writes through to storage 8 MiB at a time, and rests while storage is the slower
  side** (`manager._write_body`). Why, with the numbers, is `kodi-performance`'s section on a
  large file write stalling Kodi through its log: unsynced, a download on the Bravia stalled the
  app thread for up to 8.8 s. Two designs were measured and dropped: a sync on a thread beside
  the network reads left the flash no gap (2.4 s stalls), and resting after every sync cost the
  P1D up to 40 %.
- **A download moves nothing of Kodi's.** `downloads/nativeport.py` is the manager's view of
  the library: it leaves the row's plugin URL alone (the resolver plays the local file however
  the item is reached), hands the badge and the "Kofin Downloads" tag to the pass by invalidating
  the item, its show and the show's seasons (`backends/api/downloaded.py`, the download state
  is an input of the desired state and joins `inputs_token`), reads Kodi's play state back by
  the mapped id, and leaves the local watched mark of a vanished download to the server
  round-trip that already follows it, since a setter would start the userdata echo cycle. The
  badge is on the movie or the episode and on the episode's season and show; the season is the one of the episode's number (`Applier.downloaded_seasons`). The
  badge key is `kofindownloaded`: Kodi's `SetArtForItem` refuses an art type with a dot in it
  (they are the types derived from a parent, `tvshow.poster`), so the setter accepts
  `kofin.downloaded` and stores nothing. A skin tests both keys, `ListItem.Art(kofin.downloaded)`
  from the SQL build's day and this one (skin.contuary read the first alone). **The
  Downloaded-episodes node filters on a tag the show never carries** ("Kofin Downloaded
  episodes", `downloads.EPISODE_TAG`): Kodi's tag rule on episodes matches the episode's tags
  or its show's (kontell/xbmc #12), so a rule on "Kofin Downloads" lists every episode of a
  show with one download, and a path rule has nothing to match. The node and the tag need
  `episode_tags`, and a Kodi without it has no such node. The port's operations are `attached` (the file landed, or is still
  there at startup: the item goes to the pass and the answer is always yes, so the startup
  reconcile re-plans every done file and a finish that died before its badge is healed) and
  `detached`. A season's download menu is answered through the catalogue
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
- **A library listed for fewer fields is listed again by name** (`library.LISTING`,
  `Library.stale_listings`, `store.remember_listing`). A catch-up fetches changed items alone,
  so a field added to `downloader.info` or `MUSIC_FIELDS` would otherwise never reach a row
  filed before it: `OriginalTitle` was missing from the listing, and every movie's original
  title was empty in Kodi. The record is per library and written only for a library whose
  listing was published; a selected library the server's views left out, or one being rebuilt,
  stays stale, and each catch-up lists what is stale until it has been.
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
  is Kodi's to make and to drop: it goes with the last movie that names it, and a scan or a
  movie's patch makes it again from the name alone, bare, while the collection's payload has not moved. `sets.idSet` is a
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
  both, and a setter is 46 ms a song for nothing (without kontell/xbmc #13 `UpdateSong` also
  rewrote `dateAdded` to the moment of the call), so a song that matches is never written,
  repair or not.
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
- **Kodi 23 with kontell/xbmc #4 runs its databases in WAL, and kofin never switches them.**
  Without #4 they are rollback journals, and the switch is the user's to make: the mode lives
  in the file, so a profile the SQL build or the companion ever switched is already WAL. What the
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
  item's listing is a lookup on every item, quadratic over an import. WAL and the name
  indexes (kontell/xbmc #4 and #5, measured as the companion's switch and its
  `ix_kofin_{actor,tag,studio}_nocase` indexes) take the scan from 1,265 s to 446 s; with
  the hold below, 259 s and a 424 s pass. Shows are the same shape: 79 shows and 4,316
  episodes scan in 1,292 s on a rollback journal without the indexes and 294 s with both
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
  Kodi, 446 s to 259 s beside WAL and the name indexes. The user's value is recorded **before**
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
- The wake-time FastSync on `GUI.OnScreensaverDeactivated` is **unconditional on purpose**: it is
  the only cover for a websocket that went half-open while asleep.
- Every property in `core/state.py` is shared across service generations — see
  `kodi-addon-lifecycle` for why two run at once, and treat that module accordingly.
- **SyncPlay converges position on commands, and between them only by confirmed tempo
  pulses — never by a continuous loop.** The old `Player.SetTempo` ladder was removed after
  measurement, not after taste: stock Kodi plays a tempo only while sync-to-display is on,
  and the setting slaves the media clock to the panel, so a refresh rate that is not a whole
  multiple of the frame rate imposes a fixed rate error — 0.5 %–4.3 % across three Piers
  devices, the worst 14× the ±3 % the ladder could command (`docs/syncplay-drift-shakedown.md`
  §10). The actuator is Kodi's own tempo with that setting **off** (kontell/xbmc #14 and
  #16, with #37), where the same devices free-run within a few hundred ppm, and only where
  the Kodi has it: `TempoSession.begin` asks for the `tempo` feature, and a member without it
  (Piers, stock Kodi 23) keeps to the group's commands, as every audio item did on 0.19, and
  plays a live item without the timeshift route. inputstream.tempo, the retired Piers
  build's actuator, is not used.
  `syncplay/tempo.py` issues one bounded pulse at a time through `Player.SetTempo`, as a
  correction the OSD does not show where Kodi declares the flag (#38), confirms it by reading
  the `tempo` property back, reads what it moved off the player clock, settles before
  measuring again, and gives up on a one-signed residual (`docs/syncplay-fine-sync.md`).
  Music takes pulses through PAPlayer's tempo (#15) within fixed limits of its own
  (`music_profile`: at most 1 %, within the atempo threshold, a 25 ms deadband, a 500 ms
  budget); the two sliders are video's. An item Kodi cannot play at a tempo
  (`Player.TempoEnabled`) gets command-only sync; nothing is routed or stamped on a play, so a
  transcode, a download and a provider's play take a pulse like any other.
- **A pulse never outlives a pause.** Kodi refuses `Player.SetTempo` while paused and keeps
  the tempo in force across a pause, so a pulse cut on a paused player leaves its rate
  behind. The 1.0 is owed: the playing tick and `SyncPlayManager.on_resumed` (first thing, in
  a group or not) ask for it again, and `state.PROP_SYNCPLAY_PULSE` records a rate in force so
  that a new service generation puts it back (`Service._settle_left_tempo`), and so does the
  service player's resume while there is no manager. The record goes only when a read shows
  1.0, never on an accepted `SetTempo`. That property is not in `clear_all`, on purpose.
- **A pulse ends before its track does.** PAPlayer makes a track queued behind the playing one
  at the tempo in force, and the group's boundary hold then pauses on it, so a pulse ends,
  settle included, `END_MARGIN_S` plus `musicplayer.crossfade` before its item, or is
  shortened, or waits for the next (`PulseScheduler._fit_to_item`). The whole schedule has to
  fit, ramps included (`fit_pulse`).
- **A live item's group position is on a live clock, ten days up.** The player clock of a
  live stream is session time and means nothing to another member; the engine reads a live
  member's position as `getTime()` plus Kodi's `presentationtime − time` (#19: the stream's own
  clock, refused when it began under a minute ago, as a server remux's does) or else the join
  origin (the server time at which the player clock read zero), and a proposer anchors the
  group `LIVE_DELAY_S` behind its reading, offset by `LIVE_PTS_EPOCH_S` on the wire so a
  live-clock anchor can never be mistaken for the session-time zero a member without the clock
  proposes. `claim_is_live` is the one spelling of
  live (kofin's own claim carries `Live`; a bus claim has no runtime), and a live item is never
  seeked — ffmpeg's HLS demuxer refuses a seek on a live playlist — only pulsed, and two forward
  pulses Kodi ends at the live edge (#25) mean the edge, not a fault (`docs/syncplay-fine-sync.md`).
  In a group with fine sync on a live item plays through inputstream.ffmpegdirect's timeshift
  (`plugin/play.py::live_timeshift`): Kodi plays a live stream at a tempo only with a time range.
- A sync thread that will not stop is a thread inside the HTTP retry ladder
  (`docs/library-thread-stop.md`); the two rules that follow are easy to undo.
- **Never call `xbmc.Player.stop()`** — use `core/kodirpc.py::stop_player`. Kodi's binding sends
  `TMSG_MEDIA_STOP` without a `DelayedCallGuard`, so it blocks on the app thread *holding the GIL*,
  and the app thread's stop path waits on MyVideos writes that a GIL-starved kofin thread may be
  holding. That is a deadlock with no recovery but a force-stop (issue #155, measured on Omega and
  Piers). `pause()` has the same missing guard but does not tear the player down, so it does not
  reach the wait — it is a stall risk, not a deadlock, and is deliberately left alone inside the
  SyncPlay transplant.
- **"Recently added albums" asks `/Items/Latest`, never albums sorted by `DateCreated`** — an
  album's DateCreated is when the scanner last created its row, and a rescan re-creates rows in
  folder order (`docs/dynamic-libraries-plan.md` §3).
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
- **Kofin logs at debug.** The official repository allows `xbmc.log` at the debug level only,
  so `Logger` has no `info`; `warning` and `error` are for faults (a failed request, a refused
  call, a thread that will not stop), and an expected event, however notable, is debug.
- Docs in `docs/` use one line per paragraph — `tools/unwrap_md.py` fixes wrapped files.

## Comments

Comments and docstrings say what the code cannot show, in the present tense: a Kodi, Jellyfin,
SQLite or Python behaviour the line depends on, an invariant, a contract. What an earlier build
did, approaches tried, measurements, device names, plan and audit tags, PR numbers and the story
of how a rule was found go in the commit message or the PR body. Each rule is explained once,
where it belongs, and deleting a comment beats rewording it. `kontell/xbmc #N` belongs where a
feature is declared or probed (`requirements.json`, `contract.py`); elsewhere the feature name
is enough.

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
