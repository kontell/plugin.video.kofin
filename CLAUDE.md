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

**The transplant boundary.** `lib/kofin/sync/writers/`, `lib/kofin/sync/kodidb/`, `fields.py`, `obj.py` and `lib/kofin/syncplay/` are ported fork code; `lib/kofin/sync/shims.py` provides the fork-compat helpers they import. They keep the fork's *shape* — the `obj` dict mapped from `obj_map.json`, the positional `*_obj` spec lists fed to `values()`, one writer method per media type — so a change can still be read against the fork. They do **not** keep the fork's semantics where the fork was wrong: about half the lines in `writers/movies.py`, `kodidb/movies.py` and `kodidb/music.py` post-date the port, each deviation annotated in place ("Deviation from the fork: …") and pinned by name in the L2 suite. What protects correctness is therefore `test_sync_writers.py` across every gated schema — full-fidelity rows, byte-identical idempotency dumps, zero-orphan removal — not equivalence with the fork: the A/B harness (`tests/live/ab_diff.py`) ran once, on the day of the port, for movies only, and is a historical artefact. Any restructure inside the transplant is proven by the idempotency dumps (same rows before and after), and a new deviation carries its note and its L2 test. The pipeline (`library.py`, `full_sync.py`, `downloader.py`, `views.py`) began as a port and is now kofin's own; see `docs/sync-refactor-assessment.md`. Shell code (`core/`, `plugin/`, `service/`) follows current idioms; expect two dialects and match whichever file you are in.

