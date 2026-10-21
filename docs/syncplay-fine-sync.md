# SyncPlay fine sync — tempo pulses through Kodi's own tempo

Between group commands, fine sync keeps a member on the group's position by playing briefly faster or slower. On this branch (Kodi 23) the actuator is Kodi's own tempo, from kontell/xbmc #14 and #16, with #37 keeping a tempo beyond the atempo threshold from drawing a clock correction, #18 giving a position read to the millisecond, and #38 playing each pulse as a correction the GUI does not show. The api and sql branches (Kodi 21 and 22) route the same scheduler through the inputstream.tempo add-on instead; inputstream.tempo has no Kodi 23 build. The design of the scheduler is the add-on era's, measured on it (`tests/live/results/S4.8-fine-sync.md`); what changed with the actuator is below.

## 1. Why there is a controller again

`docs/syncplay-drift-shakedown.md` §10 retired the `Player.SetTempo` ladder because its precondition — Kodi's "Sync playback to display" — slaves the media clock to the panel and imposed rate errors up to 4.3 % on the rig, far beyond the ±3 % the ladder could command. The same measurement found that with that setting **off**, all three devices free-run within a few hundred ppm of real time. Stock Kodi plays no tempo in that configuration, which is why the add-on existed.

Kodi 23 with #14 and #16 does: the audio asks the engine for the playing rate (a resample ratio the engine now keeps across its own sync passes) and becomes the clock, as it is at 1.0. The rate is pitch-corrected by atempo beyond `audiooutput.atempothreshold` (2 %) and resampled within it, which moves the pitch with the rate.

So the rule in `CLAUDE.md` stands — no continuous loop against a biased reading — and what runs is not that. Between commands the scheduler issues one **pulse** at a time, confirms it was taken, reads what it moved off the player clock, and waits a settle before looking again.

## 2. Eligibility

Nothing is routed and nothing is stamped on a play. The scheduler arms on the play claim of whatever plays (`syncplay/tempo.py::PulseScheduler._arm`): kofin's own play, a download, a provider's play over the public bus. Whether the item can take a tempo is `Player.TempoEnabled`, asked at each decision rather than once, because it changes during a play (a timeshift buffer has no range for its first moments). An item that cannot take one now is left to the seek path, exactly as an unrouted item was.

Audio items stay command-only: PAPlayer's choreography (never seek it paused) is the commands'.

The actuator is JSON-RPC, since Python's `xbmc.Player` has no tempo call: `Player.SetTempo` with the tempo as a float (Kodi reads it as a double and refuses a JSON integer), and the `tempo` property, which reads what the player has taken. Kodi rounds a tempo to three decimals, so `plan_pulse` plans on that grid and works the pulse's length out from the rate Kodi will play.

