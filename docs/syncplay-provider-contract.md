# The SyncPlay provider contract, v1

Date: 2026-08-31
Status: implemented (plan G2 and G3.6, `feat/sync-provider-contract` + `feat/syncplay-descriptors`).
Audience: any Kodi add-on that wants its content driven by the kofin-hosted SyncPlay engine — proposing what it plays to a group, and letting followers start it.

## 1. The model

The kofin service hosts the one SyncPlay engine per Kodi (feasibility report §5.2): it owns the group membership, the websocket, time sync, command execution and fine sync, and it drives the **global** Kodi player. A *provider* is anything that owns content: it tells the engine what is playing (**claim**), how to start its content at a position (**register**), and may ask for its item to become the group queue (**propose**). Everything else — pause/seek/resume choreography, ready reports, barriers, fine-sync pulses — is the engine's and needs nothing from the provider.

Trust model: the bus is local and unguarded on purpose. Nothing irreversible crosses it, and a hostile local add-on can already write any window property; a nonce here would be ceremony. kofin's own destructive IPC stays in its closed, nonce-guarded registry (`core/ipc.py`), which this contract deliberately does not touch.

## 2. Wire

Send with `JSONRPC.NotifyAll`, your own add-on id as the sender; payloads are JSON objects carrying `"v": 1`. Unknown fields are ignored (additive evolution); an unsupported `v` is dropped, never guessed at. Payload cap 8 KiB.

```python
import json, xbmc

def sync_send(message, data):
    xbmc.executeJSONRPC(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "JSONRPC.NotifyAll",
        "params": {"sender": "plugin.video.example",
                   "message": message,
                   "data": dict(data, v=1)}}))
```

## 3. Messages

### `SyncProvider.Register` (provider → service)

`{"v": 1, "provider": "<name>", "play": {"url_template": "...", "audio": false}}`

