# Benchmark audit: plugin.video.kofin vs jellyfin-kodi

Date: 2026-08-11. A comparative audit of the rewrite against the upstream it replaced, run as an adversarial measurement exercise rather than a demo. The output is an executive-summary report answering one question: **what did the rewrite actually buy, what did it cost, and what should go back upstream.**

This document is the *pre-registration*. It is written and committed **before any run**, and the scenarios, metrics and scoring rubric below are frozen once it lands. Anything that changes after the first run goes in a "Deviations" section of the report with a reason, never a silent edit here. That is the strongest bias control available to us and it is worth more than any protestation of neutrality.

---

## 0. Auditor discipline

The benchmark is executed as an unbiased auditor would execute it. That is a set of rules I follow while running, not a disclaimer the report recites:

* **Pre-registration.** Scenarios, metrics and thresholds fixed before any number exists. No metric may be added after seeing a result, and no scenario dropped because its result is inconvenient.
* **Scripted measurement.** Every headline number comes from a script neither addon can see or influence — a counting proxy in front of the server and a truth oracle that reads the Jellyfin API, never either addon's own logs.
* **A neutral oracle, not an A/B.** `tests/live/ab_diff.py` compares the two addons to *each other*, which cannot say who is right. The audit compares each addon independently to the server's own answer.
* **Adversarial scenario choice.** The failure-mode set is drawn from where kofin's own `CLAUDE.md` says it is fragile, not from where it is strong.
* **No unevidenced claims.** Every "kofin fixes this" needs a commit, a test or a scenario id behind it; without one it is recorded as unfixed.
* **Both arms get their best case.** Neither is handicapped to make a number look better.
* **Raw data kept.** Every run's artefacts ship with the report so any figure can be recomputed.

Three setup facts follow from this and shape the runs:

1. **Each arm runs as it ships.** kofin on KofinSyncQueue, jellyfin-kodi on the official KodiSyncQueue — both installed and Active. This is product against product, which is what a user actually gets, and the paired server plugin is part of what the rewrite delivered rather than an advantage to be factored out. The report says plainly that the change-feed protocols differ, so a traffic or latency gap on the incremental scenarios is a gap between *stacks*, not between client code alone.
2. **The writers are a transplant.** kofin's Kodi DB writers are near-verbatim jellyfin-kodi. Fidelity *parity* is the expected result and is not a rewrite achievement. Fidelity *divergence*, in either direction, is the finding.
3. **Scope limits to state, not discover.** Kodi Omega 21.3 only — Piers is schema-gated in kofin and untested live here, so jellyfin-kodi's Piers behaviour is out of scope and stated as untested rather than implied. Upstream jellyfin-kodi is alive (2.1.0 released 2026-07-05, last commit 2026-08-08), so pin the exact commit under test at phase 0.

---

## 1. Environment, as measured 2026-08-11

| | |
|---|---|
| Kodi | Omega 21.3 (Debian `2:21.3+dfsg-1.2+b1`), JSON-RPC `localhost:8080` (`kodi:kodi`), TCP notify 9090 |
| Profiles | `kofin-test` (populated: 1773 movies / 86 shows / 4505 eps / 22155 songs / 1626 albums), `jellyfin-kodi` (libraries empty, previously used) |
| Jellyfin | 10.11.11, `192.168.1.167:8096` (= `jelly.konell.xyz`), id `2606bcf8…` |
| Server plugins | Kodi Sync Queue 15.0.0.0 **and** Kofin Sync Queue 1.0.0.0, both Active |
| Addons under test | kofin 0.14.0 (345 commits, 125 merged PRs) vs `plugin.video.jellyfin` 2.1.0+py3 (upstream, no fork markers) |
| Corpus space | `/media/minipie/bluecon/video/{movies,shows}` — **both empty**, 465 G free on the NFS mount |
| Existing instruments | `tools/perfprobe.py`, `tests/live/ab_diff.py`, `docs/testing-plan.md` scenario catalog |

**Disk: 18 G free on `/home` (67 % used), 465 G on the media mount.** Both profiles' Kodi databases and texture caches live on `/home`. Kodi caches artwork lazily on *render* rather than on sync, so sync scenarios are cheap regardless; with this much headroom the UI-browse scenarios can browse the full library rather than a capped sample, which is the realistic case and the one worth measuring. Runs still gate on free space and abort below 3 G — a guard against a runaway, not a design constraint — and `Textures13.db` is truncated between arms as part of the cold reset.

