# Video versions — implementation plan

Date: 2026-07-29. Extends the phase-3 movie-extras work (native `videoversion` assets) to Jellyfin **multi-version movies**, and fixes the known **extras duration** bug along the same code path. Upstream comparison is against [jellyfin-kodi#1110](https://github.com/jellyfin/jellyfin-kodi/pull/1110) (*Native Video Versions and Extras*, open) and the narrower extras-only [jellyfin-kodi#1106](https://github.com/jellyfin/jellyfin-kodi/pull/1106). Rewrite-research §9 listed versions as YAGNI for v1; extras already exercise the asset tables, so this is the deferred follow-on called out in S3.4 notes.

**Deliverable**: multi-version movies sync as native Kodi video versions (`videoversion.itemType = VERSION`, named `videoversiontype` rows); the info-dialog Versions UI works; each version plays the correct Jellyfin MediaSource; extras keep working and show the **feature's** duration, not the film's. Omega (MyVideos131) and Piers (MyVideos146) both green on L2; live gate on Omega.

---

## 1. Scope

**In**:
* Map Jellyfin `MediaSources` (count > 1, or a single non-standard-named source) onto Kodi `videoversion` rows with `itemType = VERSION`.
* Name each version via find-or-create / find-or-match `videoversiontype` (prefer Kodi's seeded builtins — "Director's Cut", "Theatrical Cut", … — over always inventing USER-owned types).
* Primary version: keep the movie's own `files`/`movie.idFile` row as the primary version; stamp a non-40400 type when Jellyfin's primary MediaSource has a real edition name.
* Alternate versions: one `files` + `videoversion` row each, plugin play URL carrying both `id` (Jellyfin item) and `mediasourceid` so play resolves the right source.
* Streamdetails + runtime on **every** asset file (versions **and** extras) — fixes the duration bug.
* Play-path selection: honor `mediasourceid` query param (today always takes `MediaSources[0]`).
* L2 fixtures for Omega + Piers; live gate for a multi-version movie + re-check S3.4 duration.

**Out**:
* TV multi-version (Kodi has no native TV versions UI; YAGNI until a real request).
* Merging/unmerging versions on the server (Jellyfin admin concern).
* Settings toggle (kofin ships extras always-on; versions follow the same default — cost is bounded, see §5).
* PR #1106's separate "extra as its own media type / create_entry_extra" model — superseded by native assets.
* Changing how TV extras are browsed (plugin listing stays).

---

## 2. Upstream comparison

### 2.1 What kofin already has (movie extras)

| Concern | kofin today |
|---|---|
| Source | `SpecialFeatureCount > 0` → `Api.special_features(id)` |
| Rows | `files` + `videoversion` (`itemType = EXTRA` from `schema.EXTRA_ITEM_TYPE`) |
| Type name | `ExtraType` → `schema.EXTRA_TYPE_NAMES` → find-or-create `videoversiontype` with `owner = USER (2)` |
| Identity | upsert key = plugin play URL in `files.strFilename` |
| Streams / runtime | **not written** ← duration bug |
| Playstate / artwork on the asset | not written |
| kofin.db reference for the asset | none (extras play by Jellyfin id in the URL) |
| Schema constants | VERSION read from seed row `40400`; EXTRA from `schema.EXTRA_ITEM_TYPE` `{131: 1, 146: 2, 147: 2}` (147 keeps Piers's numbering — `docs/myvideos147-gate.md`) |
| Failure mode | best-effort; never gates movie sync |
| Setting | none (always on) |

Key code: `writers/movies.py` `extras()` / `extra_filename()`; `kodidb/movies.py` `add_extra_asset` / `get_extra_type_id`; `schema.py` `EXTRA_ITEM_TYPE`.

### 2.2 Upstream PR #1110 (versions + extras)

Unified `add_versions(API, obj, extra=False)` path:

* Versions from `item["MediaSources"]`; extras from `get_extras` (SpecialFeatures).
* Skips the source whose `Id == item.Id` (primary) when iterating alternates; primary type is resolved in `movie_add` via `get_or_create_videoversiontype`.
* Each alternate: full item fetch (`get_item(source.Id)`), own path/file, streams, playstate, `videoversion` artwork, jellyfin.db reference.
* Type naming: regex on Jellyfin name vs filename (`" - Director's Cut"` suffix, strip `/3D|/DVD|/Bluray`, fall back to Standard Edition `40400`). **kofin leaves this** — use `MediaSource.Name` as-is (empty → 40400).
* `itemType` for extras: **`self.itemtype + 1`** (works on both Omega and Piers only because VERSION and EXTRA are adjacent — fragile if Kodi ever inserts a type between them).
* New types inserted with `owner = 1` (system/provider, not USER).
* Cleanup: any version file whose type id is not in the current set is deleted; movie delete cascades all version files.
* Gated behind settings `useVersions` / `useExtras`.
* Also touches native-mode path slash fixes (irrelevant to kofin — plugin mode only).

### 2.3 Upstream PR #1106 (extras only)

Separate `extra` writer with `create_entry_extra` and per-extra `videoversiontype` rows. Functionally inferior for kofin's model (kofin already matched the native-asset design #1110 uses for extras). **Do not port.** Useful only as a second reference for "write streams on the extra's file_id".

### 2.4 What to take / leave from #1110

| Idea | Verdict for kofin |
|---|---|
| MediaSources → VERSION rows next to existing EXTRA rows | **Take** — core of the feature |
| Write streams + runtime on every asset file | **Take** — also fixes extras duration |
| Match/create named `videoversiontype` | **Take**, but prefer matching Kodi builtins by name (case-insensitive) before creating USER-owned types; keep `owner = USER (2)` for created rows (matches extras + Kodi's convert-to-extra flow) |
| `itemtype + 1` for EXTRA | **Leave** — keep `schema.EXTRA_ITEM_TYPE`; add explicit VERSION resolution from seed `40400` (already done) |
| Extra `get_item` per MediaSource | **Leave as default** — MediaSources already ship on the movie DTO (`downloader` Fields include `MediaSources`) with `Path`, `Name`, `RunTimeTicks`, `MediaStreams`, `Container`. Use the source payload; only fetch a full item if a future case needs UserData we do not already have |
| Settings gate | **Leave** — always-on, same as extras |
| jellyfin.db reference per version file | **Optional v1** — play URL carries ids; playstate for non-primary versions can land later. Prefer not growing kofin.db shape until playstate-on-versions is a real need |
| Primary-only guard (`check_movie_file_primary`) | **Not needed** — kofin never writes version/extra jellyfin ids as movie references today, so movie updates cannot re-enter as "version rows" |
| Cleanup of removed versions | **Take** — same upsert/prune pattern as `extras()` |

---

## 3. Omega vs Piers

Confirmed against fixtures `tests/fixtures/myvideos131_seed.sql` / `myvideos146_seed.sql` and `schema.EXTRA_ITEM_TYPE`:

| | Omega MyVideos131 | Piers MyVideos146 |
|---|---|---|
| `videoversiontype` seed `40400` `itemType` | **0** (VERSION) | **1** (VERSION) |
| EXTRA `itemType` | **1** | **2** |
| Builtin type ids / names | same 40400–range catalogue | same, with `itemType` column shifted |
| Table shape `videoversion` / `videoversiontype` | identical columns | identical columns |

Rules already in CLAUDE.md / `schema.py`:

* VERSION itemType: **read from the seeded 40400 row** at writer init (`Movies.itemtype`) — never hardcode 0/1.
* EXTRA itemType: **`schema.EXTRA_ITEM_TYPE[schema_version]`** — never `itemtype + 1`.
* Any new constant that differs by schema goes in `schema.py`, keyed by version id, never inlined in writers.
* L2 suite is parameterized over both schemas; every asset assertion must use the helpers `version_item_type()` / `extra_item_type()` already in `test_sync_writers.py`.

Piers already has the extras pass green (L2). Versions add no new schema gate work — only new assertions on the VERSION side of the same tables.

---

## 4. Duration bug (extras)

### 4.1 Symptom

Extras list shows the **film's** duration. After an extra is played, Kodi rewrites that file's duration from the actual stream and the UI updates. Matches "wrong at rest, right after play".

### 4.2 Cause

`add_extra_asset` only inserts `files` + `videoversion`. The movie's streams are written to the **movie's** `file_id` via `add_streams` in `movie()`; extras get no `streamdetails` rows. Kodi's extras/versions UI falls back to parent/movie runtime when the asset file has no video stream duration.

PR #1110 / #1106 both call `add_streams` on the asset `file_id` with the feature's own `Runtime` and `MediaStreams`. kofin does not.

### 4.3 Fix (same change versions need)

For each extra (and each alternate version):

1. Map the feature/source's media streams through the existing `API.video_streams` / `audio_streams` / `media_streams` helpers (or a thin equivalent that accepts a MediaSource / SpecialFeature DTO).
2. Convert `RunTimeTicks → seconds` the same way `movie()` does.
3. Call `self.add_streams(file_id, streams, runtime)` after the asset row exists.
4. On prune/delete, existing `delete_extra_file` already cascades `streamdetails` via the Kodi `delete_file` trigger — no new cleanup.

Also refresh streams on re-sync when the feature set is unchanged but we want duration correctness for already-synced libraries: either (a) always rewrite streams for existing assets (cheap, few rows), or (b) one-shot repair on next movie update. Prefer **(a)** — `add_streams` already deletes-and-reinserts for a file_id.

L2: assert `streamdetails.iVideoDuration` for an extra's `idFile` equals the feature's runtime, **not** the parent movie's.

---

## 5. Design

### 5.1 Data flow

```
movie DTO (already has MediaSources, SpecialFeatureCount)
        │
        ├─ movie_add / movie_update          → movie row + primary videoversion
        │     primary type ← MediaSource where Id == item.Id (or first)
        │     named via resolve_version_type(name) → 40400 or builtin or USER type
        │
        ├─ versions(obj, item)               NEW
        │     for each MediaSource with Id != primary:
        │       files + videoversion (itemType=VERSION, idType=resolved)
        │       plugin URL: id=<item.Id>&mediasourceid=<source.Id>&mode=play
        │       add_streams(file_id, source streams, source runtime)
        │     prune VERSION rows whose URL no longer desired
        │
        └─ extras(obj, item)                 EXISTING, extended
              SpecialFeatures fetch (unchanged)
              files + videoversion (itemType=EXTRA)
              + add_streams(...)             NEW (duration fix)
              prune EXTRA rows (unchanged)
```

Primary file identity stays the movie's existing plugin URL (`id=<movieId>&dbid=<kodiId>`). Alternate versions do **not** change `movie.idFile`.

### 5.2 Version type resolution

**Source of the label is only `MediaSource.Name`.** No filename-stem comparison, no ` - <edition>` parse, no #1110 path heuristics. The on-disk basename still appears in the plugin play URL for identity; it is not used to invent a type name.

```
resolve_version_type(MediaSource.Name, item_type) -> idType
  1. strip whitespace
  2. empty or "Standard Edition" (case-insensitive) → 40400
  3. SELECT id FROM videoversiontype
       WHERE name = ? COLLATE NOCASE AND itemType = ?
     (matches builtins and any USER types we already created)
  4. else INSERT owner=USER, itemType=VERSION, return lastrowid
```

Do **not** invent a parallel map like `EXTRA_TYPE_NAMES` for versions — the seeded catalogue is the map.

### 5.3 Play path

Today (`plugin/play.py`):

```python
sources = info.get("MediaSources") or []
source = sources[0]
```

Change: if request has `mediasourceid`, pick the matching source (fallback to `[0]` with a warning if missing — server may have reorganized sources). Primary movie URLs omit the param and keep current behavior.

Alternate-version filenames (plugin query string):

```
plugin://plugin.video.kofin/<libraryId>/?filename=...&id=<movieId>&mediasourceid=<sourceId>&mode=play
```

Use the **movie** Jellyfin id (not a phantom version id) so `api.item` / `playback_info` stay on the parent; `MediaSourceId` is forwarded into the stream URL builder (already accepts it at `play.py` / `api` layer).

Extras keep `id=<featureId>` (features are real Jellyfin items) — no mediasourceid needed.

### 5.4 Primary version naming

When the primary MediaSource has a meaningful edition name, set the primary `videoversion.idType` to the resolved type instead of hardcoding `40400` in `add_video_version_obj`. Today:

```python
add_video_version_obj = [..., "{VideoVersionItemType}", 40400]
```

Make the last slot `"{VideoVersionTypeId}"` (default 40400). On update, rewrite the primary version's `idType` if the edition name changed (`UPDATE videoversion SET idType=? WHERE idFile=?` — new query, small).

Single-source movie whose only MediaSource is labeled "Director's Cut" still shows that label in the Versions UI — the case #1110 explicitly called out.

### 5.5 Idempotency and prune

Mirror `extras()`:

* `desired[filename] = source` for every non-primary MediaSource.
* Existing VERSION assets for the movie: delete file_ids whose filename not in `desired`.
* Missing filenames: insert.
* Present filenames: refresh streams (+ optional type/name if we want rename detection — nice-to-have; type change can force delete+add if the URL identity is name-stable).

Identity key = plugin URL (includes `mediasourceid`), so a source rename that keeps the same MediaSourceId updates streams in place; a replaced source (new id) prunes the old row.

### 5.6 Cost

* No extra HTTP for versions — `MediaSources` already on the movie DTO (`downloader` Fields line includes it).
* Extras still one SpecialFeatures call per movie with `SpecialFeatureCount > 0` (unchanged).
* Stream writes are local SQL only.

### 5.7 Failure mode

Versions pass is best-effort inside the movie write, same try/except envelope as `extras()`. A bad MediaSource must not roll back the movie row. Checksum short-circuit (`check_unchanged`) still skips the whole movie write when the Etag is unchanged — so a code upgrade that only adds streams needs either a one-time library repair or a schema/feature stamp; see §7 migration note.

### 5.8 Playstate (versions vs extras)

| | Jellyfin | Kodi |
|---|---|---|
| Multi-version movie | **One** `UserData` per item (resume / played / playcount). `MediaSourceInfo` has no UserData fields. Playback progress *sessions* carry `MediaSourceId` (which file is playing) but the **stored** resume is still on the item. | Bookmark + playcount are **per `files.idFile`**, so each version file *can* hold its own local resume. |
| Extra (special feature) | Separate item → own `UserData` | Own `idFile` → own bookmark |

There is **no clean Jellyfin API for per-version playstate**. Copying item-level UserData onto every alternate `idFile`, or inventing per-MediaSource storage, would be a hack — out of scope.

**Versions:** keep today's movie behaviour — item UserData → primary `movie.idFile` only; playback reports go to the movie item id (with `MediaSourceId` for the session, as already). Alternate version files get no synced playstate. Local Kodi resume on an alternate after local play stays Kodi-local.

**Extras:** real items, so per-extra playstate would be a clean later addition (feature UserData → that file's bookmark; report against feature id). Not part of this work (needs report + userdata paths, not just streams).

---

## 6. Code map

| Area | Change |
|---|---|
| `sync/schema.py` | Optional `VERSION_ITEM_TYPE` doc comment only — still read from 40400 at runtime. No new map required unless we ever cannot trust the seed. |
| `sync/kodidb/queries.py` | Parameterize `add_video_version_obj` type id; add `update_video_version_type` (idType by idFile); optionally generalize `get_extra_assets` → `get_video_assets(movie_id, item_type)` (already filtered by itemType — rename for clarity). |
| `sync/kodidb/movies.py` | `resolve_version_type(name)` (builtin-first); `add_version_asset` / reuse `add_extra_asset` renamed to `add_video_asset`; keep `extra_itemtype` + `itemtype`. |
| `sync/writers/movies.py` | `versions(obj, item)` pass; primary type in `movie_add`/`movie_update`; extend `extras()` with streams; shared helper `_asset_streams(dto_or_source)`. |
| `sync/fields.py` | Small helper to build Streams dict from a MediaSource or SpecialFeature payload (or call existing `API` methods with a synthetic item). |
| `plugin/play.py` | Select MediaSource by `mediasourceid` param. |
| `tests/unit/test_sync_writers.py` | Multi-version fixtures; duration assertions for extras; Omega+Piers via existing param. |
| `tests/unit/test_play.py` (or play-related unit) | MediaSource selection. |
| `docs/testing-plan.md` | New live gate S-versions (or S3.6); amend S3.4 duration note. |
| `docs/rewrite-research.md` | Move versions out of YAGNI §9 when this lands. |

No settings XML / strings required for v1 (native UI is all Kodi strings).

---

## 7. Migration / already-synced libraries

This is only about the **add-on upgrade path** (old kofin → new kofin on an already-synced library), not Jellyfin or Kodi upgrades.

* **New installs**: full sync writes versions + streamed extras correctly.
* **Existing installs after upgrade** — **repair-only** (locked):
  * Movies whose Jellyfin Etag changes pick up versions and fixed extra durations on the next normal update.
  * Unchanged movies keep wrong extra durations and no version rows until the user runs **Repair library** (or a full resync / metadata touch that changes Etag).
* No one-shot migration flag, no forced re-write of multi-version/extras movies without Etag churn. Changelog notes "Repair library if extras still show film duration or versions are missing."

---

## 8. Tests

### L2 (writers, Omega + Piers)

1. **Multi-version write**: movie with 2 MediaSources → 2 `videoversion` rows with `itemType = version_item_type()`; primary `idFile == movie.idFile`; alternate has plugin URL containing `mediasourceid=`; types resolve to sensible names (builtin Director's Cut when named that way).
2. **Single non-standard primary**: one MediaSource named "Director's Cut" → primary `idType` is the Director's Cut builtin (or USER type with that name), not 40400.
3. **Idempotency**: second write byte-identical dump.
4. **Prune**: drop one MediaSource → alternate file + videoversion gone; movie row intact.
5. **Removal**: movie remove leaves zero `videoversion` / orphan `files` / `streamdetails` for that media.
6. **Extras duration**: feature with `RunTimeTicks = 600_000_000` (60s) → that file's `streamdetails.iVideoDuration == 60`; movie runtime remains the film's.
7. **Extras still correct itemType**: existing extras tests keep asserting `extra_item_type()` (1 / 2).
8. **Versions never gate**: inject a broken MediaSources shape → movie still written.

### L1

* `play` with `mediasourceid` selects that source; missing id falls back to `[0]`.
* `resolve_version_type` unit cases: empty → 40400; `MediaSource.Name` "Director's Cut" → builtin id; novel name → created USER row, second call returns same id. No filename input.

### Live (Omega)

* **S-versions**: multi-version test movie shows Versions affordance on info dialog; both versions listed with correct names and durations; playing each reports the matching file / MediaSource (log `MediaSourceId=…`); resume/watched on primary still works.
* **S3.4 regression**: The 25th Hour extras show ~feature duration before any play; play still works.

---

## 9. Implementation order

1. **Duration fix for extras** (shipped as PR #25). Streams helper + `extras()` call + L2 duration assertion — independently user-visible.
2. **Video versions** (single follow-up): play-path `mediasourceid` selection + version type resolution + primary type id + `versions()` pass + L2 Omega/Piers + live S-versions + docs.

Play selection without version rows is a dead param; version rows without play selection all play the primary source. Keep them one PR.

---

## 10. Key decisions

1. **Always-on, no settings** — matches extras; MediaSources are already on the DTO so the cost is SQL-only.
2. **Schema constants stay explicit** — VERSION from seed 40400, EXTRA from `schema.EXTRA_ITEM_TYPE`; never `itemtype + 1` as in #1110.
3. **USER-owned created types, builtin-first match** — aligns with kofin extras and Kodi's own convert-to-extra; reuses "Director's Cut" etc. without duplicating rows.
4. **Version label = `MediaSource.Name` only** — no filename parse; empty/Standard → 40400.
5. **No per-version jellyfin.db refs in v1** — play URL is authoritative; avoids reference-table churn and the #1110 primary-guard complexity.
6. **MediaSource payload, not get_item** — avoids N extra HTTP calls on every multi-version movie sync.
7. **Duration fix ships with (or before) versions** — same `add_streams` call site; do not leave extras half-done.
8. **Plugin-mode only** — no native-path / DVD / Bluray version special cases from #1110.
9. **Add-on upgrade is repair-only** — no forced re-write; users who want existing libraries fixed run Repair library.
10. **No per-version playstate sync** — Jellyfin has no per-MediaSource UserData; Kodi can store per-file resume but wiring that without a server model is a hack. Item-level on the primary only (§5.8).

---

## 11. Open questions

None remaining.

---

## 12. PR plan

### PR 1 — Extras duration fix (done: #25)
* Streamdetails for movie extras; `streams_and_runtime` helper; L2 duration assertions.

### PR 2 — Native video versions (this work)
* **Title**: feat(sync): native Kodi video versions from Jellyfin MediaSources
* **Files**: `plugin/play.py` (`mediasourceid`), `writers/movies.py`, `kodidb/movies.py`, `kodidb/queries.py`, L2 + play unit tests, testing-plan / rewrite-research / README
* **Deps**: PR 1 (merged — streams helper)
* **Description**: One PR for the whole feature — play-path MediaSource selection, `versions()` pass, primary type from `MediaSource.Name`, prune, streams on alternate files, Omega+Piers L2. Always-on. No settings.

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| Kodi UI still shows film duration if it reads `movie.c11` instead of asset streamdetails | Verify on live S3.4 before declaring fixed; if skin/core uses parent runtime, document limitation |
| MediaSource.Id stability across server rescans | Identity includes mediasourceid; a rescan that regenerates ids will prune+readd (acceptable; same as path renames) |
| Builtin name mismatch ("Directors Cut" vs "Director's Cut") | COLLATE NOCASE exact match on `MediaSource.Name`; no fuzzy match — USER type is fine |
| #1110-style delete of *all* versions when primary changes | Do not copy that; prune only VERSION rows not in `desired`, never touch EXTRA from the versions pass |
| Checksum short-circuit hides duration/versions on add-on upgrade | Repair-only (§7); changelog tells users to Repair library if needed |
