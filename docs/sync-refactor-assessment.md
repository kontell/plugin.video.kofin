# Sync system refactor assessment

| Field | Value |
|---|---|
| **Date** | 2026-08-27 |
| **Scope** | `lib/kofin/sync/` — 18,083 lines across 36 files, six weeks old at the time of writing (first commit 2026-07-16); 115 of the repo's 491 commits touch it. |
| **Question** | Does the sync system merit a refactor, and if so which parts? |
| **Method** | Full read of `library.py`; deep reads of the other three slices (full-sync/downloader, views/playlists/clean, writers/kodidb); `git blame` and `git log` accounting since the port commit `aabd711`; a `sys.setprofile` census of which `library.py` defs `pytest tests/unit` actually executes; mypy with `check_untyped_defs=True` over `kofin.sync.*`; a unit probe for the node-rename defect. Every claim carries a file:line or a sha. Line references are against `8335248` (v0.21.1); the Tier 0 change to `full_sync.py` landed after the assessment and shifts that file's lines below 373 by a handful. |
| **Verdict** | Yes — a **targeted refactor of the shell side** of `sync/` (`library.py`, `full_sync.py`, `views.py`, `downloader.py`), plus a correction to the transplant premise in CLAUDE.md. The writers and kodidb should get cheap typing and an import-direction fix, not a restructure. |

## 1. Baseline

Commits into `sync/` by subject prefix split **31 `fix` : 31 `feat`** (plus 23 `sync`, 4 `music`, 3 `perf`). Fix commits per file: `library.py` 14, `full_sync.py` 7, `kodidb/queries_music.py` 6, `kodidb/music.py` 6, `writers/music.py` 5, `writers/movies.py` 5, `views.py` 5.

`tests/unit` is green: 2,495 passed in 81 s. (`test_downloads_manager.py::test_siblings_share_one_season_directory` fails when run alone or under `-x` and passes in the full run — flaky, outside `sync/`, CI on `main` green.)

