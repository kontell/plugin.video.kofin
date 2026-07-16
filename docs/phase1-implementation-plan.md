# plugin.video.kofin — Phase 1 implementation plan

Date: 2026-07-16. Operationalizes build phase 1 from `rewrite-research.md` §11; the report governs architecture — where this plan is more specific it decides, where it conflicts the report wins unless noted. Test gates reference `testing-plan.md`.

**Phase 1 deliverable**: a working, sync-less Jellyfin addon — settings-driven login (password + Quick Connect), dynamic browsing of all server libraries, direct-play/transcode playback decided by a pvr.kofin-style device profile, playback reporting, native resume, the transcode-bitrate context item, remote-control target, and a service that restarts reliably. No database writes of any kind.

---

## 1. Scope

**In**: addon skeleton + tooling; `core/` (log/masking, settings, state, IPC, HTTP, auth, API, device profile, websocket); service lifecycle + player reporting + remote-control subset; plugin router/browse/listitems/play; Account + Transcoding settings tabs; transcode context item; Add-user-to-session root item.

**Out (deliberately)**: everything sync (jellyfin.db, writers, views/nodes, Library/Sync tabs), segments, Up Next, SyncPlay, extras, cinema mode/trailers, folder-play & queueing context, the general "Kofin options" context menu, Playback/Interface/Advanced tabs (their features arrive with them), Piers live testing. Watched/favorite context toggles on dynamic items are the one small userdata write kept in phase 1 (they're two POSTs, and browsing without them feels broken).

## 2. Decisions locked in phase 1 (later phases build on these)

* **Identity**: addon id `plugin.video.kofin`, name "Kofin", provider `kontell`, GPL-3.0. Client identity header: `Authorization: MediaBrowser Client="Kofin", Device="<deviceName>", DeviceId="<deviceId>", Version="<addon version>", Token="<token>"` — header only, never query-string tokens (exception: none in phase 1).
* **addon.xml**: `xbmc.python` 3.0.1; requires `script.module.requests` (2.22.0+matrix.1) and `script.module.websocket` (1.6.4) only. Extension points: `xbmc.python.pluginsource` (`provides>video audio image`) with **`<reuselanguageinvoker>true</reuselanguageinvoker>`** — this is what makes the plugin-process `requests.Session` genuinely persistent (report §5.2); it also makes the no-mutable-module-globals rule load-bearing from day one. `xbmc.service` with `start="login"`. One `kodi.context.item` (Play with transcoding, visible on `!String.IsEmpty(ListItem.Property(kofin.id))`).
* **Routes**: query-style `plugin://plugin.video.kofin/?mode=<name>&...`; one router dict in `plugin/router.py`. Phase-1 modes: `login logout testconnection restart browse play playtranscode watched unwatched favorite unfavorite adduser`. Later phases add modes; names never change meaning.
* **Settings ids are API**: hidden level-4 keys `isLoggedIn accessToken userId deviceId serverName serverId displayUser`; visible keys per §6 tables. The settings *store* is the only durable state in phase 1.
* **IPC message names** (NotifyAll sender `plugin.video.kofin`): `Restart`, `AuthChanged` — declared in `core/ipc.py`; nothing sends a string not defined there. (Server online/unreachable/unauthorized turned out to need no cross-process messages — each is detected and surfaced inside the process that owns it.)
* **Window props** (`core/state.py`, the only three): `kofin.online` (bool), `kofin.play.json` (resolved-play handoff queue, plugin→service), `kofin.playing.id`. Anything else someone wants must argue its way into state.py.
* **Masking policy** (`core/log.py`, single chokepoint): tokens/passwords/api_key values → `***`; userId/deviceId → first 6 chars + `…`; applied to every log line via regex on known patterns *and* explicit redact calls at the sites that handle secrets. Always on; no setting.
* **Log levels**: `debug` → `xbmc.LOGDEBUG` (visible only with Kodi debug logging), `info` → `LOGINFO`, warnings/errors accordingly. No addon log-level setting, ever.
* **Strings**: `strings.po` ranges — 30000–30049 general, 30050–30099 Account, 30100–30199 Transcoding, 30200+ reserved for later tabs. Every visible setting has label *and* help string.
* **Python**: 3.9-conservative style (no `match`, no PEP 604 annotations), enforced at **3.10** by mypy — current mypy supports no older target, and every real Omega build ships ≥3.11 (Kodi's own depends, LibreELEC/CoreELEC). Type hints everywhere, `black` formatting, no `__future__`, no module-level mutable state (lint-guarded by review; the reuselanguageinvoker + soft-restart designs both depend on it — documented exception: the append-only masking registry in `core/log.py`).

## 3. Repo bootstrap (step 0)

`git init` in `plugin.video.kofin/`; GPL-3 LICENSE; `.gitignore` (pyc, .venv, tests/live/results); tooling ported from the fork: `tox.ini` (envs: black-check, mypy, pytest), `requirements-dev.txt` (pytest, mypy, black, Kodistubs), `mypy.ini`. `tools/dev-install.sh`: rsync the tree (minus docs/tests/tools) to `~/.kodi/addons/plugin.video.kofin/`, then `kodi-builtin 'UpdateLocalAddons()'` and enable via JSON-RPC — the kodi-drive loop from the testing plan. `tools/unwrap_md.py` already exists.

## 4. Module map (target tree, port sources, rough size)

| Module | Source / nature | ~Lines |
|---|---|---|
| `addon.xml`, `service.py`, `default.py`, `context_play.py` | new, thin (entry stubs calling into `lib/`) | 90 |
| `lib/core/log.py` | new — xbmc.log adapter, lazy formatting, masking chokepoint | 90 |
| `lib/core/settings.py` | new — typed get/set, credential store helpers, `Settings` snapshot object | 130 |
| `lib/core/state.py` | new — the three window props, typed | 60 |
| `lib/core/ipc.py` | new — message registry, `notify()`/`decode()`, upnext-style hexlify helper (used later) | 70 |
| `lib/core/http.py` | rewrite of `jellyfin_kodi/jellyfin/http.py` — one Session per process, keep-alive, retries w/ backoff+jitter, `(connect, read)` timeouts, error taxonomy (`ServerUnreachable`, `Unauthorized`, `HTTPError`) | 160 |
| `lib/core/auth.py` | slimmed single-server port of `connect.py`/`connection_manager.py` — address normalization, `/System/Info/Public` ping, AuthenticateByName, Quick Connect (port of upstream #1117 flow), logout, credential read/write to hidden settings | 220 |
| `lib/core/api.py` | trimmed port of `jellyfin_kodi/jellyfin/api.py` — browse queries, item(s), PlaybackInfo, sessions (playing/progress/stopped/capabilities), userdata (played/favorite), images, QuickConnect endpoints | 350 |
| `lib/core/deviceprofile.py` | **Python port of pvr.kofin `BuildDeviceProfile`** (`JellyfinChannelLoader.cpp`) + kofin additions: video `audioBitrate`/`maxAudioChannels` on transcode profiles, music profile (`musicMaxBitrate`/`musicTranscodeCodec`/`musicTranscodeBitrate`), per-play `MaxStreamingBitrate` override for the context item | 260 |
| `lib/core/ws.py` | port of `ws_client.py` (30s keepalive) + jittered reconnect backoff | 180 |
| `lib/service/main.py` | new — Service object, `while not aborted: build; run; if restart: continue` loop, notification hub (IPC subset), capabilities registration, online/offline handling with backoff (no modals) | 260 |
| `lib/service/player.py` | port of `player.py` **reporting core only** (no segments/syncplay/upnext hooks — those graft on in phases 3/4): onPlayBackStarted handshake with `kofin.play.json`, session_playing, 10 s progress ticker owned by the player (replaces the old 1 s service poll), pause/resume/seek/stop, transcode close, external-player guard | 380 |
| `lib/service/remote.py` | port of `monitor.py` subset — remote `Play` (with `order_from_start_index` fix), `Playstate` (stop/pause/unpause/seek), `GeneralCommand` (volume/mute/DisplayMessage), server restart/unreachable notifications | 260 |
| `lib/plugin/router.py` | new — mode dict → handlers, param parsing | 80 |
| `lib/plugin/browse.py` | port of `default.py` `browse()`/`browse_subfolders()`/`browse_letters()`/DYNNODES minus multi-server, minus `get_video_extras` stub; root listing = views + Add user + Settings | 420 |
| `lib/plugin/listitems.py` | **rewrite** of `objects/actions.py` `set_listitem` on `InfoTagVideo`/`InfoTagMusic` setters (`setResumePoint`, `addVideoStream/addAudioStream/addSubtitleStream`, `setCast`, `setUniqueIDs`…) — the largest genuinely new chunk; obj_map stays behind in phase 2, this builds listitems straight from the DTO | 380 |
| `lib/plugin/play.py` | port of `helper/playutils.py` decision core minus every dialog: PlaybackInfo(profile) → default source → direct-play vs transcode URL → external subs URLs → `setResolvedUrl` → push play-state to `kofin.play.json` | 380 |
| `resources/settings.xml` | new (Account + Transcoding sections) | 360 |
| `resources/language/.../strings.po` | new | 130 entries |
| Unit tests (`tests/unit/`) | new + adapted fork patterns | 600 |

## 5. Work breakdown (ordered; each step lands green on L0)

1. **Bootstrap** (§3). *DoD*: `tox` green on an empty package; `dev-install.sh` installs a stub addon that appears in Kodi. (S)
2. **Skeleton addon**: addon.xml, entry stubs, root listing with a placeholder item; service logs start/stop banners. *DoD*: enabled via JSON-RPC, root opens, `kodi-logtail errors` clean. (S)
3. **core foundations**: `log` (+masking), `settings`, `state`, `ipc`. *DoD*: L1 tests — masking never leaks a token/api_key/password in any format tried; settings round-trip; ipc encode/decode. (M)
4. **HTTP + auth + api**: session/keep-alive/retries; login (password), Quick Connect, logout; api surface for phase 1. *DoD*: L1 with mocked transport (retry/backoff, 401→`Unauthorized`, address normalization: bare host → http://host:8096, https passthrough, trailing-slash strip); live smoke: authenticate against minipie test user via `tools/` snippet. (L)
5. **Account tab + login UX**: settings.xml Account section per §6, `mode=login` flow (ping → Quick Connect offer with code dialog + poll, or keyboard user/pass), logout with confirm, test-connection notification, hidden-cred writes, visibility deps. *DoD*: **S1.2, S1.3** pass; masking grep of a full login session clean (part of S1.2). (M)
6. **Service lifecycle**: Service object + restart loop; websocket connect after auth; `/Sessions/Capabilities/Full` registration; online/offline notifications with backoff; `mode=restart` → IPC → teardown (join deadlines) → rebuild. *DoD*: **S1.9** (20× bounce loop: one banner per cycle, single server session, no errors). (M)
7. **Browse + listitems**: router, root listing (views via `/Users/{id}/Views`), library browse + DYNNODES subfolders + letters/genres, listitem builder with art/streams/resume, watched/favorite context toggles. *DoD*: **S1.4** headless `Files.GetDirectory` suite; InfoTagVideo fields spot-checked via JSON-RPC `Files.GetDirectory` properties. (L)
8. **Device profile + Transcoding tab**: port BuildDeviceProfile; settings section per §6. *DoD*: L1 matrix — for each (codec allowed/excluded × HDR allowed/excluded × 10bit/rext flags × force toggles × resolution/bitrate caps) assert the profile JSON's DirectPlayProfiles/CodecProfiles/TranscodingProfiles exactly; goldens reviewed once against pvr.kofin's C++ behavior. (L)
9. **Play + reporting**: resolve flow, `kofin.play.json` handoff, player callbacks + 10 s ticker, stop cleanup (session_stopped + close transcode), native resume in listings (`setResumePoint`) and honor `sys.argv[3] resume:`. *DoD*: **S1.5, S1.6, S1.7** against the test server. (L)
10. **Transcode context item**: `context_play.py` → `mode=playtranscode`; bitrate select from `contextBitrates` (single value ⇒ bypass); per-play profile override. *DoD*: **S1.8**. (S)
11. **Remote control subset**: `remote.py` wiring; verify "Play On → Kodi" from another Jellyfin client, Playstate seek/pause, volume command, message display. *DoD*: manual remote-drive session logged; no unhandled-command stack traces. (M)
12. **Hardening pass**: Add-user root item; error-path sweep (server down mid-browse → one notification, listing fails soft; 401 mid-session → single re-auth hint); S1.1 screenshot set + tooltip audit; exit checklist (§8). (M)

Steps 7 and 8 can proceed in parallel after 4; everything else is effectively linear.

## 6. Settings spec (phase-1 tabs)

### Account (`category id="account"`, strings 30050+)

| id | type/control | default | deps/notes |
|---|---|---|---|
| `serverAddress` | string/edit | "" | enabled only when `isLoggedIn=false`; help explains host[:port] or full URL |
| `loginButton` | button/action → `RunPlugin(...mode=login)` `close=true` | | visible when logged out |
| `serverName` | string/edit, read-only (enable=false) | "" | visible when logged in |
| `displayUser` | string/edit, read-only | "" | visible when logged in |
| `testConnection` | button → `mode=testconnection` | | visible when logged in |
| `logoutButton` | button → `mode=logout` | | visible when logged in; confirm dialog inside handler |
| `restartAddon` | button → `mode=restart` | | always visible |
| `sslVerify` | bool/toggle | true | help: disable only for self-signed certs |
| `deviceName` | string/edit | "Kodi" | re-registers capabilities on change (settings-diff handler) |
| hidden lvl-4 | `isLoggedIn accessToken userId deviceId serverName serverId displayUser` | | written only by auth.py |

### Transcoding (`category id="transcoding"`, strings 30100+; ids/controls mirror pvr.kofin, values are real units not enum indexes)

| id | type/control | default (proposal) |
|---|---|---|
| `forceDirectPlay` | bool | false |
| `forceRemux` | bool (hidden when forceDirectPlay) | false |
| `forceTranscode` | bool (hidden when forceDirectPlay) | false |
| `directPlayVideoCodecs` | list[string] multiselect | h264,h264_10bit,hevc,hevc_rext,av1,mpeg2video,vp9,vc1 |
| `directPlayAudioCodecs` | list[string] multiselect | aac,mp2,mp3,ac3,eac3,opus,flac,dts |
| `allowedHdrTypes` | list[string] multiselect | all (HDR10, HLG, HDR10+, DOVI variants — pvr.kofin's set) |
| `preferredVideoCodec` | spinner h264/hevc/av1 | h264 |
| `preferredAudioCodec` | spinner aac/ac3/mp3/opus | aac |
| `maxAudioChannels` | spinner 2/6/8 | 6 |
| `maxStreamingBitrate` | spinner Mbps 3/6/10/15/20/25/40/60/120/unlimited | unlimited |
| `maxResolution` | spinner 720p/1080p/4K/unlimited | unlimited |
| `audioBitrate` | spinner kbps 96/128/192/256/320/384/448/640 | 384 |
| `contextBitrates` | list[string] multiselect, Mbps 1/2/3/4/6/8/10/15/20/25/40/60/120 | 3,10,20 |
| *Music group*: `musicMaxBitrate` | spinner kbps 128/192/256/320/unlimited | 320 |
| `musicTranscodeCodec` | spinner aac/mp3/opus | opus |
| `musicTranscodeBitrate` | spinner kbps 96/128/192/256/320 | 128 |

All non-obvious semantics carry help strings (which codecs mean "will direct play", what force-remux does, why HDR exclusions force transcode). Defaults marked *proposal* — flag any you want changed before step 8 pins the matrix goldens.

## 7. Key flows (behavioral contracts)

* **Login**: normalize address → `GET /System/Info/Public` (name/id/version; failure = one notification) → if `/QuickConnect/Enabled` offer select [Quick Connect | Username & password] → QC: `Initiate` → code in a progress dialog → poll `Connect?secret=` (1.5 s, cancelable) → `AuthenticateWithQuickConnect`; or keyboard user/pass → `AuthenticateByName` → write creds to hidden settings, set `isLoggedIn`, notify service via IPC → service starts websocket + registers capabilities. Never a modal from the service side.
* **Play resolve** (plugin process): `mode=play&id=` → `GET item` → `POST PlaybackInfo` with the device profile (context item passes an overridden `MaxStreamingBitrate` + force-transcode profile) → default MediaSource → direct-play URL `/Videos/{id}/stream?static=true&MediaSourceId=…` (anonymous by design, same as the music endpoints — the report §10 risk entry covers a future lockdown, with Kodi's `url|Header=Value` pipe syntax as the ready fallback) or the server-issued `TranscodingUrl` for HLS (it embeds an api_key — the masking chokepoint must cover it wherever the URL is logged) → external-sub URLs via `setSubtitles` → InfoTag + `setResumePoint` → `setResolvedUrl` → append play-state dict to `kofin.play.json`.
* **Reporting** (service): `onPlayBackStarted`/`onAVStarted` claims the entry from `kofin.play.json` → `session_playing`; ticker thread posts `session_progress` every 10 s while playing; pause/resume/seek callbacks report immediately; `onPlayBackStopped/Ended` → `session_stopped` + `close_transcode` if transcoding; player owns its ticker lifecycle (no service polling).
* **Soft restart**: IPC `Restart` → service sets stop flag, joins ws + ticker threads (deadline 5 s, log if breached), closes session, rebuilds Service object in the outer loop. Twenty consecutive cycles must leak nothing (S1.9's assertion).
* **Settings-diff handlers** (phase-1 subset of the engine): `deviceName` → re-register capabilities; `sslVerify`/`serverAddress` (only editable logged-out) → picked up at next login; transcoding keys → read fresh at each play (no action).

## 8. Exit checklist (phase gate)

* L0 + L1 green in tox; device-profile matrix goldens reviewed; masking unit tests include every credential format used.
* Live: S1.1–S1.9 all pass on this box against the minipie test user (scripts + evidence in `tests/live/results/`).
* Masking grep over a full debug-logged session (login → browse → play → restart): zero hits for token/api_key/password.
* `kodi-logtail errors` clean across the smoke set; no leftover state on the box beyond the installed addon (skill cleanup rule).
* Report §3/§4 cross-check: anything phase 1 implemented differently than the report describes → report updated in the same commit.
