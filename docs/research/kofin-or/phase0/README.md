# Kofin OR — phase 0 record

4 October 2026. [Implementation plan](../../../kofin-or-implementation-plan.md) · [Parity ledger](../../../kofin-or-parity.md).

## Outcome and limits

Phase 0 establishes the branch, compatibility contract, acceptance ledger, reproducible probes and initial SQL baseline. **It does not implement or publish the API backend.** The development manifest is `0.90.0`, but current internal packages still contain the legacy SQL backend. The first public OR prerelease remains phase 3, after package isolation and a complete movie lifecycle.

The P1D Piers Flatpak is approved for development and release validation, as confirmed on 8 October 2026. Its behavioral results count toward acceptance without a separate binary-provenance gate. No upgrade or full-library reset was performed. Dynamic browser/resolver runtime code was unchanged; its independent regression gate is recorded in the ledger.

### Recorded environment

| Component | Value |
|---|---|
| Main baseline | `db709a28905ca3b697d7135814407e9476d29361`, version 0.29.0 |
| OR branch/version commit | `kofin-or`, `ed0c1b8` (manifest/changelog only, 0.90.0) |
| Shared CI | `5576a05` on OR; `fd382a5` on main (`cherry-pick -x`) |
| P1D Kodi | 22.0 beta2, `20260831-e513e0ff-dirty`, existing Flatpak |
| JSON-RPC | 13.200.0; captured from inside Kodi |
| Python | 3.13.15; `xbmc.python` 3.1.0 |
| Core addon API | `xbmc.addon` 21.90.802 |
| Installed Kofin under test | 0.29.0; all 49 sync Python files hash-match baseline main |
| Initial/final native library | 1,787 movies; 79 shows; 4,324 episodes; 0 music videos; 22,348 songs; 1,556 albums; 307 artists |
| Disposable Jellyfin | Official portable 12.1.0; generated media, loopback HTTP, remote access/discovery disabled |
| Jellyfin source reference | `../../ref/jellyfin/` from repo root; commit `4829b0b49eded3c6597ea61eef8b2344f8078826` at inspection |

The production Jellyfin movie enumeration returned **1,791** accessible movies. That is distinct from the pre-existing **1,787** native movies; this probe did not synchronize their difference.

## Compatibility contract

[requirements.json](requirements.json) contains the concrete interface floor used by [check_or_capabilities.py](../../../../tools/check_or_capabilities.py). The OR manifest now requires `xbmc.addon >= 21.90.802`; `xbmc.python >= 3.0.1` is retained. The additional application-major check rejects Omega. A core version alone does not establish compatibility: required method parameters, enum values and Python tag methods must also exist.

Run from the repository root:

```sh
python3 tools/check_or_capabilities.py docs/research/kofin-or/phase0/capabilities.json --allow-dirty
```

Without the explicit research exception, the checked-in dirty capture is rejected. The captured qualification field remains false because the tool checks interface presence, not behavioral safety or every release gate. Historical captures retain the policy at capture time; the current plan accepts the P1D Flatpak. Phase 1 will connect capability detection to startup without importing native SQL discovery. The contract should grow with enabled features; optional native assets are not a movie-preview prerequisite.

The captured Python music tag lacks `setLoaded`. The working Piers bridge is modern music tags plus `ListItem.setInfo("music", {"size": actual_size})`. Do not invent sizes or encode a generation into a fabricated size to force a scan. Initial supplied playcount/lastplayed was discarded; apply userdata after import.

## Scanner findings

| Scenario | Observed result | Consequence |
|---|---|---|
| Bind movie source; scan two query-style item URLs | Two native movies with supplied metadata | Supported insertion route |
| Bind show root and individual show (`containssingleitem=true`); scan | Show and episode imported | Reproduce both bindings; root binding alone was insufficient in earlier research |
| Bind music-video source; scan | One native music video | Supported insertion route |
| Movie scalar/userdata patch | Values applied | Public mutation/readback works |
| Movie refresh | Kodi ID changed; playcount restored from supplied tags (2), replacing the patched value (4) | Reconcile identity and preserve/restore desired userdata |
| Loaded music listing | Two songs plus native album/artist grouping | Supported with bridge |
| Change music title, preserve URL/size | Normal scan retained old title | Metadata-only edits need a rescan policy |
| Change actual fixture size | Normal scan applied new title and retained IDs | File hash detects this change; it is not a general metadata trigger |
| Fail before returning music rows | Both previously imported songs removed | Provider failure is treated destructively |
| Return one row, then fail listing | One of the previous two songs remained | Partial failure is also destructive |
| Explicit complete 2→1→0 listing | Scoped deletion worked | Intentional omission is useful only with complete snapshots |
| Register plugin music path as user source | Source appeared in `Files.GetSources` | Manual setup is supported |
| User source → “Scan item to library” → full tag scan Yes | Two songs retained, same IDs | Forced rescan works with loaded tags on this build |
| Change only title, repeat full tag scan | New titles applied, same IDs | Manual forced scan closes the metadata-hash gap |

