# pvr.kofin sync — implementation plan

| Field | Value |
|---|---|
| **Date** | 2026-09-01 |
| **Source** | `docs/syncplay-generic-backend-feasibility.md` §6.2 (live deferred behind recordings, position work behind the catchup/timeshift constraints) and `docs/syncplay-generic-backend-plan.md` §7 (contract-only integration; pvr.kofin excluded until it holds its own tempo route and claim delivery). This plan is that re-entry, for everything pvr.kofin plays: recordings, live channels, and catchup. |
| **Repos** | `pvr.kofin` (the adapter, stream properties, claim delivery), `plugin.video.kofin` (engine notes and a contract clarification only), `inputstream.tempo` (only if a probe finds an HLS gap), `jellyfin-plugin-syncplayv2` (only where a probe forces the descriptor route). |
| **Scope** | Three content classes in four tiers: recordings with fine sync (P1), live tune-together (P2), catchup sync (P3), live position convergence behind the edge (P4). Explicitly out: EPG-programme identity across *differing* channel line-ups ("watch the same programme wherever it airs" is future work — here a programme is identified through its channel). |
| **Rule** | Contract-only: kofin gains no matcher and no pvr knowledge; everything pvr.kofin needs to say crosses the public bus (`docs/syncplay-provider-contract.md`). Probes before design commits — each tier names the probes it is contingent on. Every tier carries its live gate before the next begins. |
| **Revised** | 2026-09-01: the P0 probes ran (`tests/live/results/P0-probes/PROBES.md`) and their verdicts are folded in below — the claim transport is the TCP JSON-RPC socket, the P1 initiator gains tempo by an engine-side reload policy, P3 is full fine sync from the start (inputstream.tempo is an ffmpegdirect fork and keeps the catchup machinery), and P4's anchor is the source PTS timeline, which every playback method shares. |

## 1. Why this order

Recordings first because they are the easy case on every axis and prove the shared machinery: a recording is a bounded file with a real runtime, the follower route through kofin's ordinary play path was already live-proven once (the withdrawn G2 gate), and the tempo route is stamped on a plain stream with no live edge in sight. P1 shipping means the claim transport (P0a) and the tempo stamping both work — everything later reuses both.

Live splits in two because its value does: tune-together (P2) is most of the point and needs no position model, while position convergence (P4) fights the live edge — a member can fall back into its timeshift buffer but can never play ahead of the edge, so naive convergence pins the fastest member and stalls. P4 targets a small fixed delay behind the edge and lets an edge-pinned member hit the pulse ladder's existing one-signed give-up, degrading to P2 behavior — the ladder's failure mode is the fallback, not a new mechanism.

Catchup (P3) sits between them and is deliberately ahead of P4: a catchup programme is a bounded stream anchored to a programme's own clock, so its position sync is two-sided like a recording's — the hard parts are identity and reach (can a second member be started into the *same* programme at a position), not actuation.

## 2. Decisions carried in

The claim names the tempo file and the engine arms from the claim (`tempo._arm` reads only the claim — the G2.3 proof). So pvr.kofin owns its tempo file path, stamps it through stream properties and repeats it in the claim; the `kofin.syncplay.tempo` window property, which a binary add-on cannot conveniently read, is never needed on this path. The contract doc gains one sentence saying so.

`RunTimeTicks: 0` already means "unbounded" end to end: the server does not clamp positions on it (G3.1), descriptors carry it (G3.3), and the engine passes it through (G3.6). A live claim is a 0-runtime claim; a recording or catchup claim carries its real runtime; no new field anywhere.

The claim transport is the localhost TCP JSON-RPC socket (:9090): `services.esenabled` defaults to on, the socket takes no auth, and a persistent connection also *receives* the bus — which is how a binary add-on hears `SyncSession.State`. P0a killed the alternatives (no builtin execution exists in the dev-kit) and named the one hazard: two Kodis on one host contend for the port, so a claim can land on the wrong Kodi's bus — a documented limitation, not a design driver.

The sync anchor is the **source PTS timeline**. P0d part 2 measured it: two simultaneous transcode sessions of one channel produced segments with identical start PTS, on the same clock as the raw tuner stream — Jellyfin preserves source timestamps per job, stream sharing or not. So direct-play, remux and transcode members all sit on one clock; catchup additionally carries a wall-clock base in its URL format by construction. No HLS manifest offers `PROGRAM-DATE-TIME` anywhere, and delay-behind-own-edge is retracted (edges differ in content position per member). What P4 needs built: tempo's `.state` reporting the absolute source PTS beside today's session-relative `content_ms` (owned code, small; mind the 33-bit TS rollover).

The provider is decided per class, and the cheap answer wins where it exists. Recordings are Jellyfin items, so their queue is plain GUIDs (`provider: "jellyfin"`) and needs no descriptor. Channels are Jellyfin items too, so if P0b finds kofin's play route streams a channel GUID, live needs no descriptor either. Catchup has no Jellyfin item to name — a past programme exists nowhere as an id — so catchup is descriptor-only by construction (`.8` server required); P0f settled the mechanics: inputstream.tempo is an ffmpegdirect fork that kept the whole catchup/timeshift machinery under its own property namespace, and the pulse actuates in catchup mode, so P3 is full fine sync, not command-only.

