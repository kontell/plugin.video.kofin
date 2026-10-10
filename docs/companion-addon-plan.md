# Companion add-on plan

Draft, 10 October 2026. This plans the companion service add-on that phase 6 of `docs/kofin-or-implementation-plan.md` decided on and deliberately did not build: the add-on, developed outside this repository and never part of the OR archive, that is allowed direct access to Kodi's databases and closes the parity gaps stock Piers public interfaces cannot. It takes the contract's kofin-or side as it ships in 0.93.0 (`sync/backends/api/companion.py`, `core/contract.py`) and the ledger's `G` rows (`docs/kofin-or-parity.md`) as its inputs, and gives the chapter seeding mechanism its own section because it is the first request kind that is per play rather than per row.

## 0. Summary

The companion is a Python service add-on, `service.kofin.companion`, published on repository.kontell only, that reads one document kofin-or writes, reconciles Kodi's databases toward it, and reports what it did in a status document kofin-or folds into its capability report. It carries main's database writers and schema gate as its own code, writes nothing kofin-or does not own, never writes while a scan runs, and every write it makes is undone by the document: a per-play write leaves when its request is withdrawn, an edit to a row leaves with the row. It is built in six phases, C0 to C5, each one request kind or family, each closed by a live gate on the P1D and by the ledger row moving from `G` to implemented-by-companion. kofin-or stays complete without it.

Four decisions are needed before code, listed in section 9: the identity and repository, how much of main's transplant to carry, whether the companion ever talks to the server (this plan says no), and whether the music family is in scope before the phase 8 Kodi contributions land.

## 1. Principles

1. **Only what stock public interfaces cannot do.** Every handler names the ledger row it closes and the phase 8 contribution that retires it; a Kodi version that has the contribution gates the handler off.
2. **kofin-or never depends on it.** Absent, the gap stays open and is logged once per generation, as today; present, the gap closes without a setting on kofin-or's side.
3. **Acknowledgement by readback where a public read exists, by the status document where none does.** A grouped version shows in `VideoLibrary.GetMovieDetails`, a seeded chapter in `Textures.GetTextures`; a season plot or an episode tag has no public read, so for those the companion's `done` carries the request's content hash and kofin-or re-requests only when the hash moves.
4. **Never while a scan runs, never a row kofin-or does not own.** Ownership is the resolver URL under kofin's native paths, read back from the row before the write, or for a texture the kofin device id inside the key.
5. **Schema-gated like main.** A database version outside the gate serves nothing and the status document says which.
6. **Reversible by the document.** The document is desired state, not a queue; the companion keeps a ledger of what it wrote and reconciles ledger against document at every wake, so a withdrawn request, a crash and a reset all converge the same way.
7. **No server access and no credentials.** kofin-or stages in its own profile whatever the companion needs from the server, so the companion is pure Kodi and holds no token.
8. **Carried, not re-derived.** The schema gate, the fixtures, the database wrapper, the kodidb helpers, the version and texture writers and their L2 tests come from main with their deviation notes intact.

## 2. Identity and distribution

- **Id and name:** `service.kofin.companion`, "Kofin Companion". The id is the placeholder kofin-or already looks for (`companion.ADDON_ID`), so no kofin-or change is needed to be found.
- **Extension points:** `xbmc.service` started at login, a settings page with one toggle per request family and a log-level switch, and a script entry for "Reconcile now" and "Show status" so a user can act without reading a log.
- **Repository and channel:** `kontell/service.kofin.companion`, GPL-3.0 like its siblings, the same CI shape as this repository (black, mypy, pytest, package; a `v*` tag opens a draft release that is published by hand). Published on repository.kontell and never submitted to the official repository, which is the whole reason it exists apart. The flatpak and the P1D already auto-update from repository.kontell.
- **Versioning:** its own line from 1.0.0; compatibility is the contract version in the document, not the add-on versions. The companion serves every contract version it knows and refuses a newer one with a status line.
- **Dependencies:** `xbmc.python 3.0.1` and nothing else declared. It does not require `plugin.video.kofin`, because the same add-on id is also main, which writes these rows itself; the companion runs only when kofin's `api-capabilities.json` says `backend: api`, and idles with a status line beside main or with no kofin at all.
- **Kodi floor:** Piers, the OR build's own floor (`xbmc.addon 21.90.802`). The gate opens MyVideos 149, MyMusic 84 and Textures 14 at first; Omega's 131, 83 and 13 are not carried, since no OR build runs there.

## 3. The contract, version 2

### 3.1 The request document

