# Shell refactor, phase 1: the cheap, high-yield tier

| Field | Value |
|---|---|
| **Date** | 2026-08-28 |
| **Source** | `docs/shell-refactor-assessment.md` §10, Tier 1 (plus the two ledger defects it names as Tier 1's first: C3 and Y1). Findings and line references live there; this document is the work order. Line references here are against `1685883` (the `fix/shell-phase0` tip). |
| **Branch** | `refactor/shell-phase1`, stacked on `fix/shell-phase0` (PR #201), draft PR against it. One commit per item, each revertible on its own; the PR merges after #201. If the SyncPlay two-client live gate (P1.2's group leg) is deferred, it is the only piece that leaves the phase — every code item lands here. |
| **Scope** | The whole of Tier 1: type the manager/service/client seams behind `Protocol`s, move the three service-only modules out of `plugin/`, and the four `core/` consolidations (`kodirpc`, `api`, `deviceprofile`, `http`/`stdhttp`), plus the small shared helpers and the test hygiene. No module *splits* (that is Tier 2 — `player.py`, `browse.py`/`play.py`, the dispatch tables, the SyncPlay phase enum). No writer, node-generator or sync-pipeline change. |
| **Rule** | Every item is behaviour-preserving, so its proof is *identity*: mypy where the change is typing; and for anything with an observable surface, a before/after snapshot on the two rigs that must be **byte-identical** (a keyed DB dump, the generated node tree, the built device-profile JSON, a request-count, a wire-shape recorder). A refactor whose snapshot moves is a finding, not a rename — it does not land until the move is explained and pinned by a test, or reverted. |

## 1. Why these, and in this order

Tier 1 is where the assessment's cross-cutting findings (§2) get paid down: the untyped manager and client seams that spawned seven `*Library` fakes, fourteen `*Api` fakes and four `FakeDialog`s; the service code wearing the `plugin/` package; the four duplicate-primitive clusters in `core/`. None of it changes what the add-on does — which is the point, and which is why the oracle is identity. The order is by blast radius and by dependency: the pure typing flips first (they touch signatures, not bodies, and green mypy is their whole proof); then the seam Protocols and the client seam, which the moves and consolidations lean on; then the moves; then the four consolidations, each proven by an oracle that already exists; then the small helpers; then the test hygiene; then one end-to-end identity pass on both rigs.

| # | Item | Closes (assessment) | Oracle |
|---|---|---|---|
| P1.0 | Rig prep and the "unchanged" baselines | — | the identity oracle every later gate diffs against |
| P1.1 | `syncplay` typing flip + Y1 `_player_lock` | §6, Y1 | mypy; single-client group smoke |
| P1.2 | The SyncPlay REST-client `Protocol` | §2.2, the 26 string verbs | mypy; group smoke |
| P1.3 | The service manager-port `Protocol`s + `ServiceHooks` + `_forward` | §2.2, the 32 unchecked accesses, 7 `type: ignore` | mypy; bounce + every command class |
| P1.4 | `Api.for_plugin(creds)` + shared `FakeApi`/`FakeDialog` | §2.3, the 10 constructions | mypy; every plugin surface resolves |
| P1.5 | Move the service code out of `plugin/` (`plugin_url`→core, `parse_segments`→core, who's-watching, skip) | §2.1, the 8 cross-layer imports, 4 URL builders | node tree identical; the moved surfaces work |
| P1.6 | `kodirpc` onto one `_call` (incl. C3) | §5, C3, two transports, `except` 12→3 | `test_kodirpc` unchanged; setting-read on both generations |
| P1.7 | `api._get_list()` + `_as_user` everywhere | §2.5, the 11 list-GETs / 14 hand `userId` | wire-shape recorder; listings dump-identical |
| P1.8 | `deviceprofile` one `_envelope` | §2.5, the five duplicated blocks | device-profile JSON byte-identical |
| P1.9 | `http`/`stdhttp` one retry ladder | §2.5, the copied ladder; the missing abort in stdlib | `test_stdhttp` agreement; play + sync + an outage abort |
| P1.10 | Small shared helpers | §2.5 (`VIDEO_MEDIA_TYPES`, `files.absolute_path`, the remove sequence), §3 (`spawn_once`), §6 (`imagecache`), the six Timers | seed/remove/spawn surfaces work; snapshots unchanged |
| P1.11 | Test hygiene | §2.6 (leaked threads, missing tests) | the suite is quiet and complete |
| P1.12 | End-to-end identity, both rigs | — | every baseline unchanged |

## 2. The rigs

Unchanged from phase 0 (`docs/shell-refactor-phase0-plan.md` §2): native Omega 21.3 with `kofin-test` (production 10.11.11) and `kofin-jf12` (throwaway, jf12 v12 on `:8098`), and the Piers 22.0-beta flatpak on JSON-RPC 8081 / EventServer 9778 against production. The same three rig rules apply: every deploy is followed by disable→enable and a `--->>> kofin service` line newer than the tree's ctime; a refused deploy is handed over as a `! tools/dev-install.sh` one-liner; and `tests/unit` runs with `--basetemp` on a real volume only if run before phase 0's D3 fix is in the tree — it is, so the default tmpdir is green. `kofin-jf12` is the profile for the downloads and image-cache smokes; `kofin-test` for the listing, play, device-profile and node-generation identity checks; both for the setting-read (C3) split.

The one addition phase 1 needs is a **second Kodi for the SyncPlay group leg** (P1.2). The `kofin-spectator` git worktree (`/media/minipie/bluecon/dev/kofin-spectator`, on `feat/syncplay-fine-sync-transcode`) is that second client, and the SyncPlay live scenarios in `docs/syncplay-*.md` are the method. If the two-client rig is not stood up this session, P1.2's group play/pause/seek leg is recorded `[NOT RUN — two-client rig]` and its typing is proven by mypy and a single-client group create/leave; the code still lands.

Assertion surfaces, in trust order, are phase 0's: sqlite3 on the profile databases (with the `-wal` copied alongside); `kodi.log` through `kodi-logtail`; Kodi JSON-RPC (every call with a client timeout); Jellyfin REST; screenshots last. The three phase-0 RunScript probes under `tests/live/harness/probe_*.py` are the model for phase 1's new ones.

## 3. The oracles

Phase 1 changes no writer, node generator or sync path, so the phase-0/phase-2 baselines are unchanged by construction and are the regression floor: `tests/live/dump_diff.py` against `omega-p16`/`piers-p16`, and `tests/live/node_snapshot.py` against S-P2.0. On top of those, phase 1 adds three identity oracles for the surfaces it *does* touch, all established on the before build (the `fix/shell-phase0` tip) at P1.0:

1. **The device-profile JSON** (`tests/live/harness/probe_device_profile.py`, new): runs `deviceprofile.build(ProfileConfig.from_settings())` and `build_download(ProfileConfig.for_downloads())` inside Kodi and logs the canonical-JSON of each. P1.8's whole proof is that both are byte-identical after the `_envelope` refactor. Captured per rig (the two boxes have different codec support, so two before-sets).
2. **The wire-shape recorder** for `api`: `test_api.py`'s `RecordingHttp` already asserts the url/params/kwargs of every tested verb; P1.7 adds no live oracle beyond a listing dump-identical to `omega-p16`, because the endpoints it touches are the ones the sync and browse paths already exercise.
3. **The JSON-RPC-call trace** for `kodirpc`: `test_kodirpc.py` drives `xbmc.executeJSONRPC` by string and asserts the request shape; P1.6's live oracle is that a setting read returns the same value on each generation as before (and that the C3 fix makes a *missing* setting and a *failed* call distinguishable — the one place behaviour deliberately changes, §P1.6).

For the typing items (P1.1, P1.2, P1.3, P1.4) the oracle is green mypy plus `tests/unit`, and a live smoke that the wiring still connects — no snapshot, because a `Protocol` annotation emits no bytecode difference.

## 4. The items

### P1.0 — Rig prep and the "unchanged" baselines

**Change.** Add `tests/live/harness/probe_device_profile.py` (§3.1). Confirm the phase-0/phase-2 baselines are present (`tests/live/results/S-P1.0-before/`, `S-P2.0-before/`). Deploy the `fix/shell-phase0` tip to both rigs with the bounce rule.

**Live.** S1-P1.0: the device-profile JSON captured on both rigs → `tests/live/results/S1-P1.0-before/{omega,piers}-deviceprofile.json`; a same-build re-run confirmed identical (the method assertion, as phase 1 of the sync refactor did for dumps). A keyed dump on `kofin-test` and a node snapshot, confirmed still matching `omega-p16`/S-P2.0 on the phase-0 tip, so a later diff cannot be blamed on drift accumulated since those baselines. (Scenario ids in this phase are prefixed `S1-P1.*` to avoid colliding with the sync refactor's `S-P1.*`.)

### P1.1 — `syncplay` typing flip, and the `_player_lock` asymmetry (Y1)

**Change.** Flip `check_untyped_defs = True` in the `[mypy-kofin.syncplay.*]` block (`mypy.ini:40`); `disallow_untyped_defs` stays off — bodies checked, signatures not forced, exactly as `kofin.sync.*`. Fix the 24 real-but-noise errors the assessment counted: `tempo.py` 14 (a `TempoFile` local in six methods that only run armed), `manager.py` 8 (an `assert self.group is not None` idiom behind `in_group()`), `ui.py` 2 (the `Dialog.select` list type). Then Y1: take `_player_lock` in `PlaybackController._do_pause` (`playback.py:476`) and `_do_seek` (`:493`), so an in-flight Pause/Seek on the Timer thread cannot interleave with a `_seek_and_settle` the dispatcher holds the lock for.

**Proof.** `mypy` clean with the flipped block; `tests/unit`. `test_syncplay_playback.py::TestPause` and `::TestSeekAndStop` already drive both methods, so the lock change is assertion-covered; add one test that a Pause scheduled while a pre-align holds the lock waits for it (a recording lock).

**Live.** S1-P1.1, single client on `kofin-jf12`: create a SyncPlay group, join, and leave (`kofin.menu.syncplay` → the group menu), confirming the manager builds, the tempo session arms and the leave tears down clean — the typed bodies run. The Y1 contention only manifests under a real group with contended timers, which is P1.2's two-client leg; noted there.

### P1.2 — The SyncPlay REST-client `Protocol`

**Change.** Define `SyncPlayApi` (a `Protocol` in `syncplay/` or `core/api.py`) naming the 26 verbs `syncplay/manager.py:172,191` reach through `getattr(api_client, name)(*args)` — the SyncPlay and session verbs `core/api.py` exposes with zero static callers. Type the two dispatch sites against it so a renamed verb in `core/api.py` fails mypy instead of surfacing as a logged "SyncPlay x failed". No behaviour change: the dispatch stays `getattr`, only its target is now a checked surface.

**Proof.** `mypy` — the 21 `core/api.py` methods that had no static caller now have one (the Protocol), so a rename is caught. `tests/unit`.

**Live.** S1-P1.2a, single client: as P1.1's group create/leave, plus one command that round-trips a verb (a group pause/unpause issued from the one client) to confirm the dispatch still reaches `core/api.py`. **S1-P1.2b, two clients (`kofin-spectator` as the second):** a group play, a pause, and a seek converge across both — the S4.x method in `docs/syncplay-fine-sync.md`. If the second rig is not stood up this session, S1-P1.2b is `[NOT RUN — two-client rig]`; the verbs are covered by unit (`test_syncplay_manager.py` drives `_post`/`_api` by name) and the single-client leg.

### P1.3 — The service manager-port `Protocol`s, `ServiceHooks`, and `_forward`

**Change.** Three `Protocol`s in a new `service/ports.py`: `LibraryPort` (the 11 names the note above `library.py:892` lists for FullSync's `FakeHost`, plus the service-only members `is_alive`, `enqueue_command`, `startup_done`, `stop_client`, `stop_thread`, `workers_alive`, `userdata` — the service's surface is wider than FullSync's, so `LibraryPort` extends or restates it), `DownloadsPort` (`submit`, `cancel`, `remove`, `remove_all`, `wake`, `stop`), `SyncPlayPort` (`on_notification`, `refresh_tempo_session`, `stop`, plus the string-forwarded `on_wake`/`on_sleep`/`on_kodi_play`). Retype `service/main.py:160-162` from `Optional[Any]` to `Optional[LibraryPort]` etc. Give `SettingsApplier` a `ServiceHooks` protocol in place of `service: object` (`settings_apply.py:60`), replacing the seven `type: ignore[attr-defined]` and four `getattr`. One `_forward(manager, name, *args)` helper for the two identical `getattr(manager, name)(*args)`-with-swallowing-`except` blocks (`main.py:1140-1147` `_syncplay_forward` and `player.py:684-692` `_syncplay_event`).

**Proof.** `mypy` — the 32 previously-unchecked member accesses are now checked. `test_service.py`'s seven hand-rolled `*Library` fakes (`DeadLibrary`, `RunningLibrary`, `RecordingLibrary`, `CatchUpLibrary`, `StuckLibrary`, `FinishedLibrary`, `_FakeLibrary`) collapse onto one shared fake in `tests/unit/fakes.py` that satisfies `LibraryPort` — each test keeps the one or two attributes it varies. `tests/unit` green with the reduced fake set.

**Live.** S1-P1.3, `kofin-test`: a service bounce, then one command of each class to prove every typed port still wires — a `RepairLibrary` (nonced harness → `LibraryPort.enqueue_command`), a settings-driven library add/remove through `onSettingsChanged` (`ServiceHooks` → `_start_library`), a download add/remove on `kofin-jf12` (`DownloadsPort`), and a `System.OnWake` (`SyncPlayPort.on_wake` via `_forward`). No snapshot — the Protocols emit no bytecode; the smoke is that the wiring the annotations describe is the wiring that runs.

### P1.4 — `Api.for_plugin(creds)` and the shared test doubles

**Change.** `Api.for_plugin(creds)` classmethod in `core/api.py` (or a `plugin/api.py` seam) folding the ten spellings of `plugin_transport(settings.get_bool("sslVerify")) + Api.from_credentials(…, creds, interactive=True)` — `browse._api`/`playall._api` (Optional), `context._api`/`actions._api` (non-Optional), and the five inline builds (`adduser`, `play`, `userprefs`, `librarypicker`, `account`). Callers become `Api.for_plugin(creds)` (or a thin `_api()` that returns it). Add a shared `FakeApi` and `FakeDialog` to `tests/unit/fakes.py`, retiring the fourteen local `*Api` classes and the four `FakeDialog`s where they only reproduce the shared shape.

**Proof.** `mypy`; `tests/unit`. `test_browse.py` patches one target (`api.for_plugin`) instead of `browse._api` ×17; the count drops across the plugin suites. Behaviour identical — the constructed `Api` is the same object with the same transport.

**Live.** S1-P1.4, `kofin-test`: a browse listing, a play resolve, and *Test connection* all succeed (each formerly built its own `Api`); on `kofin-jf12` the offline path (`context._offline_menu`) still constructs. A listing dump-identical to `omega-p16` — the `Api` construction change must not alter what a listing fetches.

### P1.5 — Move the service code out of `plugin/`

**Change.** Four moves, each a relocation with imports rewired, no logic change:
- `plugin/listitems.py::plugin_url` → `core/urls.py` (a pure builder). Its 33 call sites across `plugin/` (context, listitems, streams, browse), `service/` (remote, player) and `syncplay/playback.py` import from `core/urls`; `sync/nodes/video.py:271,276` and `core/state.py:100 LYRICS_DIRECTORY`, which hand-build the same `plugin://plugin.video.kofin/?…` string, use it too — four builders become one.
- `plugin/play.py:357`'s `service.segments.parse_segments` dependency reversed: `parse_segments` → `core/segments.py` (a pure parser), imported by both `service/player.py`'s segment engine and `plugin/play.py`. The `plugin.play → service.segments` cycle edge is gone.
- `plugin/adduser.py:146-359` (`detach_all`, `restore_additional_users`, `_publish_from_server`, `show_picker` — the who's-watching service half its own docstring names) → `service/whoswatching.py`. The two routes (`:254-274`, `:362-434`) stay in `plugin/adduser.py` and call into it. `service/main.py:628,854` and `settings_apply.py:212` import the new module, not `plugin`.
- `plugin/skip.py` → `service/skip.py` (its only caller is `service/player.py:1626`).

After this the only `service → plugin` import left is `subtitles.fetch_to` (`latesubs.py:47`), which is noted for a Tier-2 follow and left.

**Proof.** `mypy`; `tests/unit` (the tests for the moved code move with it — `test_adduser.py`'s service-half tests, `test_router` for the remaining routes, `test_library_playback`'s `plugin_url` uses). A grep gate in the exit checklist: `service/` and `syncplay/` import `kofin.plugin` at exactly one site (`latesubs`).

**Live.** S1-P1.5, both surfaces on `kofin-test`: the **who's-watching picker** (`kofin.menu.who` → the service opens it), select a co-watcher, confirm the session attaches; the **skip overlay** — play an item with a segment and confirm the Skip/Play-Next overlay opens (`service/player.py:1626` → `service/skip.py`); a **remote play** with a start position (P0.7's probe, now importing `plugin_url` from `core/urls`) still builds the URL and seeks. And the **node tree**: a forced regeneration on both rigs is **byte-identical to S-P2.0** — the `sync/nodes/video.py` builders now call `core/urls.plugin_url`, and the generated `plugin://` paths must not move a byte.

### P1.6 — `kodirpc` onto one `_call`, and the C3 fix

**Change.** Move the nine hand-rolled `json.loads(executeJSONRPC(json.dumps({...})))` blocks onto `_call` (added `ccfd731`, used by only the four newest functions); add `_active_players()` for the four typed-out `Player.GetActivePlayers` loops; bring the two raw `executeJSONRPC` calls outside the module (`service/remote.py:198`, `service/player.py:1869`) onto it. The 12 `except Exception` drop to the three that guard Kodi's *answer shape*. **C3 (the one deliberate behaviour change):** `_call` today returns `None` for both an RPC error and a legitimately-absent result, so `kodi_setting` reads a transient failure as "this Kodi lacks the setting" — which `syncplay/tempo.py:425-428` branches on for the Kodi version. Split the two: `_call` distinguishes "error/no answer" from "result present" (a sentinel or a `(ok, result)` tuple), and `kodi_setting` returns `None` only for a genuine "setting does not exist" reply, raising or retrying on a transport failure. The tempo queue-depth read then no longer mistakes a blip for Omega.

**Proof.** `test_kodirpc.py`'s 22 tests drive `xbmc.executeJSONRPC` by string and pass unchanged for the mechanical move; add tests for C3 — a `Settings.GetSettingValue` *error* is distinguishable from a *missing* setting, and `kodi_setting` no longer collapses them. `mypy`.

**Live.** S1-P1.6, the setting-read split across both generations (the reason C3 matters): on **Piers** (`videoplayer.queuetimesize` exists) a probe reads it and gets the real tenths; on **Omega** (the setting does not exist) the probe gets `None` = "absent" — and, with the server briefly unreachable during the read, gets an *error* distinguishable from absent (the C3 fix), where before it read absent and slaved tempo to the Omega constant. Plus `stop_player`/`resume_player`, `current_audio`/`current_subtitle` still answer (a play + a subtitle pick).

### P1.7 — `api._get_list()` and `_as_user` everywhere

**Change.** A `_get_list(path, params)` helper returning `response.json() if response.content else []`, replacing the 11 hand-rolled list-GET copies. The 14 hand-typed `{"userId": self.user_id}` dicts (two mis-cased `"UserId"` at `:548,847`) go through `_as_user()` (already 17 uses). Five inlined field lists and the free-string `fields` parameters are left as-is — collapsing the field vocabulary is a larger change and out of Tier 1's mechanical scope.

**Proof.** `test_api.py`'s `RecordingHttp` asserts the url/params/kwargs for every tested verb — the wire shape is pinned, so `_get_list` and `_as_user` are byte-for-byte on the wire. `mypy`; `tests/unit`.

**Live.** S1-P1.7, `kofin-test`: the listing endpoints `_get_list` now serves (next-up, latest, resume, episodes, playlist items) render — browse Continue-watching, Next-up, a show's episodes, a playlist — and a listing is **dump-identical to `omega-p16`**. The `_as_user` unification touches the userId on those same calls; a watched-toggle round-trips (the userdata path).

### P1.8 — `deviceprofile` one `_envelope`

**Change.** One `_envelope(config, max_bitrate, transcoding, direct, burn)` folding the five blocks shared by `build` (`:197`) and `build_download` (`:290`): the profile envelope (`:232-248` ≡ `:323-345`), the music transcoding dict (`:474-481` ≡ `:394-401`), the AudioBitrate condition (`:464-471` ≡ `:386-393`), the video DirectPlayProfile (`:532-542` ≡ `:410-422`), and the token derivation (`build` computes `tokens/h264/h264_10bit/hevc/hevc_rext` at `:225-229`, `build_download` again at `:320,338-342`) — `_codec_profiles` (`:545`) already receives `config`, so it derives its own. The CLAUDE.md transcoding-profile rule is preserved: every codec in `_transcoding_profiles` stays gated on the same list as direct play (`test_deviceprofile.py:173,180,195,205` pin it).

**Proof.** `test_deviceprofile.py`'s 31 tests over every config leg; **snapshot `build()` and `build_download()` JSON before and after** over those configs and assert byte-identical (a unit-level golden, added). `mypy`.

**Live.** S1-P1.8, the device-profile JSON identity (§3.1): on **both rigs** the probe's `build`/`build_download` JSON is **byte-identical to S1-P1.0-before** — the two boxes' different codec support is exactly why both are captured. Then a functional smoke: a direct play, a transcoded play, and a music download each resolve with the expected method (the profile is what the server ranks), on `kofin-jf12` for the download.

### P1.9 — `http`/`stdhttp` one retry ladder

**Change.** Extract the ~40-line retry loop + per-request log line + status taxonomy shared by `http.py:195-257` and `stdhttp.py:191-237` into one `_run_ladder(attempt, retries, abort, url)` in `http.py`, used by both transports. `StdlibHttp` gains the abort check it lacks today (`stdhttp.py:96` inherits `abort=None` and never consults it) — harmless in the plugin process (short-lived, no abort passed) but it makes the two transports honestly identical, which is what `test_stdhttp.py:116,136` assert.

**Proof.** `test_stdhttp.py:116,136` already assert the taxonomy and retry budget agree between the two; they pass unchanged. `test_http.py`'s abort and stream paths stay pinned. `mypy`.

**Live.** S1-P1.9: a plugin play resolve (`StdlibHttp`, the ~1 s-import transport) and a service sync (`Http`, requests) both still complete on `kofin-test`; an outage abort exits promptly — repoint `serverAddress` at a closed port during a service catch-up (P0.4's method) and confirm the ladder gives up within its documented ~150 s ceiling and the service goes offline cleanly, not hung.

### P1.10 — Small shared helpers

**Change.** Each a mechanical dedup with an existing test at the seam:
- `spawn_once(slot, target, name)` in `service/main.py` for the six one-shot `Optional[Thread]` slots (`:602-611,617-625,643-647,664-671,820-824,1116-1124`) plus the two in `player.py` (`:1010-1015,1050-1055`), with the join table derived from the slots (`:1303-1310`).
- `_later(seconds, func, *args)` in `syncplay/` for the six fire-and-forget `threading.Timer` blocks (`manager.py:580-582,883-885,1115-1117,1388-1394,1478-1480`, `playback.py:125-127`).
- `core/imagecache.py`: the JPEG SOF parser + `_SOF_MARKERS` + `THUMBNAILS` (`chapters.py:41,52-54,67-87` ≡ `artcache.py:49,74-76,89-112`) and the fetch→write→collect loop (`chapters._seed:175-207` ≡ `artcache._seed_batch:267-292`). The `with Database("texture")` write **stays in each caller** — it is schema-gated and per-feature; only the header parse and the download loop move.
- `downloads/manager.py::_remove_download(row, mark_watched)` for the two copies of the ordered remove sequence (`:441-455` ≡ `:1093-1101`), pinned by `test_remove_restores_deletes_and_prunes` and `test_a_vanished_file_is_cleaned_up_not_just_restored` (and phase 0's two new refused-restore tests).
- `VIDEO_MEDIA_TYPES` constant for the seven `("movie","episode")` literals (`manager.py:591,1032,1115,1223,1280`, `repoint.REPOINTABLE`, `player.py:434`); `quality.ORIGINAL/TRANSCODE` aliased to `store.QUALITY_*` so the string-equality dependence is named.
- `files.absolute_path(root, rel_path)` / `part_path` for the ten `os.path.join(root, rel_path)` sites (`manager.py:573,661,677,741,986,1058,1184,1332,1536`, `play.py:424`).

**Proof.** `tests/unit` — every helper has a caller-level test already; add a direct test for `imagecache`'s parser (the two callers' size assertions move onto it) and `spawn_once`. `mypy`.

**Live.** S1-P1.10, `kofin-jf12`: chapter thumbnails on a play and the actor-art cache seed both still write the texture cache (`imagecache` parse + the per-caller DB write); a download remove and a vanished-file cleanup (the `_remove_download` fold) — the latter re-using P0.9's setup; the one-shot threads (`backdrop`, `precache_art`, `chapter_sweep`) all fire on a bounce (`spawn_once`). No snapshot beyond the texture-cache rows being written as before.

### P1.11 — Test hygiene

**Change.** A `Player` fixture in `conftest.py`/`test_player.py` that calls `stop_threads()` so the ~130 leaked `_Reporter` daemon threads per run are joined; `stop()` in the `test_syncplay_manager.py` fixture so its 94 dispatcher threads do not leak. Add the missing tests the census found uncalled: `plugin.browse.next_episodes`, `plugin.actions.update_libraries`, `plugin.account.test_connection` (phase 0 added one path; complete it), `service.remote._seek_when_playing` (phase 0's P0.7 added the thread; test the join), and drive `service.main.run`/`_join_workers` at least once.

**Proof.** `tests/unit` green with no leaked-thread warning under `-W error::pytest.PytestUnraisableExceptionWarning` (or a thread-count assertion in the fixture). Coverage of the named defs.

**Live.** None — test-suite only.

### P1.12 — End-to-end identity, both rigs

Everything once more on the finished branch, in one sitting: `tests/unit` green on this host's default tmpdir; both rigs deployed with the bounce rule; **the four identity oracles unchanged** — keyed dump `kofin-test` vs `omega-p16`, node tree + props vs S-P2.0 on both, device-profile JSON vs S1-P1.0-before on both, and a listing's request-count vs the phase-1 baseline; plus a functional sweep of the moved surfaces (who's-watching, skip overlay, remote seek, a play at three codecs, a music download, a setting read on each generation, a SyncPlay group create/leave). `kodi-logtail errors` clean on both. Results → `tests/live/results/S1-P1.12-{omega,piers}.md`, and the `S1-P1` section of `docs/testing-plan.md` marked PASS/PARTIAL per scenario.

## 5. Not in phase 1

- Every Tier 2 item: the `player.py` three-way split, the `browse.py`/`play.py` listing-wrapper and resolved-item-tail dedup, the two dispatch tables (`onNotification`, `_handle_group_update`), and the SyncPlay `Phase` enum. The seams phase 1 types are what make those cheaper; phase 1 does not take them.
- The `api` field-vocabulary collapse (the five inlined field lists and the free-string `fields` params) — larger than a mechanical dedup, left for a focused pass.
- The `subtitles.fetch_to` last `service → plugin` import — moved only when its Tier-2 owner (`latesubs`/`player` subtitle wiring) is touched.
- Any change to what the writers, node generators or sync paths produce. Phase 1's floor is that S-P2.0 and `omega-p16` still match.

## 6. Exit checklist

- [ ] `tox` green; `mypy` clean with `check_untyped_defs` now on for `kofin.syncplay.*`.
- [ ] `service/main.py`'s three manager slots and `SettingsApplier` are typed against `Protocol`s; no `Optional[Any]` or `service: object` remains on those seams; the seven `type: ignore[attr-defined]` are gone.
- [ ] `grep -rn 'from kofin.plugin' lib/kofin/service lib/kofin/syncplay` returns exactly one hit (`latesubs → subtitles`); `plugin_url` and `parse_segments` live in `core/`.
- [ ] `test_service.py`'s `*Library` fakes are one shared fake; the plugin suites share `FakeApi`/`FakeDialog`; the leaked-thread fixtures are in place.
- [ ] The four identity oracles recorded unchanged on both rigs (dump, node tree, device-profile JSON, request-count); C3's one deliberate behaviour change is the only non-identity, and it is pinned by a test and a live setting-read on both generations.
- [ ] S1-P1.0 through S1-P1.12 under `tests/live/results/`, each with build sha, rig, server, and the identity result; PARTIAL entries (the two-client SyncPlay leg, if deferred) say what was not run and why.
- [ ] `docs/testing-plan.md` has the `S1-P1` section; `docs/shell-refactor-assessment.md` §10 Tier 1 marked done with the date.

## 7. Open questions

1. **One stacked branch or a small stack of PRs?** The assessment said "one PR each" for the top Tier-1 bullets. Recommendation: one `refactor/shell-phase1` branch, one commit per item (as phase 0), reviewed as a unit — the items are individually small and share the same identity oracle, and a stack multiplies the rebase cost against `fix/shell-phase0`. Split only P1.2's two-client SyncPlay leg out, since it needs a rig the rest does not.
2. **The SyncPlay two-client group leg (S1-P1.2b) this session or a syncplay-focused one?** It needs `kofin-spectator` stood up as a second client on shared or separate ports. Recommendation: land P1.2's code (typing only) here with the single-client smoke, and run the two-client convergence in the next SyncPlay session against the existing `docs/syncplay-fine-sync.md` scenarios — the typing cannot regress runtime behaviour, so the risk of deferring the group leg is nil.
3. **`Api.for_plugin` on `core/api.py` or a `plugin/api.py` seam?** `for_plugin` reads `settings.get_bool("sslVerify")` and builds the plugin transport, which is plugin-process concern; but `Api` lives in `core/`. Recommendation: the classmethod on `Api` (it is a constructor), taking the verify flag it needs — `core/` already reads settings in `ProfileConfig.from_settings`.
4. **`imagecache` a new `core/` module or a fold into `sync/kodidb/texture.py`?** The texture-cache *write* is schema-gated and belongs beside the schema (it stays in the callers either way); the header-parse and fetch loop are not. Recommendation: a new `core/imagecache.py` for the parse + fetch, leaving the gated `Database("texture")` write in `service/chapters.py` and `service/artcache.py` — the shared code is then free of the schema gate, which is the honest boundary.
5. **C3's shape — sentinel or `(ok, result)`?** `_call` must let a caller tell "no answer / error" from "result is `None`/absent". Recommendation: a module-private sentinel returned on transport failure, with `kodi_setting` mapping only the genuine "setting absent" reply to `None`; a tuple churns every one of `_call`'s call sites for no readability gain.
