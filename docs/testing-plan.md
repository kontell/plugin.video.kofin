# plugin.video.kofin — testing plan

Date: 2026-07-16. Companion to `rewrite-research.md`; scenario IDs map to its build phases (§11). Live testing drives the Kodi instance on this box with the `kodi-drive` tooling (`~/bin/kodi-remote`, `kodi-builtin`, `kodi-shot`, `kodi-diff`, `kodi-logtail`; web server `localhost:8080` `kodi:kodi`, EventServer UDP 9777).

---

## 1. Test environment

* **Kodi**: this box runs Omega (`~/.kodi/userdata/Database/MyVideos131.db` present). Kodi must be running for live tests; the JSON-RPC ping (`Application.GetProperties`) is the readiness check. Piers coverage is schema-level (L2) until a Piers install exists — revisit when the box upgrades.
* **Jellyfin**: a dedicated **test user** and dedicated **test libraries** on the server, never the real ones. Mutating scenarios (delete, rename, watched-flips, retention games) are restricted to test libraries built from expendable media — tiny ffmpeg-generated files (`ffmpeg -f lavfi -i testsrc=duration=90 …`) named to scrape as: ~30 movies (some in a collection, two with `extras/` special features, one multi-version), ~10 shows / ~200 episodes (one show with specials, one with theme/extras), a small music library, a few music videos. Server address is configurable per run (LAN or `jelly.konell.xyz`); one segments-capable show (Intro Skipper-analyzed or chapter-based) is needed for S3.x.
* **Server plugins**: official KodiSyncQueue installed from the catalog (tier-2 scenarios); KofinSyncQueue side-loaded when phase 5 lands (tier-1); both removable for tier-3.
* **Peer addons**: `service.upnext` installed only for S3.3 (not currently on the box); the old `plugin.video.jellyfin` (kontell fork build) installable for the A/B equivalence scenario.
* **Assertion surfaces** (in trust order): sqlite3 on `MyVideos131.db`/`MyMusic83.db`/kofin's `jellyfin.db`; Kodi JSON-RPC; Jellyfin REST (`/Sessions`, `/Items`, `/UserItems/…`); the addon's own debug log via `kodi-logtail`; screenshots last — **pixels prove rendering, never state** (skill rule).

## 2. Test layers

* **L0 — static**: `black --check`, `mypy`, flake; tox config carried from the fork. Every commit.
* **L1 — unit (pytest, no Kodi)**: fork's test suite ports with the sync/syncplay code; Kodi API stubbed the way the fork does it (typings + stub modules). New-code targets: change-feed provider ladder (record parsing for all three tiers, optional-field handling), Etag skip decisions, priority ordering (userdata → removals → added newest-first → metadata → image-only), ISO-8601 parser (7-digit fractions), masking function (token/user-id/device-id never survive), IPC message registry, settings diff engine (whitelist add/remove plans), device-profile builder (codec/HDR/bitrate matrices → expected DirectPlay/CodecProfiles/TranscodingProfiles JSON).
* **L2 — writers against real schemas (pytest + sqlite, no Kodi)**: create pristine `MyVideos131`+`MyMusic83` (Omega) and `MyVideos146`+`MyMusic84` (Piers) databases from Kodi's own DDL (generated once from `ref/kodi-omega-full` / `ref/kodi-piers-full`, checked into `tests/fixtures/`). Run writer fixtures for every media type plus extras assets (asserting `videoversion.itemType` **1 on Omega, 2 on Piers**). Invariants: idempotency (write twice → byte-identical dump), removal leaves zero orphans in any link table, unknown future schema number → writers refuse and surface the "sync paused" state.
* **L3 — live integration**: scenario scripts in `tests/live/` (bash/python wrapping the kodi-drive helpers + curl + sqlite3), each printing PASS/FAIL with evidence paths. Manual only where judgment is needed.
* **L4 — UX review**: `kodi-shot` gates with `kodi-diff` against approved baselines (clock region already masked by the tool).

## 3. Live-test conventions (kodi-drive rules, made binding)

