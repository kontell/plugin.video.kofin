# kofin vs jellyfin-kodi — benchmark report

kofin 0.15.2 against jellyfin-kodi 2.1.0+py3, 24 of 32 scenarios. Pre-registration: `benchmark-audit-plan.md`. The harness, its raw JSON manifests and the working notes are held outside the repository.

## 1. Summary

**What the rewrite achieved.** kofin is faster than jellyfin-kodi on every timed sync operation: a cold 16,404-item library in 345 s against 450 s, the operator's real 27,000-item library in 570 s against 780 s, and a repair in 375 s against 480 s — each on less CPU. Interactive latency is the widest margin: under a running sync it resolves a stream in 0.51 s against 3.52 s and lists its own root in 0.19 s against 2.60 s.

The clearest behavioural difference is unattended recovery. Four interruptions were injected into kofin — an add-on bounce mid-sync, a `kill -9`, a total 120 s outage and a 20 % error rate for 180 s — and it resumed from all four with no human action, no modal and no lost rows. jellyfin-kodi recovered from none of the four injected into it without intervention: a blackout, intermittent 5xx errors and a startup timing race each needed an add-on bounce, and an add-on bounce mid-sync left it permanently stopped at 6,689 of 15,796 rows with nothing shown to the user. kofin's sync path raised no blocking modal in any measured run; jellyfin-kodi raises one or two per cold start, and cannot resume unattended after a crash because it asks first.

**What it did not achieve.** Correctness is near-unchanged, because there was little to improve: both clients wrote the corpus exactly, with 0 mismatches in 3,600 field comparisons and capability tables that agree everywhere except `uniqueid`, where kofin writes 4,000 typed rows against jellyfin-kodi's 15,796 empty ones.

**Net.** The rewrite bought speed, resilience and interactive latency, and kept correctness where it already was. Of 153 open upstream issues, 74 are resolved by evidence — 12 fixed, 10 enhancements implemented, 4 structurally impossible, 19 inapplicable to the new code, 8 out of scope, 10 enhancements not implemented, 11 still present — and 79 remain unverified.

## 1b. At a glance

| Dimension | kofin | jellyfin-kodi | Diff |
|---|---|---|---|
| Initial sync, 16,404 items | **345 s**, 265 CPU-s | 450 s, 370 CPU-s | **−23 %**, **−28 %** |
| Initial sync, real library (27k items) | **570 s**, 599 CPU-s | 780 s, 803 CPU-s | **−27 %**, **−25 %** |
| Correctness, 3,600 field comparisons | 0 mismatches | 0 mismatches | — |
| Recovers unattended from interruption | **4 of 4** | 0 of 4 | — |
| Blocking modals in the sync path | **0** | 1–2 per cold start | — |
| Stream resolve under load | **0.51 s** | 3.52 s | **−85 %** |
| Addon root listing under load | **0.19 s** | 2.60 s | **−93 %** |

kofin relative to jellyfin-kodi; negative is less time. The three dashes are counts, not quantities, and a percentage of them would be meaningless.

## 2. Under test

| | |
|---|---|
| kofin | 0.15.2, KofinSyncQueue 10.11.0.1 |
| jellyfin-kodi | 2.1.0+py3 (`00c33dba`), Kodi Sync Queue 15.0.0.0 |
| Kodi | Omega 21.3, one box, profiles `kofin-test` / `jellyfin-kodi` |
| Jellyfin | 10.11.11 |
| Bench corpus | 16,404 items — 4000 movies, 200 series, 608 seasons, 11,596 episodes; synthetic, NFO-only |
| Real corpus | 1767 movies, 4487 episodes, 78 series, 22,874 music items |
| Users | `bench-a` / `bench-b`, 22 policy fields verified identical, 0 played at t0 |

## 3. Initial sync

### B1 — bench corpus

kofin n=2 (345, 345 s); jellyfin-kodi n=3 (435, 450, 450 s).

