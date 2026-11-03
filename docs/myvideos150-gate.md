# Opening the schema gate for MyVideos150

| Field | Value |
|---|---|
| **Date** | 2026-11-03 |
| **Status** | Gate open — `SUPPORTED["video"] = {131, 146, 147, 148, 149, 150}` |
| **Upstream** | xbmc/xbmc `Piers` at `846211c81d` (2026-11-02), version string 22.0-RC1 (21.90.901); the 149 side is `22.0b2-Piers` (`e513e0ff43`), where the 149 fixture came from, and `22.0rc1-Piers` (`28ea2eac1e`) is 149 too |
| **Bump commit** | `31234724a5` — "[Video][Database] Repair versions turned into extras by upgrades < v131" (authored 2026-10-17, committed 2026-10-29, merged to Piers 2026-10-31 in `58024beae7`) |
| **Raised by** | #390, from `tools/check_kodi_schema.py` |

---

## What 150 is

Like 147, and unlike 148 and 149, **150 moves no DDL**. The step is one call:

```cpp
  if (iVersion < 150)
    KODI::DATABASE::MIGRATION::RepairVideoAssetTypesV150(*this, *m_pDS, *m_pDS2, iVersion);
```

Migration steps 123 to 131 were written for the `itemType` values of their day (version 0, extra 1) but use the ones v134 introduced (version 1, extra 2), so an upgrade from before 131 turned movie versions and their types into extras. `RepairVideoAssetTypesV150` puts them back, choosing by the version the database started at:

