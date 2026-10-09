# Phase 4 — the Piers video catalogue through public Kodi APIs

Implemented 2026-10-08 on `feat/or-phase4-video` against `kofin-or`, verified on the P1D Piers Flatpak (22.0 beta2, `20260831-e513e0ff-dirty`) in the `OR` profile with the installed Jellyfin 12.2.0 server. The internal API build now synchronizes selected movie, show, music-video and mixed libraries, with collections, into Kodi's native video library. Release version `0.91.0`.

## Foundation

`store.py` keeps each payload once in the shared `api_item` table and membership as intervals in `api_entry` (`added`/`removed` generations), so a scanner listing at the pinned generation is a WHERE clause and a publish writes one row per changed item. `api_native` maps every kind to its Kodi id with a small applied summary — the keys Kofin owns, the userdata it last wrote, a payload hash — instead of a second copy of the payload. Closed intervals are collected once no retained generation can reach them, a pending removal keeps its last placement until applied, and the database runs with incremental auto-vacuum. On the P1D, `kofin.db` went from 235 MB (1,792 movies, 45 snapshot generations of 38.7 MB each) to 72 MB holding 6,616 items.

`paths.py` gives each selected library one scanner root per content type — `plugin://plugin.video.kofin/native/<namespace>/<library>/{movies,tvshows,musicvideos}/` — with shows as folders under the `tvshows/` root and episodes filed under their show. `RemoveContentForPath` on the library root removes one library in one call.

Enumeration is a daily or Update-library pass; between passes the websocket events and the shared change-feed contract (`sync/changefeed.py`, KofinSyncQueue on this server) carry the changes, with a `MinDateLastSaved` catch-up when no companion is installed. A library selected while no worker was running is enumerated at the next tick.

## Native operations

`native.py` runs the pass; `readback.py` reads back by scope — one library's movies, one library's shows, one show's episodes or seasons — and confirms a refresh by the id it produces. Independent detail patches travel 25 to a JSON-RPC array, and only when the scanner's row differs from the desired state: the serializer and the setter policy are one function (`metadata.details`), so a fresh import needs no patch at all. Batching changes little per row — Kodi's own write and the widget refresh behind each setter dominate — but it keeps the pass's bookkeeping in step. Acknowledgements and userdata expectations commit by the batch.

Shows carry their seasons' names and plots through `addSeason`, their episodes' sort numbering through the InfoTag and a `hash` property Kodi skips an unchanged show by. A show-level change with no setter (cast, a season plot) is `RefreshTVShow(refreshepisodes)`, which deletes and re-creates every episode: the episodes' local watched marks and positions are captured as local edits first, then every episode and season of the show is put back to pending and re-mapped in the same pass. Episode cast and stream changes use `RefreshEpisode`.

Collections are filed at import through the tag's set and their title, plot and art patched through `SetMovieSetDetails`; a movie in several collections takes one, chosen stably by name. A collection's membership change invalidates the movies it affects.

After review the reconciler was split along its seams: `kinds.py` is the kind table (methods, properties and how each kind is removed), `readback.py` the scoped readback cache, `removal.py` the table-driven removal, `patch.py` the apply pass, `identity.py` the playback and userdata lookups, and `native.py` the gates, bindings, scans and the pass itself. The review also found four acknowledgements made ahead of their confirmation — a whole-library clear that raised on a member with no mapping, episodes forgotten before their show's removal was confirmed, collections without a Kodi row failing every pass, and a removed collection forgotten with its set still filed — each fixed with a regression test, plus a refresh poll that re-listed its scope every 100 ms and a selected-but-vanished library that re-enumerated on every tick.

## What Kodi decided

Five findings fixed the design during the day, each read from the e513e0ff source and confirmed on the P1D:

- **A plugin folder needs its own binding.** `URIUtils::GetParentPath` takes a plugin path's parent to be the plugin root, so a show folder never finds the `tvshows/` root's binding and the scanner lists every show and imports none ("No (new) information was found"). Every show is bound with `containssingleitem` before the root scan, as the phase 0 probe did, and unbound when it is removed.
- **The API manifest must declare every content type.** The package rewrite kept only `movies`; Kodi then refused the plugin for TV shows outright (`IsMediaLibraryScanningAllowed`).
- **A show's `dateadded` is derived.** `tvshowcounts` takes the newest episode file's date; `SetTVShowDetails` cannot move it, and 68 of 79 shows stayed pending on it until it left the show's desired state.
- **An empty season is invisible.** `season_view` joins episodes, so `GetSeasons` never returns a season with none; fifteen such seasons and nine unnumbered specials — season 0 with no number, which `ProcessItemByVideoInfoTag` cannot file — are applied without a native row and never trigger a scan.
- **Timestamps are local.** Kodi stores and shows `lastplayed` and `dateadded` in local time and writes its own that way; the server's UTC text read an hour off and differed from every value a playback set.

## Measurements