Size against the nearest upstream baseline (`plugin.video.jellyfin` 2.1.0, the fork's parent):

| File | Upstream | kofin | Ratio |
|---|---|---|---|
| `library.py` | 1,020 lines / 34 `Library` methods | 2,932 / 80 | 2.9× |
| `full_sync.py` | 712 | 1,390 | 2.0× |
| `views.py` | 1,076 | 1,936 | 1.8× |
| `downloader.py` | 348 | 818 | 2.4× |
| `objects/movies.py` → `writers/movies.py` | 430 | 838 | 1.9× |
| `objects/tvshows.py` → `writers/tvshows.py` | 828 | 1,001 | 1.2× |
| `objects/music.py` → `writers/music.py` | 652 | 844 | 1.3× |

Prose density (comment + docstring lines): `library.py` 32%, `downloader.py` 30%, `full_sync.py` 26%. A third of the orchestrator is explanation of why, which is a refactor constraint in its own right — every move has to carry its prose with it.

## 2. `library.py`: where the bugs live, and where the tests cannot reach

### Shape

`Library(threading.Thread)` declares 52 instance attributes in `__init__` (`library.py:169-290`) plus 6 class-level defaults. Ten of the attributes are clocks (`retry_at/retry_delay`, `resume_at/resume_delay`, `auto_prune_at/auto_prune_interval/recovery_pending`, `refresh_due_at/refresh_hold_until`, `download_backoff_until`, `playlist_poll_at`, `last_fast_sync_started`), each with its own arm/flush pair and its own floor/ceiling/reset rule: `_arm_refresh_settle/flush_refresh_settle` (`:1319-1361`), `schedule_recovery_prune/_arm_recovery/flush_recovery_prune` (`:1957-2032`), `schedule_retry` + the `retry_at` check in `service()` (`:2274-2283`, `:661-671`), `defer_playlist_poll/poll_music_playlists` (`:409-440`), `_schedule_resume/resume_pending_libraries` (`:2212-2272`), `pending_skin_reload/flush_pending_reload` (`:1398-1404`). None shares an abstraction.

**43 of 77 methods (56%) have exactly one call site.** They group as steps of five procedures: the `service()` tick (`:628-782`, 16 branches), `startup()` (`:1708-1780`), `process_commands()`' ten-arm `if/elif` (`:783-901`, 29 branches), `refresh_libraries()` (`:1158-1267`) and `fast_sync()` (`:1860-1936`). The genuinely shared internals are `whitelist`, `required_kinds`, `update_status_strings`, `schedule_retry`, `add_library`, `refresh_libraries` and `enqueue_command`.

The three writer threads are one drain loop copied three times — `UpdateWorker.run` (`:2567-2670`), `UserDataWorker.run` (`:2697-2756`), `RemovedWorker.run` (`:2869-2932`): open lock + both databases, build writers per `db_file`, `get(timeout=1)`, dispatch, swallow `LibraryException`/`Exception` with `_report_unapplied`, `task_done`, commit every `COMMIT_INTERVAL`, check `should_stop`, set `is_done`. Workers get `source`/`db_file` attributes attached from outside after construction (`:1097`, `:1146-1147`, `:1611`) and read back through `getattr(thread, "source", None)` (`:1030`, `:1050`, `:1549`) and `getattr(thread, "unreachable", False)` (`:643`). `pending_items` reads `queue.Queue`'s internal deque (`:983`).

27 `except Exception` sites — the highest count of any file in the package.

### History: 43 commits, 23 fixes, four clusters

| Cluster | Fixes | What each added |
|---|---|---|
| Refresh / skin-reload gating (`:1158-1540`) | `9acfabb`, `4ef65f7`, `5f99a52` + four feature reworks `52c64b5`, `af52a04`, `9147b10`, `d02a306` in four weeks | `_refresh_music` probe scan; `_reload_skin_after_repair`; `force_reload`. `d02a306` → `5f99a52` is a same-day regression. |
| Timers and backoff ladders | `3d142c0`, `5fc1670`, `52f349c`, `d5b7a49`, `6058719` | each added a new `*_at`/`*_delay`/`*_pending` pair to `__init__` and a `flush_*` hook to `service()` |
| Worker lifecycle and dispatch | `98d14f5`, `c20d4ee`, `5fc1670`, `d5b7a49`, `0041923`, `9d7d439` | `removal_writer_for`; bounded queues; `flag_unapplied` threaded through three constructors; `_release_worker`; the `refused` set |
| Object lifetime across service generations | `bddb428`, `b8b28d5`, `de83e75` | `close_progress` in a `finally`; `workers_alive`; the full-sync claim moved off the fork's Borg onto the instance |

The remaining fixes are two-or-fewer each: probe/heal (`c933371`, `b392aee`), `check_version` migrations (`ecce990`, `ab73ce9`), progress accounting (`9c4d02c`), transaction order (`7abf529`), toasts (`2841014`).

### Tests

`tests/unit/test_sync_library.py` (2,596 lines) builds the real class through one helper (`:93-99`) with a `FakeApi` and `FakePlayer` local to the file. Patch census: **84 `monkeypatch.setattr`, 30 of them on `Library`'s own methods** (`update_status_strings` ×12, `add_library` ×12, `remove_library` ×6), plus 19 on `xbmc.getCondVisibility` and 15 on `xbmc.executebuiltin`. Tests set 20 distinct attributes on the instance directly and override `startup`, `service`, `process_commands`.

The `sys.setprofile` census over the whole suite: **21 of 94 defs in `library.py` never execute**, among them `startup`, `add_library`, `remove_library`, `test_databases`, `check_version`, `stop_client`, `claim_full_sync`, `release_full_sync`, `workers_alive`, and the entirety of `UserDataWorker`, `SortWorker` and `RemovedWorker` (only `UpdateWorker` is ever constructed, from `test_sync_writers.py`). `service()` is entered exactly once, with its first two steps stubbed and empty queues (`:2385-2410`); `.start()` is never called. `test_service.py` never touches the real class: seven hand-rolled `*Library` fakes each expose one to four attributes.

The refresh tests pass for the wrong reason: `_moved_databases` treats any fingerprint exception as "moved" (`:1269-1318`), there is no MyVideos file to hash under the test fixture, and `widgetstate` is mentioned zero times in the file — so every `refresh_libraries` test sails through the gate the widget-refresh plan built.

## 3. `full_sync.py` and `downloader.py`

### One walk, four copies, one of them fixed

The "begin_walk → get_items → lock → construct writer → set_restore_point → per-item dialog.update → write → commit both → clear_restore_point" skeleton appears four times: `movies()` (`:685-729`), `tvshows_pass` inside `tvshows()` (`:784-829`), `musicvideos()` (`:861-899`), `boxsets()` (`:1179-1214`). `movies()` and `musicvideos()` are identical after name-normalisation. **Only the TV copy has `apply_or_skip`** (`:817`, `:731-765`), so a movie deleted mid-page — whose writer then calls `server.get_local_trailers` (`writers/movies.py:267`) — still aborts the whole library; `test_sync_full.py:58-66` records that exact bug as fixed for shows. **Erratum (2026-08-27, found live in S-P1.3c):** the movie claim is wrong in its mechanism. `writers/movies.py` guards both of its child fetches — `trailer()` wraps `get_local_trailers` in `except Exception` and `extras()` does the same for `/SpecialFeatures` — so a movie deleted mid-page is *written* from the page it was fetched with, the child 404 logged and swallowed, and nothing reaches the walk. The unguarded child fetch on the movies side is the **boxset** writer's `get_movies_by_boxset` (`writers/movies.py:641`), which is the case the one walk changes live; the tvshows `/Seasons` fetch was the other, and that copy already skipped. The four-copies-one-skip observation and the uniformity argument stand; the movie-specific defect does not. **Closed by P2.5c (phase 2):** `trailer()` and a new `special_features()` — the listing is fetched *before* the row is written — re-raise a 404 on the movie's own child fetch, so the walk's gone-skip now covers a movie as it covers a set. `music()` (`:902-1007`) is a different shape again: one `music_database_lock` held for the whole library including network waits (`:904`), no restore points, no per-page commit.

### The Library port

`FullSync` reaches into nine duck-typed `Library` members: `database_lock` (×5), `music_database_lock` (×3), `claim_full_sync`, `release_full_sync`, `stamp_watermark_if_empty`, `refresh_libraries` (×2), `defer_playlist_poll`, `added/updated/removed`, and `getattr(self.library, "sync_failure_toasted", None)` (`:664`, because tests pass `library=None`). The tests reproduce that surface five different ways: `RecordingLibrary` and `ClaimLibrary` in `test_sync_full.py:107-119, 258-278`, `PublishLibrary(ClaimLibrary, RecordingLibrary)` (`:328-335` — multiple inheritance to combine two halves), a `SimpleNamespace` (`:206`), another `RecordingLibrary` in `test_sync_prune.py:56-77`, another `SimpleNamespace` in `test_sync_writers.py:1330-1336`. `local_reference_map` and `PRUNE_SERVER_TYPES` are hoisted to module level (`:57-62`, `:85-155`) so `library.py:36` can import the divergence probe's inputs from the orchestrator — the docstring at `:94-100` apologises for it.

`process_libraries` (`:367-393`) said "one bad library does not abandon the rest", but the `try` wrapped the `for` and `process_library` re-raises everything, so the first failure ended the loop and `failures` never held more than one entry. Fixed in Tier 0 (below).

### Downloader

`_get_items` (`:497-639`) is 143 lines at nesting depth six with two closures. `@stop` on a generator function (`:496`) runs its abort/offline checks once, at generator creation, never per page — the comment at `:584-586` admits the real mid-walk abort comes from the writers' own `@stop`. `GetItemWorker.run` re-queues its chunk on `ServerUnreachable` (`:798`) but **drops it on any other exception** (`:809-811`): those ids are lost until the recovery prune. The `/Items` filter block is typed out four times (`:198-222`, `:313-328`, `:405-418`, `:475-490`). The pager reads `limitIndex`/`limitThreads` from settings inside itself (`:505-506`), which is why every prune test needs a `FakeAddon`.

Monkeypatch totals for the slice: `test_sync_full.py` 18 calls / 8 targets, `test_sync_prune.py` 22 / 10, `test_changefeed.py` 0 / 0, `test_sync_db.py` 13 / 6. None of the four files runs `movies()`, `tvshows()`, `musicvideos()` or `music()`; only `test_sync_writers.py` runs `boxsets()`.

## 4. `views.py`: five concerns, three gate spellings, one verified defect

1,936 lines and 76 functions covering: the view table and server listing (`Views.__init__` … `views_hash`, `:568-719`), video node XML (`:826-1452`, ~700 lines), music node XML as module-level functions (`:346-546`, ~300 lines), smart playlists (`:853-887`, `:1753-1796`), skin window properties (`:1454-1751`, ~300 lines), and deletion/prune/migration (`:1798-1936`). `get_nodes` (`:721-824`) touches every one of them.

The module-level music functions and the `Views` methods duplicate five mechanics at ~100 lines a side: `_write_music_parent` ↔ `node_parent`, `_delete_music_folder` ↔ `delete_node_folder`, `_delete_music_nodes` ↔ `delete_nodes`, `_prune_music_nodes` ↔ `prune_nodes`, `music_library_views` ↔ `get_nodes`+`node_order`. They differ in accidentals: `os.makedirs` vs `xbmcvfs.mkdir`, `NODE_ROOT` vs the literal `"kofin"`, rewrite-whole vs parse-and-amend. Inside the 13 `node_*` builders (`:1230-1452`) the "ensure `<order>`" block appears ×4, "ensure `<limit>`" ×6, the playcount rule ×4, and `node_nextepisodes` (`:1240`) and `node_favepisodes` (`:1440`) are byte-identical. The "parse-or-create" `try/except` is copied five times (`:857-868`, `:976-1011` where the create branch is itself written twice, `:1122-1132`, `:1172-1186`, `:1210-1221`).

**The deletion gate has three spellings in one file**: literal `"kofin"` ×9 (`:1771, 1795, 1827, 1831, 1851, 1867, 1906, 1911, 1935`), `"kofin_"` ×1 (`:1885`), `NODE_ROOT` ×4 (`:504, 508, 537, 540`). Every deletion path was enumerated and each is gated or is a primitive whose callers gate — but nothing structural ties the spellings together, and the same file has the "exactly one spelling" rule for reference checksums.

**Verified defect — library rename empties its nodes.** Rename a synced library on the server and regenerate: `all.xml` and the `.xsp` carry both the old and the new tag rule under `<match>all</match>`, so the node requires both tags and lists nothing. `add_node` (`:1197-1203`) and `add_playlist` (`:878-883`) append a rule when none matches the new tag but never remove the old one; `_write_music_filter_node` (`:468-476`) documents exactly this failure mode and rewrites whole. Reproduced with a unit probe against the repo's own `test_sync_views` fixtures (rules `['Movies', 'Films']`). Not yet seen live.

Second drift: `window_clear` (`:1717-1743`) hand-lists sub-node suffixes for six nodes; `NODES` (`:131-143`) also has `genres/sets/random/recommended`, whose `.id/.path/.type/.artwork` props are written by `window_node` (`:1600-1603`) and never cleared.

Dead: `DYNNODES` (`:152-203`, 52 lines, zero references). Misleading: `MUSIC_DOWNLOADED_FILE`'s comment (`:79`) says the pruner names it; it does not. `order_media_folders`' docstring (`:1455-1457`) describes a mutation the body does not perform.

`test_sync_views.py` needs a real `kofin.db` and `sync.json` for every test (`Views.__init__` loads sync.json, `views_hash` opens kofin.db) and a nine-patch autouse fixture bridging `xbmcvfs.*` to the real filesystem, because the module mixes `os.path`/`os.makedirs` (24 sites) with `xbmcvfs` (26 sites) on the same paths.

**Ownership semantics disagree across four folders**: video nodes spare foreign files (`:1827`), music nodes spare (`:504`), video playlists spare (`:1771`), music playlists delete (`playlists.py:396-403`, `:492-497` — the folder is the boundary, by design and documented in the module docstring), and `clean.py` removes all four wholesale (`:301`, `:336`). CLAUDE.md said the prefix gated deletion under both `Kofin/` folders; it gated only the video one. Corrected in Tier 0.

## 5. The transplant premise is stale

CLAUDE.md said the writers "were proven equivalent to the fork's against real libraries (`tests/live/ab_diff.py`), and that proof only holds while semantics stay put."

- The harness ran **once, on 2026-07-17 — the day of the port commit `aabd711`** (`tests/live/results/S2.2-ab-equivalence.md`), for **movies only, 21 fields**, keyed on IMDB id. TV, music and boxsets were never A/B'd. `ab_diff.py` has one commit. `docs/phase3-implementation-plan.md:103` promised it as the guard for extras and it was not run; `docs/benchmark-audit-plan.md:15` retires it as "cannot say who is right". It would fail today by design: `7c4c1fd` writes per-provider uniqueid rows the fork never wrote.
- Post-port lines by `git blame`: `writers/movies.py` **53%**, `kodidb/music.py` **56%**, `kodidb/movies.py` **54%**, `fields.py` **41%**, `writers/tvshows.py` 25% (61% of that is "Deviation from the fork" commentary), `writers/music.py` 23%. Untouched: `kodidb/musicvideos.py`, `kodidb/artwork.py`, `kodidb/tvshows.py` (lost one dead method), `obj.py`. Of ~40 post-port commits, 4 are mechanical, ~12 are features living inside writer methods (extras, video versions, critic ratings, per-provider uniqueids, downloads tag + reassert, boxset guard/heal/state, pooled-series re-homing, refusal reporting, music sources, transcode paths, blank-artist credit, discography rekeying), ~20 change which rows get written, most of them bug fixes annotated in place.
- The rule actually followed is *keep the fork's dict/spec/positional shape, deviate where the fork was wrong, write it down, prove it at L2*. That is a good rule. The L2 suite (`test_sync_writers.py`, 100 tests × 4 schema legs with byte-identical idempotency dumps) is a strong oracle — but ~80 of its tests pin the deviated behaviour by name, so it proves kofin against kofin, not against the fork.
- One genuine direction violation: the writers import the shell. `writers/movies.py:22-24` and `writers/tvshows.py:27-29` import `kofin.downloads` (`TAG`, `repoint`, `store`) and call `reassert_on` inside `movie()`/`episode()` (`:208`, `:589`); `writers/music.py:11,13` import `kofin.downloads.repoint` and `kofin.sync.musicsources` and call them inside `song()`/`album()` (`:417-419`, `:244-247`). `musicsources.py:25` imports `kodidb.music` back.
- The cost of the obj-dict convention, measured across the four writers: **928 `obj["Key"]` literals** (88 distinct keys, ~16 invented mid-method rather than in `obj_map.json`), **186 `values()` calls against 128 positional spec lists** (70 in `queries.py`, 21 in `queries_music.py`, 37 in `queries_map.py`), 18 `temp_obj = dict(obj)` copies, and `e_item[N]` indexed positionally at 55 sites although `kofindb.py:19-32` installs a namedtuple row factory. A mistyped key fails at runtime as `KeyError` inside `values()`.
- `mypy.ini` runs `check_untyped_defs=False` over `kofin.sync.*`, so function bodies are not checked. Flipping it on gives **157 errors in 13 files — 61 of them `"Kodi" has no attribute "cursor"`** (the base class never declares what every subclass assigns before calling `Kodi.__init__`); the remainder sit mostly in the shell-side pipeline (`library.py` 34, `full_sync.py` 22, `views.py` 14, `downloader.py` 10) and include real ones (`library.py:2845` passes `str | None` to `dict.get`).
- `kodidb/`: 20 `create_entry*` methods, all `SELECT coalesce(max(id),0)` + 1, with `create_entry_unique_id`/`create_entry_rating` duplicated verbatim in `movies.py:39-47` and `tvshows.py:24-32` while the base class's `sync_unique_ids` depends on subclass methods `MusicVideos` lacks (`kodi.py:110-112`). `kodidb/music.py` branches on `version_id < 72/74/80` at 17 sites the schema gate (MyMusic 83/84 only) makes unreachable; `add_stream_video_obj_19` survives in `queries.py:319` although `kodi.py:4-6` says the Kodi-19 arm is gone. The class-level person cache (`kodi.py:18-54`) is process-lifetime mutable state not listed among CLAUDE.md's exemptions, and its safety argument fails if Kodi's own Clean Library prunes unlinked actors mid-session.

## 6. What is fine, and what is dead

**Leave alone:** `changefeed.py` (pure planners, zero monkeypatches, 40 tests — the model for the rest), `newcontent.py`, `widgetstate.py`, `clean.py`, `playlists.py`, `musicsources.py`, `kodisetup.py`, `schema.py`, `db.py`. Each is single-purpose, ≤ 350 lines, and tested at the seam it exposes.

**Dead code, confirmed by grep (definition only):** `views.DYNNODES`; `downloader.get_episode_by_show` (`:105-117`, identical to `get_episode_by_season` minus `SeasonId`); `shims.JSONRPC` + `get_grouped_set` (`:219-264`); `kofindb.get_kodi_ids_by_media_folder` (`:217-221`) and its query (`queries_map.py:94-100`); `db._kofin_db_path` (`:56-57`); `FullSync.sync`/`update_library` class attributes (`:171-172`, Borg leftovers that force every test to set `sync.sync` by hand); `GetItemWorker.source` (written, never read); the 17 `version_id < 82` arms in `kodidb/music.py` and `add_stream_video_obj_19`.

## 7. Recommendation

### Tier 0 — truth (hours) — **done 2026-08-27**

- Rewrite the CLAUDE.md transplant paragraph to say what protects correctness today: the L2 suite across every gated schema, in-place deviation notes, and the idempotency dumps as the oracle for any restructure. Stop citing `ab_diff.py` as a live proof.
- Correct the playlist-gate sentence: `playlists/video/Kofin/` is prefix-gated, `playlists/music/Kofin/` is folder-gated (`playlists.py` module docstring). Same folder name, opposite semantics — the doc must say so.
- Make `process_libraries` do what its docstring says: per-library `try`, collect and continue, re-raise `LibraryExitException` immediately. A library that syncs last after a failure is published from the loop, because `start()` re-raises before its end-of-sync refresh. Tests: two-libraries-first-fails, exit-abandons-the-rest, last-published-after-failure.

### Tier 1 — cheap, high yield (days) — **landed 2026-08-27 as phase 1, PR #191** (`docs/sync-refactor-phase1-plan.md`; live gates in `docs/testing-plan.md` §S-P1)

- Declare `cursor: sqlite3.Cursor` on `Kodi`, fix the ~25 real errors, flip `check_untyped_defs=True` for `kofin.sync.*`.
- Delete the dead code in §6; L2 across MyMusic 83/84 proves the music arms unreachable.
- Extract the one walk in `full_sync.py` — `_walk(library, item_type, key_suffix, writer_cls, apply, describe, dialog)` — from the four copies. Closes the movies/musicvideos/boxsets mid-page-404 gap for free and leaves one percent/heading expression instead of four plus eleven. Music keeps its own shape until a live test blesses per-page locking.
- Re-queue the chunk on generic exceptions in `GetItemWorker.run`.
- Hoist the `kofin.downloads`/`musicsources` calls out of the writers behind a `post_write(obj, item)` seam (after `writers/movies.py:201`, `tvshows.py:585`, `music.py:411`; tag injection needs a pre-`add_tags` seam at `movies.py:166-180`). `test_downloads_repoint.py:89-99, 373` and `test_sync_writers.py:3051-3140` already pin the observable outcome, so the change is dump-verifiable.

### Tier 2 — structural (one to two weeks), safety net first — **landed 2026-08-27 as phase 2, PR #192** (`docs/sync-refactor-phase2-plan.md`; live gates in `docs/testing-plan.md` §S-P2)

Before moving anything: a golden-XML test of the full generated node tree (the DB A/B proof never covered node XML), a two-libraries-first-fails test (now landed), and real `UserDataWorker`/`RemovedWorker`/`SortWorker` tests.

`library.py`:
- Pull the refresh/visibility policy (`:1158-1540`, ~400 lines: `refresh_libraries`, `_moved_databases`, `_arm_refresh_settle`, `flush_refresh_settle`, `_reload_skin_*`, `_video_content_hidden`, `_music_content_hidden`, `_refresh_music`, `refresh_added`, `metadata_pending`) into its own module beside `widgetstate.py`, with `Library` calling `arm(databases)` and `tick()`.
- Replace the seven clocks with one deferred-action primitive (due time, hold cap, floor/ceiling ladder) so a new "wait, then do" no longer means a new attribute pair and a new `flush_*` hook.
- Turn `process_commands`' `if/elif` into a dispatch table; one worker drain loop parameterised by the type→method dispatch, with `source`/`db_file` as constructor arguments.
- Name the `Library` port that `FullSync` uses (locks, claim/release, enqueue, refresh, failure set) as a small object so the five fakes become one.

`full_sync.py`: split restore points (`:437-551`, pure), prune (`:1010-1150` + `local_reference_map` + `PRUNE_SERVER_TYPES`, so `library.py:36` imports the probe's inputs from the prune module), boxsets (`:1153-1285`) and removal (`:1288-1383`) into their own modules; move the claim and its toast from `__init__` to `__enter__`; inject the sync.json loader. `FullSync` shrinks to `libraries/start/process_libraries/process_library` (~250 lines).

`downloader.py`: move `GetItemWorker` next to its only consumer; give `_get_items` `limit`/`threads` and a `should_stop` callable so pager tests drop the `FakeAddon`/`FakeWindow`/`_monitor` patches; yield `(items, restore_point)` instead of re-using and clearing one dict (`:630-634`) — the transplant callers `writers/movies.py:649` and `tvshows.py:263` only iterate, but the change crosses the boundary and needs saying so.

`views.py`: one deletion primitive (`is_managed`, `remove_managed_entries`, `remove_empty`) with one gate spelling, so a deletion path that forgets the gate cannot be written; split into view-table, video nodes, music nodes (already module-level — a move), playlists (fold into `playlists.py`, which already owns `FOLDER_NAME`/`FOLDER_ICON`), and skin props (a documented wiki contract — keep names stable); rewrite-whole instead of parse-and-amend for `add_node/add_dynamic_node/add_single_node/add_playlist/node_index` (fixes the rename defect; nothing user-editable lives in those files, `order` and `icon` are already overwritten every pass); table-drive the 13 `node_*` builders and derive `window_clear`'s suffix list from `NODES`.

### Not worth doing

- Replacing the obj-dict + spec-list convention with dataclasses. It is the whole slice — 928 key sites, 186 `values()` calls, 128 spec lists, `queries_map.py` — and the only thing it buys over Tier 1's typing flip is `KeyError`-at-runtime for a mistyped key. Do it only if the writers keep changing at the current rate (~1,900 lines in six weeks); if the deviation work is done, the shape is ugly but stable.
- Re-running `ab_diff.py` as a gate. It would need extending to ratings, uniqueids, tags, versions, TV and music, the fork-synced reference database no longer exists in a comparable state, and every known deviation would have to be allow-listed — at which point it proves what the L2 suite already proves. If a neutral oracle is wanted, `docs/benchmark-audit-plan.md` (compare each addon to the server) is the instrument.
- Reshaping `music()`'s lock scope or the pager's semaphore depth: both were measured live (`downloader.py:553-564`, `docs/library-thread-stop.md`).
