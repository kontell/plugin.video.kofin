# CLAUDE.md

## Kodi knowledge lives in kodi-drive

Shared Kodi knowledge is **not** in this file. Use the `kodi-drive:*` skills, or read
`../kodi-drive/README.md`.

Directly relevant: `kodi-database-writing`, `kodi-library-data`, `kodi-texture-cache`,
`kodi-library-nodes`, `kodi-plugin-handles`, `kodi-addon-manifest`, `kodi-addon-lifecycle`,
`kodi-announcements`, `kodi-playback-resume`, `kodi-performance`, `kodi-addon-release`, and
`jellyfin-client` under `adjacent/`.

**Do not add generally-useful Kodi findings here** — contribute them to kodi-drive. This file
holds only what is specific to *this* add-on.

## What this is

Kofin (`plugin.video.kofin`) is a Jellyfin client addon for Kodi that mirrors selected server libraries directly into Kodi's own SQLite databases (MyVideos/MyMusic) for native library UX, with playback resolved back through `plugin://` paths. It is a rewrite of `jellyfin-kodi` on the principle "rewrite the shell, transplant the organs": the shell (entry points, settings, lifecycle, playback decisions) is new code; the hard-won parts (Kodi DB writers, sync pipeline, SyncPlay) are near-verbatim ports from the Kontell fork (branch:combined/syncplay-sync). See `docs/rewrite-research.md` for the rationale; the per-phase plans in `docs/` record design decisions and deviations.

## Commands

```bash
# Everything the repo gates on (black, mypy, pytest):
tox

# Individually (venv from requirements-dev.txt):
pytest tests/unit -q
pytest tests/unit/test_sync_writers.py::test_movie_write_full_fidelity   # single test
pytest tests/unit/test_sync_writers.py -k piers                          # one schema leg
black --check --diff .
mypy                                                                     # config in mypy.ini

# Dev loop against the local Kodi:
tools/dev-install.sh        # rsync working tree into ~/.kodi/addons and reload
tools/build.py [OUTDIR]     # Kodi-installable zip (default ./dist)
```

Live verification runs against a real Kodi per the scenarios in `docs/testing-plan.md`; evidence
lives in `tests/live/results/` (gitignored). Configure the target in
`~/.config/kodi-drive/targets.env` — see `kodi-connect`, and never put a host or credential in
this file.

A service-only change needs an add-on disable/enable bounce; new `strings.po` ids need a **full
Kodi restart**, because Kodi caches add-on strings for the process lifetime
(`kodi-process-control`).

## CI and releases

GitHub Actions mirrors the pvr.kofin split (quality gate vs tag release):

- `.github/workflows/ci.yml` — on every PR and push to `main`: `black`, `mypy`, and `pytest tests/unit` as separate Checks jobs, plus a `package` job that uploads a Kodi-installable zip artifact (`plugin.video.kofin-<ver>-prN-<sha>.zip` / `…-main-<sha>.zip`, 14-day retention). Live tests are not run in CI.
- `.github/workflows/release.yml` — on a `v*` tag: re-runs the quality gate, builds `plugin.video.kofin-<ver>.zip`, asserts the tag matches `addon.xml`, and opens a **draft** GitHub release whose body is the top paragraph of `changelog.txt`.

### Cutting a release

1. Bump `version="X.Y.Z"` in `addon.xml`.
2. Prepend a new top entry to `changelog.txt`:
   ```
   vX.Y.Z
   - <bullet>
   - <bullet>

   v…
   ```
   The top paragraph (up to the first blank line) becomes the release body.
3. Commit, merge to `main`, wait for CI green.
4. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.
5. Review the draft (`gh release view vX.Y.Z`), then **publish it yourself** —
   `kodi-addon-release` explains why a workflow cannot.

PR zips are Actions artifacts only; production install zips come from published GitHub releases.

## Architecture

**Two processes, three entry points.** `default.py` → `lib/kofin/plugin/router.py` handles every `plugin://` invocation (browse, play, settings buttons); `service.py` → `lib/kofin/service/main.py` is the long-running background service (sync, websocket, player monitoring, SyncPlay); `context_*.py` are context-menu shims. The plugin process is short-lived and must stay thin — anything stateful belongs to the service.

**Cross-process traffic is closed-world.** `core/ipc.py` is the registry of every NotifyAll message kofin sends — nothing may notify a string not declared there. `core/state.py` holds the *only* shared live state (a handful of window properties); anything else "must argue its way into this module". This replaces the fork's ~30 ad-hoc window properties.

**No module-global state.** Service restart tears down and rebuilds objects in-process (no module reload). Module-level mutable state breaks that path. The few exemptions (schema discovery cache, the shims monitor) carry in-place comments explaining why they are restart-safe; new globals need the same argument or a different design.

**The transplant boundary.** `lib/kofin/sync/` (writers, kodidb, pipeline) and `lib/kofin/syncplay/` are ported fork code; `lib/kofin/sync/shims.py` provides the fork-compat helpers they import. Do not "improve" semantics inside the transplant — the writers were proven equivalent to the fork's against real libraries (`tests/live/ab_diff.py`), and that proof only holds while semantics stay put. Shell code (`core/`, `plugin/`, `service/`) follows current idioms; expect two dialects and match whichever file you are in.

