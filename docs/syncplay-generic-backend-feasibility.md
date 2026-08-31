# A generic sync backend — SyncPlay v2 as a Kodi-wide coordinator: feasibility

Date: 2026-08-31
Scope: Can Jellyfin SyncPlay coordinate synchronized playback for **any** Kodi add-on's content — pvr.kofin channels and recordings, plugin.video.youtube videos, arbitrary streams — with the Jellyfin server acting purely as coordinator (group state, clock discipline, scheduled commands) and never hosting or resolving the media? What interface would other add-ons program against, and what does each layer of the existing stack need? Per direction, **protocol v2 is the floor**: stock v1 servers are examined only to establish why they cannot carry this (§3), not as a deployment target.
Sources: full read of `lib/kofin/syncplay/` and its integration surface (`service/main.py`, `service/player.py`, `service/remote.py`, `core/api.py`, `core/ipc.py`, `core/state.py`, `plugin/play.py`); `ref/jellyfin` @ v12.0-rc6 (stock SyncPlay: controller, `Group.cs`, states, `SessionManager`); `jellyfin-plugin-syncplayv2` (the owned v2 server plugin, vendored engine included); `inputstream.tempo` (property contract, stream classes, rate path); `pvr.kofin` (stream delivery, catchup); `syncplay-conformance/docs/SYNCPLAY.md` (the v2 spec) and its kit; `ref/jellyfin-web` `QueueCore.js`; `ref/plugin.video.youtube` (play route).

---

## 1. Verdict

**Feasible, and the stack is unusually well positioned for it — but only because every layer that matters is already owned.** The work splits into three parts with one architectural crux:

| Layer | Verdict | Why |
|---|---|---|
| Server (coordinator) | Feasible as a **v2 plugin extension** ("external content" queue entries). Not feasible on stock SyncPlay. | The library coupling in the group engine is six call sites in one vendored file, used for exactly two things: access checks and one `RunTimeTicks` clamp (§3, §4). Everything else — states, tolerances, barriers, beacons, snapshots, hot join, rendezvous — is content-blind. |
| Kodi engine | Feasible with a bounded refactor. | kofin's `lib/kofin/syncplay/` is five named couplings away from content-agnostic, and its seams (`SyncPlayApi`, `SyncPlayPort`, the tempo property contract) are already written down (§5). |
| Per-add-on interface | Feasible as a small **provider contract**: register, claim, start. | The engine drives the *global* Kodi player; a provider only has to say what is playing and how to start content at a position (§5.3, §6). |

The crux is **content identity on the wire**. The SyncPlay queue is a list of Jellyfin item GUIDs and the server refuses, at four enforcement points, any GUID the library cannot resolve for every member (§3). So foreign content needs a first-class *content descriptor* in the protocol — a v2-plugin extension (§4) — while Jellyfin-hosted content (everything pvr.kofin plays) needs **no wire change at all**, because its channels and recordings already have real item GUIDs (§6.2).

Fidelity is not uniform and should not be promised as such: direct streams routed through inputstream.tempo get today's fine sync (75 ms deadband, confirmed pulses); anything else gets command-converged sync, which is exactly the graceful degradation the engine already implements per item (§7).

Recommended shape: three phases (§9), the first two of which need no server change and prove the interface on real item ids before the protocol grows a descriptor.

## 2. The stack as it stands

Five repositories already form a complete, conformance-tested v2 system; the feasibility question is one of generalization, not construction.

- **`plugin.video.kofin`** — the only conforming v2 client (spec §13): manager + dispatcher (`syncplay/manager.py`), command scheduler and player choreography (`playback.py`), NTP-style time sync on the dedicated socket (`timesync.py`), fine-sync pulse scheduler (`tempo.py`). ~4,300 lines, transplant-hardened, with live rigs (`tests/live/syncplay_fine_sync.py`, `syncplay_music.py`).
- **`jellyfin-plugin-syncplayv2`** — the coordinator: DI-shadows stock `ISyncPlayManager` entirely (`PluginServiceRegistrator.cs:32-39`), vendors the group engine from the kontell fork, adds `Hello`, the dedicated `/SyncPlay/TimeSync` socket, `StateSnapshot`, `PositionBeacon`, `StateVersion`, hot join, rendezvous. ~5,100 lines (~3,600 vendored), built per Jellyfin ABI (10.11 / 12) with the build component gating features.
- **`syncplay-conformance`** — the spec (`docs/SYNCPLAY.md`) plus fake clients driving a real server with fault injection; also a reusable Python client core.
- **`inputstream.tempo`** — the fine-sync actuator: demuxer-side rate shifting (0.5×–100×, `FFmpegStream.cpp:3465`), commanded by an atomically-replaced tempo file, confirmed by a `.state` line per applied change. **Zero Jellyfin coupling in the binary** — the property contract, tempo file and state file are content-agnostic, the tempo file already lives at add-on-neutral `special://temp`, and two non-kofin consumers already drive it (KoShelf, and the patched YouTube add-on — §6.3).
- **`pvr.kofin`** — a Jellyfin Live TV PVR client whose playable objects are, server-side, real Jellyfin items.