| # | Item | Delivers | Proof |
|---|---|---|---|
| P0a | Claim-delivery probe | how a binary add-on reaches the bus | bench evidence |
| P0b | Follower-tune probe | the live provider/key and follower start route | bench evidence |
| P0c | Tempo-on-live probe | pulses actuating a live HLS behind timeshift | bench evidence |
| P0d | Time-base probe | a wall-clock anchor and the natural edge spread | bench evidence |
| P0e | Recording-tempo probe | the tempo route on a recording stream | bench evidence |
| P0f | Catchup probe | catchup identity, reach, position start, tempo | bench evidence |
| P1 | Recordings with fine sync | the full ledger on a recording, both members pulsing | live two-member gate |
| P2 | Live tune together | channel changes propagate to the group | live two-member gate |
| P3 | Catchup sync | two members inside the same past programme, converging | live two-member gate |
| P4 | Live position convergence | members held together behind the edge | live gate, fine-sync ledger |

## 3. P0 — probes before design commits

**(a) Claim delivery from a binary add-on.** Whether `kodi::ExecuteBuiltin("NotifyAll(…)")` exists and delivers from pvr.kofin's API version; else the localhost JSON-RPC TCP socket (setting-gated — measure whether the setting is on by default on the target devices); else a companion script add-on shipped beside pvr.kofin whose only job is relaying bus messages. The answer ranks by what a user must not have to configure, and it serves every tier in this plan.

**(b) The follower tune route for channels.** Play a live channel GUID through kofin's play route on the bench: does `/Items/{id}/PlaybackInfo` answer for a channel, does the stream open, does reporting hold? If yes, followers stream the channel like jellyfin-web does and the live queue is plain GUIDs. If no: probe the native tune instead — what a JSON-RPC `Player.Open({channelid})` needs, and whether pvr.kofin can expose a GUID→channel mapping — and the live queue becomes a descriptor.

**(c) Tempo on a live stream.** pvr.kofin hands a channel stream to inputstream.tempo via `PVR_STREAM_PROPERTY_INPUTSTREAM` with `manifest_type=hls`: does playback survive, does a pulse actuate, and what does the timeshift buffer do at each end of a pulse? A negative result takes P4 off the table and P2 ships alone for live, which is still the bulk of live's value.

**(d) A shared time base for live.** Whether jf12's live HLS carries `EXT-X-PROGRAM-DATE-TIME` (or any wall-clock anchor two members can agree on), and how far apart two members' live edges naturally sit — if the natural spread is already a second or two, P4's target delay can be small; if it is tens of seconds, the buffer maths change.

**(e) The tempo route on a recording.** The same stream-property handoff as (c) but through `GetRecordingStreamProperties` on a bounded stream — expected to be the easy case, and the probe exists to catch the surprise, not to justify the design. Includes re-verifying the follower leg: a recording GUID through kofin's play route, which the withdrawn G2 gate proved once on an earlier tree.