**The schema gate.** `sync/schema.py` refuses to write any Kodi database version not in its `SUPPORTED` map (currently Omega MyVideos131/MyMusic83 and Piers MyVideos146/MyVideos147/MyMusic84). Supporting a new Kodi version means: dump `.schema` fixtures from a real untouched install into `tests/fixtures/`, extract creation-time seed rows, add the version to the L2 parameterization in `tests/unit/kodifixtures.py` + `test_sync_writers.py`, confirm the suite passes, then open the gate. Version-dependent constants (e.g. `EXTRA_ITEM_TYPE` — Piers renumbered Kodi's VideoAssetType enum) are keyed in `schema.py`, never inlined in writers. A bump that provably changes no DDL can reuse the previous version's fixture, but only with the upstream evidence written down — `docs/myvideos147-gate.md` is the worked example, and `test_sync_schema.py` refuses any `SUPPORTED` entry lacking a fixture and its keyed constants.

**Three databases.** Kodi's own MyVideos/MyMusic (written directly, explicit column lists everywhere so additive schema changes are harmless); `kofin.db` mapping Jellyfin ids to Kodi ids with checksums for idempotency; `sync.json` as the resumable sync queue (entries leave only on success or a server-side 404). The chapter-thumbnail feature additionally writes Kodi's texture cache (Textures13/14) at playback time, behind the same schema gate (`sync/kodidb/texture.py`, seeded and reverted per play by `service/chapters.py`).

**Tests are layered.** L1 units run against Kodistubs plus fakes in `tests/unit/fakes.py`. The L2 writer suite (`test_sync_writers.py`) runs the real writers against pristine databases built from the checked-in schema dumps, parameterized over every gated schema (`[omega]`/`[piers]`/`[piers147]` ids) — full-fidelity, idempotency (byte-identical dump), and zero-orphan removal invariants. Live gates per phase are tracked in `docs/testing-plan.md`.

## Constraints that are easy to re-break

Generic Kodi constraints that used to live here are now in kodi-drive, and are not repeated:
`<reuselanguageinvoker>` placement and plugin-handle hangs (`kodi-addon-manifest`,
`kodi-plugin-handles`), `xbmcaddon.Addon()` during an update and overlapping service generations
(`kodi-addon-lifecycle`), the string cache (`kodi-process-control`), bare-number node labels and
`folder.jpg` (`kodi-library-nodes`), texture-cache re-encoding and chapter cache keys
(`kodi-texture-cache`), never writing the library through JSON-RPC and the MyMusic trigger clock
(`kodi-database-writing`), `PlayMedia`'s resume flag and subtitle ordering
(`kodi-playback-resume`), `UserDataChanged` fan-out and the 10.9 route migration
(`jellyfin-client`), and settings `<dependencies>` on a `list[string]` (`kodi-addon-manifest`).

What remains is kofin's own:

- Generated nodes live under `special://profile/library/video/kofin/` and **every deletion path is
  gated on the `kofin` name prefix** — hand-made nodes share the tree.
- Generated playlists live in a `Kofin/` folder under Kodi's own `playlists/video/` and
  `playlists/music/` — **capital K**, and the prefix gates deletion there too.
- Every show's path row carries `strContent='tvshows'` + `metadata.local` **and**
  `useFolderNames=1`, and the episode object repeats the stamp.
- **A boxset pass ending with zero linked members must not stamp its reference checksum** —
  `writers/movies.py` writes it NULL on purpose, and the NULL is what drives the drift probe.
- Reference checksums have exactly one spelling: `fields.reference_checksum`. Never inline
  `"|plugin"`; the feed and prune comparators used to, and agreed with each other while both
  being wrong.
- The backdrop is `fanart.webp` and **every part of that name is load-bearing**
  (`service/backdrop.py`, `core/api.py::splashscreen`, `plugin/browse.py`).
- Extras/videoversion writes read the VERSION itemType from the seeded 40400 row and the EXTRA
  value from `schema.EXTRA_ITEM_TYPE` — both differ across gated schemas.
- `downloader._get_items` **must fail loudly**: it submits every page up front, so a swallowed
  error yields zero pages, which reads downstream as an empty library rather than a failure.
- Widget refreshes are fingerprint-gated and command paths own their own
  (`sync/widgetstate.py`, `docs/widget-refresh-plan.md`).
- The wake-time FastSync on `GUI.OnScreensaverDeactivated` is **unconditional on purpose**: it is
  the only cover for a websocket that went half-open while asleep.
- Every property in `core/state.py` is shared across service generations — see
  `kodi-addon-lifecycle` for why two run at once, and treat that module accordingly.
- A sync thread that will not stop is a thread inside the HTTP retry ladder
  (`docs/library-thread-stop.md`); the two rules that follow are easy to undo.
- `discography` has no unique index, so `INSERT OR REPLACE` never replaces
  (`kodi-database-writing` has the general shape; the repair path is kofin's).
- Docs in `docs/` use one line per paragraph — `tools/unwrap_md.py` fixes wrapped files.