The document stays where and what it is: `special://profile/addon_data/plugin.video.kofin/companion/requests.json`, rewritten whole through a `.part` rename whenever the open set changes, with `v`, `generation`, `namespace` and `requests`. Version 2 adds `device` (kofin's device id, which every chapter key carries) and `kinds` (the kinds this kofin-or publishes, so the status can say which it cannot serve). Each request keeps `id`, `kind`, `library` and `item`, and its kind's own fields. The document is the desired state: a request present is wanted, a request absent is withdrawn, and there is no acknowledgement message to forge or lose.

### 3.2 The ping and the fallback

`Companion.Requests` over `JSONRPC.NotifyAll` stays the wake-up (`contract.COMPANION_REQUESTS`), carrying the path, generation and count. The companion also reads the document at start and whenever its mtime moves on a slow timer, so a ping lost while it was not running costs a timer tick, not a request.

### 3.3 The status document

New: `special://profile/addon_data/service.kofin.companion/status.json`, rewritten after every reconcile, with the companion version, the contract versions served, the gate result per database (`MyVideos149: ok`, `Textures14: ok`, or the version found and `unsupported`), the kinds served, the generation last applied, and per request its outcome: `done` with the content hash, `waiting` with the reason (`scan`, `locked`), or `refused` with the reason (`not owned`, `unsupported kind`, `gate`). A `Companion.Status` NotifyAll from the companion, declared inbound in `core/contract.py`, lets kofin-or re-read it at once; without the message kofin-or reads it on its next pass. kofin-or folds the served kinds and the refusals into `api-capabilities.json` beside the presence it already records, and logs each refusal once.

### 3.4 Acknowledgement per kind

| Kind | Public read kofin-or confirms by | Falls back to status |
|---|---|---|
| `group_versions` | `Readback.versions_in` no longer lists the rows | no |
| `chapter_art` | `Textures.GetTextures` with `url contains <item id>` and `chapter=`, count equal to the request's | no |
| `retire_assets` | the file rows gone from the scoped readback | no |
| `episode_tags`, `season_plot`, `stream_flags` | none | yes, by content hash |
| `remove_set` | `VideoLibrary.GetMovieSets` no longer lists it | no |
| music kinds | `AudioLibrary` reads where the field is public, else status | mixed |

### 3.5 Ownership, scan hold and concurrency

Before a write the companion reads the row it is about to touch and requires its `files.strFilename` or `path.strPath` to sit under `plugin://plugin.video.kofin/native/<namespace>/`, or for a texture that the key contains the document's device id; anything else is `refused: not owned`. It writes only while `Library.IsScanningVideo` and `Library.IsScanningMusic` are false and kofin's `kofin.native.hold` property is clear, wakes on `VideoLibrary.OnScanFinished` and `AudioLibrary.OnScanFinished`, and keeps every transaction short with main's busy-timeout wrapper. Statement order follows Kodi's own triggers, as the P1D stub learned: the version row moves before the duplicate movie is deleted, because Kodi's delete trigger removes a version row only while it still points at the deleted movie.

### 3.6 Ledger, sweep and reset

The companion's ledger, `special://profile/addon_data/service.kofin.companion/written.json`, lists every row and file it wrote by request id. At every wake it withdraws what the ledger holds and the document no longer names, applies what the document names and the ledger lacks, and confirms the rest. A whole-profile reset on kofin-or's side empties the document, so the companion's per-play rows go; its edits to Kodi's own rows need no reset, since they leave with the rows. Nothing of the companion's outlives the rows kofin-or owns.

## 4. Architecture

- `service.py` builds one `Companion` object per service generation: an `xbmc.Monitor` subclass for `onNotification` (`Other.Companion.Requests`, scan finished) and `onSettingsChanged`, a reconciler thread woken by an event or the timer, a handler registry keyed by kind, the gate, the ledger and the status writer. No module-global state, for the same restart reason as kofin.
- Each handler is one module with `desired(request)`, `apply(db, request)`, `withdraw(db, entry)` and `confirm(db, entry)`, idempotent, logging one line per request, and declaring the gate it needs (which database, which versions) and the Kodi version that retires it.
- Carried from main: `sync/schema.py` with its `SUPPORTED` map, keyed constants and fixture checks; `sync/db.py`; `sync/kodidb/movies.py`, `texture.py` and the `queries*` modules; the versions and extras methods of `sync/writers/movies.py`; `core/imagecache.py` for image dimensions; later the music writers the C5 kinds need. `tools/check_kodi_schema.py` and the schema-watch workflow are copied, since the companion has a gate and kofin-or does not; main keeps its own.
- The contract's shape lives in one JSON Schema file in this repository, `lib/kofin/sync/backends/api/companion-schema.json`, vendored into the companion and checked by both test suites, so a field added on one side fails the other's tests.

## 5. Phases

Each phase is one kofin-or pull request (the request kind, its readback, the capability flag and tests), one companion pull request (the handler, its L2 tests and any fixture) and a live gate on the P1D with the scratch-library recipe, after which the ledger row moves from `G` to implemented with a companion tag and the gaps report is updated.

### C0: Presence

The repository, CI, the service skeleton, the gate, the document reader, the status writer and the ledger, with no handler. Exit: `api-capabilities.json` on the P1D shows the companion's version and an empty served list, a document with an unknown kind yields `refused: unsupported kind` in the status, and the add-on idles beside main's profile with a status line and no open database.

### C1: Version grouping (S24, the first half of S25)

The `group_versions` handler makes the P1D stub Kodi's own: `ConvertVideoToVersion` semantics, that is the assets moved to the owner (`UpdateAssetsOwner`), the duplicate's movie art converted to version art (`SetVideoVersionDefaultArt`), the duplicate movie row deleted with its hash preserved, the version type named from the request's `name` through `resolve_version_type` and orphan types swept, and the default version set from a new `default` field kofin-or adds to the request from the server's default source. kofin-or's readback acknowledges as it already does. Exit: the scratch movie's versions are grouped within one pass of their import, the Versions Manager lists them under one title with the server's default, and a second pass opens no request.

### C2: Chapter art (S35, A03)

Section 6. Exit there.

### C3: Asset lifecycle (the rest of S25)

`retire_assets` for a version or extra the server renamed, removed or dropped: a renamed source renames the owned `videoversiontype` row, a dropped source's file row and version row go at once instead of waiting for Clean, and an extra that left the payload goes the same way. kofin-or publishes the file rows it would otherwise leave to Clean. Exit: rename and removal round trips on the scratch library without a Clean.

### C4: Video metadata (S14, S18, S23, S09)

Four small handlers: `episode_tags` writes the favourite tag's `tag` and `tag_link` rows with `media_type` episode and prunes them on unfavourite; `season_plot` updates `seasons.plot` for an owned season; `remove_set` deletes an owned set row that kofin-or has emptied; `stream_flags` writes exact `streamdetails` flags, source and version per file. Each names its retiring contribution (phase 8 orders 2 and 3). Exit: each round trip on the P1D, and the node tree's episode-favourite filter listing the tagged episode.

### C5: Music (S12, S27, S28, S29, S30)

The largest and last family, carrying main's music writers: `music_dates` for the bookkeeping dates, `music_shells` for empty album and artist rows and the singles shell, `music_credits` for discography and role rows, and `music_source` for source and album-source links. It depends on kofin-or's music catalogue carrying each fact, and it is the family most likely to be retired by phase 8 orders 1 and 3 upstream, so it is scheduled after those contributions are proposed and built only for what they do not take. Exit: the S27 and S30 scenarios on the P1D.

Out of scope: S45 page atomicity, which is not a row problem and stays with upstream job reporting.

## 6. The chapter seeding mechanism

### 6.1 What Kodi needs, and why it is the companion's

On 22.0b2 the chapters dialog sets every tile's art to `image://video@<encoded dynpath>/?chapter=N`, built from the resolved stream URL (`GUIDialogVideoBookmarks.cpp`), and the GUI resolves that key through the texture database only: a file at the right cache path without its row pair is invisible. On a miss the generated-image loader extracts a frame, and `CDVDFileInfo::CanExtract` refuses every http and https source because the internet-stream classifier lists them before the dead LAN clause; the probe on the P1D on 10 October 2026 confirmed it (15 chapters, no extraction, blank tiles) and also showed that the one scheme Kodi serves over HTTP without classing it a stream, `dav://`, does extract (7 visible tiles in 207 to 826 ms each) but makes Kodi fetch the stream's sibling files and parse the video as an EDL file, so it is not a route. Main's answer stands: write the server's chapter images into the cache under the keys the dialog is about to ask for (`docs/chapter-thumbnails-feasibility.md`), and the row pair is a database write, which makes it the companion's.

### 6.2 The split

kofin-or does the server side at claim time and the companion the Kodi side, so the companion fetches nothing and holds no token.

- **kofin-or, at `Player.onPlayBackStarted` after the claim,** where main's gated-off hook sits (`service/player.py::_start_chapter_thumbs`): when the item is eligible (a direct play of a video type with a path), it fetches the chapter list, keeps the chapters that carry an `ImageTag` and fall within the player's chapter count, downloads each image at `maxWidth` 640 into `companion/chapters/<request id>/<n>.jpg` under its own profile, measures the dimensions, and publishes a `chapter_art` request: the item id, the dynpath exactly as resolved, the play session, and `chapters` as a list of `n`, file path, width and height. Main's `service/chapters.py` worker is reused minus its database steps: the same thread, cancel event and cleanup.
- **kofin-or, on stop, end, error or a new claim,** withdraws the request, and after the readback shows zero rows, or after a short grace period, deletes the staged files. At service start the document is rewritten without chapter requests, because no claim is live, so a companion that comes up later removes what a crash left.
- **The companion's `chapter_art` handler** computes each key per Textures schema (`chapter_art_key`, the wrapped form with Kodi's encoder on 14), copies the staged file to `Thumbnails/<c>/<crc>.jpg` (`cached_rel_path`), inserts the row pair with `TextureCache.add` (empty hash so it is never revalidated, the mandatory `sizes` row), records the keys in the ledger, and on withdrawal removes rows and files with `TextureCache.remove`. At start it sweeps rows whose url carries the document's device id and no open request names (main's `sweep`), and never touches a row whose key lacks the device id.

