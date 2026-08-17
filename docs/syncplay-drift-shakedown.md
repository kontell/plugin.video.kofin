# SyncPlay drift shakedown — three Piers devices

> **Outcome: the feature was removed.** Gate 0 (§10) measured the actuator against the error it was aimed at and the actuator lost — by up to 14x, unfixably, on all three devices. `syncPlayDriftCorrection`, `syncPlayTolerance` and the whole `Player.SetTempo` ladder are gone; `#30552` now warns about the sync-to-display setting instead. §1–§9 are the plan as written before the measurement, kept because the reasoning is what makes §10 legible — and because the sampler and the tempo gate it specifies are still the tools for verifying a group (S4.7). Do not read §§5–9 as work still queued: only the clock-off arm and S4.7 survive the outcome.

Goal (as set): decide how effective `syncPlayDriftCorrection` actually is, how it fails, and what number `syncPlayTolerance` should default to on a LAN. Secondary goal: close the two open phase-4 gates in `docs/testing-plan.md` — **S4.3** (the smooth `SetTempo` path, unprovable on Omega) and **S4.7** (a real multi-device group).

Target server: Jellyfin 10.11.11 `minipie` at `192.168.1.167:8096`, and the same server through caddy at `https://jelly.konell.xyz`.

## 1. The mechanism, with its actual numbers

Drift correction is a **rate-only** controller. It never seeks (`playback.py::_correct_drift`, `utils.py::correction_action`):

* Sampled once per second from the SyncPlay loop; `diff = group_estimate − player.getTime()`, positive = we are behind.
* `group_estimate` comes from the last command's `(PositionTicks, When)` extrapolated on the server clock, re-anchored by a v2 `PositionBeacon` every **5 s** (server: `PositionBeaconInterval`).
* Actuator is `Player.SetTempo`, gated on Kodi's **`videoplayer.usedisplayasclock`** ("Sync playback to display"), which is off by default and off on all three boxes today. No tempo → drift is tolerated, silently. PAPlayer (music) never gets tempo at all.
* Schmitt trigger: engage past `syncPlayTolerance` (default **75 ms**, slider 50–1000), release once inside `min(80, tolerance/2)`.
* Rate = `1 + clamp(diff/8000, ±0.03)` **rounded to 0.01**, so the actuator is quantised to ±1/2/3 %, changed at most every 2.5 s.
* Drift ≥ **1500 ms** is out of scope: tolerate and wait for the server. Transcoding widens everything (engage ≥ 1500 ms, ceiling 3000 ms).
* A correction still engaged after **10 s** is abandoned, then a **30 s** blackout (`CORRECTION_MAX_ENGAGED_S`, `DRIFT_BLACKOUT_AFTER_GIVEUP`). Buffering does the same thing immediately.

Server side (`jellyfin-plugin-syncplayv2`, 10.11.0.2, Active on the target): a member's position is compared against the group **only in `WaitingGroupState`, on a Ready report**, against `clamp(2 × ping, 500, 2000)` ms. `ResyncSession` is a reconnect mechanism, not a drift corrector.

**So: while a group is Playing, nothing on the server corrects position.** Continuous drift is entirely kofin's tempo loop, and the code comment "the server re-syncs" only comes true when something forces a Ready — a buffer stall, a pause, a seek, a queue change.

## 2. What the code predicts before we touch a device

Two constants decide the whole steady state.

**Budget per engagement: 10 s × 3 % ≈ 300 ms.** Any drift larger than roughly `disengage + 300 ms` cannot be closed inside one engagement, so it hits the give-up and the 30 s blackout, converging in ~300 ms steps at ~40 s per step (or never, if it is being re-created). That makes ~300 ms the controller's practical reach — while the tolerance slider offers **1000 ms**, i.e. a range the actuator cannot serve. Test R-D and the sweep in §6 are aimed straight at this.

**Steady state is a sawtooth**, its period set by the device's own media-clock rate error `r` (ppm), not by anything SyncPlay does. Error grows at `r/1000` ms per second, closes at 10/20/30 ms/s depending on the band it is in:

| tolerance E | release D | close E→D | regrow at 100 ppm | regrow at 1000 ppm | duty @100 ppm | duty @1000 ppm |
|---|---|---|---|---|---|---|
| 50 | 25 | ~2.8 s | 250 s | 25 s | ~1 % | ~10 % |
| 75 (default) | 37.5 | ~3.8 s | 375 s | 37 s | ~1 % | ~9 % |
| 150 | 75 | ~6 s | 750 s | 75 s | ~0.8 % | ~7 % |
| 250 | 80 | ~10 s | 1700 s | 170 s | ~0.6 % | ~5 % |
| 350+ | 80 | > 10 s | — | — | give-up cycle | give-up cycle |

Where 100 ppm ≈ crystal tolerance between two boxes (0.36 s/hour) and 1000 ppm ≈ a 59.94/60 Hz mismatch on a device that cannot show 24p (3.6 s/hour).

That was the prediction. **Gate 0 measured it and the table's optimism did not survive** — the real rate errors on this rig are 5× to 43× the 1000 ppm column, and the worst is outside the controller's authority altogether. §10 has the numbers; read this table only as the arithmetic of the controller, not as a forecast for these devices.

