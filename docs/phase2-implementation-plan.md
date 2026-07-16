# plugin.video.kofin — Phase 2 implementation plan

Date: 2026-07-16. Operationalizes build phase 2 from `rewrite-research.md` §11 — the **sync transplant**. The report governs architecture; the fork (`ref/jellyfin-kodi`, branch `combined/syncplay-sync`) is the source of the proven code. Test gates reference `testing-plan.md` (S2.x). Phase 1 is complete and live-verified; everything here builds on its core (`Api`, `Http`, settings/credentials, IPC, service lifecycle).

**Phase 2 deliverable**: kofin mirrors selected Jellyfin libraries into Kodi's own databases — initial full sync, incremental catch-up and realtime updates against the **official KodiSyncQueue** plugin (tier 2), library selection/update/repair driven entirely from the settings dialog, video nodes so libraries appear as native home items, and a schema gate that refuses unknown Kodi databases. The fork's phase-1 sync improvements (priority split, userdata-from-payload, post-download Etag skip, watermark honesty, degrade-not-die, auto-resume, indexes) come along in the transplant.

---

## 1. Scope

**In**: writers + mapping DB + downloader/orchestrator/full-sync transplant; schema gate with pristine-DB test fixtures; Library and Sync settings tabs (selection multiselect, update/repair/refresh-boxsets buttons, apply-on-save diff engine); views/nodes generation; websocket LibraryChanged/UserDataChanged wiring; library-item playback (dbid paths, context transcode on library items); widget refresh policy; companion-plugin tier detection with a status line; the A/B equivalence harness and the S2.x live gates on a dedicated Kodi profile.

**Out (later phases)**: KofinSyncQueue plugin and everything tier-1 (queue-side Etag, typed records, retention envelope — phase 5); plugin-free fallback (phase 6); extras sync, segments, Up Next (phase 3); SyncPlay (phase 4); Playback/Interface/Advanced settings tabs; Piers live testing (schema map covers it; fixtures deferred to hardening).

## 2. Decisions locked in phase 2

