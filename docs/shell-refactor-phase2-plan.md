# Shell refactor, phase 2: the structural tier

| Field | Value |
|---|---|
| **Date** | 2026-08-28 |
| **Source** | `docs/shell-refactor-assessment.md` §10 Tier 2, with the findings it stands on (§4 `service/player.py`, §5 `plugin/browse.py`/`play.py`, §2.4 the dispatch tables, §6 the `phase` machine) and the one `service → plugin` import phase 1 left by name (`latesubs → subtitles`, §2.1). Line references were written against `94c9338` (the `refactor/shell-phase1` tip) and re-grounded at P2.0 against `8b27bf8` (the `fix/audit-hardening` tip): `browse.py` and `play.py` are unchanged; `player.py`'s claim block is still `:299–585` and the engine `:1218–1704` (the audit's H11 grew `_claim` at `:1709`); `main.onNotification` runs `:986–1166` (H6 added an arm); `_handle_group_update` is `:598` and the nine phase writes sit at `:76, :811, :1057, :1075, :1146, :1159, :1164, :1223, :1295`; the `settings_apply` table is `:65–78`. |
| **Branch** | `refactor/shell-phase2`, stacked on `fix/audit-hardening` (PR #204, itself on #203), draft PR against it; one commit per item, each revertible on its own. Merge order: #200 → #201 → #202 → #203 → #204 → this. |
| **Scope** | The whole of Tier 2: the `player.py` three-way split, the `browse.py`/`play.py` listing and resolve dedup, the two dispatch tables, the SyncPlay `Phase` enum — plus the `subtitles.fetch_to` carve that empties the service→plugin import list. No writer, node-generator or sync-pipeline change; nothing from the assessment's "not worth doing" list (§5 below). |
| **Rule** | Tier 2 is "structural (a week, safety net first)" — so every split lands **after** its safety net exists, and the phase-1 identity oracles are the regression floor throughout: device-profile JSON, keyed dump, node trees and the S1-P1 listing captures must not move a byte (`tests/live/results/S1-P1.0-before/`, re-confirmed at P2.0). A snapshot that moves is a finding, not a rename. |

## 1. Why these, and in this order

Tier 2 is where the two files the assessment called "three programs in one class" and "the same tail copied six and three times" get their splits — now that phase 1 has typed the seams those splits lean on (`service/ports.py`, `SyncPlayApi`, `Api.for_plugin`) and phase 0 has closed the defects that made the routes dangerous to touch (`router.dispatch`'s `try/finally`). Safety nets come first because these items move *bodies*, not just signatures: the route golden and the phase-transition table are written against the phase-1 tip before a line moves, and every later commit is diffed against them. The order is safety nets, then the two plugin dedups (smallest bodies, strongest existing pins), then the player split (the largest move, behind a 70-test harness that already drives the seam), then the dispatch tables and the enum (mechanical once the tables' contents stopped moving), then the subtitles carve, then one end-to-end pass.

| # | Item | Closes (assessment) | Oracle |
|---|---|---|---|
| P2.0 | Safety nets and baselines | — | the route golden and the phase table exist and pass on the before build |
| P2.1 | `browse.py`: one `listing()` wrapper, one `structural_rows()` | §5, the six openings and six menu bodies | route golden byte-identical; live listings identical |
| P2.2 | `play.py`: one resolve tail, `play_state` reuse, the URL params named | §5, the three tails and the 13 restated keys | `test_play`'s 55 pins; live plays resolve identically |
| P2.3 | `service/player.py` split: claim module + segment engine | §4, 1,875 lines / 68 methods / 34 attributes | `test_segments`' 70 tests change only at the constructor; `test_player`'s 48 unchanged; S3-method live gates |
| P2.4 | The two dispatch tables + the download wire parse moved | §2.4 | every command class still lands; the branch counts collapse |
| P2.5 | The SyncPlay `Phase` enum | §6, nine write sites, ten tuple reads, no lock discipline | the P2.0 phase table byte-identical; the 94 manager tests |
| P2.6 | `subtitles.fetch_to` → `core/` | §2.1, the last `service → plugin` import | the grep gate reads zero; a late-subtitle live attach |
| P2.7 | End-to-end identity, both rigs | — | every baseline unchanged |

## 2. The rigs

Unchanged from phase 1 (`docs/shell-refactor-phase1-plan.md` §2): native Omega 21.3 (`kofin-test` production, `kofin-jf12` jf12 v12) and the Piers 22.0-beta flatpak on 8081/9778, with the three rig rules (deploy → disable→enable → a `--->>> kofin service` line newer than the tree; refused deploys handed over as a `!` one-liner; the unit suite green on the default tmpdir). The phase-1 additions carry forward: `probe_device_profile.py`, `probe_kodi_setting.py`, `probe_regen_nodes.py`, and the S1-P1.0-before baseline set (the dump baseline is `omega-dump.json`, not the stale `omega-p16`). Backgrounded gates run under `set -o pipefail` — the phase-1 lesson, recorded in `docs/testing-plan.md`. The two-client SyncPlay rig (`kofin-spectator`) is still not assumed; anything needing it is `[NOT RUN — two-client rig]` as in phase 1.

## 3. The oracles

Phase 2 changes no writer or node generator, so the phase-1 floor stands: device-profile JSON, keyed dump vs `S1-P1.0-before/omega-dump.json`, and forced-regeneration node trees, all byte-identical. On top of that, two new safety nets are **built at P2.0 on the before build** and pinned for the rest of the phase:

1. **The route golden** (`tests/unit/test_browse_golden.py`, new): every listing route driven over one canned `FakeApi` world through the existing `directory` fixture, capturing `(path, label, isFolder)` per row plus the route's `setContent` and `endOfDirectory` outcome, hashed per route and the hashes embedded in the test — the same shape as P1.8's device-profile golden. P2.1/P2.2 must not move a byte of it. The live twin: `Files.GetDirectory` captures of root, continue-watching, search, nextepisodes, a node menu and a filter menu on `kofin-test`, saved under `S1-P2.0-before/` and re-captured byte-identical at P2.7.
2. **The phase table** (`tests/unit/test_syncplay_phase.py`, new): a `(phase, event) → phase` table driven through the existing manager fixture — join, group start, ready, command, stop, leave, kick — asserting the transitions the nine write sites (`manager.py:76, :803, :1049, :1067, :1138, :1151, :1156, :1215, :1287`) actually make today. P2.5 replaces the spellings, not the table.

For the player split the oracle already exists: `test_segments.py`'s `Engine` harness (`:114`) drives exactly the three entries the engine keeps (`prepare_segment_state`/`segment_tick`/`note_seek`), and `test_player.py`'s 48 tests never reach engine internals. For the dispatch tables the oracle is the command-class live sweep phase 1 established (S1-P1.3's method), plus the unchanged unit contracts in `test_service.py`/`test_syncplay_manager.py`.

## 4. The items

### P2.0 — Safety nets and baselines

**Change.** Write the route golden and the phase table (§3) against the phase-1 tip and commit them green — they are the before. Add nothing else.

**Live.** S1-P2.0: both rigs deployed and bounced on the phase-2 branch point; the phase-1 identity trio re-confirmed (device-profile sha, dump vs `omega-dump.json`, tree regen); the six `Files.GetDirectory` listing captures taken on `kofin-test` → `S1-P2.0-before/`, with a same-build re-capture proving them byte-stable (the method assertion, as every phase has done for its oracle).

### P2.1 — `browse.py`: one listing wrapper, one structural-rows body

**Change.** A `listing(request, build)` wrapper owning the shared opening — `request.handle < 0` guard, `_api()`/logged-out refusal, `except JellyfinError` → `endOfDirectory(succeeded=False)` — for the six routes that spell it today (`root :529`, `next_episodes :618`, `continue_watching :642`, `browse :677`, `search :725`, `extras :877`; phase 0's router `finally` already guarantees the handle closes on *any* exception, so this is now pure dedup, not the P1 fix). One `structural_rows(request, rows)` for the six structural-menu bodies (`_search_menu :794`, `_alpha_menu :900`, `_tag_letters :920`, `_filter_menu :966`, `_node_menu :1021`, the extras entry `_append_extras_entry :1097`) — the ListItem → `structural_art` → `plugin_url` → `addDirectoryItems` shape, about 180 lines out — and the media→(label, type, content) triples folded into the module tables that already exist (`:162`, `:171`, `:187`) so no function carries its own copy.

**Proof.** The route golden byte-identical; `test_browse.py`'s 62 tests unchanged (they assert through the same `directory` fixture the wrapper feeds). `mypy`.

**Live.** S1-P2.1: the six listing captures byte-identical to `S1-P2.0-before/`; one logged-out probe (creds cleared on `kofin-jf12` momentarily, or the existing `_api → None` unit pin cited) confirming the refusal leg still closes the handle.

### P2.2 — `play.py`: one resolve tail, and the URL parameters named once

**Change.** One `_resolve_to(request, listitem, ok)` for the three resolved-item tails (`offline_answer :477/:484`, `resolve_downloaded :576`, `play :775/:782`). `resolve_downloaded` builds its claim through `play_state(...)` (`:286`) instead of restating thirteen of its keys, with the downloaded-play deviations (local path, no transcode, the claim pushed rather than backfilled — the CLAUDE.md rules) passed explicitly. The play-URL parameter names (`transcode`, `bitrate`, `mediasourceid`, `audioindex`, `subtitleindex`, `burnsubs` — 4/4/5/3/3/3 spellings across `play.py`, `context.py`, `streams.py`) become constants in `core/urls.py` beside the builder, imported by `play`, `context`, `streams` and `listitems.play_path`, so the two ends of the URL cannot drift.

**Proof.** `test_play`'s 55 tests pin the pushed play item and the resolved ListItem and pass unchanged; `test_context`/`test_streams` pin the parameter spellings. `mypy`.

**Live.** S1-P2.2 on the phase-1 sweep's method: a direct play, a forced transcode at a chosen bitrate, and a downloaded item played online by id (`kofin-jf12`) — all three resolve exactly as the S1-P1 campaign recorded (stream.mkv static / master.m3u8 with the gated codec list / the local file path), and the claimed play state reaches the service player.

### P2.3 — `service/player.py`: the three-way split

**Change.** Two moves out of the 1,875-line module, both along seams the file already draws:

- The claim/back-fill block — `self`-free module functions `_downloaded_path :299` through `backfill_library_claim :516-585`, plus `musicdb_song_id`/`mapped_jellyfin_id`/`playing_jellyfin_id`/`library_claim`/`_offline_claim`/`_local_item_facts`/`_attach_cached_segments` — moves verbatim to `service/libraryclaim.py`. `player.py` and `service/kodiuserdata.py` import it; the eleven function-local `kofin.downloads`/`kofin.sync` imports go with it.
- The segment engine — `prepare_segment_state :1218` through the engine tail around `:1707`, the 21 engine attributes and `_segment_reset :1290`, plus the pure timing helpers it owns (`crossed_into :142` … `next_episode_label :269`) — becomes a `SegmentEngine` class in `service/segments.py`, beside the `SegmentChecker` that drives it (P1.5 already made that module the checker's home). `Player` constructs and owns the engine; the checker's two hook calls and `note_seek` forward to it. The overlay call keeps its `service/skip` import.

`Player` keeps what is actually a player: the session reporter (`_Reporter :587`, `_Ticker :849`, `onPlayBack*`, `_claim :1709`, `finalize :906`, `stop_threads :960`), lyrics (`_start_lyrics :738`), default tracks, and the delete/remove offers (`:991`, `:1019`). No behaviour change anywhere; the S5 write-race note and the `offer_remove_download` self-IPC stay as they are (ledger, §5).

**Proof.** `test_segments.py`'s 70 tests change only where the `Engine` harness constructs (the seam the assessment measured); `test_player.py`'s 48 and `test_chapter_thumbs`' suite pass unchanged; the phase-1 leaked-thread fixture keeps the run quiet. `mypy` — the split modules come out fully typed (they already are).

**Live.** S1-P2.3, the segment method from the S3 catalog re-run on the split build (`kofin-jf12`, jf12's segment-bearing episodes): S3.1's auto-skip fires at the boundary and S3.3's Play-Next overlay offers at the lead — the engine's whole observable surface — plus a chapter-thumb play (the reporter/finalize half) and a claim from a widget play (`libraryclaim` through `Player.OnPlay`).

### P2.4 — The two dispatch tables

**Change.** `Service.onNotification` (`main.py:986`, ~145 lines, 29 branches) becomes two tables in the `settings_apply.py:59-72` shape: Kodi-bus `method → handler` and IPC `name → handler`, each handler a small method the table points at; the `DOWNLOAD_ADD`/`DOWNLOAD_CANCEL` payload parsing inside it moves to `downloads/wire.py` (new — it is the downloads wire format, and the manager's tests already pin the parsed shape). `SyncPlayManager._handle_group_update` (`manager.py:590`, 71 lines, 10 `gtype` arms) becomes a `gtype → handler` table the dispatcher reads. The guard order (nonce check at the door, the in-group gates) moves into the handlers unchanged.

**Proof.** `test_service.py`'s onNotification contracts (the forged/nonced command tests, the wake/screensaver tests, the download IPC tests) and `test_syncplay_manager.py`'s group-update tests pass unchanged — the tables are reachable through exactly the same entry points. `mypy`.

**Live.** S1-P2.4, one command of each class as S1-P1.3 ran them: a nonced `RepairLibrary`, a `DownloadAdd`/`DownloadRemove` pair on `kofin-jf12`, the screensaver-wake FastSync, and a SyncPlay group update (the single-client create/leave drives `GroupJoined`/`GroupLeft` through the new table).

### P2.5 — The SyncPlay `Phase` enum

**Change.** A `Phase(str, Enum)` — `IDLE/LOADING/WAITING_READY/SYNCED` — in `syncplay/utils.py`, `str`-valued so every log line, comparison and published property keeps its spelling. The nine write sites and the ten tuple reads (`manager.py` ×7, `playback.py` ×3, two orderings of the same pair today) move onto the enum and two named sets (`FOLLOWING = {WAITING_READY, SYNCED}`, `STARTABLE = {IDLE, SYNCED}`), with the transitions named where they happen. No lock is added — the machine's thread story is unchanged and documented; the enum removes the spelling drift, not the race (ledger, as the assessment scoped it).

**Proof.** The P2.0 phase table passes byte-identical; the 94 manager tests and the playback suite unchanged. `mypy` (`check_untyped_defs` has been on since P1.1).

**Live.** S1-P2.5: the single-client SyncPlay create/leave again — the phase walks idle → waiting_ready → synced → idle in the log exactly as the S1-P1 run recorded.

### P2.6 — The last `service → plugin` import

**Change.** The pure half of `plugin/subtitles.py` — the fetch, naming and cache-directory helpers behind `fetch_to :267` — moves to `core/subtitles.py`; `plugin/subtitles.py` keeps the route-facing surface and re-exports what the play path uses, and `service/latesubs.py:47` imports `kofin.core.subtitles`. The naming contract ("a track that arrives late is labelled exactly as it would have been on time") is the invariant: both halves import one implementation, so it cannot fork.

**Proof.** `test_play`'s subtitle tests and the latesubs suite unchanged; the exit grep — `grep -rn 'from kofin.plugin' lib/kofin/service lib/kofin/syncplay` — returns **nothing**. `mypy`.

**Live.** S1-P2.6 (`kofin-jf12`): a play whose subtitle extraction outlasts the resolve — the latesubs chase attaches the track late with the same filename the on-time path would have used (`Player.setSubtitles` appends live, per the ledger).

### P2.7 — End-to-end identity, both rigs

Everything once more on the finished branch: `tests/unit` green (pipefail); both rigs deployed with the bounce rule; the phase-1 identity trio unchanged (device-profile sha, dump vs `omega-dump.json`, forced-regen trees); the six listing captures byte-identical to `S1-P2.0-before/`; the functional sweep — plays at three shapes, segments (S3.1/S3.3), a download add/remove, the picker, SyncPlay create/leave, a command of each class, the late-subtitle attach; `kodi-logtail errors` clean on both. Results → `tests/live/results/S1-P2.*/` and the `S1-P2` section of `docs/testing-plan.md`.

## 5. Not in phase 2

- Everything on the assessment's "not worth doing" list, unchanged: splitting `Service` (its cost is prose), splitting `downloads/manager.py` or giving the store a session object without a profile, rewriting `core/ws.py`, collapsing `playback.py`'s poll loops, restructuring `tempo.py`, a typed/dataclass `Api`, table-driving `node_query`'s 22 arms.
- The `api` field-vocabulary collapse (the five inlined field lists, the free-string `fields` params, the three caller-owned constants) — still the focused pass phase 1 deferred; the P2.2 constants are URL *parameter names*, not fields.
- `offer_remove_download`'s self-IPC round trip (`player.py:1019` region): giving `Player` a service handle is a new seam, not a split; ledger for a later pass.
- The `phase` machine's missing lock and the `_item` write race (S5): documented, unchanged — Tier 2 names the spellings, not the concurrency design.
- The two-client SyncPlay live leg (S1-P1.2b), still owed to a SyncPlay-focused session.

## 6. Exit checklist — all met 2026-08-29 (PR #205)

- [x] `tox` green; the route golden and phase table pass byte-identical to their P2.0 capture.
- [x] `service/player.py` under ~800 lines with the claim module and `SegmentEngine` beside it; `test_segments` changed only at the harness constructor; `test_player` untouched.
- [x] `browse.py`'s six openings and six menu bodies are one wrapper and one body; `play.py` has one resolve tail and no restated `play_state` keys; the URL parameter names have one spelling in `core/urls.py`.
- [x] `Service.onNotification` and `_handle_group_update` are tables; the download wire parse lives in `downloads/wire.py`.
- [x] `grep -rn 'from kofin.plugin' lib/kofin/service lib/kofin/syncplay` returns nothing.
- [x] The phase-1 identity trio and the six listing captures unchanged on both rigs; S1-P2.0–S1-P2.7 recorded under `tests/live/results/` with build shas; `docs/testing-plan.md` gains the S1-P2 section; assessment §10 Tier 2 marked done with the date.

## 7. Open questions

1. **`SegmentEngine`'s home — `service/segments.py` beside the checker, or its own `service/segmentengine.py`?** Recommendation: `service/segments.py`. The checker's docstring already narrates the tick contract, the two classes are one lifecycle, and a ~600-line module of one subsystem beats two files that only ever change together.
2. **The claim module's name — `service/libraryclaim.py` as the assessment sketched, or fold into `service/kodiuserdata.py`, which already imports it back?** Recommendation: `libraryclaim.py`; `kodiuserdata` is the userdata echo policy, and folding a 300-line claim block into it trades one mixed file for another.
3. **`Phase` as `str`-Enum in `syncplay/utils.py`, or a new `syncplay/phase.py`?** Recommendation: `utils.py` — the constants the machine compares against (`FOLLOWING`, `STARTABLE`) sit beside the timing constants the same call sites read, and a nine-line enum does not need a module.
4. **The download wire parse — `downloads/wire.py`, or onto `downloads/store.py`?** Recommendation: `wire.py`. The store is row vocabulary; the wire format is the IPC payload contract, and `test_service`'s download-IPC tests move their import, nothing else.
5. **The route golden's world — one canned `FakeApi` world for every route, or per-route fixtures?** Recommendation: one world (a small library of two movies, a show, an album, a person), because the golden's job is byte-stability across P2.1/P2.2, not coverage — the 62 existing browse tests keep that job.
6. **`core/subtitles.py` now, or leave the last import for the Tier-3 that may never come?** Recommendation: now (P2.6) — it is a ~40-line carve, it makes the exit grep read zero, and the naming contract gains an importable single home.