The failed-listing tests each start from a separately restored, verified two-song baseline. Scan-finished callbacks arrived despite data loss. Neither that callback nor an RPC `OK` can mark a generation applied without readback.

### Source setup

Video content bindings use existing `VideoLibrary.SetSourceContent` with `metadata.local`; no GUI video source entry is required for the tested explicit scans. The probe binds `movies/`, `musicvideos/`, `shows/`, and `shows/show/`. It clears only its own bindings on cleanup. This does not implement Kofin's future user-selection UI.

Music insertion works with `AudioLibrary.Scan(directory=...)` without a registered user source. The full tag rescan action requires registration as a **music source**. In the Music files/source window, add `plugin://plugin.video.kofin.phase0/music/`, name it **Kofin phase0 fixture**, and decline an automatic scan if using the scripted sequence. Open that source's context menu in the source list, select **Scan item to library**, and answer **Yes** to the full tag scan prompt. The same action is not exposed inside a plugin directory on this build.

The public audio Scan schema and `UpdateLibrary(music,...)` route have no force flag. Implementation choices still need testing: supported scalar setters after initial music ingestion, targeted complete rescans for structural changes, manual full rescan where unavoidable, and an upstream explicit rescan/failure contract. Registered source membership does not establish parity with arbitrary synthetic source/album links.

### Music release gate

Publish immutable complete directories from Kofin-owned storage, pin the active generation, and keep the previous complete generation during network failure. Never enumerate Jellyfin from scanner callbacks. These measures prevent a server outage from becoming an empty listing; **they cannot guarantee safety if the provider itself crashes or fails before returning its cached rows**. Persist enough metadata/userdata for reconciliation after such a failure. Phase 5 must test crash/interruption recovery and decide the acceptable Piers limitation before general music rollout. The reproducible deletion behavior is a high-priority §9.1/§9.4 Kodi contribution.

## Initial SQL-main performance and behavior

[sql-baseline.json](sql-baseline.json) contains three samples for each catalogue, source hashes, fetch timing, counts, refusals and readback assertions. The installed main writer runs inside P1D's embedded Python with its current settings. Every SQLite connection is guarded to remain beneath a new disposable benchmark directory; no live Kodi database is opened by the benchmark. Raw production DTOs stay in the temporary remote profile and were removed during cleanup.

| Operation | Fixed 100 movies, median seconds | Real 1,791 movies, median seconds |
|---|---:|---:|
| Initial persistence | 1.0594 | 52.9154 |
| Unchanged pass | 0.4550 | 4.2516 |
| Metadata delta (10 movies) | 0.1193 | 0.5535 |
| Userdata delta (10 movies) | 0.0415 | 0.0523 |
| Deletion (10 movies) | 0.0112 | 0.3607 |

All six samples passed: expected native/mapping counts, no duplicates on unchanged replay, changed plots, playcount 7, expected deletion counts and no refused items. Native-file commits precede mapping commits every 50 items. The real DTO fetch took 22.7985 seconds and is reported separately; required child replies were prefetched/cached. Each sample uses fresh private databases; filesystem/CPU caches are not flushed.

**Scope:** movie persistence stage using private MyVideos149 and Kofin schemas. Pipeline hooks, artwork downloads, full mixed-media orchestration, peak memory and GUI responsiveness are excluded. These numbers establish the first reproducible writer baseline; they are not an end-to-end sync SLA or a Kodi performance claim. Full mixed-library and live browsing latency remain phase 1/3 and upstream performance-study gates. Compare a later API implementation with the same DTO capture/fields/operations before claiming a speedup.

## Disposable Jellyfin fixture

[jellyfin_fixture.py](../../../../tests/live/or_phase0/jellyfin_fixture.py) creates a new private directory, boots the supplied portable server, initializes an administrator, disables remote access/discovery, generates two movies, one TV episode and two tagged songs with ffmpeg, and disables metadata providers for those libraries. It refuses an existing work directory and never reads production connection settings. Credentials are private (directory 0700, file 0600); the process is stopped in `finally`.

```sh
python3 tests/live/or_phase0/jellyfin_fixture.py \
  --binary /path/to/portable/jellyfin \
  --out /tmp/jellyfin-fixture-result.json
```

