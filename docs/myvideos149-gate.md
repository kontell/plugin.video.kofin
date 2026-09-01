# Opening the schema gate for MyVideos149

| Field | Value |
|---|---|
| **Date** | 2026-09-01 |
| **Status** | Gate open — `SUPPORTED["video"] = {131, 146, 147, 148, 149}` |
| **Upstream** | xbmc/xbmc `22.0b2-Piers` (`e513e0ff43`), the 148 side being `bbfcf50529` (2026-08-14) |
| **Bump commit** | `8501cbdcc3` — "[Video] Store flags alongside audio and subtitle stream details." (authored 2026-08-24, committed 2026-08-27) |
| **Raised by** | #222, from `tools/check_kodi_schema.py` — the watcher's first catch |

---

## What 149 is

Like 148, and unlike 147, **149 moves DDL**. It is one more additive column on the same table, and nothing else:

```cpp
  if (iVersion < 149)
    m_pDS->exec("ALTER TABLE streamdetails ADD iFlags INTEGER DEFAULT 0");
```

`CreateTables` gained the same column inline — `… iSource integer, iVersion integer, iFlags integer)` — so a freshly created 149 and a migrated one converge on the same shape.

`iFlags` is the per-stream flag set from `StreamFlags` (`xbmc/cores/VideoPlayer/Interface/StreamInfo.h`), a bitmask:

| Value | Flag |
|---|---|
| `0x0000` | `FLAG_NONE` |
| `0x0001` | `FLAG_DEFAULT` |
| `0x0002` | `FLAG_DUB` |
| `0x0004` | `FLAG_ORIGINAL` |
| `0x0008` | `FLAG_COMMENT` |
| `0x0010` | `FLAG_LYRICS` |
| `0x0020` | `FLAG_KARAOKE` |
| `0x0040` | `FLAG_FORCED` |
| `0x0080` | `FLAG_HEARING_IMPAIRED` |
| `0x0100` | `FLAG_VISUAL_IMPAIRED` |
| `0x100000` | `FLAG_STILL_IMAGES` |
| `0x200000` | `FLAG_WEBVTT_DATA_PACKETS` |

It is written for **audio and subtitle rows only**: `CVideoDatabase::SetStreamDetailsForFileId` added `iFlags` to its audio and subtitle `INSERT`s and left the video one alone, matching `CStreamDetailAudio`/`CStreamDetailSubtitle` gaining an `m_flags` member while `CStreamDetailVideo` did not.

The same commit takes `CStreamDetail::STREAM_DETAILS_VERSION` from 2 to 3 and adds `STREAM_DETAILS_VERSION_FLAGS = 3` beside it, so Kodi can tell a row written before flags existed from one written without any set. That is the `iVersion` column 148 added, doing the job it was groundwork for.

## What else was checked

Mechanically, `bbfcf50529` (the 148 side, the ref `docs/myvideos148-gate.md` used) against the `22.0b2-Piers` tag:

| Check | Result |
|---|---|
| `VideoDatabaseDDL.cpp` | **byte-identical apart from the one `streamdetails` string** — no other hunk at all, so the views, the indices and the `videoversiontype` seeds are untouched |
| `CreateAnalytics` (views, triggers, indices) | unchanged — it lives in the file above |
| `VideoDatabaseColumns.h` | **byte-identical** — no `VIDEODB_ID_*` ordinal moved |
| `VideoManagerTypes.h` (`VideoAssetType`) | **byte-identical** — `VERSION = 1`, `EXTRA = 2` |
| `CMusicDatabase::GetSchemaVersion()` | still **84** (`MusicDatabase.cpp` byte-identical) |
| `CTextureDatabase::GetSchemaVersion()` | still **14** (`TextureDatabase.h` byte-identical) |

So `EXTRA_ITEM_TYPE[149] = 2`, and the music and texture fixtures stand untouched.

## 149 is not master-only

The issue reported 149 on `master` with the standard caveat that a development-line version may move again before it ships. It has already gone further than that: **`22.0b2-Piers` is tagged at 149**, and LibreELEC master pinned `PKG_VERSION="22.0b2-Piers"` at 02:22Z on 2026-09-01, so every LibreELEC 13.0 nightly from that day carries it. Someone on a Piers nightly meets the closed gate today, which is why this was not deferred to the release.

## The fixture is a real dump

`tests/fixtures/myvideos149.sql` was dumped (`sqlite3 -readonly … .schema`) from the same Raspberry Pi 3B that produced the 148 fixture, moved on to LibreELEC 13.0 `nightly-20260901-00c159b` — Kodi **22.0-BETA2 (21.90.802) Git:22.0b2-Piers**. The database is pristine: 0 movies, 0 episodes, 0 files, 0 paths, 0 shows, `version` = (149, 0), and 387 `videoversiontype` rows. `myvideos149_seed.sql` is those 387 rows, **byte-identical to 147's and 148's**.

That database was **created** at 149, not migrated. The video databases were deleted before the update, so Kodi had nothing to migrate from; the proof is in the dump text, as it was for 148. sqlite rewrites a table's stored `CREATE` when `ALTER TABLE ADD COLUMN` runs, so a migrated `streamdetails` would end `iFlags INTEGER DEFAULT 0` — the migration's own capitalisation and default. The dump instead reads `iFlags integer`, which is `CreateTables` verbatim.

Against the 148 fixture the dump differs on **exactly one line**, the `streamdetails` create, and it is the line upstream says it should be. Nothing else in the 120 lines moved.

