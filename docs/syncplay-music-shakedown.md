# SyncPlay music shakedown — characterise the current path, then find a better one

Goal: two numbers and a decision. First, **what group music playback actually costs today** — the R-K scenario `docs/syncplay-drift-shakedown.md` specified and never ran, because Gate 0 ended that day. Second, which of three alternative paths is worth shipping: **downloaded files** (available now, no code), **VideoPlayer + inputstream.tempo** (the actuator video got in `docs/syncplay-fine-sync.md`), or both together.

Target server: Jellyfin 10.11.11 `minipie` at `192.168.1.167:8096`, direct — this study is about the player, and R-G already covers the proxy.

Rig: four members. `P1D-OMEGA` native 21.3 on `192.168.1.112:8080`, `P1D-PIERS` the 22.0-BETA1 flatpak on `:8081`, `TAB` (Galaxy Tab S5e, Android 13), `BRAVIA` (4K AE2, Android 14). The two P1D instances are the measurement pair — §6.1 is only possible there — and the Android boxes are the realism arm.

---

## 1. Where music is today, in the code

Music never touches the fine-sync controller. One line does it:

```python
# plugin/play.py:128, tempo_route()
if item.get("Type") in AUDIO_TYPES or play_method not in TEMPO_METHODS:
    return None
```

so no `inputstream` property is stamped, no `Tempo` route reaches the play claim, and `PulseScheduler` has nothing to nudge. Music plays through **PAPlayer** (`plugin/playall.py:142` builds an explicit `xbmc.PLAYLIST_MUSIC`), and PAPlayer has no rate control at all — not Kodi's `Player.SetTempo`, a VideoPlayer feature, and not inputstream.tempo's, which is not routed here.

The rest of the audio handling is a set of workarounds for PAPlayer, each load-bearing and documented in place:

| Site | Behaviour | Why |
|---|---|---|
| `playback.py:143` `_prealign_unpause` | audio returns early | "a paused PAPlayer must never be seeked" |
| `playback.py:289` `_do_unpause` | resume first, align never | seeks and even the pause toggle queue up until user input on some builds |
| `playback.py:351` `_align_after_resume` | audio returns early | "whose resume choreography aligns itself" |
| `playback.py:393` `_resume_with_retries` | nudge until the clock moves | a single toggle can be swallowed at a boundary |
| `playback.py:652` | no seek for a paused audio hold | paused PAPlayer again |
| `playback.py:270` | phase, not player reads, at a boundary | `isPlaying`/`getTime` intermittently report no media that is right there |

So group music today is **pure toleration**: members converge only when a command arrives, and between commands nothing observes or corrects.

**The track boundary is the other half, and gapless is already gone because of it.** On a native playlist advance `manager.py:1203 on_playback_started` fires before A/V rolls and calls `playback.ensure_paused()`; `_identify_held_play` maps the Kodi id to a Jellyfin id and proposes the transition; the group answers with a scheduled `Unpause`. Every boundary is a full server round trip with the audio held silent across it. A 45-minute episode pays that once; a 3-minute track pays it every 3 minutes. **This study therefore has no gapless to protect and nothing to lose by leaving PAPlayer** — the question is only how much smaller the gap can be made.

Two things are absent from the tree and must be treated as unknowns, not defaults: **`musicplayer.crossfade`** appears nowhere in `lib/` (a non-zero crossfade overlaps two tracks and makes both the hold and any position reading meaningless), and there is no music-specific handling of ReplayGain or passthrough.

---

## 2. Downloaded music is a different path already, and it is untested

Music downloads shipped in phase 3 (`docs/offline-downloads-plan.md` W3.2, gate G12). A downloaded song is **repointed into MyMusic** — `song.idPath` and `strFileName` rewritten to the local file — so it plays from `musicdb://` as an ordinary local file. G12 recorded it playing "instantly" with the server blackholed.

Two consequences, and they pull in opposite directions:

