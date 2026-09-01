# Generic sync backend — implementation plan

| Field | Value |
|---|---|
| **Date** | 2026-08-31 |
| **Source** | `docs/syncplay-generic-backend-feasibility.md` §9 (three phases; the standalone-host fourth was rejected in §5.2 — group membership requires a Jellyfin logon, and kofin is the logon owner). Protocol v2 only throughout. |
| **Repos** | `plugin.video.kofin` (G1, G2, G3.6-G3.8), `jellyfin-plugin-syncplayv2` (G3.1-G3.4), `syncplay-conformance` (G3.5), the youtube tempo patch atop `bee4a524` (G3.7, optional variant). |
| **Branches** | One per phase in each repo: `refactor/syncplay-seams` (G1), `feat/sync-provider-contract` (G2), `feat/syncplay-external-content` (G3 client), plugin builds `x.y.0.7` (G3.1 alone) and `x.y.0.8` (G3.2-G3.4). One commit per item, each revertible on its own. |
| **Scope** | Everything needed for pvr.kofin recordings and plugin.video.youtube to sync through the kofin-hosted engine. Explicitly out: live-channel position sync (tune-together only, and only after G3.1), a standalone service host, any v1 accommodation beyond the visibility filtering v2 already implies. |
| **Rule** | Transplant discipline: G1 is a restructure of ported code and lands only when both live rigs (`tests/live/syncplay_fine_sync.py`, `tests/live/syncplay_music.py`) pass **unchanged** against it. Nothing in `core/ipc.py`'s closed registry is opened; the public bus is a separate module with its own rules. Every phase carries its unit proof and its live gate before the next begins. |
| **Amended** | 2026-09-01, §7: contract-only integration. The in-service matcher tier (G2.5, G3.7's identity half) is withdrawn and pvr.kofin leaves the scope; phase G4 carries the withdrawal and the fork-side youtube adapter. G1–G3 below stand as the executed record. |

## 1. Why this order

G1 creates the seams without changing behavior, so its correctness is *identity* — the cheapest thing to prove and the foundation everything else injects into. G2 exercises the full cross-add-on path (bus, claims, registry, adapter, follower start) on **real Jellyfin item ids**, so no server or wire change can be blamed when something misbehaves. Only G3 grows the protocol, and by then the client interface it feeds has been live-tested for a phase.

| # | Item | Delivers | Proof |
|---|---|---|---|
| G1.0 | Rig baselines | the before-state both rigs must reproduce after | rig result snapshots |
| G1.1 | Provider port + registry | `play_item`/`_start_item` dispatch through `Provider` | L1 + byte-identical play URL |
| G1.2 | Held-play resolver injection | `manager.py:1535`'s `sync.db` import behind the port | L1 |
| G1.3 | Typed claim | the claim dict as a documented TypedDict with `Provider` | mypy + L1 |
| G1.4 | Live identity gate | both rigs unchanged | rig runs vs G1.0 |
| G2.0 | Probes | the three facts G2's design rests on | bench evidence in `tests/live/results/` |
| G2.1 | Public bus | `SyncProvider.*` / `SyncSession.*` messages | L1 validation tests |
| G2.2 | Session state property | `syncsession.state` mirror | L1 + skin-side read |
| G2.3 | Claim intake | foreign claims reach `_local_file_info` | L1 |
| G2.4 | Registry intake | registered play templates become providers | L1 |
| G2.5 | PVR recordings adapter | initiate from the Recordings window, followers stream | live two-device gate |
| G2.6 | Contract doc + strings | `docs/syncplay-provider-contract.md`, i18n | `test_translations.py` |
| G3.1 | Server hardening | null-guards + no-clamp-at-zero, shipped alone | plugin unit tests + conformance |
| G3.2 | `Hello` capability | `Capabilities: ["ExternalContent"]`, per-device registry | conformance |
| G3.3 | Descriptor queue entries | `SetNewQueueEx`/`QueueEx`, side-table by `PlaylistItemId` | plugin unit tests |
| G3.4 | Wire + visibility | descriptors in `PlayQueue`/`StateSnapshot`; groups hidden from non-capability members | conformance |
| G3.5 | Spec + scenarios | SYNCPLAY.md § External content; `descriptor_*` scenario family | kit green on both ABI rows |
| G3.6 | Engine descriptor path | propose/apply foreign content through the registry | L1 + live |
| G3.7 | YouTube adapter | identity, follower start, optional tempo variant | live two-device gate |
| G3.8 | End to end | the full ledger on the rig | results recorded |

## 2. Decisions carried in from the report

kofin's service hosts the engine; the provider IPC contract is the public boundary; nothing about hosting leaks into what consumers program against (feasibility §5.2).

Ids stay **opaque strings inside the engine**. The queue mirror, `current_item_id`, reports and commands are all keyed the way they are today; interpretation lives at the provider seam. G3 adds descriptor parsing at exactly one boundary (`_apply_play_queue`/snapshot), nowhere else — this is what keeps G1 small.

The PVR adapter lives **inside the sync service**; pvr.kofin (C++, no NotifyAll) changes only if G2.0c forces a mapping file. Followers play recordings through kofin's normal play route — stream URL, resume/report pipeline, tempo-routable — not through the PVR window. G2.0a verifies the assumption this rests on before any adapter code exists.

The public bus carries nothing irreversible, so it is unguarded — no nonce, no HMAC. A hostile local add-on can already write any window property on the box; the bus adds no privilege it does not have. `core/ipc.py`'s registry, sender and nonce are untouched.

No new master setting: `syncPlayEnabled` gates the whole feature, providers included. A provider that registers while SyncPlay is off gets no announce and no state property, which is the existing "disabled means inert" contract.

## 3. Phase G1 — seams inside kofin (no behavior change)

### G1.0 — Baselines

Run both rigs on the pre-G1 tree and keep the results (`tests/live/results/G1.0-before/`): the fine-sync rig's pulse/align ledger and the music rig's boundary-hold ledger are the oracles G1.4 diffs against. `tox` green is the L1 baseline.

### G1.1 — The provider port

**Change.** New `syncplay/providers.py`: a `Provider` Protocol in the `ports.py` style — `play_target(key: str, start_ticks: int) -> PlayTarget` where `PlayTarget` is `{url: str, audio: bool}` — and a `ProviderRegistry` holding providers by name with `"jellyfin"` as the default route. `playback.play_item` loses the `plugin_url` import and the `item` dict: it takes `(target, start_ticks)` and keeps the choreography (stop-and-wait, playlist clear/add, `play`) exactly as it is. `manager._start_item`'s `api.item()` lookup moves into `JellyfinProvider.play_target` (it is what needed the item: the URL id and the audio/video playlist choice); the load-failure semantics (`_load_failed` on a lookup or start error) stay in the manager. `JellyfinProvider` lives beside the registry and is constructed by the service with the api client and the plugin-URL builder injected.

**Proof.** L1: a fake provider records the dispatch; `JellyfinProvider.play_target` produces the byte-identical `plugin://plugin.video.kofin/?mode=play&id=…&startticks=…` string today's code builds (assert the string, not the behavior); audio items still select the music playlist. mypy checks the Protocol the way `SyncPlayApi` is checked at the `service/main.py` construction site.

### G1.2 — The held-play resolver

**Change.** `Provider` gains `resolve_kodi_id(kodi_id: int, media: str) -> Optional[str]`; `manager._identify_held_play` calls the registry instead of importing `kofin.sync.db` (the deferred import moves into `JellyfinProvider`, same comment, same laziness). A `None` from every provider keeps today's `_unmanaged_local_play` path.

**Proof.** L1: the existing held-play tests in `test_syncplay_manager.py` re-pointed at the fake provider; one new test that an unmapped id still demotes to spectator.

### G1.3 — The typed claim

**Change.** A `Claim` TypedDict in `syncplay/ports.py` naming the keys the engine actually reads (`Id`, `PlayMethod`, `PlaySessionId`, `Tempo`, and new optional `Provider` defaulting `"jellyfin"`). `current_claim()`/`_local_file_info` behavior unchanged; this is documentation-as-types plus the one field G2 needs.

**Proof.** mypy; no runtime change to assert beyond the suite staying green.

### G1.4 — Live identity gate

Both rigs against the G1 tree, results diffed against G1.0: the same scenarios pass, the `[ syncplay/play ]`, `[ syncplay/align ]` and `[ syncplay/pulse ]` ledgers show the same shapes, and a group start's resolved play URL is identical in the log. Any deviation is a G1 bug by definition.

## 4. Phase G2 — the provider contract and PVR recordings

### G2.0 — Probes before design commits

(a) Play a Live TV **recording** GUID through kofin's play route on the bench (`plugin://…mode=play&id=<recording-guid>`): does `/Items/{id}/PlaybackInfo` answer for recordings, does the stream resolve, does reporting work? (b) Capture `Player.GetItem` (and the OnPlay notification payload) for a PVR recording and a channel on both supported generations — what identity is actually readable. (c) List `/LiveTv/Recordings` and check the fields against what (b) exposes: is a name/path match deterministic, or does pvr.kofin need to drop a `recordings.map` in its addon_data? Each probe's evidence lands in `tests/live/results/G2.0-probes/`. **The follower design of G2.5 is contingent on (a); the fallback if it fails is JSON-RPC `Player.Open({recordingid})` on followers with adapter-synthesized claims and command-only sync — decided then, not now.**

### G2.1 — The public bus

**Change.** New `core/contract.py` (deliberately not in `core/ipc.py`): the envelope `{v: 1, sender: <addon id>, data: …}`, the four message names — `SyncProvider.Register`, `SyncProvider.Claim`, `SyncSession.Propose`, `SyncSession.Menu` — a validating decoder (size caps, required keys, unknown-field tolerance), and a documented sender snippet consumers copy rather than import. Receiver wiring joins the `_KODI_HANDLERS`/`_IPC_HANDLERS` tables in `service/main.py`; `SyncSession.Menu` lands on the same worker as `ipc.SYNCPLAY_MENU` (which stays, unchanged, as kofin's own root-entry path). Malformed messages are logged and dropped — never an exception on the notification thread.

**Proof.** L1: decoder accepts the documented shapes, rejects oversize/missing-key payloads, tolerates additive fields; handler dispatch tests in the existing service-harness style.

### G2.2 — The session state property

**Change.** One new property in `core/state.py` — `syncsession.state`, JSON: `{in_group, group_name, members: [names], phase, current: {provider, key} | null}` — with the module-charter argument written above it: it is cross-process shared live state that foreign add-ons must read, which is exactly what the module exists to hold. Published by the manager on join/leave, queue application and phase transitions (a `_publish_session_state` called from the dispatcher, so ordering is free); cleared in `_leave_locally` and the service teardown list.

**Proof.** L1: property content across a scripted join/queue/leave sequence; teardown clears it.

### G2.3 — Claim intake

**Change.** `SyncProvider.Claim` payloads (the G1.3 `Claim` shape plus `provider`/`key`) are held by the service as the *foreign claim*, consulted by `_local_file_info` only when kofin's own play-state claim is absent, and invalidated on `onPlayBackStopped`/`onPlayBackEnded`. A foreign claim carrying a `Tempo` route arms the pulse scheduler exactly as kofin's does — `tempo._arm` already reads only the claim.

**Proof.** L1: kofin's claim wins when both exist; a foreign claim identifies a held start; invalidation on stop; a claim with a tempo route arms the scheduler against a scripted state file.

### G2.4 — Registry intake

**Change.** `SyncProvider.Register` records `{provider, play: {url_template}, capabilities}` and wraps the template as a `TemplateProvider` in the G1 registry (`{key}` and `{position_s}` substitution, `audio` from the registration). No persistence: the service broadcasts its presence (the `SyncSession.State` publish at `mark_ready`) and providers answer by re-registering — restart-safe with no module globals, per the shell rules.

**Proof.** L1: registration round trip; template substitution including the zero-position case (`startticks=0` must survive — the same falsy-zero trap `play_item` documents); an unregistered provider in a queue item falls to `_load_failed`.

### G2.5 — The PVR recordings adapter

**Change.** In-service adapter (new `syncplay/adapters/pvr.py`): on the Kodi bus `Player.OnPlay` with an item of type recording, read the JSON-RPC details, resolve the Jellyfin GUID per G2.0c, and synthesize a claim (`provider="jellyfin"`, `key=<guid>`) so the existing hold-and-propose path runs unmodified. Followers need nothing: the queue item is a plain Jellyfin GUID and resolves through `JellyfinProvider` (per G2.0a). Channels are explicitly not in this item.

**Proof.** L1 with recorded G2.0 payloads as fixtures. Live gate (new `tests/live/syncplay_pvr.py`, results kept): two devices; A starts a recording from the PVR Recordings window and proposes; B follows streaming through kofin; group pause/seek/spectator/leave all exercise; the fine-sync ledger shows pulses when B's stream is direct-played.

### G2.6 — Contract doc and strings

**Change.** `docs/syncplay-provider-contract.md`: the four messages, the claim shape, the state property, the template rules, and the inputstream.tempo ListItem property contract promoted from implicit to published (the §5.3 "page of the contract"). New user-visible strings (adapter toasts, provider-missing errors) go through the i18n toolchain — id block after 30599, all 26 JSONs, `gen.py`/`validate.py`/`pocheck.py` in one commit.

**Proof.** `test_translations.py`; the doc reviewed against what G2.1-G2.5 actually built.

## 5. Phase G3 — the descriptor extension

### G3.1 — Server hardening, shipped alone

**Change** (`jellyfin-plugin-syncplayv2`, build `x.y.0.7`). Null-guard the six unguarded `GetItemById` derefs (`Engine/Group.cs:265, 1124, 1140, 1175, 1245, 1261`) — a vanished item logs and no-ops instead of NREing inside the group lock, and `NextItemInQueue` guards *before* advancing the queue pointer. `SanitizePositionTicks` treats `RunTimeTicks == 0` as unbounded (`max(0, ticks)`) instead of clamping to 0. Both are stock bugs too; keep the commits upstreamable and annotated per `VENDORED.md`.

**Proof.** Plugin unit tests for both behaviors; the conformance suite unchanged and green on both ABI rows — this build must be indistinguishable in every existing scenario.

### G3.2 — Capability negotiation

**Change** (build `x.y.0.8` from here). `Hello` response gains `Capabilities: ["ExternalContent"]`; `ProtocolVersionRegistry` records the capability per device from the `Hello` body (absent = none, same downgrade semantics as the version). Spec §2.1 gains the field.

**Proof.** Conformance: a client that never sends the capability never sees a descriptor (asserted in G3.4's scenarios).

### G3.3 — Descriptor queue entries

**Change.** Plugin routes `POST /SyncPlay/SetNewQueueEx` and `POST /SyncPlay/QueueEx`: entries are `{ItemId: guid}` or `{Content: {Provider, Key, Name, RunTimeTicks, ImageUrl?}}` (caps: Provider/Key length, Name length, non-negative ticks). Item entries keep `HasAccessToQueue`; descriptor entries skip it. Descriptors are stored in a side-table keyed by the server-generated `PlaylistItemId` beside the vendored `PlayQueueManager` (whose `Guid` playlist is untouched — descriptor entries carry a sentinel item GUID internally, never surfaced); `RunTimeTicks` for the playing item comes from the side-table when one exists.

**Proof.** Plugin unit tests: mixed queue accepted, validation split correct, side-table follows queue mutations (`Remove`, `Move`, `Next`/`Previous`), runtime sourcing.

### G3.4 — Wire and visibility

**Change.** For capability members, the send choke point (`Wire/Sender.cs`) emits a plugin-owned `WirePlayQueueUpdate` whose playlist entries carry `Content` where a descriptor exists; `StateSnapshot.PlayQueue` likewise. Members without the capability: any group whose queue holds a descriptor entry is filtered from `List`/`GetGroup` (the `ListShadowController` already owns the route) and `Join` answers `LibraryAccessDenied` — byte-for-byte the UX stock gives a queue you cannot resolve.

**Proof.** Conformance (G3.5's scenarios); a jellyfin-web smoke check that a descriptor group simply does not appear.

### G3.5 — Spec and conformance scenarios

**Change** (`syncplay-conformance`). SYNCPLAY.md gains the External content section (entry shape, capability, visibility rule, the RunTimeTicks-0 semantics) and §12 rows for the new caps. New scenarios: `descriptor_queue_basic` (barrier, ready gating, scheduled start on a descriptor item — no media exists anywhere), `descriptor_visibility` (non-capability member sees no group, join refused), `descriptor_no_clamp` (Ready reports at large positions on a 0-runtime item are not zeroed), `descriptor_mixed_queue` (advance from item to descriptor and back). Kit fake clients grow the capability flag.

**Proof.** Suite green on 10.11 and 12 rows; the scenario table in the README maps each to its section.

### G3.6 — The engine's descriptor path

**Change** (kofin). `Hello` handling records the server capability. A propose whose claim has `provider != "jellyfin"` goes through `syncplay_set_new_queue_ex` (new `SyncPlayApi` verbs, Protocol updated) with the descriptor built from the claim; without server capability the propose is refused with a toast (never silently downgraded to a broken GUID). `_apply_play_queue` and `_apply_snapshot` read `Content` per entry; a descriptor entry resolves `play_target` through the registry by `Provider`; a provider that is not registered fails the load exactly as an item lookup failure does today. Everything downstream — reports, commands, NextItem — is already `PlaylistItemId`-keyed and does not change.

**Proof.** L1: queue application with mixed entries dispatches correctly; propose builds the documented body; capability-absent propose refuses. mypy on the extended Protocol.

### G3.7 — The YouTube adapter

**Change.** In-service adapter (`syncplay/adapters/youtube.py`): identity by parsing the playing item's `plugin://plugin.video.youtube/play/` URL for `video_id`; registration of the template `plugin://plugin.video.youtube/play/?video_id={key}&seek={position_s}` (the `seek` parameter exists at `yt_play.py:189`); runtime for the descriptor from `player.getTotalTime()` at propose time, `0` if unreadable (the G3.1 no-clamp path carries it). The cooperating variant is a small patch on top of the existing tempo patch (`ref/plugin.video.youtube` @ `bee4a524`): read `PROP_SYNCPLAY_TEMPO` and stamp the session's tempo file instead of its own, and send a proper `SyncProvider.Claim` — at which point progressive playback gets fine sync.

**Proof.** L1 for URL parsing and template registration. Live gate: two devices, YouTube group start, seek, pause, spectator; with the patched add-on and DASH off, the pulse ledger shows confirmed pulses on progressive streams.

### G3.8 — End to end

The full ledger on the rig, results recorded: a mixed evening — a Jellyfin movie, a PVR recording, a YouTube clip in one group queue — with joins mid-item (hot join), a transcoding member (rendezvous), and a v2-without-capability device confirming it never sees the descriptor group.

## 6. Open items and fallbacks

G2.0a failing (recordings do not resolve through PlaybackInfo) switches G2.5's follower to `Player.Open({recordingid})` with adapter claims on both ends and command-only sync — the adapter's shape survives, the fidelity row moves down one tier.

G2.0c failing (no deterministic recording match) adds one pvr.kofin item: write `recordings.map` (Kodi recording id → Jellyfin GUID) to its addon_data on every recordings refresh — a C++-side file write, no IPC.

Live channels stay out until recordings prove the adapter and G3.1 is deployed; then "tune together" is a channel adapter reusing the G2.5 shape, and position work waits on the catchup/timeshift constraints the feasibility report records.

The descriptor's `ImageUrl` is carried but unused by kofin initially; it exists so a future member UI can show what the group is watching without a provider installed.

## 7. Amendment (2026-09-01) — contract-only integration, phase G4

Decided after G3.8: the in-service matcher tier is withdrawn. A matcher can never wire fine sync — only the add-on that builds the resolved ListItem can route it through inputstream.tempo — so matcher-tier membership is capped at command-only forever, and command-only membership is not worth a second integration tier ("not much point in sync without tempo"). The public contract (G2.1–G2.4, G2.6) becomes the **only** integration surface: kofin ships the engine and the contract; a target ships its own adapter — register on the announce, claim on resolve with a `tempo` block, ListItem routed through inputstream.tempo. An add-on that does not integrate is a spectator, which is the contract's stated default, not a degraded membership kofin maintains on its behalf.

pvr.kofin leaves the scope with the tier. A PVR-playing member has no tempo route kofin can supply (recording playback never passes a ListItem kofin resolves), so its membership was the permanent command-only case. Excluded, not rejected: it re-enters when pvr.kofin can hold both halves itself — hand recording playback to inputstream.tempo through its recording stream properties, and deliver its own `Register`/`Claim` (a binary add-on has no NotifyAll; the JSON-RPC TCP socket or a companion script add-on are the known routes). Until then a recording playing in a group is spectator playback like any other unintegrated add-on, and G2.7's planned `tests/live/syncplay_pvr.py` rig is dropped with it.

What the executed record still proves is unchanged by the withdrawal: G2.5's live gate proved hold-and-propose, follower start and claim intake end to end — a synthesized claim enters `_local_file_info` exactly where a bus claim does — and G3.6–G3.8 proved the descriptor path, the registry dispatch and a bus-registered provider on two real members. G4 removes the *source* of the synthesized claims, not the paths they proved.

| # | Item | Delivers | Proof |
|---|---|---|---|
| G4.1 | Withdraw the matcher tier | `syncplay/adapters/` deleted with its service wiring and the PVR identity helpers | `tox` green with the matcher suites dropped; the bus-claim and registry L1s unchanged |
| G4.2 | Contract doc revision | cooperation is the model, spectator the default | doc reviewed against the post-G4 tree |
| G4.3 | The youtube fork adapter | `Register` + `Claim` + tempo atop `bee4a524` | live two-member gate: the first contract fine-sync proof |

### G4.1 — Withdraw the matcher tier

**Change** (kofin). Executed as a rewrite of the open stack (2026-09-01), so the tier never lands: PR #214 now carries the contract without `syncplay/adapters/`, and PR #215 the descriptor path without the YouTube matcher. The withdrawal inventory — nothing of it reaches `main`: the adapters package (`pvr.py`, `youtube.py`, the `claim_soon`/`register_builtin_providers` dispatcher), the two `service/main.py` wiring points (the OnPlay probe, the built-in registration at SyncPlay start), `providers.py`'s `recording_guid` and the `media == "recording"` arm of `resolve_kodi_id`, and `core/kodirpc.py`'s `pvr_recording_file`/`playing_file`/`playing_details` (all matcher-only — `addon_details` stays, `tempo.py` reads it). Everything the matchers fed is untouched: `on_foreign_claim`, the registry and `TemplateProvider`, the descriptor propose/apply, the state property.

**Proof.** The rewritten-tip-vs-old-tip diff is exactly the matcher inventory (482 deletions, no other change); every bus-path L1 (claim intake, registry round trip, descriptor dispatch) passes unchanged, which is the demonstration that the contract never depended on the tier. mypy, pytest, ruff, black green at both rewritten commits; CI on both PRs.

### G4.2 — Contract doc revision

**Change.** Folded into the rewritten G2 commit, so the doc lands already right: §7's worked example ("no cooperation needed") is replaced by the cooperating add-on's own adapter as the worked example (three touches: register, claim, tempo route), the spectator default is stated as the whole story for unintegrated add-ons, and §6 (the tempo route) is the reason to integrate. No wire change — the messages, shapes and property are already v1-frozen.

**Proof.** Doc reviewed against the rewritten tree; no separate code.

### G4.3 — The youtube fork adapter

**Change** (the youtube fork, atop the tempo patch at `bee4a524`). The adapter is fork-side and is what G3.7 called the "cooperating variant", now the only variant: send `SyncProvider.Register` (the `/play/?video_id={key}&seek={position_s}` template) on start-up and on every `SyncSession.State` announce; on play resolve, read `kofin.syncplay.tempo`, stamp the session's tempo file on the ListItem instead of its own, and send `SyncProvider.Claim` with the `tempo` block, the title and the runtime. kofin changes not at all — that is the point of the phase.

**Proof.** Live two-member gate, results kept: group start from a registered template, seek and pause choreography, and the pulse ledger showing confirmed pulses on a progressive stream — the first live proof of fine sync over the contract, closing the gap that every prior gate drove claims from inside kofin.