`provider` is your namespace: lowercase `[a-z0-9._-]`, 2-40 chars, and never `jellyfin` (that slot is the engine's own). `url_template` is how a follower starts your content: a `plugin://` URL naming `{key}` (required) and optionally `{position_s}` (whole seconds). Substitution is token-replace with the key URL-quoted — the template is treated as opaque text, so braces beyond the two tokens are left alone. Without `{position_s}` the engine still converges position after the start (one visible seek). `audio: true` routes starts to the music playlist.

A provider whose content is *tuned rather than fetched* — a PVR EPG tag has no URL any template could carry — registers `{"play": {"delegated": true}}` instead of a template: the engine then broadcasts `SyncSession.Start` (below) when a follower must start that content, and the provider executes the start itself; the playback it produces completes the engine's load like any local play, and the ordinary load watchdog covers a start nobody executes.

Register on start-up **and every time you see `SyncSession.State`** (§4): the service announces on its own start, and registrations live only as long as a service generation — there is no persistence, by design (`kodi-addon-lifecycle`: generations overlap and rebuild).

### `SyncProvider.Claim` (provider → service)

`{"v": 1, "provider": "<name>", "key": "<content id>", "play_method": "DirectPlay|DirectStream|Transcode", "play_session": "..."}`

Sent when your add-on resolves playback: it is how the engine knows what is on screen, which is what lets a member *propose* your item to the group and lets a spectator's own playback be told apart from the group's. Only `provider` and `key` are required. kofin's own claim always wins when both exist; a claim is playback-scoped and dropped when the player stops. A `tempo` block — the inputstream.tempo route of Kodi 21 and 22 — is accepted and ignored on Kodi 23 (§6).

A claim with no `runtime_ticks` is a **live** claim — a channel, not an item. Positions on a live stream are session-relative, so the engine never seeks one and never reads its player clock as a group position. **A live channel is tuned, not fetched, so its claim belongs to a delegated provider** (see Register), not to a provider with a play template: a template would send followers to a URL of the engine's making while the member who pressed play watches the channel its own add-on tuned — two pipelines for one group, and the follower without the programme information, the live OSD or the timeshift bar its add-on would have given it. pvr.kofin claims live channels and catchup programmes alike under its own name for exactly that reason, telling them apart by the key, and executes both starts itself (measured on the rig, 2026-09-02: a follower on the template route opened `plugin://…?mode=play&id=` while the initiator played `pvr://…`; delegated, both open the same `pvr://` channel). The engine places a live member on a **live clock** — the stream's own timestamps when Kodi reports them (`presentationtime`, kontell/xbmc #19: a broadcast's PTS, which every member's stream carries whatever moment it opened; an inputstream reports it through `INPUTSTREAM_TIMES::ptsPresentationOffset`), and otherwise the server time at which its player clock read zero, which is the live edge it opened at — and converges it by pulses alone where Kodi can play the stream at a tempo (a stream with a time range to move in: a timeshift buffer or a live window, kontell/xbmc #25): a proposer anchors the group 5 s behind its own reading, a member behind pulses forward until it meets the group or runs into its live edge (Kodi puts it back to 1.0 there; two such returns end fine sync for it), a member ahead holds still for the excess (its timeshift buffer keeps the picture) and pulses back the remainder. A member Kodi cannot play at a tempo, or a group anchored on a proposer's session time, tunes together on commands only. The source clock wraps every 2³³ ÷ 90 kHz; the engine compares on that circle, and a live group position on the wire is offset by ten days so it cannot be mistaken for session time.

Playback with no claim at all keeps today's behavior: the engine demotes that member to spectator rather than guessing — the correct default for every add-on that has not opted in.

### `SyncSession.Propose` (provider → service)

`{"v": 1, "provider": "<name>", "key": "<content id>", "position_ticks": 0, "name": "...", "runtime_ticks": 0}`

Asks for the item as the group's queue (the programmatic form of the engine's hold-and-propose). A `jellyfin` key is proposed as a plain item GUID and works against any v2 server. Any other provider's key goes out as an external-content descriptor (SYNCPLAY.md §14) built from the payload — `name` (display, ≤256) and `runtime_ticks` are optional, and a zero runtime means "unknown": the server then treats positions on the item as unbounded rather than clamping them. That leg needs the server to have negotiated the `ExternalContent` capability; without it the propose is refused with a log line, never silently downgraded to a key the group cannot resolve. Ignored outside a group.

### `SyncSession.Menu` (provider → service)

`{"v": 1}` — opens the SyncPlay group menu on the service's worker, exactly as kofin's own root entry does. This is how a provider UI offers "watch together".

### `SyncSession.Start` (service → the named provider)

`{"v": 1, "provider": "<name>", "key": "<content id>", "position_ticks": 0}` — the engine asks a **delegated** provider (see Register) to start its content at a position. Broadcast like every bus message; filter on your own provider name. Fire-and-forget by design: fail quietly and the engine's load watchdog gives playback back to the member.

### `SyncSession.State` (service → everyone)

`{"v": 1}` — a ping saying the session state changed: **read the property, never a payload** (a payload could be overtaken; the property cannot). Sent on group join and leave, on service start (your cue to re-register), and on service stop.

## 4. The state property

`syncsession.state` on the home window, JSON:

`{"v": 1, "in_group": bool, "group_name": "...", "members": ["name", ...], "phase": "idle|loading|waiting_ready|synced", "current": {"provider": "...", "key": "..."} | null}`

Deliberately not kofin-prefixed: the name is part of this contract and survives any future re-hosting of the engine. Cleared when the service stops.

## 5. What a provider never has to do

Observe the player, execute commands, report ready/buffering, correct drift, handle snapshots or rejoins — the engine does all of it against the global player, whatever add-on produced the stream. A provider that only ever wants to *follow* (its content started on it by others) needs exactly one message: `Register`.

## 6. Fine sync

On Kodi 23 fine sync needs nothing from you. The engine pulses whatever plays through Kodi's own tempo (kontell/xbmc #14 and #16), whenever Kodi says the playing item can take one (`Player.TempoEnabled`), and leaves it to the commands when it cannot. What decides that is your stream, not your adapter: a live stream needs a time range to move in, so play it through an inputstream with a timeshift buffer or a live window. kofin no longer publishes `kofin.syncplay.tempo`; an adapter written for Kodi 21 and 22 that reads it finds nothing and plays its stream plainly, which is what Kodi's tempo needs.

On Kodi 21 and 22 (kofin's api and sql branches) fine sync goes through inputstream.tempo instead, and those branches' copy of this contract describes the route.

## 7. Worked example: a cooperating video add-on

Integration is the add-on's own adapter — kofin ships no per-add-on shims, and an add-on that sends nothing is a spectator whose playback is simply its own (§3's no-claim default). The whole adapter is two touches. On service start and on every `SyncSession.State` announce, send `Register` with your play template. When your play route resolves an item, send `Claim` with your provider name and content id. Nothing else: hold-and-propose, follower starts through your template, pause/seek/spectator and fine sync all run in the engine, and your stream converges to tens of milliseconds like kofin's own wherever Kodi can play it at a tempo (§6).