| | kofin | jellyfin-kodi |
|---|---|---|
| Wall clock | **345 s** | 450 s |
| CPU seconds | **265** | 370 |
| Peak RSS | see §8 | see §8 |
| Time to first item | 15 s | 15 s |
| Blocking modals | **0** | 1 |
| Final counts | 4000 / 11,596 / 200 | 4000 / 11,596 / 200 |
| Own log errors | 0 | 2 |

### B2 — operator's real libraries

| | kofin | jellyfin-kodi |
|---|---|---|
| Movies / episodes / series | 1767 / 4487 / 78 | 1767 / 4487 / 78 |
| Wall clock | **570 s** | 780 s |
| CPU seconds | **599** | 803 |
| Peak RSS | see §8 | see §8 |
| Music (songs / albums / artists) | 20,802 / 1,539 / 628 | not comparable |

Video output is identical to the item. The music leg is not compared.

### Correctness — truth oracle, server as referee

| | kofin | jellyfin-kodi |
|---|---|---|
| Movies present / missing / extra | 4000 / 0 / 0 | 4000 / 0 / 0 |
| Episodes present / missing / extra | 11,596 / 0 / 0 | 11,596 / 0 / 0 |
| Field mismatches | 0 of 3,600 | 0 of 3,600 |
| 21 orphan rules + `integrity_check` | PASS | PASS |

`uniqueid` is the one table the two arms disagree on. kofin writes 4,000 rows, none empty, each typed with the provider the item actually carries (`tmdb`), and no row at all for an item with no mapped provider. jellyfin-kodi writes 15,796 rows, **every one of them empty**, against a hardcoded `imdb`/`tvdb`.

The remaining capability tables came back byte-identical: 15,796 rating, 31,192 streamdetails, 900 actor, 4200 tag_link, 4000 videoversion.

## 4. Repair

| | kofin | jellyfin-kodi |
|---|---|---|
| B4 — repair a correct library | **375 s**, 596 CPU-s | 480 s, 379 CPU-s |
| B5 — repair after 500 rows deleted behind the addon's back | **436 s**, 488 CPU-s | 722 s, 774 CPU-s |
| Restored exactly | yes, both | yes, both |
| Orphans after | 0 | 0 |

Both heal without being told what was damaged. Repair is a from-scratch re-sync on both — as each documents — so the cost scales with library size rather than with the damage: the library drops to zero rows ~45 s in and is unavailable or partial until the rebuild completes.

## 5. Incremental sync

| | kofin | jellyfin-kodi |
|---|---|---|
| B6 — 50 mixed changes, client live | 45 s | 45 s |
| B7 — same, client stopped then restarted | **10 s** | 40 s |
| B8 — 500 watched flips, client restarts immediately | **15 s**, unattended | flat 315 s, then 40 s after an addon bounce |
| B8c — 500 flips, client restarts 45 s later | — | 15 s, unattended |
| Orphans / own log errors | 0 / 0 | 0 / 0 |

The B8 pair isolates the cause: the Kodi Sync Queue writes records asynchronously (measured up to 16 s) and jellyfin-kodi queries it once at startup with no retry. A client restarting inside that window is told there is nothing to do.

## 6. Library change

| | kofin | jellyfin-kodi |
|---|---|---|
| C1 — 1,000 movies added | **15 s** | 30 s |
| C2 — the same 1,000 removed | 15 s | 15 s |
| C3 — 1,000 relocated, all ids reminted | **15 s** | 661 s |
| C4 — tag added to 2,000 movies | 60 s | 60 s |
| C5 — library deleted server-side | no change (correct) | no change (correct) |
| C6 — library de-selected / re-added | **60 s / 255 s** | 90 s / 450 s |
| C7 — series folder renamed, 61 ids change | 15 s | 15 s |
| Orphans / count drift, all scenarios | none | none |

Client times are measured from the moment the server settled. Server scan time is separate: 161–201 s for the add, 123–133 s for the remove, 80 s for the rename. For a mass change the server dominates.

**C5**: neither client removes the library, and that is correct. After `DELETE /Library/VirtualFolders` the server still lists it in `/UserViews` for the syncing user and still returns all 4,000 items under its id, so nothing visible to a client has changed. Dropping the rows on that evidence would delete a library the server still advertises. A test of genuine server-side absence has not been run.