* **The per-track cost collapses.** No PlaybackInfo round trip, no server stream open, no network in the critical path. What remains at a boundary is the group round trip alone, and it is symmetric across members that all have the file. This is why the path is worth testing early — and it needs **no code at all**.
* **It bypasses `plugin/play.py` entirely.** A repointed library row never reaches the play route (`play.py:472`), so `tempo_route()` is never called for it. The same premise break bit phase 4 already: `BACKFILL_MEDIA_TYPES` assumed video always reaches the play route, and a downloaded item played from the library went unclaimed until that was fixed (`offline-downloads-plan.md` line 129).

What *does* still work is the part that matters most here: `_identify_held_play` (`manager.py:1465`) identifies items by **Kodi library id**, not by the play route, and the mapping row survives the repoint (the W3.2 addendum exists to guarantee exactly that). So **group transitions already work for downloaded music today.** It has no actuator, like streamed music — but it does not pay the stream-open cost. That is a free arm in the experiment and it should be run first.

---

## 3. Why music's tolerance is not video's

This is the argument for doing any of this, and it is not video's argument.

Video's shipped band is ±150 ms with a 75 ms deadband (`syncplay-fine-sync.md` §4, S1-P1.2b). For music in one house that is far too loose, for two reasons that do not apply to a talking head:

* **The precedence effect.** Two sources of the same audio fuse into one image below roughly 30–40 ms of separation; above it the second arrival is heard as a discrete echo. A household playing an album in the kitchen and the living room hears the group's delta directly whenever sound leaks between rooms — and it is a step change in quality at a few tens of milliseconds, not a gradual one.
* **Rhythm makes a constant offset audible.** A steady 120 ms offset on dialogue is invisible; on a snare it is a flam.

So the bar is **tens of milliseconds, not hundreds** — and a converge-on-commands-only design cannot hold that across a 40-minute album by construction. If §7 shows the current path already holds ±30 ms because commands are frequent enough, none of §8 is worth building.

---

## 4. The four arms

| Arm | Source | Player | Actuator | Code needed |
|---|---|---|---|---|
| **A** | stream | PAPlayer | none | none — this is today |
| **D** | download | PAPlayer | none | **none** — shipped, never measured in a group |
| **B** | stream | VideoPlayer + tempo | pulses | §8.2 |
| **E** | download | VideoPlayer + tempo | pulses | §8.2 + §8.3 (the hard one) |

A and D are measurements of shipped behaviour and cost only rig time. B is the change §8.2 describes. E needs B *plus* a way to route a library-row play through VideoPlayer, which §8.3 treats as an open problem rather than a step.

The study's shape is: measure A and D, and only build B if D does not already clear §3's bar. **D may be the answer on its own**, and finding that out is cheap.

---

## 5. Pre-flight

Once, before any scenario, recorded:

