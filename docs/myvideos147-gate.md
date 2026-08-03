# Opening the schema gate for MyVideos147

| Field | Value |
|---|---|
| **Date** | 2026-08-03 |
| **Status** | Gate open — `SUPPORTED["video"] = {131, 146, 147}` |
| **Upstream** | xbmc/xbmc `5100ce04b5` (2026-08-02), `version.txt` = 22.0 BETA1 (Piers) |
| **Bump commit** | `1d72708ac3` — "[Video][Database] Fix malformed DOS paths/embedded DOS paths from rar addon in the video database." |

---

## What 147 is

Kodi 22 bumped `CVideoDatabase::GetSchemaVersion()` from 146 to 147 mid-beta. The bump is **data-only**: the `if (iVersion < 147)` block in `xbmc/video/VideoDatabaseMigration.cpp` contains no `CREATE`, `ALTER`, `DROP`, `INSERT` or index statement — only `UPDATE`s that repair paths the rar VFS addon wrote badly.

Two repairs, over movies and episodes:

* `SanitiseUrlEncodingV147` lowercases `%XX` escapes in the **host element** of a URL, so `rar://…%2F…` and `rar://…%2f…` stop comparing unequal. Applied to `path.strPath` where it is `LIKE 'rar://%'`, and to `episode.c18` (base path).
* DOS paths get their separators normalised (`/` → `\`), applied to `movie.c22` (base path) and, via `FixParentPath`, to the parent-path ids `movie.c23` / `episode.c19` — repointing them at the corrected `path` row.

Nothing else in the block touches the database.

## Why the schema is unchanged

Verified mechanically against the two trees — `ref/kodi-piers-full` at `eb23114439` (the 146 side, 2026-07-07) versus `origin/master` at `5100ce04b5` (the 147 side):

| Check | Result |
|---|---|
| `CVideoDatabase::CreateTables()` body | **identical** |
| `CVideoDatabase::CreateAnalytics()` body | **identical** |
| `xbmc/video/VideoDatabaseDDL.cpp` (the views) | unchanged — absent from `git diff --stat` |
| `xbmc/video/VideoDatabaseColumns.h` | changed, but the diff is a C++ refactor (`my_offsetof` → pointer-to-member); no `VIDEODB_ID_*` ordinal moves |
| `VideoAssetType` (`VideoManagerTypes.h`) | unchanged — `VERSION = 1`, `EXTRA = 2` |
| `CMusicDatabase::GetSchemaVersion()` | still **84** |
| `CTextureDatabase` | schema untouched (Textures14 stands); its only diff is a constructor change |

So a MyVideos147 database — freshly created or migrated up from 146 — carries exactly the 146 schema.

## Why the 146 fixture is the 147 fixture

The checked-in `tests/fixtures/myvideos146.sql` was re-verified against a **real Piers install** on the day the gate opened: the Bravia's live `MyVideos146.db` was pulled with its WAL, checkpointed, and dumped — **155 schema statements, zero differences** from the fixture.

That fixture plus the DDL identity above is what `myvideos147.sql` is: a byte-for-byte copy, `cmp`-clean against 146 by design rather than by accident. The same holds for `myvideos147_seed.sql` — the seed rows come from `CreateTables`, which is identical, so the `videoversiontype` numbering (`40400 Standard Edition`, …) is unchanged.

No 147 install existed anywhere reachable when this was written; the Bravia runs 22.0-BETA1 from 2026-06-30, which predates the bump. **When a 147 install is to hand, replace the fixture with a real dump** — `cmp` against 146 should still come back clean, and if it does not, this document is wrong and the gate should close until it is fixed.

## Impact on kofin

None, in either direction.

* **The migration cannot touch a kofin row.** It only rewrites paths matching `rar://%` or satisfying `URIUtils::IsDOSPath`. kofin writes `plugin://plugin.video.kofin/<library-id>/…` paths exclusively — neither predicate can match one.
* **The writers need no change.** Explicit column lists everywhere, no ordinal moved, no table gained or lost a column.
* **`EXTRA_ITEM_TYPE[147] = 2`**, same as 146, because the asset-type enum did not move. The extras pass keeps reading the VERSION itemType from the seeded `40400` row.
* **Music and textures are unaffected** — 84 and 14 as before.

## What changed here

* `sync/schema.py` — 147 in `SUPPORTED["video"]`, `EXTRA_ITEM_TYPE[147] = 2`.
* `tests/fixtures/myvideos147.sql` + `_seed.sql` — see above.
* `tests/unit/kodifixtures.py` — `PIERS_VIDEO_VERSION_147`, and the provenance note.
* `tests/unit/test_sync_writers.py` — a third L2 leg, `[piers147]`, so every writer invariant (full fidelity, idempotency, zero-orphan removal, extras itemType) is proven against 147 as well. 146 stays a leg: an install that never ran the newer build still has it.
* `tests/unit/test_sync_schema.py` — 147 passes the gate, discovery prefers a 147 file over a left-behind 146, and a new invariant test refuses to let any future version enter `SUPPORTED` without both a fixture and an `EXTRA_ITEM_TYPE`/`CHAPTER_ART_WRAPPED` entry.
