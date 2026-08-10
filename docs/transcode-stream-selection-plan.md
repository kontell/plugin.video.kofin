# Transcoding stream selection — implementation plan

Date: 2026-07-30. Audio and subtitle stream selection for playback that does not direct play, plus honouring Jellyfin's own default tracks on every path.

**Deliverable**: text subtitles are selectable through Kodi's native subtitle menu on a transcode, with no restart and no cost to playback start; audio (and image-based subtitles) are selectable through a kofin dialog reachable during playback, which restarts at the current position; the track a playback *starts* on is the one Jellyfin's user profile nominates, on both direct play and transcode.

Everything in §2 was measured against Jellyfin 10.11.11 (`jelly.konell.xyz`) and Kodi 21.3 (Omega) on 2026-07-30. Section 2 is the evidence the rest of the plan rests on; it is worth reading before §3, because three plausible designs die there.

---

## 1. Scope

**In**:
* Pass `MediaSourceId` to `PlaybackInfo` so `AudioStreamIndex`/`SubtitleStreamIndex` are honoured at all (§2.6).
* Attach every **text** subtitle's `DeliveryUrl` to the resolved ListItem, on the transcode path as well as direct play — native Kodi subtitle selection, zero startup cost.
* Apply Jellyfin's `DefaultAudioStreamIndex` / `DefaultSubtitleStreamIndex` to Kodi at playback start, on both paths, behind a setting.
* A kofin stream-selection dialog for what Kodi cannot do natively on a transcode: audio, and image-based subtitles. Restarts playback at the current position.
* Its entry point: a context item on the played listing item, reached by pressing Back during playback (§2.7).
* Report progress correctly across a restart so the server's play session stays coherent.

**Out**:
* Changing `SubtitleProfiles`. They already produce the right answer (§2.2) and every change tested made things worse (§2.5).
* Burned-in subtitles as a default. Offered only as the explicit fallback for image subs (§3.4).
* Music. `Audio` items have one stream by construction.
* SyncPlay interaction with restarts — a group member restarting its own stream is out of scope; the dialog is suppressed while a group is active.
* Live TV.

---

## 2. What was measured

### 2.1 A Jellyfin transcode carries exactly one audio track

`master.m3u8` for a forced transcode contains three `EXT-X-STREAM-INF` variants (codec fallbacks) and an `EXT-X-IMAGE-STREAM-INF` trickplay stream. There is **no `EXT-X-MEDIA:TYPE=AUDIO`**, and every variant hardcodes the same `AudioStreamIndex`. Requesting `master.m3u8` with no `AudioStreamIndex` at all still yields a single variant with a single audio codec.

Kodi agrees: playing a kofin transcode of a 2-audio-track film, `Player.GetProperties.audiostreams` returned exactly one entry.

**Native audio switching on a transcode is not possible.** Any audio change is a new stream URL, which is a restart.

### 2.2 Burned-in subtitles are already avoided, and the profile is why

kofin's `SubtitleProfiles` declare eight formats — including `pgssub`, `dvdsub`, `pgs` — each as both `Embed` and `External`. Against a film with 50 embedded subtitle streams (46 of them PGS), every stream came back `DeliveryMethod: External` with a `DeliveryUrl`. Nothing is burned in today.

That is not incidental. Dropping the image formats from the profile flips the same streams to `Encode`:

| SubtitleProfiles | PGS stream idx 4 | text stream idx 41 |
|---|---|---|
| kofin today (8 formats × Embed/External) | `External` | `External` |
| text formats only | **`Encode`** | `External` |

So the answer to "is burn-in necessary for embedded subs" is **no, and kofin already does not use it** — the profile is what decides, and kofin's is right. Burn-in is a capability to keep in reserve for image subs (§3.4), not something to remove.

### 2.3 …but kofin delivers none of them on a transcode today

`play.external_subtitles()` requires `stream["IsExternal"]`, so only sidecar files are attached. Embedded subtitles are excluded even though the server offered a working `DeliveryUrl` for each.