The v2 architecture is three planes (spec §1): REST control (`POST /SyncPlay/*`), WebSocket feedback (commands, group updates, snapshots, beacons), and time sync (dedicated socket, discovered via `Hello`). The server schedules starts at `now + max(2×highest ping, 500 ms)`, tolerates per-member error at `clamp(2×ping, 500 ms, 2000 ms)`, and recovers unfixable members by rendezvous. None of that machinery knows what a movie is.

## 3. Why the queue is the crux — the server evidence

The one place SyncPlay touches the Jellyfin library is queue identity, and it is enforced hard. On stock v12.0-rc6 (`ref/jellyfin/Emby.Server.Implementations/SyncPlay/Group.cs:198-217`):

```csharp
var item = _libraryManager.GetItemById(itemId);
if (item is null || !item.IsVisibleStandalone(user)) { return false; }
```

`GetItemById` returns null for an unknown GUID, so **a fabricated id fails identically to a forbidden one**, and the check runs for *every current member*, not just the requester (`AllUsersHaveAccessToQueue`, `Group.cs:219-237`). Enforcement points: `SetNewQueue` (`Group.cs:490-513`), `Queue` (`:575-599`), `Join` (`SyncPlayManager.cs:171-178`, answered with `LibraryAccessDenied`), and — decisively — `List`/`GetGroup` (`SyncPlayManager.cs:281,306`): a group holding one unresolvable GUID becomes **invisible and unjoinable for everyone, its creator included**. The v2 plugin's vendored engine keeps the same shape (`Engine/Group.cs:263-270`), with the additional hazard that the file is `#nullable disable` and six sites dereference `GetItemById(...)` unguarded (`Engine/Group.cs:265, 1124, 1140, 1175, 1245, 1261`) — a synthetic id that slipped in would NRE inside the group lock (HTTP 500) and, worst, in `NextItemInQueue` the queue pointer advances *before* the deref, so the group's index and every client's view diverge permanently.

What the server actually *needs* from the item is one number. `RunTimeTicks` is read at those sites and consumed at exactly one place, `SanitizePositionTicks` (`Group.cs:430-434` stock; `Engine/Group.cs:1003-1006` plugin), the clamp applied to every `Seek` and every `Ready` report. With `RunTimeTicks == 0` every reported position clamps to 0 and the correction machinery fires permanently ("Session got lost in time", `WaitingGroupState.cs:453-467`). Everything else the coordinator does — the state machine, ready gating, buffering grace, group-wait deadline, snapshots, beacons, hot join, rendezvous, even auto-advance (fully client-driven via `NextItem`; the server holds **no timer** and computes position on demand as `PositionTicks + (UtcNow − LastActivity)`) — never reads media metadata at all.

Two stock side-channels exist and were examined so they can be **ruled out**: `GroupName` is a 200-char free string but write-once at creation (`Group.cs:115`) — a label, not a channel; and `POST /Sessions/{id}/Command` relays arbitrary `Arguments` dictionaries with an access check that is literally a no-op (`SessionManager.cs:1540-1545` null-checks and nothing else). The latter would technically carry per-item descriptors between members, but it is a bug-shaped foundation — undocumented, unscoped, and exactly the kind of thing an upstream security pass deletes. A protocol built on it inherits its lifespan. Not recommended.

Finally, v1 clients actively refuse foreign queues: jellyfin-web re-fetches every queued item on each `PlayQueue` update and either discards the update (single unknown id) or positionally corrupts the `PlaylistItemId` mapping (partially resolvable list) — `ref/jellyfin-web/src/plugins/syncPlay/core/QueueCore.js:117-160`. So descriptor-carrying groups must be **hidden from members that have not negotiated the capability**, which is consistent with what servers already do to groups a user cannot resolve.

