# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

Live verification happens against a running Kodi (JSON-RPC on `localhost:8080`, `kodi:kodi`) per the scenarios in `docs/testing-plan.md`; scenario evidence lives in `tests/live/results/` (gitignored). A service-only change needs an addon disable/enable bounce; new `strings.po` ids need a full Kodi restart (string cache).

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
5. Review the draft on GitHub (`gh release view vX.Y.Z`), edit if needed, publish.

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

- Never attach `<dependencies>` to a `list[string]` setting in `resources/settings.xml`: on Kodi Omega this silently unregisters the setting the condition references (comment at the spot in the file).
- A library node `<label>` that is a bare number is resolved against **Kodi's own** strings (`CGUIControlFactory::FilterLabel`, and `plugin.library.node.editor` does the same), where the 30000+ addon range is empty — so addon strings must be written out as text (`sync/views._node_label`) and only Kodi-core ids stay numeric. A wrong one here is silent: the node renders with no title rather than failing.
- Generated nodes all live under `special://profile/library/video/kofin/` and every deletion path is gated on the `kofin` name prefix — hand-made node files sit in the same tree and are never ours to remove. `views_hash()` folds in `NODE_LAYOUT`; bump it whenever the generated tree's shape or labels change, or existing installs never regenerate.
- Every show's own path row carries `strContent='tvshows'` + `metadata.local` **and** `useFolderNames=1`, and the episode object repeats that stamp because it writes the same row. The info dialog only re-reads an item from the database when a scraper resolves for its path, and `URIUtils::GetParentPath` drops a `plugin://` path straight to the addon root, so the library row above is never consulted — without the stamp shows and episodes lose cast, tags, country, director and writer; without `useFolderNames` the dialog refuses to open at all ("dont lookup on root tvshow folder"). The addon root stays bare for the opposite reason (`writers/tvshows.py`).
- A plugin route that blocks on a modal cannot be reached as a library node: Kodi runs the node's `<path>` through `CDirectory::GetDirectory`, and the dialog fights that fetch. Such routes fire an IPC and exit (`plugin/syncplay.py`, `plugin/adduser.py`); the service owns the dialog on a named worker thread. A node `<path>` is a folder path, so it tends to be written `?mode=syncplay/` — `router.dispatch` strips that trailing slash before the handler lookup, because without it the mode simply misses the table and silently serves the root listing.
- MyMusic's own schema triggers stamp `dateNew`/`dateModified` with SQLite `DATETIME('now')`, unreachable by Python clock freezing — music dump comparisons must go through `music_dump()` in `test_sync_writers.py`.
- A boxset pass that ends with zero linked members must **not** stamp its reference checksum (`writers/movies.py` writes it NULL on purpose): the NULL is what re-verifies emptied sets every walk so they spring back after a permission flap without any Etag movement. Stamping it "for consistency" recreates the permanently-empty-set bug (`docs/boxsets-robustness-plan.md` V6). Likewise `boxset_state` rows are the drift probe's expectation — a membership write that skips the stamp, or a set removal that leaves the row, makes the probe heal-loop or go blind.
- Kodi caches addon strings and some settings schema for the process lifetime; when a new string renders blank after `dev-install.sh`, restart Kodi before debugging further.
- Docs in `docs/` use one line per paragraph (no hard wrapping); `tools/unwrap_md.py` fixes wrapped files.
- Extras/videoversion writes must read the VERSION itemType from the seeded 40400 row and the EXTRA value from `schema.EXTRA_ITEM_TYPE` — both differ between Omega and Piers.
- Chapter-thumb cache keys must match Kodi byte-for-byte or the seeding silently renders blank tiles: raw `chapter://` on Omega, percent-encoded `image://video@…/?chapter=N` on Piers (keyed by `schema.CHAPTER_ART_WRAPPED`). The encoder/CRC vectors in `test_chapter_thumbs.py` are bench-verified against real installs — change `sync/kodidb/texture.py` only against them.
- Jellyfin 10.11 rejects a `/Items` query whose `SortOrder` count differs from its `SortBy` count with an opaque 400 ("Error processing request"), so a caller overriding one must override both; `downloader.align_sort_order` enforces the arity for every `get_items` caller.
- `PlayMedia`'s `resume` flag cannot resume a `plugin://` path: Kodi gates it on `GetItemResumeInformation().isResumable`, which a bare plugin path never satisfies (it logs `LoadDetails: Unsupported item type`), so the flag downgrades itself to `noresume` and the play route is handed `resume:false`. A caller that starts its own playback must state the position in the params (`startticks`), as `plugin/context.py` does.
- `downloader._get_items` must fail loudly: it submits every page up front, so a swallowed error there yields zero pages, which reads downstream as an empty library that synced fine. A consumer that abandons the generator early must go through the `finally` that cancels the pending pages and releases the buffer semaphore — without it, `ThreadPoolExecutor.shutdown(wait=True)` waits forever and Kodi freezes on quit.
- Widget refreshes are fingerprint-gated and command paths own their own (`sync/widgetstate.py`, docs/widget-refresh-plan.md): a field home widgets render that the module does not hash is a stale-widget bug — extend the section and its `test_widgetstate.py` case together — while the deliberate omissions (raw play counts, lastPlayed values) are the gate's point, moving on every playback echo while rendering nothing. Never re-add a blanket refresh to `process_commands`, re-inline the drain refresh past the settle, or "fix" a missing refresh by firing builtins outside `refresh_libraries` — each re-creates the per-echo widget flicker the gate exists to stop.
- `UserDataChanged` names the user it is about and must be filtered on it (`service/main.py:own_userdata`): Jellyfin delivers the message to every session that *contains* the subject, and a session contains its additional users (`SessionInfo.ContainsUser`), so a device running Who's watching? is subscribed to everything its co-watchers play *anywhere* — their phone, a browser, another Kodi box. Applied unfiltered it wrote a stranger's watched flag and resume point into this Kodi's library while the server's copy for the logged-in user stayed correct, so nothing ever sent a correction. The feature is one-way by design: playing *here* credits the co-watchers, which the server already does from the session.
- The wake-time FastSync (`GUI.OnScreensaverDeactivated`) is unconditional on purpose: it is the only cover for a websocket that went half-open during long idle (the reconnect catch-up fires only on *detected* drops), and an empty catch-up is refresh-free under the fingerprint gate — do not re-gate it behind a setting, and do not add screensaver-inhibit logic back (an interrupted sync resumes from sync.json; the removed `dbSyncScreensaver` path also lost the user's screensaver setting on a mid-sync quit).