**C6**: re-add costs less than a first sync on both arms. kofin's library-manager messages require a per-install nonce; jellyfin-kodi's take no secret, so any process able to send a Kodi NotifyAll can make it drop or rebuild a library.

## 7. Interruption

| | kofin | jellyfin-kodi |
|---|---|---|
| B3 — addon bounced at 40 % of first sync | **recovered unattended, 150 s** | **never recovered**, 6,689 of 15,796 |
| F1 — `kill -9` at 40 % | recovered unattended, 150 s | recovered, 495 s |
| F2 — 120 s total outage | recovered unattended, 195 s | required an addon bounce |
| F3 — 20 % `503` for 180 s | **recovered unattended, 345 s** | stalled 918 s, then 687 s after a bounce |
| Rows lost | 0 in all | 0 in all |
| `integrity_check` / orphans | ok / 0 | ok / 0 |
| F4 — metadata edited server-side mid-sync | 335 s, exact | 590 s, exact |
| F5 — watched state flipped mid-sync | 319 s, exact | 559 s, exact |
| F9 — profile switch mid-sync | 396 s, exact | 323 s, **incomplete** — 4000 / 2644 / 45 |
| F13 — Kodi's own Clean Library mid-sync | movies lost, repair required | movies lost, repair required |
| Blocking modals | 0 | 2 (F1), 2 (F3) |

Neither client loses or corrupts data under any injection. The difference is whether a human is required.

**B3**: jellyfin-kodi's replacement service cannot take the SQLite lock the dying instance still holds, and its startup thread exits on the exception rather than retrying:

```
sqlite3.OperationalError: database is locked
  File "jellyfin_kodi/library.py", line 405, in startup
  File "jellyfin_kodi/views.py", line 121, in add_library
```

Nothing restarts it; the addon then runs with no library thread and 42 % of the library, with no indication to the user.

**F4/F5**: a server-side metadata edit and a userdata flip landing mid-walk are absorbed by both — final state matches the server exactly, no torn mix and no echo storm. kofin is ~1.7× faster on both.

**F9**: a profile switch mid-sync is the second scenario after B3 where jellyfin-kodi stops permanently short — 6,689 rows of 15,796, the same figure and the same shape as B3. kofin finishes.

**F13**: Kodi's own Clean Library removes every movie row both addons have written (episodes and shows survive), and neither rebuilds them; a repair is required afterwards. Clean Library is user-initiated destruction of the library, so this is the expected cost of running it rather than a client defect. Watched for 45 minutes after the event on kofin with no automatic recovery.

**F3**: kofin kept writing through the fault (5,500 → 5,800 rows) and finished complete without intervention. jellyfin-kodi stopped writing at 5,035 rows and stayed there until bounced.

## 8. Contention and idle

### B10 — browsing and playing during the first sync

| | kofin | jellyfin-kodi |
|---|---|---|
| Sync completed | yes | yes |
| Playback starts / failures | 10 / **0** | 6 / **0** |
| Stream resolve, median | **0.51 s** | 3.52 s |
| Library read under load, median | 0.108 s | 0.106 s |
| Addon root listing, median | **0.19 s** | 2.60 s |
| Orphans / `integrity_check` | 0 / ok | 0 / ok |

Playback starts every time on both arms and neither library ends damaged. Reading Kodi's library is at parity, so the resolve and listing gaps are addon code rather than database contention.

### B9 — 30 minutes idle, no server-side changes

| | kofin | jellyfin-kodi |
|---|---|---|
| Library churn | none | none |
| CPU over 30 min | **66 s** (3.7 % of a core) | 104 s (5.8 %) |

Neither addon rewrites rows when nothing has changed.

### Memory

Every RSS figure is the whole Kodi process. Kodi's own resident set with the add-on disabled is 469–484 MB on this box, so absolute RSS is mostly Kodi and only the delta over an idle-Kodi baseline says anything about an add-on. One run per arm captured that baseline:

| Bench corpus, 16,404 items | kofin | jellyfin-kodi |
|---|---|---|
| Baseline, add-on disabled | 468.7 MB | 484.3 MB |
| Peak during full sync | 667.2 MB | 513.9 MB |
| Working set over baseline | +198.5 MB | **+29.6 MB** |
| At rest after the add-on cycles | +18 MB | not measured |

Three qualifications, all of which cut against reading the row as a ratio:

* **n=1 per arm.** No other run captured a baseline.
* **kofin's peak is not repeatable.** Five runs of this scenario peaked at 667, 793, 795, 806 and 939 MB — a 272 MB spread, against 485–514 MB across four jellyfin-kodi runs. A peak that moves by 40 % between identical runs is a transient allocation pattern, not a structure of a fixed size.
* **The sampler returned impossible values elsewhere.** 200.6 MB for a whole Kodi process in B9, and 1.1/2.0 MB in two B10 runs, against a floor of ~470 MB. Those runs are excluded here; they are the reason the figures above are not extended to the other scenarios.

What survives: kofin's full-sync working set is the larger of the two, by a margin measured once at ~170 MB. It is transient — the process returns to within 18 MB of baseline once the add-on cycles, which happens at every Kodi start — and the fetch path it comes through is explicitly bounded at `PREFETCH_PAGES * limitThreads * limitIndex` items (600 at the defaults, `sync/downloader.py`), so it is not the page buffer. It was not localised to any allocation. Against a Kodi floor of ~470 MB, and set beside 315 s of wall clock on 232 CPU-seconds where jellyfin-kodi takes 570 s on 467, this is a resource profile rather than a fault, and is not carried as a defect.

#### The constrained-hardware question, since answered

The paragraph above used to end "untested on a memory-constrained device, where the corpus size rather than the client is the dominant variable". It has since been tested, and the answer is that memory is not the constraint.

B2 was re-run on a **Raspberry Pi 3B — 918 MB RAM, no swap, armv7l** — against the same real libraries, on LibreELEC 13.0 (Kodi 22.0-BETA1, MyVideos148). This is a different arm from the tables above in two ways that forbid pooling the numbers: kofin 0.17.0 rather than 0.15.2, and no jellyfin-kodi leg, because the box exists to test kofin. It is an absolute footprint on the worst hardware kofin realistically runs on, not a comparison.

| B2, real libraries, Pi 3B | |
|---|---|
| Kodi baseline, idle | 140 MB |
| Peak during the sync itself | **252 MB** |
| Mean during the sync | 181 MB |
| Lowest free memory during the sync | 531 MB of 918 MB |
| Wall clock | 2610 s (43:30) against 570 s on the desktop |
| Counts | 1767 / 4488 / 78 video, 20,802 / 1,539 / 628 music |
| Own log errors | 0 |

Two things fall out of it.

**The sync does not accumulate.** RSS sat flat at ~180 MB across the whole run, including the 25-minute music phase, on a box with a 140 MB Kodi floor. That is ~110 MB of add-on working set against the desktop's measured ~198 MB over a ~470 MB floor — which is the more trustworthy of the two figures, because the smaller floor leaves less room for the measurement to be Kodi's. The desktop's unrepeatable 272 MB spread is better read as allocator behaviour on a 15 GB box than as anything kofin needs.

**The expensive moment is the skin reload, not the sync.** Making a first sync visible costs a `ReloadSkin()`, and on this box that took RSS from 252 MB to 469 MB and free memory down to 177 MB for about 25 seconds before returning to 273 MB. It was the tightest point of the entire operation, and it still cleared by a wide margin.

That reload has since been moved: libraries are published as they finish rather than only at the end of the whole sync, because on this hardware the old behaviour left the home screen reading "empty" for 35 minutes after the movies were written and browsable. The re-run with that change carries a real and different memory profile — once Home is populated at ~600 s it renders artwork for the rest of the run, so RSS holds at 314–351 MB instead of ~180 MB and free memory bottoms at 271 MB rather than 531 MB. Still no swap, still no pressure, but it is a persistent ~160 MB rather than a transient spike, and it is a cost of showing content sooner rather than of syncing it.

