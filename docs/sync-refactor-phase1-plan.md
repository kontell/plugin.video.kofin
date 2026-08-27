# Sync refactor, phase 1: the cheap, high-yield tier

| Field | Value |
|---|---|
| **Date** | 2026-08-27 |
| **Source** | `docs/sync-refactor-assessment.md` §7, Tier 1. Findings and line references live there; this document is the work order. |
| **Branch** | `refactor/sync-phase1`, stacked on `docs/sync-refactor-assessment` (PR #190). One commit per item below, each revertible on its own; the PR merges after #190. |
| **Scope** | Five code items and the rig work they need: typing over `kofin.sync.*`, dead-code removal, one library walk in `full_sync.py`, the `GetItemWorker` re-queue, and the writer→shell import hoist. Nothing from Tier 2 — no module splits, no clock primitive, no `views.py` work. |
| **Rule** | Every item lands with its unit proof *and* its live gate on both local generations. A live gate that cannot be run is written down as such, not skipped silently. |

## 1. Why these five, and in this order

Each item is small enough to review as one diff, is proven by an oracle that already exists (mypy, the L2 idempotency dumps, the live database dumps), and either closes a defect the assessment found or removes something that makes Tier 2 more expensive. The order is by blast radius: the static and deletion changes first, so the walk extraction and the hook — the two that change what runs on a sync — are gated against a rig already proven on the new base.

| # | Item | Closes | Proof |
|---|---|---|---|
| P1.0 | Rig preparation and the "before" snapshots | — | the oracle every later gate diffs against |
| P1.1 | `check_untyped_defs` over `kofin.sync.*` | ~25 real type errors, 61 phantom ones | mypy gate; smoke sync |
| P1.2 | Dead code | 17 unreachable MyMusic arms, a Kodi-19 query, five dead functions | L2 across MyMusic 83/84; live music sync dump-identical |
| P1.3 | One walk in `full_sync.py` | four copies of the skeleton, one of them with the mid-page-404 skip; live, the unguarded child fetch that abort-then-skips is the **boxset** writer's (the movie writer guards its own — assessment §3 erratum) | L2; live full syncs dump-identical; the 404 scenario on the boxsets walk |
| P1.4 | `GetItemWorker` re-queues on any exception | ids lost until the recovery prune | L1; live outage replay |
| P1.5 | `post_write` hook: writers stop importing `kofin.downloads`/`musicsources` | the only shell→transplant import direction violation | L2 dumps; live downloads repoint and music sources |
| P1.6 | End-to-end regression on both rigs | — | the "before" snapshots vs the finished branch |

## 2. The rigs

Two Kodi generations run on this box, and they share every port, so **one Kodi at a time** unless the flatpak's ports are moved (open question 1).

| | Omega | Piers |
|---|---|---|
| Install | native Kodi 21.3, `~/.kodi` | flatpak `tv.kodi.Kodi` 22.0~b1, data under `~/.var/app/tv.kodi.Kodi/data/` |
| Profile | `kofin-test` (`~/.kodi/userdata/profiles/kofin-test/`) | master (the flatpak has no profiles) |
| Databases | MyVideos131 / MyMusic83 / Textures13 | MyVideos148 / MyMusic84 / Textures14 |
| Ports | JSON-RPC 8080 (auth from `guisettings.xml`, never printed), EventServer 9777 | JSON-RPC 8080, EventServer 9777 — the same two |
| Server today | production 10.11.11, three libraries whitelisted | production 10.11.11, one library whitelisted |
| Deploy | `tools/dev-install.sh` (rsync + `UpdateLocalAddons()`) | none yet — P1.0 adds a target override |
| Launch | as today | `flatpak run tv.kodi.Kodi` |

Two Jellyfin servers, used for different things:

- **Production** (10.11.11, API access only, the household's libraries — 1,7xx movies, 7x shows / 4,5xx episodes, music). Read-only. This is where the *scale* gates and the dump comparisons run, because a refactor that preserves semantics must produce the same rows for the same input and this is the largest fixed input available.
- **jf12** (`dev/test-server`, v12.0-rc5 on `:8098`, disposable; `bin/jf12-run start`, `./provision.py`, `bin/jf12-reset --keep-media` in 30 s). This is where anything **mutating** happens — deleting an item mid-sync, downloading an item, creating a collection. Smoke tier: 6 movies, 2 series / 51 episodes, 4 artists / 43 albums / 475 tracks; `--full` symlinks the real trees for the one scenario that needs a long write phase (P1.3c). No music-video library exists on either server: the musicvideo walk is L1/L2-covered only, and the plan says so where it matters.

Pointing a profile at jf12 means logging that profile in again, which resets its `kofin.db` and `sync.json`. Only the Omega `kofin-test` profile does this (it has before, 2026-08-17), and only for the mutation scenarios; it is pointed back at production at the end of P1.3 and re-synced. The flatpak stays on production throughout.

**Assertion surfaces**, in trust order and unchanged from `docs/testing-plan.md` §1: sqlite3 on the profile databases (copy the `-wal` alongside the `.db` before reading a snapshot, or the rows are not there — `kodi-db-snapshots-need-wal`); the addon's log via `kodi-logtail mark` / `since` / `errors`; Kodi JSON-RPC; Jellyfin REST; screenshots last and only for what renders.

**Deploying patched add-on code may be refused by the harness.** It worked on 2026-08-11 and is tried first; on a denial the fix stays on the branch and the deploy step is handed over as a one-liner (`! tools/dev-install.sh`, `! tools/dev-install.sh --flatpak`), after which the driving and the assertions continue from here.

## 3. The oracle: dump-identical

The L2 suite's strongest invariant is the byte-identical idempotency dump, and it has a live analogue. For each rig, P1.0 takes a **"before" set**: a fresh full sync on the PR #190 build of the same libraries the "after" runs will sync, then `sqlite3 <db> .dump` of MyVideos and MyMusic with the per-user and per-run columns masked (`playCount`, `lastPlayed`, `bookmark`, `settings`, `files.dateAdded` is *kept* — it comes from the server and must not move). Every later gate that syncs the same libraries on the new build dumps the same way and diffs. The expectation is **empty**; a non-empty diff is a finding, and the item does not land until it is either fixed or written into the item's commit message as intended, with its L2 test.

Where ids can legitimately differ (a repair re-creates rows, so `idMovie`/`idFile` renumber), the comparison is on the dump with ids normalised the way `tests/live/ab_diff.py` already does for movies — keyed on the server's ids, not Kodi's. That harness is reused as-is for movies; a thin extension keys shows/seasons/episodes on `uniqueid` and albums/songs on `strMusicBrainz*`, and lives beside it as `tests/live/dump_diff.py` (P1.0).

## 4. The items

### P1.0 — Rig preparation and the "before" snapshots

**Change.** `tools/dev-install.sh` learns a target: `--flatpak` sets `DEST` to `~/.var/app/tv.kodi.Kodi/data/addons/plugin.video.kofin` and the reload goes to whichever Kodi answers on the configured port. `tests/live/dump_diff.py` as in §3. `docs/testing-plan.md` gains a `## S-P1` section with the scenario ids below, filled in as they run.

**Live.** With the #190 build deployed on both rigs: (a) Omega, production, the three whitelisted libraries: **Repair** all three (fresh rows), wait for the drain, snapshot + dump → `tests/live/results/S-P1.0-before-omega/`. (b) Piers, production, its one library: the same → `S-P1.0-before-piers/`. (c) jf12 up, smoke tier provisioned, one collection created over the API holding two of the six movies (the boxsets walk needs one), and the Omega `kofin-test` profile pointed at it and synced once; snapshot → `S-P1.0-before-jf12/`. Then the profile goes back to production and P1.0's (a) is re-checked against its own dump: **the first assertion of the plan is that a resync on the *same* build is dump-identical**, so a later diff cannot be blamed on the method.

### P1.1 — `check_untyped_defs` over `kofin.sync.*`

**Change.** `cursor: sqlite3.Cursor` declared on `kodidb.Kodi` (61 of the 157 errors). Fix the rest — `library.py` 34, `full_sync.py` 22, `views.py` 14, `downloader.py` 10, the writers ~10 — as annotations or as real corrections (`library.py:2845` passes `str | None` to `dict.get`; `LOG.exception(error)` with a non-string at two sites). Flip `check_untyped_defs = True` in the `[mypy-kofin.sync.*]` block; `disallow_untyped_defs` stays off — bodies are checked, signatures are not forced. The `mypy.ini` comment about "rewriting proven writer SQL to satisfy a linter" is rewritten: nothing here rewrites SQL, and the transplant paragraph in CLAUDE.md now says what actually protects the writers.

**Proof.** `mypy` clean with the new block; `tests/unit` green. Each non-annotation fix carries an L1 test if it changes behaviour.

**Live.** S-P1.1: a **FastSync** on each rig (screensaver wake or `kofin.sync.fastsync` IPC), one userdata flip on the server (watched toggle on a production item this user owns), and a Repair of the smallest whitelisted library; log clean of tracebacks; dump-identical to S-P1.0. This is a smoke gate — the item is static — but two of the real corrections sit on the catch-up path and this is what exercises them.

### P1.2 — Dead code

**Change.** Delete: `views.DYNNODES`; `downloader.get_episode_by_show`; `shims.JSONRPC` + `get_grouped_set`; `kofindb.get_kodi_ids_by_media_folder` and its query; `db._kofin_db_path`; the `FullSync.sync`/`update_library` class attributes (the tests that set `sync.sync = {...}` keep working — the attribute is instance state from `libraries()` on); `GetItemWorker.source`'s dead write; the `version_id < 72/74/80` arms in `kodidb/music.py` (17 sites) and `queries.add_stream_video_obj_19`. Correct the `MUSIC_DOWNLOADED_FILE` comment and the `order_media_folders` docstring while there.

**Proof.** L2 across MyMusic 83 and 84 is the proof that the music arms were unreachable: the suite runs the music writers against both gated schemas and the dumps do not move. `grep` shows zero callers for each deletion (the assessment already did; the commit message repeats the list).

**Live.** S-P1.2: a music library Repair on **both** rigs (Omega MyMusic83, Piers MyMusic84 — the two versions the gate admits, and the two the deleted arms sat below), then dump-identical to S-P1.0 for MyMusic. Also on both: `library://music/kofin/` nodes list the library's albums (the `MUSIC_DOWNLOADED_FILE` comment fix must not have touched behaviour).

### P1.3 — One walk in `full_sync.py`

**Change.** `_walk(library, item_type, key_suffix, writer_cls, apply, describe, dialog)` extracted from `movies()`, `tvshows_pass`, `musicvideos()` and `boxsets()`; the four become five-line callers. `apply_or_skip` is what every caller passes as `apply`, so the mid-page 404 and the orphan skip are uniform. The `boxsets` outcome tally and the `tvshows` skipped tally are returned by the walk, not tracked by hand. Restore points, per-page locking and commit cadence are the walk's and are unchanged. `music()` is **not** folded in: its one-lock-per-library shape was measured (`docs/library-thread-stop.md`) and changing it is a Tier 2 decision. The four percent expressions and eleven `"Kofin: …"` headings collapse into the walk.

**Proof.** L2: `test_sync_writers.py`'s `boxsets()` tests keep passing untouched; new full_sync-level tests run the walk with a fake writer and a `PagingApi` for each of the four callers, including the 404-mid-page case for movies and musicvideos that today exists only for shows (`test_item_gone_server_side_is_skipped_not_fatal`). The unit suite proves that the walk is the same code for all four; the live gates prove that the same code writes the same rows.

**Live.** Four scenarios:

- **S-P1.3a dump-identical at scale, both rigs.** Repair every whitelisted library on Omega (movies, shows, music) and on Piers, against production; dump-identical to S-P1.0. Request count per library from the log equals S-P1.0's (the walk must not change paging).
- **S-P1.3b interrupted resume (S2.7 re-run), Omega.** Kill the service mid-Shows-repair (`Addons.SetAddonEnabled` false), confirm `sync.json` holds the pending entry and a restore point, re-enable, confirm `Resuming interrupted sync` and final counts equal S-P1.0. Once on Piers too, because the restore-point key includes the walk's `key_suffix` and both generations must read the same one.
- **S-P1.3c the mid-page 404 on a movie, Omega on jf12 `--full` movies.** The defect this closes. `limitIndex` set to 500 so one page holds hundreds of movies and the write phase runs for tens of seconds after the page GET; start a movies Repair; when the progress bar passes ~50, `DELETE /Items/{id}` a movie known to be further down the same page (pick it from the page order — `DateCreated desc`). Old build: `library … failed`, the sync-failed toast, the entry still pending. New build: one `skipped … 404` line, the library completes, its entry leaves `sync.json`, the deleted movie is absent from MyVideos and from `kofin.db`. Then the same with the collection created in P1.0 for the **boxsets** walk (delete one member during the boxsets pass). Musicvideos: no library exists; L1 only, stated in the results.
- **S-P1.3d the failing-library-keeps-going fix from #190, live.** With jf12 stopped (`jf12-run stop`) and the Omega profile pointed at it: a `SyncLibrary` for two libraries → both stay pending, one failure toast; start the server → the resume poll syncs both. This is the live half of what #190's tests prove.

Afterwards the Omega profile returns to production and re-syncs (dump-identical to S-P1.0 again — the round trip through jf12 must leave nothing behind).

### P1.4 — `GetItemWorker` re-queues on any exception

**Change.** `GetItemWorker.run`'s generic `except Exception` puts the chunk back (as the `ServerUnreachable` arm already does), sets `download_errors` so the watermark holds, and flags the items unapplied. Bounded: a chunk that fails three times is dropped with every id flagged, so a poison item cannot spin a worker forever — that is what the recovery prune is for.

**Proof.** L1: a worker whose `api.items` raises a non-transport error once re-queues and succeeds on the retry; three failures drop the chunk and flag each id; `download_errors` set in both cases.

**Live.** S-P1.4: the deterministic outage replay, Omega on production: `SIGSTOP` is not available for production, so the addon's `serverAddress` is repointed at a closed port for 30 s during a catch-up with work queued (the S2.6 method), then restored: watermark held, chunk re-queued, catch-up completes exactly once on recovery. This exercises the unreachable arm the change shares its plumbing with; the generic arm has no reproducible live trigger and stays L1-proven. Said so in the results.

### P1.5 — `post_write` hook

**Change.** Two seams on the writers: `pre_tags(obj, item)` before `add_tags` in `movie()`/`episode()` (the downloads `TAG` injection), and `post_write(obj, item)` at the end of `movie()`, `episode()`, `song()` and `album()` (the downloads `reassert_on` / `reassert_music_on` and `musicsources.link_library_source`). The writers hold a list of hooks the shell registers when it builds them (`UpdateWorker.run`, `full_sync`'s walks, the removal and userdata workers do not need them); the three `from kofin.downloads import …` lines and the `musicsources` import leave `sync/writers/`. `musicsources.py` keeps importing `kodidb.music` — that direction is fine.

**Proof.** L2: `test_downloads_repoint.py:89-99, 373` and `test_sync_writers.py:3051-3140` pin the observable rows and pass unchanged once the hooks are registered by the test harness the way the shell registers them; a new test asserts a writer built with *no* hooks writes the same rows minus the tag and the source link — the fork's rows.

**Live.** S-P1.5a downloads, Omega on jf12: download one smoke-tier movie and one episode (the offline-downloads gates in `docs/offline-downloads-plan.md` W1.7); Update libraries; the rows stay repointed (`files.strFilename` a bare basename under a real directory row, `idFile` unchanged) and the `Downloads` tag is on the item; play it — it plays from disk. S-P1.5b music sources, **both rigs** on production: music Repair; `source` holds one row per whitelisted music library named after it; `album_source` links every kofin album; then a Kodi music scan of an empty source (`AudioLibrary.Scan` on a nonexistent path) empties the table and the `ReassertMusicSources` command restores it — the in-session heal the hook must not have broken. Dump-identical to S-P1.0 for both databases.

### P1.6 — End-to-end regression, both rigs

Everything above once more, on the finished branch, against production, in one sitting: Repair all whitelisted libraries on Omega and Piers → dump-identical to S-P1.0; a FastSync with one userdata flip; a new-content toast for one addition induced through the prune path (the S-newcontent method); the S2.7 interrupted resume on Omega; a service bounce with workers in flight (`workers_alive` path, #155's territory — `stop_player` untouched but the drain is). Results → `tests/live/results/S-P1.6-{omega,piers}.md`, and the `S-P1` section of `docs/testing-plan.md` marked PASS/PARTIAL per scenario with the evidence paths.

## 5. What the plan deliberately does not do

- No change to `music()`'s lock scope, the pager's semaphore depth, or `_get_items`' shape (Tier 2, and two of them were measured).
- No move of `GetItemWorker`, no split of `full_sync.py`, no clock primitive, no `views.py` work — those are Tier 2 and need the golden-XML and worker tests first.
- No obj-dict replacement.
- No new settings, no IPC changes, no schema-gate changes.

## 6. Exit checklist

- [ ] `tox` green on the branch; `mypy` runs `check_untyped_defs` over `kofin.sync.*`.
- [ ] Every deletion in P1.2 named in its commit message with its zero-caller grep.
- [ ] `_walk` is the only place the walk skeleton exists; `apply_or_skip` is passed by all four callers.
- [ ] S-P1.0 through S-P1.6 recorded under `tests/live/results/`, each with the build sha, the rig, the server and the dump-diff result; PARTIAL entries say what was not run and why (musicvideos, the generic re-queue arm).
- [ ] Omega `kofin-test` back on production, whitelist as before, dump-identical to S-P1.0.
- [ ] `docs/testing-plan.md` has the `S-P1` section; `docs/sync-refactor-assessment.md` §7 Tier 1 marked done with the date.

## 7. Open questions

1. **Flatpak ports.** Move the Piers flatpak to JSON-RPC 8081 / EventServer 9778 (two lines in its `guisettings.xml`, with that Kodi down) so both generations can run at once and P1.6 can drive them side by side — or keep the shared ports and run one at a time? Recommendation: move them; it is a one-time rig change and the `--flatpak` deploy target reads the port from the same place.
2. **jf12 `--full` for S-P1.3c.** The 404-mid-page scenario needs a movie page long enough to race by hand, which the six-movie smoke tier is not. `provision.py --full` symlinks the real movie tree (scan time in minutes, `/home` space is the constraint the README flags). Alternative: a `--full-movies-only` provision. Which?
3. **P1.5 in this phase or the next.** It is the one item that edits writer bodies; the assessment put it in Tier 1 because the seams are small and the L2 pins exist. If it should wait for Tier 2's worker tests, P1.5 drops out and the import-direction violation stays on the ledger.
4. **Repair as the "fresh rows" method for the before/after sets.** Repair renumbers Kodi ids, which is why the diff keys on server ids (§3). If a bit-for-bit `.dump` comparison is wanted instead, the before/after sets must come from an initial sync into an empty profile database — more rig work, stronger oracle. Recommendation: keyed comparison; it is what `ab_diff.py` already does and it is what the writers' semantics are about.