### 6.3 Acknowledgement and timing

kofin-or confirms by `Textures.GetTextures` filtered on `url contains <item id>` and `chapter=`: the count equal to the request's is the acknowledgement and is logged with the time since play start; zero after withdrawal confirms the removal. Main measured 27 images landing within about 0.7 s of play start on the LAN; the ping and the copy add a fraction of a second, so the budget is tiles present before a user can reach the dialog. A dialog opened earlier shows blank tiles once, because the GUI caches a failed texture only in memory while the dialog holds it; closing and reopening re-requests and hits the rows.

### 6.4 Limits carried over from main

A transcode's HLS stream carries no chapters and a chapterless file lists none, so both are skipped; Jellyfin's dummy chapters never reach the player; server-side chapter image extraction is a per-library opt-in and its absence leaves the feature inert, to be documented; Kodi's `myvideos.extractchapterthumbs` off leaves the dialog without art and is never touched. Every play has its own keys because the dynpath carries the play session id, so the steady state is zero rows and the sweep handles crashes; Piers's own cache cleaner is the backstop.

### 6.5 Tests and the gate

The companion carries `tests/unit/test_chapter_thumbs.py`'s key, encoder, CRC and row-pair tests with the `textures14.sql` fixture; kofin-or gains publish-and-withdraw tests against the fake player for eligibility, the overlap with the player's chapter count, cancellation mid-download and the restart rewrite. The live gate is a kofin-resolved play on the P1D of a film with 15 chapters and one with 27, a window grab of the dialog with every visible tile rendered, the acknowledgement line in kofin's log, and zero chapter rows after stop.

