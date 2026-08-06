# Healing-loop convergence: closing the probe→heal→probe cycles

| Field | Value |
|---|---|
| **Date** | 2026-08-06 |
| **Status** | Planned — not started |
| **Addon** | `plugin.video.kofin` |
| **Symptom** | Startup probes re-schedule the same heal forever for two library shapes that exist in the wild (a movie in two collections; a series pooled across libraries); recovery retries have floors but no ceilings; three latent rewrite loops sit one code change from live |

---

## Overview

The 2026-08-06 healing-loop audit found that the addon's reconcilers are individually well-guarded but two of them can never converge, because **the heal path does not re-stamp the expectation on the axis the detector reads**: `boxset_state` is stamped per-set mid-walk while later passes move the counted rows, and a pool reference's `media_folder` is stamped once at creation and never corrected by any update leg. Each produces a permanent per-boot probe firing plus a scheduled heal that structurally cannot close the gap. A third loop — a permanently-unwritable item re-arming an hourly `UpdateLibrary` — is acknowledged in-code (`library.py:82-84`) and rate-limited but uncapped. Three more loops are latent, held shut by invariants that live in comments: the userdata echo cycle terminates only because SQLite writes bypass Kodi's announcer, the reference checksum format is spelled in three places that agree only while `direct_path` stays `False`, and an Etag-less DTO would stamp an unmatchable `json.dumps(UserData)` checksum.

**What we do:** five contained fixes — a walk-end `boxset_state` restamp, pool references as library-neutral placeholders with a re-home path, escalating ceilings on the two uncapped retries, one spelling of the reference checksum with an Etag-presence guard, and a tolerant `node_index`. No new settings, no new IPC, no schema-gate changes; one additive kofin.db-free query in `queries_map.py`.

Evidence map (all findings verified in source during the audit):

| # | Loop | Mechanism | Frequency | Fixed by |
|---|---|---|---|---|
| L1 | Boxset shared member | `movie.idSet`/`parent_id` are single-valued; each set's pass steals the shared movie (`writers/movies.py:644-649`) after the earlier set stamped its count (`:562-563`); probe (`library.py:1764`) sees the loser drifted | every service start + every boxsets walk, forever | F1 |
| L2 | Pooled series | pool row stamps the pooling library's `media_folder` (`queries_map.py:157-168`); `update_reference` sets checksum only (`queries_map.py:242-247`); `local_reference_map` (`full_sync.py:121-145`) and `probe_divergence` (`library.py:1682`) misattribute it forever | `UpdateLibrary` every boot + spared-warning and refetch every prune | F2 |
| L3 | Poison item / failing library | `flag_unapplied` → `UpdateLibrary` hourly with a floor but no cap (`library.py:84,1629-1666`); a non-404 library failure retries 60s→30min with a toast per attempt, backoff reset each boot (`full_sync.py:458-466`, `library.py:1890-1897`) | hourly / per-backoff, forever | F3 |
| L4 | Checksum spellings (latent) | `fields.sync_checksum` vs `"|plugin"` literals in `changefeed.py:315` and `full_sync.py:826`; Etag-less fallback stamps `json.dumps(UserData)` (`obj.py:119`) that no comparator matches | dormant; any `direct_path` flip or custom-`Fields` caller makes every affected item permanently "changed" | F4 |
| L5 | Node regeneration (latent) | `node_index` raises `ValueError` when a whitelisted view is missing from `SortedViews` (`views.py:584`) before the `viewsHash` stamp (`:426`); `SortedViews` flips on a transient `/Library/MediaFolders` 403 (`:255-266`) | full tree rewrite per startup and per library command while the precondition holds | F5 |
| — | Userdata echo (latent) | no value-equality guard on the apply path; cycle terminates only because SQLite writes raise no Kodi announcement (`service/kodiuserdata.py:18-19`) | dormant; any JSON-RPC library write re-opens it | F4 (constraint only) |

---

## F1 — boxsets: walk-end restamp

The root defect is stamp timing, not the steal: last-wins ownership of a shared member is the fork's semantic and stays, but the probe's expectation must be measured **after the walk stops moving rows**, not per-set mid-walk.

New writer method `Movies.restamp_boxset_states(guarded_ids)`: one pass over kofin.db's set references (`get_item_ids_by_media("set")`, `kofindb.py:192`) against one `GROUP BY idSet` count (`get_boxset_movie_counts`, `kodidb/movies.py:286`), stamping each via `add_boxset_state` (`kofindb.py:90`) with `counts.get(kodi_id, 0)` — the exact queries the probe reads (`library.py:1810-1824`), so expectation and measurement are the same computation by construction. Called at the end of `full_sync.boxsets()` after the sweep, before the summary line; it runs whether or not the walk was resumed, because it is measurement, not deletion (the sweep's fresh-start gate does not apply). The per-set stamp at `writers/movies.py:562-563` stays — it is what makes an *interrupted* walk resume with sane state; the walk-end restamp is the convergence point.

