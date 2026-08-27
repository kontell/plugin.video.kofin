# Sync refactor, phase 2: the structural tier

| Field | Value |
|---|---|
| **Date** | 2026-08-27 |
| **Source** | `docs/sync-refactor-assessment.md` §7, Tier 2, plus the findings phase 1 left on the ledger (`docs/sync-refactor-phase1-plan.md` §4b, `docs/testing-plan.md` §S-P1). |
| **Branch** | `refactor/sync-phase2`, stacked on `fix/sync-phase1-findings` (PR #193, itself on `refactor/sync-phase1` #191), draft PR #192. Landed: P2.5 `dffbb9e`/`4b3799c`/`6767e5d` (#193), P2.0 `5b7794a`, P2.1 `835d754`, P2.2 `9b1222b`, P2.3 `39abd2c`, P2.4 (see git log). One commit per item, each revertible on its own; merges after #191. **Decided 2026-08-27:** the three phase-1 findings (P2.5) land first as their own small PR stacked between #191 and this one, so phase 2 restructures a clean baseline; `sync/nodes/` is a package; rewrite-whole node files proceed on the assessment's finding that nothing user-editable lives under `kofin/`; the `kofin-jf12` profile's databases may be reset for the first-sync scenario. |
| **Scope** | The four shell-side files the assessment named, restructured along the seams it mapped: `views.py` (one deletion primitive, split by concern, rewrite-whole, table-driven builders), `full_sync.py` (restore points, prune, boxsets and removal as their own modules; the Library port named), `library.py` (the refresh policy out, one clock primitive, a dispatch table, one worker loop), `downloader.py` (the pager's seams). Plus the three small defects phase 1 found. Nothing inside `writers/`, `kodidb/`, `fields.py` or `obj.py` beyond what P2.4 states. |
| **Rule** | Safety nets before any move; every item lands with its unit proof *and* its live gate on both local generations; **nothing is ever deleted through jf12 and no file is written under any jf12 library path** — its media is the household's (§2). |

## 1. Why these, and in this order

Phase 1 was the cheap tier and it held: five items, every gate keyed-identical on both generations, and the one live surprise (a 400 where a 404 was assumed) was caught by the oracle and fixed in-tree. Phase 2 is the tier that moves code between files and changes the shape of `library.py`; the oracle for rows (`tests/live/dump_diff.py`) carries over unchanged, but `views.py` and the refresh policy need oracles of their own before anything moves, which is why P2.0 comes first and is not small.

| # | Item | Closes | Proof |
|---|---|---|---|
| P2.0 | Safety nets and baselines | the tests the assessment found missing: golden node XML, real worker tests, a `service()` tick harness | the oracles every later gate diffs against |
| P2.1 | `views.py` | three gate spellings, five duplicated mechanics, the rename defect, `window_clear` drift | golden XML; live node trees byte-identical, a server-side rename fixed |
| P2.2 | `full_sync.py` split + the Library port | eleven concerns in one class, nine duck-typed reach-ins, five test fakes | L1/L2; live Repairs identical, S2.7 resume, prune plans identical |
| P2.3 | `library.py` decomposition | four fix clusters (refresh gating, clocks, worker lifecycle, generation lifetime) | tick harness; live widget-refresh and ladder gates, both rigs |
| P2.4 | `downloader.py` seams | the 143-line pager, `@stop` on a generator, the reused dict | pager tests without Kodi fakes; live Repairs identical |
| P2.5 | Phase-1 findings | the logged nonce, the resume-shadow `files` row, the swallowed child 404s | L1/L2; live removal leaves no orphan rows |
| P2.6 | End to end, both rigs | — | dump-identical, node-identical, prop-identical to P2.0 |

## 2. The rigs, and what phase 1 taught about them

Everything from phase 1 persists: the flatpak Piers on JSON-RPC 8081 / EventServer 9778 with `tools/dev-install.sh --flatpak`, the `kofin-jf12` profile on Omega, jf12 on the local `~/.dotnet` runtime, the built-in dim screensaver set on both Kodis, the in-Kodi harnesses in `tests/live/harness/` (with a copy under the flatpak's data dir, because its sandbox cannot see `/tmp`). Per-session facts that bit once and must be re-checked every time: a fresh or re-entered profile can have `debug.showloginfo` off (request counts read as zero — set it and verify before counting); `FastSync` is not a NotifyAll message (wake the screensaver instead); a new profile has no web server (`kodi_settings.py`, then answer the warning over the EventServer).

**The media rule, in full.** jf12 is a disposable *server*; its media is the production media. The smoke farm's movie, show and music folders are symlinks into `/media/minipie/…`, and `JF12_FULL_*` are those trees directly. Phase 1 deleted *Rush (2013)* from the NAS by deleting its item through jf12's API. So, for every phase-2 scenario on jf12: no `DELETE /Items/{id}`, no library removal with file deletion, no file created, moved or removed under `/home/conor/jf12/media/**` except inside a directory that `readlink -f` proves is local (today: `Phase One Test (2026)/`, a real directory holding a generated file). Item metadata edits (`POST /Items/{id}`), library renames, collections, userdata and downloads are server-state only and stay allowed. A "gone item" is staged by removing a **generated** file from a **local** directory and refreshing, never by deleting through the server. Production is read-only, as before.

Servers: production for scale and dump comparisons (both rigs); jf12 (`kofin-jf12` profile) for the metadata-mutating scenarios.

## 3. The oracles

- **Rows**: `tests/live/dump_diff.py` keyed sets, exactly as phase 1. The phase-1 finished-build sets *are* the phase-2 before sets: `omega-p16.json` and `piers-p16.json` (`c2849cd`, taken after the incident, so they describe the server as it is). Season names are stored normalised (`""` for NULL) from `94b427f` on; compare like with like.
- **Node tree** (new, P2.0): every file under `library/video/kofin/`, `library/music/kofin/` and `playlists/video/Kofin/` + `playlists/music/Kofin/`, byte-compared after regeneration, on both rigs. Kept as `tests/live/results/S-P2.0-before/<rig>-nodes/`. Snapshotted by a small `tests/live/node_snapshot.py`.
- **Skin props** (new, P2.0): every `Kofin.nodes.*` window property, read through `XBMC.GetInfoLabels`, as JSON; the contract skins read (`plugin.video.kofin.wiki/Skin-integration.md`).
- **Requests**: per-walk counts from the debug log against phase 1's baselines (Movies 38, Collections 112, Recordings 15, Music 474 + 5).
- **Widget refreshes**: the `widgets moved` / `widgets unchanged` / `Container.Refresh` / `ReloadSkin` lines per scenario, against the widget-refresh plan's ledger (F1–F9).

## 4. The items

### P2.0 — Safety nets and baselines

**Unit.** (a) `tests/unit/test_nodes_golden.py`: for two fixtures (video whitelist with a mixed view; music with two libraries and downloads on), generate the full tree into a tmp dir and compare every file against checked-in golden XML; include the rename case as a test that **fails today** (both tag rules under `<match>all</match>`) and is fixed by P2.1. (b) Real tests for `UserDataWorker`, `SortWorker` and `RemovedWorker` run against the L2 fixtures (today only `UpdateWorker` is ever constructed). (c) A `service()` tick harness: a `Library` with fake queues, fake threads with `is_done`/`source`/`db_file`, a fake clock, one `service()` call per case — the scaffolding P2.3 refactors against. (d) `tests/live/node_snapshot.py` and a `props` mode in `tests/live/harness/kofin_ipc.py`'s sibling that dumps `Kofin.nodes.*`.

**Live.** Both rigs on `c2849cd`: regenerate nodes (bump the hash by toggling a hidden setting or `RemoveLibrary`+`SyncLibrary` of nothing — the harness `set` mode on `viewsHash=""` then a bounce), snapshot the trees and the props → `S-P2.0-before/`. Confirm the request-count baselines still hold with debug on (a Movies Repair on Omega, the Music Repair on Piers).

### P2.1 — `views.py`

**Change.** A `sync/nodes/` package: `fs.py` (`is_managed(name)`, `remove_managed_entries(root, keep)`, `remove_empty(root)` — the only code that deletes, with one gate spelling), `video.py` (the `Views` node generation, prune, migrate), `music.py` (the module-level music functions, moved), `props.py` (the window properties, names unchanged); `views.py` keeps the view table, the server listing and the hash. Playlist writing folds into `playlists.py`. Every node file is written whole (the music side's shape) instead of parse-and-amend — that is what fixes the rename; `order`/`icon` were already overwritten every pass, and only the two creation-only parent `index.xml` files stay creation-only. The 13 `node_*` builders become a table driving one writer; `window_clear` derives its suffixes from that table. The `Views.get_nodes()` orchestrator stays the entry point the service calls.

**Proof.** The golden tests from P2.0 pass unchanged except the rename case, which now passes; the deletion primitive has its own tests (foreign file spared under both `kofin/` roots and under `playlists/video/Kofin/`; `playlists/music/Kofin/` folder-gated, as documented). `test_sync_views.py` loses its nine-patch fixture for the generation half (the generators take a view list).

**Live.** S-P2.1a both rigs: regenerate → node tree and props **byte-identical** to S-P2.0. S-P2.1b Omega on jf12: rename the smoke Movies library server-side (`POST /Library/VirtualFolders/Name` — server state, no files) → regenerate → `all.xml` and the `.xsp` carry only the new tag, `Files.GetDirectory` on the node lists the movies; rename it back. S-P2.1c: `RemoveLibrary` of one library (jf12 profile) → every `Kofin.nodes.N.*` prop for it cleared, including the sub-node ones today's `window_clear` misses; re-add. S-P2.1d: the migrations (`migrate_flat_*`) on a planted flat layout, both rigs.

### P2.2 — `full_sync.py` split and the Library port

**Change.** `sync/restorepoints.py` (pure), `sync/prune.py` (+ `local_reference_map`, `PRUNE_SERVER_TYPES`; `library.py` imports the probe's inputs from here), `sync/boxsets.py` (walk, sweep, restamp), `sync/removal.py`. `FullSync` keeps `libraries/start/process_libraries/process_library` and the `_walk`. The claim moves from `__init__` to `__enter__`; the sync.json loader is injected. The nine reach-ins become one small object (`SyncHost`: the two locks, claim/release, enqueue, refresh, the failure set, `stamp_watermark_if_empty`, `defer_playlist_poll`) that `Library` builds and the tests fake once.

**Proof.** The five Library fakes across the test files become one; `test_sync_full.py` and `test_sync_prune.py` drop the `save_sync`/`notification` patches and the `sync.sync = …` rituals. Existing walk, prune and boxset tests pass unchanged.

**Live.** S-P2.2a both rigs: Repair at scale identical, request counts on baseline; S2.7 resume on Omega (the Shows initial sync killed mid-episodes, as phase 1) and on Piers (Music). S-P2.2b Omega on production: `UpdateLibrary` with nothing changed → the prune plans `missing:0 changed:0 stale:0` for every library (log), no writes, no refresh. S-P2.2c Omega on jf12: the generated movie's file removed from its **local** directory and the library refreshed → the prune reports `stale:1` and the removal writer takes the row; the file put back and refreshed → `missing:1` and the addition toast. S-P2.2d boxsets: the phase-1 collection scenario without the delete — a collection edited (member added/removed by `POST /Collections/{id}/Items`) → the boxset guard/heal outcomes and a silent drift probe on the next boot.

### P2.3 — `library.py` decomposition

**Change.** `sync/refresh.py`: `Refresher` owning `refresh_libraries`, the fingerprint gate, the settle/hold clocks, the first-content and repair reloads, `_video/_music_content_hidden`, `_refresh_music`, `refresh_added`/`metadata_pending`; `Library` calls `arm(databases)`, `tick()`, `now(databases, force_reload)`. `sync/clock.py`: one deferred-action primitive (`due`, `hold`, floor/ceiling ladder, `fire()`), replacing the seven clock pairs. `process_commands` becomes a dispatch table. `sync/workers.py`: one drain loop parameterised by a dispatch map, `db_file` and `source` as constructor arguments, `GetItemWorker` moved beside it; `pending_items` counts without reaching into `queue.Queue.queue`. `Library` shrinks to construction, startup, the tick, the queues and the event entry points.

**Proof.** The P2.0 tick harness drives each moved procedure through `Library` as the service does; the refresher and the clock get direct tests (settle folds two arms into one fire, the hold cap, the ladder's doubling and reset, the fail-open fingerprint); the 21-defs-never-executed count from the assessment goes to zero. `test_sync_library.py`'s 30 self-patches come down to the collaborators that are really external.

**Live** — the widget-refresh ledger, both rigs: S-P2.3a a favourite flipped on and off (userdata echo) → `widgets moved: video/userdata` once each way and **`widgets unchanged`** on the repeat of an identical value; S-P2.3b two userdata echoes seconds apart (a music track change on Piers's Music) → one refresh, not two (the settle); S-P2.3c a fresh profile's first sync (the `kofin-jf12` profile reset to empty databases, re-synced) → one `ReloadSkin` per media kind and Home showing content; S-P2.3d a Repair → the repair reload; S-P2.3e music refresh → the nonexistent-path probe scan finishes in 0 s with zero song probes; S-P2.3f the ladders on jf12: server frozen → resume poll at 60/120/240 s; a poison entry (the phase-1 bad-id method) → recovery prune floor then ceiling, never silent; S-P2.3g a wake FastSync queued during a running catch-up is coalesced (`covered by the pass in flight`); S-P2.3h a service bounce with 200 queued updates in flight (the S-P1.4 shape without the freeze) → the old generation's workers finish, `workers_alive` holds the rebuild, no second lock over the same file, rows identical.

### P2.4 — `downloader.py` seams

**Change.** `_get_items(api, query, limit, threads, should_stop)` with the settings read by the caller; the `@stop` on the generator replaced by a per-page `should_stop()` check; pages yielded as `(items, restore_point)` instead of one reused-and-cleared dict. The two transplant call sites that iterate pages (`writers/movies.py` `boxset_current` via `get_movies_by_boxset`, `writers/tvshows.py` via `get_seasons`) change their loop variable and nothing else — said so in the commit, and the L2 dumps are the proof. The four copies of the `/Items` filter block become one helper.

**Proof.** Pager tests without `FakeAddon`/`FakeWindow`/`_monitor` (eight autouse patches gone); the in-order-consumption and restore-point-only-advances-past-handed-pages invariants get direct tests.

**Live.** S-P2.4 both rigs: Repair identical with request counts on baseline; S2.7 resume from a mid-pass restore point on Omega.

### P2.5 — The phase-1 findings (lands first, as its own PR)

**Change.** (a) `process_commands` and the service log guarded payloads with `_nonce` stripped (the guard secret stays out of kodi.log). (b) The resume-shadow `files` row (the second row under the add-on root path an episode with a resume point gets — movies never get one, the writer's resume goes on their own file row; the plan said "episode or movie" and was wrong on that half) is removed with its bookmark by the removal writers; a new L2 zero-orphan rule covers `files` and `bookmark`. (c) `extras()` and `trailer()` still swallow every other error, but a **404 on the item's own child fetch** propagates as gone, so the walk skips it instead of writing a row for a deleted movie — with the gone-probe already in place this is the one case it cannot reach today. (b) and (c) are writer changes and carry their in-place deviation notes.

**Proof.** L1 for (a); L2 for (b) (an episode with a resume point removed leaves no `files`/`bookmark` row behind, across every gated schema) and (c) (the fake API 404s the child fetch → the movie is skipped, not written).

**Live.** S-P2.5b Omega on jf12: play the generated movie for a few seconds, stop (a resume point is reported), `RemoveLibrary` Movies → the unlinked-`files` count is unchanged from before the sync; re-add. S-P2.5c cannot be staged without a server-side delete and stays L2-only — said so.

### P2.6 — End to end, both rigs

The finished branch: Repair of every whitelisted library on both rigs dump-identical to `omega-p16`/`piers-p16`; node trees and props identical to S-P2.0; wake FastSync; the refresh ledger scenarios once more; a service bounce with work in flight. Results under `tests/live/results/S-P2.*`, `docs/testing-plan.md` §S-P2.

## 5. Not in phase 2

- The obj-dict / spec-list convention (assessment §7, "not worth doing" unless the writers keep changing).
- `music()`'s one-lock-per-library walk and the pager's semaphore depth (measured live; a Tier 3 question with its own measurement).
- Re-running `ab_diff.py`.
- Any change to what `Kofin.nodes.*` props are called (a skin contract).

## 6. Exit checklist

- [x] `tox` green at every commit (black, mypy over 117 files, 2,650+ unit tests; the three `test_downloads_manager.py` tests that fail only in local ordering are pre-existing and pass in CI); `mypy` still checking every `kofin.sync` body. The never-executed count was not re-measured — the moved bodies now sit behind direct tests (`test_sync_clock.py`, `test_sync_refresh.py`, `test_sync_restorepoints.py`, `test_sync_host.py`, `test_downloader_pager.py`, the worker tests), which is what that number was standing in for.
- [x] One deletion primitive; `grep -rn '"kofin"' lib/kofin/sync/nodes` finds the gate once (the other hit is `Database("kofin")`, the database name).
- [~] `full_sync.py` 1,453 → **945** lines and `library.py` 2,950 → **2,098** — well short of the 400 / 1,500 the plan guessed. What stays in each is what the plan said would stay (the library queue, the per-library dispatch, the walk and the four per-kind passes; the tick, the queues, startup and the entry points) plus their docstrings; going further means moving the passes out of `full_sync.py` and the startup/fast-sync half out of `library.py`, which is a phase of its own. Each moved module has its own tests.
- [~] S-P2.0 through S-P2.4 recorded, S-P2.6 in progress; node trees byte-identical on both rigs and props identical except the intended change (150 stale sub-node props cleared on Omega); the rename fix is L1-covered — a Jellyfin 12 rename is a new library id, so the live scenario cannot reach the code (`tests/live/results/S-P2.1/README.md`).
- [x] The media rule honoured: every results file lists the jf12 mutations made (userdata, collections, virtual-folder renames, `/Library/Refresh`, one generated file moved aside inside a `readlink -f`-proven local directory) and none touched a file under a share.

## 7. Decisions (were open questions; answered 2026-08-27)

1. **Layout:** `sync/nodes/` as a package (`fs.py`, `video.py`, `music.py`, `props.py`), `views.py` keeping the view table, the server listing and the hash.
2. **P2.5 first, as its own PR:** the logged nonce, the resume-shadow `files` row and the swallowed child 404 land on a branch stacked on `refactor/sync-phase1`, ahead of this one; phase 2 rebases onto it.
3. **Rewrite-whole is confirmed:** a hand-edited node file under `kofin/` is overwritten on regeneration; the assessment's finding that nothing user-editable lives there stands, and the plan proceeds on it.
4. **S-P2.3c may reset the `kofin-jf12` profile's databases** for the first-sync scenario; it is the throwaway profile.

These answers are the go-ahead; implementation starts with P2.5's PR, then P2.0.