* **Transplant discipline**: the fork's sync code is ported with *mechanical* adaptation only — imports, helper shims, addon id, path bases. The SQL and pipeline logic stay recognizably identical; the A/B gate (S2.2) is the correctness proof. No restyling pass: `mypy` runs relaxed over `kofin/sync/**` (`disallow_untyped_defs = False` per-module) and tightens opportunistically later. Rewriting proven writer SQL to satisfy a linter is how transplants die.
* **Mapping DB**: `addon_data/plugin.video.kofin/kofin.db`, schema byte-identical to the fork's jellyfin.db (same `jellyfin` table name, `view` table, version row, and the fork's secondary indexes) — renaming tables buys nothing and costs diff-ability.
* **Library item paths** (identity in Kodi's DB — locked now, migrations are painful): path `plugin://plugin.video.kofin/{LibraryId}/` (per-media-type sub-shapes exactly as the fork writes them, e.g. tvshows `{LibraryId}/{SeriesId}/`), filename `?mode=play&id={ItemId}&dbid={KodiId}&filename={name}&mode=play` shape preserved. The phase-1 `play` handler already ignores unknown params; it gains `dbid` awareness (report `PlaySessionId` etc. unchanged).
* **Schema gate map**: Kodi 21 → MyVideos **131** / MyMusic **83**; Kodi 22 → MyVideos **146** / MyMusic **84**. Discovery finds the newest matching file (fork's mechanism minus the `UpdateLibrary()` mtime hack); an unmapped/newer schema → write-sync disabled + one notification + status line in the Library tab. Never write blind.
* **IPC registry additions** (`core/ipc.py`): `SyncLibrary`, `RemoveLibrary`, `RepairLibrary`, `UpdateLibrary`, `RefreshBoxsets` (settings buttons → RunPlugin → `ipc.notify` → service library manager). Payloads carry `{"Id": "<library id or csv>"}`.
* **Settings-diff engine** becomes a real module (`service/settings_apply.py`): a registry of `setting id -> handler(old, new)` consulted from `onSettingsChanged`; phase 1's inline deviceName/sslVerify handlers move in; phase 2 adds `librarySelection` (the whitelist csv) whose handler computes add/remove sets and dispatches sync/removal — this is the "apply on OK" mechanism for the multiselect.
* **advancedsettings.xml is never mutated** (report §6): if `<videolibrary><cleanonupdate>true</cleanonupdate>` is present (incompatible with plugin paths), warn with a notification at service start — the user fixes it themselves.
* **Widget refresh policy** (fork commit `e4f8dc3f`, non-negotiable): after direct DB writes, `Container.Refresh` only when `Window.IsMedia`; never `UpdateLibrary()`.
* **Native Kodi thumbnails wherever possible**: generated video nodes and menu/folder entries use Kodi's stock icon names (`DefaultMovies.png`, `DefaultTVShows.png`, `DefaultMusicVideos.png`, `DefaultRecentlyAddedMovies.png`, …) so every skin substitutes its own native artwork — never addon-branded icons on structural entries (the old addon stamped its icon/fanart on everything). Server artwork belongs only on real media items (posters, fanart, thumbs from Jellyfin image tags). This also applies retroactively to phase 1's browse node menus (All / Recently added / …), which currently ship no art — step 4 adds the stock icons there too.
* **Kodi's native dual-write is accepted**: Kodi itself bumps playcount/bookmarks on library items it plays; the server remains authority — the round trip (report → UserDataChanged → sync) converges the row. No player-side Kodi-DB writes in kofin.
* **Strings**: 30250–30299 Library tab, 30300–30349 Sync tab.

## 3. Port map (fork `jellyfin_kodi/` → kofin `lib/kofin/sync/`)

| Source (fork) | Target | ~Lines | Adaptation notes |
|---|---|---|---|
| `database/__init__.py`, `jellyfin_db.py`, `queries.py` | `sync/db.py`, `sync/kofindb.py`, `sync/queries_map.py` | 900 | Drop the `UpdateLibrary(video)` discovery hack; schema-gate on open; WAL + fork indexes kept; `embyPathMigratedMusicDB` migration dropped (no legacy installs) |
| `objects/obj.py` + `obj_map.json` | `sync/obj.py` + `obj_map.json` | 300 | Verbatim; mapping file is data |
| `objects/movies.py`, `tvshows.py`, `musicvideos.py`, `music.py` | `sync/writers/…` | 2230 | Strip `direct_path` branches (native mode is gone — `get_path_filename` keeps only the plugin:// arm); addon id/path base; `PathValidationException` machinery deleted |
| `objects/kodi/*` (kodi, movies, tvshows, music, musicvideos, artwork, queries, queries_music, queries_texture) | `sync/kodidb/…` | 2450 | Near-verbatim SQL; people-cache Borg + fork indexes kept; texture-cache handling (`queries_texture`) kept |
| `objects/utils.py`, `helper/api.py` | `sync/fields.py` | 450 | The Fields/artwork helpers writers depend on |
| `downloader.py` | `sync/downloader.py` | 380 | Fork's in-order paging + chunking; client calls rerouted to kofin `Api` (new endpoints §5 step 3) |
| `library.py` | `sync/library.py` | 1300 | The orchestrator: startup, fast_sync, priority queues, workers, watermark honesty, degrade-not-die, retry scheduling; `helper.event`/window-prop plumbing → kofin ipc/state; the `syncParallelMusic` remnants deleted (report YAGNI); progress via `xbmcgui.DialogProgressBG` as in fork |
| `full_sync.py` | `sync/full_sync.py` | 800 | RestorePoints, resume-without-modal, update/repair modes; `enableMusic` auto-flip dropped (derived from whitelist) |
| `views.py` | `sync/views.py` | 1000 | Node/xsp generation; regenerate only when the view-set hash changed; forced node ordering removed; node `<icon>`s use Kodi stock names (Default*.png), not addon art |
| `helper/xmls.py` (subset) | `sync/kodisetup.py` | 150 | `verify_kodi_defaults` port + cleanonupdate *detection* (warn, don't edit) |

New code: `sync/schema.py` (gate + version map, ~80), `plugin/librarypicker.py` (multiselect dialog + hidden-setting write, ~120), `service/settings_apply.py` (~120), settings XML/strings (~350), API additions (~80), fixtures + tests (~700).

## 4. Settings spec

### Library tab (`category id="library"`, 30250+)

| id | type/control | notes |
|---|---|---|
| `selectLibraries` | button → `RunPlugin(...mode=selectlibraries)` `close=true` | multiselect dialog over server views (movies/tvshows/music/musicvideos/mixed types only), pre-checked from `librarySelection`; writes the csv back; the diff engine applies on save |
| `syncedLibraries` | read-only label | names of the current whitelist, written by the service |
| `updateLibraries` | button → `mode=updatelibs` | fast-sync catch-up + prune pass (S2.10); all synced libraries |
| `repairLibraries` | button → `mode=repairlibs` | per-library picker (or all) → remove + re-add; confirm dialog |
| `refreshBoxsets` | button → `mode=refreshboxsets` | boxset re-link pass |
| `syncStatus` | read-only label | schema-gate + companion-tier line ("Official KodiSyncQueue" / "no companion plugin — realtime only" / "sync paused: unknown Kodi database vXXX") |
| hidden lvl-4: `librarySelection` (csv of view ids), `lastIncrementalSync`, `viewsHash` | | written by service/picker only |

Removals from the whitelist confirm via yesno inside the handler before deleting rows. Music libraries appear in the same multiselect (selecting one activates the MyMusic writer/gate).

### Sync tab (`category id="sync"`, 30300+)

`syncNotification` bool **default off**; `syncNotificationCount` int **default 1000** (visible when notification on; above this many pending items → notification, never a modal); `syncDuringPlay` bool default true; `dbSyncScreensaver` bool default true (fast-sync on screensaver deactivate); `limitIndex` int default 50; `limitThreads` int default 3; artwork group: `enableCoverArt` bool default true, `compressArt` bool default false, `maxArtResolution` spinner (0/720/1080/2160, default 0); `startupDelay` int seconds default 0.

## 5. Work breakdown (ordered; each step lands green on L0/L1/L2)

1. **Schema gate + fixtures + kofin.db** (M): `sync/schema.py` with the version map; Omega fixture DBs generated by dumping the live box's pristine schemas (`sqlite3 MyVideos131.db .schema` → fixture SQL, MyMusic83 likewise — exact by construction); Piers fixtures deferred (map refuses 146 until then — hardening item). `sync/db.py` + `sync/kofindb.py` ports with L2 tests: open/gate/refuse-unknown, mapping-table round trip, index presence. *DoD*: L2 green; unknown-schema path surfaces the status string.
2. **Writers transplant** (L): `sync/writers/*` + `sync/kodidb/*` + `obj` + `fields`. L2 fixtures: write a movie (with people/genres/studios/streams/art/uniqueids), series→season→episode, boxset link, musicvideo, album→song into pristine 131/83; **idempotency** (second write → byte-identical dump); **removal integrity** (delete series → zero orphans in any link table). *DoD*: L2 suite green including the three invariants.
3. **Pipeline transplant** (L): `sync/downloader.py`, `sync/library.py`, `sync/full_sync.py`. `Api` additions: `sync_queue(last_sync, filters)` (`Jellyfin.Plugin.KodiSyncQueue/{userId}/GetItems`), `server_time()` (`GetServerDateTime` — also the tier probe), paged `items()` with StartIndex, `shows_episodes` paging as the fork uses. Service wiring: library manager thread started once online + whitelist non-empty; `_on_ws_event` routes `LibraryChanged`/`UserDataChanged` into the queues (remote handler keeps its messages); `GUI.OnScreensaverDeactivated` → fast_sync when `dbSyncScreensaver`; `syncDuringPlay` gate honored by workers. *DoD*: L1 on queue-routing units; live smoke deferred to step 7.
4. **Views/nodes + Kodi defaults** (M): `sync/views.py` port with the views-hash regeneration guard; `verify_kodi_defaults`; cleanonupdate warning. *DoD*: nodes appear for a synced library on the test profile; second start regenerates nothing (log proves it).
5. **Library + Sync tabs** (M): settings XML + strings; `librarypicker` dialog; `settings_apply.py` diff engine (librarySelection → add/remove with confirm); button handlers → IPC → library manager; `syncStatus`/`syncedLibraries` labels maintained by the service. *DoD*: selection round-trips through the dialog; add/remove dispatch verified with a fake manager (L1); buttons SaveAndClose+run (live in step 7).
6. **Library playback integration** (S): `play` handler accepts `dbid`; context transcode resolves `ListItem.DBID` → item id via kofin.db when `kofin.id` is absent; addon.xml context visibility extended to library DBTYPEs. *DoD*: L1 mapping-lookup test; live in step 7.
7. **Live gates on the dedicated test profile** (L): profile setup per §6, then S2.1 (counts + field fidelity vs server), S2.2 (A/B normalized diff vs the master profile's old-addon DB — read-only reference), S2.3 (incremental add/edit/delete/watched/resume round trips, websocket and cold paths), S2.4 (50 watched flips → zero `Items?Ids=` fetches), S2.5-tier2 (no-op updates → downloads but zero writes), S2.6 (watermark honesty under mid-sync server loss), S2.7 (interrupted full sync auto-resumes), S2.8 (series deletion → no orphans), S2.9 (no UpdateLibrary scans in the log, Container.Refresh only in media windows), S2.10 (update heals injected holes; repair rebuilds; watched survives via tombstones), S2.11-tier2 (status line correct with the plugin present/absent). Mutating server-side tests use expendable items only — coordinate with Conor on which library hosts add/delete tests before S2.3/S2.8 run (the report's test-library build-out is still pending).
8. **Hardening** (M): error sweep (server loss during every phase; notifications budget — one per failure class per cycle); performance snapshot into `tests/live/results/` (initial sync wall time, catch-up latency for 100 changes); docs cross-check (report §11 phase-2 wording vs reality); teardown scripts.

Steps 1–2 are sequential; 3–4 can overlap; 5–6 after 3; 7 gates everything.

## 6. Live-test environment (this box)

* **Dedicated Kodi profile** `kofin-test` with *separate databases* (Profiles → add profile, "separate" media info/sources), so kofin's writes never touch the master profile's library (which the old jellyfin addon owns and actively syncs). Switch via `LoadProfile(kofin-test)` builtin; kodi-drive tooling works unchanged (web server/EventServer are global). Addon settings are per-profile → run the Quick Connect login once inside the profile (same flow as the phase-1 batch, kofin-test user).
* **Old-addon coexistence**: plugin.video.jellyfin keeps running in the master profile; in the test profile it should be **disabled** (its service would otherwise sync the test profile's fresh DB too and the A/B comparison would be polluted). Re-enable on teardown.
* **A/B reference**: the master profile's `MyVideos131.db` already holds the old addon's sync of the same server libraries — the harness (`tests/live/ab_diff.py`) dumps both DBs normalized (path prefixes mapped, per-user columns `playCount`/`lastPlayed`/bookmarks excluded, ids re-based) and diffs. Differences must be explainable and intended.
* **Teardown**: `LoadProfile(Master user)`, delete the kofin-test profile directory, re-enable anything disabled, run the baseline row-count assertion on the master DB (untouched).

## 7. Risks / watch items

* **Writer coupling to fork helpers**: the writers import a web of `helper` utilities; the shim layer must preserve semantics exactly (especially `values()`/query formatting). Mitigation: A/B gate + idempotency fixtures before any live sync.
* **Music scale**: the user's real music library is ~21k songs; first live music sync should target the smaller music view first (check sizes at implementation) and the artwork settings' effect measured before syncing the big one.
* **jellyfin.db lock contention** was solved in the fork by serialized writers — keep the one-writer-per-DB rule when wiring queues (report R7).
* **Retention on the official plugin**: `RetDays=0` default means the user's server queue may be huge on first fast-sync after a long gap — the notification-count setting (default 1000, off) exists for exactly this; verify the de-modal path.
* **Kodi profile quirks**: some skins/addons misbehave on LoadProfile; if the profile approach fights back, fallback is a second Kodi instance via `KODI_HOME`/portable — decide only if needed.

## 8. Exit checklist

* L0 green (mypy relaxed only over `kofin/sync/**`); L1/L2 green including writer idempotency, removal integrity, and schema-gate refusal.
* S2.1–S2.11 (tier-2 scope) pass on the test profile with evidence in `tests/live/results/`.
* A/B diff report reviewed: every difference annotated as intended.
* Masking grep still clean over a full sync session (art URLs, sync-queue calls).
* Master profile untouched (row-count baseline identical); teardown complete.
* Report/testing-plan updated in the same commit as any deviation.
