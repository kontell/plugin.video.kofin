# Opening the schema gate for MyVideos148

| Field | Value |
|---|---|
| **Date** | 2026-08-16 |
| **Status** | Gate open — `SUPPORTED["video"] = {131, 146, 147, 148}` |
| **Upstream** | xbmc/xbmc `bbfcf50529` (2026-08-14), the 147 side being `b55ac64adc` (2026-08-09) |
| **Bump commits** | `26e3bab56b` — "[Video] Track source of streamdetails." (adds `iSource`, bumps 147→148) and `e9530c9959` — "[Video] Add StreamDetails version to database." (adds `iVersion` inside the same 148 block) |

---

## What 148 is

Unlike 147, **148 moves DDL**. `docs/myvideos147-gate.md`'s reasoning — reuse the previous fixture because the bump is data-only — does not apply here and must not be reached for by analogy.

The whole migration is two additive columns:

```cpp
  if (iVersion < 148)
  {
    m_pDS->exec("ALTER TABLE streamdetails ADD iSource INTEGER DEFAULT 40");
    m_pDS->exec("ALTER TABLE streamdetails ADD iVersion INTEGER DEFAULT 1");
  }
```

Both landed on 2026-08-10 from one PR, under a single schema number. `CreateTables` gained the same two columns inline, so a freshly created 148 and a migrated one converge on the same shape.

`iSource` records **where a stream detail came from**, as a precedence ladder (`xbmc/utils/StreamDetails.h`), and the comment in the enum is the operative part: higher wins.

| Value | Source |
|---|---|
| 0 | `UNDEFINED` |
| 10 | `EXTERNAL` — set for anything a Python add-on writes via `InfoTagVideo` |
| 20 | `MEDIA` — probed by VideoPlayer/DVDFileInfo |
| 30 | `NFO` |
| 40 | `LEGACY` — what the migration stamps on every pre-existing row |

`iVersion` is `CStreamDetail::STREAM_DETAILS_VERSION`, currently 2. It is stored, serialised and exposed through JSON, but no code yet branches on it; it is groundwork.

## What else was checked

Mechanically, `b55ac64adc` (147) against `bbfcf50529` (master), the same table `docs/myvideos147-gate.md` used:

| Check | Result |
|---|---|
| `VideoDatabaseDDL.cpp` | the `streamdetails` create, **and nothing else** — the only other hunk is a dropped `#include` |
| `CreateAnalytics` (views, triggers, indices) | unchanged |
| `VideoDatabaseColumns.h` | unchanged — no `VIDEODB_ID_*` ordinal moved |
| `VideoManagerTypes.h` (`VideoAssetType`) | unchanged — `VERSION = 1`, `EXTRA = 2` |
| `CMusicDatabase::GetSchemaVersion()` | still **84** |
| `CTextureDatabase::GetSchemaVersion()` | still **14** |

So `EXTRA_ITEM_TYPE[148] = 2`, and the music and texture fixtures stand untouched.

## The fixture is a real dump

`tests/fixtures/myvideos148.sql` was dumped (`sqlite3 -readonly … .schema`) from a pristine Kodi 22.0-BETA1 (21.90.801) on LibreELEC 13.0 `nightly-20260816`, a Raspberry Pi 3B test box — 0 movies, 0 episodes, 0 files, 0 paths. `myvideos148_seed.sql` is its 387 `videoversiontype` rows, **identical to 147's**.

That database was **created** at 148, not migrated up. The proof is in the dump text: sqlite rewrites a table's stored `CREATE` when `ALTER TABLE ADD COLUMN` runs, so a migrated `streamdetails` would end `iSource INTEGER DEFAULT 40, iVersion INTEGER DEFAULT 1` — carrying the migration's own capitalisation and defaults. The dump instead reads `iSource integer, iVersion integer`, which is `CreateTables` verbatim.

