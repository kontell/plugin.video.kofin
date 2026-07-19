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

## Architecture

**Two processes, three entry points.** `default.py` → `lib/kofin/plugin/router.py` handles every `plugin://` invocation (browse, play, settings buttons); `service.py` → `lib/kofin/service/main.py` is the long-running background service (sync, websocket, player monitoring, SyncPlay); `context_*.py` are context-menu shims. The plugin process is short-lived and must stay thin — anything stateful belongs to the service.

**Cross-process traffic is closed-world.** `core/ipc.py` is the registry of every NotifyAll message kofin sends — nothing may notify a string not declared there. `core/state.py` holds the *only* shared live state (a handful of window properties); anything else "must argue its way into this module". This replaces the fork's ~30 ad-hoc window properties.

**No module-global state.** Service restart tears down and rebuilds objects in-process (no module reload). Module-level mutable state breaks that path. The few exemptions (schema discovery cache, the shims monitor) carry in-place comments explaining why they are restart-safe; new globals need the same argument or a different design.

**The transplant boundary.** `lib/kofin/sync/` (writers, kodidb, pipeline) and `lib/kofin/syncplay/` are ported fork code; `lib/kofin/sync/shims.py` provides the fork-compat helpers they import. Do not "improve" semantics inside the transplant — the writers were proven equivalent to the fork's against real libraries (`tests/live/ab_diff.py`), and that proof only holds while semantics stay put. Shell code (`core/`, `plugin/`, `service/`) follows current idioms; expect two dialects and match whichever file you are in.

**The schema gate.** `sync/schema.py` refuses to write any Kodi database version not in its `SUPPORTED` map (currently Omega MyVideos131/MyMusic83 and Piers MyVideos146/MyMusic84). Supporting a new Kodi version means: dump `.schema` fixtures from a real untouched install into `tests/fixtures/`, extract creation-time seed rows, add the version to the L2 parameterization in `tests/unit/kodifixtures.py` + `test_sync_writers.py`, confirm the suite passes, then open the gate. Version-dependent constants (e.g. `EXTRA_ITEM_TYPE` — Piers renumbered Kodi's VideoAssetType enum) are keyed in `schema.py`, never inlined in writers.

**Three databases.** Kodi's own MyVideos/MyMusic (written directly, explicit column lists everywhere so additive schema changes are harmless); `kofin.db` mapping Jellyfin ids to Kodi ids with checksums for idempotency; `sync.json` as the resumable sync queue (entries leave only on success or a server-side 404).

**Tests are layered.** L1 units run against Kodistubs plus fakes in `tests/unit/fakes.py`. The L2 writer suite (`test_sync_writers.py`) runs the real writers against pristine databases built from the checked-in schema dumps, parameterized over both Kodi generations (`[omega]`/`[piers]` ids) — full-fidelity, idempotency (byte-identical dump), and zero-orphan removal invariants. Live gates per phase are tracked in `docs/testing-plan.md`.

## Constraints that are easy to re-break

- Never attach `<dependencies>` to a `list[string]` setting in `resources/settings.xml`: on Kodi Omega this silently unregisters the setting the condition references (comment at the spot in the file).
- MyMusic's own schema triggers stamp `dateNew`/`dateModified` with SQLite `DATETIME('now')`, unreachable by Python clock freezing — music dump comparisons must go through `music_dump()` in `test_sync_writers.py`.
- Kodi caches addon strings and some settings schema for the process lifetime; when a new string renders blank after `dev-install.sh`, restart Kodi before debugging further.
- Docs in `docs/` use one line per paragraph (no hard wrapping); `tools/unwrap_md.py` fixes wrapped files.
- Extras/videoversion writes must read the VERSION itemType from the seeded 40400 row and the EXTRA value from `schema.EXTRA_ITEM_TYPE` — both differ between Omega and Piers.
