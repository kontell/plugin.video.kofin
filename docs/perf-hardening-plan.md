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

Change: a small executor in `plugin/play.py` — kick the segments fetch and the item GET concurrently with PlaybackInfo (PlaybackInfo needs only the id; `stream_url` joins on the item afterward), and `subtitles.localize` fetches sidecars concurrently (order-stable results, per-file timeout and URL fallback unchanged). Requests' session is pool-backed and thread-safe for this use.

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

## Wave 4 — structural options (design doc first, decide after re-measuring)

### W4.1 Browse listing cache (JellyCon pattern) — `docs/browse-cache-plan.md`

Serve cached parsed listings instantly, revalidate in the background, `Container.Refresh` on change. Kofin can do better than JellyCon: the service already watches the changefeed, so invalidation can be event-driven instead of TTL-guessed. The doc must settle: cacheable set (structural menus, all/genres/sets/artists/albums) vs deliberately-live listings (continue watching, next up, in-progress, recent — never cached); storage (pickled DTO list keyed on user|server|url in addon_data); playstate staleness on cached whole-library listings; eviction. This changes the "dynamic listings are always live" stance, so it ships only with that doc agreed.

### W4.2 Warm-service handle forwarding (Emby NG pattern) — prototype-gated

The plugin stub forwards its handle over a local socket to the always-running service, which builds the listing with warm caches and keep-alive sessions — eliminating the per-click interpreter constant entirely (Emby NG proves cross-process handle use works). Large change with portability and lifecycle questions (service down → stub falls back to today's in-process path). Decision rule: prototype only if Waves 1–2 leave click latency above ~1 s on the target boxes; measure a stub round trip first (~expect < 150 ms).

### W4.3 Skip the item GET on library plays

Persist the DTO fields play needs into kofin.db at sync time (or read them from Kodi's own row via dbid) so library-initiated direct play resolves with PlaybackInfo alone (~90 ms LAN, more WAN). Touches path identity and several flows (SyncPlay startticks, context transcode, extras without dbid keep the GET) — design note first, after W2.6 lands.

### W4.4 Upstream invoker-reuse follow-up

Reproduce the no-reuse behavior on a stock build (Bravia/Piers via ADB, or LibreELEC) with the minimal probe from the audit; if it reproduces there, file the xbmc issue with the probe as the repro. Kofin's own mitigation (W1.2/W1.6) does not depend on the outcome.

## Acceptance summary

| Metric (this box) | Baseline | After W1 | After W2 |
| --- | --- | --- | --- |
| Static node menu | 1.5–1.8 s | ≤ 0.6 s | — |
| Movies-all (1,766) | 14.6–16.3 s | ≤ 10 s | ≤ 3.5 s |
| Offline root render | ~54 s | < 10 s | — |
| Direct-play resolve (plugin phase) | 1.94 s | ~1.0 s | ≤ 0.7 s |
| Click-to-frame (direct play, LAN) | 2.76 s | — | ≤ 1.6 s |
| Info-dialog cast (8 actors, first open) | ~0.9 s | — | ~0.01 s after W3.1 seed |

Every PR: tox green (black, mypy, pytest), L2 suite untouched-byte-identical where sync/ is involved, probe numbers in the description, live gates as named per item.
