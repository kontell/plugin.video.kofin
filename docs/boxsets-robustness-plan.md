# Boxset sync robustness: guard, heal, diagnose

| Field | Value |
|---|---|
| **Date** | 2026-08-03 |
| **Status** | Implemented on `fix/boxsets-healing`; L2/L1-tested and live-verified (S-boxsets, 2026-08-03) |
| **Addon** | `plugin.video.kofin` |
| **Symptom** | Collections drift into an unlinked state (sets exist, `movie.idSet` gone) and stay that way until a manual **Refresh boxsets** |

---

## Overview

Boxset membership lives in three replicas: the server's collection children, kofin.db's `parent_id` rows, and MyVideos' `movie.idSet`. The only reconciliation point is `Movies.boxset()` (`sync/writers/movies.py:411`), and it is gated on the set item's Etag: unchanged Etag ⇒ the whole membership pass is skipped. Membership can change while the set's Etag does not (a member removed and re-added gets a fresh item id and a fresh `movie` row with `idSet` NULL — the set item itself was never touched), so drift, once created, is permanent until a manual refresh.

Worse, the write path can *create* the drift: `boxset_current()` treats "membership query returned zero items" as "unlink every local member", then stamps the new checksum — freezing the damage behind the Etag gate. And no recurring process (prune, divergence probe) covers boxsets at all, so nothing ever notices.

**What we do:** five contained changes — an unlink guard, an Etag-skip health check backed by a tiny `boxset_state` table, a repair path for vanished `sets` rows, a stale-set sweep + summary line on the boxsets walk, and a local-only drift probe at startup that schedules a boxsets pass when state disagrees. No new settings, no new IPC, no checksum-format change.

---

## How it works today (code map)

The writer: `Movies.boxset(item)` (`writers/movies.py:411`) → `check_unchanged` (`fields.py:411`) skips on Etag match **before** any existence or membership check → else upsert the `sets` row → `boxset_current(obj)` (`:459`) loads "current" from kofin.db parent rows, pages the server's members via `get_movies_by_boxset` (`downloader.py:98` — `/Users/{uid}/Items?ParentId=<set>&IncludeItemTypes=Movie&Recursive=true`, plus the `IsMissing`/`LocationTypes` filters), links newcomers, pops survivors → back in `boxset()`, everything left in `obj["Current"]` is unlinked (`:439-453`, `idSet = NULL` + parent cleared) → `add_reference` stamps the checksum (`:456`).

Movie rows never carry the link themselves: `add_movie`/`update_movie` (`kodidb/queries.py:325,624`) exclude `idSet`, so plain updates preserve links and only `boxset_current` ever sets them. A movie **remove + re-add** therefore always lands unlinked.

When the walk runs: `FullSync.process_library("Boxsets:…")` (`full_sync.py:386-411`) → `boxsets()` (`:886`) walks every server set. A `Boxsets:` entry is queued by initial sync of a movies/mixed library, by every **UpdateLibrary** (retention overrun, unapplied-item recovery, divergence probe, settings button), and by **RefreshBoxsets** (`boxsets_reset()` + full re-add). It is *not* run by plain startup fast-sync. Incrementally, BoxSet records reach `movies.boxset()` via the UpdatedWorker (`library.py:1825`); removals dispatch through `removal_writer_for` (`library.py:2012`).

What never covers boxsets: `PRUNE_SERVER_TYPES` (`full_sync.py:43`) has no boxsets entry and the whitelist never contains the boxsets view, so neither the update-mode prune nor `probe_divergence` (`library.py:1371`) ever looks at sets. A set deleted server-side while only tier-2 is available (or Kodi is off past feed retention) is a ghost forever.

---

## Live-verified facts (jelly.konell.xyz, Jellyfin 10.11.11, 2026-08-03)

Verified with a throwaway "Kofin Probe Collection" (created, mutated, deleted; local test profile confirmed litter-free afterwards, 54 sets / 0 drift before and after).