Each rate goes as a correction where Kodi takes the flag (#38): the player plays it as any tempo, and the GUI shows no seek bar for its start or its end and normal speed throughout, while the `tempo` property still reads the rate. Kodi refuses a parameter its schema does not name, so `takes_correction` reads `Player.SetTempo`'s schema when an item arms. On a Kodi without the flag the pulses play the same, and each change puts up the seek bar for three seconds, labelled with the rate.

## 3. The session (`syncplay/tempo.py::TempoSession`)

At group join (not at a re-join of the same group) the manager calls `begin()`, which arms when `syncPlayTempo` is on; `end()` at leave. There is no add-on to find, no queue to shorten and no file to publish. A tempo change reaches the clock once the audio already queued at the output has played out at the old rate — VideoPlayer times its clock change by that delay — so `videoplayer.queuetimesize` plays no part, and the shorter-queue setting is gone with the code that restored it.

## 4. The scheduler (`syncplay/tempo.py::PulseScheduler`)

Driven from the controller's existing 250 ms loop, only in the `synced` phase after an Unpause.

Each tick appends `group estimate − player position` to a 3 s window. Once the window is full the median decides:

- inside the deadband (75 ms — one frame is 42 ms, and the Tab's position reads jittered by about that before #18; at 50 ms it pulsed ±50 ms against its own noise): nothing;
- up to the pulse budget (*Largest error closed by tempo*, default 5 s, max 10 s): a pulse `(r, T)` with `(r − 1) × T` equal to the residual. The rate scales with the residual between 0.5 % and the *Fastest correction speed* (default 25 %), aiming at a 5 s pulse. Above 5 % the rate is ramped in and out in 5 % steps every 250 ms, and the hold is shortened by what the ramps already displace, so the schedule still moves exactly the residual;
- beyond it, and only when every sample in the window is beyond it: a seek, at 1.0, aimed ahead by this device's measured seek lag, then a 30 s blackout during which a residual the seek left behind is closed by pulses after all, saturated at the rate ceiling for up to 40 s.

A pulse is asked for with `Player.SetTempo` and **confirmed** when the `tempo` property reads it back (`applied after N ms`: the player thread's dead time on VideoPlayer). If it is not read back within 3 s, 1.0 is asked for and the item falls back to command-only sync; a SetTempo refused while playing does the same. When the pulse ends, 1.0 is asked for and read back the same way. What the pulse moved is read off the player clock once the settle has passed: the change in position less the time that passed, logged against what the pulse was asked to move. Then a settle (`SETTLE_S`, set from the rig's rate-change delay) before the window starts again.

A pulse ends, settle included, at least `END_MARGIN_S` before its item does; one that does not fit is shortened, or left to the next item.

After a resume, a residual inside the budget is left to fine sync rather than seeked (`_align_after_resume`). Every command cuts a running pulse before acting (`PlaybackController._execute`), and every seek first asks for 1.0 (`_seek_and_settle` → `before_seek`) without waiting: Kodi keeps a tempo across a seek, and the speed message and the seek take VideoPlayer's one message queue in order.

**A pulse never outlives a pause.** Kodi refuses a tempo while paused and keeps the one in force across a pause, so a pulse cut on a paused player — by a viewer's pause, or a command arriving on a paused player — leaves its rate behind. It is owed: the next playing tick and every resume (`SyncPlayManager.on_resumed`, in a group or not) ask for 1.0 again. The rate a pulse leaves in force is also recorded in a window property (`state.PROP_SYNCPLAY_PULSE`) for as long as it is in force, and a starting service generation puts 1.0 back from it (`Service._settle_left_tempo`): Kodi's tempo lives as long as its process, and so does the property, so an add-on update mid-pulse cannot leave the item playing fast or slow to its end.

**Kodi or the viewer moving the tempo.** The scheduler reads the tempo every tick. One it did not ask for — the OSD's play speed, which Kodi 23 offers with the display clock off — is put back to 1.0 once it has held two seconds (`FOREIGN_GRACE_S`), logged once per item, and nothing is measured while it holds; a shorter step, such as the live gate's injection, is left for the pulses that follow to close. With fine sync off kofin never touches the tempo. Kodi putting a pulse's rate back to 1.0 itself ends the pulse; on a live stream playing faster than its source that is the live edge (#25: half a second with nothing to read), and two forward pulses ended that way in a row end fine sync for the item (`giving up: at the live edge`, no toast — the edge is not a fault). A read that lags the last ask by less than a second is the player thread catching up, not someone else's change.

**Giving up.** A residual that keeps coming back one-signed is a rate mismatch, not drift. After three consecutive pulses in the same direction, each finding the residual regrown faster than 3000 ppm since the previous one ended, the scheduler stops for the item, logs why, and the manager shows `#30592` once per group. The display clock being on is the usual cause, and `#30552` already says to leave it off.

## 5. Log lines

```
[ syncplay/tempo ] fine sync armed (Kodi's tempo)
[ syncplay/tempo ] fine sync armed for <item> (video: budget 5000ms, up to 25.0%)
[ syncplay/pulse ] +118ms: 1.024x for 4.9s (applied after 12ms)
[ syncplay/pulse ] moved +116ms (wanted +118ms)
[ syncplay/pulse ] cut by Pause
[ syncplay/pulse ] 1.024x left in force after a pause; back to 1.0x
[ syncplay/tempo ] 1.500x set outside fine sync; back to 1.0x
[ syncplay/pulse ] +40ms left: the item cannot take a tempo now
[ syncplay/align ] +6400ms is beyond the pulse budget: seeking
[ syncplay/pulse ] giving up: the residual regrows faster than 3000 ppm across 3 pulses — a rate mismatch, not drift. Is 'Sync playback to display' on?
[ syncplay/tempo ] fine sync disarmed
```

`applied after` is the time from the call to the player reporting the rate; the clock follows once the audio queued at the output has played out. `moved` versus `wanted` is the pulse's accuracy read off the player clock. `[ syncplay/window ]` at debug gives each decision's median and the window's spread, which is this member's read noise between pulses.

## 6. What to verify live

`tests/live/tempo_gate.py` qualifies the actuator on a device: slopes, read noise, the refusal while paused, the read-back and rate-change delays, clock corrections across a ramped pulse. `tests/live/syncplay_fine_sync.py` drives the scheduler end to end on real members, injecting a residual through `Player.SetTempo` on one member's JSON-RPC. The gate is S4.8 for Kodi 23 in `docs/testing-plan.md`.
