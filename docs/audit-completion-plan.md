# Thorough code audit of plugin.video.kofin

| Field | Value |
|---|---|
| **Date** | 2026-08-30 |
| **Status** | Done — report frozen 2026-08-30 |
| **Tree** | `main` `48f3ae5` (addon `0.22.0`); LOC 43,806 / 123 modules; tox counts pending A0 |
| **What this is** | A full-tree correctness / robustness / security audit of *this* add-on, producing a frozen findings report and then a stacked fix plan |
| **What this is not** | The kodi-drive knowledge-repo sweep (`kodi-drive/skills/audit`); the 2026-08-11 benchmark vs jellyfin-kodi (`docs/benchmark-audit-plan.md`); a re-run of the 2026-08-29 hardening items already specified in `docs/audit-fixes-plan.md` |
| **Supersedes** | The never-committed `docs/audit-completion-plan.md` A2–A6 (the reading passes over the ~55 % the 08-29 audit did not open) |
| **Kodi access (2026-08-30)** | **Confirmed on `P1D`.** Omega 21.3, profile `kofin-test`, Home, JSON-RPC up. Started over SSH (`KODI_P1D_SSH`) because Kodi was not running. Installed kofin on that profile is **0.21.1**; this tree is **0.22.0** — A4 deploys before any live High. |

---

## 0a. Kodi access — checked at planning time

`kodi-orientation` rule 1: get a real Kodi at planning time, do not guess. `kodi-connect` was loaded and run.

**What is configured.** `~/.config/kodi-drive/targets.env` (now mode **600**) with default target `P1D` and named targets `BRAVIA`, `LIBREELEC`, `PIXEL`, `TAB`. Helpers live in `/media/bluecon/dev/kodi-drive/bin` and were **not** on `PATH` in this session — A0 exports that directory. Credentials were never printed.

**Why the first probe looked like "no Kodi".** `kodi-discover` sweeps JSON-RPC on 8080; every named target refused because **Kodi was not running**, not because the API was off. This agent host has no `kodi` binary and no `~/.kodi`; loopback `:8080` is ProtonMail's `hydroxide` (a 401 that is not Kodi). `kodi-remote` only copies official keys (`TRANSPORT`, `HOST`, `PORT`, `ADDR`, …). P1D's SSH hop is a **custom** `KODI_P1D_SSH=user@host` — `TRANSPORT`/`ADDR` are unset — so the helpers never tried SSH. That was the miss; the key *is* in `targets.env`. BatchMode SSH as `conor` succeeded.

**What is now running (started this session on the existing `:0` X11 session).** `kodi.bin` on `P1D`, Debian `2:21.3+dfsg-1.2+b2` (JSON-RPC `major=21 minor=3 tag=stable`). `services.webserver` / EventServer / zeroconf already true in `guisettings.xml`. Current profile **`kofin-test`**. Other profiles present: Master user, `jellyfin-kodi`, `kofin-jf12`. Home window (10000). `plugin.video.kofin` installed and enabled at **0.21.1**.

**Confirm control (re-runnable):**

```sh
export PATH="/media/bluecon/dev/kodi-drive/bin:$PATH"
kodi-remote get Application.GetProperties '{"properties":["version","name"]}'
kodi-remote get Profiles.GetCurrentProfile '{}'    # properties:["label"] is invalid on this Omega
kodi-remote get Addons.GetAddonDetails '{"addonid":"plugin.video.kofin","properties":["version","enabled","installed"]}'
```

**Implications for the audit.** A0–A3 can use this box when a finding cites Kodi behaviour. A4 live Highs run here, on `kofin-test` for dumps and `kofin-jf12` for mutating scenarios. **Deploy current `main` first** (`tools/dev-install.sh`) — live 0.21.1 would confirm the previous release, not this tree. Do not test on Master user (`kodi-test-rig`). The jf12 media rule still holds. `BRAVIA` / `TAB` / Piers flatpak remain unconfirmed this session (ADB empty; no flatpak on this host); they stay optional for ARM-cost and schema-live legs.

