# Set default streams — feasibility report

Date: 2026-08-16
Scope: Can Kofin offer "Set default streams" on the Jellyfin actions context menu — a persistent default audio and/or subtitle track for an item, a season or a whole show, gated on `honourJellyfinDefaultTracks`, labelled by what the item actually offers?
Sources: Jellyfin server at tag `v10.11.11` (`MediaSourceManager.cs`, `SessionManager.cs`, `UserDataManager.cs`, `PlaystateController.cs`, read directly), the household server (10.11.11, 1773 movies / 88 series) for every bench probe in §2 and §3, and Kofin's own play/context/streams code.

---

## 1. Verdict

**Feasible.** The server-side mechanism is real and a client can drive it without playing anything — proved on the bench in §2.3. But two findings move the design away from the obvious implementation:

- The only writer of Jellyfin's remembered track is the **playback-reporting path**, one item per report, and each report takes over the session's `NowPlayingItem`. A whole show means one report per episode (271 of them for *King of the Hill* on this server), including while something else is playing.
- **Stream indices are not stable across episodes of a show** (§3). A show-wide rule cannot be an index at all; it has to be a descriptor resolved per episode.

| Requirement | Outcome |
|---|---|
| Set a default audio/subtitle track that survives to the next play | ✅ two mechanisms, §5 |
| Offer it only when there are 2+ streams to choose between | ✅ free for Movie/Episode — the fetch `manage()` already makes carries `MediaStreams` (§4) |
| Label the entry by what is on offer | ✅ the menu is built in Python; `streams.menu_offer` already computes the audio/subtitle/both token |
| Set it for a whole season or show | ⚠️ only via a per-episode descriptor match; index-based is wrong (§3) |
| Only visible when `honourJellyfinDefaultTracks` is on | ✅ one `settings.get_bool` in `_manage_options` |
| No new server prerequisite | ✅ for paths A and B; path C needs the companion plugin |

## 2. What the server actually does

### 2.1 There is no API for this

`UpdateUserItemDataDto` — the body of `POST /UserItems/{itemId}/UserData` — carries eleven fields, and neither stream index is among them: `IsFavorite`, `ItemId`, `Key`, `LastPlayedDate`, `Likes`, `PlayCount`, `PlaybackPositionTicks`, `Played`, `PlayedPercentage`, `Rating`, `UnplayedItemCount`. The `GET` side returns the same shape. Confirmed against the live server's own `/api-docs/openapi.json`, so this is not a spec-versus-server divergence.

The `UserItemData.AudioStreamIndex` / `SubtitleStreamIndex` columns exist, and have exactly **one** writer in the whole server: `SessionManager.UpdatePlaybackSettings` (`SessionManager.cs:964`), reached only from `OnPlaybackProgress` (`:938`), reached only from the public progress handler (`:871`) — i.e. from `POST /Sessions/Playing/Progress`.

### 2.2 Who reads it

`MediaSourceManager.SetDefaultAudioAndSubtitleStreamIndices` (`MediaSourceManager.cs:454`) resolves every MediaSource's `DefaultAudioStreamIndex` / `DefaultSubtitleStreamIndex`, and the remembered value is consulted **first**:

- Audio (`:426`): used when `userData.AudioStreamIndex.HasValue && user.RememberAudioSelections && item.EnableRememberingTrackSelections`, and only if the index still matches a live audio stream. It also stamps `DefaultAudioIndexSource = AudioIndexSource.User`.
- Subtitle (`:393`): same three conditions **plus `user.SubtitleMode != SubtitlePlaybackMode.None`**. An index of `-1` is explicitly allowed and means "no subtitle".

`EnableRememberingTrackSelections` is `virtual … => true` on `BaseItem` (`BaseItem.cs:730`) with no override in the video types, so it is true for everything Kofin plays.

That resolution runs for the item detail endpoint too, not just PlaybackInfo — which is what makes the write observable (§2.3) and makes the picker cheap (§4).

### 2.3 Writing it without playing — bench proof

Item: *Africa Addio* (`8fcc332d…`), untouched — `Played=false`, `PlayCount=0`, position 0 — with two audio streams, index 1 `ita` and index 2 `deu`. Runtime 138.4 min. Server thresholds `MinResumePct=5`, `MaxResumePct=90`, `MinResumeDurationSeconds=300`. The account has `RememberAudioSelections=true`, `RememberSubtitleSelections=true`, `SubtitleMode=Smart`.

