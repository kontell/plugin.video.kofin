# Performance & hardening plan (from the 2026-08-06 audit)

Scope: fix the three measured complaints (browse latency, direct-play startup, actor thumbnails) and the confirmed hardening defects from the code sweep, in four waves ordered by payoff-per-risk. Every perf item carries a measured baseline and an acceptance target; every hardening item names its failure scenario. Baselines were taken on the kofin-test profile (Debian Kodi 21.3, 1,766-movie library, LAN HTTPS): movies-all listing 14.6–16.3 s, static node menu 1.5–1.8 s, direct-play click-to-frame 2.76 s (1.32 s import constant, 0.40 s sidecar subtitle), actor thumb ~110 ms each cold with 102/19,661 cached.

Ground rules for the whole plan:

- The transplant boundary holds: `sync/` and `syncplay/` changes below are named defect fixes on failure paths, never semantic "improvements"; the L2 writer suite (byte-identical dumps) gates each one.
- Perf claims are re-measured with the committed probe (W1.0) before/after, and the numbers go in the PR description.
- New settings are plain bools (never a `<dependencies>` on a `list[string]` — the Omega unregister bug), and new strings need a full Kodi restart during dev.
- Do not regress the standing constraints: wake-time FastSync stays unconditional, no blanket widget refreshes, `views_hash` untouched (no node-tree changes here).

## Wave 1 — hot-path quick wins (small, independent PRs)

### W1.0 Commit the measurement harness

`tools/perfprobe.py`: the timed `Files.GetDirectory` runner with `_cb` cache-buster plus the log-timeline extractor used in the audit, so every later PR states before/after numbers from the same instrument. Dev-tooling only, no addon changes.

### W1.1 Hoist the per-item `Addon()` out of listing builds

Measured: `listitems.resume_of` → `settings.adjusted_resume` → `resume_offset()` constructs a fresh `xbmcaddon.Addon` per item (`core/settings.py:20`), ~2.9 ms each — ~5 s of the 15 s movies-all listing.