| # | Fact | Evidence |
|---|---|---|
| F1 | `ChildCount` is absent unless requested via `Fields`; kofin's `info()` does **not** yield it (`ItemCounts` is dead on 10.11) | by-id fetch with the real `info()` list: no `ChildCount` |
| F2 | `RecursiveItemCount` — already in `info()` — **is** returned for BoxSets on both the walk and `Ids=` fetches; equals membership for movie-only sets but recurses into series (1 movie + 1 series with 2 seasons/3 episodes ⇒ 7) | probe collection readings |
| F3 | `ChildCount` counts *direct* children of any type: mixed probe set read `ChildCount: 2` while the Movie-typed membership query returned 1 | probe collection |
| F4 | Set Etag changes on every explicit `/Collections/{id}/Items` add/remove (`1f13…` → `e78c…` → `9c70…`), and is **not** user-dependent (admin and kofin-test read identical Etags) | probe collection |
| F5 | Membership query against a nonexistent **or deleted** ParentId fails loudly: HTTP 400 "Error processing request", so `_get_items` raises and today's code aborts *before* any unlink. Mass-unlink therefore requires a 200-with-zero-items answer (permission filtering, filter-hidden members, or a genuinely emptied set) | bogus GUID and deleted probe id both 400 |
| F6 | `ChildCount` cost is negligible here: the full 54-set walk with `info()+ChildCount` measured ≤ the walk without it (~0.9s both); a 100-series page showed no measurable delta either. We still scope it to the BoxSet queries only (the `RecursiveItemCount`-on-albums lesson: per-item count fields have bitten before) | curl timings |
| F7 | Collection create/delete arrives as one coalesced `LibraryChanged` (the probe id appeared in `ItemsAdded` *and* `ItemsRemoved` of the same message, alongside ~280 unrelated `ItemsUpdated`); kofin processed it without leaving rows behind | kodi.log 17:35:25 |

The suspected real-world trigger for mass drift: storage offline during a server scan ⇒ members deleted ⇒ re-added on the next scan with **new item ids** ⇒ collections relink server-side by path with the set item untouched ⇒ every local `movie` row is fresh (`idSet` NULL) and every set Etag is unchanged ⇒ nothing ever relinks. The S2.2 A/B run already caught this id-churn in the wild ("one movie the server replaced with a 4K encode — old item 404s"). The server-side relink-by-path leg was not exercised live (would mean deleting real library items); the local mechanics are code-proven and L2-provable.

---

## Failure map