## 4. The server work: external-content queue entries in the v2 plugin

The extension is narrow because the plugin already owns every choke point. Proposed wire shape, negotiated exactly like v2 was (a capability in `Hello`, a per-device registration in `ProtocolVersionRegistry`):

- A queue entry becomes `{ItemId}` **or** `{Content: {Provider, Key, Name, RunTimeTicks, ImageUrl?}}` — e.g. `{Provider: "youtube", Key: "dQw4w9WgXcQ", Name: "…", RunTimeTicks: 2120000000}`. `Provider:Key` is the identity; the server never resolves it.
- `SetNewQueue`/`Queue` accept descriptor entries via a plugin route (or the `ProtocolVersionSniffer` pattern — `Filters/ProtocolVersionSniffer.cs:31-58` already demonstrates reading raw bodies past stock model binding). Descriptor entries **skip** `GetItemById`/`IsVisibleStandalone`; item entries keep today's checks. Mixed queues are legal (a Jellyfin movie then a YouTube clip).
- `RunTimeTicks` comes from the descriptor; `0` means "unbounded" (live) and must **disable** the clamp for that item rather than clamp to 0 — this also fixes the latent stock bug where a deleted library item mid-queue NREs, and makes live TV items usable (§6.2).
- `PlayQueueUpdate` and `StateSnapshot` carry the descriptor alongside `{ItemId, PlaylistItemId}` for capability-negotiated members. The plugin's wire layer is built for this: `WireGroupUpdate.Data` is `object?` with an open string `Type` (`Wire/Wire.cs:19-23`), and every outbound message passes one send choke point (`Wire/Sender.cs`) where a plugin-owned `WirePlayQueueUpdate` can replace the stock DTO. Alternative: a server-side descriptor table keyed by the server-generated `PlaylistItemId`, fetched by a plugin GET — less elegant, zero change to existing DTOs.
- **Visibility**: groups whose queue contains any descriptor entry are filtered out of `List`/`Join` for members without the capability (the `ListShadowController` at `Order = -1` already owns that route). Same UX as today's unresolvable-queue filtering.
- The six unguarded `GetItemById` derefs get null-guards regardless — that is a robustness fix worth shipping on its own.