Two cross-checks came off the same box, and both are DDL-only so its content does not matter: `MyMusic84.db` and `Textures14.db` dumped **byte-identical** to the checked-in `mymusic84.sql` and `textures14.sql`. That is a real Kodi 22.0-BETA2 agreeing with fixtures taken from a 22.0-BETA1 and, for MyMusic, originally from the Bravia.

**The box is no longer the pristine one the 148 document describes.** It has since become a working kofin test install with a 20,930-song music library, so only the `MyVideos*.db` files were removed; `MyMusic84.db` was left alone and the whole `Database/` directory was copied to `/storage/backup-database-2026-09-01` first. The *video* fixture is unaffected by any of that — Kodi built it from `CreateTables` with nothing to inherit.

## Impact on kofin

**None, and this time none even in principle.**

* **The writers need no change.** Every `streamdetails` statement in `sync/kodidb/queries.py` names its columns (`add_stream_video`, `add_stream_audio`, `add_stream_sub`), and both readers — `get_video_duration` and `service/libraryclaim.py`'s duration probe — select `iVideoDuration` by name. One more additive column is exactly the case the explicit-column-list rule exists for. The `[piers149]` L2 leg proves it rather than asserting it.
* **`EXTRA_ITEM_TYPE[149] = 2`**, unchanged from 146/147/148, because the asset-type enum did not move. The extras pass keeps reading the VERSION itemType from the seeded `40400` row.
* **kofin's rows land at `iFlags` NULL, and nothing reads that as a defect.** `CVideoDatabase::GetStreamDetails` guards the new field with `if (!pDS->fv(17).get_isNull())` and otherwise leaves `m_flags` at `FLAG_NONE`, so a NULL is indistinguishable from a row Kodi itself wrote for a stream with no flags set. (That read is positional: `iFlags` is ordinal 17 in the fixture, so it lines up.)

  This is the point where 149 differs from 148 and is *easier*. A NULL `iSource` has a consequence — it sits at the bottom of the precedence ladder, so the player may overwrite kofin's server-derived details. Flags take no part in any comparison: the bump commit leaves both `IsWorseThan` bodies untouched and adds nothing to `ShouldUpdateWithNewDetails`. A NULL `iFlags` costs nothing at all.

## The live check

Run on the same Pi immediately after the dump, with the branch build (0.22.0) installed over the 0.18.1 that was there: Kodi 22.0-BETA2, MyVideos149, MyMusic84, the music library whitelisted.

**Zero `schema gate` errors, and a full sync cycle against the real server** — items dequeued, artwork written, `--[ sync/2026-09-01T14:44:26Z ]` — with the 20,930-song music library intact afterwards.

MyVideos149 stayed empty throughout: the box's whitelist is music-only, so the live leg proves the gate and the music writers on a real 149. The video writers on 149 are the `[piers149]` L2 leg's job, exactly as they were 148's.

**What the closed gate looked like first, and why it is not a finding.** Before the deploy, 0.18.1 against 149 logged 484 `schema gate: unknown video database v149` lines in 612 s of uptime — one per service tick — because `_reap_library` empties the slot, `_start_library` builds a fresh `Library`, and it dies on the gate again a second later. That is not current behaviour, and it is worth writing down so the next gate bump does not re-report it: `_recover_threads` on `main` paces library rebuilds through `_library_backoff` (5 s doubling to a 120 s ceiling), and its docstring names this exact case — "a persistent failure there — an ungated Kodi database, most plainly". 0.18.1 predates that backoff. The pacing also survives a build that dies *in* startup, which is what a closed gate causes: `Library.run` calls `stop_client()` — setting `stop_thread` — before it sets `startup_done`, so `_recover_threads`'s `startup_done and not stop_thread` reset cannot fire and the ladder is not restarted each lap.

No before/after CPU figure is offered across that boundary: the deploy moved the build and the gate together, so nothing measured across it can be attributed to either alone.

## The follow-up 149 makes available

Jellyfin's `MediaStream` already carries `IsDefault`, `IsForced` and `IsHearingImpaired`, which map one-to-one onto `FLAG_DEFAULT`, `FLAG_FORCED` and `FLAG_HEARING_IMPAIRED`. Writing them would let Kodi's own track selection see what the server knows before the file is ever opened, rather than after the first play.

It is recorded here rather than shipped, per the transplant rule, and it has one dependency worth stating: `iFlags` exists on 149 alone — not on 131, 146, 147 or 148 — so the statement has to differ per gated schema. That is the same second set of `add_stream_*` queries `docs/myvideos148-gate.md` flagged as the first real fork in the transplanted query table, and `iFlags` should ride *with* an `iSource` change if one is ever made rather than fork the table a second time.

## What changed here

* `sync/schema.py` — 149 in `SUPPORTED["video"]`, `EXTRA_ITEM_TYPE[149] = 2`.
* `tests/fixtures/myvideos149.sql` + `_seed.sql` — dumped from the Pi, see above.
* `tests/unit/kodifixtures.py` — `PIERS_VIDEO_VERSION_149`, and the provenance note.
* `tests/unit/test_sync_writers.py` — a fifth L2 leg, `[piers149]`, so every writer invariant (full fidelity, idempotency, zero-orphan removal, extras itemType) is proven against 149.
* `tests/unit/test_downloads_repoint.py` — the same fifth leg.
* `tests/unit/test_sync_schema.py` — 149 passes the gate, and discovery prefers it over three left-behind files.
* `README.md` and `CLAUDE.md` — the supported-schema list.
* `docs/myvideos149-gate.md` — this document.
