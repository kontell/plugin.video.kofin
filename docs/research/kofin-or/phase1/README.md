# Phase 1 — shared sync boundary and internal API package

Implemented 2026-10-06. The shared changes land on `main` first and are carried to `kofin-or` with `cherry-pick -x`. Main keeps its SQL backend and release sequence; OR selects the API profile and keeps the reserved `0.90.0` version. These are internal builds. Native API ingestion and the first public prerelease remain phase 3; the development repository remains phase 2.

## Contracts and state

`sync/model.py` contains a validated `MediaItem` envelope and the existing metadata conversions extracted from `fields.py`: naming, runtime, stream details, ratings, provider IDs and text normalization. `fields.API` retains the settings/server-dependent wrapper. `sync/policy.py` owns the unchanged-item/checksum/userdata decision. The conversions preserve SQL serialization and its existing tests; this is not a new metadata format or a claim that the future InfoTag serializer is complete.

`sync/backend.py` defines operations and `ApplyResult`. Prepared means a SQL transaction accepted work, staged means durable desired work still awaits Kodi, and applied means confirmed application. Notifications require an applied result. Workers, full sync, collection orchestration and Library call the selected backend; native connections, writer dispatch, hooks, maintenance and library removal live under `sync/backends/sql/`. The original writer and `kodidb` paths remain to make SQL maintenance and ports reviewable. Page locks, connection reuse and native-before-mapping commit order are preserved. A failed final commit reports unapplied work and cannot announce success.

`sync/private.py` can open only Kofin's own database and reads/writes its own `sync.json`; it imports no native discovery, schema or seeding. `sync/db.py` is the compatibility facade for the SQL adapter. Downloads' private records, subscriptions and other private readers use the new module directly. The API package cannot resolve a native database kind through this layer.

A backend/version marker distinguishes SQL and API mappings. Unmarked, populated legacy mappings count as SQL. Claiming incompatible mappings fails without adoption. Generic private records remain readable; branch switching still requires a fresh native library and cleared sync state. No automatic library reset is implemented or performed in this phase.

`sync/catalogue.py` provides a server/user namespace, desired and applied generations, durable upserts/removal intent, and replayable pending work. Confirmation must match the current desired generation; a stale callback or failure cannot advance it. This is the outbox foundation. Complete immutable directory snapshots, scanner pinning, operation-specific retries, identity readback and userdata feedback suppression are phase 3 work and are not represented as finished by this table.

## Package and runtime boundary

`lib/kofin/buildconfig.py` makes the backend an explicit build choice. `tools/package_manifest.py` controls both ZIP creation and development staging, including transformed manifests/settings. `tools/build.py --profile sql|api` can build either internal profile; omitting the option uses the branch's selection. `tools/dev-install.sh` installs the same staged bytes. It no longer has a separate exclusion list.

The API package physically omits SQL adapters/writers, native discovery/schema helpers, native database classes, texture writers, SQL cleaner, playlist/source maintenance, native widget refresh, download repoint/manager and the installed-manifest mutation helper. Its manifest requires the measured Piers core floor, advertises no scanner paths yet and exposes context entries only on Kofin dynamic items. API-only capabilities code is omitted from SQL packages.

| Existing native or installed-file caller | Phase 1 API treatment | Later work |
|---|---|---|
| Full sync, delta workers, schema checks and native refresh | Library thread is disabled; no native adapter is packaged | Movie lifecycle in phase 3; remaining types in 4–5 |
| Library selection/update/repair/collection repair | Settings omitted; old URLs resolve to a localized unavailable response and close their handles | Restore with each supported native scope |
| Broad SQL database cleaner | Module omitted and route gated | Explicit owned-content API reset/removal |
| Native download repoint, tags, playlist materialization, subscriptions | Manager/repoint omitted; background startup, context offers, routes and settings gated | Downloads integration in phase 6 |
| Chapter art and actor precache | Texture classes and services omitted; player/background callbacks gated even with old settings enabled | Public API design and later ports |
| Playback/native-ID backfill and context native lookups | Legacy lookup returns no mapping in API builds; dynamic Jellyfin identity continues to work | Validated API identity cache |
| Native music sources, generated native nodes and playlists | Disabled with native Library; SQL modules omitted | Music and presentation phases |
| `Kofin.nodes.*` dynamic skin entries | Published from live server views independently of native selection/state | Continue sharing live browser policy |
| Changing server backdrop | Content-addressed files in Kofin's profile; dynamic listings use the profile URL; installed art remains bundled | Native art integration if needed |
| `reuselanguageinvoker` setting/reconciliation | Setting/handler gated, including delayed startup; installed-file helper omitted | Keep manifest static |
| Private downloads/settings/history storage | Remains readable; no native file is needed to import or open it | Do not confuse preserved records with enabled downloads |

The API service starts without native modules, opens/claims private state and writes `api-capabilities.json` in its profile. Capability inspection uses `xbmc.executeJSONRPC` and the Python InfoTag interfaces directly; users need no HTTP server. A damaged private store or failed capability probe is logged and leaves native sync disabled without preventing the service's dynamic connection/playback lifecycle. Capability presence is not behavioral qualification.