**Guarded sets are excluded from the restamp.** A set that returned `BOXSET_GUARDED` deliberately keeps a `None`/stale state so the membership query retries on every walk until the server answers sanely (boxsets-robustness-plan P1); restamping it would grade the suspicious answer as healthy and silence the designed retry. The walk already collects outcomes for its summary line — it passes the guarded ids to the restamp as the exclusion set.

V6 is unaffected: an emptied set stamps state 0 in its own pass, the restamp re-stamps 0, and the NULL reference checksum still forces per-walk re-verification. The zero-members-never-stamp rule is untouched.

Convergence argument, replacing the one in `probe_boxset_drift`'s docstring: after a completed walk, stored equals post-walk reality for every non-guarded set, *including both sides of every shared-member steal*. An incremental Etag-driven pass on one overlapped set can still steal mid-cycle and drift the other set's stored count — that costs exactly one probe firing and one walk, whose restamp re-closes it. Cycles are now bounded at one walk per external disturbance instead of one walk per boot forever.

Docs ride along: boxsets-robustness-plan.md gains failure-map row **V7 — member shared by two sets** pointing here; CLAUDE.md's boxsets bullet gains the invariant that the walk-end restamp (guarded sets excluded) is what closes shared-member drift, so a future stamp-site change keeps the exclusion.

---

## F2 — pooled series: neutral placeholder, re-home on contact

Three legs, ordered so each is independently useful.

**P0 (answered 2026-08-06, design refined accordingly):** `obj["LibraryId"]` on the update leg is *inherited from the stored reference* (`e_item[6]`, `writers/tvshows.py:81`) — which is exactly why wrong attribution was sticky — while the add leg resolves `self.library or find_library(...)` (an `/Items/{id}/Ancestors` walk that already exists with per-parent memoization, `fields.py:507`). So the incremental path *can* resolve, and P2 below runs on the update leg itself. Two hazards surfaced: `get_view_name` crashes on a missing row (`fetchone()[0]`), so a NULL-folder reference resolved inside the old `try` would TypeError into the **add leg** and create a duplicate show — the leg is restructured so the view name resolves only after the folder is known non-NULL; and `RemoveLibrary` selects references by `media_folder`, so a NULL placeholder would have dangled after its shared Kodi row died — resolved by dropping pool siblings with the row via the existing `delete_item_alias_by_kodi` query (the season-alias machinery), matching the season cascade which already deletes all references by kodi_id. A sibling series still live on the server returns as a *missing id* on its own library's next pass, never a dangling reference the prune reads as synced. (Observed, deferred: the episode-removal cascade's show-drop resolves one reference by kodi_id and would still strand a sibling — same one-line fix if it ever matters.)

**P1:** `add_reference_pool_obj` stamps `media_folder` NULL (`queries_map.py:166`, one line). A pool row is a placeholder for a series another library owns, not an attribution — with NULL, the pooling library's `local_reference_map` stops counting it, which alone ends the pooling-side divergence (`probe_divergence` +1 every boot) and the prune's stale/spared warning for the common case where the pooled series' own library is never synced.

**P2:** the tvshow update leg re-asserts `media_folder` when the writer has library context: additive `update_reference_media_folder` query in `queries_map.py` (`SET media_folder = ? WHERE jellyfin_id = ?`), called from `tvshow_update` with the walking library's id. A full walk of the series' own library then adopts the row; whether the prune's missing-arm downloads (which route through the incremental pipeline) can also assert it is P0's answer. The blanket alternative — extending `update_reference` itself — is rejected: seasons and episodes carry NULL `media_folder` by design and every updater's obj list across the transplant would need changing.

