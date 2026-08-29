# Audit fixes: the hardening pass before shell phase 2

| Field | Value |
|---|---|
| **Date** | 2026-08-29 |
| **Source** | The 2026-08-29 hardening/performance/robustness audit (28 findings: F1–F9, M1–M8, R1–R10, A4-1), as verified the same day against this tree — every finding re-read at its cited lines, the four claims that cite Kodi checked against a 21.3-Omega checkout, F2's trigger executed on jf12, F1 re-timed, A4-1 counted on the local profiles. The verification changed three findings (A4-1's rule, F7's status list, the thread premise under F6/R5) and closed one (M5); the rest hold as written. Line references here are against `271a4c5` (the `fix/music-restore-path` tip). |
| **Branch** | `fix/audit-hardening`, stacked on `fix/music-restore-path` (PR #203), draft PR against it; one commit per item, each revertible on its own. Merge order becomes #200 → #201 → #202 → #203 → this → `refactor/shell-phase2`. |
| **Scope** | Every finding the verification confirmed and could specify: five High (F1, F2, F4, A4-1, and F5 promoted), the Medium set minus F3/F9, every Minor, the transplant tidy-ups (R1/R2/R3/R6), the F6 instrument, R5, and a linter in the gate. Not F3 (Tier 3, planned elsewhere), not F9 (needs a design the finding does not supply), not M5 (not a cost), not the audit-completion plan's reading passes (`docs/audit-completion-plan.md` is a separate work order). |
| **Rule** | This branch lands **before** phase 2 because phase 2 is proven by identity oracles — keyed dump, node trees, the route golden — that any behaviour change would force to re-baseline, and A4-1 moves every movie's dump row. Each item carries the test that would have caught it; each fix inside `sync/` or `syncplay/` is a named defect fix on a failure path, gated by the L2 dumps; nothing here is a semantic improvement. **Nothing is deleted through jf12 and no file is written under any jf12 library path** — F2's live gate is a user-policy change, restored afterwards. |

## 1. Why these, and in this order

Blast radius first, then dependencies, then the one-liners together so the branch history reads as a ledger of the audit.

| # | Item | Closes | Oracle |
|---|---|---|---|
| H0 | Safety nets and the before-set | — | the transport contract harness runs on the before build; profile counts and dumps recorded |
| H1 | The kofin.db row factory memoised | F1 | 40× per row; `widgetstate.fingerprint("video")` timed on a real library |
| H2 | An unversioned film is the Standard Edition | A4-1 | L2: single source → 40400, the named-label pin intact, zero orphan type rows on removal; live: 1,799 → 0 after Repair |
| H3 | An empty view listing is not a deletion order | F2, R7 | unit: every-whitelisted-view-removed refuses; live: kofin-test's access withdrawn and restored, rows unchanged |
| H4 | 3xx and non-JSON bodies enter the error taxonomy | F4 | contract test, both transports; a redirecting address fails at login with the Location named |
| H5 | Transient statuses ride the retry ladder | F7 | contract test: 503-then-200 succeeds within the GET budget; POST unchanged |
| H6 | Kodi's Clean library resets the people cache | F5 | unit: `OnCleanFinished` → reset; live: cast correct after remove → Clean → Repair |
| H7 | The SyncPlay leave cannot hold teardown | R4 | unit: offline skips, online bounded; live: outage teardown while in a group under 10 s |
| H8 | The one-liners | F8, R8, R9, R10, M2, M3, M4, M6, M7 | one unit test each |
| H9 | IPC payloads travel as hex | M1 | round-trip with a quote and a backslash; live NotifyAll |
| H10 | Transplant tidy | R1, R2, R3, R6 | L2 dumps byte-identical; a `youtu.be` trailer parses; an engine test for `obj.py` |
| H11 | The claim wait, measured | F6 | one log line; a Bravia album played |
| H12 | The removal confirmation off the callback thread | R5 | unit: `onSettingsChanged` returns before the dialog; live: a removal declined and confirmed |
| H13 | A linter in the gate | M7, R1's class | `tox` and CI run ruff; the tree is clean on the chosen ruleset |
| H14 | End-to-end, both rigs | — | the S1-P1 identity set unchanged; the dump moves only where H2 says |

## 2. The rigs

Native Omega 21.3 with the `kofin-test` profile (production 10.11.11, 1,784 movies — the A4-1 scale) and `kofin-jf12` (jf12 v12, 713 items — the F2, F5 and teardown scenarios, where a lost library costs minutes). The Piers flatpak (MyVideos148/MyMusic84) for the L2-parity legs; it exited cleanly after S1-P1 and needs relaunching (`tools/dev-install.sh --flatpak`). The Bravia for H11 only — the claim wait is a per-device number. jf12's admin credential lives in `targets.env` and is read inside the harness, never printed; the two probes from the verification (`f2_probe.py`, `ms_name_probe.py`) go into `tests/live/harness/` at H0.

## 3. The oracles

The unit suite (2,708 at `271a4c5`) plus the tests each item adds. The L2 writer suite across every gated schema: byte-identical dumps for H10, and for H2 a dump that differs from the before-set **only** in `videoversion.idType` and the `videoversiontype` rows — anything else moving is a finding. The keyed live dump (`tests/live/dump_diff.py`) on both profiles, same rule. The S1-P1 identity set (device-profile JSON, node trees, `S1-P1.0-before/`) — nothing here touches what it measures, so it must not move at all. And four numbers recorded before and after: `videoversiontype` rows per profile, `fingerprint("video")` milliseconds, teardown seconds with a group joined, and the claim wait distribution.

## 4. The items

### H0 — Safety nets and the before-set

**The transport contract harness.** `tests/unit/transportserver.py`: a `http.server` on a loopback port, driven by a script of canned answers (status, headers, body) per path, run for **both** transports — `plugin_transport()` (`core/stdhttp.py`) and the requests `Http` (`core/http.py:132–145`, `:250–285`). It is what pins H4 and H5, and it must pass on the before build with the *current* behaviour asserted (302 → `{}`, HTML 200 → `ValueError`, 503 → `HttpError` first try) so the H4/H5 commits flip named assertions rather than add them.

**The before-set.** `tests/live/results/S-H0-before/`: keyed dumps of `kofin-test` and `kofin-jf12` (WAL included — `kodi-db-snapshots-need-wal`), `SELECT count(*) FROM videoversiontype WHERE owner != 0` and the unreferenced subset per profile (verification: 1,788/6 master, 1,799/15 kofin-test, 350/344 kofin-jf12), the fingerprint timing from H1's harness, and a fresh S1-P1 identity capture to confirm nothing drifted since `94c9338`.

**The probes committed.** `tests/live/harness/jf12_user_policy.py` (create a throwaway user with no folders, query `/UserViews` and `/Library/MediaFolders`, delete the user; verified answer on v12: `200 {Items: 0}` and `403`) and `jf12_mediasource_names.py` (`MediaSource.Name` against the file stem; 6/6 equal). Both read credentials from the env files and print none.

### H1 — The kofin.db row factory memoised

`sync/kofindb.py:16–25` builds a `namedtuple` class per row; every `JellyfinDatabase` read pays it (`widgetstate.py:144`, `prune.py:65/:217`, `shims.jellyfin_item`, the library's own reads). Memoise the class on the tuple of column names (`functools.lru_cache(maxsize=64)`), keep the field access every caller relies on. Verified: 92 µs → 2.3 µs per row on 20,000 five-column rows; the audit's fixture put `fingerprint("video")` at 1.918 s → 0.134 s.

Test: the same column tuple yields the same class object; two different column tuples do not; the suite unchanged. Harness: `tools/perfprobe.py` gains a `fingerprint` probe (copies of a profile's `kofin.db` + MyVideos, read-only) so the number is re-runnable — it is the H0 before-set's fourth figure. Not a perf change to `_reference_digest` itself; the audit measured that its Python is 19 ms of the 1,989.

### H2 — An unversioned film is the Standard Edition

`kodidb/movies.py:98–107` sends every non-empty, non-"standard edition" `MediaSource.Name` to `get_extra_type_id` (`:84–96`), which mints a `videoversiontype` row per distinct name; `writers/movies.py:184–188` feeds it the primary source's name, and Jellyfin's name for a single-file movie **is the file stem** (6/6 on jf12; the production rows read `Nations Champ Rugby N.Z. v Ire. 2026_07_18_07_30_00`). Nothing sweeps them: Kodi's only deletes are the v128 and v131 migrations in `CVideoDatabase::UpdateTables` (`VideoDatabase.cpp:6360`, `:6396`), and v128's exists because "now they're all displayed in the version type selection for movies" — the same defect. The picker query has no join (`CVideoDatabase::GetVideoVersionTypes`, `:12669`). Counted: 1,788 rows for 1,782 movies on the master profile, 0 movies with more than one version.

**The rule** — not the audit's "no alternates → 40400", which would break `test_movie_single_named_primary_version` (`test_sync_writers.py:1069–1088`, a deliberate pin: one source named *Director's Cut* keeps the builtin). Instead: *a name equal to the source's own file stem is Jellyfin's default for an unversioned file and maps to 40400; any other name is a label and resolves as today.* `resolve_version_type(name, path)` compares `name.strip().lower()` to the stem of `MediaSource.Path` (basename, extension dropped, the same normalisation Jellyfin applies — verify the trailing `" - "` case seen on jf12's *20,000 Leagues*); a missing path falls back to today's behaviour. With alternates present Jellyfin strips the folder prefix so the names are suffix labels and the existing `versions()` pass (`writers/movies.py:338–385`) is untouched.

**The sweep.** `Movies.delete` (`kodidb/movies.py:128–134`) and `delete_extra_asset` (`:120–123`) drop the version row and leave the type; add `sweep_orphan_version_types()` — Kodi's own v128 statement: `DELETE FROM videoversiontype WHERE id NOT IN (SELECT idType FROM videoversion) AND owner = USER AND itemType = VERSION` (owner from `schema.VIDEO_ASSET_OWNER_USER`, itemType from the seeded row as today) — called after each movie removal and once at the end of a movies walk. Existing referenced rows convert through the normal write path: `movie_update` already calls `set_video_version_type` (`:249`), so a Repair rewrites every film to 40400 and the walk-end sweep takes the rows. No bespoke migration — a stem comparison is impossible from Kodi's side (kofin's `files.strFilename` is the plugin URL), and Repair is the documented remedy.

Tests (L2, every gated schema): the fixture movie's single source named as its stem → `idType 40400` and **no** new `videoversiontype` row; the named-label pin unchanged; `movie_with_versions` unchanged; write → remove → zero `videoversiontype` rows with `owner != 0` (the zero-orphan invariant extended to this table); write with a stem name, rename the file, rewrite → the old type row is gone. Docs: CLAUDE.md gains the rule beside the extras/videoversion constraint; `changelog.txt` tells users to run Repair to clear the version list.

### H3 — An empty view listing is not a deletion order

`views.get_views` (`views.py:112–154`) stamps `SortedViews` from the listing and hands every view row absent from it to `REMOVE_LIBRARY`, which `library.remove_library` (`library.py:1976–1991`) turns into `removal.remove_library` — every synced row of that library out of MyVideos/MyMusic. `complete` (`:72–110`) only drops on an exception from `/Library/MediaFolders`; a 403 (every non-admin) and a successful empty `/UserViews` leave it True. **Executed on jf12 v12:** a user with no folders and no Live TV gets `/UserViews → 200 {Items: 0}` and `/Library/MediaFolders → 403` — the exact shape. The house rule already lives in `boxsets.sweep_stale` (`boxsets.py:97–120`) and the prune's `get_existing_ids`; this is the third and largest case.

The floor, placed **before** the `SortedViews` stamp — an empty stamp would also regenerate an empty node tree through `views_hash` (`:165–167`): if the whitelist (`sync["Whitelist"]`, ids compared with `Mixed:` stripped) is non-empty and the listing contains none of it, warn with the count and return without stamping, removing or saving. A partial loss proceeds as today — a library really gone for this user is still removed. With Live TV granted the listing holds one view, which is why the gate is "none of the whitelist", not "no views".

Tests: FakeApi answering `Unauthorized` for media folders and `{Items: []}` for views against a two-library whitelist → no `REMOVE_LIBRARY`, no `SortedViews` change, a warning; the same with one library still listed → the other is removed as before; an empty whitelist with an empty listing → unchanged (nothing to protect). Live S-H3 below.

### H4 — 3xx and non-JSON bodies enter the error taxonomy

`run_ladder` returns anything under 400 (`http.py:120–127`); the stdlib exchange hands a 302 back as a `Response` (`stdhttp.py:260`) whose empty body `Api.get` reads as `{}` (`api.py:125`), while requests follows redirects (`allow_redirects` defaults on, `http.py:250–285`) — so the service works and the plugin lists nothing. A non-JSON 200 raises `ValueError` from `Response.json` (`stdhttp.py:70–71`, and requests' equivalent) past every `except JellyfinError`. Both verified by reading; the audit executed them.

Fix, both transports: `run_ladder` raises `HttpError(status)` for 300–399 with the `Location` header in the message, and the requests transport passes `allow_redirects=False` so it reaches the same line; `Api` decodes through one `_json(response)` that wraps `ValueError` in `HttpError(response.status_code, "non-JSON body")` at `get` (`:116–126`), `post` (`:128–148`) and `_get_list` (`:186`). Nobody has a working redirect setup today (the plugin already lists nothing), so refusing on both sides costs no one and names the cause at login: the connect path (`plugin/account.py`) surfaces the Location so the user fixes the address.

Tests: the H0 harness — 302+Location → `HttpError` naming it, HTML 200 → `HttpError`, JSON 200 unchanged, each on both transports; `browse()` reaches its "browse failed" path on an `HttpError` from a listing.

### H5 — Transient statuses ride the retry ladder

Only `transport_errors` continue the ladder (`http.py:94–105`); every status ≥ 400 raises at once, so a 502 while a proxy waits for Jellyfin, a 503 during warm-up or a 429 spends none of `METHOD_RETRIES` (`:44`). Add `RETRY_STATUSES = (429, 502, 503, 504)`: for those, when attempts remain, log and continue exactly as a transport error does; on the last attempt raise `HttpError` as today. POST keeps zero retries and is unaffected. **Not 500**: Jellyfin answers deterministic 500s for broken items, and three replays with backoff would add ~3.5 s to every such request in a walk for nothing.

Tests: 503, 503, 200 on a GET succeeds with three requests logged; 503 ×4 raises `HttpError(503)`; a POST answered 503 raises on the first; 500 raises on the first for every method.

### H6 — Kodi's Clean library resets the people cache

`Kodi._people_cache` (`kodidb/kodi.py:31–65`) is primed once per process and reset only by tests. Kodi's Clean library deletes every actor with no surviving link (`CVideoDatabase::CleanDatabase`, `VideoDatabase.cpp:10291–10297`); kofin never deletes actor rows, so its own removals and prunes leave exactly that set. `actor_id INTEGER PRIMARY KEY` has no `AUTOINCREMENT` (`tests/fixtures/myvideos131.sql:10`), so a freed id at the top of the table is reused — a stale cache entry then names the **wrong** actor, not merely a missing one. And `docs/benchmark-report.md:156` already records that Kodi's Clean removes every kofin movie row, after which the user runs Repair against that cache.

Fix: `main.onNotification` gains `VideoLibrary.OnCleanFinished → Kodi.reset_people_cache()` beside the existing `AudioLibrary.OnScanFinished` arm (`main.py:1008`); the reset is safe mid-batch because `_prime_people_cache` re-primes lazily on the next `get_person`. Docs: `docs/clean-databases-plan.md:3` says Kodi's clean "cannot remove plugin-path rows"; the benchmark measured that it does (a plugin path survives only through `CPluginDirectory::CheckExists`, `VideoDatabase.cpp:10029`) — reword to match the measurement.

Tests: the notification resets both class attributes; a write after the reset re-primes from the table and links the new id.

### H7 — The SyncPlay leave cannot hold teardown

`SyncPlayManager.stop` (`syncplay/manager.py:115–127`) opens with `_api_raw("syncplay_leave")` → `Api.post` (`api.py:595–596`, `:128–148`) at `DEFAULT_TIMEOUT` (6 s, 30 s; `http.py:26`) with no retries and no abort check, on the service main thread inside `Service.stop`'s `_stop_syncplay()`. Against a server that vanished without closing the socket that is up to 36 s past Kodi's five-second grace — the shape `docs/library-thread-stop.md` exists for.

Fix: skip the leave when `state.is_offline()`; otherwise send it with its own budget — `Api.post` gains an optional `timeout` (the transport's `request` already takes one, `http.py:256`), and `syncplay_leave` passes `(2.0, 3.0)`. A leave is a courtesy to the group; teardown does not wait on it.

Tests: offline → no request; online → the request carries the short timeout; a transport error is swallowed and `_leave_locally` still runs. Live S-H7 below.

### H8 — The one-liners

Each its own commit and test; they share a section because none takes more than a few lines.

- **F8** `full_sync.py:842/:870/:898`: `max(total_items, 1)` for the denominator and the percent clamped to 100 — `get_item_count` (`downloader.py:162–178`) counts `/Items` while artists walk `/Artists`. Test: a music walk with `TotalRecordCount 0` and one artist completes.
- **R8** `kodiuserdata.py:140–170`: the resume-bookmark check (`kodirpc.resume_seconds`, a local read that works offline) moves above the `is_offline()` branch, so the offline park (`:123–138`) can no longer zero a position the online push would have refused. Test: offline, bookmark present → nothing parked.
- **R9** `timesync.py:122–127`: `T1`/`T2` through `.get()` with a None check that returns False, so `_measure_http` is reached. Test: a reply with `T0` only falls through.
- **R10** `auth.py:38–56`: the default-port rule becomes `parts.port is None and parts.scheme == "http"` (urlsplit handles the bracketed literal). Test: `[fd00::1]` → `http://[fd00::1]:8096`; `host:8920` and `https://host` unchanged.
- **M2** `ipc.py:172`: `hmac.compare_digest`; `rotate_nonce` (`:140–144`) opens the temp file with `os.open(..., O_CREAT|O_WRONLY|O_TRUNC, 0o600)` so there is no 0644 window. Test: the nonce file's mode.
- **M3** `ws.py:15–18`: the `sys.modules["numpy"] = None` line and its comment go — zero numpy references in the pinned 1.6.4 (`addon.xml:6`), checked in both installed copies.
- **M4** `log.py:45–50`: `for secret, replacement in list(_secrets.items())`. Test: a secret registered from another thread mid-`mask` does not raise.
- **M6** `workers.py:169–170` and `full_sync.py:564–565`: the note — Kodi's database commits first so a crash between the two leaves a visible duplicate to rewrite, never a mapping without rows that `check_unchanged` would skip forever.
- **M7** `downloads/repoint.py:39–40` (the duplicate `store` import) and `core/stdhttp.py`'s six unused imports (`random`, `time`, `BACKOFF_BASE_SECONDS`, `HttpError`, `ServerUnreachable`, `Unauthorized`). H13 keeps them out.

### H9 — IPC payloads travel as hex

`ipc._encode` (`ipc.py:184–188`) escapes `"` after `json.dumps`, so a value containing a quote becomes `\\"`, and `CUtil::SplitParams` (`Util.cpp:1121–1129`, "only every second character can be escaped") reads that as an escaped backslash then a closing quote — the parameter ends early. `test_ipc.py:19–24` models the unescape as a plain replace, so it cannot see this. Every payload today is an id, an int, `auto:<id>` or a Jellyfin type string (`ipc.notify` sites surveyed: `views.py:152`, `downloads/auto.py`, `plugin/actions.py:141–550`, `plugin/streams.py:307`, `service/player.py:1047/:1066`), so it is a trap for the next field, not a live bug.

Fix: encode as the hex form `decode` (`:190–208`) already accepts — `'"[%s]"' % hexlify(json.dumps(data).encode())` — one receiver (`main.py:1030`). Tests: a round trip through a real `SplitParams`-style parse of a payload holding `"`, `\` and a non-ASCII name; `test_notify_encodes_payload` updated to the new wire shape. Live S-H9 below.

### H10 — Transplant tidy

Four defects on removal and mapping paths, all inside the boundary, all proven by the L2 dumps not moving.

- **R1** — nine `for…else` blocks whose loops contain no `break`, all in deletion code (`writers/tvshows.py:233, 858, 875, 882, 927, 948`; `writers/music.py:708, 730, 749`, the `for` lines). Each `else` runs unconditionally today; dedent them so a future `break` cannot change what is deleted. Behaviour-preserving by construction; the dumps gate it.
- **R2** — `get_child` is dead in both writers (`tvshows.py:989`, `music.py:804`; no caller in `lib/` or `tests/`) while `prune.py:38` and `:95` cite it as the reference for their own walk. Delete both and rewrite the two comments to describe the walk.
- **R3** — `obj["Trailer"].rsplit("=", 1)[1]` (`movies.py:271–275`, `tvshows.py:355–359`) raises `IndexError` on `youtu.be/<id>` and `/shorts/<id>`, caught as a trailer-less item with a full `LOG.exception` per film. A `youtube_video_id(url)` helper beside `gone_on_fetch` in `fields.py:335`, parsing `v=`, `youtu.be/`, `/shorts/`, `/embed/`, returning None for anything else — None keeps today's "no trailer" without the traceback. Tests: the four shapes and a non-YouTube URL, both writers.
- **R6** — `__recursiveloop__` (`obj.py:130–140`) discards the recursive generator (no `yield from`), and `__filters__` (`:153–171`) overwrites `result` so only the last filter decides. Unreachable today (`obj_map.json` has no two-colon key and no `&` filter). Fix both — `yield from`, and AND across filters with the single-filter result unchanged — with an engine test that feeds a two-level key and a two-filter query; the dumps prove the live mappings did not move.

### H11 — The claim wait, measured

`onPlayBackStarted` (`player.py:796–820`) calls `_claim` (`:1709–1740`) inline: up to `BACKFILL_GRACE_SECONDS` (3 s, `:63`) for library audio and downloaded video, up to `CLAIM_TIMEOUT_SECONDS` (10 s, `:58`) while `getPlayingFile()` is empty. The premise needed checking, and the source settles it: Kodi delivers Python player *and* monitor callbacks on **the thread that created the object**, and only from inside a Kodi API call — `PythonCallbackHandler::isStateOk` requires the creating `PyThreadState`, and `makePendingCalls` runs from `Monitor::waitForAbort` (every 100 ms of its loop, `Monitor.cpp:57`), `xbmc.sleep`, `Window.doModal` and `DelayedCallGuard`'s destructor. So `_claim`'s `waitForAbort(0.25)` **delivers** the pending `Player.OnPlay` notification (`main.py:1004–1007` → `submit_backfill`, `player.py:974–988`) inside the wait: the grace is satisfiable, and the wait is bounded by the backfill's server GET — or the full 3 s only when the server is away. What it delays is kofin's own `onAVStarted` (the SyncPlay Ready trigger, `apply_default_tracks`), not other add-ons.

So: instrument, do not restructure. One `LOG.info` at the end of `_claim` with the elapsed wait, the branch taken (claimed / backfill grace / foreign / timed out) and the media kind. Then S-H8 on the Bravia. If the measured wait is the GET latency, the finding closes with the number; if it is the grace, the claim moves to the reporter in a follow-up. Note that phase 2's P2.3 moves `_claim` into `service/libraryclaim.py`; the log line travels with it.

### H12 — The removal confirmation off the callback thread

`_confirm_removals` (`settings_apply.py:408–435`) opens `xbmcgui.Dialog().yesno` inside `Service.onSettingsChanged` (`main.py:1163`), which by H11's thread rule runs on the service main thread — the run loop (`main.py:249`) and every kofin player and monitor callback stall until the person answers. Other add-ons are unaffected (the kodi-drive note claiming a shared thread is wrong by the source; see §5). The confirmation stays — it is the last gate before rows are deleted — and moves.

Fix: `_library_selection_changed` (`:366–400`) computes `additions`/`removal_entries` synchronously as now, then hands the confirm-and-enqueue tail to a one-shot worker through `ports.spawn_once` (`ports.py:148`), keeping the slot on `SettingsApply` so a second settings save while the dialog is up gets the existing busy behaviour rather than a second dialog. The tail is exactly the code from `:388` to `:399`. Tests: `onSettingsChanged` returns before `yesno` is answered (FakeDialog blocks on an event); confirmed → `RemoveLibrary` enqueued; declined → `librarySelection` restored, nothing enqueued; a save during the dialog does not open a second one.

### H13 — A linter in the gate

`tox` runs black, mypy and pytest and nothing that names an unused import, a duplicated import or a `for…else` without `break` (M7, R1). Add a `ruff` env to `tox.ini` and a `lint` job to `.github/workflows/ci.yml` beside `black`/`mypy`, on `F`, `B` and `PLW0120` to start — the families that found the audit's mechanical items, with no `noqa` needed once H8 and H10 land. `RUF012` (class-level mutables, the no-module-global-state doctrine) is worth a second step after the exemptions carry per-line comments; not this branch.

### H14 — End-to-end, both rigs

Deploy the tip to both rigs, bounce, and re-run the S1-P1 identity set — device-profile JSON, forced node regeneration, the listing captures — unchanged. Then on each profile: a Repair, and `dump_diff` against the H0 before-set moving **only** in `videoversion.idType` (→ 40400 for every single-version film) and the `videoversiontype` rows (the kofin-owned count → 0, or → the number of genuine labels); the four numbers recorded after; the `changelog.txt` entry and the CLAUDE.md constraints written.

## 5. Not in this branch

- **F3** — the music pass's single transaction (no `commit()` inside `music()`, `full_sync.py:813–920`; `Database.__exit__` rolls back on any exception, `db.py:120–140`). Confirmed and real, and Tier 3 of the sync refactor by design (`docs/sync-refactor-phase2-plan.md`). A4's oracle found music byte-identical between incremental and full walks, so the cost is restart-from-zero on interruption and a WAL the size of the library, not corruption.
- **F9** — a zero-playlist answer empties the managed folder (`playlists.py:432–500`). Confirmed, but a bare floor makes a user's deletion of their last playlist permanently stale; the honest fix is "prune only when the previous poll also answered zero", which needs state across polls in `_maybe_refresh_music_playlists` (`full_sync.py:293`). Churn, not loss; a follow-up with that design.
- **M5** — closed. `DialogProgressBG::update` is three setters under a `CCriticalSection` (`Dialog.cpp:591–605`, `GUIDialogExtendedProgressBar.cpp:31–41`); there is no per-item GUI cost to measure.
- **M8** — known (`docs/dynamic-libraries-plan.md` §2, W7).
- **The kodi-drive correction.** `kodi-announcements` §"Handlers run on a thread every add-on shares" is contradicted by `PythonCallbackHandler::isStateOk` and the `makePendingCalls` call sites above: callbacks run on the add-on's own creating thread, serially, only while it is inside a Kodi API call, and re-entrantly. A `kodi-drive:contribute` PR with those citations, from the session that lands H11 — not a change to this tree.
- **The audit's remaining passes** — `docs/audit-completion-plan.md` A2–A6 (the reading and instrument passes over the 55 % the audit did not open). A separate work order; this branch only lands what the first pass and its verification earned.

## 6. Exit checklist

- `tox` green on every commit, ruff included from H13 on; the L2 suite green on all six gated schemas.
- Every item's test in the tree; H0's contract harness asserting the *new* behaviour after H4/H5.
- `tests/live/results/S-H*/` holding the before-set, the four numbers before and after, the dump diffs and the S1-P1 identity re-capture; `docs/testing-plan.md` gains an `S-H` section in the catalog's format.
- CLAUDE.md: the A4-1 rule (a single-file movie's `MediaSource.Name` is its file stem, and the Standard Edition seed is what an unversioned film gets) beside the extras constraint; F2 named as the third home of the empty-listing rule beside the boxsets sweep and the prune; the `clean-databases-plan.md` sentence corrected.
- `changelog.txt`: the version-list fix with "run Repair to clear existing entries", the empty-listing floor, the redirect refusal at login, transient retries, the IPC wire change (internal).
- `docs/shell-refactor-phase2-plan.md`'s line references are against `94c9338`; P2.0 re-grounds them against this branch's tip before cutting `refactor/shell-phase2` (player.py, main.py, views.py and settings_apply.py all move here).

## 7. Open questions

1. **H2's rule** — stem-match (recommended: keeps the `test_movie_single_named_primary_version` pin, and it is Jellyfin's own semantics for an unversioned file) versus the audit's "no alternates → 40400", which is simpler and would demote that pin to a label the sync never sees in practice.
2. **H2's existing rows** — convert only through Repair and the walk-end sweep (recommended: no migration code, the remedy is one documented action) versus a startup-time `UPDATE videoversion SET idType = 40400` for every single-version movie, which would also flatten a genuine single-source label.
3. **H3's gate** — refuse only when *none* of the whitelist is listed (recommended: a library really withdrawn is still removed) versus refusing any pass that would remove more than one library at once.
4. **H4's redirects** — refuse 3xx on both transports and name the Location at login (recommended: nobody has a working redirect today, and one behaviour beats two) versus following in the stdlib transport too.
5. **H5's list** — `429, 502, 503, 504` (recommended) versus adding 500.
6. **H12 now or later** — now (recommended: fifteen lines, and phase 2 does not touch `settings_apply.py`) versus after phase 2.
7. **H13's ruleset** — `F, B, PLW0120` now with `RUF012` as a follow-up (recommended) versus the audit's fuller `F,B,RUF012,RUF021,B026,B905,F811` from the first commit, which needs per-line comments on the deliberate class-level caches before the tree is clean.