Change: `browse._add_items` (and `_extras_node`) read `settings.resume_offset()` once per request and pass it down; `listitems.resume_of(item, offset=None)` and `listitems.build(item, server, resume_seconds=None, resume_offset=None)` accept the precomputed value, reading the setting only when the caller did not (play.py's single item, existing tests). Explicit data flow — no caching, so the settings.py staleness comment stays true.

Target: movies-all ≤ 10 s (build phase 5.4 s → < 1 s). Tests: unit for the new parameters; probe re-run.

### W1.2 Lazy route imports and deferred `requests`

Measured: invoker reuse never happens on this build (probe-verified; see memory `kodi-debian-no-invoker-reuse`), so `router._handlers()` importing all ten handler modules — and through browse→api→http the whole `requests` tree (0.9–1.6 s) — is paid on every click, including static node menus that make no network call.

Change: replace `_handlers()` with a declarative `ROUTES: {mode: ("kofin.plugin.browse", "browse")}` table resolved via `importlib` per dispatch; move `import requests` inside `Http` (local import in `session()`/`request()`, `TYPE_CHECKING` guard for annotations) so importing `core/http.py` is free until a request fires.

Target: static node menu ≤ 0.6 s; every route (play included) sheds the unused-module share. Tests: unit asserting each ROUTES entry resolves; mypy stays green; probe re-run.

### W1.3 Fail-fast interactive HTTP profile

Measured/traced: browse and play calls inherit `retries=3` × (6 s connect + backoff) — an unreachable server makes the root listing take ~54 s (views + sessions serially) and a play attempt hang ~27 s before the failure toast.

Change: `Api` grows an `interactive` construction flag (browse/play/context use it) that applies `retries=1` and the existing timeout to its calls. Service/sync callers keep current behavior.

Target: offline root render < 10 s, offline play failure toast < 10 s. Tests: unit with a fake session that always times out, asserting attempt counts per profile.

### W1.4 Root render without the `/Sessions` round trip

`_who_is_watching_label` (`browse.py:333`) costs a server call per root render. The service already knows the additional-user set (it restores them on connect): publish the names to a window property in `core/state.py` (updated on connect and on add/remove user), and have root read the property. Root falls back to the base label when the property is empty.

### W1.5 POST retry semantics (hardening, early because damage is silent)

Traced: `Http.request` retries every method; a lost response replays non-idempotent POSTs — duplicate SyncPlay groups/queue entries, group double-advance, duplicate playback-history rows, and `PlaybackInfo` (`AutoOpenLiveStream=True`) leaking a second transcode session nothing closes.

Change: default retries by method in `Http.request` — GET/HEAD 3, DELETE 1, POST 0 — with the explicit `retries` parameter still winning. Audit every `Api.post` caller and opt back in only where replay is provably safe (none identified so far; document the table in the PR).

Tests: unit asserting per-method defaults and override; live SyncPlay smoke (create/join/queue) unchanged.

### W1.6 Bytecode-cache experiment (timeboxed)

Kodi's Python skips bytecode caching, which is most of the `import requests` cost. Experiment: `sys.dont_write_bytecode = False` at the top of `default.py`/`service.py`, measure whether `__pycache__` appears beside the addon and `script.module.requests` and what it saves per click; check behavior on an addon update (stale-pyc invalidation is mtime/size-based, should be safe) and on one non-Linux box before adopting. Keep or revert on the numbers; this is the only Wave-1 item allowed to fail cheaply.

## Wave 2 — structural browse/play costs and the top data-integrity fixes

### W2.1 Split `MediaStreams` out of whole-library browse queries

Measured: the field is 1.4 s vs 0.47 s server TTFB and 12.7 MB vs 3.5 MB on movies-all; `_fill_music` never reads it at all.

Change: two field lists in `browse.py` — bounded listings (Limit≤25 nodes, one season's episodes) keep `MediaStreams`; unbounded ones (all/unwatched/favorites/sets/genre drill-downs, generic children) and every music query drop it. Rows on unbounded listings lose codec/HDR flags — stated in the PR and README; add a `browseStreamDetails` bool later only if someone misses it.

Target: movies-all ≤ 3.5 s end-to-end after W1.1+W1.2. Tests: unit on the query builder; probe re-run.

### W2.2 Session reporting off Kodi's callback threads

Traced: `onPlayBackStarted`/`onPlayBackStopped` and the `Player.OnPlay` backfill run retrying HTTP (up to ~27 s offline) on the announcement thread every addon shares (`service/player.py:583,667,1372`; `service/main.py:488`).

Change: a single FIFO reporter worker owned by the service player (the `kodiuserdata.py` pattern): callbacks capture player state synchronously (position at stop must be read at event time), enqueue, return; the worker owns claim → playing → progress → stopped ordering. Live gate: with the server blackholed (iptables DROP), start/stop playback and verify Kodi stays responsive and another addon's player callback fires promptly.

W2.5 revision, recorded after live verification: `ping_timeout` is poison against this server — its own 2-minute keepalive ping corrupts websocket-client's pong bookkeeping and every healthy connection died on an exact 130 s cycle on both boxes — and `reconnect=` invokes neither `on_error` nor `on_close`, so drops were silent. The landed design: `run_forever(ping_interval=10)` only, the run loop owns reconnection, and half-open detection is app-level (the server echoes every KeepAlive; 75 s of inbound silence recycles the socket — measured detection 54 s under a symmetric blackhole, 0 reconnects in 300 s healthy). Firewall-based gates must scope to tcp/443 and carry a deadman timer: an unscoped drop to the server also kills the NFS mount this repo lives on.

### W2.3 `sync.json` atomic writes and loud torn reads

Traced: `save_sync` is open+write (no rename) while writer threads call `get_sync` per item; a torn read yields an empty whitelist and writers silently skip items while the watermark advances (`sync/db.py:170`, `sync/fields.py:539`).

Change (transplant, minimal diff): `save_sync` writes tmp + `os.replace`; `get_sync` distinguishes file-absent (defaults, as today) from unparseable (raise `LibraryException`) so `find_library` fails loudly into the existing unapplied/recovery path — trace and test that path as part of the PR. Note separately (not in this change): `find_library`'s per-item `get_sync()` re-read is also a sync-perf smell; leave it unless the transplant owner signs off.

### W2.4 Rollback on exception and commit ordering

Traced: `Database.__exit__` commits partial transactions on the exception path (no `rollback()` exists in the repo) and closes outside `finally` (`sync/db.py:118`); the writer `with` blocks commit kofin.db before MyVideos (`sync/library.py:2233` et al.), so a failed Kodi commit strands mappings/checksums that Etag-gated walks then skip forever.

Change: `__exit__` rolls back when `exc_type` is set, closes in `finally`; swap the `with` nesting so kodidb commits before kofin.db at every site (`library.py`, `full_sync.py`). Pre-change subtask: trace when sync.json queue entries are popped relative to commit, so a rollback can never lose an item the queue no longer holds — write the finding into the PR. Gate: full L2 suite (dumps must stay byte-identical) plus a new unit that raises mid-batch and asserts rollback-to-last-periodic-commit.

### W2.5 Websocket liveness

Traced: no `ping_timeout`, so a half-open socket is never detected (CLAUDE.md treats wake-time FastSync as the only cover — keep FastSync unconditional regardless); `_on_ws_connected` does seconds of blocking work on the socket's own read loop; no connect timeout.

Change: `run_forever(ping_interval=10, ping_timeout=5, …)`; move the post-connect work (capabilities, restore users, FastSync enqueue) onto a named worker thread the handler merely spawns; set a connect timeout via `websocket.setdefaulttimeout` with an in-place comment. Live gate: DROP the server mid-idle, confirm disconnect detection ≤ ~15 s and catch-up on reconnect.

### W2.6 Play-path concurrency

Measured: sidecar subtitles fetch sequentially before `setResolvedUrl` (0.4 s each, ≤ 8 files → up to ~3 s), and item + PlaybackInfo + segments are serial round trips.

Change (as landed, correcting this entry's original claim): the item GET stays ahead of PlaybackInfo — `StartTimeTicks` needs the resume position, which in the server-resume case only the item DTO knows, so "PlaybackInfo needs only the id" was wrong. What overlaps: the segments prefetch runs on its own thread from the moment the item is known (beside PlaybackInfo and the subtitle fetches), and `subtitles.localize` fetches sidecars concurrently (order-stable results, per-file timeout and URL fallback unchanged; the MAX_FILES cap now bounds attempts rather than successes). Requests' session is pool-backed and thread-safe for this use.

Target: plugin resolve phase ≤ 0.7 s on LAN after W1.2 (from 1.94 s). Tests: unit for order stability and fallback; live timeline re-run, including a 2+-sidecar item.

## Wave 3 — the dialog fix, restart safety, and the remaining confirmed defects

### W3.1 Actor-art texture pre-seeding

Measured: Kodi's fetch+decode+cache pipeline costs ~110 ms per actor cold vs ~1 ms warm; 19,661 actor URLs, 102 cached — every first info dialog pays cast-size × pipeline. The URLs themselves are correct (400 px, ~64 KB, server resize 30–65 ms) — do not touch the URL shape (a change would roll out incoherently across Etag-gated items).

Change: a service-side seeder module reusing the chapter machinery (`sync/kodidb/texture.py`, schema-gated, keys verified against `schema.py` constants — actor art is raw-URL-keyed on both gated versions, unlike wrapped chapter keys). Work list = actor thumb URLs in MyVideos not present in Textures; runs as an idle-time trickle (global idle > 60 s, stops on playback, bounded batch per wake) plus a "Pre-cache actor art now" settings button. New bool setting, default off, help text stating the disk estimate (~0.4–1.3 GB for this library). Seeded files are durable by design; Kodi's own texture GC prunes unused entries.

Acceptance: after a manual run, first-open info dialogs show cast instantly (probe: the 8-actor cold total drops from ~0.9 s to ~0.01 s); no seeding activity during playback.

### W3.2 Restart teardown safety

Traced: `_shutdown` joins the library thread for 15 s, proceeds anyway, and `state.clear_all()` clears `PROP_SYNC_STOP` — reviving the orphan's stop-guards while a second `Library` (second lock domain) starts; `FullSync`'s class-level Borg then latches `running=True` and kills the new sync until Kodi restarts (`service/main.py:544`, `sync/full_sync.py:155`).

Change: loop-join with abort check until the library thread is actually dead (log progress every 15 s) before `clear_all`; replace the Borg dict with per-instance state owned by `Library` (removing a module-global the restart design forbids — justify at the site); stop/join the named worker threads (`_chapter_sweep` holds a Textures cursor) in `_shutdown`. Live gate: service restart during an active full sync, twice in a row.

### W3.3 IPC nonce for destructive commands

Traced: `onNotification` trusts the caller-supplied `sender` string, so any addon or JSON-RPC client can forge `RemoveLibrary`/`RepairLibrary` (`service/main.py:498`, `core/ipc.py:53`).

Change: service writes a per-generation nonce to a window property (argued into `core/state.py`); plugin/context senders include it; the service verifies it for destructive messages only (RemoveLibrary, RepairLibrary, CleanDatabases, AuthChanged, Restart) and logs+drops mismatches. Also wrap `ipc.decode` in a guard (malformed payloads currently raise on the notification thread). Live gate: forged NotifyAll via curl is refused; real settings-button flows still work.

### W3.4 Downloader offline behavior

Traced: a `GetItemWorker` hitting `ServerUnreachable` discards its 50-id chunk and dies; `Library.service()` immediately respawns workers against the still-full queue — a permanent 3-thread retry storm with silent item loss (`sync/downloader.py:748`, `sync/library.py:870`).

Change (transplant, minimal): re-queue the chunk before the worker exits; add a backoff timestamp the spawn path respects (no new workers for N minutes after a ServerUnreachable death). Tests: unit with a failing fake server asserting chunk retention and spawn suppression.

### W3.5 Smalls batch

One PR of independently-testable fixes: guard `int()` parses in `service/remote.py`; `Database.__enter__` closes the connection if WAL/setup raises; `UserDataWorker`/`RemovedWorker` failures schedule the recovery prune instead of only logging (`library.py:2371,2535`); close worker `Http` sessions in `finally`; cap `new_content` accumulation with a flush threshold; backdrop writer single-flight + tolerate read-only installs; suppress urllib3's per-request insecure warning when `sslVerify=false` and note the plaintext-token reality in the README.

### W3.6 play.json claim race (design-first, small)

Traced: `push_play_item`/`claim_play_item` are cross-process read-modify-write on one window property — a race loses or resurrects entries (`core/state.py:92`). Fix direction: replace the shared list with single-writer keys or an `fcntl`-locked file in addon_data; pick in a short design note in the PR (window properties offer no CAS). Rare in practice, so it rides Wave 3.

## Wave 4 — revised after re-measuring (2026-08-07)

The wave was written when a click cost 1.5–16 s and the structural options looked necessary. Waves 1–3 changed the arithmetic, so the wave was re-derived from fresh measurements on both boxes rather than executed as drafted. What the measurements showed:

| Listing | Local (Omega, load ~3.5) | Bravia (Kodi 22, settled) | What it isolates |
| --- | --- | --- | --- |
| Node menu, 8 rows, **no network** | 0.82–0.96 s | 0.42–0.61 s | interpreter + kofin imports |
| Root, 11–12 rows, one call | 1.9–2.2 s | 1.15–1.46 s | + `requests` import + a request |
| Recent, 25 rows | 1.8–2.5 s | 1.12–1.43 s | same shape |
| Movies-all, 1,766 rows | 2.5–2.9 s | 2.7–2.8 s | + 3.5 MB fetch + 1,766 builds |

The gap between a network-free click and any networked one is ~1 s on both boxes, and it is one import. Measured inside Kodi's own Python: `import requests` 1.112 s (Omega) / 0.929 s (Kodi 22), against `http.client`, `ssl`, `json` and `urllib` at 0.000 s — they are already loaded before the addon runs. Kodi 22's native bytecode caching does not help, because the cost is executing the package's module bodies rather than compiling them; both boxes carry `.pyc` for requests and both still pay it.

A caution for anyone re-running these: a box measured within a few minutes of a Kodi restart reads 2–3x slower while the service completes its startup sync (the Bravia measured 6.4–7.6 s cold against 2.7 s settled for the same listing). Let it go quiet first — the last kofin log line stops moving — and note the load average.

### W4.0 A standard-library transport for the plugin process — **done, replaced W4.2/W4.3**

`core/stdhttp.py`: the exact surface `Api` uses (`request`/`close`, the same error taxonomy, the same per-method retry budget) over `http.client`, with one connection kept alive across an invocation's calls so the saved import is not handed back as TLS handshakes. The plugin process uses it through `http.plugin_transport`; the service and the sync stack keep `requests`, where a long-lived process pays the import once and the proven code path is worth more than a second's saving that nobody waits for.

Measured, same conditions before and after:

| | Before | After |
| --- | --- | --- |
| Root, Bravia | 1.15–1.46 s | **0.79–0.90 s** |
| Recent (25), Bravia | 1.12–1.43 s | **0.79–0.84 s** |
| Movies-all, Bravia | 2.7–2.8 s | **2.37–2.48 s** |
| Recent (25), local | 1.8–2.5 s | **0.75–1.20 s** |
| Movies-all, local | 2.5–2.9 s | **1.55–1.64 s** |
| Click-to-first-frame, local | ~2.7 s | **1.81–1.95 s** |

The check that it did what it claims: on the Bravia a *networked* listing (0.79 s) now sits within ~0.2 s of the *network-free* node menu (0.55–0.76 s), so the import is gone from the click path and what remains is the request itself.

### W4.1 Browse listing cache — deferred, and rescoped if it ever returns

The case has thinned. A bounded listing is now ~0.8 s on both boxes, of which a cache could save perhaps 0.2 s; only the whole-library listing has real headroom (2.4 s on the TV, ~1.8 s of it fetch and build). Against that: a staleness policy, event-driven invalidation, disk, and a reversal of the "dynamic listings are always live" stance that the rest of the addon is built on. Not worth it for one listing kind. If it is ever revisited, scope it to unbounded listings only and keep continue-watching, next-up, in-progress and recent permanently live.

### W4.2 Warm-service handle forwarding — dropped

Its justification was eliminating the per-click interpreter constant. That constant is now 0.42–0.61 s on the TV and 0.82–0.96 s on a *loaded* laptop, and the wave's own decision rule was "prototype only if Waves 1–2 leave click latency above ~1 s on the target boxes". They do not. Cross-process handle forwarding, a socket server, a fallback path for a service that is down, and the lifecycle questions underneath are a poor trade for a sub-second saving in a codebase whose value is that it can be read.

### W4.3 Skip the item GET on library plays — dropped

~90 ms of a click-to-frame that is now 1.8–1.9 s locally, against changes to path identity, SyncPlay start ticks, the context transcode and the extras path. W4.0 took ten times as much off the same route for less risk.

### W4.4 Upstream invoker-reuse follow-up — file it, expect nothing

Confirmed in Kodi's source that plugin invocations do pass the flag: `CScriptRunner::ExecuteScript` reads `reuselanguageinvoker` from the addon's ExtraInfo and hands it to `CScriptInvocationManager::ExecuteAsync`, and `CLanguageInvokerThread::Reuseable` gates on `!m_bStop && m_reusable && GetState() == InvokerStateScriptDone && m_script == script`. Yet neither test box ever reuses — no "Reusing LanguageInvokerThread" line has appeared on either — so the cause is downstream in invoker state or lifecycle and needs a debug build to pin. Cheap to report with the minimal probe from the audit; not on kofin's critical path, and W4.0 makes the addon much less dependent on the answer. If reuse ever did work, every import cost in this document would vanish at once.

## Acceptance summary

| Metric (this box) | Baseline | After W1 (measured) | After W2 (measured) |
| --- | --- | --- | --- |
| Static node menu | 1.5–1.8 s | ~0.68 s avg | — |
| Movies-all (1,766) | 14.6–16.3 s | 3.3–3.4 s | **1.6–1.7 s** |
| Movies-all, Bravia (Kodi 22, Android) | 22.5–22.7 s | 9.8–10.3 s | (re-measure on deploy) |
| Offline root render | ~54 s | < 10 s (computed) | — |
| Direct-play resolve (plugin phase) | 1.94 s | ~1.0 s | **0.68 s** |
| Click-to-frame (direct play, LAN) | 2.76 s | 2.28–2.67 s | **~1.2 s** (1.81–1.95 s on a loaded box after W4.0) |
| Info-dialog cast (8 actors, first open) | ~0.9 s | — | **0.03 s seeded / 0.99 s unseeded control (W3.1, measured)** |

Wave 3 outcome, recorded: all six items landed and were live-gated (#92-#97). Two defects surfaced only under live running and are now regression-tested — the seeder's work list did not advance past its first page (186 images, then a claim of completion), and its two entry points duplicated every fetch. The IPC guard's secret lives in a 0600 file rather than a window property because JSON-RPC can read window properties, which is the channel being closed; forged RemoveLibrary/Restart were refused live on both boxes while genuine signed commands were accepted. The play queue became a directory of claimable files (unlink-as-claim) rather than an fcntl lock, because `addon.xml` declares platform `all`.

W1.6 outcome, recorded: measured and not adopted — Kodi 22 writes bytecode caches natively (the flag is a no-op there) and the Debian box showed no warm-condition change; details on PR #78.

Every PR: tox green (black, mypy, pytest), L2 suite untouched-byte-identical where sync/ is involved, probe numbers in the description, live gates as named per item.
