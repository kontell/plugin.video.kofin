# SyncPlay remaining work — skip calibration, Ignore DTS, live inject

| Field | Value |
|---|---|
| **Date** | 2026-09-09 |
| **Status** | Remaining after today's pulse-budget PR |
| **Companion** | `docs/syncplay-fine-sync.md`, `docs/syncplay-pvr-plan.md`, `docs/syncplay-pvr-shakedown.md`, `docs/testing-plan.md` S4.8 |

This is the leftover from the three SyncPlay comments after the pulse-budget slider and the 40 s pulse cap.

---

## Already shipping (not this document)

`syncPlayPulseBudget` is default 5000 / min 1000 / step 1000 / max 10000, still milliseconds on the same setting id.

`PULSE_MAX_S` is 40.0 so one pulse at 25 % closes 10 s — the slider max — in one go (a 5 s residual is 20 s at 1.25×). Existing stored `2500` stays 2.5 s. `#30593` / `#30594` / `#30596` were not reworded.

The first draft of this work kept `PULSE_MAX_S` at 10 s (two pulses for a 5 s residual). That was overridden: there is no reason a pulse must stop at 10 s when the budget can now be 10 s.

Live items remain unseekable. No i18n blast, no full Kodi restart — an add-on bounce is enough.

---

## Remaining PRs

### PR2 — Skip-lag lifetime, log, clamp

Skip calibration already exists: `PlaybackController.seek_lag_ms` (default 500 ms) is an EMA of restart time plus landing error, learned from every *playing* seek that shows a hold, aimed ahead on `correct_position` / `_align_after_resume`.

```
lag = (restart_ms - started) - (restart_pos - target_ms)
seek_lag_ms = 0.5 * old + 0.5 * lag
```

That **is** "the delta post skip, added to subsequent skips, updated at each skip." The Tab: restart ~370 ms, land 350 ms early → lag ~720 ms (`docs/syncplay-fine-sync.md` §4, S4.8).

What is *not* true today: it is not reset on leave (`_leave_locally` → `stop_loop` resets pulse `_gain` to 1.0 and leaves `seek_lag_ms` alone), there is no log line, and a pathological seek can walk the EMA to seconds.

**Do:**

- Reset `seek_lag_ms = SEEK_LAG_DEFAULT_MS` in `PlaybackController.stop_loop` so lifetime matches pulse `_gain` and the comment's "kept until leave."
- INFO log when the EMA actually updates: `[ syncplay/seek ] lag 720ms (restart 370ms, land -350ms, ema 610ms); group residual +80ms`. The group residual is diagnostic only — do not fold it in.
- Clamp after the EMA: `SEEK_LAG_MIN_MS = 0.0`, `SEEK_LAG_MAX_MS = 3000.0`.
- Keep the restart+landing formula. Post-settle group residual mixes timesync and queue delay into a pipeline compensator; the 30 s blackout plus pulses already finish leftovers.
- Do **not** apply lag on paused `_prealign_unpause` (the group is not moving).
- Do **not** log a lag line on the live early-return. Live never seeks (`_skip_live_seek`).
- Do **not** add `syncPlaySeekLagPersist` unless open question 1 is yes. If yes: boolean default false, in-memory only, strings `#30639` / `#30640` (the 30639–30649 gap; do not reuse 30600–30638 / 30650–30673), 28-file i18n, full Kodi restart. Help must say this is wrong for a box that moves LAN ↔ WAN.

**Files:** `lib/kofin/syncplay/playback.py`, `lib/kofin/syncplay/tempo.py` (the min/max constants), `tests/unit/test_syncplay_tempo_wiring.py`, `docs/syncplay-fine-sync.md` §4 and §5.

**Tests:** existing four seek-lag tests stay. Add `test_stop_loop_resets_seek_lag` / `test_leave_resets_seek_lag`; `test_seek_lag_ema_is_clamped`; `test_seek_lag_log_line` (fires iff a hold was seen). A group Seek does not teach lag (`_do_seek` pauses first).

**Not:** a parallel calibrator, addon_data, a kofin Ignore DTS setting, seeking live items.

### PR3 — Ignore DTS live A/B

A measurement, not a kofin setting. Jellyfin's M3U **Ignore DTS (decoding timestamp)** sets `MediaSourceInfo.IgnoreDts` and ffmpeg gets `-fflags +igndts` (often `+genpts` on copy). Live SyncPlay's group clock is source PTS (`source_ms`). DirectPlay may never hit server ffmpeg, so a mixed DirectPlay + remux group is the interesting case.

jellyfin/jellyfin#13301: unchecking the option does not always drop the flag (recordings hardcode `IgnoreDts = true` in `GetRecordingStreamMediaSources`). An arm whose ffmpeg line disagrees with the checkbox is invalid, not a kofin finding.

**Rig:** P1D flatpak, Tab, Bravia. Same as `docs/syncplay-pvr-shakedown.md` §2. Play from the PVR UI. D-arm is `forceTranscode=false` and `forceTranscoding=false` — pvr.kofin has no dashboard `forceDirectPlay`. Bounce the tuner / open a fresh live stream per arm (`ReadAtNativeFramerate` is snapshotted at open; IgnoreDts is the same class).

