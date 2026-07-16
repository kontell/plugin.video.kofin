# plugin.video.kofin — rewrite research

Date: 2026-07-16 Scope: whether and how to rewrite `jellyfin-kodi` (+ its companion server plugin `jellyfin-plugin-kodisyncqueue`) as a fresh addon, `plugin.video.kofin`. Sources examined: `ref/jellyfin-kodi` (kontell fork, branch `combined/syncplay-sync`, incl. `docs/sync-improvement-plan.md` and `ref/syncplay-report.md`), `ref/jellyfin-plugin-kodisyncqueue` (v15, Jellyfin 10.11), `ref/jellyfin` (server 10.11), `ref/kodi-omega-full` + `ref/kodi-piers-full` (Kodi 21/22 source), `ref/service.upnext`, `dev/pvr.kofin`.

---

## 1. Verdict

**Rewrite the shell, transplant the organs.** A fresh `plugin.video.kofin` is the right call — but not a from-scratch one. The parts you want changed (settings, lifecycle, login, playback decisions, menus, dialogs) are exactly the addon's *shell*, which is also where the 2015-era Python-2 heritage is thickest. The parts that are genuinely hard to get right — the Kodi SQLite writers, the incremental sync pipeline, SyncPlay — were all recently hardened in the fork, have tests, and port over as packages.