1. **Both P1D instances up and separable.** Omega on 8080/9777, Piers flatpak on 8081/9778 (`tools/dev-install.sh --flatpak`). `targets.env` has no flatpak entry — add `KODI_PIERS_*` (`HOST=192.168.1.112`, `PORT=8081`, `TRANSPORT=ssh`, `ADDR=conor@p1d`) so the helpers can address it as a first-class target.
2. **Same build everywhere.** Deploy this tree to all four, disable→enable, confirm a `--->>> kofin service` line newer than the tree's ctime on each. Build skew between members was the unresolved confound in the 2026-07-10 field capture (`notes/…/syncplay-kodi-implementation.md`) and must not recur.
3. **Settings frozen and recorded on all four**: `musicplayer.crossfade=0` (except M7), `audiooutput.passthrough=false`, ReplayGain off, `videoplayer.usedisplayasclock=false`, screensaver `None`, Android screen-off raised with `svc power stayon true` on charge.
4. **The asset — built 2026-08-31, `tools/make_sync_asset.py`.** *Kofin Test Signals — Kofin Sync Test Album*, 10 FLAC tracks, 29m45s, in the `Music-Alt` library at `/media/bluecon/music-alt/Kofin Test Signals/`; Jellyfin album id `12a6f171c157efc60f81ca02445d73dc`. Verified by `tools/verify_sync_asset.py`, which gates every property the measurement depends on:

   | Property | Measured | Why it is gated |
   |---|---|---|
   | Marker grid | **0.000 samples**, max 0.000 | every timing read inherits any grid error |
   | Matched-filter SNR | 33 900× (90.6 dB) | discrimination against the programme, in-window |
   | Precision floor | **54 µs** (a consistent bias, so it largely cancels differentially) | no §10 threshold may sit near it; 550× below the 30 ms bar |
   | Opus 128 / AAC 256 / MP3 320 | bias +0.00, jitter 0.00 samples | arm D's downloads transcode to Opus, so cross-codec comparison must hold |
   | Internal dropouts | 0 ms quiet runs, min block −19 dBFS | silence in a capture must be the player's, never the asset's |
   | Track identification | 10/10, worst margin 2.73× | a capture identifies the track from the marker alone |
   | Edges | head −11 dB, tail −14…−19 dB vs −15 dB whole | leading or trailing silence reads as a boundary gap |

   Four design points are consequences of failed checks, not preferences, and must survive any regeneration: the marker is a **6 ms chirp at 8–16 kHz** with the **percussion low-passed at 5 kHz**, because a 2–6 kHz marker was swamped by drum hits and the filter locked onto percussion in nearly every window; the sweep **direction alternates** per track, because frequency offset alone left neighbours at 0.69 of the correct peak; the bed is generated **past the end and cut**, because an envelope anchored to a truncated segment plays its whole decay inside the stub (every track faded to −50 dBFS over its last 300 ms); and the pads **overlap by their release**, because a release meeting the next attack at zero put a dip at every bar line. Regeneration is deterministic — the same `--seed`-free recipe reproduces byte-identical files (md5-checked).
5. **The same album downloaded to all four**, verified repointed (`song.idPath` at the local row) — arm D's precondition, and G12's protocol already covers checking it.
6. **Baseline position-read integrity.** `Player.GetProperties` `time` advances monotonically for PAPlayer on each member for 60 s; record the boundary read-hole rate (`playback.py:270` says to expect holes; the sampler must survive them, and the rate is itself a reported number).

---

## 6. Instrumentation — three channels, in trust order

### 6.1 Sample-accurate audio capture (the P1D pair) — the reason the pair is the measurement rig

P1D runs PipeWire 1.6.8 with PulseAudio compatibility; `parec` and `pw-record` are both present. Give each instance its own null sink and record both:

Implemented as `tools/music_capture.py`, which runs on P1D and does the whole cycle: null sinks, `audiooutput.audiodevice` per instance, playback, `parec` on each monitor, then every setting restored before any module is unloaded.

**Route by setting, never by moving streams.** Measured 2026-08-31: PipeWire's Pulse compatibility makes the two Kodi streams indistinguishable (same application name, no PID, uncorked when idle, new index after every stop/open), and unloading a null sink that still has a stream attached wedges Kodi's AudioEngine so hard that only a restart clears it. The full account is in `tests/live/results/music-A/M0-rig-gate.md`.

Both captures come off one clock on one host, so the delta between them is **sample-accurate and includes the sink latency JSON-RPC cannot see**. Offline, from the two files:

* **Δ(t)** by cross-correlating the click train in a sliding 5 s window — the true audible delta, ±1 ms, once per window;
* **track-start gap** as the silence run between the last sample of track N and the first of N+1, per member, to the sample;
* **dropouts and restarts** as silence runs *inside* a track — the objective form of "smoothness".

This is the only channel that measures what a listener hears. It does not reach the Android members; their arm is §6.2 and §6.3 and their absolute numbers are not comparable to the pair's. That is a stated limit, not an oversight.

### 6.2 The controller's own log

`LOG_PATTERN` in `tests/live/syncplay_fine_sync.py` greps `syncplay/(pulse|tempo|align|resumed|landed|hold|unpause)` — widened 2026-08-31 from the video set.