That headroom also makes **texture-cache growth a metric rather than a hazard**: the two addons choose different artwork URLs and sizes, so cache bytes accumulated for the same rendered library is a real difference worth reporting. It is added to the §3.3 metric block.

---

## 2. Fairness setup

Done once, verified by script before every run, because a single unequal variable makes the whole exercise decorative.

1. **Two fresh Jellyfin users**, `bench-a` and `bench-b`, created for this audit, neither an admin. Identical library policy, identical (empty) watched state, identical parental/access settings, verified by diffing their `/Users/{id}/Policy` and `/Users/{id}/Configuration` payloads. The existing users are *not* reused — `kofin-test` currently sees a `Music-Alt` library the `jellyfin-kodi` user cannot, which alone would void every comparison.
2. **Arm-to-user assignment is fixed and stated**, and each arm gets its own Kodi profile so userdata, resume points and DBs never cross.
3. **Identical whitelists** in both addons: the same library set, verified from each addon's own settings before the run.
4. **Cold state between arms**: profile `addon_data/<addon>` removed, `MyVideos131.db` / `MyMusic83.db` / `Textures13.db` deleted, `kofin.db` and `sync.json` gone, Kodi restarted. A "cold" run that inherited a warm texture cache is a silently wrong number.
5. **Server-side warmth is a confound** — the first arm to touch a library warms Jellyfin's own caches. Cancelled by **A-B-B-A ordering** across the three repeats of every timed scenario, and by a discarded warm-up pass before the first measured one.
6. **3 repeats minimum** per timed scenario; report **median with min–max**, never a single run. A scenario whose spread exceeds 20 % of its median gets more repeats or a stated reason.
7. **Nothing else runs on the box** during a timed pass: no other Kodi profile, no encode job in `/media/minipie/bluecon/video/encode`, no scraper sweep. Checked by the runner, recorded in the run manifest.

---

## 3. Instrumentation

Three new tools under `tests/live/bench/`. Building these first is the majority of the setup cost and everything else depends on them.

### 3.1 The counting proxy — `benchproxy.py`

A small async HTTP proxy on `localhost:8099` forwarding to the Jellyfin server, with both addons' `serverAddress` repointed at it. It logs per request: timestamp, method, path (query normalised), status, request bytes, response bytes, duration, arm tag. Media bytes (`/Videos/…`, `/Audio/…`) bucket separately so a playback stream cannot swamp a sync total. WebSocket upgrades pass through and are counted as connections plus frame bytes.

This is the audit's single most valuable instrument, because **it measures both addons identically and neither can see it**. Traffic numbers taken from an addon's own debug log are an addon's opinion of its own behaviour; these are not.

It doubles as the **fault injector** (§7): a control socket makes it return 503s, drop connections mid-body, add latency, or blackhole a path prefix. One tool, two jobs, and fault injection that is byte-identical for both arms.

Proxy overhead perturbs timings, so **timing passes run direct and traffic passes run proxied** — separate passes, never one number claiming to be both. The proxy's own pass-through overhead is measured once and published.

### 3.2 The truth oracle — `oracle.py`

Reads the Jellyfin API for the bench libraries and builds an addon-independent expectation, then diffs each arm's Kodi database against it:

* **Presence**: every server item present in Kodi, nothing extra. 100 % coverage, both directions.
* **Fidelity**: a fixed field map (Kodi `movie_view` / `episode_view` / `album`+`song` columns ↔ Jellyfin fields) applied to a stratified sample — 200 movies, 200 episodes, 100 albums, chosen by a seeded RNG so both arms get the *same* items. The field map is written and reviewed **before** the first run and derived from Kodi's schema, not from either addon's writer code.
* **Structural integrity**: the 23 orphan link-table rules already used by S2.8, plus `PRAGMA integrity_check`.
* **Coverage-not-diff**: fields only one addon writes (chapter thumbs, critic ratings, video versions, playlists, library nodes) are reported as a *capability table*, not as fidelity failures against an addon that never claimed them. Scoring them as diffs would be exactly the thumb-on-the-scale this audit exists to avoid.

### 3.3 The runner — `run.py`

Drives a scenario end to end: assert preconditions, cold-reset the arm, start log marks and samplers, run, collect, tear down, write a manifest. Samples `kodi.bin` RSS and CPU from `/proc` at 1 Hz. Emits one JSON per run into `tests/live/bench/results/` (gitignored; published as an archive with the report).

