# Dynamic libraries: gap analysis against JellyCon and improvement plan

Written 2026-08-20 from six observations about kofin's dynamic listings, read against JellyCon (`jellyfin/jellycon` HEAD `47e1c5c`, 2026-08-17), Kodi's own code (`ref/kodi-omega-full`, 21.x), and a live session on the `kofin-test` profile (Kodi 21.3 Omega, Jellyfin 10.11.11, Contuary). Screenshots, the driving scripts and the raw numbers are in `tests/live/results/dynamic-libraries-2026-08-20/`.

"Dynamic library" here means everything kofin lists live through `plugin://` rather than through Kodi's database: the unsynced libraries on the add-on root, the node menus under them (`plugin/browse.py NODES`), Continue watching, Next up, Search, Extras, Recordings, and every drill-down under those. The plan below touches only `plugin/`, `core/api.py`, `addon.xml` and the settings — nothing inside the transplant.

## How a dynamic library is reached, and why the window matters

Contuary does not consume the `Kofin.nodes.*` window properties the service publishes for unsynced libraries (`Includes_KofinGenerated.xml` ships three empty stubs), so a dynamic library is reached through the add-on root: Videos → Add-ons → Kofin, Music → Add-ons → Kofin, a favourite or a skin shortcut, and Contuary's search dialog, which opens kofin in the **video** window (`Custom_1107_SearchDialog.xml:46`). Two of the six observations turn on which window the listing is in, because Kodi — not the add-on — decides what a click and a context menu do for plugin rows, and it decides differently per window.

One fact about kofin shapes all of the music behaviour below: `addon.xml` declares `<provides>video audio</provides>`, and `CGUIViewStateFromItems` (`xbmc/view/GUIViewState.cpp:574-586`) sets the listing's playlist to music for `Provides(AUDIO)` and then overrides it with video for `Provides(VIDEO)`. Every kofin listing therefore queues onto Kodi's *video* playlist, in the music window too (verified live: the queued album showed up as `playlistid 1`, player `playerid 1`, type `audio`). Playback itself is unaffected — PAPlayer plays the items and `core/kodirpc.py` takes whichever player is active — so this is cosmetic, but it is why a "Play album" kofin builds itself should name `xbmc.PLAYLIST_MUSIC` explicitly.

## Findings

### 1. "Reset resume position" on a Continue watching row does nothing that lasts

The entry is Kodi's own (`CVideoRemoveResumePoint`, string 38209, `xbmc/video/ContextMenus.cpp:76-90`). It is visible because kofin stamps the server position on the row with `setResumePoint` (`plugin/listitems.py:289-296`), and `IsVisible` only asks `GetItemResumeInformation(item).isResumable`. `Execute` queues `CVideoLibraryResetResumePointJob`, whose whole effect is `CVideoDatabase::DeleteResumeBookMark` (`VideoDatabase.cpp:3396-3436`): delete the MyVideos bookmark for the item's file id, or for its *path* when the tag carries none. A dynamic row has no library file id and its path (`plugin://plugin.video.kofin/?mode=play&id=…`) has a `files` row only if Kodi itself saved a bookmark for it on an earlier stop — the kofin-test profile has none. Nothing talks to Jellyfin, and the `VideoLibrary.OnUpdate` announcement that `service/kodiuserdata.py` forwards for library rows is only emitted for library content types. The job's completion then refreshes the container (`VideoLibraryQueue.cpp:238-244`), which re-reads the listing and stamps the server's position straight back — the entry appears to do nothing because, for this row, it did nothing.

kofin's own "Jellyfin actions" menu (`plugin/context.py:_manage_options`) offers watched/unwatched, favourite, extras, downloads, delete and settings — no resume reset, although `Api.set_resume_position` (`core/api.py:698-711`, `POST /UserItems/{id}/UserData {PlaybackPositionTicks: 0}`) already exists and is what the service uses when Kodi resets a *library* row. JellyCon has no reset either; its users get there through "Mark Unwatched" (`lib/functions.py:396-407`), relying on the server zeroing the position along with the played flag.

