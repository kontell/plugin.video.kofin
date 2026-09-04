# PVR SyncPlay shakedown — direct play, remux, catchup, all through tempo

Goal: prove a three-member live group stays together on **direct play**, **force remux**, and **catchup**, with every stream riding **inputstream.tempo**. This is the P2 / P3 / P4 live gate of `docs/syncplay-pvr-plan.md`.

It is a measurement plan, not a design change. Each arm either passes with a ledger or names the residue that still needs code.

**Repos.** `plugin.video.kofin` (engine, this branch), `pvr.kofin` (stream properties, claims, catchup), `inputstream.tempo` (the only actuator). Jellyfin 10.11 stays stock — no core encoding patches.

**Rig.** `P1D-PIERS` (Kodi 22 flatpak), `TAB` (Galaxy Tab S5e), `BRAVIA` (Android TV). All three have kofin, pvr.kofin and inputstream.tempo. Drive with kodi-drive (`KODI_TARGET=p1d` / `tab` / `bravia`); Tab and Bravia over ADB. Evidence under `tests/live/results/pvr-shakedown/` (gitignored). Hosts and credentials stay in `targets.env`.

---

## 1. What is already true

pvr.kofin stamps `PVR_STREAM_PROPERTY_INPUTSTREAM` and, when that add-on is tempo, a `tempo_file` at `special://temp/inputstream_tempo.pvr.kofin` (`IptvSimple.cpp` `AppendTempoProperties`). Live uses the **Inputstream** spinner (`inputStream`, Tempo = 3). Catchup uses **Catchup inputstream** (`catchupInputstream`, Tempo = 1). Default for both is ffmpegdirect, so a box that has never been flipped will not pulse.

The engine arms fine sync from the claim's tempo block. A live claim (`RunTimeTicks` 0) is never seeked; convergence is pulses and holds (`LIVE_HOLD_MIN_MS`). The group position is the **source PTS** when tempo reports one older than `LIVE_CLOCK_MIN_START_S` (60 s). A remux that restamps from ~10 s is refused as a job clock; this branch supplies a **join origin** (server time of player t=0) so the group can still propose on a shared clock.

A shared-start Unpause **rebinds** that origin onto the group position so a fake decoder-open gap is not pulsed. A deferred / late Unpause does **not**: that gap is the picture offset of a later open, and zeroing it leaves pictures apart with residual ~0.

JSON-RPC `Player.Open {channelid}` while in a group is unmanaged playback (detaches). Tune from the PVR Home widget. JSON-RPC `Player.Time` on a PVR channel is **EPG-relative**, not stream PTS. Pictures (match clock, burned-in timecode) are the sync assertion. `Player.Time` matching is not.

`forceTranscode` is **Force remuxing** (copy). `forceTranscoding` is **Force the server to transcode**. This shakedown never turns the second one on.

Zap Unpause during LOADING is not `last_command`; `prepare_ready` runs it (`fix/syncplay-zap-unpause`).

### Ledger (dated)

| Date | What | Result |
|---|---|---|
| 2026-09-03 | Remux without join clock | player clocks agree, pictures ~4 s apart, `[ syncplay/tempo ] live group is on session time; no pulses` |
| 2026-09-03 | Join clock at AV start only | pictures matched at Unpause (67:28/67:28), then pulses closed a fake open-time gap and they ended 2 s apart (68:56 vs 68:54) with residuals ~0 |
| 2026-09-03 | Join clock rebound at Unpause, start-together | one matched +130 ms pulse each, residuals inside ±100 ms, pictures still matched 35 s later |
| 2026-09-03 | Channel zap Unpause during LOADING, before the zap-Unpause fix | Tab deferred, then ignored the Ready re-issue as a repeat, stayed paused |
| 2026-09-04 | Remux zap, rebind on the deferred Unpause | join clock rebound, pulses ~+110 ms, residuals ±150 ms, Premier Sports 2 match clocks **71:06 vs 71:10** (Tab ahead). Rebind present; pictures are the fail |

---

## 2. Pin these settings on every member, every arm

kofin: `syncPlayEnabled=true`, `syncPlayTempo=true`. pvr.kofin **Inputstream = Tempo**. Catchup arms also **Catchup inputstream = Tempo**. Timeshift stays on (the default).

Do not edit `settings.xml` on a running add-on. Disable pvr.kofin, write the file in the active profile, enable — `kodi-addon-driving`. Bounce kofin after a code deploy (`SetAddonEnabled` false→true); prove the new service line in the log.

Play from the **PVR UI** (Home recently-played, Channels, Guide). Do not `Player.Open` a `channelid` over JSON-RPC.

---

## 3. The three arms

| Arm | pvr.kofin | What Jellyfin must be doing | Clock the engine should use |
|---|---|---|---|
| **D** Direct play | `forceDirectPlay=true` (hides remux/transcode) | `/Sessions` `PlayMethod=DirectPlay` or `DirectStream`, no remux job | broadcast PTS via tempo `.state` (`source_ms` older than 60 s) |
| **R** Force remux | `forceDirectPlay=false`, `forceTranscode=true`, `forceTranscoding=false` | `PlayMethod=Transcode`, copy codecs, job restamped from ~10 s | join origin; rebind only on shared-start Unpause; deferred Unpause keeps the open-time gap as residual |
| **C** Catchup | same as D or R for the live path; catchup from EPG / Play from EPG | bounded, terminated catchup window (`catchup_terminates`); not a live-joined playlist | programme-relative, two-sided, tempo pulses |