Every scenario reports the same metric block, so the report's tables are joins rather than prose:

* wall-clock to completion (and to *usable* — first library node populated)
* HTTP request count, bytes in/out, request count by path class (proxied pass)
* peak and mean `kodi.bin` RSS; CPU-seconds
* Kodi DB rows written, final DB size on disk
* texture-cache bytes accumulated for the same rendered library (the two addons pick different artwork URLs and sizes)
* **UI responsiveness under load**: `perfprobe.py dir --fresh` against a fixed library node every 30 s *during* the sync, reported as median and p95. This is the metric users actually feel and neither project publishes it.
* log ERROR and WARNING counts, deduplicated by message
* oracle verdict: presence, fidelity, orphans

---

## 4. The corpus

Real libraries are never mutated. All destructive scenarios run against a purpose-built bench library.

**Titles.** Any public title+year list will do; nothing here depends on a particular provider. TMDB's keyless daily exports (`files.tmdb.org/p/exports/…`, confirmed reachable) are one convenient source, an OMDb or Wikipedia dump is another, and synthetic titles work if realism stops mattering. Real titles are worth a mild preference only because they make dedup meaningful and the bench library browsable.

**Metadata: NFO sidecars, generated by us.** The corpus writes Kodi-style `.nfo` files beside each media file and the bench libraries enable Jellyfin's NFO reader with network providers **off**. This is the better choice for a benchmark on three counts: it is *deterministic* (both arms see byte-identical metadata, and a re-run months later reproduces it), it is *fast* (no network, no rate limit — it removes the largest unknown from the schedule), and it lets us **dial metadata density on purpose** — people, genres, studios, ratings, premiered dates, taglines, multiple external ids — so the oracle's fidelity fields are all populated rather than left to whatever a scraper happened to return.

The server already has TMDb, TheTVDB, OMDb, MusicBrainz and AudioDB Active, so a scraped variant is available if a realistic-scrape leg is ever wanted. It is not the default: a network scraper introduces run-to-run variance and, worse, can re-scrape *between* the two arms and hand them different libraries.

**Deduplication.** Titles are generated synthetically from a seeded vocabulary, so collision with real media is structurally impossible rather than merely unlikely — which is what makes a mass-delete scenario safe to run at all. `corpus.py --verify-against` still asserts disjointness against the live library as a second line of defence.

**Verified 2026-08-11 on a 512-item pilot**, before committing to the full corpus:

* NFO-only works, and every fidelity field the oracle checks arrives from it: overview, tagline, genres, certificate, community rating, studio, country, cast (7 people), premiere date and a provider id, with no network provider consulted.
* `MediaStreams` are genuine — `h264` at the generator's mixed resolutions with correct 2- and 6-channel audio — so stream-detail fidelity is testable.
* **Runtime is the probed 6-second file duration, not the NFO's `<runtime>`.** Jellyfin prefers what it measures. Identical for both arms so the comparison stays fair, but runtime cannot *discriminate* in the bench corpus; B2 on the real library is what covers that field.
* Media generation is CPU-bound, not IO-bound (4.2 files/s to local disk against 5.2 over NFS on this 8-core box), so it scales with cores and belongs on the server.

**Two server behaviours that cost time to find, both now handled in `setup_bench.py`:**

1. A newly created library is invisible **even to an admin** whose `EnableAllFolders` is false: `/Items` answers `"conor is not permitted to access Library X"` and every count reads zero — indistinguishable from a scan that found nothing. The bench libraries must be granted to the querying admin explicitly.
2. `/Library/Refresh` rescans *every* library on the server, which is minutes of needless work against the real ones and perturbs anything else running. Scope it with `/Items/{id}/Refresh`. Scan completion is read from the `Scan Media Library` task state, never from item counts, because a library the caller cannot see reports zero throughout.

**Media.** `ffmpeg -f lavfi` 8-second H.264+AAC files, ~80 KB each — real playable media, so Jellyfin produces genuine `MediaStreams` and stream-detail fidelity is actually testable. A handful get deliberate variety: multi-audio, a sidecar subtitle, a second version, an `extras/` folder, one show with specials.

**Scale.** 4,000 movies + 200 shows × ~60 episodes ≈ 12,000 episodes ≈ 16,000 items, roughly 1.3 GB on the NFS mount. That is ~2.5× the current real library — enough that the arms' scaling behaviour separates, small enough to generate in an afternoon and re-sync in a coffee break.

