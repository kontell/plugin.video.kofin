# Shell refactor, phase 0: defects and truth

| Field | Value |
|---|---|
| **Date** | 2026-08-28 |
| **Source** | `docs/shell-refactor-assessment.md` §10, Tier 0. Findings, line references and the defect ids (P1, D1, …) live there; this document is the work order. Line references here are against `c07d9fd` (main after #199). |
| **Branch** | `docs/shell-refactor-assessment` carries the assessment and this plan (the #190 pattern); `fix/shell-phase0` is stacked on it, one commit per item, each revertible on its own. Live results go under `tests/live/results/S-P0.*/` and a `## S-P0` section in `docs/testing-plan.md`. |
| **Scope** | The Tier 0 list: eight behaviour fixes (P1, P2, P3, S1, C2, D1, D5 and the D3/D2 test corrections), the dead code in §9, and the four documents that describe code that is gone (§2.7). No restructuring, no new seams, no `Protocol`s — that is Tier 1. |
| **Rule** | Every item lands with its unit proof, and with its live gate on the rig that can stage it. A gate that cannot be staged is recorded as L1-only in the results, never skipped silently. Phase 1's media rule stands: nothing is deleted through jf12 and no file is written under any jf12 library path. |

## 1. Why these, and in this order

Each item is one diff a reviewer can hold in their head, closes a defect the assessment verified (by probe or by reading), and touches nothing Tier 1 or 2 will restructure — so none of this work is thrown away later. The order is by blast radius: text and deletions first, then the test corrections that make the suite trustworthy on this host, then the behaviour fixes from least to most entangled, each gated live before the next lands.

| # | Item | Closes | Proof |
|---|---|---|---|
| P0.0 | Rig check and the "before" evidence | — | the negative controls every later gate is measured against |
| P0.1 | Truth: dead code, stale comments, stale docs | C1, §2.7, §9 | grep zero-caller lists; `tests/unit`; mypy |
| P0.2 | Test truth: `free_space_ok` stubbed, the 148 repoint leg | D3, D2 | the three tests pass on a small tmpfs; the repoint L2 runs four legs |
| P0.3 | `SYNC_LIBRARY` out of the IPC registry; `UPDATE_LIBRARY` and `REFRESH_BOXSETS` guarded | C2 | L1; a forged `NotifyAll` dropped live |
| P0.4 | `test_connection` answers a 5xx | P2 | L1; a stub server answering 500 |
| P0.5 | `_show_names` reads the title over JSON-RPC, not MyVideos SQL | P3 | L1; the subscribed-shows picker |
| P0.6 | `router.dispatch` closes the handle on any exception | P1 | L1; an unopenable `kofin.db` under a play resolve |
| P0.7 | The remote seek leaves the websocket thread | S1 | L1; a `PlayNow` with a start position followed by a `Pause` |
| P0.8 | A late cancel neither leaks nor poisons | D1 | L1 (the probe); a cancel fired at the last byte, then a re-download |
| P0.9 | A refused restore keeps the download | D5 | L1; a blanked `restore_filename`, then Remove |
| P0.10 | Lyrics on the current contract | C1 (behaviour half) | S3.7 re-run as it exists today |
| P0.11 | End-to-end regression, both rigs | — | keyed dumps and node trees against the phase-2 baselines |

## 2. The rigs

Unchanged from phase 2 (`docs/sync-refactor-phase2-plan.md` §2): native Omega 21.3 with the `kofin-test` (production) and `kofin-jf12` (throwaway, on jf12) profiles, and the Piers 22.0-beta flatpak on JSON-RPC 8081 / EventServer 9778 against production. jf12 (`dev/test-server`, v12.0, `:8098`) is where anything that mutates state happens: downloads, the blanked column, the forged messages, the unopenable database. `kofin-jf12` is the profile for every mutating scenario; `kofin-test` and Piers run only the read-only ones (P0.4's stub server is the exception — it repoints `serverAddress` for a minute and puts it back).

Three rig rules learned in phases 1 and 2 apply to every deploy here: `tools/dev-install.sh` (`--flatpak` for Piers) does not bounce an already-enabled add-on, so every deploy is followed by disable → enable and a `--->>> kofin service` line newer than the tree's ctime before anything is measured; a refused deploy is handed over as a one-liner and the driving continues from here; and on this host `tests/unit` is run with `--basetemp` on a real volume until P0.2 lands, because `/tmp` is a 3 GB tmpfs and D3 fails three tests on it.

Assertion surfaces, in trust order: sqlite3 on the profile databases (with the `-wal` copied alongside); `kodi.log` through `kodi-logtail mark`/`since`/`errors`; Kodi JSON-RPC (every call with a client timeout — a wedged invoker is one of the things being measured); Jellyfin REST; screenshots last.

## 3. The oracles

Phase 0 changes no writer, no node generator and no sync path, so its regression oracle is the cheapest one there is: the keyed dumps from phases 1 and 2 (`tests/live/dump_diff.py` against `omega-p16` and `piers-p16`) and the S-P2.0 node trees and props (`tests/live/node_snapshot.py`). P0.11 runs both and expects identical.

For the behaviour fixes the oracle is a negative control: every live gate is run first on the build *before* the item (the previous commit on the branch, or main for the first) to reproduce the defect, then on the build with it. A gate whose negative control does not reproduce is a finding about the assessment, and is written down as such before the fix lands.

## 4. The items

### P0.0 — Rig check and the "before" evidence

**Change.** None to the add-on. Confirm both rigs answer, both profiles are logged in, and the phase-2 baselines exist (`tests/live/results/S-P1.0-before/`, `S-P2.0-before/`). Confirm `kofin-jf12` holds one completed download of a smoke-tier episode (P0.8 and P0.9 need one; download it now if not, it is a read of jf12). Run `tests/unit` on main twice on this host: once with the default tmpdir (records the three D3 failures as the "before") and once with `--basetemp` on a real volume (green). Deploy main to both rigs with the bounce rule.

**Live.** S-P0.0: the two unit runs, the `--->>> kofin service` lines on both rigs, and one `Files.GetDirectory` on `plugin://plugin.video.kofin/` per rig answering in under a second — the plugin-handle control P0.6 will be compared against.

### P0.1 — Truth: dead code, stale comments, stale docs

**Change.** Delete, each with its zero-caller grep in the commit message: `core/state.py` `PROP_LYRIC_CONTROL`, `lyric_control_id`, `has_lyrics`, `get_context_bitrates`; `core/lyrics.active_index` and its five tests (`test_lyrics.py:254-268`); `core/ipc.encode_hex` and its test (`test_ipc.py:29`); `core/kodirpc.addon_enabled` and its three tests (`test_syncplay_tempo_wiring.py:83-95` — `tempo.py` uses `addon_details`); `service/player.py`'s duplicate `import json` (`:20`); `syncplay/tempo._queue_secs_in_force` (`:781-791`); `syncplay/timesync.TimeSync.server_now_ms`/`server_now_iso` (`:90-93`; the manager keeps its own at `manager.py:206-210` — `rtt_ms` stays, it is an attribute the tests read); `downloads/store.done_ids` (`:450-456`) and `series_has_done` (`:459-469`, its test moves onto `series_done_on`); `downloads/store.set_restore_filename` (`:381-383`, its two callers `test_downloads_manager.py:445, :1529` move onto `set_restore_filename_on`). `player.py:348 LYRICS_ADDON` is not deleted but used: `_start_lyrics :741-744` gains `elif mode == LYRICS_ADDON` in place of the bare `else`, so the constant documents the branch it names.

Rewrite what describes the deleted driver. `core/state.py:79-92` says the service drives the highlighted line with `Control.SetFocus`; the truth since `a350452` (2026-07-28) is that kofin publishes `PROP_LYRIC_HAS`/`PROP_LYRIC_JSON`/`PROP_LYRIC_PATH` and stops, and rendering and clock-following belong to `script.kofin.lyrics` (`player.py:746-749` already says so). `plugin/lyrics.py:9-10` says the same wrong thing and gets the same correction. `docs/testing-plan.md:83` (S3.7) describes the slot-ladder design `56d3385` replaced the same day: it gets a one-line `[SUPERSEDED — see S-P0.10]` prefix, not a rewrite of history. `docs/offline-downloads-plan.md:33` (W1.7) says restore re-runs the writer's path stamping; the code stores `restore_filename` verbatim and refuses without it, for the reasons `downloads/repoint.py:15-24` gives — the sentence is replaced by those reasons. `service/player.py:1-16` gains the four programs the file holds beyond the reporter and the segment engine (lyrics, the library claim and back-fill, default-track selection, the delete and remove offers); `service/settings_apply.py:1-9` stops narrating phases 1 and 2 and describes the 12-entry table.

**Proof.** `tests/unit` green; `mypy` clean; `grep -rn` for each deleted name returns only the commit that removed it.

**Live.** None of its own — P0.10 is the lyrics half, P0.11 the rest.

### P0.2 — Test truth: `free_space_ok` stubbed, the 148 repoint leg

**Change.** `test_downloads_manager.py`'s autouse `env` fixture (`:21-43`) gains `monkeypatch.setattr(manager_module.files, "free_space_ok", lambda root, size: True)`; the one test that wants the refusal (`:400-410`) already overrides it per test and keeps working. `test_downloads_repoint.py:31-37` gains `kodifixtures.PIERS_VIDEO_VERSION_148` with id `piers148`, the leg `test_sync_writers.py:120-124` already runs.

**Proof.** The three D3 tests pass with the default tmpdir on this host (the failing condition), and the whole file passes alone and under `-x`. The repoint suite runs four legs; a dump that differs on 148 is a finding, not a fixture problem — `docs/myvideos148-gate.md` explains what 148 moved (`streamdetails.iSource`/`iVersion`) and why a NULL there is not a regression.

**Live.** None; these are test corrections.

### P0.3 — `SYNC_LIBRARY` out of the registry; `UPDATE_LIBRARY` and `REFRESH_BOXSETS` guarded

**Change.** `core/ipc.py`: delete `SYNC_LIBRARY` (`:28`) and its `_REGISTRY` entry (`:68`); `service/main.py:31` drops it from `LIBRARY_COMMANDS`. The library's own command table keeps the internal `"SyncLibrary"` name (`sync/library.py:794`) — its two enqueuers (`settings_apply.py:393`, `library.py:1802`) call `enqueue_command` directly and never went over NotifyAll. `UPDATE_LIBRARY` and `REFRESH_BOXSETS` join `GUARDED` (`:84-100`): the first plans a prune that deletes rows, the second re-walks every collection — both things the guard's own rationale names — and both senders are plugin buttons that already reach `ipc.notify` (`plugin/actions.py:145, :148, :152`), so the nonce rides for free. `onNotification`'s `if/elif` (`main.py:1040-1107`) has no `else`: a name that is registered but matched by no arm falls through silently, and so does an unregistered one. Add `else: LOG.debug("unhandled IPC %s", name)`.

**Proof.** `test_ipc.py`: `notify("SyncLibrary")` raises `ValueError` (unregistered); `verify` rejects `UpdateLibrary` and `RefreshBoxsets` without the nonce and accepts them with it. `test_service.py`: a `NotifyAll` from sender `plugin.video.kofin` naming `SyncLibrary` reaches no `enqueue_command`; `UpdateLibrary` without a nonce logs `dropped unauthenticated`; the real button path (`ipc.notify` → `onNotification`) still enqueues. `tests/live/harness/kofin_ipc.py` needs no change — it calls `ipc.notify`, which adds the nonce for whatever `GUARDED` holds.

**Live.** S-P0.3, Omega `kofin-jf12`, negative control first on main: `JSONRPC.NotifyAll` from the shell with `sender: "plugin.video.kofin"`, `message: "SyncLibrary"`, `data: ["{\"Id\": \"<Shows library id>\"}"]` → the log shows `command/SyncLibrary` and a full sync of Shows starts (the forgery works); the same with `UpdateLibrary` and `{}` → `Update pass planned`. On the new build: neither is enqueued; the `SyncLibrary` line is the new debug fall-through, the `UpdateLibrary` line is `dropped unauthenticated UpdateLibrary`. Then the settings button *Update libraries* → All on the new build → `Update pass planned` (the legitimate path carries the nonce), and *Refresh collections* → the boxsets walk runs.

### P0.4 — `test_connection` answers a 5xx

**Change.** `plugin/account.py:186-196` gains `except JellyfinError as error:` after the two specific arms, answering `_notification(_text(30821) % error, toast.ERROR)`. New string `#30821 "Server error — %s"` through the i18n toolchain (`tools/i18n/README.md`: `en_gb`, the 26 JSONs, `gen.py --snapshot`, `gen.py && validate.py && pocheck.py`, all committed together; tagged pending native review like every other string).

**Proof.** `test_account.py`: `public_info` raising `HttpError` → the 30821 toast, no exception out of the route; `Unauthorized` and `ServerUnreachable` unchanged. `test_translations.py` green (the validators are what catch a locale left out).

**Live.** S-P0.4, Omega `kofin-jf12`: a stub HTTP server on `127.0.0.1:8099` answering 500 to everything (ten lines of `http.server`); `kofin_login.py set serverAddress=http://127.0.0.1:8099` (the service restarts and goes offline, which is fine — the button reads the credentials directly), then *Test connection* from settings. Negative control on main: `HttpError` traceback in the log, no toast. New build: the toast with the status text, no traceback. `serverAddress` back to jf12, service back online.

### P0.5 — `_show_names` reads the title over JSON-RPC

**Change.** `plugin/actions.py:459-482` keeps the `kofin.db` id lookup and replaces the MyVideos `SELECT c00 FROM tvshow` with `kodirpc.tvshow_title(kodi_id)` — a new four-line reader over `VideoLibrary.GetTVShowDetails` with `properties: ["title"]`, the sanctioned way to read Kodi's library from anywhere (`kodi-database-writing` bans writing through JSON-RPC, not reading). The plugin process then opens no Kodi database at all.

**Proof.** `test_plugin_actions.py` patches `_show_names` twice today; a direct test drives it with `xbmc.executeJSONRPC` faked the way `test_kodirpc.py` does and asserts the title, the id fallback when the show is not mapped, and the id fallback when the RPC answers nothing.

**Live.** S-P0.5, Omega `kofin-test` on production: *Automatic downloads → choose shows* lists titles, not ids, and picks land in the setting as before.

### P0.6 — `router.dispatch` closes the handle on any exception

**Change.** `plugin/router.py:160-168`: `handler(request)` runs inside `try`/`except BaseException` that sets a flag and re-raises, and a `finally` that calls `endOfDirectory(handle, succeeded=False)` when `handle >= 0` and either the route builds nothing (today's rule) or the handler raised. A listing route that returned normally has already closed its own handle and is not touched. The exception still propagates, so Kodi logs the traceback exactly as now; the difference is that the caller waiting in `WaitOnScriptResult` is released. For the `play` route the same call stands in for the `setResolvedUrl(False)` it never reached — both paths signal the same fetch-complete event in `CPluginDirectory`, which the live gate confirms on both generations.

**Proof.** `test_router.py`: a listing handler raising `RuntimeError` → `endOfDirectory` called once with `succeeded=False` and the exception propagates; a listing handler that returns → `endOfDirectory` not called by `dispatch`; an action route → unchanged (closed once); a handler raising on a negative handle → nothing closed, exception propagates. The first of these is the test the assessment noted nobody had written: no test anywhere raised a non-`JellyfinError` inside a handler.

**Live.** S-P0.6, Omega `kofin-jf12` with downloads enabled (so the play resolve calls `downloaded_file` → `store.get`, `play.py:420`). The trigger is an unopenable `kofin.db`: `chmod 000` on it for the length of the probe (instant `sqlite3.OperationalError` at connect; the service's own per-call opens fail and log for that minute, which is acceptable on the throwaway profile — the alternative, an exclusive lock held from a shell, is cleaner for the service but waits out `Database`'s 120 s busy timeout, `sync/db.py:85`, per probe). Negative control on main: `Files.GetDirectory` on `plugin://plugin.video.kofin/?mode=play&id=<movie>` with a 30 s client timeout → the traceback lands in the log and the call never returns; a second `Files.GetDirectory` on the root listing, after `chmod` back, also does not return — the parked invoker the router comment describes. Kodi is restarted to recover. New build: the same call answers an error within a second, the log shows the traceback followed by the handle close, and the root listing afterwards answers normally without a restart. Once on Piers too — the invoker behaviour is Kodi's, and both generations honour `reuselanguageinvoker`.

### P0.7 — The remote seek leaves the websocket thread

**Change.** `service/remote.py:106-127`: `_play` hands the seek to `_start_seek(seconds)`, which spawns a daemon thread (`kofin-remote-seek`) running the existing loop and keeps it in `self._seek_thread` — a new `PlayNow` replaces the reference, the old thread exits on its own 10 s bound. The loop itself is unchanged but for a debug line when it gives up; the module docstring (`:4-6`, "this thread never blocks") becomes true again.

**Proof.** `test_remote.py`: a `PlayNow` with `StartPositionTicks` returns from `handle` before any seek (a `FakePlayer.isPlaying` that answers False twice, then True; `waitForAbort` faked to return False without sleeping), and after `handler._seek_thread.join(1)` the seek is recorded once with the right seconds; no `StartPositionTicks` → no thread.

**Live.** S-P0.7, Omega `kofin-jf12` on jf12 with a transcoded start (so the player takes several seconds to start, which is the case that used to lose the seek): from the shell, `POST /Sessions/{kodiSession}/Playing?playCommand=PlayNow&itemIds=<episode>&startPositionTicks=3000000000` with the test token, followed within 200 ms by `POST /Sessions/{kodiSession}/Playing/Pause`. Negative control on main: the log shows `remote PlayNow` and the pause handled only after the seek loop returns (the gap is the time to first frame, or the full 10 s when it is exceeded), and a slow start loses the seek. New build: the pause is handled within the socket's normal latency, and `Player.GetProperties time` reads 5:00 ± 2 s once playing. Once on Piers.

### P0.8 — A late cancel neither leaks nor poisons

**Change.** `downloads/manager.py:_transfer`: after `_pull_original`/`_pull_transcode` returns (`:566-573`) and before the sidecars, `if self._cancelled(item_id): raise _Cancelled()` — the same exception the chunk loop raises, so `_process`'s existing arm (`:499-503`) deletes the `.part`, removes the row and clears the flag. Immediately after `store.finish` (`:585`), `self._clear_cancel(item_id)`: from that line on the item is done, and a cancel that arrives during the last second (sidecars, the rename) is superseded by completion — the row is DONE and the user removes it instead. Said in a comment at the site, with the window named.

**Proof.** `test_downloads_manager.py`, the assessment's probe as tests: a cancel drained while the sidecar fetch runs → the row is gone, the `.part` is gone, `_cancels` is empty; a cancel drained after `finish` → the row is DONE and `_cancels` is empty; then remove, re-queue and `_process` the same id → DONE (today's build cancels it at the first chunk — the test fails on main, which is the point).

**Live.** S-P0.8, Omega `kofin-jf12` on jf12 (downloads read jf12; the media rule is not touched): queue a smoke-tier episode; a shell watcher polls the `.part` size every 50 ms against `download.size_expected` in `kofin.db` and fires `RunScript(kofin_ipc.py,DownloadCancel,Id=<id>)` the instant they match. Negative control on main: the row goes DONE and the next *Remove* + *Download* of the same id logs `download cancelled mid-transfer` at its first chunk and the row disappears — the poison. New build: whichever of the two correct outcomes the timing produced (cancelled before `finish`, `.part` deleted and row gone; or DONE with the flag cleared), the subsequent re-download completes. The results record which outcome occurred.

### P0.9 — A refused restore keeps the download

**Change.** `_apply_remove` (`manager.py:441-455`): when `repoint.restore` answers False, log at ERROR, toast `#30822 "Kept %s — its library entry could not be restored"` and return with the row, the file, the tag and the badge untouched. `_handle_vanished` (`:1093-1101`): the file is already gone, so a refused restore is logged at ERROR and the cleanup proceeds — the row must go, or the 300 s sweep finds the same missing file every pass; the MyVideos row heals on the item's next writer pass, which is the same recovery the assessment described. The second string goes through the toolchain with P0.4's.

**Proof.** `test_downloads_manager.py` with the `repoints` fixture's `restore` answering False: remove → media present, row DONE, `unstamp_tag`/`clear_badge` not called, the toast; vanished → cleanup as today plus the error line.

**Live.** S-P0.9, Omega `kofin-jf12`, the downloaded episode from P0.0: service stopped, `UPDATE download SET restore_filename = '' WHERE jellyfin_id = '<id>'` in `kofin.db` (kofin's own database on the throwaway profile — inside the media rule), service started. Negative control on main: *Remove download* deletes the file and leaves `files.strFilename` in MyVideos as the local basename — a row pointing at a file that no longer exists (read with the `-wal` alongside). New build: the toast, the file present, the row DONE, `strFilename` unchanged. Then a Repair of the library re-captures the column (the writer pass is what captures it), and *Remove download* succeeds and restores the plugin URL.

### P0.10 — Lyrics on the current contract

**Change.** None beyond P0.1. This is S3.7 re-run against the design that exists, so `docs/testing-plan.md` stops carrying a PASS for one that does not.

**Live.** S-P0.10, Omega `kofin-test` on production, `musicLyricsMode=1`: play a song with timed lyrics → `kofin.lyric.has` is `true`, `kofin.lyric.json` holds the lines with their starts, `kofin.lyric.path` carries the song id and changes with the next song; with `script.kofin.lyrics` installed the overlay renders and follows the clock, without it nothing else in kofin moves; stop → all three cleared. `musicLyricsMode=2` → `kofin.lyric.has` unset and the lyrics on the playing item's tag. Recorded as the replacement for S3.7.

### P0.11 — End-to-end regression, both rigs

Everything above on the finished branch, in one sitting, plus the oracle: `tests/unit` green on the default tmpdir of this host; both rigs deployed with the bounce rule; Piers: a Music Repair keyed-identical to `piers-p16`, node tree and props identical to S-P2.0; Omega `kofin-test`: a FastSync with one userdata flip and a Repair of the smallest whitelisted library keyed-identical to `omega-p16`, tree identical to S-P2.0; one playback per rig from a library row with the 10 s report visible; *Update libraries* and *Refresh collections* from settings (the two newly guarded messages) on both; a download add and remove on `kofin-jf12`; `kodi-logtail errors` clean on both apart from the traceback P0.6 stages on purpose. Results → `tests/live/results/S-P0.11-{omega,piers}.md`; the `S-P0` section of `docs/testing-plan.md` marked per scenario with the evidence paths.

## 5. Not in phase 0

- The other defects the assessment verified but did not tier: S2 (`_start_library` on the notification thread), S3 (deliberate), S4 (the 20 s dialog join), S5, C3 (`_call`'s `None` for both error and no-result), C4 (`Api.download`'s ignored timeout), C5 (the nonce-less send), D4 (the refresh on every start), Y1 (the `_player_lock` asymmetry — Tier 1's cheap list) and Y2. They stay on the ledger in the assessment's §8; C3 and Y1 are the first two of Tier 1.
- Every Tier 1 and Tier 2 item: the `Protocol`s, `Api.for_plugin`, the moves out of `plugin/`, the `kodirpc`/`api`/`deviceprofile`/`http` consolidations, the `syncplay` typing flip, the `player.py` split.
- Any change to what the writers, the node generators or the sync paths write. Phase 0's oracle is that the phase-2 baselines still match.

## 6. Exit checklist

- [ ] `tox` green; `tests/unit` green on this host's default tmpdir (D3 closed).
- [ ] Every deletion in P0.1 named in its commit message with its zero-caller grep; the four documents in §2.7 of the assessment say what the code does.
- [ ] `ipc._REGISTRY` has no message without a sender; `GUARDED` holds every library command that deletes or re-walks.
- [ ] `plugin/` opens no Kodi database (grep `Database("video")` and `Database("music")` under `lib/kofin/plugin/` returns nothing).
- [ ] S-P0.0 through S-P0.11 recorded under `tests/live/results/`, each with the build sha, the rig, the server, the negative control's result and the fix's; L1-only entries say why.
- [ ] `kofin-jf12`'s `kofin.db` column restored by a Repair; `serverAddress` back on jf12; `kofin.db` mode back to 0644; no stub server left listening.
- [ ] `docs/testing-plan.md` has the `S-P0` section and the S3.7 prefix; `docs/shell-refactor-assessment.md` §10 Tier 0 marked done with the date.

## 7. Decisions (were open questions; answered 2026-08-28)

1. **Guard both:** `UPDATE_LIBRARY` and `REFRESH_BOXSETS` join `GUARDED`; `SYNC_LIBRARY` leaves the registry.
2. **New strings:** `#30821` for the 5xx toast and `#30822` for the refused restore, both through the i18n toolchain in one pass.
3. **P0.5 lands here:** `_show_names` reads the title over JSON-RPC in this phase.
4. **`chmod 000` is P0.6's trigger**, on the throwaway `kofin-jf12` profile only.
5. **The vanished arm proceeds with the cleanup** after a refused restore, logging at ERROR.
6. **The remote seek is a one-shot thread**, bounded as today.

These answers are the go-ahead; implementation starts with P0.0.