| Disposition | What | Why |
|---|---|---|
| **Port nearly verbatim** | `objects/` (Kodi DB writers + `obj_map.json` mapping), `database/` (jellyfin.db + discovery, with fork's indexes), sync pipeline (`library.py`/`downloader.py`/`full_sync.py` with phase-1 fixes), `syncplay/` package, segments logic, websocket client | Years of edge cases + fresh fixes + tests; the riskiest code to reinvent |
| **Rewrite** | entry points, service lifecycle, settings (all of it), connection/login, `playutils` (device profile), dialogs (delete most), views regen policy, helpers/state/IPC | This is where the UX spec and the maintainability problems live |
| **Drop** | native/direct-paths mode, live TV browse+playback, multi-server, custom resume/source/audio-sub/transcode dialogs, backup, theme media (tvtunes), logLevel & maskInfo settings, AddonSignals + dateutil deps, module-reload restart hack | §7 |
| **New** | settings-driven everything, movie extras in the Kodi library, TV extras browsing, segment/Up Next coordination, transcode-bitrate context popup, KofinSyncQueue server plugin (clean protocol) | §8 |

A fresh addon id also means **no migration burden**: no legacy `data.json`, no old settings to honor, no upgrade paths through the emby→jellyfin renames. First run = clean sync. The old addon remains installable in parallel for A/B comparison during development (different id, different addon_data, different jellyfin.db) — just not simultaneously *enabled*, since both would write to the same MyVideos database.

Numbers for context: the current addon is ~22k lines of Python (excl. tests/build); the shell being rewritten is roughly half of it; the server plugin is only ~1.5k lines of C# and is trivially rewritable.

---

## 2. The system today, and what's actually wrong with it

Three cooperating pieces (full detail in `ref/jellyfin-kodi/docs/sync-improvement-plan.md` §1):

1. **The addon** mirrors selected Jellyfin libraries *into Kodi's own SQLite databases* (MyVideos/MyMusic) plus a mapping DB (`jellyfin.db`), so browsing is native-speed Kodi library UI. Playback goes through `plugin://` paths resolved back to the addon.
2. **KodiSyncQueue** server plugin records ItemAdded/Updated/Removed + UserDataSaved events in LiteDB so an offline Kodi can ask "what changed since T?" on boot.
3. **The server websocket** pushes LibraryChanged/UserDataChanged while Kodi runs; also carries remote-control and SyncPlay traffic.

That architecture is *sound* and worth keeping. Direct DB writes remain the only way to get native library UX: JSON-RPC has no insert API, and Kodi's `xbmcmediaimport` media-provider framework was never merged. The real problems are in the implementation:

### Structural problems (the reasons to rewrite the shell)

* **Global state via ~30 window properties** (`jellyfin_online`, `jellyfin_startup`, `jellyfin_play.json`, `jellyfin.skip.<id>`, …) — stringly-typed, racy, undiscoverable, and the service's own shutdown has a `TODO: review` list of props to clear (`entrypoint/service.py:513`).
* **Restart via exception + module reload.** "Restart service" raises `Exception("RestartService")` through the service loop, then `reload_objects()` deletes entries from `sys.modules` and re-imports (`service.py:474-506`, root `service.py:28-77`). This is why restart "has never worked reliably": module-level state (class attributes on `Player`, `Library` queues, window props, the borg-pattern caches) survives partially, and any thread still holding old classes keeps running against reloaded modules.
* **Python-2 skeleton**: `from __future__ import` everywhere, `six`-era idioms, `LazyLogger` indirection, deprecated Kodi APIs (`li.setInfo`, `li.addStreamInfo`, `setProperty("resumetime")` — all superseded by `InfoTagVideo` setters in Kodi 20+).
* **Vestigial dependencies**: `script.module.addon.signals` is required in addon.xml but no code imports it (sending is native `NotifyAll` — `helper/utils.py:138`; receiving is `Monitor.onNotification`). `script.module.dateutil` is used only for ISO-date parsing.
* **Interactive dialogs inside service threads** (mode-switch prompts, large-update prompts — mostly de-modalled by the fork, but the pattern remains available and used).
* **The plugin process opens a fresh HTTP session per request** (`jellyfin/http.py:98`: `keep_alive=False` outside the service), so every browse/play pays TLS setup again.
* **First-run wizard + mode switching** (`set_addon_mode`, `SyncInstallRunDone`) — complexity that exists only because native mode exists.

### Already fixed in the fork (carry these over, don't re-derive)

The fork's `combined/syncplay-sync` branch contains phase 1 of the sync improvement plan plus extras — all of it ports:

* Priority split: new content before metadata; userdata applied straight from event payloads with zero HTTP; Etag short-circuit so unchanged items are no-ops; server-load drops sharply.
* Watermark honesty (failed chunks don't advance `LastIncrementalSync`), degrade-instead-of-die on failed fast sync, auto-resume of interrupted full syncs (no modal), in-order page consumption so resume can't skip items.
* jellyfin.db secondary indexes; version-gated `/emby/` music path migration; people-cache priming; bigger transfer chunks (default `limitIndex` now 50).
* Music album duration fix; correct per-library progress. (The fork also tried parallel video∥music sync — the full-sync half was reverted on the branch and testing showed no real-world gain from the rest, so kofin does not carry it; see the YAGNI ledger.)
* **Widget refresh without `UpdateLibrary()`** — critical discovery documented in commit `e4f8dc3f`: `UpdateLibrary` after direct DB writes starts a real source scan; with remote music paths that meant ~21k HTTP probes (~3 min) per sync touching music, overlapping scans, and a reproducible Android SIGBUS in Kodi's scan cancel. The fix: `Container.Refresh` only when a media window is open; never scan. The rewrite must adopt this policy from day one.
* Segment skipping (fetch `/MediaSegments`, per-type Off/Auto/Button modes, EOF clamping, shutdown fixes) and the full **SyncPlay implementation** (`syncplay/`: manager 1129 + playback 757 + timesync + ui, with `tests/test_syncplay_*`), built to the plan in `ref/syncplay-report.md` §9.

### The companion plugin (KodiSyncQueue v15)

Small (~1.5k lines) and functional but crude — all confirmed in source:

* Records are **bare IDs + seconds-resolution timestamps**; `ItemUpdateType` (metadata vs image-only) is delivered to the plugin and thrown away; no SeriesId/SeasonId, so the addon can't order parents-before-children or demote image-only churn without re-fetching.
* Storage is LiteDB with an **O(n²) update path**: `WriteLibrarySync` does a `Find` per item and its update branch loads the *entire collection* into memory and rewrites every document (`Data/DbRepo.cs:119-138`) — per 5-second batch, during library scans.
* Default retention `RetDays=0` = keep forever (unbounded growth); the retention cutoff is not exposed in the `GetItems` response, so clients can't detect overrun (silently missing removals).
* `Enum.TryParse("")` on the filter yields `Movies` — an empty filter silently excludes movies (`API/KodiSyncQueueController.cs:57-61`).
* Culture-sensitive `DateTime.Parse` on the `LastUpdateDT` param; double `GetItemById` per item in visibility translation; legacy `Kodi/{...}/file.strm` endpoints nothing current uses.

---

## 3. Proposed architecture

### Targets

* **Kodi 21 (Omega) and 22 (Piers)**; `xbmc.python` 3.0.1 (Omega baseline runs on both). Python syntax floor 3.8 (distro builds), bundled interpreters are 3.11 (Omega) / 3.14 (Piers) — so no Python-2 compat anywhere, no `__future__`, type hints throughout, `mypy`/`black`/`tox` configs carried from the fork.
* **Jellyfin 10.11+ as a hard floor** — no older-server fallbacks anywhere. That guarantees the EF-core refactor's `DateLastSaved` filtering, userdata tombstones (watched state survives repairs), the in-memory item cache, and the media-segments API, and deletes a whole class of version-gated code paths. There is no benefit to a *higher* floor today: master (10.12.0 as of this checkout) contains none of the sync wishlist (`UserData.LastModified`, `ItemSortBy.DateLastSaved`, removal tombstones — verified absent), and KofinSyncQueue covers those gaps regardless; when upstream features do land, gate them by capability probe rather than raising the floor.
* Dependencies: `script.module.requests`, `script.module.websocket` only. Drop `script.module.addon.signals` (vestigial) and `script.module.dateutil` (replace with a ~10-line ISO-8601 parser — Jellyfin's 7-digit fractional seconds trip `fromisoformat` on Python < 3.11).
* License: GPL-3.0 (transplanted jellyfin-kodi code is GPL-3).

### Process model and layout

Kodi runs the addon as two kinds of process: the long-lived **service** (`xbmc.service`) and short-lived **plugin invocations** (`xbmc.python.pluginsource`) per directory listing / playback resolve, plus context-menu scripts. Layout:

```
plugin.video.kofin/
  addon.xml               # service + pluginsource + context items
  service.py  default.py  context.py
  lib/
    core/                 # shared by both processes
      settings.py         #   typed settings access, credential store, masking
      client.py           #   single-server Jellyfin HTTP client (persistent session)
      ws.py               #   websocket client (ported)
      api.py              #   endpoint wrappers incl. device profile builder
      ipc.py              #   NotifyAll send/receive message registry
      log.py              #   xbmc.log adapter, always-on masking
    service/
      main.py             #   Service object, lifecycle, notification hub
      sync/               #   library.py/downloader.py/full_sync.py (ported, renamed)
      writers/            #   objects/kodi/* + obj_map.json (ported)
      jellyfindb.py       #   mapping DB (ported)
      player.py monitor.py segments.py
      syncplay/           #   ported wholesale
      settings_apply.py   #   onSettingsChanged diff engine (§4)
    plugin/
      router.py browse.py play.py listitems.py extras.py
  resources/
    settings.xml language/ skins/ (skip-button dialog only)
```

### State management

Replace the window-property soup with three explicit tiers:

1. **Durable state → hidden settings** (`level=4`, the pvr.kofin pattern): access token, user id, device id, server name/address, logged-in flag, library whitelist, last-sync watermark, restore points (or a small JSON file for the latter). This kills `data.json` and makes the plugin process's auth trivial (`Addon().getSetting`), with one masking chokepoint.
2. **Cross-process live flags → a `WindowState` helper** wrapping the *few* legitimately shared flags (server online, sync active, "queued play items"), each a typed property with a constant — not ad-hoc strings.
3. **Everything else → plain object attributes** owned by the service.

### IPC

Keep Kodi-native `NotifyAll` + `Monitor.onNotification` (it's what the addon already does — no AddonSignals). One `ipc.py` module declares every message name/payload both sides use; the Up Next wire format (hexlified JSON array, `sender=<id>.SIGNAL`, reply on `upnextprovider.<id>_play_action`) is producible with the same primitive (verified against `service.upnext/resources/lib/monitor.py:115-129`).

### Reliable restart (fixing "has never worked")

* The service main loop becomes `while not aborted: build Service(); run; if restart_requested: continue` — **no module reloading**. With zero module-level mutable state (enforced by the rewrite), tearing down the object graph and rebuilding it is sufficient and testable. The current implementation fails precisely because state lives on classes and in `sys.modules`.
* The settings button "Restart addon" sends one IPC message; the service drains writers, closes the websocket and session, joins threads with a deadline, rebuilds.
* Escalation fallback (also the post-update path): `Addons.SetAddonEnabled(false→true)` via JSON-RPC from the transient `RunPlugin` process — Kodi then genuinely restarts the service process. Reserve it for a "hard restart" long-press/second button; the soft path should be the normal one.

### Connection & login (single server)

Multi-server support is dropped (§7). `connect.py` + `connectionmanager` + `data.json` + server-select/user-select custom skin dialogs all collapse into:

* Settings: `serverAddress` (edit, enabled when logged out), Login button → resolves+pings the server, then username/password keyboard dialogs or **Quick Connect** (already in upstream 2.1.0), stores token/user/device-id in hidden settings, flips `isLoggedIn`.
* Visibility dependencies mirror pvr.kofin exactly (server name / logged-in user shown read-only when logged in; Logout + Test connection buttons).
* The websocket + sync stack starts when credentials validate, from the service, without dialogs.

### HTTP & websocket

* One `requests.Session` per process, keep-alive always (fixes the per-request TLS churn in browse), retry with backoff+jitter, explicit `(connect, read)` timeouts, gzip on.
* Auth header only (`Authorization: MediaBrowser ... Token=`), never query-string tokens, except the endpoints that require them (image/audio URLs written into Kodi DBs are token-free by design — Jellyfin's static stream endpoints are anonymous; see Risks §10).
* Websocket client ported as-is (30s KeepAlive, auto-reconnect); add jittered backoff and a reconnect-storm cap.

### Kodi database writers (the transplant)

* Keep direct SQLite writes, WAL, and the jellyfin.db mapping exactly as the fork has them — including the fixed people-cache, indexes, and Etag-in-checksum short-circuit.
* **Schema gate**: map Kodi major → expected schema (Omega: MyVideos **131** / MyMusic **83**; Piers: MyVideos **146** / MyMusic **84**, from `VideoDatabase::GetSchemaVersion` in both trees). Discover the actual file, compare: known → go; newer/unknown → *disable write-sync with a clear notification* instead of writing blind. This converts "new Kodi silently corrupts" into "sync paused until kofin updates".
* Concrete Piers churn to encode (from `VideoDatabaseMigration.cpp`): video-asset `itemType` renumbered (VERSION 0→1, **EXTRA 1→2** — directly affects the extras feature), new columns (`sets.strOriginalSet`, `seasons.plot`, `movie/tvshow.originalLanguage`, `tvshow.tagLine`), streamdetails cleanups. None are structural for our writers, but the constants must be per-schema.
* **One writer thread per database file** (video, music); removals/updates/userdata for the same DB serialize through it (kills the R7 writer races by construction). The orphan-episode self-heal (R8) becomes "queue the series fetch, then re-queue the episode" instead of HTTP inside the write lock.
* Widget refresh policy from the fork: `Container.Refresh` when a media window is up; never `UpdateLibrary()`.

### Sync engine

Port the fork's pipeline (it already implements plan Phase 1), then follow the existing roadmap (plan §6) as kofin work items:

* **Phase 2** — companion plugin v2 = **KofinSyncQueue** (§8.4): typed records let the addon order added-items newest-first and Series→Season→Episode, demote image-only updates, skip downloads entirely for records whose Etag it already has, and detect retention overrun in-band (auto-targeted repair instead of silent loss).
* **Phase 3** — full-sync overhaul: `SortBy=DateCreated desc` so fresh content is browsable minutes into an initial sync; episodes paged per-library instead of per-show (kills the 500-shows ⇒ 500 requests pattern); persistent DB connections with periodic commits; buffer and replay websocket events that arrive during long syncs.
* **Phase 4** — plugin-optional fallback (`MinDateLastSaved` catch-up + periodic ID-set reconcile for removals; the 10.11 floor guarantees the APIs). Fallback, not replacement — userdata has no per-user cursor upstream (plan R11, still true on 10.12 master).

**"Update libraries" that actually works.** Today "update" is a full-sync compare mode whose holes came from R1/R3 (resume skips, watermark advancing over failures) — with those fixed, plus retention-overrun detection, define clearly:

* *Update library* = incremental catch-up via sync queue **plus** a prune pass: page the library's full ID set (ids-only, `limit=500` — cheap even at 10⁵ items) and diff against jellyfin.db both directions (delete stale, fetch missing).
* *Repair library* = drop & resync that library (userdata survives server-side via 10.11 tombstones). Both exposed as settings buttons (§4 Library tab), both non-modal, both usable while Kodi runs.

### Playback

* Resolution flow: `?mode=play&id=` → PlaybackInfo with a **device profile built the pvr.kofin way** (`BuildDeviceProfile`, `JellyfinChannelLoader.cpp`): DirectPlayProfiles from the allowed video/audio codec multiselects; virtual `h264_10bit`/`hevc_rext` entries expand to CodecProfile bit-depth/profile conditions; allowed-HDR-types → VideoRangeType conditions; max resolution → width/height conditions; preferred codec ordering; **two TranscodingProfiles** (fMP4/HLS for AV1, TS/HLS otherwise, preferred-codec-led); force-direct-play / force-remux / force-transcode toggles. Additions over pvr.kofin: video **audio bitrate** + max audio channels on the transcode profile, and a separate **music profile** (music max bitrate, transcode codec aac/mp3/opus, transcode bitrate) feeding the universal-audio endpoint.
* **No interactive dialogs in the standard path**: no play-method prompt, no audio/subs picker dialogs (Jellyfin default selections applied; Kodi's own OSD handles switching), no custom resume popup. Multi-version items: play the default version (context menu can expose "Choose version" later via native Kodi versions — flagged future work, not v1).
* **Native resume everywhere**: library plays already flow Kodi's resume prompt through `sys.argv[3]`; dynamic listings get `InfoTagVideo.setResumePoint()`, which Omega's play processor turns into the native "Resume from …" choice (verified: `xbmc/video/VideoUtils.cpp:723-731` reads the item's resume point before falling back to the bookmark DB). Delete `dialogs/resume.py` + its skin XML.
* **Transcode context item** (per spec): context menu "Play with transcoding" opens a select dialog of *bitrate options* — the list is a multiselect setting (e.g. 3/6/10/15/20/40/60/120 Mbit/s); **if exactly one bitrate is enabled in settings the dialog is bypassed** and that bitrate is used directly. Implementation: `context_play.py` → `Dialog().select` over the configured list → force-transcode profile with `MaxStreamingBitrate` overridden.
* Playback reporting: keep the ported player, simplify cadence (progress every 10s + on pause/resume/seek/stop; drop the separate 250s "full report" timer), and keep the fork's decoupled segment checker.
* Trailers/cinema mode: keep (`enableCinema`/`askCinema`), including local + YouTube trailer fallback; TV-show trailers arrived upstream in 2.1.0.

### Views / nodes

Keep the mechanism (Kodi video nodes + xsp playlists per synced library; it's what makes synced libraries appear as first-class home items) but regenerate only when the view set actually changed (hash of views + addon version), not every startup. `verify_kodi_defaults` stays (it repairs broken profile node indexes). The forced node ordering the current code applies is dropped (respect user/skin order).

---

## 4. Settings — complete redesign

Format: settings **v1 XML** (same format both pvr.kofin and current jellyfin-kodi use — Kodi's "new format"), `<section id="plugin.video.kofin">`, one `<category>` per tab, every visible setting with a `help=` string (concise tooltip; ~90 new localized strings in `resources/language/resource.language.en_gb/strings.po`).

**Mechanics (all verified against Kodi Omega source):**

* Action buttons: `<control type="button" format="action"><data>RunPlugin(plugin://plugin.video.kofin/?mode=…)</data><close>true</close></control>`. `close=true` triggers `CGUIDialogAddonSettings::SaveAndClose()` **before** the builtin runs (`xbmc/addons/settings/AddonSettings.cpp:217-220`) — pending edits are committed, then the action executes. This is the exact "actions performed on clicking OK" semantics requested.
* Hidden state settings: `<level>4</level>` (never rendered) for isLoggedIn, token, userId, deviceId, whitelist csv, watermarks — the pvr.kofin pattern.
* Dynamic choices (libraries, users) can't be static `<options>`: the button opens a `Dialog().multiselect` pre-checked from the hidden csv setting, writes the result back, and a read-only companion setting displays the current selection. The service's `onSettingsChanged` **diff engine** compares desired vs. actual state per key and executes handlers (add/remove library sync, reconnect, restart) — this is what makes "apply on OK" real for non-button edits too.
* Destructive actions (remove library from sync, reset database) confirm via `Dialog().yesno` inside the triggered action, not via modal service prompts.

### Root menu (plugin listing)

Only: synced/browsable **library views**, **Add user to session**, **SyncPlay**, **Settings**. Everything else that lives in the root today (manage libraries, add server, reset DB, restart, backup, path management) moves into settings or dies with its feature. "Update password" is dropped outright: a server-side password change doesn't invalidate the existing token, and if the token is ever revoked the Account tab's logout/login is the re-auth path — a dedicated flow duplicates it.

### Tab: Account  *(modeled on pvr.kofin)*

| Setting | Type | Notes |
|---|---|---|
| `serverAddress` | edit | enabled only when logged out |
| `loginButton` | button | resolve server → Quick Connect or user/pass; fills hidden creds |
| `serverName`, `displayUser` | read-only edits | visible when logged in |
| `testConnection` | button | visible when logged in |
| `logoutButton` | button | visible when logged in; clears creds (+ optional "forget sync state") |
| `restartAddon` | button | soft restart via IPC (§3) |
| `sslVerify` | bool, default true | |
| `deviceName` | edit, default "Kodi" | shown in Jellyfin dashboard |
| hidden: `isLoggedIn`, `accessToken`, `userId`, `deviceId` | level 4 | |

### Tab: Library  *(new — everything that was buried in the root menu)*

| Setting | Type | Notes |
|---|---|---|
| `selectLibraries` | button → multiselect | choose views to sync; diff applied on save: additions synced, removals confirmed then removed |
| `syncedLibraries` | read-only label | current selection |
| `updateLibraries` | button | catch-up + prune (§3 "update that works"); all or per-library picker |
| `repairLibraries` | button | per-library picker → drop & resync |
| `refreshBoxsets` | button | boxset re-link pass (kept from today's manage menu) |
| `syncEmptyShows`, `syncRottenTomatoes`-type leftovers | — | not carried unless they exist server-side today (they don't — omit) |

**Music libraries are not a separate on/off toggle.** They appear in the same multiselect as every other view; selecting one is what activates the MyMusic writer, schema gate, and migration, and deselecting the last one stops music sync (the data-reset flow wipes MyMusic only if a music library had been synced). The old `enableMusic` was already derived state pretending to be a setting — the addon flips it on itself when a music library syncs (`full_sync.py:351`); its only other consumers are the backup copy (dropped) and the reset prompt. The only genuinely music-specific settings that remain are playback ones (the Transcoding tab's music section).

"Sync theme media" is **not carried**: it existed to feed the abandoned `script.tvtunes` addon (writes `tvtunes.nfo` files with direct stream URLs and pokes tvtunes' custom-path settings — `entrypoint/default.py:1187-1270`). It's "broken" because tvtunes is dead upstream. If theme songs ever matter again it's a ~50-line exporter, YAGNI now.

### Tab: Transcoding  *(pvr.kofin's tab + video audio bitrate + music section)*

Video group (ids/defaults per pvr.kofin unless noted): `forceDirectPlay`, `forceRemux`, `forceTranscode`, `directPlayVideoCodecs` (multiselect: h264, h264_10bit, hevc, hevc_rext, av1, mpeg2video, vp9, vc1), `directPlayAudioCodecs` (multiselect: aac, mp2, mp3, ac3, eac3, opus, flac, dts), `allowedHdrTypes` (multiselect: HDR10, HLG, HDR10+, DV profiles…), `preferredVideoCodec` (h264/hevc/av1), `preferredAudioCodec` (aac/ac3/mp3/opus), `maxAudioChannels` (2/6/8), `maxStreamingBitrate`, `maxResolution`, **`audioBitrate`** (transcode audio bitrate — new), **`contextBitrates`** (multiselect of bitrates offered by the transcode context item; exactly one selected ⇒ no popup).

Music group: `musicMaxBitrate` (streaming cap), `musicTranscodeCodec` (**aac | mp3 | opus**), `musicTranscodeBitrate` (e.g. 128/192/256/320 kbps).

### Tab: Sync

Carried from today, tuned per spec: `syncNotification` **bool, default off** (replaces the inverse-threshold behavior of `syncIndicator`), `syncNotificationCount` **int, default 1000** (only visible when notification on — above this many pending items you get a *notification*, and optionally a hold-off; never a blocking modal), `syncDuringPlay` (default true), `dbSyncScreensaver` (fast-sync on screensaver deactivate, default true), `limitIndex` (page size, default 50), `limitThreads` (download threads, default 3), artwork group (`enableCoverArt`, `compressArt`, `maxArtResolution`), `startupDelay`.

`kodiCompanion` (the "server plugin present" toggle) becomes **auto-detected** at connect time — KofinSyncQueue if present, else the official KodiSyncQueue, else the plugin-free fallback (§8.4 compatibility ladder) — with a read-only status line showing which tier is active. One less setting to misconfigure.

### Tab: Playback

Groups: resume/`resumeJumpBack`, `markPlayed` threshold, offer-delete group (`offerDelete`, `deleteTV`, `deleteMovies`), cinema mode (`enableCinema`, `askCinema`), `enableExternalSubs`, remote control group (`remoteControl` on/off — Jellyfin remote-play commands), **media segments group** (fork's: `mediaSegmentsEnabled` + per-type Off/Auto/Ask for Intro/Credits/Recap/Preview/ Commercial + Up Next coordination toggle, §8.2). Transcoding got its own tab (above), so the old playback-tab transcode block disappears.

### Tab: SyncPlay

(Was crammed into Playback in the fork.) `syncPlayEnabled` (shows root item), `syncPlayDriftCorrection`, `syncPlayTolerance`, `syncPlayNotifications` (member join/leave toasts).

### Tab: Interface

`enableContext` / `enableContextTranscode` (context items on library entries), notification group (`connectMsg`, `restartMsg`, `newContentMsg` + display seconds — video/music split collapsed to one), `syncProgressDisplay` (progress-bar threshold %), widgets group (from the old "Plugin" tab: `nextEpisodesDays`, `ignoreSpecialsInNextEpisodes`), `browseCast` (fetch cast for dynamic listings — the old `getCast`, renamed so the tooltip can explain the perf cost).

The old **"Plugin" tab is dissolved** — that's what those settings were: options for the `?mode=nextepisodes` widget path and dynamic-browse metadata; they belong under Interface.

### Tab: Advanced

`generateNewDeviceId` (button, with confirm), `resetLocalDatabase` (button, with double-confirm — today's root "reset local Kodi database"), `pathSubstitution` button *only if* proven needed without native mode (likely drop — path replacements existed for direct paths), websocket/HTTP tunables kept **out** (YAGNI), and that's it.

Explicitly **gone**: `logLevel` (all logging goes through `xbmc.log` at proper levels — DEBUG-level detail appears exactly when the *user enables Kodi's* debug logging: "log level follows Kodi"), `maskInfo` (masking is **always on**: tokens, device ids, user ids and file paths hashed/redacted at the one logging chokepoint; no toggle to leak), `enableAddon` (Kodi can disable addons natively), backup group (§7), `useDirectPaths`, `startupDelay` moved to Sync.

---

## 5. Speed opportunities (ranked, beyond what the fork already landed)

1. **Persistent worker pool** in the sync engine instead of respawning `GetItemWorker`/ `UpdateWorker` threads every 2-second service tick (today: fresh `xbmc.Monitor()` and reloaded caches per spawn — plan P9, partially fixed). Queue-driven long-lived threads, event-signalled.
2. **Plugin-process HTTP session reuse + smaller payloads for browse.** Keep-alive session (§3) plus trim `browse_info()` fields and make `People` fetch opt-in (`browseCast`) — dynamic browsing is the main interactive path left.
3. **KofinSyncQueue typed records + query-time Etag** (§8.4): unchanged items are dropped before download (today's Etag skip only saves the write — the fetch still happens), newest-first additions, parent-first ordering without lookups, image-only updates demoted to artwork-touches — the "morning after" catch-up becomes seconds-to-new-content (plan §3 target ordering) with the metadata-noise class costing zero HTTP on both ends. (Requires KofinSyncQueue; against the official plugin the addon runs the fork-level fast path unchanged.)
4. **Full-sync overhaul** (plan Phase 3): per-library episode paging (one request per ~50 episodes instead of per show), `DateCreated desc` ordering, persistent connections, commit every N items decoupled from page size.
5. **Views regen skipped when unchanged** (startup does 2 HTTP calls + full node/playlist rewrite every boot today).
6. **SQLite tuning on our writes**: `PRAGMA synchronous=NORMAL` under WAL, prepared statements reused across items (the writers currently re-`format` SQL strings), single transaction per batch (fork started this; finish it).
7. **Retention/`GetServerDateTime` piggyback**: one probe per connect instead of per-sync companion checks (also feeds auto-detection of the plugin).
8. Music: the fork's Etag skip already cut unchanged-item churn; per-album batched writes are the remaining win if music sync proves slow (defer until measured — YAGNI).

Reliability policies (system-wide, cheap to state, expensive to retrofit): no modal dialogs from service threads, every failure path either retries with backoff or degrades with a notification (never a dead thread), watermarks advance only after verified completion (fork), all writes idempotent (re-running a window is always safe), threads own their teardown with join deadlines (the fork's shutdown fixes showed why).

---

## 6. Consistency with Kodi practice (the "feel native" checklist)

* Settings v1 XML with tooltips and visibility dependencies, **all at a single level** (no basic/standard/expert split — Kodi's level selector never comes into it; the only other level used is 4/internal for hidden state) — and no custom "settings via addon menus" (§4 moves them).
* `InfoTagVideo`/`InfoTagMusic` setters instead of deprecated `setInfo`/`addStreamInfo`/ property hacks; `setResumePoint` for native resume; `SORT_METHOD_*` where sortable.
* Native dialogs only (`xbmcgui.Dialog`): keyboard, select, multiselect, yesno, notification, busy — the only custom skin window left is the segment-skip button overlay (Kodi has no native skip-button primitive; service.upnext made the same call).
* Logging at real Kodi levels; debug visibility follows Kodi's toggle; sensitive values always masked.
* Kodi's own library backup guidance (userdata backup / the Backup addon) instead of a bespoke backup — the addon's data is *reconstructible by design* (resync + server-side userdata).
* Context menus via `addon.xml` context-item extensions (as today), gated by the two Interface toggles.
* No `advancedsettings.xml` mutation, no `sources.xml` writes (those were native-mode artifacts), no forced node ordering.

---

## 7. Deprecations — what each removal actually buys

| Removal | What it is today | Payoff |
|---|---|---|
| **Native/direct-paths mode** | `useDirectPaths`, path validation prompts, `sources.xml`/`passwords.xml`/`advancedsettings.xml` manipulation, path-replacement manager, first-run mode wizard, per-object dual path logic | Deletes the mode fork from every writer (`get_path_filename` branches ×6 media types), the whole `managepaths` UI, `set_addon_mode`, cleanonupdate guard + forced Kodi restart path; ends the class of "Kodi cleaned my library" bugs. All playback via Jellyfin URLs/plugin paths |
| **Live TV** | Channel browse nodes + TvChannel playback special-casing (guide-less) | pvr.kofin *is* the live-TV client; recordings remain fully available (they're plain library-less videos via dynamic browse; recordings library node stays). Removes TvChannel special cases in playutils/monitor |
| **Multi-server** | `connectionmanager`, server list in root, per-item `server=` routing, server add/remove dialogs, credentials array | Single hidden-settings credential store; deletes `connect.py` dialog flows (~4 skin XMLs), `data.json`, per-call client resolution |
| **Custom popups** (resume, server-select, user-select, manual login/server, source-select, audio/sub pickers, transcode y/n) | `dialogs/` + skins + `skipDialogTranscode`-style toggles | Native resume + settings-driven profile decide everything; ~1.5k lines + 6 skin XMLs gone; the *only* custom window left is the skip button |
| **Backup** | `mode=backup` copytree of addon_data + DB files | Use standard Kodi backup practice; kofin state is reconstructible (resync; watched state lives server-side, incl. 10.11 tombstones) |
| **Theme media sync** | tvtunes.nfo generator for abandoned script.tvtunes | Dead integration; YAGNI |
| **`logLevel` + `maskInfo` settings** | addon-managed verbosity + optional masking | Kodi-native debug toggle; masking unconditional |
| **AddonSignals + dateutil deps** | required-but-unused / trivially replaced | Two fewer install-time deps |
| **Module-reload restart** | `reload_objects()` + exception control flow | Replaced by object-graph rebuild (§3) |
| **strm endpoints** (server side) | `Kodi/{type}/…/file.strm` routes in KodiSyncQueue | Not carried into KofinSyncQueue; nothing current calls them |

Kept deliberately (cheap, in use): music-video sync, photos/homevideos/books dynamic browse, boxsets incl. grouped-sets window prop, `nextepisodes` widget mode, additional-users session management, Quick Connect, external-subs download support.

---

## 8. New features

### 8.1 SyncPlay

Port the fork's `syncplay/` package (manager / playback / timesync / ui + 1.8k lines of tests) unchanged except imports and settings ids. It implements the plan in `ref/syncplay-report.md` §9: NTP-style timesync over `/GetUtcTime` (min-RTT-of-8), scheduled command execution against offset-corrected local clock, `Player.SetTempo` drift correction with micro-seek fallback and dead-zone, `onAVStarted` as the Ready trigger, group UI, and guards so programmatic actions don't echo (the up-next suppression inside a group is already in `player.py:297-301`). The `feat/syncplay-protocol-v2` branch tracks the server-side robustness work — kofin should stay protocol-v1-compatible and adopt v2 opportunistically (that battle is on the server side; the report's §6 surgical fixes are upstream PRs, not kofin work).

### 8.2 Segment skipping + Up Next, side by side

Current state: the fork polls position 1×/s (`segments.py`), auto-skips or shows a skip button per segment type, and sends `upnext_data` at 2% progress; on a Credits segment it *also* triggers the Up Next data send (`player.py:648-650`). Two popups can still stack at the end of an episode.

Design — **one popup at a time, Jellyfin segments drive Up Next timing**:

1. On playback start of an episode, fetch segments (already done) and next-episode info; send `upnext_data` immediately **with `notification_time` = credits/outro segment start** (when a Credits segment exists). service.upnext then fires its popup exactly when credits begin — verified supported: `notification_time`/`notification_offset` in `service.upnext/resources/lib/api.py:186-193`.
2. Suppression rule in our skip logic: if a next episode exists *and* Up Next is installed+enabled (`System.HasAddon(service.upnext)` + our toggle), the **Credits segment does not show our skip button** — Up Next's popup owns that moment (its Watch Now/Cancel already covers "skip to next"). Intro/Recap/Preview/Commercial segments behave normally (auto/button per settings).
3. Fallbacks: no Up Next, or a movie, or the season's last episode ⇒ our credits skip button appears as normal. Inside a SyncPlay group ⇒ neither (group queue is authoritative — already handled).
4. The skip-button monitor loop stops blocking the checker thread (event-driven timer instead of the `_monitor_skip_dialog` busy-loop).

Full integration *into* service.upnext (replacing its popup) is indeed not feasible/needed — it's a separate addon with its own state machine; the data+play_info signal API is the sanctioned integration point and we already use it (`monitor.py:95-107` handles the play-back signal).

### 8.3 Extras (Kodi-native for movies, browseable for TV)

* **Movies — native.** Kodi 21+ stores extras as video assets: a `files` row + `videoversion` row (`itemType = EXTRA`, `idMedia = idMovie`, `media_type='movie'`, named via `videoversiontype`). Estuary shows an **Extras** button on the movie info dialog (`DialogVideoInfo.xml`, gated `DBType==movie`) opening the native extras manager. Sync path: Jellyfin `SpecialFeatureCount > 0` ⇒ fetch `/Items/{id}/SpecialFeatures`, write each as an asset row with a plugin:// play path, map Jellyfin `ExtraType` (BehindTheScenes, DeletedScene, Interview, Featurette, Short, Clip, Scene, Sample) to named `videoversiontype` entries. **Schema constant per Kodi version: EXTRA = 1 on Omega, 2 on Piers** (renumbered in migration 134). Cost: one extra request per movie that has extras, incremental via the same added/updated events.
* **TV shows — plugin browse** (Kodi has no native TV extras): an "Extras" node in the addon's dynamic listing for series/seasons that have special features, plus a context-menu item ("Browse extras") on library shows that routes to it. Same `SpecialFeatures` endpoint.
* Trailers keep the existing dedicated handling (local + YouTube).

### 8.4 KofinSyncQueue — the companion plugin rewrite

New plugin (new id, coexists with legacy KodiSyncQueue on servers that also serve stock jellyfin-kodi clients). Clean-room, ~1k lines, shaped by plan §5 + observed defects. **It is an optional accelerator, not a requirement**: the addon stays backwards compatible with the official KodiSyncQueue plugin (see the compatibility ladder below).

* **Compatibility ladder.** The sync engine consumes an abstract *change-feed provider* with one internal record type (`id, status, media_type, item_type?, last_modified?, update_reason?, etag?, series_id?`); optional fields are simply `None` when the provider can't supply them, and each enhancement keys off field presence, not plugin identity. Detection at connect time, best first:
  1. **KofinSyncQueue** → full fast path: Etag skip-before-download, newest-first + parent-first ordering, image-only demotion, in-band retention check.
  2. **Official KodiSyncQueue** → the legacy `GetItems`/`GetServerDateTime` shapes the transplanted pipeline already speaks: everything the fork ships today keeps working (priority split, userdata-from-payload, Etag skip *after* download), and retention overrun is still detected — the official plugin's `GetServerDateTime` already returns `RetentionDateTime`; the current addon just never reads it (`jellyfin/api.py:454` unused). One extra round trip instead of in-band, same targeted-repair response.
  3. **No plugin** → plugin-free fallback (plan Phase 4; the 10.11 floor guarantees the APIs).
* **Protocol**: `GET /Kofin/SyncQueue?since=<unix-seconds>&types=movies,tvshows,…` returning
  ```
  { ServerTime, RetentionCutoff,
    Items: [ {Id, Status: Added|Updated|Removed, MediaType, ItemType,
              LastModified, UpdateReason, Etag, SeriesId?, SeasonId?} ],
    UserData: [ full UserItemDataDto ] }
  ```
  — typed records (UpdateReason is already delivered to the plugin today and discarded), unix timestamps (no culture-sensitive parsing), retention cutoff in-band so the client detects overrun in the same round trip and triggers a *targeted* repair.
* **Etag in the record — skip the download, not just the write.** The addon's current Etag short-circuit fires *after* the full-item download (it compares the DTO's Etag against jellyfin.db's checksum and skips the SQL cascade). Jellyfin's Etag is `MD5(DateLastSaved.Ticks)` computed by `BaseItem.GetEtag` (`MediaBrowser.Controller/Entities/BaseItem.cs:2607-2620`, no per-type overrides; `DtoService.cs:392` puts the same value in every DTO), so the plugin can return the byte-identical string by calling `item.GetEtag(user)` on the `BaseItem` it **already loads per record** for visibility translation — computing it at *query time*, not event time, which also coalesces an event storm on one item into a single current-state comparison. The addon then drops any record whose Etag matches its stored checksum *before* forming download chunks: the no-op class of updates (validation passes, plugin re-saves, items in both Added and Updated windows) goes from "download ~5–15 KB DTO + skip write" to zero HTTP — and each avoided fetch is also an avoided server-side DTO build (per-item People/MediaStreams queries, the most expensive thing the addon inflicts on the server). Correctness is unchanged: it is the *same predicate* the fork already trusts client-side, evaluated earlier; anything that changes after the queue query raises a new event past the watermark and is caught next cycle, and userdata never rides on this path (it arrives as full DTOs in the same response). `Etag` is included on Added records too — it lets the client skip re-adds it already has (repairs, plugin re-installs) for free.
* **Storage**: indexed upsert keyed on ItemId (LiteDB `EnsureIndex` + single-record upsert, or plain SQLite/EF-core via the 10.11 plugin-database providers — decide at implementation; either removes the FindAll-rewrite O(n²) path). Coalesce Added+Updated for the same id (keep Added, bump LastModified).
* **Defaults**: retention 90 days (not infinite); scheduled cleanup task kept.
* **Fixes by construction**: no empty-filter enum foot-gun; visibility translation calls `GetItemById` once; no strm endpoints; per-user userdata rows as today (they're already full DTOs — the good part of the old design).
* Addon adoption (sync-plan Phase 2): records whose Etag matches the stored checksum dropped before download, additions ordered newest-`DateCreated`-first within parent-first batches, image-only updates demoted to artwork writes, retention overrun ⇒ automatic repair of affected libraries only.

Upstream note: the honest long-term fix (server-side tombstones + userdata timestamps) is listed in plan §4 as PR candidates; the plugin remains necessary until then.

### 8.5 Transcode context popup

Covered in §3 Playback and §4 Transcoding: `contextBitrates` multiselect drives a select dialog; single selection bypasses it. (Replaces today's binary "Play with transcode" that used the global bitrate.)

---

## 9. YAGNI ledger (things considered and *not* proposed)

* No plugin-free sync as default (fallback only, Phase 4) — the plugin path is strictly better while upstream lacks tombstones/userdata cursors.
* No Kodi "versions" sync of Jellyfin MediaSources in v1 (play default version; native versions UI is a clean later addition since extras already exercise the asset tables).
* No custom Up Next popup, no vendored fork of service.upnext.
* No theme-media replacement, no backup subsystem, no path-substitution UI (unless a real network-share use case shows up post-native-mode).
* No parallel video∥music sync. The fork built it (one writer per Kodi DB, music full-sync on its own thread) and it tested as a bust; the architecture says why: both writers still serialize on the single-writer jellyfin.db mapping DB, the Python GIL serializes the CPU side of item mapping, and the download stage — the actual bottleneck, bounded by server-side DTO builds — was already parallel (3 workers) and gains nothing from a second consumer. A honest fix would need per-media mapping DBs plus more download concurrency the server wouldn't reward: real complexity for a marginal, unproven win. Sequential sync is also simpler to report progress for and to resume. The goal it chased (shorter perceived wait) is served instead by priority ordering and the Phase-3 full-sync overhaul (newest-first, fewer round trips).
* No settings for HTTP tunables, thread counts beyond the two existing tuning knobs, WS intervals, or per-library sync schedules.
* No orjson/uvloop-style deps; stdlib json is fine at our payload sizes once re-downloads are gone.
* No attempt to hide the sync pause on unknown-future Kodi schemas behind best-effort writes — fail visible, not corrupt.

---

## 10. Risks & mitigations

| Risk | Notes | Mitigation |
|---|---|---|
| **Kodi schema churn** (annual) | Piers already renumbers asset types and adds columns (§3); MyVideos 131→146 in one cycle | Schema-gated constants; write-sync disable on unknown versions; CI creating both Omega+Piers DBs from Kodi's own DDL and running writer fixtures against them (port `tests/`) |
| **Writers are the crown jewels** | Direct-SQL correctness is the whole product | Transplant, don't rewrite; keep fixtures; feature-flag any writer change |
| **service.upnext variants** (im85288 vs MoojMidge fork) | Signal API is compatible across both; `notification_time` supported since v1.1.0 | Integration behind capability check; graceful without it |
| **Jellyfin locks down anonymous static streams** (music/theme URLs in MyMusic rely on it) | Verified: Audio stream endpoints carry no `[Authorize]` today | Keep an `api_key`-in-URL fallback path ready behind a constant; regenerate music paths on demand |
| **Settings v1 quirks** (dynamic lists, apply-on-OK) | Verified: `close=true` saves-then-runs; multiselect via dialog round-trip | Prototype the Library tab first (it exercises every mechanism) |
| **Restart semantics** | Soft restart depends on zero module-level state | Enforce via lint rule (no mutable module globals in service code) + the SetAddonEnabled hard path |
| **Single-maintainer surface area** | Two codebases (addon + server plugin) | Plugin is ~1k lines with a fixed protocol; addon carries the fork's test suite + tox/mypy/black from day one |
| **GPL-3 obligations** | Transplanted code is GPL-3 | kofin is GPL-3; pvr.kofin logic is re-expressed in Python (no C++ copying) |

---

## 11. Suggested build order (each phase shippable)

1. **Skeleton + Account/Transcoding settings + login + dynamic browse + playback** — a working sync-less Jellyfin addon: router, listitems via InfoTagVideo, device profile port (pvr.kofin), playback reporting, native resume, transcode context popup. *This validates the settings mechanics and playback stack before any DB writing exists.*
2. **Sync transplant** — writers + jellyfin.db + pipeline (fork phase 1 included), Library tab (select/update/repair), views/nodes, widget refresh policy, schema gate. Syncs against the **official KodiSyncQueue** from day one (the transplanted pipeline already speaks it). Old addon → kofin side-by-side comparison on a test profile.
3. **Player features** — segments + Up Next coordination (8.2), movie extras + TV extras browse (8.3).
4. **SyncPlay port** (8.1) + SyncPlay tab + root entry.
5. **KofinSyncQueue** (8.4) + addon adoption (typed ordering, retention-overrun repair) + full-sync overhaul (plan Phase 3).
6. **Hardening** — Piers validation pass, plugin-free fallback (plan Phase 4), translations, repo packaging (repository.kontell).

Rough proportions: the transplant phases (2,4) are mostly mechanical; phases 1 and 5 are the bulk of genuinely new code; phase 3 is small but UX-sensitive.

---

## 12. Open questions (defaults chosen, flag if wrong)

1. **Music-video sync**: kept (ported code, low churn) — drop if you don't use it.
2. **Kodi floor**: **Omega (21)** — extras/assets need schema 131; Nexus support would forfeit extras and add a third schema target.
3. **Where the rich protocol lives**: the addon supports the official KodiSyncQueue regardless (§8.4 ladder), so this is only about the fast path — ship it as the new KofinSyncQueue plugin (proposed: full control, clean protocol) or contribute additive v2 endpoints to upstream KodiSyncQueue (helps stock jellyfin-kodi users, but ties the fast path to upstream's release cadence).
4. **Recordings**: exposed as a dynamic browse node (no sync). If recording *libraries* should sync into Kodi's library, that's a small addition later.