| step | request | result |
|---|---|---|
| baseline | `GET /Items/{id}?userId=` | `DefaultAudioStreamIndex=1`, `DefaultSubtitleStreamIndex=3` |
| write | `POST /Sessions/Playing/Progress` `{ItemId, AudioStreamIndex: 2, PositionTicks: 0}` — no playback of any kind | 204; item now reports `DefaultAudioStreamIndex=2` |
| userdata drift | — | **none**: `Played`, `PlayCount`, `PlaybackPositionTicks`, `LastPlayedDate` all unchanged |
| session | `GET /Sessions` | the probe session's `NowPlayingItem` is now *Africa Addio* |
| clear now-playing | `POST /Sessions/Playing/Stopped` `{PositionTicks: 0}` | 204; `NowPlayingItem=None`, remembered index still 2 |
| revert | progress report with **no** `AudioStreamIndex` | back to `DefaultAudioStreamIndex=1` |

Second run, with a resume point planted at 30:00 (21.7% — inside the 5–90 window) via `POST /UserItems/{id}/UserData`, then a progress report echoing that exact `PositionTicks` while setting `AudioStreamIndex=2` and `SubtitleStreamIndex=-1`: both indices took, and `PlaybackPositionTicks` came back **unchanged**. Everything planted was then cleared; the item is as it was found.

So: a client can set, change and clear Jellyfin's remembered audio and subtitle track for an item it is not playing, in one request, with no collateral userdata damage.

### 2.4 The position field is load-bearing, and not in the obvious direction

`OnPlaybackProgress` (`SessionManager.cs:938`) computes whether to persist like this:

```csharp
var changed = false;
if (positionTicks.HasValue) { _userDataManager.UpdatePlayState(item, data, positionTicks.Value); changed = true; }
var tracksChanged = UpdatePlaybackSettings(user, info, data);
if (!tracksChanged) { changed = true; }
if (changed) { _userDataManager.SaveUserData(...); }
```

The `!tracksChanged` is inverted with respect to what it plainly wants to say: a report that changes **only** the track indices, with no `PositionTicks`, leaves `changed` false and never calls `SaveUserData`.

On the bench that write still *appeared* to take — a position-less report with `AudioStreamIndex=2` flipped the item's reported default to 2 immediately. That is because `UserDataManager.GetUserData` hands back a shared cached instance (`_cache.GetOrAdd`, `UserDataManager.cs:239`), so the mutation is visible to every subsequent read while never reaching the database. It is a **cache-only write that does not survive a server restart**, and it reads as a success in every way a client can observe.

The rule that falls out: **always send `PositionTicks`, echoing the item's stored `PlaybackPositionTicks`.** That forces the save, and echoing rather than zeroing preserves the resume point — a stored position is by construction already inside the 5–90% window, so `UpdatePlayState` (`UserDataManager.cs:296`) passes it straight through.

Never omit `PositionTicks` on a **stop** report, though: there `positionTicks ?? runtimeTicks` is not reached at all and the null branch does `PlayCount++; Played = true; PlaybackPositionTicks = 0` (`SessionManager.cs:1115`). Source-read, not bench-tested — there was no safe way to test it that was not also the damage.

### 2.5 What a write costs

- **The session's `NowPlayingItem` is taken over** for the duration. `PlaystateController` fills `SessionId` from the request's own auth context (`PlaystateController.cs:220`), and the plugin process shares Kofin's `deviceId`, so a "set default" issued while something is playing would momentarily report the wrong item as playing on that device — visible on the dashboard and to any other client, and only corrected by Kofin's next 10-second progress report.
- A `PlaybackProgress` event fires: webhooks and server plugins see it.
- `session.StartAutomaticProgress` is armed, so the phantom now-playing keeps ticking until a stop report arrives. Those automated ticks pass `isAutomated=true` and so write no userdata, but the session stays "playing" until cleared.
- `POST /Sessions/Playing` (start) is **not** needed and must be avoided: `OnPlaybackStart` (`:814`) does `PlayCount++`, `LastPlayedDate = now` and `Played = false` unconditionally.

