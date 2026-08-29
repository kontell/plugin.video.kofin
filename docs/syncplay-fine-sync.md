# SyncPlay fine sync — tempo pulses through inputstream.tempo

Phase B of the `inputstream.tempo` video-tempo study (`../inputstream.tempo/docs/tempo-for-video.html`, §4.8 and §7). Phase A, the add-on side, shipped as inputstream.tempo 21.4.0 / 22.4.0; this is the kofin side: routing, the pulse scheduler, and the session bookkeeping around it.

## 1. Why there is a controller again

`docs/syncplay-drift-shakedown.md` §10 retired the `Player.SetTempo` ladder because its precondition — Kodi's "Sync playback to display" — slaves the media clock to the panel and imposed rate errors up to 4.3 % on the rig, far beyond the ±3 % the ladder could command. The same measurement found that with that setting **off**, all three devices free-run within a few hundred ppm of real time. What was missing was an actuator that works in that configuration.

inputstream.tempo is that actuator. It rate-shifts inside the demuxer: audio through `atempo`, stamped at output rate, so Kodi's audio sink — and therefore its clock — keeps running at real time; video and subtitle packets projected through the same content↔output map so they stay locked to the audio; and the reported position corrected to the playing point, not the demux head. Its rig results (`../inputstream.tempo/tests/live/results/av-tempo-video.md`) measured +30 029 ppm for a requested +30 000 on the Tab with the display clock off, a readout bias of +5 ms with the queue correction in force, and accurate seeks matching Kodi's own demuxer.

So the rule in `CLAUDE.md` stands — no continuous loop against a biased reading — and what is added is not that. Between commands the scheduler issues one **pulse** at a time, confirms it was applied, measures its effect from the add-on's own account, and waits a full queue depth before looking again.

## 2. Routing (`plugin/play.py`)

While the service is in a group with fine sync armed it publishes `kofin.syncplay.tempo` (`core/state.py`): the per-session tempo file and the queue depth in force. The play route reads that property and, for a **video** item resolved to **DirectStream** or a local **DirectPlay** file, stamps the inputstream.tempo contract on the ListItem — `inputstream`, `tempo=1.0`, `tempo_file`, `queue_secs` — and carries the same route in the play claim as `Tempo`, which is how the service knows the playback is nudgeable. Transcodes are not routed (the add-on's HLS path is unqualified, study §4.7) and audio never is.

Every codec is routed, AV1 included — but not before a detour. On a Pixel 7, a rate change followed by a seek wedged the AV1 hardware decoder (`dequeueInputBuffer failed` storm, frozen or corrupt picture with the clock running on) and a second run watchdog-rebooted the phone, while the Bravia's AV1 decoder took the same sequence cleanly. The cause was on the add-on side: its post-seek probe loop freed the first packets after a seek, so every decoder was started mid-GOP (`[hevc] Could not find ref with POC` on the desktop after every seek through the add-on, none natively); inputstream.tempo 22.4.1 / 21.4.1 keeps those packets, after which the desktop's HEVC errors went to zero and the same Pixel passed AV1 at 1.03× and 1.25× with seeks. An AV1 toggle existed for a day and was removed.

`start_time` is deliberately not set: it arms the add-on's PAPlayer silence hold, and VideoPlayer seeks the demuxer before any output starts, so resume through `setResumePoint` works unchanged.

The tempo file is kofin's own (`special://temp/kofin_syncplay_tempo`), never koshelf's, so an audiobook and a group session cannot write over each other. It is reset to 1.0 with its `.state` removed at every join.

## 3. The session (`syncplay/tempo.py::TempoSession`)

At group join (not at a re-join of the same group) the manager calls `begin()`, which arms only when `syncPlayTempo` is on and inputstream.tempo is installed, enabled and at least x.4.1 on its channel (JSON-RPC `Addons.GetAddonDetails`; x.4.0 dropped the first packets after a seek). Audio passthrough is simply suspended for the session — the add-on decodes to PCM — and the setting's help says so; there is no second toggle. The reasons for not arming are logged as `[ syncplay/tempo ] fine sync …` and nothing else changes — the group behaves exactly as before.

On Kodi 22, with `syncPlayShortQueue` on, `videoplayer.queuetimesize` is set to 1 s for the session. The setting is read when the player object is constructed, so it applies to every play that starts after the join, which is all of them. The original value is written to the hidden `syncPlayQueueRestore` setting *before* the queue is touched, and only if that write stuck (a JSON-RPC setting write is not saved to disk by Kodi, so an unrecorded shortening would survive a clean exit — the Pixel was found that way); it is put back at leave, and a crash mid-session leaves the record behind for `service/main.py` to restore at the next start. Kodi 21 has a fixed 8 s queue and is left alone. The queue depth in force is what the play route stamps as `queue_secs`, so the add-on reports time at the playing point.

`end()` clears the property, writes 1.0 to the file (any playback still running returns to real time), and restores the queue.

## 4. The scheduler (`syncplay/tempo.py::PulseScheduler`)

Driven from the controller's existing 250 ms loop, only in the `synced` phase after an Unpause, and only for a routed item (it arms from the claim's `Tempo`, re-arming whenever the PlaySessionId changes; an unrouted item is logged once and left to the commands).

Each tick appends `group estimate − player position` to a 3 s window. Once the window is full the median decides:

- inside the deadband (75 ms — one frame is 42 ms, and the Tab's position reads jitter by about that; at 50 ms it pulsed ±50 ms against its own noise): nothing;
- up to the pulse budget (*Largest error closed by tempo*, default 2.5 s): a pulse `(r, T)` with `(r − 1) × T` equal to the residual. The rate scales with the residual between 0.5 % and the *Fastest correction speed* (default 25 %), aiming at a 5 s pulse: 100 ms is 2 % for 5 s, 1 s is 20 % for 5 s, 2.5 s is 25 % for 10 s. Above 5 % the rate is ramped in and out in 5 % steps every 250 ms — not seamless, just less of a jolt — and the hold is shortened by what the ramps already displace, so the schedule still moves exactly the residual;
- beyond it, and only when every sample in the window is beyond it: a seek, at rate 1.0, aimed ahead by this device's measured seek lag (restart time plus landing error, learned from every playing seek — the Tab restarts ~370 ms after a seek and lands 350 ms *early*, so a seek aimed at the group's current position leaves it ~700 ms behind), then a 30 s blackout during which a residual the seek left behind is closed by pulses after all, saturated at 3 % for up to 10 s.

A pulse is written to the tempo file and **confirmed** from the add-on's `.state` line (a later `seq` carrying the rate) before it counts; the time to confirmation is logged as the dead time. If nothing confirms within 3 s the playback is not going through the add-on after all — the file is returned to 1.0 and the item falls back to command-only sync. When the pulse ends, 1.0 is written and confirmed the same way, and the displacement is read off the add-on: Δ = content − output changes only while the rate is off 1.0, so the difference between the two confirmed state lines' head counters (`content_ms − output_ms`) is exactly what the pulse moved. Not their `delta_ms`: that is the Δ the add-on last *reported*, a queue depth behind the head, and read at the end of a pulse it is short by (r − 1) × queue — 15 % on a 1 s queue, measured. Then a quiet window of the queue depth plus one second, because the pulse is not heard until the packets already queued have played.

After a resume, a residual inside the budget is left to fine sync rather than seeked (`_align_after_resume`): a few seconds at a raised rate instead of a visible cut. Every command cancels a running pulse before acting (`PlaybackController._execute`), and every seek returns to 1.0 and waits for it first (`_seek_and_settle` → `before_seek`): a seek under a running rate lands early, because Kodi resyncs to the video's first picture after the flush and that sits further behind the audio at 1.03× (add-on results, item 5). Seeks and resumes also start a quiet window.

**Giving up.** A residual that keeps coming back one-signed is a rate mismatch, not drift. After three consecutive pulses in the same direction, each finding the residual regrown faster than 3000 ppm since the previous one ended, the scheduler stops for the item, logs why, and the manager shows `#30592` once per group. The display clock being on is the usual cause, and `#30552` already says to leave it off.

## 5. Log lines

```
[ syncplay/tempo ] fine sync armed through inputstream.tempo, queue 1.0s
[ syncplay/tempo ] videoplayer.queuetimesize 4.0s -> 1.0s for the session
[ syncplay/tempo ] fine sync armed for <item> (queue 1.0s)
[ syncplay/pulse ] +118ms: 1.024x for 4.9s (applied after 310ms)
[ syncplay/pulse ] moved +116ms (wanted +118ms)
[ syncplay/pulse ] cut by Pause
[ syncplay/align ] +640ms is beyond the pulse budget: seeking
[ syncplay/pulse ] giving up: the residual regrows faster than 3000 ppm across 3 pulses — a rate mismatch, not drift. Is 'Sync playback to display' on?
[ syncplay/tempo ] <item> is not routed through inputstream.tempo; command-only sync for this item
[ syncplay/tempo ] fine sync disarmed
```

`[ syncplay/pulse ] … (applied after …)` is the dead time from the write to the add-on applying it at the demux head; the playing point hears it a queue depth later. `moved` versus `wanted` is the pulse's accuracy as the add-on accounts for it, independent of any position read.

## 6. What to verify live

`tests/live/syncplay_fine_sync.py` drives it end to end on real members. It speaks to the server *as each member's own session* (kofin's Client name, the member's DeviceId and token read off its settings.xml), so it creates the group, joins, sets the queue and pauses without touching a menu, and every command reaches kofin over its own websocket exactly as a real one would. Positions are sampled over JSON-RPC, residuals are injected by writing one member's tempo file for a moment, and the scheduler's own log lines are read back from every box.

1. Routing: a group play logs `play <id> via DirectStream (tempo)` — or `via Transcode (tempo)` since `165d686` (2026-08-27), which put a transcoded stream through the add-on too — and the audio decoder reads `pcm_f32le`; an audio item does not.
2. A pulse closes what it says: inject ~150 ms on one member (1.2× for 0.75 s) and watch its scheduler pull it back inside the deadband, each pulse's `moved` matching `wanted` within a few percent, the other members staying quiet.
3. The seek path: inject ~1 s and see one `[ syncplay/align ]` seek followed by at most one pulse.
4. Commands cut pulses: pause the group during a pulse — `cut by Pause`, and the file reads 1.0.
5. Queue set/restore on Kodi 22: `videoplayer.queuetimesize` to 10 at join, back to the user's value at leave (40 on the desktop and the Tab, 160 on the Bravia), and after a forced kill the next service start logs the restore.
6. Give-up: turn the display clock on for one member with a mismatched panel and see three pulses, the warning, the toast, and no further pulses for the item.
7. No regression when the add-on is absent or disabled: the session does not arm, nothing is stamped, the group behaves as in 0.19.

Results: `tests/live/results/S4.8-fine-sync.md`; the gate is S4.8 in `docs/testing-plan.md`.