| # | Vector | Mechanism | Today | Fixed by |
|---|---|---|---|---|
| V1 | Member removed + re-added (file replaced, storage flap, repair) | new `movie` row has no `idSet`; set Etag unchanged ⇒ walk skips forever | permanent drift, invisible | P2 heal (+P4 probe to schedule it) |
| V2 | Membership query answers 200 with 0 items while the set has children (permission/filter edge) | `Current` leftovers = all members ⇒ mass unlink, checksum stamped ⇒ frozen | permanent empty set | P1 guard |
| V3 | Membership query fails (deleted set, server error) | `_get_items` raises before the unlink loop | already safe (F5) — keep the raise load-bearing | — (documented) |
| V4 | Kodi `sets` row vanishes (Kodi's clean-library drops memberless sets; DB damage) | Etag matches ⇒ skip before the existence check; even on Etag change, `update_boxset` UPDATEs a missing row silently and links point at a dead id | set gone / links dangling | P2 health check + R3 repair |
| V5 | Set deleted server-side with no feed record (tier 2, retention gap) | walk only upserts; prune/probe exclude boxsets | ghost set forever | P3 sweep |
| V6 | Emptied set repopulated later without Etag movement (permission flap restores) | empty state was stamped as clean | stuck empty | P2 zero-members-never-stamp rule |
| V7 | Member shared by two sets (a movie in several server collections) — found 2026-08-06 | `movie.idSet` is single-valued, so each walk's later pass steals the member *after* the earlier set stamped its count ⇒ stored ≠ current the moment the walk ends | permanent probe→walk cycle, warning every start | walk-end restamp (docs/healing-loops-plan.md F1) |

---

## Design

### P1 — the unlink guard (writer)

`boxset_current` counts what the server actually yielded (`fetched`); `boxset()` reads the DTO's server-children signal: `ChildCount` when present, else `RecursiveItemCount` (F2), else unknown.

Decision at the unlink point: if there are leftovers to unlink **and** `fetched == 0` **and** the children signal is unknown or > 0 ⇒ skip the unlink loop, skip the checksum stamp and the `boxset_state` stamp, and emit **one** `LOG.warning` naming the set, its id, the local link count, and both server counts ("keeping N local link(s); a permissions or filter change can cause this; Refresh boxsets forces a relink"). Nothing else about the pass happens — the old checksum stays, so an Etag-changed set retries (and re-warns) on every walk until the server answers sanely, while an Etag-unchanged heal attempt goes back to sleep.

If `fetched == 0` and the children signal is 0, the set was genuinely emptied: unlink as today, but say so in one `LOG.info` ("boxset X emptied server-side; unlinking N") rather than a warning — an expected change is not an alarm. (This reads review bullet 1's "or the membership query returned 0 items" as "suspicious unlink"; a confirmed-empty set is not suspicious, but it is now visible.) `fetched > 0` is a normal diff — partial pages cannot reach here because page failures raise (F5), so a completed loop with leftovers is the server's true answer.

### P2 — Etag-unchanged is not membership-healthy (writer + kofin.db)

New table in kofin.db (`db.py`, additive `CREATE TABLE IF NOT EXISTS`): `boxset_state(jellyfin_id TEXT PRIMARY KEY, linked_count INTEGER NOT NULL)` — the MyVideos link count measured at the end of the set's last successful membership pass. No timestamps, so the L2 byte-identical-dump invariant holds. Accessors in `kofindb.py`; rows die with the set (`remove()` set leg, `boxsets_reset()`).

`boxset()` only honors the `check_unchanged` skip when the set passes a local health check, all three legs answered by indexed local queries: the Kodi `sets` row exists; `COUNT(movie WHERE idSet = ?)` equals the stored `linked_count` (a missing state row — every set, immediately after upgrade — counts as unhealthy, which is the deliberate one-time relink migration); and the kofin.db parent count agrees with the MyVideos count. Additionally, per review bullet 2 verbatim: zero local members while the DTO's children signal is > 0 is unhealthy regardless of state. Unhealthy ⇒ one `LOG.info` "healing boxset …" and the full pass runs despite the matching Etag.

Healing must not trust kofin.db's "current" map (a set can be locally damaged in MyVideos while kofin.db still claims the link — then the normal pass would pop the member as already-current and fix nothing). So a forced pass runs `boxset_current` in force mode: every fetched member gets the `set_boxset` UPDATE and `update_parent_id` write whether or not it appears current — idempotent single-row UPDATEs, cheap at set scale. The normal (Etag-changed) path keeps fork semantics untouched.

After any successful membership pass, stamp `boxset_state` with the *measured* result (`COUNT(movie WHERE idSet = ?)` again — exact, self-consistent). **Exception (V6): a pass that ends with zero linked members stamps state but never stamps the reference checksum**, so an emptied set is re-verified on every walk (one count query each — negligible) and springs back the moment the server shows members again, Etag or no Etag.

R3 repair, same breath: the update-vs-add fork in `boxset()` stops keying on `e_item` alone — if the reference exists but the `sets` row is gone, log the repair (mirroring the movie leg's "missing from kodi. repairing the entry.") and take the add leg, then force-relink. Needs one new tiny query (`SELECT idSet FROM sets WHERE idSet = ?`) in `kodidb/movies.py` + `queries.py` — schema-neutral across all gated versions.

### P3 — walk summary and stale-set sweep (`full_sync.boxsets`)

The walk gains one summary line at INFO: `boxsets: N checked — X unchanged, Y written, Z healed, G guarded, S swept`. `refresh_boxsets` logs set/link totals before and after.

Sweep: the walk already pages every server set; collect the walked ids and, when the walk started fresh (no restore point at entry) and completed, remove kofin.db set references the server no longer lists (`Movies.remove` per id — the same writer path tier-1 Removed records use). Same conservatism as P1: if the walk yielded zero sets while kofin.db has any, skip the sweep and warn — an empty listing is not a deletion order (mirrors the prune's `get_existing_ids` philosophy and the `get_id_etag_map` truncation guard).

`ChildCount` is added to the walk's `Fields` only (F1/F6): `boxsets()` passes `params={"Fields": info() + ",ChildCount"}`; the incremental path keeps `info()` and falls back to `RecursiveItemCount`, which it already carries (F2).

### P4 — startup drift probe (`library.py`, beside `probe_divergence`)

Pure-local, zero server traffic, runs on the same startup tick as the divergence probe (and under its same `sync_allowed_now`/pending-work gates): join kofin.db set rows against `boxset_state` and a single `GROUP BY idSet` count over MyVideos. Any set with a missing `sets` row, missing state, or `stored != current` ⇒ one warning naming up to a handful of sets, then `add_library("Boxsets:")` — the existing targeted entry that runs just the boxsets walk, where P2 heals exactly the drifted sets and Etag-matching healthy sets stay skipped. Convergence: healed sets stamp fresh state; guarded sets kept their links so `stored == current`; unsyncable members count into neither side — no probe→walk→probe loop is possible by construction.

This is what turns "requires a manual boxsets update" into "self-heals on the next service start": V1 drift is caught by the count mismatch even though every Etag matches.

(2026-08-06: the convergence argument above missed members shared between sets — V7. `movie.idSet` is single-valued, so the walk itself re-creates the count mismatch it heals, and the probe re-fires every start. The walk-end restamp — docs/healing-loops-plan.md F1 — is what actually closes it; guarded sets stay excluded so their designed retry survives.)

### P5 — diagnosability polish

Per-member unlink stays DEBUG but the aggregate ("unlinking N of M from '<set>'") logs at INFO before the loop. `boxset_current`'s per-member "Failed to process %s to boxset." becomes a per-set counted DEBUG ("K member(s) not in any synced library") — it fires routinely for members outside the whitelist and currently reads like an error while hiding the set name. The audit SQL used to diagnose a live install goes into the testing plan alongside the new gates (sets with zero members; kofin.db vs MyVideos link-count disagreements — both queries written and exercised during this review; today's local install: 54 sets, 0 drift on either axis).

### Deliberate non-goals and accepted trades

No checksum-format change: `stored_checksum_matches` (`changefeed.py:312`) and tier-1 skip-before-download behavior are untouched; membership expectation lives in its own table. No per-set server queries on healthy passes — the health check is local; the membership query is still paid only by sets that are new, changed, healing, or empty-pending. No automatic `boxsets_reset`: the reset remains the manual nuclear option; healing never deletes a set to fix it.

Accepted trade: a mixed set whose movies were all legitimately removed (leaving only series) answers `fetched == 0, ChildCount > 0` — the guard keeps the stale movie links and warns instead of unlinking (F3 makes the cases indistinguishable by counts). Rare, harmless (a stale grouping), self-describing in the log, and Refresh boxsets clears it. Residual hole: a permission flap where `ChildCount` is also filtered to 0 unlinks "correctly" for that user's view; the zero-members-never-stamp rule (V6) is what relinks everything when access returns.

Transplant boundary: the changes live in the `boxset` legs of `writers/movies.py` plus shell hooks — deliberate, documented deviations (this doc + in-place comments), each proven by new L2 cases below. The movie/tvshow/music legs and `check_unchanged` itself are untouched, so the S2.2 A/B equivalence claim is unaffected outside boxsets.

---

## Test plan

L2 (`test_sync_writers.py`, all three schema ids, real pristine DBs, FakeApi's `boxset_children` + DTO `ChildCount` driving the server side):

- **Heal after member re-add (V1):** link two movies; `remove()` + rewrite one (fresh row, no `idSet`); `boxset()` with the *same* Etag ⇒ both linked again, `boxset_state` correct.
- **Guard blocks a suspicious empty (V2):** populated set, `boxset_children` empty, DTO `ChildCount=2`, changed Etag ⇒ links intact, reference checksum *not* advanced, state untouched, exactly one warning.
- **Confirmed-empty unlinks (and stays pending):** `ChildCount=0`, empty children ⇒ all unlinked, state 0, checksum not stamped; a second pass re-runs membership (no skip) and is byte-identical.
- **Sets-row repair (V4):** delete the `sets` row under a matching Etag ⇒ health check fails, add leg recreates the set, force mode relinks every member.
- **No heal-loop on never-movie sets:** state 0 == local 0 with `ChildCount>0` and no leftovers ⇒ no warning, no churn.
- **Idempotency + removal invariants extended:** healed second write dumps byte-identical; set removal drops the `boxset_state` row; `boxsets_reset` clears all state.
- **Sweep (V5, full_sync level):** two referenced sets, server lists one ⇒ other removed with zero orphans; server lists zero while local has sets ⇒ nothing removed, one warning.

L1: the health predicate and guard decision as table-driven units; the startup drift probe scheduling (fakes: drifted state ⇒ `Boxsets:` enqueued once; healthy ⇒ silence).

Live gates (test profile, add to `docs/testing-plan.md` as S-boxsets): (1) manufacture V1 with Kodi stopped (NULL one member's `idSet`, delete its kofin.db parent, delete its `boxset_state` — or simply delete the state row to simulate upgrade) ⇒ start Kodi ⇒ probe warns and schedules ⇒ walk heals ⇒ audit SQL clean, summary line shows `healed:1`; (2) plant a fake orphan set row ⇒ UpdateLibrary ⇒ sweep removes it; (3) Refresh boxsets button unchanged end-to-end; (4) post-upgrade first walk stamps state for all 54 sets and the second walk reports `54 unchanged`.

---

## Sizing and rollout

Roughly: `writers/movies.py` +90 (guard, health, force mode, repair, comments), `kofindb.py`/`queries_map.py` +30, `db.py` +3, `kodidb/movies.py`+`queries.py` +8, `full_sync.py` +45 (summary, sweep, walk fields), `library.py` +55 (probe), tests +350. No settings, strings, or IPC changes.

One branch (`fix/boxsets-healing`), two commits: writer+state+repair with L2 first, then sweep+probe+summary. Live gates on the kofin-test profile before merge; testing-plan.md gains S-boxsets with evidence; CLAUDE.md's constraints list gains one line (zero-member passes must not stamp the set checksum — that rule is what makes permission flaps recoverable, and it is easy to "optimize" away).