A second trap waits behind the first. When kofin stops stamping (server position 0), `GetNonFolderItemResumeInformation` (`xbmc/video/VideoUtils.cpp:708-794`) falls back to `GetResumeBookMark(path)` in MyVideos, and Kodi writes such bookmarks for plugin paths on every stop (`xbmc/utils/SaveFileStateJob.cpp:42-61`, keyed on the row's `original_listitem_url`). So a server-side reset alone can leave a stale local bookmark that keeps the row resumable, and the same mechanism can show "resume" on a dynamic row for an item finished on another device. `Files.SetFileDetails` clears a plugin-path bookmark — `CPluginFile::Exists` answers true for any `plugin://` (`PluginFile.cpp:26-29`), and `CVideoLibrary::UpdateResumePoint` calls `ClearBookMarksOfFile` for `position: 0` (`VideoLibrary.cpp:1176-1196`). Verified live: a 100 s bookmark set on `plugin://plugin.video.kofin/?mode=play&id=deadbeef…` over JSON-RPC, visible in `bookmark`, gone after `resume: {position: 0}` (the `files` row stays, as it does after any plugin play).

### 2. No cast in dynamic listings

`_fill_video` already maps `People` to `setCast`, `setDirectors` and `setWriters` (`plugin/listitems.py:298-312`), but no listing asks for it: `BROWSE_FIELDS` (`plugin/browse.py:28-41`) carries no `People`, and only the single-item fetch `api.item()` — used by `manage()` and `play()` — returns people at all. Nothing is broken; the field is withheld, and the `browseCast` setting the rewrite research planned for exactly this (`docs/rewrite-research.md:240`) was never built. The actor objects are also built without thumbnails even when people are present.

The cost, measured on the live server, is linear in items and sits on the server side: about 7–25 ms and ~7 KB per item. Recent movies, 25 rows: 116 → 709 ms and 310 → 485 KB. Next up, 12 episodes: 352 → 431 ms. Continue watching, 8 rows: 116 → 268 ms. The whole movie library, 1,775 rows: 0.65 → 42.7 s and 3.6 → 14.7 MB. A People-only follow-up request by `Ids` is no cheaper (663 ms for the same 25), so the cost cannot be moved off the critical path, only avoided on unbounded listings or paid once and cached.

Re-measured 2026-08-21 across the unbounded movie nodes, which is what decides where the line can sit: favourites (61 rows) 0.10 → 1.55 s, a genre (417) 0.28 → 10.4 s, unwatched (901) 0.39 → 21.9 s, all (1,778) 0.66 → 44.0 s. So boundedness is a crude proxy for size — 61 favourites would be perfectly affordable — but the only alternatives are asking for a count first (a round trip on every listing) or truncating a node the viewer asked to see whole. The honest fix for the big nodes is paging (W7), after which every page is bounded; until then they carry no cast and the setting's help says so.

JellyCon asks for `People` only behind `include_people`, default **off** (`lib/utils.py:371-410`, `settings.xml:88`), maps thumbnails to `/Items/{personId}/Images/Primary?tag=…` (`lib/item_functions.py:241-268`), and its cloned skin fetches the info dialog's cast panel lazily from the single-item endpoint — a skin-side trick kofin cannot rely on.

### 3. "Recently added albums" lists AC/DC, Accept and Aerosmith

The node asks for `IncludeItemTypes=MusicAlbum&SortBy=DateCreated&SortOrder=Descending&Limit=25` (`plugin/browse.py:422-428`). On the live server the 25 answers are AC/DC, Accept and Aerosmith albums all stamped `DateCreated=2026-08-17T08:20:36` (one at 08:31:21): a rescan re-created those album rows in folder order, and that is what an album's `DateCreated` is in Jellyfin — the time the album entity was last created, not when music arrived. Across the library, 1,538 albums share 768 distinct timestamps, with blocks of 47–65 albums per second. The additions the viewer means (Trainspotting, 7 August) are what `/Items/Latest?userId=…&ParentId=<view>&IncludeItemTypes=Audio&GroupItems=true&Limit=25` returns: the server sorts *songs* by `DateCreated` and hands back their albums, which is the query the web client and JellyCon (`lib/menu_functions.py:838-844`) both use. `SortBy=DateLastContentAdded` is no alternative — albums report `DateLastMediaAdded` as `0001-01-01`. The `Latest` answer carries everything `_fill_music` reads (the `Fields` list is honoured, `UserData` and `ImageTags` are present) and comes back as a bare list rather than `{Items: […]}`.

One caveat to state rather than hide: `Latest` honours the account's `HidePlayedInLatest` preference, which is on for this account (0 played among the first 200), and the API offers no "both" value — omit the parameter and the preference applies, `IsPlayed=true` or `false` selects one side. That is what the web client shows for the same user, so accepting it is parity; a client-side alternative (sort songs by `DateCreated`, group by `AlbumId`, fetch the albums by `Ids`) costs two requests and is only worth building if someone asks for played albums back.

The synced path has the same root: `sync/writers/music.py` takes `DateAdded` from the DTO's `DateCreated`, and Kodi's own Recently added albums node orders by `dateAdded`. That is transplant territory and outside this plan, but worth a look on the same evidence.

### 4. A track played from an album plays alone — in the video window

Kodi decides this in `CGUIMediaWindow::OnClick` (`xbmc/windows/GUIMediaWindow.cpp:1157-1182`): for a listing from a plugin that `Provides(AUDIO)`, it asks the *current window's* view state whether to auto-queue. The music window answers `musicplayer.autoplaynextitem && !musicplayer.queuebydefault` (`xbmc/music/GUIViewStateMusic.cpp:36-41`) — true by default — and `OnPlayAndQueueMedia` queues the whole listing from the clicked row. The video window answers through `AutoPlayNextVideoItem` (`GUIViewState.cpp:492-498`), which for content `songs` consults the "Uncategorised" entry of `videoplayer.autoplaynextitem`, off by default, so it plays the one item. Verified live on the Trainspotting album: video window → a 1-item playlist; music window → all 14 queued; video window with "Play next video automatically → Uncategorised" ticked → all 14 queued.

So the observation is true wherever kofin is opened from Videos → Add-ons, a video-window shortcut or Contuary's search, and false from Music → Add-ons. Kodi does offer "Play from here" on song rows in the video window (screenshot `shot-…-110018.png`), which is the native workaround today. JellyCon is no better here: its items are `IsPlayable=false` and play one song on Kodi's video playlist (`lib/play_utils.py:519-530`); what it adds is an explicit Play All / Shuffle / Instant Mix in its own menu.

### 5. No option to play an album

Kodi never offers Play or Queue on a plugin *folder*: the Play, Queue item and Play next context items are gated on `VIDEO_UTILS::IsItemPlayable` / `MUSIC_UTILS::IsItemPlayable`, and a plugin folder matches none of their true branches — the plugin branch wants a playable non-empty media item, and the folder branch is `m_bIsFolder && !IsPlugin()` (`VideoUtils.cpp:608-628`, `MusicUtils.cpp:934-955`); the music window's own `PlayItem` likewise only builds a playlist for `!IsPlugin()` folders (`GUIWindowMusicBase.cpp:617-651`). The album row's menu, in both windows, is Information / Add to favourites / Jellyfin actions / Play with transcoding (screenshots `…-110008.png`, `…-110027.png`), and kofin's own menu adds no play entry. JellyCon's "Play All" on Season, MusicArtist, MusicAlbum, Playlist and MusicGenre (`lib/functions.py:346-350`) is the thing to match; note it orders album tracks by `SortName` rather than track number (`lib/play_utils.py:298-327`), which kofin should not copy.

### 6. "Play with transcoding" offered on music

`addon.xml:38` shows the item for any row with a `kofin.id` property, and `listitems.build` stamps that on every dynamic row — songs, albums, artists, genres, series, seasons, sets included. Picked on a song it runs `play_with_transcode` → `mode=play&transcode=1`, which hands `force_transcode` to the device profile and asks the server for a music transcode the user never wanted. Live, `ListItem.DBTYPE` on dynamic rows reads `song`, `album`, `movie`, `tvshow` and `''` (genre), so the condition can be narrowed without a new property: movie, episode and musicvideo by DBTYPE (which already covers library rows), plus `kofin.id` together with DBTYPE `video` for trailers, recordings and home videos.

## Feature inventory against JellyCon

| Area | kofin dynamic listings | JellyCon | Gap |
|---|---|---|---|
| Continue watching | `/UserItems/Resume`, server order, video only | `/Users/{id}/Items/Resume`, unbounded | none |
| Next up | `/Shows/NextUp` node + `nextepisodes` widget mode | `/Shows/NextUp` node + widget, merges in-progress episodes first | none worth closing |
| Recently added video | `/Items` by `DateCreated`, 25 | same, plus "hide watched" option | minor |
| Recently added music | albums by album `DateCreated` — wrong (§3) | `/Items/Latest` grouped | **fix** |
| Favourites / unwatched / in progress / random | all media types | movies, TV, collections | kofin ahead |
| Genres, years, tags, A–Z | movies, shows, music videos, music | genres all; years/decades/tags/pages movies only | kofin ahead |
| Search | movies, shows, episodes, albums, songs, people → filmography | same kinds, 16 results each | none |
| Collections, playlists, photos, home videos, recordings | browse | browse (+ Live TV, which is pvr.kofin's) | none |
| Cast in listings | mapped but never requested (§2) | opt-in `include_people`, thumbnails | **add, opt-in** |
| Codec / HDR flags | bounded listings carry `MediaStreams` | all listings | none |
| Resume | native prompt via `setResumePoint`, offset-adjusted | own dialog + seek loop, silent by default | kofin ahead |
| Reset resume | Kodi's entry is a no-op (§1); no own entry | none | **add** |
| Watched / favourite / delete | Jellyfin actions, gated on server fields | own menu, delete gated on `CanDelete` | none |
| Play all / shuffle / instant mix | none (§5) | Play All, Shuffle, Instant Mix | **add** |
| Song click queues the album | music window yes, video window no (§4) | never | Kodi setting + §5 |
| Force transcode | on every dynamic row (§6) | video types only | **fix** |
| Multi-version play | first `MediaSource` | select dialog when several | optional |
| Go to show / season from an episode row | none | "Go To Series/Season" | optional |
| Trailers | library rows only | `setTrailer` on dynamic movies/series | optional |
| Extras | node + context entry, series/seasons | movies only | kofin ahead |
| Play next / skip intro | service-side, all plays | service-side | none |
| Listing cache / paging | none; whole-library nodes are one request | pickle cache with background refresh; optional paging | see follow-ups |
| Context menu mechanism | Kodi context items, declared in `addon.xml` | a 100 ms poller that closes Kodi's menu and opens its own | not to copy |
| Default view memory | Kodi's | own per-content override | not needed |

## Plan

Ordered by value over cost. Every item is plugin-side, adds no IPC message and no module state, and keeps the plugin process short-lived; labels reuse Kodi core strings wherever one exists so the 26 generated locales are untouched.

### W1 — Hide "Play with transcoding" from music and containers (small)

Change `addon.xml` `context_play.py` visibility to `[String.IsEqual(ListItem.DBTYPE,movie) | String.IsEqual(ListItem.DBTYPE,episode) | String.IsEqual(ListItem.DBTYPE,musicvideo) | [!String.IsEmpty(ListItem.Property(kofin.id)) + String.IsEqual(ListItem.DBTYPE,video)]] + !String.IsEmpty(Window(Home).Property(kofin.context.bitrates))`. Dynamic movie and episode rows already carry those DBTYPEs through `setMediaType`, so `_focused_item_id` keeps finding `kofin.id` first; library rows are unchanged; songs, albums, artists, genres, series, seasons and sets lose an entry that could only misfire. Square brackets for grouping — parentheses fail the whole expression (`addon.xml` comment).

Test: none unit-testable beyond `test_translations`; live check the entry on a song, an album, a genre, a series, a dynamic movie, a library movie and a recording (needs a full add-on reload for the manifest to be re-read).

### W2 — A working "Reset resume position" in Jellyfin actions (small)

In `_manage_options`, when `dynamic` and `UserData.PlaybackPositionTicks > 0`, add `xbmc.getLocalizedString(38209)` → `{"mode": "resetresume", "id": …, "path": listitems.path_for(item)}`, placed right after the watched toggle. New `actions.reset_resume`: `api.set_resume_position(id, 0)`, then `Files.SetFileDetails {file: path, media: "video", resume: {position: 0}}` through `xbmc.executeJSONRPC` so Kodi's own stale bookmark for the same plugin path cannot resurrect the row, then `Container.Refresh`. Offline, park it the way `kodiuserdata._park` does (`downloads.pending.enqueue(jellyfin_id, media, position_ticks=0)`) and still clear the local bookmark. Register the route in `router.ROUTES` outside `LISTING_MODES`.

Only dynamic rows get the entry, like the watched toggle: on a library row Kodi's own entry works and is forwarded by the service, and a second one with the same name would only ask the viewer to tell them apart. Synced items reached through Continue watching count as dynamic (no dbid travels with them), and the server-side zero reaches their library row through the normal userdata path.

Optional hardening, to be checked live before adoption: stamp `setResumePoint(0, total)` on listing rows whose server position is 0. `GetCurrentResumeTimeAndPartNumber` then answers "set, zero", which is not resumable and — more to the point — skips the MyVideos fallback, so a dynamic row for an item finished elsewhere stops advertising a stale local bookmark. The `listitems.build` comment about never stamping zero concerns the *resolved* item in `play.py`, which stays as it is; the listing path needs its own verification that the resume prompt stays away and `ListItem.IsResumable` reads false.

Tests: `test_context.py` (entry appears only with a position and only on dynamic rows; label from the core table), `test_plugin_actions.py` (server call, JSON-RPC body, refresh; offline parking), `test_router.py` (route registered in the non-listing set). Live: a Continue watching episode → reset → server `PlaybackPositionTicks` 0 and the row no longer resumable after refresh; the same with a pre-planted local bookmark on the plugin path.

### W3 — Recently added albums through `Latest` (small)

Add `Api.latest(parent_id, include_types, fields, limit)` → `GET /Items/Latest` with `userId`, `ParentId`, `IncludeItemTypes=Audio`, `GroupItems=true`, `Limit`, `Fields`, `ImageTypeLimit=1` (the 10.9+ route form, verified on 10.11.11), returning the bare list. Route `recentalbums` through it in `_list_items` as a special case ahead of `node_query`, content stays `albums`; drop the `recentalbums` branch from `node_query` and update `test_node_query_music_albums` accordingly. Document the `HidePlayedInLatest` semantics in the route's docstring rather than working around them; the "Recently played" and "Top 100" nodes are unaffected (they list songs and already sort by play data).

Tests: `test_browse.py` (the node calls `latest` with those parameters, the list shape is accepted, 25 rows, `albums` content). Live: the node lists what the web client's Music → Recently added shows, in the same order.

### W4 — Play all / Shuffle for music containers (medium)

Jellyfin actions entries on `MusicAlbum`, `MusicArtist`, audio `Playlist` and `MusicGenre` — music only, by decision; seasons and series keep Kodi's own behaviour: "Play all" (core 22083) and "Shuffle" (core 191) → `mode=playall&id=…[&shuffle=1]`. The route expands the container server-side with the sort the container deserves — album: `ParentIndexNumber,IndexNumber,SortName`; artist: `ArtistIds` + `ProductionYear,Album,ParentIndexNumber,IndexNumber`; playlist: server order via `/Playlists/{id}/Items`; genre: `GenreIds` — `SortBy=Random` for shuffle, a hard cap (500, logged when hit), `Fields` trimmed to what `listitems.build` needs. It then builds `xbmc.PlayList(xbmc.PLAYLIST_MUSIC)`, `clear()`, `add(path_for(item), build(item))` per row, `context._stop_current_playback()` if something is playing (the bare-`plugin://`-while-playing race, documented there), and `xbmc.Player().play(playlist)`. Each entry resolves through the existing `mode=play` route, so reporting, segments and downloads-first all apply per track exactly as they do for Kodi's own "Play from here" — which is the path the live music-window experiment already exercised for 14 items.

Song rows need nothing: Kodi offers Play, Queue item, Play next and (video window) Play from here, and the music window queues on click. "Instant mix" (`/Items/{id}/InstantMix`) is a natural third entry but has no core string, so it costs a translation round; defer unless wanted. SyncPlay: a viewer inside a group should probably be refused or handed to the group queue rather than start a local playlist — decide when implementing, the manager owns that rule.

Tests: `test_context.py` (entries per type), a new `test_playall.py` (expansion query per type, order, cap, playlist type, stop-before-play, shuffle flag), `test_router.py`. Live: album → Play all → 14-item music playlist in track order, per-track sessions on the dashboard; Shuffle → different order.

### W5 — Cast in bounded listings, opt-in (medium)

A `browseCast` bool on the Advanced tab, default off, with help text that states the measured cost (about 0.6 s and 175 KB on a 25-row movie listing). When on, `People` joins `BROWSE_FIELDS_STREAMS` — the field set that already marks a listing as bounded — and the special routes that are bounded by construction (Next up, Continue watching, a season's episodes); whole-library nodes, genre/year/tag/alphabet legs, search (a hundred rows is two and a half seconds of People) and music never ask for it, whatever the setting says. `_fill_video` gains the thumbnail: `xbmc.Actor(name, role, index, server + "/Items/{PersonId}/Images/Primary?tag=" + PrimaryImageTag)` when the tag is present, which Kodi fetches only when the info dialog draws it. The `api.item()` DTO already carries people, so `manage()` and `play()` are unchanged.

A later step, only if the 0.6 s matters on a TV box: a small `(item id, Etag) → People` cache in its own SQLite file under addon_data, filled by one `Ids=` request for the misses; `Etag` is one cheap field away and moves when people change. Not in this round — it is a second store to keep honest for a feature that is off by default.

Tests: `test_browse.py` (field choice follows setting and boundedness), `test_listitems.py` (actor thumbnails, index order). Live: Continue watching episode → Information → cast with photos; listing open time with and without the setting on the Bravia.

### W6 — Open music libraries in the music window (dropped)

Decided against on 2026-08-20: the wiki will carry the two Kodi-side answers instead. For the record, the idea was that the root row for an unsynced music library could stop being a folder and become an action row that runs `ActivateWindow(Music,<browse url>,return)`, so the library lands where Kodi's click queues the album and the music context entries live. It trades a normal folder descent (history, "..") for a window hop, which is why it is optional rather than assumed; the alternative is to document the two Kodi-side answers — open kofin from Music → Add-ons, or tick "Play next video automatically → Uncategorised" — in the README. The window properties already publish `ActivateWindow(Music,…)` for synced music (`sync/views.py window_node`), so the shape has precedent.

### W7 — Parity extras, in value order (optional)

"Go to show" / "Go to season" on episode rows in Continue watching, Next up and search (`ActivateWindow(Videos,<kofin browse path>,return)` from Jellyfin actions; one new string each unless a core label fits). `setTrailer` on dynamic movies with `LocalTrailerCount`/`RemoteTrailers` in the field list, resolving through a `mode=trailer` route. A "Versions" submenu for items with several `MediaSources`, reusing `mediasourceid` on the play route. Paging for the "All" nodes of very large libraries, which today are one request (3.5 MB and 0.47 s for 1,775 movies, fine on a LAN, less so on a stick).

## Status (2026-08-20)

W1–W5 are implemented and verified live on the kofin-test profile (Kodi 21.3, Jellyfin 10.11.11); the evidence is in `tests/live/results/dynamic-libraries-2026-08-20/after/`, and the gates are S9.1–S9.6 in `docs/testing-plan.md`. W1: `addon.xml` gates the transcoding item on DBTYPE. W2: `mode=resetresume` (`plugin/actions.py`) zeroes the server position, then clears Kodi's plugin-path bookmark through `kodirpc.clear_resume_bookmark`; listing rows with no server position now stamp `setResumePoint(0, total)` (`plugin/listitems.py`), which is what made the stale-bookmark case read "not resumable" live. W3: `recentalbums` goes through `Api.latest`. W4: `plugin/playall.py` behind `mode=playall`, offered on music containers only. W5: `browseCast` (Advanced tab, off by default, strings 30819-30820) adds `People` to `browse.bounded_fields()`; actor portraits ride as URLs. W6 dropped; W7 untouched.

**Follow-up, 2026-08-21.** A viewer reported cast under Recently added and not under All. That is the boundary working as designed (see the re-measured costs in §2), but two listings that *are* bounded had been left on the plain field list — search results and a person's filmography, both `SEARCH_LIMIT` — so a film's cast appeared in Recently added and vanished from a search for the same film. Both now take `bounded_fields()`. The help text (#30820) was rewording to name the listings that carry cast rather than promising it for "items browsed through the add-on"; the four node names in it are #30031, #30033, #30032 and #30049, so a translator renders them as those ids are rendered. Extras remain the one bounded listing without cast: `/Items/{id}/SpecialFeatures` takes no `Fields`.

## Deliberately not proposed

Copying JellyCon's listing cache: kofin's answer to a slow listing is to sync the library, and a pickle cache with background refresh is a second source of truth that shows stale watched flags until it catches up. Dropping `video` from `<provides>` to put kofin's audio on the music playlist: the addon is a video plugin first and the effect is cosmetic. Overriding Kodi's context menu with a poller, as JellyCon does, to remove Kodi's no-op reset entry: a context item cannot be hidden by an add-on and the poller is the kind of service work kofin's shell exists to avoid.

## Decisions (2026-08-20)

`browseCast` defaults off. "Recently added albums" follows the web client, `HidePlayedInLatest` included. W6 is dropped in favour of a wiki note. Play all / Shuffle are offered on music containers only, never on video.
