# Design records

One line per paragraph (`tools/unwrap_md.py`). Each file is the record of a decision at the time it was made, and is not rewritten when the code moves on; CLAUDE.md says how the code is now.

## The build that ships

One build files the libraries through Kodi's public interfaces, on Kodi 22 and Kodi 23, and decides each feature from what the running Kodi offers.

- `kodi-api-sync-feasibility.md`, `kofin-or-implementation-plan.md`, `kofin-or-phase3-plan.md`, `kofin-or-parity.md`, `kofin-or-feature-gaps.md` and `research/kofin-or/`: the sync through Kodi's scanner and JSON-RPC, phase by phase. The parity ledger and the gaps are against the SQL build, which no longer ships.
- `video-versions-plan.md`, `dynamic-libraries-plan.md`, `offline-downloads-feasibility.md`, `offline-downloads-plan.md`, `downloads-shakedown-plan.md`, `playlist-syncing-design.md`, `music-playlist-syncing-design.md`, `new-content-notifications-plan.md`, `transcode-stream-selection-plan.md`, `critic-ratings-plan.md`, `lan-server-discovery-plan.md`, `translation-plan.md`.
- `syncplay-*.md`: the SyncPlay provider contract, the fine sync through Kodi's own tempo and the shakedowns that led to it. `syncplay-drift-shakedown.md` is why the `Player.SetTempo` ladder went.
- `library-thread-stop.md`: why a sync thread sometimes would not stop.
- `companion-addon-plan.md`: the companion add-on that stood in for Kodi 22's missing interfaces. It is retired; a Kodi without an interface now goes without the feature.

## The SQL build

The 0.29 series wrote Kodi's databases directly. It is frozen on the `sql` branch and these records describe it:

- `rewrite-research.md`, `phase1-implementation-plan.md` to `phase5-implementation-plan.md`, `testing-plan.md`: the rewrite of jellyfin-kodi and its direct database writers.
- `myvideos147-gate.md`, `myvideos148-gate.md`, `myvideos149-gate.md`: the schema gate, opened once per Kodi database version.
- `clean-databases-plan.md`, `boxsets-robustness-plan.md`, `healing-loops-plan.md`, `widget-refresh-plan.md`, `chapter-thumbnails-feasibility.md`, `default-streams-feasibility.md`: features and repairs of the direct writer.
- `sync-refactor-assessment.md`, `sync-refactor-phase1-plan.md`, `sync-refactor-phase2-plan.md`, `shell-refactor-*.md`, `audit-*.md`, `benchmark-*.md`, `perf-hardening-plan.md`: the audits and refactors of that pipeline.