**One difference is not a 148 change.** The dump quotes `sets` with backticks where the 146/147 fixtures do not. That is upstream `80e77713eb` (2026-04-21), "Quote MySQL 9.6 reserved keyword `sets` with backticks", landing in `CreateTables` after the Bravia dumps that produced the older fixtures. Any 147 install built since April has it too. sqlite treats the two spellings identically, so it changes nothing here beyond making `cmp` against 147 report two hunks rather than one.

As a cross-check on the *existing* fixtures, the same box's `MyMusic84.db` was dumped and is byte-identical to the checked-in `tests/fixtures/mymusic84.sql`.

## Impact on kofin

**None, and specifically none on playback.**

* **The writers need no change.** Every `streamdetails` statement in `sync/kodidb/queries.py` names its columns (`add_stream_video`, `add_stream_audio`, `add_stream_sub`), and both readers — `get_video_duration` and `service/player.py`'s duration probe — select `iVideoDuration` by name. Two additive columns are exactly the case the explicit-column-list rule exists for. The `[piers148]` L2 leg proves it rather than asserting it.
* **`EXTRA_ITEM_TYPE[148] = 2`**, unchanged from 146/147, because the asset-type enum did not move. The extras pass keeps reading the VERSION itemType from the seeded `40400` row.
* **kofin's rows land at `iSource` NULL**, which `CVideoDatabase::GetStreamDetails` reads back as `UNDEFINED` (0) via `get_asInt()`. That is the bottom of the ladder, so `CStreamDetails::ShouldUpdateWithNewDetails` returns true against any incoming detail, and `SaveFileStateJob` will overwrite kofin's server-derived stream details with whatever the player observed when the two differ.

  **This is not a regression, and it is why no writer change ships here.** On 147 and earlier that same overwrite was *unconditional* on any difference; 148 added `ShouldUpdateWithNewDetails` as a new guard that can only ever suppress writes. A NULL `iSource` therefore reproduces 147 behaviour exactly.

## The follow-up 148 makes available

Writing `iSource` deliberately is now possible and would be an *improvement* over 147 behaviour rather than a repair — which is why it is recorded here instead of being smuggled into a gate bump, per the transplant rule in `CLAUDE.md`.

The prize is real: a Jellyfin stream may be transcoded, so the codec, resolution and channel count VideoPlayer observes can describe the *transcode* rather than the source file. Today, playing such an item can rewrite the library row to the transcode's properties. Stamping kofin's rows above `MEDIA` (20) would make them stick.

What needs deciding before that is written:

* **Which value.** `NFO` (30) is the honest provenance — details came from a metadata server, not from probing the file locally — and still loses to a genuinely better local probe. `LEGACY` (40) would make kofin's rows unoverwritable by anything, which is a stronger claim than the data supports.
* **It must be version-keyed**, like `EXTRA_ITEM_TYPE`: the column does not exist on 131/146/147, so the statement has to differ per gated schema. That means a second set of `add_stream_*` queries, which is the first real fork in the transplanted query table.
* **Bench it before believing it.** The transcode-overwrite path should be reproduced on a real 148 install first — it is a claim about Kodi behaviour, and this document has not run it.

## What changed here

* `sync/schema.py` — 148 in `SUPPORTED["video"]`, `EXTRA_ITEM_TYPE[148] = 2`.
* `tests/fixtures/myvideos148.sql` + `_seed.sql` — dumped from the Pi, see above.
* `tests/unit/kodifixtures.py` — `PIERS_VIDEO_VERSION_148`, and the provenance note distinguishing this real dump from 147's copied one.
* `tests/unit/test_sync_writers.py` — a fourth L2 leg, `[piers148]`, so every writer invariant (full fidelity, idempotency, zero-orphan removal, extras itemType) is proven against 148: 100 tests.
* `tests/unit/test_sync_schema.py` — 148 passes the gate, and discovery prefers it over both left-behind files.