### 6.6 Retirement

Phase 8 order 4 proposes that the dialog prefer chapter art already on the playing item before the generated key. On a Kodi that has it, kofin-or stamps `chapter<n>` art on the resolved item and publishes no request, and the companion's handler gates itself off by Kodi version.

## 7. Testing and evidence

- **Companion repository:** L1 units on Kodistubs; L2 writer tests against pristine databases built from the carried schema dumps, parameterised over the gated versions as in `test_sync_writers.py`, with the idempotency dump and zero-orphan invariants kept for every handler; contract tests against the vendored JSON Schema; evidence from live gates under its own `tests/live/results/`.
- **This repository:** tests per kind for publish, withdraw and readback, for the capability report's companion block and for the refusal logging; the ledger and the gaps report updated per phase.
- **Rig:** the P1D's OR profile with the scratch Jellyfin library recipe recorded in the rig notes, a window grab at every gate step, and the Kodi log's own lines as the second witness.

## 8. Risks

- **Two writers on one database.** The same posture main has held since the port: short transactions, the busy timeout, no write during a scan, and Kodi's trigger order respected.
- **A schema bump mid-cycle.** The gate refuses, the status names the version, kofin-or logs the gap as open, and the copied watcher opens the issue; nothing half-writes.
- **A withdrawal mid-write.** The ledger records before the write and the next wake reconciles, so the write is undone rather than orphaned.
- **A stale status.** It carries the generation and a timestamp; kofin-or treats one older than its own generation as absent.
- **The companion beside main.** Refused by the backend check at start, since main writes the same rows.
- **Document races.** The `.part` rename already in `companion.publish`, and a generation the companion has applied is not re-applied.

## 9. Decisions

Taken on 10 October 2026: the identity, repository and channel as proposed; main's transplant carried trimmed to what C1 to C4 need, with every deviation note kept beside its line; chapter images staged by kofin-or, never fetched by the companion; C0 to C4 built now and C5 only for what the phase 8 music contributions leave; the contract bumps to version 2 in C0, where the `device` and `kinds` fields arrive, and the companion serves versions 1 and 2. The questions as they were asked:


1. The identity, repository and channel as proposed in section 2.
2. Whether to carry main's transplant whole or trimmed to what C1 to C5 need; this plan recommends trimmed, with every deviation note kept beside its line.
3. Whether the companion may fetch chapter images itself from the anonymous image route instead of kofin-or staging them; this plan recommends staging, so that server reachability, the artwork cap and compression and any future authentication stay in kofin-or.
4. Whether C5 is built before the phase 8 music contributions are proposed, or only for what they leave.
5. Whether the contract bumps to version 2 with C1's `default` field or with C2's first new kind; the companion serves both either way.