Arm D is P0d's claim that two transcodes of one channel share tuner PTS, applied to **no** remux: if DirectPlay preserves source timestamps, P4 is the broadcast clock and the join origin stays idle. Arm R is the case this branch exists for. Arm C is P3: a past programme with a start and a runtime, full fine sync, picture-asserted position.

If production M3U has no catchup tags (pvr plan §8), arm C runs against the P0 bench catchup provider (timecode burned into the frame). Do not silently skip it.

Confirm the method **from `/Sessions`**, not from the setting. A DirectPlay setting that still remuxes is arm R wearing D's clothes.

---

## 4. Scenarios (each arm)

Every scenario starts `kodi-logtail mark` (P1D) / ADB log offset (Tab, Bravia) and ends on the lines since the mark. Screenshots of fullscreen video at T+0 after Unpause and T+30 s; read the match clock or burned-in time; do not assert from a single unread frame.

**S1 Start together.** P1D creates a group; Tab and Bravia join idle; P1D tunes a channel from the PVR widget. All three enter `waiting_ready` then `synced`. No 45 s load watchdog. No `Unmanaged playback`. Log: `[ syncplay/claim ] foreign: … via pvr.kofin`, `[ syncplay/play ]`, `[ syncplay/Unpause ]`, `join clock rebound` (arm R) or a real `source_ms` (arm D). Tempo armed on every member (`fine sync armed for … live:`). Pictures agree at Unpause and at T+30. Residuals after settle inside a pulse budget (~±150 ms), not a multi-second open-time gap.

**S2 Join mid-programme.** P1D playing in the group; Tab (or Bravia) joins. Follower loads, Ready, Unpause. Must not stay paused. Pictures close to the group, not a join-delay behind.

**S3 Channel zap.** From a playing group, P1D changes channel from the PVR UI. Followers get NewPlaylist + Play + Unpause. Unpause during LOADING must defer and then **run** (`prepare_ready` / Ready re-issue), not `already applied, ignoring the repeat`. No skip, no transcode reload of the live item. Arm R: the deferred Unpause must **not** log `join clock rebound`; the open-time gap is residual for holds/pulses. Pictures are the pass, not engine residual.

**S4 Pause / unpause.** Group pause holds the picture; Unpause resumes all three without a seek on live. Pictures still agree.

**S5 Leave.** One member leaves; the others keep playing; the leaver's tempo disarms.

Arm C adds **S6 Seek inside the programme** (two-sided, like a recording) and **S7 Pause-cut**, both with the picture clock as ground truth. An unbounded HLS catchup playlist is a fail, not a skip — P0f: the player position lies.

---

## 5. Pass / fail, per arm

**Pass.** All three members tempo-routed. Method matches the arm. **Pictures** stay together across S1–S5 (and S6–S7 for C) for at least ten minutes of playing, including one zap. Pulse ledger exists: either real PTS pulses (D, C) or join-clock pulses that do **not** open a multi-second picture gap (R). No stuck pause, no watchdog leave, no unmanaged detach. Engine residual inside a pulse budget is supporting evidence, not the pass.

**Fail, and it is this branch.** Stuck pause on zap → deferred Unpause still marked applied. Pictures diverge after a shared-start Unpause on remux with no rebound log → origin still AV-start. Pictures diverge after a zap / late open **with rebound present and residuals inside a pulse budget** → rebind treated an open-time picture gap as already closed. Session-time log and no pulses on remux → join origin not used as source offset. Catchup never seeks → `_skip_live_seek` still treating every delegated provider as live.

**Fail, and it is not this branch.** DirectPlay still remuxes → pvr.kofin / server profile. Tempo not stamped → Inputstream spinner not Tempo. Catchup opens as live → missing `catchup_terminates` / no catchup tags. Bravia cannot create a new tempo file under `Android/data` → `kodi-adb` stage-and-RunScript, not a kofin bug.

---

## 6. Order of work

1. Pin Tempo on all three boxes; bounce kofin; prove `inputstream.tempo` in the stream properties / claim.
2. Arm R first (the one this branch changes). S1 then S3. Stop if zap sticks or pictures walk apart after Unpause.
3. Arm D. Confirm `/Sessions` is actually DirectPlay. Expect PTS, not join-clock rebind as the clock in use (`source_offset_ms` returns a value).
4. Arm C. Enable catchup; if the household M3U has no tags, switch the P0 bench provider in. Picture clock mandatory. S6 must seek.
5. Write the ledger into `tests/live/results/pvr-shakedown/{D,R,C}/` — log greps (redacted), `/Sessions` PlayMethod, two screenshot pairs per scenario, pulse counts.

Do not start the next arm on a box that still has the previous arm's `forceDirectPlay` / `forceTranscode` in memory. Disable, edit, enable.

---

## 7. See also

- `docs/syncplay-pvr-plan.md` — P0 verdicts, P2 tune-together, P3 catchup, P4 source PTS
- `docs/syncplay-fine-sync.md` — pulse budget, tempo file, live ledger shape
- `docs/syncplay-provider-contract.md` — live claim is 0-runtime; delegated start
- pvr.kofin `IptvSimple.cpp` `AppendTempoProperties` / `GetLiveInputstream` / `catchupInputstream`
- kodi-drive: `kodi-addon-driving` (bounce), `kodi-adb` (push/checksum), `kodi-logs` (mark/since), `kodi-playback-tempo` (display-as-clock is not this actuator)