### 2.6 A subtitle default is silently inert for some accounts

`SetDefaultSubtitleStreamIndex` refuses the remembered value when `SubtitleMode == None` (`MediaSourceManager.cs:397`). An account set that way can be given a subtitle default that the server will never hand back. The Account tab already edits `SubtitleMode` (`plugin/userprefs.py:75-86`), so the menu is in a position to say so rather than appear to work — the same move `row_labels` already makes for `honourJellyfinDefaultTracks`.

## 3. The finding that decides the design

Stream indices are not stable across episodes of a show. Sampling this library, one `/Shows/{id}/Episodes?Fields=MediaStreams` per show:

```
Masters of Science Fiction    6 eps   (1,'rus'),(2,'eng')  AND  (0,'eng'),(1,'rus')
Firefly                      15 eps   5 distinct index/language signatures
Mad Men                      90 eps   audio track counts of 1, 2 and 3
King of the Hill            271 eps   3 signatures; some episodes have no second track at all
```

*Masters of Science Fiction* is the whole argument in one row: "index 1" means Russian on one episode and English on another. Writing an index show-wide does not produce a consistent result, it produces a randomly-wrong one.

Language alone does not rescue it either — the common multi-audio case here is `eng` + `eng`, a feature track plus a commentary track. A show-wide rule therefore has to store a **descriptor** and score candidates per episode: language first, then `DisplayTitle`, then codec/channels, then ordinal-within-language, and **no confident match means leave that episode on the server's own default**. Kofin already carries exactly this vocabulary — `Language`, `DisplayTitle`, `Codec`, `IsDefault`, `IsForced` are in `core/streams.py:52`.

This applies to *any* implementation. It is not a consequence of storing the rule locally; it is a property of the library.

## 4. What Kofin already has for free

`GET /Items/{id}?userId=` returns `MediaSources[].MediaStreams` **and** the resolved `DefaultAudioStreamIndex` / `DefaultSubtitleStreamIndex` — verified live, with no `Fields` request. `context.manage()` (`plugin/context.py:479`) already makes that call before building the menu, so for a Movie or an Episode the stream count, the current default and the picker rows cost **zero extra round trips**. That is the same shape as the `SpecialFeatureCount` gate on Browse extras (`:408`), and the reasoning in `_manage_options`' docstring — a `<visible>` condition can only ask Kodi things, but this menu is built from an item we already fetched — applies unchanged.

`plugin/play.py` fetches the item at `:503`, *before* `api.playback_info` at `:555`, and that call already accepts `audio_index` / `subtitle_index` (`:553`) — the parameters the in-playback stream menu's restart uses. The item DTO also carries the `MediaSources[].Id` those indices must travel with (the comment at `play.py:548` records that an index without a source id is silently ignored). So an override has a proven path in, on the first PlaybackInfo, with no extra request.

`streams.menu_offer` (`core/streams.py:408`) already reduces a stream list to `audio` / `subtitle` / `both` / none, which is precisely the "name of menu adjusts as appropriate" requirement, and `streams.label_for` (`:426`) already renders a row.

`sync/db.py` creates `boxset_state`, `download` and `pending_userdata` with plain `CREATE TABLE IF NOT EXISTS` (`:173`, `:184`, `:211`), so a side table for stored rules has an established precedent.

## 5. The three paths

**A — write it server-side through progress reports.** Verified in §2.3. Persists on the server, so every client sees it, and for a single Movie or Episode it is one request. Against it: a season or show is one report per episode, each taking over the session's now-playing (§2.5), each needing that episode's streams resolved first; 271 reports for one show on this library. Disqualified as the mechanism for the season/show bullet.

**B — store the rule in `kofin.db` and apply it at play time.** ⭐ A `stream_default` table keyed by Jellyfin id + scope (item / season / series), holding the descriptor from §3 rather than an index. Resolution happens in `plugin/play.py` between the item fetch and the PlaybackInfo call, feeding the existing `audio_index` / `subtitle_index` / `mediasourceid` parameters. One row per rule whatever its reach; no server writes; works offline; and setting a rule on a show needs only **one representative episode** to build the picker, because the per-episode matching happens lazily at playback. Against it: the rule is Kofin-local and invisible to the web UI.