Cost estimate: this touches `Group.cs` (validation + RunTimeTicks sourcing + clamp), the wire layer, `Hello`, and the version registry — on the order of a few hundred lines against a 5,100-line plugin, plus spec sections and conformance scenarios (`syncplay-conformance` gains a `descriptor_queue` scenario family; the kit's fake clients make the barrier/tolerance behavior for descriptor items testable without any real media). The plugin's ABI build matrix and build-component versioning absorb the rollout the same way hot join (10.11.0.2) and rendezvous (10.11.0.4) did.

## 5. The Kodi side

### 5.1 What is already generic, and the five couplings that are not

The engine's important property: **it drives the global Kodi player, not kofin's playback**. Pause/seek/resume-verify choreography, the buffering watch (`Player.Caching`), the audio PAPlayer handling, hold-and-propose, spectator handling, the load-allowance estimator, and the pulse scheduler all operate on `xbmc.Player` and window state — they work identically whatever add-on produced the stream. The seams are even written down already: `syncplay/ports.py::SyncPlayApi` (the full 25-verb REST contract, structurally typed), `service/ports.py::SyncPlayPort` (the 6-method service contract), and an inbound wire surface of exactly three message types.

Five couplings bind it to kofin, all identified and all injectable:

1. `playback.py:597` — `plugin_url(...)` hardcodes `plugin://plugin.video.kofin` as the way to start an item. Becomes: dispatch through the provider registry (§5.3).
2. `manager.py:1535` — `kofin.sync.db.get_item(kodi_id, media)` maps a Kodi library id to a Jellyfin id when identifying a held playlist advance. Becomes: an injected resolver; kofin's stays the implementation for `provider=jellyfin`.
3. `player.current_item()` — the play-state claim (`{Id, PlayMethod, PlaySessionId, Tempo…}`) is the engine's identity and routing source. Becomes: a provider-neutral claim shape (§5.3); kofin's claim pipeline is the reference implementation.
4. `core.settings`/`core.state`/`core.toast` and 49 localized strings live in kofin's namespace. Cost of moving is real but mechanical (the translation toolchain makes it 28 files per string).
5. `core/ipc.py` is a **closed-world registry with a single sender by design**. A cross-add-on interface must not quietly open it; it needs a second, deliberately *public*, versioned message namespace (§5.3).

### 5.2 Where the engine lives: kofin hosts

Two hosting shapes were weighed, and the standalone one is **rejected, not deferred**:

- **kofin's service hosts the engine and publishes the provider contract** (decided). One Jellyfin session, one websocket, one credential store, one time-sync socket; the transplant's test protection (L2 discipline, live rigs, the shakedown culture) stays where it is; the engine keeps its current restart story. The dependency this creates — syncing YouTube requires plugin.video.kofin installed and signed in — is no real cost, because **every participant has to be logged on to the Jellyfin coordinator anyway** (§8): an account, `SyncPlayAccess`, and a live websocket. The logon is the irreducible requirement, and kofin *is* the logon owner in this ecosystem.
- A standalone `service.syncplay` add-on with its own login: rejected. It would have to duplicate the entire Jellyfin connection stack — auth, session, websocket, reconnect — to serve users who, by the previous point, do not exist: anyone in a position to join a group is already in a position to run kofin. It also runs a second server session per device, needs a new home for settings/strings/state, and severs the engine from the rigs that keep it honest. The provider IPC contract (§5.3) remains the public boundary regardless, so nothing about this decision is baked into what consumers program against.

### 5.3 The provider contract

Transport: Kodi's JSON-RPC notification bus (`NotifyAll`), which any Python add-on can send and any service can receive — the same mechanism as `core/ipc.py`, but as a **new public registry** (versioned message names, documented payloads, no nonce for the unprivileged surface), plus one window property for shared state, mirroring how `kofin.syncplay.tempo` already crosses process boundaries. Binary add-ons (PVR clients) cannot speak NotifyAll; §6.2 shows they do not need to.

The contract is four messages and one property:

- **`SyncProvider.Register`** (provider → service, on service announce): `{provider, version, play: {url_template}, capabilities: {start_at: bool, seek: bool, tempo: bool, live: bool}}`. `url_template` is how a follower starts content: e.g. `plugin://plugin.video.youtube/play/?video_id={key}&seek={position_s}` — plugin.video.youtube accepts `seek` today (`yt_play.py:189`), so followers can land near-position before the engine's ready-flow alignment even runs. A provider without `start_at` still works: the engine's existing post-load seek (`prepare_ready`, `_seek_and_settle`) closes the gap, at the cost of one visible jump.
- **`SyncProvider.Claim`** (provider → service, at resolve time): `{provider, key, name, runtime_ticks, play_method, play_session, tempo: {file, queue_secs, manifest_type}?}` — the generalization of kofin's play-state claim, published the way `stamp_tempo_route` + `PROP_SYNCPLAY_TEMPO` already work. The claim is what lets a member *propose* what it is playing to the group, and what routes fine sync. **Unclaimed playback keeps today's behavior**: the engine demotes the member to spectator (`_unmanaged_local_play`) rather than guessing — the correct default for every add-on that has not opted in.
- **`SyncSession.State`** (service → all, window property + change notification): `{in_group, group_name, members, phase, current: {provider, key}}` — what a provider UI needs to offer "watch together" affordances, exactly as `kofin.menu.syncplay` mirrors state to skins today.
- **`SyncSession.Propose` / `SyncSession.Menu`** (provider → service): propose `{provider, key, position_ticks}` as the group queue (the programmatic form of today's hold-and-propose), and open the group menu (today's `SYNCPLAY_MENU`, made public).

The service arms fine sync per claim exactly as today: a claim carrying a `tempo` route is pulse-scheduled; one without falls back to command-only sync, logged once. The tempo property contract (`inputstream` + `inputstream.tempo.{tempo,tempo_file,queue_secs,manifest_type}` ListItem properties) gets promoted from "implicit in a JSON blob" to a published page of the contract — it is already add-on-neutral.

### 5.4 What does not change

Time sync, command scheduling, the ready flow, reports, snapshots, beacons, rejoin, the dispatcher, the give-up logic: untouched. The refactor is dependency injection at five points, not a rewrite — and under transplant discipline it must be proven by the existing rigs (`syncplay_fine_sync.py`, `syncplay_music.py`) running unchanged against the refactored engine before any provider work lands.

## 6. Per-consumer integration

### 6.1 plugin.video.kofin

The reference provider. Its resolver, claim pipeline, tempo routing and startticks handling become the `jellyfin` provider implementation; behavior is identical by construction.

### 6.2 pvr.kofin — no protocol change needed

Server-side, everything pvr.kofin plays **is a Jellyfin item**: recordings are VOD items with real GUIDs and runtimes, channels are `TvChannel` items. So the wire needs nothing: a group queue of `[recording GUID]` passes today's v2 validation for any member whose user can see Live TV.

- **Recordings** are the easy, high-value case: seekable VOD, direct-URL delivery (`JellyfinRecordingManager.cpp:554+`), fine-syncable when direct-played (ffmpegdirect and tempo are cousins — the stream classes are the same lineage). This is the right proving ground for the whole interface, because it exercises everything except the descriptor.
- **Channels** are the live class. Two server-side facts shape it: a channel's `RunTimeTicks` is null → 0, so the §4 clamp fix is required even for this Jellyfin-hosted case; and "position" is wall-clock, so sync degrades to *tune together, pause together* — which is precisely what the v2 barrier machinery provides for free. True position alignment exists only where timeshift does: pvr.kofin's catchup path can start a channel at a wall-clock offset (`CatchupController.cpp:365-378`) but only direct-play and only with catchup tags Jellyfin does not supply (`IptvSimple.cpp:398-401`) — a later phase, if ever.
- **The integration mechanics**: pvr.kofin is C++ and cannot speak the NotifyAll contract, and it does not have to. The sync service observes PVR playback via JSON-RPC (`Player.GetItem` yields channel/recording identity) and maps it to Jellyfin GUIDs by asking Jellyfin itself (`/LiveTv/Recordings`, `/LiveTv/Channels` — the service already holds the credentials), or from a mapping file pvr.kofin drops in its addon_data. Follower start is native Kodi: `Player.Open({channelid|recordingid})`. So the PVR "provider" is an **adapter inside the sync service**, not a change to pvr.kofin — at most, pvr.kofin ships the mapping file to make identity exact instead of matched.

### 6.3 plugin.video.youtube — the descriptor case

Foreign content proper. Follower start: `plugin://plugin.video.youtube/play/?video_id={key}&seek={s}` (verified: `params.get('seek', …)` in `yt_play.py:189`). Identity: an adapter can parse the playing item's plugin URL for `video_id` without any cooperation from the add-on; a cooperating fork would `Claim` properly and could carry runtime. Fidelity: DASH via inputstream.adaptive cannot be tempo-routed (one inputstream per item), so the default is command-converged sync — the same tier as a Jellyfin transcode, and the engine's per-item fallback already handles it silently. The tempo *routing*, though, is already built and proven: the patched YouTube add-on (`ref/plugin.video.youtube` @ `bee4a524`, "Add inputstream.tempo support for audio and video playback") routes progressive streams through inputstream.tempo today — with the DASH-off constraint stated in its own commit message, a per-add-on tempo file, and the version gate on rate-shifted video. What sync adds to that patch is small: point the route at the SyncPlay session's tempo file (the `PROP_SYNCPLAY_TEMPO` handshake, exactly as `plugin/play.py::tempo_route` does) and publish the `Claim` — the stream-routing problem, the hard half, is solved. The cost of the progressive route remains adaptive quality switching; a per-user opt-in, not a default. Runtime for the descriptor comes from the add-on's own metadata at propose time.

## 7. Fidelity by content class

Set expectations per class rather than per add-on — the engine already thinks this way (`tempo._arm` routes per claim):

| Class | Sync mechanism | Expected quality |
|---|---|---|
| Jellyfin VOD, direct play | Commands + confirmed tempo pulses | Today's fine sync: 75 ms deadband, pulses land within a few percent of asked, devices free-run within a few hundred ppm |
| Jellyfin VOD, transcode | Commands + reload-seeks + learned gain | Today's behavior: sub-second after convergence, reloads on seek |
| Foreign VOD, direct URL through tempo | Same as Jellyfin direct | Near-identical — the actuator and scheduler are content-blind; the load allowance is measured per device, so slow resolvers (a YouTube URL fetch) are absorbed the way transcode startup already is |
| Foreign VOD, not tempo-routable (ISA/DASH/DRM) | Command-only | Converged at each command within transport seek accuracy; drift between commands uncorrected (beacons still re-reference every 5 s) |
| Audio (any provider) | Command-only, PAPlayer choreography | Today's music behavior |
| Live PVR channel | Barrier semantics only: tune/pause/stop together | No position sync without timeshift; the §4 clamp fix required so reports are not zeroed |

## 8. Risks and hard requirements

- **Every participant needs a Jellyfin account on the coordinator, `SyncPlayAccess`, and a live websocket** — the session dies ~60 s after keep-alives stop and the member is ejected (v2's 90 s disconnect grace softens, not removes, this). This is the premise's cost: the server only coordinates, but everyone must be its client.
- **Content resolvability is now the *members'* problem, not the server's.** A descriptor the proposer can play may be unplayable for a member (region lock, missing add-on, version skew). The engine's existing answer — load failure → leave with a toast (`_load_failed`) — is correct but blunt; the descriptor's `Provider` field lets a member refuse gracefully at queue time ("provider not installed"), which the UX should use.
- **Do not build on the `Sessions/{id}/Command` relay** (§3) however tempting a zero-server-change channel looks: its permission check is a no-op today and its semantics are owned by upstream.
- **The public IPC namespace must stay disciplined.** The closed-world registry exists because ~30 ad-hoc window properties were the disease; a public contract needs the same rigor in the opposite direction — versioned, documented, additive-only, with the guarded/unguarded split thought through (nothing in this contract needs the nonce; nothing irreversible crosses it).
- **Transplant discipline applies to the refactor.** The five injection points restructure ported code; the existing live rigs must pass unchanged before and after, and the seams get L2-style tests (a fake provider) the way `synchost.py` fakes the library.
- **Vendored-engine drift**: the plugin's `Group.cs` is meant to return upstream; the descriptor extension enlarges the delta. The mitigation is what `VENDORED.md` already practices — keep deviations annotated and minimal, and keep the clamp/null-guard fixes separable since they are upstreamable on their own merits.
- **v1/jellyfin-web members** never see descriptor groups (§4 visibility rule); mixed-protocol groups on pure Jellyfin queues behave exactly as today. This is a feature, not a gap — jellyfin-web would corrupt its queue mapping otherwise (`QueueCore.js:148-150`).
- External-player configurations (`playercorefactory.xml`) remain unsupported, now for every provider (`syncplay/offer.py` already gates on it).

## 9. Recommended phasing

1. **Seam extraction inside kofin** (no behavior change): inject the URL builder, the id resolver, and the claim source; promote the claim/tempo shapes to documented types. Proven by the existing L2 suite and both live rigs running unchanged. This is worth doing even if nothing else ships — it turns the five couplings into tested interfaces.
2. **Provider contract + PVR recordings adapter** (no server change): publish the IPC namespace, implement the in-service PVR adapter, sync recordings on plain v2. This proves cross-add-on sync end-to-end on real item ids, with real ffmpegdirect streams, before the protocol grows anything.
3. **Descriptor extension**: v2 plugin (`SetNewQueue` descriptors, clamp fix, visibility filtering, `Hello` capability), spec sections, conformance scenarios, then the YouTube adapter as the first consumer. Live channels ride along via the clamp fix.

A fourth phase — extracting the engine into a standalone service add-on — was considered and dropped (§5.2): group membership requires a Jellyfin logon, so an engine host without kofin serves nobody.

## 10. Alternatives considered

- **A dedicated non-Jellyfin coordinator** (e.g. the syncplay.pl protocol, or a bespoke relay): discards the owned, conformance-tested v2 server, its clock discipline, its barrier/rendezvous machinery, and Jellyfin's auth — and no Kodi client exists for any of them. Rebuilding §7-§11 of the spec elsewhere is strictly more work than extending the plugin that already implements them.
- **Kodi-to-Kodi master/slave over JSON-RPC** (one box remote-controlling others): no clock synchronization, no scheduled starts, no ready barrier, no recovery semantics; every hard-won behavior in the spec would need reinventing, and the shakedown docs are a catalogue of why naive position-chasing fails.

The conclusion runs the other way: the Jellyfin server is the *right* coordinator precisely because the v2 stack has already paid for the hard parts — and because, end to end, every line that needs to change is in a repository this project controls.