**(f) Catchup identity and reach.** What identifies a catchup play inside pvr.kofin (the EPG tag's backend id, or channel + programme start), whether that identity is stable across two members against the same Jellyfin backend, whether a member can be *started* into a given programme at a given offset (the follower leg), and whether the catchup stream rides inputstream.tempo. This probe writes P3's key shape and start route; if reach fails — catchup can be entered only interactively — P3 collapses to "claim and spectate", recorded as such.

Evidence for all six lands in `tests/live/results/P0-probes/`.

**Executed 2026-09-01, all six answered** — `PROBES.md` there carries the detail, including the two corrections review forced (tempo's ffmpegdirect heritage; the retraction of delay-behind-edge) and the two lessons the burned-in-timecode evidence caught (an unbounded HLS catchup playlist is live-joined and the player's position silently lies; every rig script screenshots at errors and checkpoints). Deferred with reasons: buffer-end behavior under sustained pulses (P4 tier work) and the two-device edge-spread measurement (falls out of the P2 gate).

## 4. P1 — recordings with fine sync

**Change** (pvr.kofin and kofin, revised per P0a/P0e). pvr.kofin sends `SyncProvider.Claim` over the TCP socket when recording playback resolves: the recording's Jellyfin GUID as `key`, the real runtime and name, `play_method` per the stream — and **no tempo block**, because P0e proved the Recordings window's byte-stream path has no inputstream seat, so no stamp pvr.kofin could make would be true. Claims are playback-scoped and need no registration, so the P1 transport is fire-and-forget — one connection per message, no persistent socket yet. The initiator's tempo comes from kofin instead: the engine gains the provider-agnostic **reload-for-tempo policy** — a member adopting its own held play into the group, whose claim names `provider: jellyfin` but no tempo route, reloads the same GUID through kofin's ordinary play route at the group position when fine sync is armed, trading one visible reload for the full pipeline. Followers play the GUID through kofin exactly as any item.

**Proof.** pvr.kofin's suite covers the claim payload and sender; kofin L1s cover the reload policy (adopt keeps the running playback when the claim carries a tempo route or fine sync is off; reloads otherwise; the reload lands at the group position, zero included). Live gate on the P1D pair: a recording started from the PVR window becomes the group queue, the follower streams the same GUID through kofin, the initiator reloads once and then pulses — the part the withdrawn G2 gate could never show. The fine-sync ledger with confirmed pulses on both members is the tier's exit.

## 5. P2 — live tune together

**Change** (pvr.kofin and kofin, revised per P0b). A channel claim carries the channel's Jellyfin GUID and `RunTimeTicks: 0`, no tempo block. A channel change while in a group is a new claim; the initiator's hold-and-propose then re-proposes exactly as any content change does. The follower start is the probe's finding made code: kofin's play route grows a **live-TV leg** — a channel item resolves through `PlaybackInfo?autoOpenLiveStream=true` with the device profile, plays the returned `live.m3u8`, and stamps an inputstream for it (P0b showed the bare resolution reaches the demuxer unplayable; P0c showed the same stream plays through inputstream.tempo, which also arms fine sync for later). The contract doc records that a 0-runtime claim is a live claim and that convergence on it is command-only under this tier.

**Proof.** Live gate: tune from the PVR window propagates, both members on the channel inside a few seconds; join mid-programme, spectator demotion on a local tune-away, leave — all against jf12's tuner.

## 6. P3 — catchup sync

**Change** (revised per P0f: full fine sync from the start). A catchup play claims channel + programme identity as `key` under pvr.kofin's own provider name, the programme's runtime, and a tempo block — pvr.kofin names inputstream.tempo for catchup plays, emitting the same catchup properties under tempo's namespace, which P0f proved plays, seeks by URL regeneration, and takes pulses. The propose goes out as a descriptor (`Name` from the programme, so members without the provider still see what the group is watching). One design question the probes narrowed but did not close: a catchup follower cannot be started by a `plugin://` template (an EPG-tag play has no URL), so the start is either `Player.Open({broadcastid})` issued locally or a provider-executed start — a small contract extension decided at this tier, not before. Position is programme-relative — the anchor is the programme's own start, shared by construction — and convergence is two-sided, so this is recording-grade sync on archive content. Two constraints P0f wrote: an HLS catchup source must serve bounded, terminated windows (`catchup_terminates=true` — an unbounded playlist is live-joined and the player's position lies), and the gate asserts content position from the picture (the bench provider burns the content time into the frame), never from the player alone.

**Proof.** Live gate against the bench catchup provider (`P0-probes` rig: the timecoded programme, the wall-clock catchup endpoint, the request log): one member opens a past programme, the other lands inside the same programme at the group position; seek and pause choreography; the pulse ledger on both members. A member without catchup on that channel demonstrates the §14.4 graceful refusal once.

## 7. P4 — live position convergence behind the edge

**Change** (revised per P0c/P0d). The group position for a live item is defined on the **source PTS timeline** — P0d part 2 proved it is one clock for direct-play, remux and transcode members alike — never on per-member stream seconds or distance-to-edge. The build: tempo's `.state` grows an absolute source PTS field (owned code; 33-bit rollover handled), the engine reads it through the existing tempo route and reports position on it, and members converge by the ordinary pulse ladder a small fixed delay behind the newest PTS any member holds (sized at gate time from the measured spread, bounded by `stream_mode=timeshift`'s real disk-backed buffer rather than the 8 s demux queue). An edge-pinned member's one-signed residual ends pulsing for that member, which is P2 behavior — no new failure path.

**Proof.** The fine-sync rig's ledger shapes on a live channel: confirmed pulses, inter-member delta measured across ten minutes on the PTS clock, a deliberate edge-pin showing the graceful give-up. Results kept beside the P0 evidence.

## 8. Open items and fallbacks

A live member that pauses long enough slides out of its timeshift buffer; where the channel has catchup, re-anchoring the stalled member through the P3 machinery (re-enter the programme at the group position) is the natural recovery — noted as a follow-on once P3 and P4 both stand, not designed here.

Pause on live under P2 is whatever the member's timeshift makes of it; the plan promises command execution, not that a pause-behind-buffer member rejoins the group position without a seek. The gate records the observed behavior rather than legislating it.

Channel and recording access asymmetry (a member whose Jellyfin user cannot see the channel or recording) falls out of existing machinery: a jellyfin-key queue gets the stock access refusal; a descriptor queue gets the §14.4 member-side-unresolvable refusal. Nothing new to build, but the P2 gate should show one of them once.

Two residues the probes left: the two-Kodis-one-port contention (a claim sent to :9090 can reach the wrong Kodi on a shared host — documented, revisited only if a real deployment hits it), and production currently has no catchup source at all (the tuner M3U carries no catchup attributes), so P3's gate runs against the bench provider until a real one exists.