Live, on the 50-subtitle film transcoded through kofin's current profile: Kodi reported **0 subtitle tracks**. This is the actual bug behind "no subtitles when transcoding" — not burn-in, not the profile, just a filter.

### 2.4 Attaching text subtitles works, natively, and costs nothing

Attaching Jellyfin `DeliveryUrl`s via `ListItem.setSubtitles()` on a **transcoded** stream:

* the tracks appear in Kodi's native subtitle menu and switch natively mid-playback;
* they **render** (confirmed on screen);
* `setSubtitles()` itself costs 0.000–0.042 s, and Kodi fetches lazily.

Time from `play()` to a playing picture, same item, same transcode:

| external subtitles attached | time to first frame |
|---|---|
| 0 | 4.01 s |
| 2 | 4.00 s |
| 20 (distinct URLs) | 4.02 s |

**No startup cost at any count.** The `Stream.srt` fetch itself is 108 KB in 2.6 ms server-side.

One defect: Kodi labels them all `Stream (External)` with no language, because Jellyfin's route filename is fixed (`Stream.{format}`; `Stream.eng.srt` → HTTP 400). §3.3 solves the naming without touching the delivery.

### 2.5 HLS subtitle renditions are a dead end (three ways)

Declaring `{"Format": "vtt", "Method": "Hls"}` makes Jellyfin emit `EXT-X-MEDIA:TYPE=SUBTITLES` renditions, and Kodi enumerates them **with correct language tags** and logs `Created subtitles overlay codec: WebVTT Subtitle Decoder`. It looks like the ideal native answer. It is not:

1. **Nothing renders.** Controlled A/B on the same film at the same timestamps: direct stream shows "Oliver Norvell Hardy." and "- Room 14, next to the solarium."; the HLS-rendition transcode shows nothing, across seven sampled frames, with the track selected and `subtitleenabled: true`. The server side is fine — the WebVTT segments contain the cues.
2. **A refetch storm.** ffmpeg downloads *every* 30 s segment of the subtitle rendition to the end of the film on open, and again on **every seek**. ~240 requests per seek on a two-hour film.
3. **It kills direct play.** With vtt/Hls as the only subtitle profile, an item that otherwise direct plays returned `SupportsDirectPlay: False, SupportsDirectStream: False`. Keeping the existing Embed/External entries alongside preserves direct play, but then §2.5.1 still applies.

Also worth recording: Jellyfin only emits the subtitle group when the *selected* subtitle resolves to Hls delivery. Select an image sub, or none, and the group vanishes entirely.

**Do not pursue.** Recheck only if a future Kodi fixes WebVTT-over-HLS rendering.

### 2.6 `MediaSourceId` is mandatory for index selection

`PlaybackInfo` with `AudioStreamIndex=3` and no `MediaSourceId` returned `AudioStreamIndex=2` — the server's own default, silently. Adding `MediaSourceId` made the same request return `AudioStreamIndex=3` and `AudioCodec=ac3`.

`Api.playback_info` already takes `media_source_id`; `plugin/play.py` never passes it. Without this fix every index we send is ignored.

### 2.7 The entry point is the listing, reached with Back — and it works

Two separate facts, and only the second one matters for the design.

**Fullscreen video itself has no context menu.** `Input.ExecuteAction("contextmenu")` while window 12005 is active returns `OK` and does nothing. So the menu cannot be opened *over* the video.

**But that is not the gesture.** Back (or Tab) leaves fullscreen while playback continues, and the context menu on the listing is fully available there. Measured end to end on a synced library movie:

* after `Input.Back`: window is Videos (10025), `fullscreen: false`, player still active at `speed: 1` with the clock advancing;
* the focused ListItem is still the item that was launched (`DBID 1229`, `DBTYPE movie`);
* `contextmenu` opens window 10106 with both kofin entries present ("Jellyfin actions", "Play with transcoding"), and playback keeps running underneath.