**Added 2026-08-31** (logging only; the control flow of a shipped add-on is untouched, and the full unit suite is green at 2941):

* `[ syncplay/hold ] entered (transition|fresh start)` at `manager.py:1240`, and `[ syncplay/hold ] released after N ms (adopted|released|detached|stopped|ended)` from `_hold_done`, called at all five sites that clear a hold. Entered→adopted **is** the boundary wait from inside the controller, and it is what §6.1's capture times from outside. Two channels, one event, neither replacing the other.
* `[ syncplay/unpause ] resumed after N nudge(s), M ms`, and a warning on the give-up path.

A correction to this plan as first written: `_resume_with_retries` was **not** silent — it already logged `[ syncplay/unpause ] nudging the player` per nudge, so nudges were always countable. What was missing was the *outcome*: a resume that took three nudges and one that took none are indistinguishable from the nudge lines alone, and that difference is exactly what churn measures.

### 6.3 JSON-RPC sampler

`syncplay_fine_sync.py` samples `Player.GetProperties` at 4 Hz and already works for playerid 0 (`active_player()` takes `players[0]`, the music player when music is playing). Two music-specific extensions:

* **Sample `Player.GetItem` alongside the position.** At a boundary two members are on *different tracks*; differencing their positions then yields minus one track length. Every delta must be qualified by "both members report the same item", and samples where they do not become a separate series — **straddle time**, itself a metric.
* **Tolerate read holes** rather than aborting, and count them.

### 6.4 Metrics, defined once

| Metric | Definition | Channel |
|---|---|---|
| **Δ** | inter-member audible offset, same track | 6.1 (pair), 6.3 (Android) |
| **TSG** | silence from last audio of track N to first of N+1, one member | 6.1 |
| **BSS** | boundary start spread: max−min across members of the first-audio instant | 6.1 + 6.3 |
| **Straddle** | wall time during which members report different items | 6.3 |
| **Churn** | pause/unpause/seek events not caused by a user action, per track | 6.2 |
| **Recovery** | time from a perturbation to \|Δ\| back under the bar | 6.1 |
| **Dropout** | silence run >50 ms inside a track | 6.1 |

---

## 7. Phase A — characterise arms A and D

No code changes. Each scenario runs **twice, once per arm**, back to back on the same members in the same session so the network and the boxes are constant. Every run reports the full §6.4 table.