**kodi-drive note (G-candidate, observed):** on Omega 21.3, `Profiles.GetCurrentProfile` with `properties:["label"]` returns `-32602` (`Item.Fields.Base` enum); `{}` returns `{"label":"kofin-test"}`. The `kodi-profiles` skill's example uses the failing form. Contribute after the audit if it still holds.

---

## 0. Why a new audit, and why now

The 2026-08-29 pass found 28 items (F1–F9, M1–M8, R1–R10, A4-1), verified them the same day, and landed H0–H14 on `fix/audit-hardening`. It explicitly left the rest of the tree to a completion plan that was never written: "reading and instrument passes over the 55 % the audit did not open." Since that tip the tree has moved: downloads, artist re-credit (#188), transcode seek, widget fingerprint on finished downloads, context-menu DBID gating, shell/sync phase-2, music restore-path, SyncPlay tempo, MyVideos148. A second pass that only "finishes A2–A6 against `271a4c5`" would miss everything landed after, and would re-cite lines that have already moved. So this is a **fresh full-tree audit of current `main`**, with the closed H-items treated as already proven unless the cited code has drifted.

Size at plan time: **123** production modules, **~43,800** lines under `lib/kofin/` (core 5.3k, plugin 5.5k, service 6.0k, sync 18.4k, syncplay 4.2k, downloads 4.4k), **91** unit files / **~42,600** test lines. That is too much for one context window; the method below is built around bounded reading passes and a synthesis step, not "read everything once."

---

## 1. Discipline

Copied from the two audits that already worked here, and made binding.

1. **Pre-register before reading.** This document freezes the passes, the finding taxonomy, the oracles, and the "do not re-open" list. A new class of finding may be *added* in the report's Deviations section with a reason; a pass may not be dropped because it looks inconvenient.
2. **Every finding is a named ID with cited lines.** `H-`/`M-`/`R-`/`G-` plus a short slug, the file:line against the A0 pin, the failure it causes, the test that would have caught it, and whether it was verified by reading, by unit, against Kodi source, or live.
3. **No unevidenced claims.** "Looks racy" is a note, not a finding. A High that cites Kodi behaviour is checked against a 21.3 Omega (and, where the claim is versioned, Piers) checkout or a live box. A High that cites Jellyfin behaviour is checked against 10.11.11 and/or jf12 the same way `tests/live/jf12_*.py` already does.
4. **The transplant's oracle is L2, not the fork.** `test_sync_writers.py` across every gated schema (full-fidelity, byte-identical dumps, zero-orphan removal). Equivalence with jellyfin-kodi is a historical artefact (`tests/live/ab_diff.py`, movies only, day of the port). A new deviation from the fork needs its in-place note *and* an L2 pin; a missing pin on an existing "Deviation from the fork" comment is itself a finding.
5. **Live confirmation of Highs is part of the audit, not a follow-up.** The 08-29 pass changed three findings and closed one when it re-read and re-ran; that is the bar. Medium and below may stay read-verified if the trigger would break a real library.
6. **The jf12 media rule is load-bearing.** Disposable *server*, production *media*. No `DELETE /Items/{id}`, no library removal with file deletion, no write under a jf12 library path unless `readlink -f` proves it is local (today: `Phase One Test (2026)/`). A "gone item" is a generated file removed from a local directory plus a refresh.
7. **Credentials stay in `~/.config/kodi-drive/targets.env`.** Never `cat` that file, never paste a token, never commit a host. `kodi.log` at debug carries stream URLs with `api_key=`; scrub before any paste.
8. **Kodi fails silently.** A reading that concludes "this path is fine" without a test, a live gate, or a cited Kodi-source line is incomplete. Load the relevant `kodi-drive` skill before the pass that needs it (`kodi-orientation` once; then `kodi-database-writing`, `kodi-plugin-handles`, `kodi-addon-lifecycle`, `kodi-announcements`, `kodi-playback-resume`, `kodi-texture-cache`, `kodi-library-nodes`, `kodi-addon-manifest`, `kodi-performance`, `jellyfin-client` as the pass demands).
9. **Generally-useful Kodi findings go to kodi-drive, not CLAUDE.md.** A kofin-specific invariant that is easy to re-break goes in CLAUDE.md with its test. The audit records both as `G-` (kodi-drive contribute) or `C-` (CLAUDE.md constraint) so they do not vanish into the findings prose.

---

## 2. Scope

**In:** every `lib/kofin/**/*.py` module; the five entry points (`default.py`, `service.py`, `context_*.py`); `addon.xml`; `resources/settings.xml`; `resources/skins/`; English `strings.po` *contract* (quoted pairs, new ids, `PASSTHROUGH`); `tox.ini` / `.github/workflows/` / `mypy.ini`; `tools/i18n/{gen,validate,pocheck}.py`; the live harness under `tests/live/harness/` (it is how we inject faults, and a broken harness hides findings).

**Out:** `dist/`; `__pycache__`; the 26 generated locale *bodies* (the generator and `pocheck.py` are in; hand-editing a `.po` is not an audit question); gitignored `tests/live/results/` except as prior evidence to cite; `docs/` except as the ledger of what was already found and parked.

**Re-open only on drift.** H1–H14 from `docs/audit-fixes-plan.md` are closed. A0 re-grounds their line numbers; if the cited code is still the fix, the item stays closed. Parked items are **confirmed present, not rediscovered**: audit-F3 (music walk is one SQLite transaction — Tier 3 of the sync refactor), audit-F9 (a zero playlist listing empties `playlists/music/Kofin/` — needs two-poll state), M8 (dynamic-libraries W7).

**Name collisions.** F3/F9 mean different things in `audit-fixes-plan.md`, `boxsets-robustness-plan.md`, `widget-refresh-plan.md`, `healing-loops-plan.md`, and `benchmark-audit-plan.md`. This audit's IDs are prefixed `A3-` (wave + index) so they cannot be mixed with those ledgers. When a finding *is* a parked item, say so: "this is audit-fixes F3, still present at `full_sync.py:…`."

---

## 3. Finding taxonomy

| Prefix | Meaning | Bar to file it |
|---|---|---|
| **H** | High — data loss, wrong library, deadlock, auth/token leak, silent empty-success that deletes, shutdown past Kodi's 5 s grace | Cited lines **and** (unit or live or Kodi/Jellyfin source). Live confirmation before it enters the fix plan as High |
| **M** | Medium — wrong state that a Repair/Update can heal, a stall that is not a deadlock, a security property that needs a local reader (addon_data), a missing abort on a bounded retry | Cited lines; live optional |
| **R** | Robustness / tidy — unused import, one-liner correctness, transplant leftover, log noise, a test the tree should already have | Cited lines |
| **C** | New kofin constraint — easy to re-break, not yet in CLAUDE.md | Named test or live gate exists or is specified |
| **G** | Generally-useful Kodi behaviour, verified this session | `kodi-drive:contribute` item, not a kofin PR |
| **T** | Test / live-gate gap on a load-bearing rule that already has a CLAUDE.md entry | The rule and the missing oracle; not a code defect |

Severity inside H/M/R is the 08-29 scale (blast radius, then likelihood). A finding that is already a CLAUDE.md constraint with a green L1/L2 test is **not** re-filed; it is a `T-` only if the test is missing.

**Closed-as-read is allowed.** The 08-29 pass closed M5 after checking Kodi source. Record those in the report so the next audit does not re-open them.

---

## 4. Oracles (what "verified" means)

- **L0:** `tox` (black, mypy, ruff F/B/PLW0120, `pytest tests/unit`) green on the A0 pin, re-run after any speculative patch (the audit itself does not patch).
- **L1:** the existing unit file for the module, plus any new regression the finding specifies. Kodistubs + `tests/unit/fakes.py` / `synchost.py` / `transportserver.py`.
- **L2:** `tests/unit/test_sync_writers.py` on every gated schema. A writer finding that would move a dump row must say so, because the fix plan will need a before-set.
- **Identity set:** `tests/live/dump_diff.py` keyed dumps, `tests/live/node_snapshot.py` node trees, device-profile JSON, `S1-P1.0-before/` — used when a High would change rows or generated files; not re-captured for read-only findings.
- **Kodi source:** Omega 21.3 and Piers checkouts in the conventional places (`ref/kodi-omega-full`, `ref/kodi-piers-full`). Brief look, then ask; do not `find` from `$HOME`.
- **Jellyfin:** production 10.11.11 for scale; jf12 for policy/auth/empty-listing shapes. Existing probes: `tests/live/jf12_user_policy.py`, `jf12_mediasource_names.py`, `jf12_withdraw_access.py`.
- **Rigs:** native Omega on `P1D` (confirmed 2026-08-30: 21.3, JSON-RPC up). Profile `kofin-test` for dumps; `kofin-jf12` for mutating scenarios. Deploy this tree before A4 (`tools/dev-install.sh`) — the box currently has kofin 0.21.1. Piers flatpak and `BRAVIA`/`TAB` unconfirmed this session; use only where ARM cost or schema-live is the claim. SSH to P1D is `KODI_P1D_SSH` (custom key; helpers do not read it).

---

## 5. Method — six waves

Each wave writes findings into a running ledger (`docs/audit-report.md`, created at A0 as a stub with the pin and the empty tables). Waves do not edit production code. A wave that needs a live box says so in its exit checklist; reading waves may run in parallel via isolated explore agents, then a single synthesis pass merges IDs and drops duplicates.

### Wave A0 — Pin, inventory, closed-finding re-ground (half a day)

1. Record `git rev-parse HEAD`, addon version, `tox` counts, `wc -l` per package.
2. Mechanical greps, written into the report as raw lists (not findings yet):
   - `except Exception` / `except:` in `lib/kofin`
   - `xbmc.Player.stop` / `.pause` / `xbmc.sleep`
   - `NotifyAll` / `ipc.notify` callers vs `_REGISTRY` / `GUARDED`
   - `INSERT OR REPLACE` / `PRAGMA` / `%s`-interpolated SQL
   - module-level mutables (candidates for `RUF012`)
   - `Addon()` construction sites
   - `endOfDirectory` vs `LISTING_MODES`
   - `sslVerify` / `CERT_NONE` / `verify=`
   - `Deviation from the fork`
   - `TODO`/`FIXME`/`XXX`/`HACK` (expect zero in `lib/kofin`)
   - `register_secret` vs log-format sites that print URLs
3. Re-ground H1–H14 and parked F3/F9/M8 line numbers against HEAD. Drift of a *fix* (the code no longer matches the commit) is a finding; drift of a *line number* is a note.
4. Constraint coverage matrix: every CLAUDE.md "easy to re-break" bullet → existing L1/L2 test and/or live scenario ID, or `T-` for "untested". This is the cheapest way to find holes the previous audits already named.
5. Exit: pin committed to the report header; grep lists in an appendix; matrix started; tox green.

### Wave A1 — Mechanical whole-tree sweeps (half a day, can overlap A2)

These are cheap and have historically paid (M7 unused imports, R1 `for/else`, M1 IPC quotes, F4/F5 HTTP taxonomy). Each sweep either files findings or records "clean at HEAD."

| Sweep | Question | Primary files |
|---|---|---|
| IPC closed-world | Every `notify(` string is in `_REGISTRY`; every destructive/expensive method is in `GUARDED`; hex + HMAC still on the wire | `core/ipc.py`, `service/main.py` dispatch |
| IPC unguarded set | `PRECACHE_ART`, `SYNCPLAY_MENU`, `WHO_IS_WATCHING`, `ATTACH_SUBTITLE` — is "anyone on the JSON-RPC bus can fire these" still the intended contract? | `ipc.py`; kodi-drive `kodi-announcements` |
| Plugin handle | Every non-listing route closes the handle in `finally`; `LISTING_MODES` matches every `endOfDirectory` site | `plugin/router.py`, `browse.py`, `lyrics.py` |
| `reuselanguageinvoker` | Still under the pluginsource extension, not the addon root | `addon.xml` |
| Settings | No `<dependencies>` on a `list[string]`; who’s-watching empty=`all`; `sslVerify` restart | `settings.xml`, `settings_apply.py`, `whoswatching.py` |
| Context visibility | DBTYPE **and** `kofin.id`-or-DBID still both present; bitrates window property still the play-with-transcoding gate | `addon.xml` |
| Schema gate | Every `SUPPORTED` entry has a fixture + keyed constants (`test_sync_schema.py` already refuses the inverse) | `sync/schema.py`, `tests/fixtures/` |
| Empty-listing triad | The three homes still agree, and the views floor still sits **before** `SortedViews` | `views.py`, `boxsets.py`, `prune.py`/`downloader.py` |
| Fail-loud pager | `_get_items` still raises; `abandon_jobs` still releases the semaphore | `downloader.py` |
| `stop_player` | Zero `xbmc.Player.stop()`; pause left only inside SyncPlay and named | `kodirpc.py` + callers |
| Secret masking | Token / `Pw` / `api_key=` cannot survive `xbmc.log`; `register_secret` covers the live token | `core/log.py`, `http.py` debug line, `play.py` |
| i18n contract | Quoted help pairs; `_source.json` drift; `PASSTHROUGH` | `tools/i18n/`, `test_translations.py` |
| Nodes / playlists deletion gates | Video: `kofin` prefix. Music playlists: folder boundary, no prefix | `nodes/fs.py`, `playlists.py`, `views.py` |

Unguarded IPC and websocket `sslVerify` vs WSS (`core/ws.py` has no `sslopt`) are the two A1 items most likely to promote to High; they were not in the 08-29 set.

### Wave A2 — Blast-radius reading (1–2 days; this is the audit)

Read failure paths, not happy paths. For each module: entry points, what happens on empty/403/timeout/abort, which lock is held, which process it runs in, which test file is supposed to pin it. File findings as you go; do not wait for the wave to end.

Read in this order (blast radius, not package size):

| Pass | Modules | Why first | Skills |
|---|---|---|---|
| **A2.1 Deletion and empty-success** | `views.py`, `boxsets.py`, `prune.py`, `downloader.py` (`get_id_etag_map`, `_get_items`), `playlists.py` (audit-F9), `settings_apply.py` (`LOAD_CANARY`, `GUARDED_CLEARS`), `clean.py`, `nodes/fs.py` | An empty listing that looks like success wipes a library | `kodi-library-data`, `jellyfin-client` |
| **A2.2 Shutdown and the GIL** | `http.py` `run_ladder` abort, `workers.py` + `database_lock` (HTTP inside the lock), `downloader.abandon_jobs`, `service/main.py` stop, `syncplay/manager.stop`, `downloads/manager.py` stop, `kodirpc.stop_player` | A thread in the retry ladder outlives the 5 s grace; `Player.stop` deadlocks | `kodi-addon-lifecycle`, `docs/library-thread-stop.md` |
| **A2.3 Auth, TLS, logs, remote** | `auth.py`, `http.py`/`stdhttp.py`, `ws.py` (sslopt), `log.py`, `settings.xml` hidden creds, `service/remote.py` (`executebuiltin` map, `PlayMedia`) | Token in logs; WSS/HTTP verify split; a remote session is the household | `jellyfin-client`, `kodi-logs` |
| **A2.4 Playback resolve** | `plugin/play.py`, `plugin/listitems.py` (zero resume stamp), `downloads/repoint.py` + `play.py` `resolve_downloaded`, `deviceprofile.py` TranscodingProfile vs DirectPlay lists, `core/streams.py` / `plugin/streams.py` | Wrong stream, wrong file, av1 copy after a refuse, SyncPlay follower streaming a download | `kodi-playback-resume`, `kodi-inputstream` |
| **A2.5 Music rows** | `kodidb/music.py`, `queries_music.py` (`discography`, `idPath` restore), `writers/music.py` (`relink_content`, `prune_song_credits`), `library.py` `prune_orphan_paths` | Four empty albums on the Bravia was this class of bug | `kodi-database-writing` |
| **A2.6 Cross-process state** | `core/state.py` (every property; overlapping generations), `core/ipc.py` nonce file, plugin-handle routes that only fire IPC | A property is shared with the dying generation; a forgotten handle hangs Kodi | `kodi-plugin-handles`, `kodi-addon-lifecycle` |

### Wave A3 — Remaining reading, by package (2–3 days; parallelisable)

Everything A2 did not open. Each pass is one agent with a closed prompt (the package map, the CLAUDE.md constraints that apply, the test file names, "file findings, do not edit"). The parent synthesises.

| Pass | Package | Largest files | Particular questions |
|---|---|---|---|
| **A3.1** | `sync/` pipeline remainder | `library.py` (~2082), `full_sync.py`, `workers.py`, `changefeed.py`, `widgetstate.py`, `refresh.py` | Tick vs command table; watermark honesty; widget fingerprint (`tag_link` / favourite still a known S-P2.3a gap); FastSync still unconditional on screensaver; audit-F3 music transaction still one `commit` |
| **A3.2** | `sync/` transplant | `writers/{movies,tvshows,music}.py`, `kodidb/*`, `fields.py`, `obj.py`, `queries*.py` | Every "Deviation from the fork" has an L2 pin; boxset NULL checksum; stem-named versions still 40400; extras `itemType` from schema; `INSERT OR REPLACE` on `discography` |
| **A3.3** | `plugin/` remainder | `browse.py` (~1314), `actions.py`, `account.py`, `adduser.py`, `playall.py` | Interactive HTTP profile; People-off unbounded listings; who’s-watching `is_enabled`; handle-close on every action |
| **A3.4** | `service/` remainder | `main.py` (~1460), `player.py`, `segments.py`, `chapters.py`, `latesubs.py`, `artcache.py`, `kodiuserdata.py` | Claim vs lyrics race (onPlayBackStarted *above* `_claim`); chapter texture schema gate; people-cache reset still on `OnCleanFinished`; reporter off the callback thread |
| **A3.5** | `downloads/` | `manager.py` (~1690), `store.py`, `files.py`, `auto.py`, `pending.py`, `quality.py`, `probe.py` | Path join / `..` after sanitize; stop discipline; auto-next vs playing; pending userdata replay; one-download-at-a-time vs server throttle |
| **A3.6** | `syncplay/` | `manager.py` (~1574), `playback.py`, `tempo.py` (~1004), `timesync.py` | Command-only vs tempo pulses (no SetTempo ladder); leave timeout still bounded; pause() stall named; transcode seek landing check |
| **A3.7** | Manifest / skin / i18n / CI | `addon.xml`, `settings.xml`, `script-kofin-skip.xml`, `tox.ini`, workflows | Optional deps still optional; skip overlay callbacks on a Monitor wait; CI still matches tox |

### Wave A4 — Live confirmation of Highs (1–2 days, after A2/A3 freeze a High list)

**Gate, already satisfied for Omega:** `P1D` answers JSON-RPC on profile `kofin-test`. Before the first live High: `tools/dev-install.sh` so the box is this tree, not 0.21.1; bounce the add-on; re-read `Addons.GetAddonDetails` version. Switch to `kofin-jf12` only for mutating scenarios, and confirm the switch (`Profiles.GetCurrentProfile '{}'` — `LoadProfile` returning `"OK"` is not that).

Do not wait for A3 to finish if A2 already produced Highs whose trigger is safe (policy change, generated local file, blackhole, service bounce). Each High gets a short scenario in the report (not yet in `docs/testing-plan.md` — that travels with the fix). Reuse existing harnesses: `tests/live/harness/`, `dump_diff.py`, `node_snapshot.py`, `transportserver.py`, `tools/perfprobe.py`. Promotion/demotion happens here: a read-High that the box does not reproduce becomes M or is closed, with the attempt recorded.

**Do not** use this wave to clear the live-test backlog (S2.10, S-versions, W4–W7, S4.7, …). Those are a different work order. If a High's cheapest proof *is* one of those unpaid gates, run that gate and say so.

### Wave A5 — Freeze, contribute, fix plan (half a day)

1. Freeze `docs/audit-report.md`: pin, taxonomy, findings table, closed-as-read, parked-still-present, Deviations from this plan, constraint matrix, kodi-drive contribute list.
2. One line per paragraph (`tools/unwrap_md.py`).
3. `kodi-drive:contribute` for every `G-` that was actually run (the 08-29 kodi-announcements thread correction is still owed if it was never filed).
4. Write `docs/audit-fixes-plan.md` as a *new dated successor* (do not overwrite the 08-29 file). Same shape: blast radius first, one commit per item, oracle per item, stacked before whatever is currently in flight, L2 dumps as the gate inside `sync/`/`syncplay/`. Park anything that needs a design the finding does not supply (the 08-29 F9 rule).
5. CLAUDE.md gains only new `C-` constraints, each with its test name.

---

## 6. How to run this with agents

The tree does not fit in one window. The parent owns the ledger and the live box; children only read.

- **A0/A1:** one session (greps are whole-tree and cheap).
- **A2:** sequential in the parent — blast-radius passes share invariants (empty-listing, abort, TLS) and should not fork.
- **A3:** up to three `explore` (or `worktree`-isolated general-purpose) agents at a time, each given: the pass id, the file list, the CLAUDE.md constraints that apply, the test files, "cite lines against HEAD, do not edit, do not invent severity." Parent merges, de-dupes against A1/A2, assigns IDs.
- **A4:** parent only — live Kodi, credentials, jf12 media rule.
- **A5:** parent writes the two docs.

A child that wants to "just fix it" is out of contract. Fixes start after the report is frozen.

---

## 7. What success looks like

- A dated `docs/audit-report.md` whose Highs have been confirmed or demoted, whose parked 08-29 items are confirmed still present with current lines, and whose constraint matrix has a test or a `T-` for every CLAUDE.md bullet.
- A dated `docs/audit-fixes-plan.md` that is a stacked, revertible work order — or an explicit "no Highs, Mediums as drive-by" if that is the truth.
- Zero production edits in the audit commits themselves (docs + maybe a grep helper under `tests/live/harness/` if A4 needs one).
- kodi-drive PRs or issues for `G-` items that were actually verified.
- `tox` still green; the identity set untouched (the audit did not change rows).

This is **not** success: a long list of style nits, a re-statement of CLAUDE.md, a re-open of H2/H3/H4, or an audit that never touches `downloads/` and `syncplay/tempo.py`.

---

## 8. Effort and sequencing against other work

Roughly **one focused week**: A0+A1 half a day, A2 one to two days, A3 two to three days (wall-clock less if parallelised), A4 one to two days, A5 half a day. It should land **before** the next structural refactor that would force identity-oracle re-baselines (the 08-29 hardening existed specifically to go before shell phase 2 for that reason). If shell phase 2 or a writer change is already in flight, stack the audit report on `main` and the fix plan on that branch's tip, same as last time.

Out of scope for the audit week, recorded so they are not silently absorbed: unpaid live gates (S2.10 hole-heal, S-versions, W4–W7, S4.7, S-P2.3c/f/g/h, S-H8 Bravia); implementing audit-F3 (sync refactor Tier 3); designing audit-F9 (two-poll playlist floor); the benchmark re-run.

---

## 9. Open questions (resolved by the recommended defaults)

1. **Fresh full-tree vs "finish A2–A6 of 08-29"?** Fresh full-tree of current `main`. The unread 55 % is in scope; so is everything landed since; closed H-items are not re-litigated without drift.
2. **Findings only, or findings + fix plan?** Both, sequenced: freeze the report, then write the fix plan. Mixing them is how a High gets "fixed" in passing without an oracle.
3. **Live Highs in the audit week?** Yes. Medium and below may stay read-verified.
4. **Commit the report even if the High list is empty?** Yes — an empty High table with a filled constraint matrix is the most useful outcome this tree can have.