Better still, Kodi answers the "is this the item playing" question natively:

| infobool | value in that state |
|---|---|
| `ListItem.IsPlaying` | `true` |
| `String.IsEqual(ListItem.FileNameAndPath,Player.Filenameandpath)` | `true` |

So the in-playback chooser is an ordinary `kodi.context.item` gated on `ListItem.IsPlaying`. **No keymap, no settings button, no opt-in, nothing to discover.** `Player.Filenameandpath` additionally returns the full kofin play URL — Jellyfin `id` and `dbid` included — so the handler can identify the playing item without relying on focus.

### 2.8 Index mapping is ordinal-within-type

Direct-streaming a file whose Jellyfin streams are video idx 0, audio idx 1–2, subtitle idx 3–5, Kodi reported audio `[ac3, aac]` (Kodi 0,1) and subtitles `[English, English SDH, English SDH]` (Kodi 0,1,2). Jellyfin index → Kodi index is the ordinal among streams of that type.

Caveat: sidecar (`IsExternal`) subtitle streams occupy Jellyfin's index space but not the file's, so they must be excluded from the ordinal when mapping embedded subtitles, and they appear *after* the embedded ones in Kodi's list.

### 2.9 Jellyfin already tells us the default tracks

Every `PlaybackInfo` MediaSource carries `DefaultAudioStreamIndex` and `DefaultSubtitleStreamIndex`, resolved from the Jellyfin **user profile's** language and subtitle-mode preferences. Measured: one film answered audio 2 / subtitle 0; another answered audio 1 / subtitle `None` (that user's preferences select no subtitle).

`play.play_state()` already records both — and nothing has ever applied them. Kodi picks its own tracks from its own language settings on direct play, so today the Jellyfin default is ignored on the one path where it could be honoured for free.

### 2.10 The cost of a restart

| step | measured |
|---|---|
| `PlaybackInfo` round trip | 0.04 s |
| `Player.Open` → first frame (transcode spin-up) | 4.3 s |
| seek inside a running transcode | 1.9 s |

An audio switch that re-resolves and restarts at the current position is **~5–6 s to picture**. That is the honest number to put in front of the user; it is the transcode start-up cost, not something the design can shave.

---

## 3. Design

Four layers, each independently shippable, in dependency order.

### 3.1 Layer 0 — make index selection work at all

`plugin/play.py` passes `media_source_id=source["Id"]` to `Api.playback_info`, and gains optional `audioindex` / `subtitleindex` play params which flow through to the same call. Without §2.6 nothing else in this plan functions.

The play route also stashes, on the play-state pushed to `state.push_play_item`:

* the source's `MediaStreams` reduced to what a menu needs — `Index`, `Type`, `Codec`, `Language`, `DisplayTitle`, `IsDefault`, `IsForced`, `IsExternal`, `IsTextSubtitleStream`, `DeliveryUrl`;
* the order in which text-subtitle URLs were handed to `setSubtitles()`, so the dialog can map a Jellyfin index onto a Kodi subtitle ordinal without guessing;
* `DefaultAudioStreamIndex` / `DefaultSubtitleStreamIndex` (already there).

This is data the play route **already holds** from the `PlaybackInfo` it just made. No extra round trip, no extra latency.

### 3.2 Layer 1 — Jellyfin's default tracks (both paths)

New setting `honourJellyfinDefaultTracks` (default on, Playback tab).