**Staging for the change scenarios.** Generate 5,000 movies but stage 1,000 of them *outside* the library path, in `video/encode/bench-staging/`. Mass-add is then a `mv` rather than a generate, which makes change scenarios fast, repeatable and exactly reversible.

**Server side.** Two new Jellyfin libraries, `Bench-Movies` and `Bench-Shows`, visible **only** to `bench-a` and `bench-b`. Real users see nothing. Teardown is two library deletions and an `rm -rf` of two directories.

---

## 5. Scenario matrix — performance and correctness

Each is run per arm, 3 repeats, A-B-B-A interleaved.

| ID | Scenario | The question it answers |
|---|---|---|
| **B1** | Initial sync, bench library, cold profile | How long to first usable library, at what traffic and memory cost |
| **B2** | Initial sync, real library (movies + shows + music) | Does B1's answer hold on real metadata density, and does music change the ranking |
| **B3** | Initial sync interrupted at ~40 %, service bounced | Does it resume, or restart, or silently hole |
| **B4** | Library repair / full re-verify on an already-synced library | The single most-run maintenance action. Cost of proving nothing changed |
| **B5** | Repair after deliberate corruption (500 rows deleted from Kodi DB behind the addon's back) | Does repair actually *heal*, or only re-walk |
| **B6** | Incremental: 50 mixed changes with the client live (websocket path) | Latency from server change to Kodi row, per change class |
| **B7** | Incremental: same 50 changes applied while the client is down, then catch-up | The offline-queue path — and the no-op case: zero changes, what does a catch-up cost |
| **B8** | Userdata-only catch-up: 500 watched flips while down | Whether watched state costs a metadata download. kofin claims zero; the proxy adjudicates |
| **B9** | Idle steady state, 60 minutes, no changes | Background cost of simply being installed |
| **B10** | Sync running *while* the user browses and plays | The responsiveness metric under contention, plus does playback survive |

## 6. Scenario matrix — major library change

All against the bench library, all reversible.

| ID | Change | Watching for |
|---|---|---|
| **C1** | Mass add: 1,000 staged movies moved in, server scan, sync | Does the client scale the add or re-walk everything |
| **C2** | Mass remove: the same 1,000 moved out | Orphan rows, tombstones, and whether removal costs more than the add |
| **C3** | Mass relocate: 1,000 movies to a new subfolder, same content | Path churn — does the client see a move or 1,000 deletes + 1,000 adds |
| **C4** | Mass metadata edit: bulk tag change on 2,000 items via API | Etag churn with no content change; the "N downloads, zero writes" claim |
| **C5** | Whole library deleted server-side | Clean removal or wreckage |
| **C6** | Library removed from the client whitelist, then re-added | Does re-add cost a full resync |
| **C7** | Server-side rename of a show with 60 episodes | The classic parent-rename cascade |

## 7. Scenario matrix — failure modes

The user's list, plus the ones kofin's own `CLAUDE.md` flags as historically fragile — which is where an auditor should look, and where a demo would not.

| ID | Injection | Pass criteria |
|---|---|---|
| **F1** | `kill -9 kodi.bin` at 3 sync stages (early / mid / near-complete) | `PRAGMA integrity_check` clean, no duplicate or orphan rows, resumes or correctly re-drives unattended, no user action needed |
| **F2** | Server unavailable mid-sync (proxy blackhole) | No modal, no data loss, watermark does not advance, backs off, replays the missed window exactly once on recovery |
| **F3** | Server *slow*, not down: 30 s latency, 20 % 503s | Degrades rather than wedges; recovers without a bounce. The realistic outage, and the one both projects have historically handled worst |
| **F4** | Metadata edited server-side *during* a full sync | Final state matches the server, not a torn mix. Torn state is the interesting failure |
| **F5** | Watched state flipped during a full sync | Same, and no echo storm — the write→announce→echo→write loop `CLAUDE.md` warns about |
| **F6** | Addon upgraded (files replaced + bounce) mid-sync | Resumes on the new version; no half-migrated state; a schema/format change is detected rather than assumed |
| **F7** | Jellyfin server restarted mid-sync | Token survives, sync resumes |
| **F8** | Access token revoked mid-sync (401 storm) | Fails visibly and stops, rather than looping or silently truncating the library |
| **F9** | Kodi quit / profile switch mid-sync | Stops within Kodi's 5 s grace. `docs/library-thread-stop.md` records this taking ~125 s before it was fixed — the exact regression an auditor should re-measure |
| **F10** | Offline past the sync-queue retention window | Detects the gap and falls back to a full re-verify, rather than trusting an incomplete delta |
| **F11** | Library access revoked for the user mid-sync | Graceful; does not wipe the local library on a permission flap (the boxset-guard case in `CLAUDE.md` §V6) |
| **F12** | Two clients, same user, syncing concurrently | No corruption, no userdata ping-pong |
| **F13** | Kodi's own "Clean library" run during a sync | Contention handled; the addon's rows survive or are correctly rebuilt |
| **F14** | Client clock skewed ±10 minutes from server | Watermark logic is not silently wrong (a skew bug loses changes with no error) |
| **F15** | Disk full on the profile's database volume (loop-mounted small ext4, filled mid-sync) | Fails loudly, no corruption, recovers when space returns. No longer a stretch — there is room to build the loop file safely |

Scored per mode on six axes, all observable, none requiring judgement: **corrupts** (y/n) · **loses data** (y/n) · **recovers unattended** (y/n) · **needs user action** (y/n) · **tells the user** (silent / notification / modal) · **time to recover** (seconds).

---

## 8. Desk review 1 — upstream issue coverage

Scope, counted today: **jellyfin-kodi 153 open issues + 16 open PRs**, **jellycon 52 open issues**. 221 items. (402 and 146 closed respectively, not in scope.)

Pull to JSONL via `gh`, then classify each into exactly one bucket:

* **Fixed in kofin** — reproducible on jellyfin-kodi, absent in kofin, with a named commit, test or bench scenario as evidence.
* **Fixed by architecture** — the rewrite makes it structurally impossible (e.g. the ~30 ad-hoc window properties replaced by `core/state.py`).
* **Not applicable to the rewrite** — the issue names code that no longer exists. The user's examples are the archetypes: "Clean up dead settings and translations", "Refactor addon entrypoints and fix hang on exit #1056". This bucket is *not* a win and must not be counted as one; it is scope that evaporated.
* **Still present in kofin** — the honest column, and the one that makes the rest credible. Expect it to be non-empty.
* **Enhancement, implemented** / **Enhancement, not implemented** — tracked separately from bugs throughout; mixing them inflates any coverage percentage.
* **Out of scope** — server-side or Kodi-core, neither client can fix.

Two rules that turn this from assertion into measurement:

1. **No "fixed" claim without evidence.** A commit hash, a test name, or a bench scenario id. An unevidenced claim is filed as "still present" until proven otherwise — the burden sits on kofin, deliberately.
2. **Spot-check 20, chosen at random by seeded RNG, not by hand.** Actually reproduce each on the jellyfin-kodi arm, then confirm kofin's behaviour. The hit rate on that sample is published and is what licenses the reader to believe the other 200.

**jellycon is a separate table.** It is a fundamentally different architecture (no native Kodi database), so most of its issues are inapplicable by construction and saying so is not a finding. What is worth extracting: issues describing *Jellyfin-protocol* or *Kodi-platform* problems that any client hits, since those are the ones kofin might genuinely have solved and the ones worth taking upstream to both.

Deliverable: a CSV plus a counts table. The report quotes the counts and links the CSV.

## 9. Desk review 2 — what kofin should give back

Enumerate kofin's 125 merged PRs and 345 commits — 75 `fix:`, 47 `feat:`, plus the typed sync/playback/music groups. For each fix, classify:

* **Applies upstream as-is** — the bug exists in jellyfin-kodi 2.1.0 today and the patch is near-portable. *Verify against upstream's current `main`, not the 2.1.0 tag — it moved 3 days ago and some may already be fixed.*
* **Applies with porting** — same bug, different surrounding code.
* **kofin-architecture-specific** — nothing to send.
* **Already fixed upstream** — no claim to make.

The high-value candidates are visible already, and each is a bug a user can hit today on upstream:

* `UserDataChanged` unfiltered by user — writes a *co-watcher's* watched flag and resume point into the local library (`notes/jellyfin-kodi-issue-userdatachanged-additional-users.md` already exists and is upstream-ready)
* `discography` row growth — measured 24,907 rows where ~2,100 belong, on a real library, with visible year-0 strays in the UI
* Deprecated `/Users/{userId}/…` routes throughout — works on 10.11, breaks on the release that drops them
* Sync threads riding the full HTTP retry ladder on stop — ~125 s measured against Kodi's 5 s grace
* `<reuselanguageinvoker>` in the wrong extension block — silently no-ops; worth checking whether upstream has the same placement
* Rating-pointer (`movie.c05`) left dangling on a dropped critic rating

Deliverable: a prioritised list with a one-paragraph reproduction and a diff sketch each, ready to file. **This is the audit's most useful output regardless of what the benchmark numbers say**, and it is worth doing even if the rest slips.

---

## 10. Scoring and report shape

Six dimensions, weights fixed **now**:

| Dimension | Weight | Source |
|---|---|---|
| Correctness / fidelity | 30 % | oracle presence + fidelity + orphans |
| Resilience | 25 % | F1–F15 six-axis scores |
| Performance | 20 % | B1–B10 wall-clock, traffic, memory, UI p95 |
| Change handling | 15 % | C1–C7 |
| Feature coverage | 10 % | capability table |
| Upstream issue coverage | reported, unweighted | §8 counts |

Coverage is reported but not scored, because a percentage over a bucket set we defined ourselves is not a measurement and dressing it as one would be the exact failure this document opens by naming.

**Report**: 4–6 pages, executive summary throughout, technical detail relegated to appendices and the raw archive.

1. **Verdict** — one paragraph, stated plainly, including where the rewrite did not pay off
2. **Scorecard** — dimension × arm
3. **Six headline numbers** — the ones a reader will quote
4. **What the rewrite achieved** — per dimension, against the measurements
5. **What it cost** — regressions, feature gaps, new failure modes, maintenance surface. If this section is empty the audit failed.
6. **Upstream issue coverage** — counts, honest columns included
7. **What should go back upstream** — §9's prioritised list
8. **Method and limitations** — instruments, scope limits (§0.3), what was not tested
9. **Deviations from pre-registration** — every one, with reasons

Publishable as a shareable artifact once you have read it.

---

## 11. Sequencing and cost

Two separate budgets, which the first draft of this plan wrongly merged into one inflated "days" figure. Almost all the elapsed time is the box running unattended, not work.

**My working time** — writing scripts, reading issues, writing the report:

| Phase | Work | Time |
|---|---|---|
| 0 | Freeze this doc; bench users and libraries; fairness verification script | ~1 h |
| 1 | `benchproxy.py`, `oracle.py`, `run.py`, plus debugging against live Kodi | 3–6 h |
| 2 | Corpus generation scripts | ~1 h |
| 5 | Desk review §8 (221 issues, with evidence) and §9 | 5–6 h |
| 6 | Report | ~1 h |

**≈ 11–15 hours**, and phase 5 needs nothing from the box, so it overlaps the runs entirely.

**Box time** — serialized, because there is one Kodi and scenarios cannot overlap:

| | |
|---|---|
| Corpus: 16 k ffmpeg files + NFO sidecars | ~1 h |
| Corpus: Jellyfin scan, NFO only, no network providers | ~1–2 h |
| B1–B10 (3 repeats × 2 arms, B9 once per arm) | ~13 h |
| C1–C7 (2 repeats × 2 arms, incl. server rescans) | ~7 h |
| F1–F15 (2 repeats × 2 arms) | ~9.5 h |
| §8 spot-check: 20 issues reproduced live | 2–3 h |

**≈ 34–37 hours of box time.** Run back to back that is **~2 days elapsed**, most of it unattended, with my work fitting inside it. Serialization on the single Kodi instance is the only thing setting the floor.

**Trim levers, in the order I would pull them**, if that is too long:

1. Drop **B9** (idle steady state) — 2 h for a number that rarely surprises.
2. Repeats 3 → 2 on B1–B8, B10 — saves ~4 h, costs confidence in any result whose spread is wide.
3. Drop **B2** (real library) — saves ~2 h. The bench library already answers the scaling question; B2 only adds real metadata density, which the NFO corpus partly covers anyway.
4. Halve the corpus to 8 k items — saves the most, ~half the scan and ~a third of the run time, but weakens exactly the scaling comparison the benchmark exists for. Last resort.

Cutting the §8 spot-check saves 2–3 h and is the one cut I would not make: it is what converts 200 classification claims from assertion into measurement.

**Risks, and what to do about them:**

* **Disk** — no longer a live risk at 18 G free, but the free-space gate stays in every script: a corrupted run discovered at the end costs more than the check.
* **Server scan time for 16 k dummy items** — bounded now that the scan is a local NFO read, but still worth a 500-item pilot to get a real per-item rate before committing to the full corpus.
* **Upstream is a moving target** — pin the commit at phase 0 and re-check at phase 5.
* **jellyfin-kodi may simply fail some scenarios outright** (F10, C3 and B5 are plausible) — a hard failure is a legitimate result, recorded as one and not quietly softened, but also not counted twice by leaking into the other dimensions.
* **Scope creep in §8** — 221 items is the largest single time sink. If time runs short, cut the jellycon table and the enhancement buckets before cutting the spot-check; the spot-check is what makes the rest believable.

---

## 11b. Deviations from pre-registration

Logged as they happen, per §0. Every entry changed something after runs had started.

1. **Starting line for the sync clock** (2026-08-11). Fixed as "logged in, libraries chosen, nothing synced" rather than a fresh install, because jellyfin-kodi's first run is a modal library picker and kofin's is not — including setup would have measured the wizard. Reached by logging each arm in once through its **own real flow** and snapshotting; hand-writing config files was tried first and is not equivalent for either addon.
2. **jellyfin-kodi's cold reset restarts Kodi** (2026-08-11). Not a fairness choice but a necessity: that addon creates its database schema once per Kodi *process*, so a reset that deletes the file leaves it unrecoverable until restart. Declared per arm in the config and reported, since it is itself a difference between the two.
3. **A dialog answerer was added** (2026-08-11). jellyfin-kodi's full sync raises a blocking modal whenever the pending queue is non-empty and waits forever. The count of modals answered became a reported metric rather than hidden plumbing.
4. **Log-error metric corrected mid-batch** (2026-08-11, after B1 run 1). Two faults: severity matching was case-wrong (Kodi 21 logs lowercase, so it reported 0 errors against 17,791 real lines), and the count charged each arm for the *other* addon's teardown noise — profile switching stops the rival service, and jellyfin-kodi's hang-on-exit logs into the window being measured. Now reports `addon_errors` (lines naming the arm's addon) alongside the raw window total, and dedupes on message *shape* rather than raw text, since ids made 400 instances of one bug look like 419 distinct errors. **B1 kofin run 1's log figures are not comparable with later runs**; its timing, memory and counts are unaffected.

5. **B4 jellyfin-kodi run 6 is flagged as possibly contaminated** (2026-08-11). A B8 run was started while that run was still in progress, on a misread of the batch log, and it issued a `Profiles.LoadProfile` into a live measurement. The switch did not take effect and the run continued to completion, but the interference cannot be ruled out, so the run is marked and the B4 medians are quoted with and without it. The rule it breaks — nothing else runs during a timed pass (§2.7) — was already in this plan; the fix is to wait on the batch *process*, never on log text.

6. **Log-error attribution corrected again** (2026-08-11, after F2). Deviation 4 fixed the *severity* match but filtered ownership on the addon **id**, while kofin logs `[kofin] …` and jellyfin-kodi logs `JELLYFIN…`. Only tracebacks (which embed the file path) ever matched, so runs that logged hundreds of errors reported 0. Each arm now declares a `log_marker` in the config. F2 kofin was recomputed from the live log (427 errors / 28 warnings / 2 distinct shapes, ~400 of them the ghost-record issue); **B1 and B4 error counts remain undercounted and should not be quoted**. Timing, memory, counts and correctness are unaffected throughout.

7. **Server-side changes are driven by a whole-server scan, not a library-scoped one** (2026-08-12, before C1). The C-scenarios need the server to notice a change on disk, and the scoped route — `POST /Items/{libraryId}/Refresh` — resolves its target *through the calling user's own visible libraries* in 10.11, returning 404 rather than 403. The bench libraries are deliberately invisible to the real admin (that visibility is what pushed 12,404 bench records onto a production Kodi box on 2026-08-11), so no elevated caller on this server can scope a scan to them. Two ways out were rejected: re-widening the real admin's `EnabledFolders`, which is the original mistake, and minting a second administrator, which is a durable change to the operator's server that a benchmark should not make unasked. What is used instead is `POST /Library/Refresh` — not item-scoped, therefore no visibility check, and byte-for-byte the "Scan Media Library" task the server already runs nightly, in `Default` mode so nothing is replaced wholesale. Measured cost on this box: 15–30 s for a metadata edit, 161 s for a 1,000-movie add. It walks the operator's real libraries too, which is why every scan is timed and reported *separately* from the client-side number; it delays both arms identically and biases neither. Reads stay scoped — as a bench user, the only identity that can see a bench library at all.

8. **B6's first pass was re-run at finer resolution** (2026-08-12). At a 15 s poll both arms converged in the same bucket, so "45 s vs 45 s" was really "somewhere under 45 s, twice" — a tie the instrument could not distinguish from a difference. Re-run at 5 s. Reported numbers for the incremental scenarios come from the 5–10 s passes; the 15 s pass is kept but is not quotable for a comparison.

9. **C2/kofin's timing was lost and the correctness result kept** (2026-08-12). The run's harness call died between applying the change and writing its manifest. The change itself completed, so the end-state was verified directly against the arm's database — exactly 4,000 movies restored, zero `movie_without_file`, zero orphaned art — and that correctness result stands. The **timing** number does not exist for that run and is not reconstructed; C2/kofin is re-run for the clock rather than back-filled from the log.

10. **B8's conclusion was retracted and re-tested** (2026-08-12). The first pass reported jellyfin-kodi applying 0 of 500 offline watched flips and attributed it to the official Kodi Sync Queue "recording no userdata at all", on the strength of a direct query returning `UserDataChanged: 0`. Re-tested, that queue holds the userdata at every cutoff tried (568 records) and the client applies all of it. The real defect is a startup race: the plugin writes its records asynchronously — measured at up to 16 s — and jellyfin-kodi queries it exactly once at startup with no retry, so a client restarted inside that window is told there is nothing to do. Demonstrated by holding the same change set constant and varying only the gap (`--delay-before-start`): 0 s → flat for 315 s and an addon bounce required; 45 s → 15 s unattended. The original claim was a serious charge against someone else's code and is left visible in the report rather than edited away.

11. **kofin was upgraded mid-audit, client and server plugin** (2026-08-12): 0.14.0 → 0.15.0 (`f303362`), KofinSyncQueue 1.0.0.0 → 10.11.0.1. All scenarios up to and including F2 are 0.14.0; B3, F3, B9 and the single-pass re-baseline of B1/B4 are 0.15.0. **No table mixes the two.** Two further notes belong here. kofin's cold resets seed config directly rather than restoring a snapshot — `snapshots/kofin/` no longer exists and whether the 0.14.0 runs used one cannot now be established; for kofin the two paths are equivalent in a way they are not for jellyfin-kodi, and every kofin run reached an exact 15,796-row library, but it is an unverified difference between batches. And an unstarted-thread join in the runner destroyed two runs *after* their measurement completed (B5/kofin, B1/kofin 0.15.0); it is fixed at every call site, C2/kofin's correctness was verified from the database and its timing re-run rather than reconstructed.

12. **The stall-intervention policy caused a false finding, and is now bounded** (2026-08-12, after C3). The runner bounces a stalled arm after `--stall-seconds` and reports the intervention. In C3 that threshold was 600 s — chosen because every scenario to that point settled in well under it — and jellyfin-kodi's relocation catch-up takes ~660 s. The bounce landed roughly thirty seconds before it would have converged, unregistered the add-on, and triggered upstream #1163's `RuntimeError: Unknown addon id` cascade, killing the catch-up threads. The run was then written up as jellyfin-kodi permanently leaving 1,000 library entries pointing at `404`ing items — verified against the server, and entirely an artefact of the runner. Three corrections: the threshold is no longer assumed to exceed an arm's real settling time, a scenario whose purpose is to measure unattended behaviour is run with interventions **disabled** (`--stall-seconds` above the timeout) before any claim is made, and a striking result is reproduced before it is written down. The confounded run is kept as `C3` and the clean one as `C3b`.

13. **The dialog answerer now starts before the change tools, not after** (2026-08-12, during C3b). jellyfin-kodi raises "You have N updates pending — proceed anyway?" as soon as it notices a backlog, and a server scan runs for minutes. With the answerer started only once the scan finished, that modal sat unanswered for the whole change window and the arm processed nothing while the runner attributed the time to the server. The measurement clock still starts after the server settles; the fix only lets the arm work during it.

## 12. Decisions needed before phase 0

1. **Corpus scale** — 16 k items as proposed, or 8 k for a faster first pass? Gate the answer on the 500-item scan pilot.

Settled: two arms as they ship (kofin on KofinSyncQueue, jellyfin-kodi on the official plugin); NFO sidecars with network providers off; B2 and F15 both in, and UI-browse scenarios uncapped, now that disk headroom allows it.