**Matrix:** D-off / D-on (negative control — flag must not appear), R-off / R-on, T-off / T-on, then mixed P1D DirectPlay + Tab remux + Bravia remux. T-off expected clock is PTS if P0d still holds, join origin if this encode restamps — a source-clock transcode with the flag off is the P0d baseline, not contamination.

**Pass:** ffmpeg line matches the checkbox; pictures stay together; `source_ms` agrees across DirectPlay and remux to ~±150 ms (fixed bar, not `syncPlayPulseBudget`); or remux correctly falls back to join origin. Write "Ignore DTS is orthogonal to P4" and stop.

**Fail on restamp:** existing `live_on_source_clock()` / `source_offset_ms` young-clock refusal degrades to P2 command-only. Do not invent a client DTS parser. Document "leave Ignore DTS off for SyncPlay groups" in the shakedown and `docs/syncplay-pvr-plan.md` §8. A member-specific-but-old restamp (mixed-group `source_ms` disagrees while each clock looks old enough) is a one-line `source_offset_ms` policy with an L1 test, gated on that evidence.

**Fail on #13301:** record it, do not change kofin.

**Evidence:** `tests/live/results/pvr-shakedown/ignore-dts/` (gitignored). Scrub via `kodi-drive/scripts/scrub.py`. Skip calibration is not in scope; do not seek.

**Files:** `docs/syncplay-pvr-shakedown.md` (new section) or `docs/syncplay-ignore-dts.md`; optional collector `tests/live/syncplay_ignore_dts.py`. Product code: none unless the A/B forces the `source_offset_ms` follow-up.

### Live follow-up (not a merge gate) — rewrite `inject()`

S4.8's +1 s seek (2026-08-22, `ccfd731`) was against a 300 ms floor, not 2500. Today's `inject-seek` (`1.5×` / `2.0 s` ≈ +1000 ms) sits inside both the old 2.5 s budget and the new 5 s default, so it is not the seek or calibrator gate. Fine-sync §6.3 still says "inject ~1 s and see a seek" — leftover from the 300 ms era.

`inject()` writes the rate once, sleeps, then writes `1.0`. A long `inject(a, 1.5, 12.0)` never leaves a clean +6 s: `_tick` fills `WINDOW_SAMPLES` (~3 s) and `_start_pulse` overwrites the file.

**Do not** land +6 s / +12 s by sleeping longer at 1.5×. **Do not** take a 2 s hold at 4× / 7× (unproven on this rig).

Rewrite `inject()` so it re-holds 1.5× for the whole interval (yield ~500 ms if a pulse start is on the file so confirmation is not starved, then steal the rate back), then `1.0`, then one 3 s window. A `[ syncplay/pulse ] … command-only sync for this item` line fails the run. `inject-ahead` / `inject-behind` stay single-write.

Then, with PR2's log line:

| Case | Slider | Leftover | Assert |
|---|---|---|---|
| Seek at default | 5000 | ≈ +6000 ms | one `[ syncplay/align ] … seeking`; PR2 lag line |
| Leave/rejoin reset | 5000 | same after leave/rejoin | next seek logs `ema` starting from 500 |
| Pulse-not-seek | 10000 | ≈ +6000 ms | pulses only |
| Seek at max | 10000 | ≈ +12000 ms | one seek |

Append `tests/live/results/S4.8-fine-sync.md`. Do not replace the 2026-08-22 ledger.

---

## Open questions (still product calls)

1. **Persist skip lag across groups?** Recommend **no**, and no checkbox, in PR2. Pulse gain already resets on leave; re-convergence is one or two playing seeks. If yes: in-memory `syncPlaySeekLagPersist` default false, `#30639` / `#30640`.
2. **Pulse-budget unit: milliseconds with 1 s steps, or seconds with a migration?** Recommend **keep milliseconds** (already shipping). A seconds slider needs a new id or a rewrite of stored values; silently reinterpreting 2500 as seconds is a 42-minute budget.
3. **Is "delta post skip" restart+landing, or residual vs the group after settle?** Recommend **restart+landing**, with group residual on the new log line as a diagnostic.

---

## Constraints that still hold

Live items never seek. No module-global state, no new IPC, no `xbmc.Player.stop()`. `seek_lag_ms` stays an instance attribute on the controller the manager already owns. Docs in `docs/` are one line per paragraph (`tools/unwrap_md.py`). New `strings.po` ids need a full Kodi restart.

---

## References

- `lib/kofin/syncplay/playback.py` — `seek_lag_ms`, `_seek_and_settle`, `_clock_restart`, `correct_position`, `_align_after_resume`, `_skip_live_seek`, `stop_loop`
- `lib/kofin/syncplay/tempo.py` — `SEEK_LAG_DEFAULT_MS`, `BUDGET_DEFAULT_MS`, `PULSE_MAX_S`, `PulseScheduler._gain` / `reset` / `source_offset_ms`
- `lib/kofin/syncplay/manager.py` — `PlaybackController` constructed once (~73), `_leave_locally` (~859)
- `tests/unit/test_syncplay_tempo_wiring.py` — existing seek-lag tests
- `tests/live/syncplay_fine_sync.py` — `inject()`
- jellyfin/jellyfin#13301, #10856, #11723; `MediaSourceManager.GetRecordingStreamMediaSources` hardcodes `IgnoreDts = true`