The service player, on `onAVStarted` (first frame — the earliest point at which Kodi's stream lists are populated), maps the recorded Jellyfin defaults to Kodi ordinals per §2.8 and applies them with `Player.setAudioStream()` / `setSubtitleStream()` / `showSubtitles(False)`.

* Direct play: both audio and subtitle apply.
* Transcode: audio is already the server's choice (it baked the index into the URL); only the subtitle selection applies.
* `DefaultSubtitleStreamIndex is None` means the user's profile wants none → `showSubtitles(False)`.

This runs after the picture is up, so it cannot delay start. A late switch is visible as a one-frame subtitle flicker at worst.

### 3.3 Layer 2 — text subtitles on a transcode, natively

`play.external_subtitles()` becomes `play.subtitle_urls()`: drop the `IsExternal` requirement, keep `DeliveryMethod == "External"` and `DeliveryUrl`, and add `IsTextSubtitleStream` — which is exactly the set that is free to attach and renders (§2.4), and excludes the image subs that are not (§3.4).

This alone gives native, mid-playback, no-restart subtitle switching on a transcode, at measured zero cost to playback start. It is the single highest-value change in the plan and depends on nothing but itself.

The `Stream (External)` labelling (§2.4) is **not** fixed by renaming or by caching files locally — a local cache would mean N HTTP fetches before `setResolvedUrl`, which is exactly the startup delay the brief forbids. It is fixed by Layer 3's dialog, which lists the tracks under their Jellyfin `DisplayTitle` and calls `setSubtitleStream()` on the corresponding ordinal. Kodi's own menu stays generic; kofin's is correct.

### 3.4 Layer 3 — the stream dialog

**Entry point.** A new `kodi.context.item` in `addon.xml`, alongside the existing three. The user presses Back (or Tab) during playback, lands on the listing with the played item still focused and still playing, and opens the context menu on it (§2.7). Nothing to install, nothing to configure.

Its `<visible>` gates on `ListItem.IsPlaying` plus the same item-identity clause the other entries use:

```xml
<visible>ListItem.IsPlaying + [!String.IsEmpty(ListItem.Property(kofin.id)) | String.IsEqual(ListItem.DBTYPE,movie) | String.IsEqual(ListItem.DBTYPE,episode) | String.IsEqual(ListItem.DBTYPE,musicvideo)]</visible>
```

Square brackets for the grouping, not parentheses — same trap as the "Play with transcoding" entry (comment in `addon.xml`).

The handler resolves the playing item from `state.get_playing_id()` rather than from focus, and treats a mismatch with the focused item as "not the playing item" — `ListItem.IsPlaying` should make that impossible, but the dialog acts on a live playback and must not act on the wrong one.

**Availability.** The dialog's data is the play-state the play route already published at resolve time (§3.1), read from a window property by the plugin process. It is complete **before the first frame renders** — so the answer to "how long after a video starts until the pop-up is available" is *no wait at all*; it is available as soon as playback is. No API call is made when the menu opens.

The one caveat is that the play route publishes it and the service player *claims* it off the queue at `onPlayBackStarted`, so the streams must live somewhere the plugin process can still read after the claim. Simplest: a separate `kofin.playing.streams` property the service writes when it claims and clears in `finalize()`, so its lifetime is exactly the playback's.

**Behaviour.** One entry whose label adapts to what the item offers:

| offer | label |
|---|---|
| >1 audio and selectable subtitles | Audio & subtitle streams |
| >1 audio only | Audio stream |
| subtitles only | Subtitles |
| neither | menu suppressed |

Picking "Audio & subtitle streams" shows the audio list, then the subtitle list. Each list is built from the recorded `MediaStreams`, labelled with `DisplayTitle`, with the current selection marked.

**What each pick does**:

* **Text subtitle** → `setSubtitleStream(ordinal)`. Instant, no restart. This is the common case.
* **Subtitles off** → `showSubtitles(False)`. Instant.
* **Audio** → restart.
* **Image subtitle (PGS/DVDSUB) on a transcode** → restart with that index and a profile that omits the image formats, which makes the server burn it in (§2.2). This is the only place burn-in is used, it is the only way to show an image sub on a transcode, and the dialog says so in the entry's label.
* **Image subtitle on direct play** → `setSubtitleStream(ordinal)`; it is in the file, Kodi renders it, no restart.

**Restart.** Reuse the mechanism `plugin/context.py` already proves: read the current position, then `PlayMedia` a play URL carrying `startticks` plus the new `audioindex`/`subtitleindex`. Kodi's own `resume` flag cannot carry a position on a `plugin://` path (see CLAUDE.md), so the position is stated in the params, exactly as the transcode context item does. Expect ~5–6 s to picture (§2.10); the dialog shows a busy indicator rather than pretending it is instant.

The service player must treat the restart as a continuation, not a new watch: report the stop for the outgoing session and close its transcode (`finalize()` already does both), then let the new play claim normally. The `Segments`/Play-Next engine restarts with it, and `_start_inside` (already implemented) keeps a mid-intro restart from flashing a skip prompt.

**SyncPlay**: suppressed while `syncplay_group_active` — a unilateral restart would desync the group.

---

## 4. Work items

| # | Change | Files | Notes |
|---|---|---|---|
| 1 | Pass `media_source_id`; accept `audioindex`/`subtitleindex` params | `plugin/play.py` | §2.6 — prerequisite for everything |
| 2 | Publish stream metadata on the play-state | `plugin/play.py`, `core/state.py` | argue the property into `state.py` per CLAUDE.md |
| 3 | `external_subtitles` → `subtitle_urls` (text subs on every path) | `plugin/play.py` | Layer 2; standalone value |
| 4 | Apply Jellyfin default tracks at `onAVStarted` | `service/player.py` | + `honourJellyfinDefaultTracks` setting |
| 5 | Jellyfin index ↔ Kodi ordinal mapping | new `core/streams.py` | pure, L1-testable (§2.8) |
| 6 | `mode=streams` dialog | new `plugin/streams.py`, `plugin/router.py` | label adaptation, both lists |
| 7 | Restart path | `plugin/streams.py` | mirrors `context.play_with_transcode` |
| 8 | Context item gated on `ListItem.IsPlaying` + shim | `addon.xml`, new `context_streams.py` | §2.7 — no keymap needed |
| 9 | Publish/clear `kofin.playing.streams` | `core/state.py`, `service/player.py` | written on claim, cleared in `finalize()` |
| 10 | Strings | `resources/language/.../strings.po` | needs a full Kodi restart to appear |

Items 1–4 are a shippable first cut: default tracks honoured everywhere, text subtitles selectable natively on a transcode, no new UI. Items 5–10 add audio and the dialog.

## 5. Testing

**L1** (`tests/unit`): the index mapping in `core/streams.py` — ordinal-within-type, sidecar exclusion, subtitle ordinals after embedded ones, `None` default → subtitles off; the label-adaptation matrix; `subtitle_urls` filtering (text yes, image no, sidecar yes, missing `DeliveryUrl` no).

**Live gates** (add to `docs/testing-plan.md`):

| gate | expectation |
|---|---|
| T1 | Transcode a film with embedded text subs → they appear in Kodi's subtitle menu and render |
| T2 | Time to first frame with subtitles attached is within noise of without (baseline 4.0 s) |
| T3 | Switching text subtitle mid-transcode does not restart playback |
| T4 | Direct play starts on the audio/subtitle track Jellyfin's user profile nominates |
| T5 | Transcode starts on Jellyfin's default subtitle |
| T6 | Back during playback → context menu on the played item shows the new entry (§2.7 proves the mechanics; this proves kofin's entry) |
| T7 | The entry is absent on a focused item that is *not* playing, and on foreign playback |
| T8 | Audio switch restarts within ~6 s and resumes within 1 s of the previous position |
| T9 | Server dashboard shows one continuous watch across a restart, not two |
| T10 | Image subtitle on a transcode burns in and renders |
| T11 | Menu suppressed in a SyncPlay group |
| T12 | An item with one audio and no subtitles offers no menu entry |
| T13 | The entry works from a kofin plugin listing (`kofin.id` items), not just the synced library |

## 6. Risks

* **The gesture is a convention, not a prompt.** Nothing tells the viewer that Back-then-context-menu is where the streams live. It is the same gesture Kodi users already use to reach anything about a playing item, and the entry is visible the moment they get there — but a line in the addon's description or a first-run toast would help.
* **Many text subtitles.** 20 attached URLs cost nothing at start (§2.4), but Kodi's own menu becomes a wall of `Stream (External)`. Consider capping what is attached to the languages the Jellyfin user profile cares about, plus the current default — kofin's dialog remains the readable route regardless.
* **Restart and watch state.** A restart mid-item posts a stop. If the server marks progress oddly, T8 catches it; `finalize()` already sends an explicit `PositionTicks`.
* **Transcode session leak.** Every restart must `close_transcode` the old `PlaySessionId`. `finalize()` does this for `PlayMethod == "Transcode"`; verify it fires on the restart path.
* **Jellyfin behaviour drift.** §2.5's HLS renditions and §2.6's `MediaSourceId` requirement are server-version-dependent. Both are recorded here with the version they were measured against (10.11.11).

## 7. Amendment, 2026-08-10: subtitles a transcode did not attach

§3.4's decision table above is superseded for one of its four rows. The rest stands.

The plan assumed every text subtitle is attached at play time (§3.1), so "text subtitle → `setSubtitleStream`, instant" covered all of them. That assumption did not survive contact: Kodi opens every attached subtitle while *building the demuxer*, not when one is picked, and each embedded track is an on-demand ffmpeg extraction — a film with several the server could not produce stalled the picture for 20 s per track. Attaching was therefore narrowed to the single track the playback resolved with, and the others were left to the restart path alongside image subtitles.

That was wrong in a way the narrowing hid, and it was reported against Das Boot: one internal English SRT, transcoded, nothing attached, and the menu's only row offered as "burned in, restarts playback". Two faults behind it.

**The restart never worked the first time.** Cold extraction of an embedded subtitle was measured on the same server as §2 at **28 s** (2.4 GB MKV), **30 s** (2.6 GB) and **146 s** (22.7 GB); warm it is ~25 ms. The play route allowed 8 s. So the first play attached nothing, the restart re-ran the same 8 s fetch on the new stream and also attached nothing, and only a later attempt succeeded — by then one of the abandoned extractions had finished and been cached. Abandoning the request does not abandon the work, which is what makes waiting worth anything.

**A text track is not a burn-in.** Both restart cases shared one label (#30617) and one code path, so a plain SRT was announced as about to be stamped into the picture, and its restart really did send `burnsubs=1`.

The fix keeps §2's measurements and inverts §3.4's answer for this row:

* The play route no longer waits on an extraction. A sidecar keeps its 8 s (the server already has the file); an embedded track gets 4 s, because it is either warm in milliseconds or an extraction no budget catches. What does not land is **deferred**, not dropped.
* The service chases a deferred track against the *running* playback and hands it over with `Player.setSubtitles(path)` — verified on Omega 21.3: it appends to Kodi's subtitle list, is selected, and renders in sync with no gap (`service/latesubs.py`). Note it is the Player method, not the ListItem one; there is no `addSubtitle` on `xbmc.Player`, whatever `Player.AddSubtitle` over JSON-RPC suggests.
* Picking any text subtitle from the menu therefore costs a download, not a stream. The plugin process states the index over `ipc.ATTACH_SUBTITLE` and exits; the service owns the wait and the playback. **Only audio and image subtitles still restart.**

§2.5's verdict on `SubtitleProfiles` is reaffirmed with a new measurement. `{"Format": "vtt", "Method": "Hls"}` makes 10.11.11 answer `DeliveryMethod: Hls` and emit a real `#EXT-X-MEDIA:TYPE=SUBTITLES` rendition, so the server side works — but Kodi lists the track and then never starts the picture: black screen, `time` stuck at 0:00 with `speed: 1`, for over two minutes. Ruled out. Every JSON-RPC probe reports success, so only a screenshot plus a position poll catches it.

The §6 risk "Many text subtitles" is closed by the same change from the other side: only one track is ever attached, so Kodi's own menu is never a wall of `Stream (External)`, and the rest are named properly in kofin's dialog.