All on the installed catalogue (1,788 movies in the Movies library, 79 shows / 4,325 episodes of which 4,316 are importable / 351 seasons in Shows, 4 shows / 15 episodes in Documentaries, 54 collections), from the Flatpak's log.

| Step | Wall time | Notes |
|---|---:|---|
| Movies: enumerate and publish | 22 s | 9 pages, 54 collection member listings |
| Movies: scan | 100 s / 132 s | two imports; 56 / 74 ms a movie |
| Movies: apply pass | 36 s before batching, seconds after | zero `SetMovieDetails`; the first pass was 3,685 one-row commits |
| Documentaries: select to Done | 2.5 s | 4 shows, 15 episodes, scan 554 ms |
| Shows: enumerate and publish | 42 s | 1 + 2 + 22 pages |
| Shows: bind 79 folders and scan | 228 s | 4,316 episodes added, about 53 ms an episode |
| Shows: apply pass | 36 s | zero `SetEpisodeDetails`; 79 episode readbacks |
| Deselect Documentaries (one of two show libraries) | about 40 s | one clear, the other library re-read, not re-scanned |
| Deselect Movies (with 54 sets) | 3 min 45 s | one clear; the home screen's set widget refreshed every second behind Kodi's per-row announcements; no set survived |
| Kodi's Clean Library, add-on enabled | 8 s | 6,000 `check_exists` answers through the reused invoker; nothing removed |
| Repair all (complete enumeration, every row compared, 4,316 episodes patched for the local-time change) | 11 min 2 s | enumeration 44 s; patches 25 to an array, about 146 ms an episode with the home screen's TV widgets refreshing 8,860 times behind them |

"Zero patches" is read from the passes' cost, not from announcements — this Kodi build logs none: the apply passes after the imports took 36 s for 1,842 and 4,751 items, while the one pass that did write, the repair, took 11 minutes for 4,316 rows. `patch.Applier.flush` logs the rows of every batch it sends.

GUI round trips sampled every three seconds from another host stayed between 180 and 400 ms during the Movies import and reached 465 ms at most (median 233 ms, 90th percentile 339 ms) during the repair's patch pass; the transport's own floor is about 150 ms. Nothing stalled, so the pass is not paced.

## Verification

Unit: the backend's fake Kodi (`tests/unit/apikodi.py`) imports from the pinned listing per scanner directory and keeps the rules above — a show folder imports only through its own binding, `clearmode: "remove"` deletes everything under a path, a refresh replaces the row and its id, `GetSeasons` hides an empty season, `Clean` drops empty sets. The lifecycle suite covers every kind, the one-call removal of one of two libraries of the same type, the show refresh with captured local edits, collections, batching, the change-feed catch-up with and without a companion, websocket events and the first-content reload per kind. black, ruff, mypy and the full suite pass.

Live, on the P1D: a native episode played from the library through the resolver (`DirectStream`), a seek and stop wrote the resume point to Kodi and the server (869 s both sides) and the server's `UserDataChanged` echo left no local edit behind; an episode marked played on the server reached its Kodi row within a second; the in-progress rule returned exactly the episodes and movies with a server position (1 and 5); dynamic browsing listed the root and the Shows library beside the native rows. Screenshots are under `tests/live/results/or-phase4/` (gitignored).

## On a 1 GB device

Verified on 9 October 2026 on a LibreELEC 13 nightly (Kodi 22.0b2, armv7, 918 MB, no swap) with the Movies library and both show libraries selected together — the configuration the Flatpak runs never had, because its libraries were selected one at a time. Three defects, none visible on the P1D, and the pass that reaches this section's measurements is the one that carries their fixes.

**Two scans issued together cancel each other.** 0.91.0 had imported nothing there in twelve hours: every pass issued one `VideoLibrary.Scan` per root, back to back, and Kodi's `UpdateLibrary` builtin, which the JSON-RPC method executes, stops a running scan when another is requested instead of queuing it (`LibraryBuiltins.cpp`). Each pass logged one `Starting scan … Finished scan` of 17 ms with no listing, then recorded 6,515 failures one database open at a time (an episode pass of 266 s with no Kodi call). `native.scan` now issues one scan and waits for it; the applier records failures by the batch.

**A tag taken from a temporary ListItem dangles.** A full scan started by hand did list and import, at about two items a second (1,287 movies in 12 min, 3,188 episodes in 26 min), until Kodi segfaulted in `InfoTagVideo::setGenres`, and did so again under the fixed scans. `metadata.item_separator` took a video tag from `xbmcgui.ListItem(offscreen=True).getVideoInfoTag()`; the tag is a pointer into an item the interpreter had freed by the next line, and every API-build listing on every platform had written that freed memory. The x86_64 Flatpak never showed it.