Requires ffmpeg and an unused loopback port (default 18997). The observed official archive was `https://repo.jellyfin.org/files/server/linux/latest-stable/amd64/jellyfin_12.1-amd64.tar.xz`, SHA256 `09e58d1dc3722fb36b5ef0975fe55a65de5c73e8867ef7bbcd7ac81d44e1c59b`; verify this hash if reproducing from that mutable download location. The executable hash is also recorded in the evidence. No binary is committed.

The fixture verified userdata (favourite, playcount and 12-second resume), deletion of one generated movie file and its restoration, stopping the server after receiving page 1 of 2, a failed subsequent request, restart, restored item counts and persisted userdata. These are real server operations, not mocks. They validate the fixture; **Kofin sync recovery is not yet implemented or tested by it**. See [jellyfin-fixture.json](jellyfin-fixture.json). Startup and network field names were checked in local Jellyfin source (`StartupController.cs`, `NetworkConfiguration.cs`); the live instance's OpenAPI schema supplied the endpoint contract.

## Reproduce the Kodi probes

Use an idle test Kodi and configure a target in adjacent `kodi-drive`. The host driver defaults to P1D; addresses and credentials are loaded privately from kodi-drive. It uses SSH for owned fixture deployment and EventServer for `RunScript`; the actual native operations use **in-process** public APIs. The production addon will not require an HTTP server or SSH.

```sh
python3 tests/live/or_phase0/drive.py deploy
python3 tests/live/or_phase0/drive.py capture --out /tmp/phase0-capabilities.json
python3 tests/live/or_phase0/drive.py lifecycle --out /tmp/phase0-lifecycle.json
python3 tests/live/or_phase0/drive.py prepare-forced --out /tmp/phase0-forced-before.json
# Register the fixture music source and perform a full tag scan as above.
python3 tests/live/or_phase0/drive.py read-forced --out /tmp/phase0-forced-after.json
python3 tests/live/or_phase0/drive.py set-forced-metadata --out /tmp/phase0-metadata-before.json
# Perform the full tag scan again; URL and size remain unchanged.
python3 tests/live/or_phase0/drive.py read-forced --out /tmp/phase0-metadata-after.json
python3 tests/live/or_phase0/drive.py sql-baseline --out /tmp/phase0-sql-baseline.json
python3 tests/live/or_phase0/drive.py cleanup --out /tmp/phase0-cleanup.json
# Remove only the temporary music source in the UI; decline another library removal.
python3 tests/live/or_phase0/drive.py remove
```

The benchmark requires a configured installed main addon and existing large movie catalogue. The fixture's own media are synthetic and intentionally not playable. Deployment refuses to overwrite an unowned addon directory, and removal requires successful native cleanup and no remaining fixture music source. If interrupted, retain the fixture, rerun cleanup and then remove it; do not delete the provider first. The SQL benchmark emits checkpoints but the driver waits for the final `complete` marker.

## Evidence and cleanup

| File | Contents |
|---|---|
| [capabilities.json](capabilities.json) | Build, addon/Python capabilities, method definitions and referenced types, initial library totals |
| [lifecycle.json](lifecycle.json) | Video insertion/patch/refresh, normal music edits, failure and deletion scenarios |
| [forced-before.json](forced-before.json), [forced-after.json](forced-after.json), [forced-metadata-after.json](forced-metadata-after.json) | Song IDs and metadata across manual full tag scans |
| [music-source.json](music-source.json) | Registered synthetic source and observed full tag scan prompt |
| [sql-baseline.json](sql-baseline.json) | Fixed and production-sized writer samples; no production titles/URLs |
| [jellyfin-fixture.json](jellyfin-fixture.json) | Disposable server lifecycle assertions |
| [cleanup.json](cleanup.json), [final-state.json](final-state.json) | Zero remaining owned media; original native totals restored; source absent; Home window |

The fixture addon/profile, its private benchmark files and raw real-media DTOs were removed from P1D. Its user music source and video content bindings were removed. The original installed 0.29.0 addon and existing Kodi build remain. Locally created disposable Jellyfin processes were stopped; private test directories may be removed or retained for diagnosis. No credentials or production media metadata are in this evidence bundle.

## CI and maintenance policy

CI runs `black`, `ruff`, `mypy`, `test`, and `package` on main and kofin-or pushes and on PRs. Artifact names include the actual branch or PR number. Both branches use required checks, require the branch to be current, and prohibit force pushes/deletion; protection applies to administrators. Shared changes land on main and are ported with `cherry-pick -x` (or the reverse for a common fix first found on OR). OR-only dependencies/version/backend changes remain separate.

The phase 0 validation includes all 3,498 unit tests (3,474 in the initial run plus 16 socket tests rerun with loopback permission and 8 new contract tests), Black formatting, Ruff, mypy (134 source files), and a ZIP build. Test harnesses/docs are excluded from the ZIP. No release tag, GitHub release, repository feed update or official submission belongs to phase 0.