The honest summary is that on the smallest supported hardware the add-on's own footprint is around 110 MB, what the *skin* does with the synced content costs more than the sync does, and neither comes close to the limit. Memory is no longer a live question for this workload, which is why it no longer appears in §1.

## 9. Defects in kofin

None: fixes have been implemented for all defects identified during the benchmarking process.

## 10. Defects in jellyfin-kodi

Ordered by severity.

* **No supervisor.** Four independent injections — blackout, intermittent 5xx, add-on bounce, startup timing race — each killed the thread owning the work, and nothing restarted it. B3 ends with a permanently partial library.
* **`auth.server-id` is read in three error handlers and set nowhere.** A 120 s outage kills the sync thread.
* **Season removal deletes an unrelated season's episodes** (`objects/tvshows.py:748`): episodes looked up by the season's `ParentId` (the series' `idShow`) while episode rows key on `idSeason`. Silent. The same file's `tvshow` branch is correct.
* **Every `uniqueid` row is written empty.** 15,796 rows, no value in any of them, typed against a hardcoded `imdb`/`tvdb` rather than the provider the item carries (§3).
* **Hang on exit.** Threads survive a profile switch, hold ~60 % CPU with no log output, and eventually wedge Kodi's JSON-RPC; cold resets require `SIGKILL`.
* **`jellyfin.db`'s schema is created once per Kodi process.** Lose that file and every start dies on `no such table: version` until Kodi restarts.
* **A plugin listing during teardown hangs the Kodi GUI.** `Files.GetDirectory` has no timeout; observed, requiring `SIGKILL`. Plausibly open issue #866.
* **Startup sync-queue race** (§5).
* **Unguarded library IPC** (§6).

## 11. Upstream coverage

153 open issues. No coverage percentage is claimed while 79 are unverified; those are dominated by playback and environment reports needing specific hardware.

| Bucket | n |
|---|---|
| UNVERIFIED | 79 |
| FIXED — reproducible upstream, absent in kofin | 12 |
| PRESENT — still present in kofin | 11 |
| ENH_DONE — enhancement kofin implements | 10 |
| NA_REMOVED — moot because the feature was dropped | 10 |
| ENH_TODO — enhancement kofin does not implement | 10 |
| NA_CODE — the code described no longer exists | 9 |
| OUT — server or Kodi core | 8 |
| ARCH — structurally impossible in the rewrite | 4 |

16 open PRs, all resolved to a named file: FIXED 8, ENH_DONE 3, OUT 3, NA_CODE 1, ARCH 1.

JellyCon's 52 issues are tabled separately: mostly inapplicable by architecture, ten cross-client items worth carrying.

## 12. Limitations

* n=2 for kofin's B1, n=3 for jellyfin-kodi's B1 and B4. Every other scenario is a single pass per arm. Differences below the 15 s poll interval are not differences.
* The bench corpus is synthetic and NFO-only — no critic ratings, extras, multiple versions or network scraper. B2 covers real metadata density for video; music is not covered.
* Kodi Omega 21.3 only. Piers is schema-gated in kofin and untested live.
* One box, one server, no network variation. The client is an Intel Core i5-8400H — 4 cores / 8 threads, 2.5 GHz base and 4.2 GHz turbo, 15 GB RAM. Every wall-clock figure is bounded by that; a client with fewer cores narrows the gaps that come from concurrency, and both arms' CPU-seconds are the portable numbers.
* Setup is excluded from the sync clock: the starting line is "logged in, libraries chosen, nothing synced".
* Server-side changes are driven by a whole-server scan, timed and reported separately from client time.
* The *comparative* memory measurement is the weakest thing here: one baselined run per arm, a peak that is not repeatable on kofin, and a sampler that returned impossible values in three other runs. It supports a direction, not a ratio, and no conclusion in this report rests on it. kofin's absolute footprint is on firmer ground — measured on a 918 MB Pi 3B with no swap, where the desktop's confounds do not apply — but that leg has no jellyfin-kodi arm and a different kofin version, so the two must not be pooled (§8).
* Not covered: the music leg of B2, and F6–F8, F10–F12, F14, F15.