**The schema gate.** `sync/schema.py` refuses to write any Kodi database version not in its `SUPPORTED` map (currently Omega MyVideos131/MyMusic83 and Piers MyVideos146/MyVideos147/MyVideos148/MyMusic84). Supporting a new Kodi version means: dump `.schema` fixtures from a real untouched install into `tests/fixtures/`, extract creation-time seed rows, add the version to the L2 parameterization in `tests/unit/kodifixtures.py` + `test_sync_writers.py`, confirm the suite passes, then open the gate. Version-dependent constants (e.g. `EXTRA_ITEM_TYPE` — Piers renumbered Kodi's VideoAssetType enum) are keyed in `schema.py`, never inlined in writers. A bump that provably changes no DDL can reuse the previous version's fixture, but only with the upstream evidence written down — `docs/myvideos147-gate.md` is the worked example. Do not reach for that shortcut by analogy: 148 *does* move DDL (`streamdetails` gains `iSource`/`iVersion`), and `docs/myvideos148-gate.md` is the worked example of the full path, including why a NULL `iSource` is not a regression and what writing it deliberately would buy. `test_sync_schema.py` refuses any `SUPPORTED` entry lacking a fixture and its keyed constants.

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
  `playlists/music/` — **capital K** — but the two folders have opposite ownership rules.
  Under `playlists/video/Kofin/` the `kofin` prefix gates deletion and a foreign file is
  spared (`views.py`); under `playlists/music/Kofin/` the **folder** is the boundary — the
  managed `.m3u8` files are named after the server, carry no prefix, and every file not in
  the managed set is removed on the next poll (`playlists.py`). Nothing of the user's may
  be told to live there.
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
- **A TranscodingProfile is a device statement, not a spare tyre.** Jellyfin's `StreamBuilder`
  *ranks* the transcoding profiles instead of taking the first that matches, and one whose
  `VideoCodec` list holds the source codec ranks top so the server can stream-copy into it. So a
  codec offered there is a codec the server may hand back untouched, whatever `DirectPlayProfiles`
  says — kofin's unconditional fMP4/av1 leg answered `VideoCodec=av1` + `-codec:v:0 copy` for a
  device that had just refused av1, from *second* place in the list. Every codec in
  `_transcoding_profiles` must be gated on the same list that gates direct play; reordering
  cannot substitute for withdrawing it.
- `downloader._get_items` **must fail loudly**: it submits every page up front, so a swallowed
  error yields zero pages, which reads downstream as an empty library rather than a failure.
- **A downloaded item plays from disk however it is reached, online included.** The repoint
  (`downloads/repoint.py`) only covers plays that go through Kodi's own row, so `plugin/play.py`
  is the only place that can enforce it for anything playing by *id* — a kofin listing, a widget,
  a SyncPlay group start. The preference was offline-only once, and the plan's "downloaded items
  never reach the plugin" is what SyncPlay disproved: the initiator adopts the group queue for
  the playback it is already running (`syncplay/manager.py`), so only the follower reloads, and
  it streamed media the initiator was playing locally. `resolve_downloaded` pushes the claim
  itself rather than leaving it to `backfill_library_claim`, which needs a Kodi dbid off
  `Player.OnPlay` that a group start does not carry. A request naming a source, a track or a
  bitrate still streams — a download has only the tracks it was made with.
- Widget refreshes are fingerprint-gated and command paths own their own
  (`sync/widgetstate.py`, `docs/widget-refresh-plan.md`).
- The wake-time FastSync on `GUI.OnScreensaverDeactivated` is **unconditional on purpose**: it is
  the only cover for a websocket that went half-open while asleep.
- Every property in `core/state.py` is shared across service generations — see
  `kodi-addon-lifecycle` for why two run at once, and treat that module accordingly.
- **SyncPlay converges position on commands, and between them only by confirmed tempo
  pulses through inputstream.tempo — never by a continuous loop against Kodi's own
  tempo.** The `Player.SetTempo` ladder was removed after measurement, not after taste:
  that actuator exists only while Kodi's sync-to-display is on, and the setting slaves the
  media clock to the panel, so a refresh rate that is not a whole multiple of the frame rate
  imposes a fixed rate error — 0.5 %–4.3 % across three Piers devices, the worst 14× the
  ±3 % the ladder could command (`docs/syncplay-drift-shakedown.md` §10). The actuator in use
  now is inputstream.tempo, which rate-shifts inside the demuxer and works with that setting
  **off**, where the same devices free-run within a few hundred ppm. `syncplay/tempo.py`
  issues one bounded pulse at a time, confirms it from the add-on's state file, waits a queue
  depth before measuring again, and gives up on a one-signed residual
  (`docs/syncplay-fine-sync.md`). A transcode, an audio item, or a Kodi without the add-on
  gets command-only sync, exactly as 0.19 did.
- A sync thread that will not stop is a thread inside the HTTP retry ladder
  (`docs/library-thread-stop.md`); the two rules that follow are easy to undo.
- **Never call `xbmc.Player.stop()`** — use `core/kodirpc.py::stop_player`. Kodi's binding sends
  `TMSG_MEDIA_STOP` without a `DelayedCallGuard`, so it blocks on the app thread *holding the GIL*,
  and the app thread's stop path waits on MyVideos writes that a GIL-starved kofin thread may be
  holding. That is a deadlock with no recovery but a force-stop (issue #155, measured on Omega and
  Piers). `pause()` has the same missing guard but does not tear the player down, so it does not
  reach the wait — it is a stall risk, not a deadlock, and is deliberately left alone inside the
  SyncPlay transplant.
- `discography` has no unique index, so `INSERT OR REPLACE` never replaces
  (`kodi-database-writing` has the general shape; the repair path is kofin's).
- **"Recently added albums" asks `/Items/Latest`, never albums sorted by `DateCreated`** — an
  album's DateCreated is when the scanner last created its row, and a rescan re-creates rows in
  folder order (`docs/dynamic-libraries-plan.md` §3). The sync writes album `dateAdded` from the
  same field; that is a known, separate question.
- **"Play with transcoding" is gated on `ListItem.DBTYPE`, not on `kofin.id` alone** — every
  dynamic row carries the id, songs, albums and genres included, and the DBTYPE clause is what
  keeps the entry off them (`addon.xml`).
- **A listing row with no server position still stamps `setResumePoint(0, total)`** — a zero point
  with a total is "set, nothing to resume" to Kodi, which is what stops it falling back to the
  bookmark it saved for the plugin path; the *resolved* item in `plugin/play.py` must never be
  stamped with zero, for the opposite reason (`plugin/listitems.py` explains both).
- The who's-watching shortlist packs three states into one string setting
  (`plugin/adduser.py`): `all`, an id list, or `none` for "feature off". **Empty is `all`, not
  off** — it is what every install predating the sentinels holds, and it is what an unreadable
  settings store reads back, so reading it as off would hide the root entry and strip the session
  on an add-on update. `is_enabled` is the only spelling of that test, and turning the feature off
  detaches the session's co-watchers (`detach_all`) because the picker is the only way off one.
- `kofin.menu.who` and `kofin.menu.syncplay` are the skin-facing mirrors of those two root
  entries (same gates as `plugin.browse.root`). Skins cannot read addon settings; the service
  publishes on `mark_ready` and whenever the two settings change.
- Docs in `docs/` use one line per paragraph — `tools/unwrap_md.py` fixes wrapped files.

## Translations

English lives in `resources/language/resource.language.en_gb/strings.po`; the other 26 locales are
**generated**, never hand-edited. Translations are authored into `tools/i18n/tr/<locale>.json` and
rendered by `tools/i18n/gen.py`. `tools/i18n/README.md` has the recipes and `PROMPT.md` is the
brief every translation is written against. They are machine translations, tagged
`pending native review` in each file header — leave that line until a native speaker has actually
looked.

Adding or rewording a user-visible string means touching 28 files, and the toolchain is what keeps
that mechanical: add the id to `en_gb`, add the key to all 26 JSONs (or to `PASSTHROUGH` if it is a
bare technical token), `gen.py --snapshot`, then `gen.py && validate.py && pocheck.py`, and commit
the lot together. `tr/_source.json` records the English each translation was made from, so a
**reworded** msgid fails the generator instead of silently keeping a stale translation — the drift
`pvr.kofin` had to repair by hand. `tests/unit/test_translations.py` runs the validators, so CI
catches a locale left out of step.

Three help strings quote another string's wording verbatim (`#30794` quotes `#30618`; `#30607`
quotes `#30609`/`#30610`; `#30080` quotes `#30817`) and `pocheck.py` enforces all of them —
translate such a pair together or the help names a control that is not on screen under that name.
The quoted label goes in ASCII double quotes even where the locale uses its own quotation marks
elsewhere in the same string, because that is what the check looks for. `#30624`/`#30626`/`#30631`/`#30633`/`#30635`
substitute an item *name* where their plural partners take a count; the category noun in "Dune movie
added to library" is what separates the film from the album.