**P3:** the spared arm heals instead of warning forever. `full_sync.prune` already confirms spared ids exist server-side (`full_sync.py:854-859`); for each, `_rehome_spared` resolves its true library through the same `find_library` the writers use — one spelling of "which library owns an item" — then restamps `media_folder` to the whitelisted ancestor view, or NULL when no synced library owns it. Seasons and episodes are exempt (they carry no `media_folder` by design; their fate follows their series — a pooled show's seasons can land in the spared set via the kodi-parent chain, and stamping them would break that invariant). This converts the permanent `"kofin bugs"` warning into a converging heal, and it is also the **existing-install migration**: poisoned rows surface as spared on the next `UpdateLibrary` and get re-homed — no upgrade pass, no state-table change. A resolution failure skips the id and the next prune retries.

---

## F3 — retry ceilings

**P1 — escalating recovery floor.** (Implementation finding, 2026-08-06: the audited "hourly forever" loop was actually worse in a different way — the failed recovery's own drain always settles *inside* the floor, where `schedule_recovery_prune` consumed the flags and scheduled nothing, so a poison item was retried exactly once and then went silent until unrelated feed activity touched it. The fix therefore restores retrying *and* bounds it.) A failure inside the floor now books the retry (`recovery_pending`) for a tick hook (`flush_recovery_prune`, beside `flush_refresh_settle`) that fires it when the floor passes; each fired attempt stamps the next floor from the current ladder rung and climbs it (`AUTO_PRUNE_MIN_SECONDS` doubling to `AUTO_PRUNE_MAX_SECONDS` = 24h); a clean drain resets the ladder only when no retry is owed, so unrelated drains mid-backoff keep the clock. In-memory on the Library manager — a boot forgets, costing at most one immediate retry per restart, against the cost of a persistent attempt table (new kofin.db surface for a rate problem).

**P2 — failing-library toast dedup.** The per-attempt error toast (`full_sync.py:459`) fires once per library per service lifetime; later attempts log only. The dedup set lives on the Library manager, not on `FullSync` — FullSync is a Borg and per-run state there is exactly the trap its docstring warns about (`full_sync.py:85-90`).

**P3 — the non-exit `LibraryException` drop.** `process_library` swallowed any non-exit `LibraryException` and returned success, removing the entry (the in-file TODO at `full_sync.py:449-457`). Taxonomy verified before changing it: per-item conditions (`LibraryOrphanException`, items deleted mid-page) are absorbed one level down in `apply_or_skip` on every walk, and on the incremental path by the workers' own handler that feeds `flag_unapplied` — so what reaches the `process_library` handler is a pass-level failure, notably the prune-map truncation guard (`downloader.py:346`), a designed fail-loud check this very handler was silently defusing. Non-exit `LibraryException`s now fail like any other error: toast (deduped), log, entry stays queued, resume backoff owns the retry.

**P4 (optional, decide after P1 lands).** The retention-repair latch sticks forever if its `UpdateLibrary` fails (`library.py:699-708` clears only on success; `:1564` blocks re-enqueue): clear it on command failure so the next overrun detection re-arms, behind the same escalating-floor pattern. Trades "stuck but silent" for "retrying but bounded"; take it only if the P1 pattern reads well.

Untouched on purpose: the 404 poison-drop (`full_sync.py:420-432`) and the resume backoff shape — both are correct convergence rules.

---

## F4 — one checksum spelling, Etag or NULL

**P1:** `fields.reference_checksum(etag)` returns `"<etag>|plugin"`; `sync_checksum` delegates to it (keeping the `direct` arm in one place); `changefeed.stored_checksum_matches` (`changefeed.py:312-315`) and the prune's inline literal (`full_sync.py:826`) both import it. One L1 asserts the writer-stamped value equals both comparators' expectation for the same Etag — the test that fails the day someone resurrects `direct_path`.

**P2:** the mapping fallback `mapped_item["Checksum"] = json.dumps(item["UserData"])` (`obj.py:119`) becomes `None`, and `fields.py:450` stops falling back to it (`obj["Checksum"] = sync_checksum(item, writer.direct_path)`, unconditionally). A reference written with a NULL checksum already has defined semantics — re-verify every walk (the boxset V6 rule) — which is stable and greppable, unlike a UserData dump that moves on every playback and can never match. `add_reference` warns when handed NULL for a non-BoxSet type, naming the item, so the offending caller's field list is one grep away; BoxSet NULL is legitimate and exempt; `update_reference` warns on any NULL (boxsets stamp through `add_reference` only). **P0 answered 2026-08-06:** the only `obj["Checksum"]` assignment sites are `fields.py:450`, the season writer's own `sync_checksum` stamp, and the boxset V6 NULL — nothing reads the mapped default before the overwrite; and `fields.artwork_only` already guards its `update_reference` behind `if checksum:` so the image-only path cannot write NULL.

**P3:** an L1 guard asserting `"Etag"` is present in every pipeline field list (`downloader.info`, `basic_info`, `music_info`, `music_page_info`) — the regression source for the fallback ever becoming reachable.

**P4:** two new CLAUDE.md constraint bullets: never write Kodi's library through JSON-RPC — the userdata echo cycle terminates only because direct SQLite writes raise no announcement (`service/kodiuserdata.py:18-19`), and an announcer-visible write turns it into an infinite ping-pong; and reference checksums have exactly one spelling, `fields.reference_checksum` — never inline `"|plugin"`.

---

## F5 — node generation resilience

**P1:** `node_index` stops assuming membership: a whitelisted view missing from `SortedViews` gets an append-at-end order (`len(SortedViews)` plus a stable tiebreak on view id) and a DEBUG line, instead of `ValueError` (`views.py:584`). This removes the only known deterministic crasher between `get_nodes()` entry and the `viewsHash` stamp, which is what turned a transient server degradation into a full tree rewrite per startup and per library command.

**P2 (deferred 2026-08-06):** stable ordering under degradation: when `/Library/MediaFolders` fails and the code degrades to views-only (`views.py:255-266`), reuse the previous `SortedViews` order for ids still present and append newcomers, rather than adopting the alternate endpoint's order wholesale — a transient 403 then no longer regenerates the tree twice per flip. Needs a check that `SortedViews` consumers tolerate retained ids the degraded answer lacked. Deferred with P1 shipped: the crash loop is closed, and the residual is bounded churn per flip.

**Adjacent risk observed while verifying P1's reachability (recorded, not fixed here):** `get_views` fires a `REMOVE_LIBRARY` IPC for every kofin.db view absent from the freshly-stamped `SortedViews` (`views.py:301-306`). Under the same views-only degradation, a whitelisted library that is a media folder but not a user view would be missing from the degraded answer — making a transient 403 a potential library-removal trigger. Whether that combination occurs in real deployments needs its own verification before touching the removal path; it belongs with P2 if taken up.

Deliberate non-change: `viewsHash` stays stamped-last-and-not-in-`finally` — a failed generation must retry, and P1 removes the known deterministic failure inside the try.

---

## Deliberate non-goals

No deterministic ownership for shared boxset members: last-wins is the fork's semantic inside the transplant, and it becomes stable once expectations converge (same walk order, same winner). No poison-quarantine table in kofin.db: the escalating floor bounds the damage without new persisted state; revisit only if a real install shows the 24h cap still too chatty. The widget fingerprint gate, its deliberate omissions, and the wake/reconnect FastSync behavior are untouched — they are working dampers, and re-gating them is exactly what CLAUDE.md warns against. The library thread's fail-stop on an unhandled tick exception (`library.py:454-457`) stays: supervised restart is a lifecycle decision, not a loop fix. Websocket reconnect backoff is out of scope (bounded churn, separate discussion).

---

## Test plan

L2 (`test_sync_writers.py`, all three schema ids, plus full_sync-level cases in the style of the boxset sweep tests):

- **Shared member (L1/V7):** two sets sharing one movie; full boxsets walk ⇒ the probe predicate is false for both sets; a second walk reports all-unchanged and dumps byte-identical.
- **Incremental steal converges:** from a converged state, run `boxset(A)` alone with a changed Etag (steals the shared movie) ⇒ one full walk re-closes both states; a third walk is a no-op.
- **Guarded exclusion:** a set guarded on its first pass keeps `stored = None` after the walk ⇒ the probe still flags it (the designed retry survives the restamp).
- **Pool two-order:** FakeApi grows `get_seasons` with a foreign `SeriesId`; sync library A then B, and B then A ⇒ in both orders the reference ends attributed to the owning library (or NULL when unsynced), a second prune classifies nothing missing/stale/spared, and `probe_divergence`'s counts match.
- **Spared re-home:** a reference misattributed to library A whose id the server resolves under B ⇒ prune re-homes instead of warning; with B unwhitelisted it stamps NULL.
- Existing idempotency and zero-orphan invariants keep passing untouched.

L1: the escalating floor as a table-driven unit (fail→3600→7200→…→86400 cap→reset-on-clean); the `LibraryException` keep-entry change; `reference_checksum` equality across writer/changefeed/prune; the field-list Etag guard; `node_index` fallback ordering; the NULL-checksum warning exempting BoxSet.

Live gates (test profile, added to `docs/testing-plan.md` as S-loops): (1) create an overlap collection on the test server (one movie in two collections), full sync, restart the service twice ⇒ no `boxset drift probe` warning after the first walk, summary shows unchanged; (2) same show in two libraries, sync in the pooling-first order, run `UpdateLibrary` twice ⇒ second pass logs no spared/missing for the pooled id and the boot probe is silent; (3) whitelist a view then remove it server-side, restart ⇒ nodes generate without a traceback, hash stamps, second start skips generation.

---

## Sizing and rollout

Five branches, priority order, each L2/L1-first with its doc updates in the same PR: `fix/boxset-restamp` (writers/movies.py +25, full_sync.py +10, tests +120; CLAUDE.md + boxsets-plan V7 ride along), `fix/pool-media-folder` (queries_map +10, writers/tvshows.py +15, full_sync.py +35, core/api.py +10, tests +150), `fix/recovery-ceilings` (library.py +30, full_sync.py +15, tests +80), `chore/checksum-spelling` (fields.py +10, obj.py +2, changefeed.py +3, full_sync.py +3, kofindb warn +8, tests +60; CLAUDE.md bullets), `fix/node-index-fallback` (views.py +15, tests +40).

The first two kill the live loops, the third caps the acknowledged one, the last two close the latent ones. F1 is independent and smallest — it ships first and retires the daily false-positive drift warning, which is the finding with the worst signal-degradation cost.