**Pairwise spread is up to 2×E.** Tolerance is measured per member against the group timeline; two members can sit at opposite edges. A "max 400 ms between any two devices" target therefore means tolerance ≤ ~180 ms, not 400.

## 3. Ground truth measured today (2026-08-17, from `192.168.1.112`)

Pre-flight facts already established, so the test day does not spend time on them:

The three members are ADB-reachable Android boxes running the **same** Piers build — 22.0-BETA1, Git `20260621-77395cf42e` — on three different display stacks. Same build, three platforms is a better rig than three of anything identical: it separates "kofin's controller" from "this box's clock".

| Role | Device | RPC | ADB | Display modes | Screen-off | Notes |
|---|---|---|---|---|---|---|
| **A** | Bravia 4K AE2, Android 14 | 192.168.1.198:8080 | :38759 (rotates) | 48 / 50 / 60 Hz, idle at 48.000004 | 600 s | switches modes (`adjustrefreshrate=2`), so the one member that may sit near 0 ppm |
| **B** | Pixel 7 Pro, Android 17 | 192.168.1.218:8080 | :45575 (rotates) | 60 / 120 Hz only | 300 s | no 24p mode → the natural ~1000 ppm member; a phone, so thermal throttling over an hour is a real confound |
| **C** | Galaxy Tab S5e (SM-T720), Android 13 | 192.168.1.150:8080 | :44857 **+ USB** | 60 Hz fixed | 600 s | also ~1000 ppm; the USB link is what makes it the only box that can safely lose Wi-Fi |
| (D) | LibreELEC on a Pi (vc4-hdmi) | 192.168.1.217:8080 | — (SSH root) | — | — | a *different* Piers build (`62a5ed5`); optional fourth member, and the only box that can firewall itself |
| ctrl | desktop | 127.0.0.1:8080 | — | — | — | **21.3 Omega** — not a member; the positive control for R-L |

All five read `usedisplayasclock=false`, `adjustrefreshrate=2` (desktop 0), and `audiooutput.passthrough=false`. Passthrough being off matters: tempo needs the audio resampled, so a passthrough household is a case the feature may not serve at all — worth stating in the verdict rather than discovering later.