* **M0 — rig gate.** ~~Asset plays direct on all four; captures line up; positions advance; hole rate recorded.~~ **Run 2026-08-31 — PASS**, `tests/live/results/music-A/M0-rig-gate.md`. 0 read holes in 480 samples, item keys stable and comparable across members, free-run spread **199 ppm** (§2.1's prediction confirmed). Two findings: JSON-RPC read uncertainty exceeds §10's 30 ms bar on three of four members, so §6.1's capture is the only channel that can answer for Δ; and **music bypasses `plugin/play.py` entirely in direct mode**, which invalidates §8.2 — see §8.5. Capture alignment passes: Δ constant at +119.3 ms across six windows, correlation 0.974, zero dropouts — the channel adds no jitter of its own.
* **M1 — steady playlist.** ~~Ten tracks, no interaction.~~ **Run 2026-08-31 (arm A) — `tests/live/results/music-A/M1-steady-playlist.md`.** Arm A fails every §10 bar: **TSG median 1890 ms** against a 250 ms bar, **BSS 135 ms** against 50 ms, **Δ median −90 ms** against 30 ms. Straddle among the three healthy members was **0 s of 1830** — the group transitions together; what it cannot do is transition *quickly*. The Bravia fell out after track 3 and never recovered (2 boundaries, not 9), which M2 must chase. The two channels disagreed by ~100 ms of sink latency on the same pair, which is §6.1's justification made concrete. **Arm D run 2026-08-31 — `tests/live/results/music-A/M1-arm-comparison.md`.** Downloading **halves TSG (1880 → ~850 ms**, reproduced across two runs: 880 and 840 ms) and **does not move Δ at all** (−90 → −108 ms). Neither arm clears any §10 bar, so §10's "simplest arm that clears the bar wins" does not resolve here and §8 remains the only lever for Δ. Arm D does establish the **boundary floor**: ~730–880 ms is the group round trip plus player start, which no local caching goes below — any design keeping the propose-and-wait handshake per track inherits it. **M2 (2026-08-31) chased the two dropouts and they are unrelated** — `tests/live/results/music-A/M2-dropout-investigation.md`. Arm A's Bravia was a **harness artefact**: the driver dismissed kofin's stopped-prompt with `Input.Back`, and a cancelled selection falls into the spectator branch by design (`manager.py:1394`), so it spent the run as a spectator the group never waited for. Arm D's Tab is a **real defect**: its queue update was dropped by the version dedup, the `Unpause` was deferred to a ready flow that never ran, and kofin left the group on a 45 s load timeout — stalling both healthy members for 10.5 s on the way, which is the one outlier in arm D's TSG.
* **M2 — pause and unpause.** Group pause mid-track ×3 at varied offsets, then the hard one: pause within 2 s of a boundary. Watch for a member that never resumes — `_resume_with_retries` exists because a toggle can be swallowed there.
* **M3 — skip.** Next ×3, previous ×2, and a skip issued *during* a boundary hold, where a proposal is already in flight.
* **M4 — seek within a track.** ±10 s. Confirms the paused-seek prohibition does what its comment claims.
* **M5 — hot join.** A member joins at track 4, and again mid-track. How long it straddles, and where it lands.
* **M6 — the realism arm.** Android members, one direct-play and one transcoded (the AAC copy), so the transcode path is characterised before §8 excludes it.
* **M7 — crossfade on.** `musicplayer.crossfade=5` on one member. Expected to be bad; the point is *how* bad, and whether kofin should refuse to sync a member that has it on.
* **M8 — the soak (R-K, finally).** The album **on repeat** for 40 minutes, no interaction, all four — it is 29m45s, and looping is preferred to a longer asset because a 14-track album would push the marker band to 18.3 kHz where Opus starts to cut. The repeat means a track id recurs in a long capture, which §6.3's `Player.GetItem` series and the wall clock disambiguate. The one number nothing else produces: **uncorrected cumulative Δ**, and whether the group drifts past §3's bar and stays there.

**Phase A's output is two baseline tables** in `tests/live/results/music-A/`, and a decision: if arm D clears §10's bars, §8 is not built and the finding is "download the album". Phase B does not start until that is written down.

---

## 8. Phase B — VideoPlayer + inputstream.tempo

### 8.1 The mechanism is already proven next door

`plugin.audio.kotome` is an audio client that plays through **VideoPlayer and inputstream.tempo** on this rig today, and it settles what would otherwise be this plan's largest open question (`main.py:1667`):

```python
# mediaType=musicvideo routes the ListItem to VideoPlayer while still
# landing in WINDOW_VISUALISATION for audio-only content.
vtag = li.getVideoInfoTag()
vtag.setMediaType("musicvideo")
li.setProperty("inputstream", "inputstream.tempo")
li.setProperty("inputstream.tempo.mime_type", track.get("mimeType", "audio/mp4"))
li.setProperty("inputstream.tempo.tempo_file", TEMPO_FILE)
```

So: a `VideoInfoTag` with `mediaType="musicvideo"` forces VideoPlayer **and keeps the visualisation window**, which answers the OSD and now-playing concern at the same time. `setContentLookup(False)` and an explicit `mime_type` come with it — kofin's video path sets neither, and audio-only content is exactly where the mime type stops being inferable from the URL.

**One contradiction to settle before writing code.** Three sources disagree about `start_time`: `inputstream.tempo` README:39 says "Audio-only items under PAPlayer … never set it for video"; kofin's `play.py:152` says it "arms a hold meant for PAPlayer resumes" and deliberately omits it; kotome sets it *with* `StartOffset` under VideoPlayer and calls the hold "player-agnostic". Two of those cannot both be right. It matters for hot join and seek (M5, M4), so **M-B0** is a short probe that resolves it empirically rather than by reading, and confirms on P1D-PIERS that a kofin-routed musicvideo item lands on VideoPlayer, that the tempo file moves its clock, and that the play still scrobbles and increments the play count.

### 8.2 The change

* **`plugin/play.py`** — `tempo_route()` grows an audio branch instead of the blanket `AUDIO_TYPES` bail: audio routes when the play method is DirectPlay or DirectStream, passthrough is off, and the new setting is on. Transcoded audio stays unrouted, consistent with video (study §4.7). `stamp_tempo_route` gains the `musicvideo` info tag, `setContentLookup(False)` and `mime_type` per §8.1.
* **A setting, `syncPlayMusicVideoPlayer`, default off.** Non-negotiable: it makes Phase C an A/B inside one session rather than a comparison across two days, and it is the revert if the field disagrees.
* **`syncplay/playback.py`** — every audio special-case in §1's table becomes conditional on *which player holds the item*, not on `_is_audio()`. Under VideoPlayer, `_prealign_unpause` and `_align_after_resume` should run: they are what makes a video start tight and they are the mechanism expected to collapse BSS. The PAPlayer branches stay — the setting is off by default and that path remains shipped.
* **`syncplay/tempo.py`** — no change expected; the scheduler is player-agnostic. If it needs one, that is a finding.
* **Queue depth.** `queue_secs` is amortised over a 45-minute episode and paid **every 3 minutes** on an album. Whatever video uses is probably wrong here. **M-B1** sweeps 1 / 2 / 4 s against TSG and against pulse effectiveness, and this is the most likely place for the change to make things worse.

### 8.3 Arm E is a separate problem

A repointed library row never reaches `plugin/play.py` (§2), so none of §8.2 applies to downloaded music. Options, in increasing order of intrusiveness: a `playercorefactory.xml` rule matched on the downloads root (Kodi-native, no kofin code in the play path, and the cleanest if it works); a mimetype or content-type stamp written at repoint time; or intercepting `Player.OnPlay` and restarting the item, which is ugly enough that it should probably disqualify arm E.

**Do not attempt E until A, D and B are measured.** If D already clears the bar, E is the wrong shape — it would add an actuator to a path that does not need one.

### 8.5 M0 finding — §8.2 does not reach music (2026-08-31)

`sync/writers/music.py:606` picks the MyMusic path from the `musicTranscode` setting. With it **off** — the default, and the rig's setting — a song's stored path is a direct `<server>/Audio/<id>/stream.<ext>?static=true` URL that Kodi opens itself, so `plugin/play.py` never runs and `tempo_route()` is dead code for music. Confirmed live: the library path and the playing path are the same direct URL on two members. With it **on**, music does reach the plugin route — but it is then transcoded, and §8.2 excludes transcoded audio from routing to stay consistent with video.

So removing the `AUDIO_TYPES` gate changes nothing in the mode the rig runs, and the only mode that reaches the route is one the plan refuses to route. **Arm B needs what §8.3 said only arm E needed.** The least-bad shape is a third path mode — plugin paths for music *without* transcoding — which is a change to the sync writer plus a library rewrite on every box, not a one-line edit in the play route. That cost belongs in the comparison, and it is a further reason to measure arm D before building anything.

### 8.4 What this may cost

Each is a measured item in Phase C, not a caveat:

* **Gapless is not on this list.** §1: the boundary hold already destroys it in a group, and `tempo_route` returns `{}` outside a group session (`core/state.py:389`), so solo playback keeps PAPlayer and keeps gapless untouched. This was the risk I expected to dominate and it does not exist.
* **Passthrough households cannot use this at all** — tempo needs the audio resampled. The setting must be inert, not broken, when passthrough is on.
* **Library integration**: scrobbling, play counts, `MusicPlayer.*` infolabels (a `musicvideo` item populates `VideoPlayer.*`), party mode, and what skin.contuary shows. kotome's WINDOW_VISUALISATION behaviour is encouraging but kotome is not writing to a music library.
* **Start latency per track** from `queue_secs` (§8.2).
* **Battery and heat on the Android members** — VideoPlayer for an audio file is a heavier pipeline, and the Pixel's thermal behaviour is already a known confound.

---

## 9. Phase C — re-run and compare

Phase A's scenarios, unchanged, with `syncPlayMusicVideoPlayer` on, in the same session as A and D where possible. M1, M2, M3, M5 and M8 are the comparison set; M7 re-runs because the answer may differ; M4 re-runs because the paused-seek prohibition no longer applies and that is a behaviour change worth confirming rather than assuming.

Report every §6.4 metric as a **paired table across all measured arms with the same member set**, and report §8.4's costs alongside. The table must make a win-with-a-cost visible rather than burying it.

---

## 10. What "improved" means

Provisional, to be replaced by numbers derived from Phase A — a bar set before the baseline exists is a guess:

| Metric | Bar | Why |
|---|---|---|
| **Δ**, steady, P1D pair | median ≤ 30 ms, p95 ≤ 50 ms | §3, the precedence effect |
| **Δ**, 40-min soak | no monotonic growth; ends where it started ±30 ms | the actuator's whole job |
| **TSG** | ≤ 250 ms, and no worse than arm A | below where a gap reads as a stumble rather than a beat |
| **BSS** | ≤ 50 ms | boundaries are where a group is most audibly wrong |
| **Churn** | 0 non-user events per track, steady state | a resume needing three nudges is a bug, not a tolerance |
| **Dropouts** | 0 | non-negotiable |

**The simplest arm that clears the bar wins.** If D clears it, ship "download the album" and write B up as unnecessary. B is reverted if TSG regresses against A, or if Δ improves by less than §8.4 costs — stated in advance so a marginal result is not argued into a win afterwards.

---

## 11. Sequencing and cost

| | Work | Time |
|---|---|---|
| **Day 0** | ~~Asset (§5.4), `KODI_PIERS_*`, sampler (§6.3), capture rig (§6.1), log lines (§6.2)~~ — **all done 2026-08-31, each self-tested**. Only **M0** remains, and it needs the devices. | done |
| **Day 1** | M1–M8 on arm A, then arm D. Phase A tables written, decision recorded | ~6 h |
| **Day 2** | *Only if D misses the bar:* M-B0 (§8.1) → §8.2 → M-B1 queue sweep | ~5 h |
| **Day 3** | Phase C re-runs and the paired comparison | ~4 h |

Day 0 is the highest-value block and needs no devices: the asset and the sampler are what make every later number trustworthy, and both can be built and self-tested on minipie. Day 1 is where this study most plausibly ends.

---

## 12. Deliverables

* `tests/live/results/music-A/` and `music-C/` — the arm tables and their raw captures.
* ~~`tests/live/syncplay_music.py`~~ — the driver, sharing `Member` with `syncplay_fine_sync.py`; `--selftest` proves the boundary artefact is excluded (naive differencing reports −2960 ms where the qualified metric reports +40 ms).
* ~~`tools/music_capture.sh`~~ — null sinks plus `parec`, with a `probe` mode that validates the path without touching a running Kodi and a `restore` mode for a run killed before its trap. Proven on P1D end to end: correlation put the capture 597 ms behind the source and the silence detector independently found 590 ms of leading silence, agreeing to 7 ms.
* ~~`tools/analyse_capture.py`~~ — cross-correlation, gaps, dropouts; `--selftest` recovers an injected 37.5 ms offset to 0.000 ms and a 420 ms gap exactly, and refuses windows where the members are on different tracks rather than scoring them.
* ~~The asset recipe, committed~~ — `tools/make_sync_asset.py` and `tools/verify_sync_asset.py`. The numbers are only reproducible against the same asset, and the verifier is the gate that says so.
* A `## S-M` section in `docs/testing-plan.md`, ids filled in as they run.
* Whichever arm wins, plus its setting — or a written finding that none of them is worth the cost.