## Dynamic browsing

Root and drill-down listings, filters, search, Next up, Continue watching, music, Play all/Shuffle, extras, stable browse/play URLs, media-source selection, watched/favourite actions and resume remain shared live-server paths. They do not consume the future scanner catalogue and do not require native sync selection or a Kodi-ID mapping. Existing downloads are explicitly unavailable in the internal API profile until their native dependencies are ported.

The extracted API package runs the established browser, ListItem, Play all, stream menu, playback/source/resume and action tests against its own shipped modules. Browser goldens cover no native selection, mixed selection, initial sync and failed sync. Separate package checks deny native imports and database access, import every shipped module, exercise service startup and delayed settings readiness, and reject writes outside the profile. A profile-art test checks that changing server artwork leaves installed artwork and texture cache alone. ZIP and development staging bytes are compared for both profiles.

Large-scan browser latency cannot be measured for this API implementation yet: phase 1 deliberately has no scanner. That concurrent workload, native identity interactions and real scanner recovery remain gates for the phase 3 preview; the current regression results do not substitute for them.

## Verification

Local complete suite: **3,651 passed**. Mypy checked 151 source files; Ruff and Black checks pass. The existing SQL writer suite still exercises its historical schema fixtures, including Piers MyVideos149/MyMusic84. Retaining those main-branch regressions does not add Omega support to OR. Both branches run the common suite; SQL-focused units select the SQL backend explicitly, while package subprocesses run the actual API distribution with no such override.

P1D uses the user-approved Flatpak **22.0 beta2, `20260831-e513e0ff-dirty`**. No RC1 binary was substituted. Live checks ran in isolated Kodi script interpreters with synthetic server responses, disposable MyVideos149/MyMusic84/private databases, private settings/window state and intercepted UI notifications. The active installed Kofin was not replaced. SQLite guards reject any path outside the fixture; the API run rejects native imports and permits only its own `kofin.db`.

The SQL comparison executes the actual FullSync media passes (100 movies, one series/season/episode, one music video, one artist/album/song) and real Update/UserData/Removed workers, comparing main base `fd382a5bc7440880b9eb41f702c8a7cd0629de0c` with the refactor. Each of five stages has identical populated table contents and mapping contents: initial full sync, unchanged full sync, metadata delta, userdata delta and removals. Music comparison normalizes only the runtime `lastScraped`, `dateNew`, `dateModified` values, preserving nullness; it excludes the administrative `versiontagscan` table. The deliberately new backend marker is excluded from mapping comparison. Userdata, metadata dates, relationships, source membership, IDs and checksums remain in the comparison. Both runs report zero unapplied items and cleared restore points.

Single-fixture timings are recorded in `comparison.json` as diagnostic evidence, not a throughput benchmark: the workers include their queue-drain timeout, and synthetic local DTOs omit server/network costs. Large production-library performance is still covered by the separate phase 0 baseline and will need new API scanner measurements.

The extracted API package starts its service and delayed settings lifecycle on P1D, reports private state ready and passes the public interface contract using the explicit dirty-build exception, with native access blocked. `stock_release_qualified` stays false. Production library counts before/after are unchanged. The owned phase 1 fixture is removed after evidence capture; the production library is never nuked for verification.

The harness uses explicit exceptions for its in-Kodi checks: Kodi can run optimized Python, which strips `assert` statements, including any call placed inside one. The guard and service invocation must execute independently of optimization.

## Reproduce the live checks

Run from the checkout being tested. Supply the adjacent kodi-drive path and an empty local work directory; SSH/RPC credentials are resolved only from its target configuration. The baseline commit must be available locally. `deploy` refuses an unowned remote fixture directory and active playback; `remove` checks the owner marker and the recorded unchanged-library result.

```bash
python3 tests/live/or_phase1/drive.py deploy --driver-dir ../kodi-drive --workdir /tmp/kofin-phase1-evidence
python3 tests/live/or_phase1/drive.py run sql baseline --driver-dir ../kodi-drive --workdir /tmp/kofin-phase1-evidence
python3 tests/live/or_phase1/drive.py run sql shared --driver-dir ../kodi-drive --workdir /tmp/kofin-phase1-evidence
python3 tests/live/or_phase1/drive.py run api api --driver-dir ../kodi-drive --workdir /tmp/kofin-phase1-evidence
python3 tests/live/or_phase1/drive.py check --driver-dir ../kodi-drive --workdir /tmp/kofin-phase1-evidence
python3 tests/live/or_phase1/drive.py remove --driver-dir ../kodi-drive --workdir /tmp/kofin-phase1-evidence
```

## Next boundaries

Phase 2 can create the development feed independently. Phase 3 must implement committed movie snapshots, scanner callbacks, resolver identity, API completion/readback, fresh-library gating and recovery before publishing `0.90.0`. Enabling the Library thread or advertising a scanner path alone is insufficient. Public prerelease automation and Kodi v23 contributions have not been implemented by phase 1.