**A listing held the library.** With both fixed, the two show roots imported their remaining shows in 216 s and the pass issued the movies scan. Kodi had 540 MB; the movies root listing loaded its 1,788 payloads before building a ListItem and the process grew by 100 MB in a minute (RSS 544 → 624 MB, 150 MB available at the last sample), the listing never finished, and the device stopped answering ssh and the webserver until it was power-cycled twenty minutes later. The pass behind it would have done the same at twice the size: it built a record with its payload for every pending item, 6,616 of them — 58 MB of JSON and 200 MB of Python objects, measured on the P1D's store. Listings and the pass now read payloads on demand through `PayloadWindow` (200 ids a query, at most 512 held) and let each go once its ListItem or patch is built; the tvshows root reduces each episode to its numbering as it reads it.

**An empty date is no opinion.** With the three fixed, the pass planned the whole catalogue and left 187 rows pending for ever: 186 episodes and one show with no premiere date on the server, whose desired `firstaired` or `premiered` was an empty string. Kodi fills a missing date itself — its zero date on the P1D, which reads back empty through JSON, a sibling's date on the box, which does not — and its setters ignore an empty one, so the readback never matched and every pass re-patched them, four minutes a pass on that device. The serializer leaves the key out, as it already did for `dateadded`; the next pass acknowledged all 187 in 7.6 s with no Kodi call.

**What the device measured, with every fix in place.** The two show libraries' remaining 10 shows and 1,143 episodes imported in 216 s; the movies root of 1,788 took 1,630 s to scan (about 1.1 movies a second, where the Flatpak does 13), and the listing that feeds it 14 s; the apply pass then planned 60 seasons in 49 s (all patched), 4,199 episodes in 159 s and 1,788 movies in 58 s with no Kodi call. Kodi's resident set peaked at 569 MB during the movies scan with 300 MB or more available throughout; the GUI round trip from another host averaged 23–33 ms with one 7.3 s stall as the episode patches ended; the low-memory watchdog left on the box never fired and nothing crashed. The progress bar the API build now shows (`backends/api/progress.py`) was read off a screenshot of the device at 78 % of the music enumeration.

## Three lessons from the music pass, applied

The music pass of phase 5 was designed a month after this one and, on the LibreELEC box, outperformed it on every axis that mattered there: a change re-scans one directory, a readback carries four properties, and an unchanged item costs a hash compare. The same day the box verified this pass, three of those were brought across.

**One folder per movie.** The movies root listed the whole library in one directory, so one new movie changed the directory hash and the scanner re-walked all 1,788 (27 minutes on the box, 100–132 s on the P1D) and the listing that fed it was the largest allocation in the plugin process. Every movie now has a folder of its own under the root, bound like a show's with `containssingleitem` (`native.bind_folders`, 25 bindings to a batch); a few new movies are scanned by folder, and a first import or a mostly-missing library walks the root once with `scanrecursive` switched on for the walk only. Kodi lists every sub-folder of a recursive plugin root on every scan of it — a plugin folder never carries the mtime its fast hash wants (`VideoInfoScanner.cpp`, `DoScan`) — so recursion is never left on: a manual Update library against the root lists folders and skips. Kodi's extras convention lives in a movie's own folder, which this layout is also the first step towards. A profile from the one-directory layout needs the Movies library deselected and reselected: the old rows' URLs are not owned, and an import beside them would double the library.

**A minimal scope readback, the full row only for what moved.** The scope readback asked 27 properties of every movie on every pass that had anything pending; it now carries what finds and owns a row and the userdata a viewer may have edited (`kinds.MINIMAL`), paged like the music one. The full row is read by id for an item whose state moved (`Readback.full`), or as one paged listing when at least a tenth of a scope and 25 rows are about to be compared, as after a first import (`Readback.prefetch_full`).

**Acknowledged state is not compared again.** A mapping carries the payload hash and a token of everything else `details` reads — server, library, separator, set name, the seasons' hashes (`metadata.inputs_token`). A pending item whose mapping matches both against the row it found is acknowledged without building or comparing a desired state. It fires for a retried pass and a re-published item; Repair bypasses it on purpose, because the compare is what Repair is for. On the box the first acknowledgement of 4,199 episodes cost 159 s of Python at 33 ms each; that cost is paid once per item now.

**And the artwork.** Kodi decodes and re-encodes every image it caches and caps the result at its own heights (`CTextureCacheJob::CacheTexture`, `CPicture::CacheTexture`), so a 4000×6000 poster is decoded on the device for a 1080-high cache file. The `maxArtResolution` and `compressArt` settings already asked the server to resize and encode first, but only the SQL writers honoured them; the API listings and the dynamic rows do now (`listitems.art_query`), and both default on at 1080. Changing either changes every art URL, so the art re-caches and every row is patched once.

## Known limits

Episode favourites (S14), season plots after import (S18), empty seasons (S17) and empty or shared collections (S23) remain gaps Kodi's public API cannot express; the Kofin views cover them. Music videos are unit-tested only — the server has no music-video library. Kodi merges two shows with one title and premiere across libraries into one native show; a whole-library clear therefore re-reads the other libraries' shows and re-imports a merged victim rather than preventing the merge. The Kofin version chooser named in the plan's work list is not part of this change.