**C — an endpoint on the KofinSyncQueue companion plugin.** `IUserDataManager` reaches those columns directly, and a show-wide write recurses server-side at no per-episode request cost. But the companion is optional by design — `changefeed.py` tiers down to the official KodiSyncQueue and then to nothing — so this cannot be the only path. Worth keeping as an enhancement that makes a Kofin rule visible to other clients.

**Recommendation: B.** A falls out of it for free: the moment an episode actually plays on the chosen track, Kofin's ordinary progress report (`service/player.py:1772-1773` already sends both indices) teaches the server that episode's default, with no extra code and no phantom session.

## 6. Sketch of B

- `sync/db.py`: `stream_default(jellyfin_id TEXT, scope TEXT, kind TEXT, descriptor TEXT, PRIMARY KEY(jellyfin_id, kind))` — `scope` in `item`/`season`/`series`, `kind` in `audio`/`subtitle`, `descriptor` a JSON blob of the §3 fields plus an explicit `none` sentinel for "no subtitle" (which maps to index `-1`).
- `plugin/context.py`: one entry in `_manage_options`, gated on `honourJellyfinDefaultTracks`, on the item Type being a video type, and on there being something to choose — audio needs 2+ tracks, subtitles need 1+ because "off" is the other option, exactly as `menu_offer` already decides. Labelled from the same token.
- A new `plugin/` module for the picker: scope prompt when the item is an Episode, then an audio list and/or a subtitle list built with `streams.label_for`, marking the current effective default, with a "clear" row.
- Series/Season entry: one `/Shows/{id}/Episodes?Limit=1&Fields=MediaStreams` to get a representative episode, made **when the entry is chosen** rather than when the menu opens — see §7.
- `plugin/play.py`: resolve the most specific matching rule (item → season → series), score it against this item's streams, and pass the winning indices into `api.playback_info`. No match, no override.
- Tests: `test_context.py` for the gating and labelling, a new suite for descriptor scoring against the real signatures in §3 (*Masters of Science Fiction* is the regression case), `test_play.py` for the override reaching `playback_info`.

## 7. Open decisions

1. **Series/Season entry gating.** The manage fetch returns no streams for a folder. Either probe on every context-menu open for a show — measured 0.02–0.26 s for up to 271 episodes against this server on the LAN, materially worse on a TV box against a remote one — or show the entry unconditionally for shows and probe when it is clicked, accepting that it can open on a show with nothing to choose. The second is recommended; it keeps the menu's open cost at exactly what it is today.
2. **Whether the stored rule honours `honourJellyfinDefaultTracks` on both paths.** Today that setting gates only `player.apply_default_tracks` (`service/player.py:1137`); `play.py` bakes the server's `DefaultAudioStreamIndex` into a **transcode** regardless, because the server encodes the track it chose. Gating the menu on the setting is right; gating the *override* needs a deliberate answer, or the setting will mean two different things on the two play paths.
3. **Scope UI shape.** Three rows (this episode / this season / whole show) on an Episode, versus a flat "set for the show".
4. **Translation cost.** Roughly 6–10 new ids × 28 files through `tools/i18n/`, including a caveat string for §2.6 if that warning is wanted.

## 8. What this does not cover

- Music. `Audio` items resolve `DefaultAudioStreamIndex` to the first audio stream unconditionally (`MediaSourceManager.cs:468-474`), so there is nothing to choose and the entry should not appear on song/album/artist rows.
- Multi-version items. A rule stored against an item applies to whichever MediaSource the play resolves; whether a rule should be per-version is unanswered and probably not worth answering until someone asks.
- Whether a remembered index survives a server-side rescan that renumbers streams. The server validates the index against the live stream list before using it (`:432` audio, `:403` subtitle) and falls back to its own default, so the failure mode is "the rule quietly stops applying", not a wrong track. With B the descriptor re-resolves instead, which is strictly better.

## 9. See also

- `docs/transcode-stream-selection-plan.md` §2.9 and §3.2 — where `honourJellyfinDefaultTracks` came from, and the layering it belongs to
- `lib/kofin/plugin/userprefs.py` — the account-level preferences this feature sits underneath
- `kodi-drive:jellyfin-client` — the client-side Jellyfin knowledge this report draws on; §2.4's cache-only write is a candidate for it