Every member resolves `jelly.konell.xyz` to **192.168.1.167** (the LAN's Pi-hole at `.211` answers, and Private DNS is off on both the Android 13 and 17 boxes, so nothing bypasses it). The "proxy leg" is therefore a same-host caddy hairpin, not a WAN trip — R-G measures TLS and caddy overhead, and must not be written up as internet latency.

ADB ports rotate daily on all three. Only the log-pull and device-shell steps care: the sampler addresses devices by RPC port 8080, which is stable.

Clock sync achievable on this LAN, 12 samples each, same NTP exchange kofin uses:

| Leg | RTT min / med / max | offset spread | offset stdev |
|---|---|---|---|
| WS `/SyncPlay/TimeSync` direct | 2.0 / 3.0 / 4.4 ms | 2.5 ms | 0.6 ms |
| HTTP `/GetUtcTime` direct | 6.9 / 8.3 / 10.5 ms | 2.9 ms | 0.8 ms |
| WS `/SyncPlay/TimeSync` via caddy | 2.0 / 2.5 / 3.8 ms | 1.6 ms | 0.5 ms |
| HTTP `/GetUtcTime` via caddy | 20.0 / 23.2 / **108.1** ms | 45.2 ms | 12.5 ms |

Reads out of that:

* Both endpoints negotiate `ProtocolVersion 2` and advertise `TimeSync.WebSocketPath = /SyncPlay/TimeSync`; **caddy proxies that socket correctly**, and the proxied socket is as fast as direct. The proxy leg is only dangerous if the socket is *not* used.
* HTTP fallback through caddy is 3–10× worse and 45 ms wide. Min-RTT windowing still salvages it, but this is the one configuration where clock error is a material fraction of a 75 ms tolerance.
* The server stamps `RequestReceptionTime == ResponseTransmissionTime` identically, so server processing is folded into RTT on the HTTP leg — it inflates RTT rather than biasing the offset.
* The two transports disagree systematically by ~2–8 ms. A group where one member falls back to HTTP acquires a small permanent bias (scenario R-H).
* **Clock sync is not the limiting factor on this LAN** (±3 ms). Media-clock rate error and output-pipeline latency are.

## 4. Pre-flight

Per device, all of it RPC-settable or ADB-settable, none of it needing the GUI:

1. `videoplayer.usedisplayasclock = true` — **without this the feature under test does nothing**, and it was off on all three. Note the contradiction this exposes in our own strings: `#30552` advises turning Sync-playback-to-display *off*, `#30554` says drift correction needs it *on*. Gate 0 turned that contradiction into a measurement, and §10 resolves it.
2. **`videoscreen.whitelist` must not be empty** — it is empty on all four boxes, which is the Kodi default, and an empty whitelist means **no mode switching happens at all** however `adjustrefreshrate` is set. Gate 0 measured the consequence: the Bravia played 23.976 content on its 50 Hz desktop mode and its media clock ran **4.28 % fast**, which is outside the ±3 % the drift controller can even reach. Populate the whitelist with the modes the panel offers, then confirm `System.ScreenMode` matches `Player.Process(videofps)` *during* playback — not from the idle mode list, which says nothing about what Kodi will choose.
3. kofin: `syncPlayEnabled=true`, `syncPlayDriftCorrection=true`, `syncPlayTolerance` per run.
4. Debug logging on without the on-screen overlay: `advancedsettings.xml` pushed to `…/org.xbmc.kodi/files/.kodi/userdata/` and Kodi restarted (the Tab has no such file today — check the other two). Overlay **off** before any screenshot (`kodi-debug-overlay-hides-subtitles`), and per `kodi-adb` a push succeeds where a delete under `Android/data` fails.
5. **Screen-off timeouts are shorter than a soak** — 600 s, 300 s, 600 s against a 60-minute run. `settings put system screen_off_timeout 3600000` plus `svc power stayon true` while charging; record the originals and restore afterwards. Kodi screensaver `None` as well. A device that blanks mid-run reads as a hung Kodi to any poller (`kodi-test-rig`), and on the Pixel it will also stop rendering entirely.
6. Keep the Pixel on charge and note that an hour of 1080p will heat it: sample `dumpsys thermalservice` (or battery temperature) alongside each run, because thermal throttling is the most likely explanation for a divergence spike on that box — and it is a real-world one, not an artefact.
7. Same Jellyfin user on every member, fixed for the whole exercise; user-level playback settings change the transcode decision.

Host-side prerequisites:

* `~/.config/kodi-drive/targets.env` line 7 is `KODI_LIBREELEC_SSH_CREDS: root:libreelec` — a colon, not `=`, so **every kodi-drive tool that sources the file errors out** (`kodi-remote` fails before it runs). Also `JELLYFIN_URL` is defined twice; the proxy URL wins. Fix both, and add entries for the Pixel and the Tab.
* SSH with sudo on the Jellyfin host `192.168.1.167` if the surgical stall injector in R-A is wanted — see the injector matrix there.
* Test asset (below) in a Jellyfin library, direct-playable on all three.

**Test asset.** Generate a 1-hour timecode film rather than using a real movie — it makes every channel below cheap to read:

```sh
ffmpeg -f lavfi -i "testsrc2=size=1920x1080:rate=24000/1001" \
       -f lavfi -i "sine=frequency=1000:beep_factor=4:sample_rate=48000" \
       -vf "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf:\
text='%{pts\\:hms}  f=%{n}':fontsize=72:fontcolor=white:box=1:boxcolor=black@0.7:x=40:y=40" \
       -c:v libx264 -profile:v high -level 4.0 -pix_fmt yuv420p -preset veryfast -crf 20 -g 48 \
       -c:a aac -b:a 128k -ac 2 -t 3600 -shortest \
       "SyncPlay Timecode (2026)/SyncPlay Timecode (2026).mp4"
```

h264 High/yuv420p + stereo AAC in mp4 is direct play on all three boxes, so the main runs are not confounded by transcoding; the burned-in frame counter gives frame-exact photo comparison, and the 1 Hz beep gives sub-millisecond audio cross-correlation. Keep one HEVC/TrueHD title around for R-F.

**23.976 fps, not 24, is deliberate**: it is what real films are, and it is the rate that has no matching mode on a 60 Hz-only panel. Encoding at a flat 24 would hand the Pixel and the Tab a 24→60 relationship that is *also* a mismatch but a different one, and would make the measured ppm figures unrepresentative of what a user's library does.

## 5. Instrumentation — three channels, in trust order

### 5.1 The controller's own view (log — **in place**)

The quantity the controller acts on had no log line at all, which would have made the whole exercise inferential. Added (logging only; no semantics touched inside the transplant):

* `LOG.debug` once per drift tick in `_correct_drift`: signed `diff_ms`, the applied tempo, the **age of the drift reference**, and `no-tempo` / `transcode` markers — a stale reference is how a beacon outage would present, and it is not the same thing as drift.
* `LOG.info` on engage and on release only — `engaging 1.03x at +400ms` / `released at +60ms after 4.2s`. Duty cycle and engagement count are then readable without debug logging, which is worth keeping permanently for field support.
* `LOG.debug` in `_on_beacon`: the beacon's position and how long ago the server stamped it.

Verified end to end through one engage → step-down → release cycle (`playback.py`, tolerance 150):

```
[ syncplay/drift ] +400ms tempo 1.00 ref 0.0s
[ syncplay/drift ] engaging 1.03x at +400ms
[ syncplay/tempo ] 1.03
[ syncplay/drift ] +200ms tempo 1.03 ref 0.0s
[ syncplay/drift ] +120ms tempo 1.03 ref 0.0s
[ syncplay/tempo ] 1.02
[ syncplay/drift ] released at +60ms after 0.0s
```

Everything else needed was already logged: `[ syncplay/tempo ]`, `[ syncplay/drift ] … won't converge, backing off`, `Tempo restore skipped …`, `Time sync: offset … rtt …`, `[ syncplay/<Command> ] at … (+Nms)`, `[ syncplay/align ]`, `[ syncplay/buffering ]`, `[ syncplay/recovered ]`.

### 5.2 External truth (JSON-RPC sampler — **in place**)

`tests/live/syncplay_drift.py`, host-side, no add-on involvement:

```sh
# one run per scenario; refuses to start unless every device is already playing
tests/live/syncplay_drift.py sample --device bravia=192.168.1.198:8080 \
    --device pixel=192.168.1.218:8080 --device tab=192.168.1.150:8080 \
    --seconds 3600 --hz 4 --tolerance 150 --label p2-tol150
tests/live/syncplay_drift.py aggregate tests/live/results/drift/<run-id>
```

* One **batched** round trip per device per sample: `Player.GetProperties` (`time`, `totaltime`, `speed`, `cachepercentage`) + `XBMC.GetInfoBooleans` (`Player.IsTempo`, `Player.Caching`) + `XBMC.GetInfoLabels` (`Player.PlaySpeed`). JSON-RPC `speed` is an int and will **not** show a 1.01 tempo, so the infolabels are the only external view of the actuator. Batch requests and HTTP keep-alive are confirmed against all three members and the desktop; a Kodi that answered non-batch would fall back automatically.
* Sockets are kept alive per device. That is measurement, not tidiness: a fresh TCP handshake per sample would land a whole round trip in the uncertainty column.
* Row is `host_ms` (round-trip midpoint), `unc_ms` (half the round trip), `pos_ms`, plus the state columns and an `err` token; samples wider than `--max-unc` (default 15 ms) are dropped at aggregation rather than trusted. CSVs land under `tests/live/results/drift/<run-id>/` (gitignored) beside a `meta.json` recording each device's Kodi version, playing item, `usedisplayasclock` and `adjustrefreshrate`.
* Aggregation interpolates onto a common 250 ms grid — legitimate because playback is locally linear, and refused across gaps wider than `--max-gap` (default 2 s) so a buffer stall cannot be interpolated into existence. Output is `summary.json` + `summary.md`: per pair p50/p95/max |divergence| plus first/last/growth, and per device the fitted ppm rate error, fit RMS, tempo duty cycle, engagements/hour and caching episodes.
* It prints live pairwise divergence every 10 s while sampling, so the run is visible from whichever room you are standing in.
* Validated twice before the test day: the reduction against synthetic runs with known answers (500 ppm injected → 500.0 reported; a 30 s gap excluded rather than interpolated; tempo duty and engagement counts exact), and the whole sampling loop against two fake Kodis (800 ppm injected → 802.9 reported, start offset and growth both correct).

Per `kodi-jsonrpc`: a single `time` reading agrees with whatever you hoped, so the sampler's own two-samples-differ property is what proves a player is running rather than wedged.

**What the channel can resolve on these three boxes** — measured idle, 10 samples per method, min/med/max ms:

| Call | Bravia | Pixel | Tab |
|---|---|---|---|
| `JSONRPC.Ping` | 2.5 / 3.4 / 113.1 | 30.7 / 102.2 / 160.4 | 4.6 / 7.0 / 105.0 |
| `Application.GetProperties` | 2.5 / 2.9 / 4.4 | 6.2 / 102.3 / 182.8 | 5.4 / 7.6 / 60.9 |
| `Player.GetActivePlayers` | 2.8 / 3.2 / 7.5 | 7.5 / 101.8 / 181.1 | 7.1 / 15.5 / 47.2 |
| `XBMC.GetInfoBooleans` | 11.5 / 19.7 / 21.1 | 32.2 / 77.9 / 132.7 | 12.1 / 23.4 / 75.8 |
| `XBMC.GetInfoLabels` | 19.3 / 19.8 / 20.2 | 76.2 / 104.0 / 136.9 | 7.1 / 24.5 / 100.1 |

Two independent effects, and both shaped the tool:

* **Infolabels cost an app-thread round trip.** On the Bravia a property read answers in 2.5–3 ms while an infolabel takes ~20 ms, because infolabels are evaluated on the GUI thread. Batching them with the position would have put every position timestamp behind that dispatch — which is why the sampler now sends the position alone at full rate and refreshes the flags at ~1 Hz with their own timestamp. It is a 6× precision difference on the fast box and more on the others.
* **The Pixel's radio quantises to ~100 ms.** Its median is ~100 ms on *every* method including Ping, with a min of 8 ms — the signature of Wi-Fi power save at a beacon interval, not of Kodi. Waking the screen and `svc power stayon true` improves the Tab (median 19 → 7 ms) but barely moves the Pixel (105 → 98 ms), and `cmd wifi force-hi-perf-mode` is root-only on Android 17, so there is no shell-level fix.

The expectation to test on the day is that **continuous playback keeps the radio out of power save** — a streaming video is exactly the traffic pattern that prevents it — so re-measure per member once the asset is playing. If the Pixel's median stays near 100 ms, its ±50 ms uncertainty cannot resolve a 75 ms tolerance and that member's drift must be read from channel 5.1, with RPC kept only for corroboration. This is why `unc_ms` is per-sample rather than a run-level average: a bad window shows up instead of quietly widening every number.

### 5.3 Calibration, before any group exists (this is the highest-value hour of the day)

* **Measurement noise floor, per member**: free-running playback, 10 Hz for 60 s. Fit `pos` against the host clock; the residual RMS is channel 5.2's resolution limit on that box, and the `unc_ms` distribution says whether playback lifted the radio out of power save (§5.2). A member whose residual is not comfortably under 25 ms is measured by channel 5.1, not 5.2 — decide that per member here, before any group run, rather than discovering it in the P2 numbers.
* **Per-device rate error, in ppm**: free-running playback of the timecode asset for 5 min per device, `usedisplayasclock` **on**, then **off**. This single number predicts the whole sawtooth from the §2 table before any group runs — and it separates "the feature is ineffective" from "this device diverges at 1000 ppm and the feature is doing exactly what it can". Record `System.ScreenMode` during that playback too: the mode Kodi actually selected is what explains the ppm, and the idle mode list does not (the Bravia idles at 48.000004 Hz and may or may not choose a 23.976-family mode when the film starts).
* **Which timesync transport each device actually got**: `SyncPlay time-sync socket at /SyncPlay/TimeSync` in each log, plus its `Time sync: offset … rtt …` distribution. Do not assume the desktop's numbers (§3) hold for a Wi-Fi Android TV.

### 5.4 Perceptual ground truth (once, not per run)

Media-clock equality is not screen equality: output pipeline latency (panel, audio device, game mode) differs per device and kofin cannot know it. This rig makes the measurement easy — two of the three members are a phone and a tablet, so stand them side by side and shoot one photo: the burned-in frame counters give a frame-exact offset, and a recording of both beeps cross-correlates to about a millisecond. Do the same against the Bravia once, since a TV panel is where the pipeline latency is largest. The **difference** between that and channel 5.2 at the same instant is that pair's constant pipeline offset — the number that decides whether same-room viewing needs a manual per-device trim (a feature question, out of scope here, but this is where the evidence for it comes from).

## 6. Scenarios

Baseline and sweep establish the numbers; R-* attack the feature. Every scenario records: the three CSVs, the three add-on logs (`adb pull …/.kodi/temp/kodi.log`), the group's server-side log, and a one-line verdict. `syncPlayNotifications` on, so toasts are visible.

### Gate 0 — does tempo do anything on Android? (run this first, before everything)

All three members are Android, and Android is the one platform where the actuator is unproven. `CanTempo()` returns the `usedisplayasclock` setting on every platform, but the Android video path is MediaCodec plus AudioTrack, and kodi-drive records only that webOS overrides `CanTempo` — Android was never characterised. A `SetTempo` that reports success and changes nothing would leave the whole matrix below measuring a controller with no output.

Per device, ~2 minutes: play the asset, sample at 10 Hz for 30 s, `Player.SetTempo 1.03`, sample 30 s more, restore. Fit the slope in each window — the second must be ~3 % (≈30000 ppm) steeper. Also confirm `Player.IsTempo` and `Player.PlaySpeed` follow, and listen for a pitch artefact or a dropout as AudioTrack resamples.

If a member's slope does not move, drift correction cannot work on that platform, and **that is the headline finding of the shakedown** — record it, then run the rest with whichever members do respond (and P1 remains worth running on all three, since toleration is what those members are left with).

### P1 — Uncorrected baseline (the control)

`syncPlayDriftCorrection=off` on all three, direct play, one full hour of the timecode asset, group start from the plugin on device A. Measures the natural divergence the feature has to fight, and the start-alignment floor (`UNPAUSE_ALIGN_MS=100`) at t=0. Without this run, no "effectiveness" claim afterwards means anything.

### P2 — Tolerance sweep

Correction on, 25 min per setting, **50 / 75 / 150 / 250 / 1000**, same asset and start procedure each time. Report per setting: p50/p95/max pairwise divergence, tempo duty cycle, engages/hour, give-ups/hour, and a subjective note per device (any audible pitch/rate artefact, any visible OSD speed indicator — `kodi-playback-tempo`: the tempo OSD is skin-driven and cannot be suppressed by an add-on, so a tight tolerance buys a flickering indicator). Prediction to falsify: 50–150 behaves as the §2 table says; 1000 never converges and shows a give-up cycle instead.

### R-A — Buffer stall and recovery (the realistic failure)

Three durations: **2 s** (under `BUFFERING_DEBOUNCE=2.5`, so no report is sent and tempo alone must close it — precisely the feature's job), **15 s** (report → group Waiting → toast → Ready → server position check at `clamp(2×ping,500,2000)`), **40 s**. Each with correction on and off. Record time-to-back-inside-tolerance, and whether the 2 s case ever converges.

Choosing the injector matters more here than anywhere else, because all three members reach the LAN over the **same Wi-Fi that carries their ADB link** — cutting a member's radio also cuts the control channel:

| Injector | Works on | Cost |
|---|---|---|
| `svc wifi disable` / `enable` over ADB | **Tab only** | a genuine radio outage, and safe *only* there because its USB ADB link survives. On the Bravia and the Pixel this strands the box until someone re-enables Wi-Fi by hand. |
| `tc` egress rate limit on the Jellyfin host, filtered by client IP | any member | the surgical one: starves the video buffer while the websocket's small frames still get through, which is what isolates R-A from R-J. Needs SSH + sudo on `192.168.1.167`. |
| `iptables` locally | the Pi (D) | port-scope it — this network shares NFS with the host (`websocket-client-ping-timeout-poison`). |
| kill and restart a per-device `benchproxy.py` | any member | drops every in-flight connection at once, media and websocket together. Blunter than `tc`, needs nothing but the test host. |

Note what `benchproxy.py` cannot do: its `down`/`slow`/`error` modes act when a request *arrives*, so they cannot starve an already-streaming response — direct play is one long GET. Killing the process is the lever, unless a bandwidth-throttle mode is added to its relay loop (~15 lines, but that file is untracked in-flight benchmark work, so ask before touching it).

### R-B — Precise drift step (device-agnostic injector)

`Player.SetTempo 1.20` on one member for 5 s, then back to 1.0 → a clean ~1 s lead. kofin has **no** speed-change callback, so this never leaks to the group as a user action (unlike `Player.Seek`, which forwards). Note kofin will overwrite the injected tempo when it engages, and will not restore an injection it did not make — the injector must clear it. Step sizes: 100 ms, 300 ms, 600 ms, 1200 ms, 2500 ms. This is the transfer-function measurement: recovery time vs step size, and where the ladder changes behaviour.

### R-C — Rate divergence (steady-state, not a step)

**This rig supplies the scenario for free**, which is the best thing about it: the Pixel and the Tab have no 23.976 or 48 Hz mode, so with `usedisplayasclock` on they should each carry a persistent ~1000 ppm error while the Bravia switches to 48 Hz and sits near zero. So R-C is not an injection but a reading of P2: confirm the sawtooth period and duty cycle against §2 per member, confirm the walkers are the two 60 Hz boxes, and confirm each stays inside tolerance rather than walking away.

The inverse is the injection worth doing: set `adjustrefreshrate=0` **on the Bravia** so it stops switching to 48 Hz, and check it joins the 1000 ppm column. That is the same physics with the sign of the experiment reversed, and it rules out "the Bravia is fine for some other reason".

### R-D — The 300 ms budget and the give-up ladder

From R-B's 600 ms and 1200 ms steps, verify the predicted `10 s engage → back off → 30 s blackout → re-engage` cycle in the log, and time full recovery. If a 1 s step really takes ~2 minutes, that is a tuning finding, not a bug report: the give-up timer should scale with the engage band (or the band should be capped at what 3 % × 10 s can reach).

### R-E — The dead band above 1500 ms

Inject a 2 s step (R-B) and leave it. Expect: client tolerates, and — per §1 — **nothing corrects it** while the group keeps playing. Then establish what does clear it: a group pause/unpause, a seek, a buffering cycle, a queue advance. Time each. This is the scenario most likely to produce a real defect: a member permanently 2 s out with no path back short of user action.

### R-F — A transcoding member

Force one member to transcode (a user bitrate cap, or the HEVC/TrueHD title). Its engage band becomes 1500 ms and its ceiling 3000 ms. Measure its steady offset against the two direct-play members, and check whether Jellyfin's segment length makes reported position quantised — `TRANSCODE_QUANTUM_MS=3000` assumes ~3 s segments and Jellyfin's default is 6 s (`jellyfin-fmp4-seek-segment-bug`). Verdict wanted: is a mixed direct/transcode group usable, or does the transcoding member need to be declared unsynchronisable?

### R-G — Everything through caddy

Repeat P2 at the winning tolerance with all three members on `https://jelly.konell.xyz`, 25 min. Confirm each device gets the WS timesync socket (§3 shows caddy proxies it); if any falls back to HTTP, that member's clock error grows to ±10 ms and 45 ms wide. Also watch the ping-scaled server tolerance (`2 × ping`) and the hot-join lead (`max(2 × ping, 500 ms)`) — both are ping-driven and kofin only reports ping every ~25 s, starting from a 500 ms default.

### R-H — Mixed endpoints (the realistic household)

Two members direct, one through the proxy. Tests whether the ~2–8 ms systematic difference between the WS and HTTP offset estimates shows up as a permanent per-device bias, and whether the ping spread changes command scheduling.

### R-I — Command choreography and hot join

Per-device user pause/unpause/seek in turn; a hot join into a playing group (the `_prealign_unpause` path, `UNPAUSE_ALIGN_MS=100`); a queue advance to the next item. Assert: exactly one authoritative command per user action (no echo storm), joiner arrives inside tolerance, and post-event convergence within 10 s. Screenshot the OSD only where it proves rendering (`kodi-screenshot-review`).

### R-J — Socket loss and wake

Block the websocket only (port-scoped) for 30 s — inside the server's 90 s `DisconnectedGracePeriod` — then restore: expect reconnect → `StateSnapshot` → convergence, and measure post-reconnect drift. Then a sleep/wake or screensaver-deactivate cycle on one member: `force_update(reset=True)` re-measures the clock, and the unconditional wake FastSync fires. Measure how long the offset takes to re-converge and what drift shows meanwhile.

### R-K — Music (the no-actuator case)

A 20 min album across all three. Tempo never applies to PAPlayer, so this quantifies what pure toleration costs — the honest control for judging whether tempo is worth having on video. Assert no tempo attempt appears in any log, and that no paused-PAPlayer seek happens (`SEEK_REPAUSE_WINDOW_MS`, the audio holds in `_do_seek`/`prepare_ready`).

### R-L — Piers regression checks

Across the whole day's logs: **zero** `Tempo restore skipped …` warnings on any Piers box (the Omega bug is reported fixed in 22 and `kodi-playback-tempo` flags it as unverified — this is the verification), and `SyncPlay tempo control available: True` on every video start. Positive control: run the same 5 min on the Omega desktop and confirm the warning **does** fire there and tempo self-disables. That closes S4.3 from both sides.

## 7. Pass thresholds and what "realistic tolerance" will mean

Perceptual anchors, so the numbers mean something: two screens in one field of view diverge visibly at about one frame (~40 ms) on a cut; audio from two devices in one room combs at ~20–30 ms and slaps by ~50 ms; separate rooms in one house tolerate ~250 ms; a shared reaction (both laughing at the same joke) tolerates ~500 ms.

Proposed gate for the feature, direct play, three Piers devices, `usedisplayasclock` on:

* **p95 pairwise divergence ≤ 150 ms and max ≤ 400 ms** over a 60 min soak — the same-house, different-room use case.
* Tempo duty cycle **≤ 15 %** with no audible artefact reported at any device.
* **Zero** unexplained give-up cycles in a steady-state run, and zero `Tempo restore skipped` on Piers.
* Every induced step ≤ 1200 ms recovers to inside tolerance within **60 s**.
* Same-room, frame-accurate viewing is explicitly **not** gated: §5.4 will show the pipeline offset is a per-device constant the protocol does not model.

The recommended default tolerance falls out of P2 plus the 2×E pairing rule: to hold max pairwise under 400 ms the setting must be ≤ ~180 ms, and the §2 table says duty cycle barely differs between 75 and 150. The likely recommendation is therefore **150 ms**, with 75 kept only if the sweep shows no OSD flicker and no give-ups. State it from the measured table, not from this paragraph.

## 8. Deliverables

* `tests/live/results/S4.3.md` and `S4.7.md` rewritten from PARTIAL to a verdict, and `docs/testing-plan.md` updated in place.
* A results table in this file: per scenario, the numbers and the verdict.
* Findings that are Kodi-general, not kofin-specific — **whether `Player.SetTempo` does anything on Android at all** (Gate 0; `kodi-playback-tempo` has an open question here and no Android answer), the ppm rate error `usedisplayasclock` imposes when the panel has no matching mode, whether the tempo→1.0 restore skip is really fixed in 22 — go to kodi-drive via `kodi-drive:contribute`, not into `CLAUDE.md`.
* Expected code follow-ups (each needs its own issue, not a drive-by): the give-up timer vs engage band mismatch (R-D); the `#30552`/`#30554` contradiction about Sync-playback-to-display; a warning (or a disabled toggle) when `syncPlayDriftCorrection` is on while `usedisplayasclock` is off, which is the state **all three boxes are in right now**; `TRANSCODE_QUANTUM_MS` vs Jellyfin's real segment length (R-F); and whether >1500 ms drift needs a client-side recovery path at all (R-E).

## 9. Sequencing and cost

Runtime is dominated by soaks, so run them in the background and use the gaps for the injected scenarios.

* **Prep (no devices needed):** §5.1 log lines and the §5.2 sampler are **done and self-tested**; what remains is the timecode asset, the `targets.env` fixes, and entries for the Pixel and the Tab. The log lines are a behaviour change to a shipped add-on, so they want a changelog line and a PR of their own rather than riding into a results commit.
* **Day 1:** per-device pre-flight, including the screen-off timeouts and an `advancedsettings.xml` push per box (40 min) → **Gate 0** on all three (10 min, and it can end the day early in a useful way) → §5.3 calibration (1 h) → P1 baseline (1 h) → P2 sweep (2 h) → §5.4 photo/audio pair (20 min).
* **Day 2:** R-A, R-B, R-D, R-E (2 h, injected, quick) → R-C as a re-read of P2 plus the Bravia inversion (30 min) → R-F (30 min) → R-G, R-H (1 h) → R-I, R-J (45 min) → R-K, R-L (45 min).

Re-run `adb devices` at the start of each session: the ports on all three rotate. Nothing else depends on them — the sampler works off RPC 8080 — but the log pulls and device shells do.

Confounds to keep out of the data: a device blanking or sleeping mid-soak (§4.4 — this rig's defaults are shorter than a soak on every box); the Pixel throttling thermally after an hour of 1080p; a refresh-rate switch mid-run (record the mode, do not let `adjustrefreshrate` change between runs of the same arm); Wi-Fi contention, since all three members share one radio path — record RTT alongside position so a divergence spike can be attributed rather than guessed; a library sync running on a member (pause syncs during soaks); and any add-on update, which bounces the service and starts a new SyncPlay generation.

## 10. Results — Gate 0 (run 2026-08-17)

Tooling: `tests/live/tempo_gate.py`, driving Kodi directly (no kofin in the path), against a locally served 1080p h264 clip generated with the §4 recipe. 60 s windows at 10 Hz after a 10 s settle, ~570 samples per window; the fitted rate carries a standard error of ~140–230 ppm, so anything above ~500 ppm is real.

**The gate passes: `Player.SetTempo` moves the media clock on Android Piers.** All three members, same 22.0-BETA1 build:

| device | free-run ppm ± SE | with tempo 1.03 | delta (want +30000) | `PlaySpeed` / `IsTempo` | verdict |
|---|---|---|---|---|---|
| Bravia | +42763.6 ± 141.8 | +74095.4 | **+31332** | 1.03 / true | PASS |
| Pixel 7 Pro | −6286.9 ± 155.9 | +23050.3 | **+29337** | 1.03 / true | PASS |
| Tab S5e | +4761.1 ± 142.2 | +34822.7 | **+30062** | 1.03 / true | PASS |

So the actuator is real on every member, and S4.3's "smooth `SetTempo` path" is demonstrable on Piers. Two by-products settle other open questions: RPC uncertainty during playback is **1.7 / 3.3 / 2.5 ms** median, so the external channel resolves a 75 ms tolerance on every box (the ~100 ms idle latency in §5.2 was Wi-Fi power save, and streaming lifts the radio out of it); and the tempo→1.0 **restore skip does not reproduce** — position jitters ~105 ms per sample with no tempo at all, and the restore-window jump (170 ms) is indistinguishable from the tempo-window jump (172 ms), both an order of magnitude below the 1500 ms `_verify_tempo_restore` detector.

### 10.1 The finding: the feature's precondition can cost more than the feature can fix

The free-run column is the story. Tempo is available **only** with `videoplayer.usedisplayasclock` on — with it off, `SetTempo` is rejected `-32100` on all three, measured — and that setting slaves the media clock to the panel. So the rate error is set by the content-rate to refresh-rate ratio, and on this rig that ratio is never 1.

Three controls, same box, one variable each:

| Bravia | content | clock gate | free-run ppm |
|---|---|---|---|
| mismatched | 23.976 | **on** | **+42763.6 ± 141.8** |
| gate removed | 23.976 | **off** | −69.9 ± 141.9 |
| rate matched | **25.000** | **on** | **−65.9 ± 220.3** |

+42763 ppm against 50/47.952 = +42708 ppm predicted — 56 ppm apart, well inside one standard error. It is the PAL speed-up, arrived at by accident: a 23.976 film played out of a 50 Hz mode runs **4.28 % fast**, 2.6 minutes per hour. That is **14× the ±3 % the controller can command**, so drift crosses the 1500 ms tolerate ceiling in ~35 s and never returns. Tempo cannot fix a rate error larger than its own authority, and here its own precondition created one.

The other two boxes are the same mechanism, smaller: with the gate off they read +203.7 ± 149.3 (Pixel) and +21.2 ± 145.2 (Tab) — real time to within noise. With it on they read −6287 and +4761, i.e. ~0.5 % each and **in opposite directions**, so they diverge from each other at ~11000 ppm ≈ 40 s per hour. Both are inside the ±3 % cap, so tempo can hold them — but only by sitting permanently off 1.0×, which is a different feature from the brief excursions §2 assumed.

### 10.2 It is not configurable away on this hardware

At their working resolutions the panels offer:

| device | modes | best available for 23.976 | outcome |
|---|---|---|---|
| Bravia | 48 / 50 / 60 | 48 Hz → +1001 ppm | **Kodi will not switch** — see below |
| Pixel | 60 / 120 | 120 Hz → +1001 ppm | untested |
| Tab | **60 only** | none — no clean cadence exists | structurally stuck at +4761 ppm |

`videoscreen.whitelist` was empty on all four boxes (the Kodi default, which disables mode switching entirely) and `whitelistdoublerefreshrate` was false (which forbids using 48 Hz for 23.976 content). Setting both on the Bravia — whitelist `[48, 50, DESKTOP]`, double-rate allowed — **changed nothing**: `System.ScreenMode` stayed `3840x2160 @ 50.000000` and the rate stayed +42737 ± 140.5 ppm, with no mode-change attempt logged at all. On Android the display mode is the platform's decision (`match_content_frame_rate` is unset on all three and is not shell-settable), so the whitelist is not the lever here.

Net: **no member of this rig can reach a matched rate for 23.976 content**, which is the most common film rate there is. The feature is not broken in general — 25 fps content on the Bravia's 50 Hz panel measured −66 ppm, and 24.000 content on the Pixel's 120 Hz mode should match exactly — it is conditional on a rate match that these panels cannot provide for 23.976.

### 10.3 What this changes

* **The `#30552` / `#30554` contradiction resolves in favour of `#30552`.** "Works best with Sync Playback to Display turned off" is correct, and now has a measured basis: with the gate off, all three members track real time to within ~200 ppm, which is tighter group behaviour than any of them achieve with it on. Kodi's default (off) and kofin's default (`syncPlayDriftCorrection=false`) are the better SyncPlay configuration on this hardware. `#30554` must say what the precondition costs, not just that it exists.
* **P1/P2 need a third arm.** As specified they compare correction on/off with the display clock on, which on this rig measures the failure mode. Add **clock off, correction off** — where the group will be tightest — because "4.3 % rate error with correction" versus "200 ppm without" is the number that tells a user which way to set both switches.
* **A code follow-up with real weight**: the controller cannot detect that it is losing. A residual that stays one-signed across several engage/give-up cycles means a rate mismatch the actuator cannot reach, and the honest response is to stop correcting and tell the user their refresh rate does not match the content — not to hold a permanent speed-up. That is a better use of the give-up path than R-D's timer tuning.
* **Two kodi-drive contributions** (`kodi-drive:contribute`, not `CLAUDE.md`): `Player.SetTempo` does work on Android and is gated exactly as documented, with the `-32100` rejection when the gate is off; and `usedisplayasclock` makes the media clock inherit the panel's rate, so a content/refresh mismatch becomes a PAL-style speed-up — measured at +42708 ppm predicted vs +42763 ± 142 observed, with the rate-matched and gate-off controls to prove the mechanism. The second one also documents that an empty whitelist plus Android's own mode policy leaves no way to fix it from Kodi's settings.