| Started at | What runs |
|---|---|
| below 123 | every `videoversion` and `videoversiontype` row becomes a version |
| 123–130, and 134 on | the four statements below |
| 131–133 | nothing (Omega's 131 upgrades without the repair) |

The four statements, for the branch every Piers database takes:

1. A `videoversion` row on its movie's own file (`movie.idFile`) that is marked extra becomes a version.
2. A system type (`owner = 0`) marked extra becomes a version type.
3. An extra whose type is a system type, or a user extras type that versions also use, is moved to a new user extras type with the same name.
4. A user type marked extra that only versions use becomes a version type.

That is every statement. The one `INSERT` writes a `videoversiontype` row; nothing creates, alters or drops a table, view, trigger or index.

## Why the schema is unchanged

Upstream, `22.0b2-Piers` against `Piers` at `846211c81d`:

| Check | Result |
|---|---|
| `VideoDatabaseDDL.cpp` (`CreateTables`, `CreateAnalytics`) | **byte-identical** |
| `VideoDatabaseColumns.h` | **byte-identical**, so no `VIDEODB_ID_*` ordinal moved |
| `VideoManagerTypes.h` (`VideoAssetType`) | **byte-identical**: `VERSION = 1`, `EXTRA = 2` |
| `CMusicDatabase::GetSchemaVersion()` | still **84**; the file's only database change wraps the `tempDisco`/`tempAlbum` scratch tables it already used in a guard that drops them (`7e820179b9`) |
| `TextureDatabase.h` / `.cpp` | **byte-identical**, Textures14 |

**The fixture is a real dump, and it equals the 149 fixture.** Piers at `846211c81d` was built in the flatpak SDK sandbox (`ref/build/kodi-dev`, `src-piers150` → `app-piers150`) and started headless on an empty profile, so Kodi created MyVideos150 from `CreateTables` with nothing to migrate. The database is pristine: 0 movies, 0 episodes, 0 shows, 0 files, 0 paths, `version` = (150, 0) and 387 `videoversiontype` rows. `sqlite3 -readonly … .schema` gives `myvideos150.sql`, and those 387 rows give `myvideos150_seed.sql`. Both `cmp` clean against `myvideos149.sql` and `myvideos149_seed.sql`.

`MyMusic84.db` and `Textures14.db` from the same profile dump byte-identical to `mymusic84.sql` and `textures14.sql`, so both fixtures stand.

## Master's 150 is a different schema

`MyVideos150` names two schemas. xbmc's `master` took 150 first, on 2026-10-06 (`a715dc5b78`, "Store the movie set sort title"), and its step adds `sets.strSortSet`. Piers's 150 does not have the column. Master is at 154 now, and its 154 step adds `strSortSet` to a database that lacks it: a Kodi 22 database at 150 moving to Kodi 23. So a version number alone does not say which DDL a database carries. The fixture here is Piers's.

The gate admits by number, so a Kodi 23 alpha database still at 150 would pass it too. The writers would be safe there: every statement on `sets` in `sync/kodidb/queries.py` names the columns it reads or writes, and `strSortSet` is a nullable `TEXT`. Kodi 23 is served the master build, which has no gate, and the watcher probes Piers alone.

## What the migration does to kofin's rows

**Nothing.** None of the four statements matches a row kofin writes:

* **A movie's own file is a version.** The primary `videoversion` row takes the `itemType` Kodi stamped on the seeded Standard Edition (40400), and an extra always gets a `files` row of its own (`add_extra_asset`).
* **kofin creates no system type.** `get_extra_type_id` creates its rows with `VIDEO_ASSET_OWNER_USER`, and every seeded system row is a version type (all 387 are `owner = 0`, `itemType = 1`).
* **An extra never shares a type with a version.** `get_videoversiontype_by_name` matches on name *and* `itemType`, so a Jellyfin version and an extras kind with the same name ("Behind the Scenes") get one type each: a user version type and a user extras type.
* **A user extras type is used by extras alone**, for the same reason, so statement 4 has nothing to retype.

Two checks:

* **L2.** `test_movie_assets_are_unchanged_by_kodis_150_repair` writes a movie with three versions (two builtin types and "Behind the Scenes") and two extras (Behind the Scenes, Deleted Scene). It runs the repair's statements against the database, transcribed from `RepairVideoAssetTypesV150`, and asserts the dump is byte-identical. It runs on every Piers leg and skips Omega's, which Kodi does not repair. The test fails when the type lookup ignores `itemType`, and when an extra is put on a system type.
* **The real migration.** A MyVideos149 filled by kofin's writers (two movies, the versions and extras above, a set, a show with an episode: 504 rows) was put in a fresh profile of the same Piers build, with one control row added: a system type marked extra, id 40900. Kodi logged `Old database found - updating from version 149 to 150` and `Update to version 150 successful`. Across all 32 tables, exactly two rows changed: `version` (149 → 150) and the control row (`itemType` 2 → 1). Every row kofin wrote came through unchanged. The migrated database's `sqlite_master` equals that of the database created fresh at 150, and the left-behind `MyVideos149.db` was untouched.

## Impact on kofin

* **The writers need no change.** The schema is 149's, statement for statement, and the `[piers150]` L2 leg proves every writer invariant against it (full fidelity, idempotency, zero-orphan removal, extras `itemType`).
* **`EXTRA_ITEM_TYPE[150] = 2`** and **`SEASON_HAS_PLOT[150] = True`**, as for 149.
* **A library kofin filed on 149 is still kofin's on 150.** The migration changed none of its rows, so the first sync after the upgrade compares against exactly what it wrote.

kofin itself was not run on the 150 Kodi. With a byte-identical schema, the gate's set membership is all that differs from 149.

## What changed here

* `sync/schema.py` — 150 in `SUPPORTED["video"]`, `EXTRA_ITEM_TYPE[150] = 2`, `SEASON_HAS_PLOT[150] = True`.
* `tests/fixtures/myvideos150.sql` + `_seed.sql` — dumped from the fresh Piers profile, see above.
* `tests/unit/kodifixtures.py` — `PIERS_VIDEO_VERSION_150`, and the provenance note.
* `tests/unit/test_sync_writers.py` — a sixth L2 leg, `[piers150]`, and the repair test.
* `tests/unit/test_downloads_repoint.py` — the same sixth leg.
* `tests/unit/test_sync_schema.py` — 150 passes the gate, and discovery prefers it over four left-behind files.
* `README.md` and `CLAUDE.md` — the supported-schema list, and the watcher's scope.
* `docs/myvideos150-gate.md` — this document.