1. Install the dev tree to `~/.kodi/addons/plugin.video.kofin`, then `kodi-builtin 'UpdateLocalAddons()'`; new addons register **disabled** — enable via `Addons.SetAddonEnabled`.
2. Every scenario starts `kodi-logtail mark` and ends `kodi-logtail errors` + `kodi-logtail since` scoped to the action; Kodi debug logging on for request-count assertions (the addon's own debug lines are the grep target).
3. Navigation is blind: screenshot and confirm focus **before** the keypress that matters; `kodi-remote debug` overlay when lost; `ActivateWindow(Home)` for a known start point after reloads.
4. Service bounce = `Addons.SetAddonEnabled` false→true (also exercises our hard-restart path); full Kodi restarts only where unavoidable.
5. Directory/browse tests run headless via `Files.GetDirectory` on `plugin://plugin.video.kofin/...` — no UI navigation needed.
6. **Cleanup is part of the scenario**: each script owns a teardown that removes what it created and asserts the baseline (MyVideos/MyMusic row counts back to pre-test, `addon_data/plugin.video.kofin` removed between suites, any patched files restored). Deletions only ever target test-library media.

## 4. Scenario catalog

### Phase 1 gate — shell (settings, login, browse, playback)

* **S1.1 settings render**: open each tab, screenshot, diff against baseline; every visible setting shows a tooltip; hidden (level-4) settings never render.
* **S1.2 login**: password flow and Quick Connect flow against the test user; wrong-password path surfaces a notification, not a stack trace. Assert `addon_data/…/settings.xml` gained token/userId/deviceId and `isLoggedIn`; assert the raw token string appears **nowhere** in `kodi.log` (masking, with debug logging on).
* **S1.3 account state flips**: after login the Account tab shows server/user read-only + logout/test-connection (screenshot); logout clears hidden creds and flips visibility back.
* **S1.4 browse headless**: `Files.GetDirectory` on the root (views + Add-user + SyncPlay + Settings), a library view, a genres node, a letter node; item properties carry ids/art/resume.
* **S1.5 playback + reporting**: `Player.Open` a movie's plugin URL; Jellyfin `/Sessions` shows the device with `PlayMethod=DirectPlay` and `PositionTicks` advancing on the ~10 s cadence; pause reflects; stop posts final position; resume point visible server-side afterwards.
* **S1.6 device-profile matrix**: files crafted per codec/HDR case: allowed codec → DirectPlay; codec excluded in settings → transcode (assert `/Sessions` `TranscodingInfo` codec + bitrate); HDR type excluded → transcode; force-remux and force-transcode toggles honored; music plays via universal audio with the configured codec/bitrate.
* **S1.7 native resume**: item with server-side resume → dynamic listing exposes it; selecting shows Kodi's native "Resume from hh:mm:ss" (screenshot); both choices land at the right position (`Player.GetProperties` time).
* **S1.8 transcode context popup**: with 3 bitrates enabled the context item lists exactly those 3; choosing one starts a forced transcode at that bitrate (`/Sessions`); with exactly 1 enabled, no dialog (log shows the bypass) straight to transcode.
* **S1.9 soft-restart loop**: trigger the settings restart button 20× scripted; after each: exactly one new service banner in the log, websocket reconnects, server `/Sessions` shows a single device entry, no thread-leak warnings.

### Phase 2 gate — library sync

Run 2026-07-17 on the `kofin-test` profile (separate DBs, Omega 21.3) against `jelly.konell.xyz` (Jellyfin 10.11.11, official KodiSyncQueue active) as the non-admin `kofin-test` user. Libraries synced: Movies (1758), Documentaries (tvshows), Music-Alt (1147 songs), Collections boxsets. Evidence per scenario in `tests/live/results/S2.*.md` (local, gitignored). Three bugs were found and fixed in the run, all committed: `/Library/MediaFolders` 403 for a non-admin user (took the view table down — now best-effort, falls back to the user's own views); the widget-refresh policy (see S2.9); and a startup-transient in the settings diff engine that once *prompted a library removal on a plain Kodi restart* (now gated behind a readiness settle + re-baseline). 7 of 11 gates pass; 4 outstanding.

* **[PASS] S2.1 initial full sync**: per-type counts in Kodi DB equal server counts for the test libraries; scripted field-fidelity diff for one movie and one episode (title, plot, people, streams, art URLs, dates, uniqueids). — 1758 movies / 54 sets / docs / 1147 songs in 1:35; counts match bar two intended diffs (a server-duplicated album merged by MBID; artist rows a superset of top-level MusicArtist items). Movie + episode field fidelity exact.
* **[PASS] S2.2 A/B equivalence (the transplant proof)**: sync the same test libraries with the old fork addon on a scratch Kodi profile, then with kofin; normalized dumps of the video tables (paths normalized for the differing addon ids) must match. Differences must be explainable and intended. — Harness `tests/live/ab_diff.py`; compared kofin's Movies against the master profile's old-addon DB (existing, not a scratch profile). 15 of 36,918 field-comparisons differ (0.041%), all explained: one movie the server replaced with a 4K encode since the master sync (old item 404s), and studio-name casing from Kodi's shared `COLLATE NOCASE` studio table being sync-order dependent (identical fork behavior). TV/boxsets not comparable on this box (master synced all TV libs + only 2 sets).
* **[PASS] S2.3 incremental round trips**: via the Jellyfin API — add an episode file + refresh, edit a title, delete an item, flip watched, set a resume position, favorite — first with Kodi live (websocket path, assert applied within seconds), then repeated with the service bounced (catch-up path). Kodi DB state asserted for each. — watched/resume/favorite/title all applied within seconds over the websocket, and every revert applied on the cold catch-up. Delete covered by S2.8. ("add an episode file" not exercised — needs real media on the server.)
* **[PASS] S2.4 userdata is zero-download**: flip 50 watched states server-side while the service is down; on catch-up, playcounts land with **zero** `Items?Ids=` fetches in the addon log. — 49 flips applied purely from the sync-queue payload; 0 download workers, 0 `Items?Ids=` fetches. Confirmed the `filter` param is an exclude list (kofin sends it correctly) and observed watermark honesty (an empty catch-up that raced the plugin flush did not advance the watermark).
* **[PASS] S2.5 Etag no-op class**: force N re-saves without changes; official-plugin tier: N downloads, zero writes (skip lines); Kofin tier (phase 5): zero downloads too (request-count grep is the assertion). — the Update button re-fetched all synced items (3067 records) in 0:41 with **3067 "Skipping unchanged" and 0 metadata writes**; the movie table md5 was byte-identical before/after. Tier-2 (download-then-skip) proven; tier-1 (skip-before-download) is phase 5.
* **[OUTSTANDING] S2.6 watermark honesty**: make the server unreachable mid-catch-up; watermark must not advance, service degrades with a notification (never a modal) and retries with backoff; on recovery the missed window replays (marker item arrives exactly once). — the core (an incomplete/empty catch-up does not advance the watermark) was observed incidentally in S2.4; the deliberate server-loss + backoff + replay-exactly-once run needs the server made unreachable to the addon (e.g. a temporary hosts/iptables override, or repointing the stored server address at a dead IP). Backoff/degrade paths are L1-covered (`schedule_retry`, `save_last_sync`).
* **[OUTSTANDING] S2.7 interrupted full sync**: bounce the service mid-initial-sync; it resumes unprompted and final counts equal S2.1 (page-hole regression). — needs a fresh full sync to interrupt (a Movies Repair remove+re-add, ~3-4 min, or the smaller Music-Alt re-sync). Resume logic (RestorePoints, `mapping()` resume-without-modal) is ported and L1-adjacent; live interrupt not yet run.
* **[PASS] S2.8 deletion integrity**: delete a whole series server-side; after sync, zero orphan seasons/episodes/files/link rows (sqlite orphan queries). — Conor deleted the Planet Oil series server-side; kofin removed it in realtime over the websocket, leaving **zero orphans across 23 link-table rules** and no residual kofin.db refs. Permanently removes the media from the server (owner's deletion).
* **[PASS] S2.9 widget policy** (*policy revised this run*): after direct writes, `UpdateLibrary(video)` **is** called — it is a no-op scan (writers stamp `noUpdate=1` on every path; `CVideoDatabase::GetPaths` skips those), measured 3–65 ms with zero "Process directory" lines, and it is the only thing that clears Kodi's cached `Library.HasContent`. `UpdateLibrary(music)` is **never** called (its paths lack noUpdate → 21k remote probes, the e4f8dc3f crash). A one-shot `ReloadSkin()` fires on the empty→non-empty transition to rebuild Estuary's `videodb://` home widgets, self-limiting to the first sync. `Container.Refresh` still only when `Window.IsMedia`. Evidence: `tests/live/results/S2.9-widget-policy.md`.
* **[PARTIAL] S2.10 update/repair buttons**: delete a Kodi-side row manually (hole) and add+remove items server-side beyond retention; *Update* heals both directions via the prune pass; *Repair* rebuilds the library and watched state survives (server tombstones). — the **Update** button works (S2.5: full re-fetch + Etag-skip) and the **Repair** picker/dispatch works (driven live earlier); the specific assertions — a manually-injected Kodi-side hole healed by the prune pass, and watched-state surviving a Repair via server tombstones — are not yet formally run.
* **[PARTIAL] S2.11 tier ladder**: with KofinSyncQueue → status line reads tier 1; plugin disabled → tier 2 against official KodiSyncQueue; both absent (phase 6) → tier 3; each transition logged and the Sync tab status line matches (screenshot). — tier-2 confirmed: the status line reads "Official KodiSyncQueue companion" and `server_time()` is the working tier probe. tier-1 needs KofinSyncQueue (phase 5); tier-3 needs the plugin-free fallback (phase 6). The tier-2 → tier-3 transition (disable the official plugin → status line updates) is runnable now and outstanding.

### Phase 3 gate — player features

* **S3.1 segment auto-skip**: seek to shortly before a known intro (`Player.Seek`); position jumps past segment end within the poll interval; notification shown; EOF-clamped segment never seeks past runtime.
* **S3.2 skip button**: button mode shows the overlay at segment start (screenshot), OK skips, ignoring it disappears at segment end; never two overlays at once.
* **S3.3 Up Next coordination**: with service.upnext installed, an episode with a credits segment and a next episode shows **only** the Up Next popup, exactly at credits start (screenshot + upnext log); accepting plays the next episode through kofin; without upnext (or on the season's last episode) our credits skip button appears instead; inside a SyncPlay group, neither.
* **S3.4 movie extras**: after sync, the two-extras movie has `videoversion` rows (itemType per schema) with sensible names; the info dialog shows the Extras button (screenshot); an extra plays and reports.
* **S3.5 TV extras**: the show with specials exposes the Extras browse node; entries play.

### Phase 4 gate — SyncPlay

Reuse `ref/syncplay-conformance` as the scripted second group member: join → scheduled unpause executes within tolerance of `When` (log timestamps vs the harness's), pause/seek propagate both directions, induced drift triggers `SetTempo` correction (observe `Player.GetProperties` speed) and re-converges, harness drop-out surfaces the group-wait toast, sleep/wake leaves and rejoins cleanly. One manual two-device session (this box + another client) as the final sanity pass.

### Phase 5 gate — KofinSyncQueue

Plugin-side unit tests in the plugin repo (record coalescing, Etag population, retention envelope, filter parsing). Then rerun S2.4/S2.5/S2.11 on tier 1 and record the request-count deltas. Retention overrun: retention 1 day + watermark forced 2 days back → targeted repair of affected libraries triggers automatically (log), nothing silently lost. Load check: server library scan of the 10k-item synthetic set — plugin write timings before/after vs official plugin (its own log timestamps).

## 5. Performance baselines (record, don't guess)

Captured by the scenario scripts into `tests/live/results/` per run: initial full-sync wall time for the test set; catch-up latency for 100 mixed pending changes (per tier); addon HTTP request count per scenario (debug-log grep); Kodi-start→library-browsable delta; S2.5 download counts per tier. Regressions between runs of the same scenario on the same data are release blockers.

## 6. Cadence

* Every commit: L0 + L1 (+ L2 when writers changed) via tox.
* Before merge to main: smoke set — S1.4, S1.5, S2.1, S2.3.
* Per release: full catalog for the phases shipped so far.
* On Kodi or Jellyfin version bumps: L2 against the new schema first, then the live smoke set.
