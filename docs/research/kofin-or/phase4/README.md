# Phase 4 — the Piers video catalogue through public Kodi APIs

Implemented 2026-10-08 on `feat/or-phase4-video` against `kofin-or`, verified on the P1D Piers Flatpak (22.0 beta2, `20260831-e513e0ff-dirty`) in the `OR` profile with the installed Jellyfin 12.2.0 server. The internal API build now synchronizes selected movie, show, music-video and mixed libraries, with collections, into Kodi's native video library. Release version `0.91.0`.

## Foundation

`store.py` keeps each payload once in the shared `api_item` table and membership as intervals in `api_entry` (`added`/`removed` generations), so a scanner listing at the pinned generation is a WHERE clause and a publish writes one row per changed item. `api_native` maps every kind to its Kodi id with a small applied summary — the keys Kofin owns, the userdata it last wrote, a payload hash — instead of a second copy of the payload. Closed intervals are collected once no retained generation can reach them, a pending removal keeps its last placement until applied, and the database runs with incremental auto-vacuum. On the P1D, `kofin.db` went from 235 MB (1,792 movies, 45 snapshot generations of 38.7 MB each) to 72 MB holding 6,616 items.

`paths.py` gives each selected library one scanner root per content type — `plugin://plugin.video.kofin/native/<namespace>/<library>/{movies,tvshows,musicvideos}/` — with shows as folders under the `tvshows/` root and episodes filed under their show. `RemoveContentForPath` on the library root removes one library in one call. A 0.90.0 profile is retired automatically on first start: its rows are removed through the same call on the old namespace directory, the old tables are dropped, the file is vacuumed and the selected libraries re-import.

Enumeration is a daily or Update-library pass; between passes the websocket events and the shared change-feed contract (`sync/changefeed.py`, KofinSyncQueue on this server) carry the changes, with a `MinDateLastSaved` catch-up when no companion is installed. A library selected while no worker was running is enumerated at the next tick.

## Native operations

`native.py` reads back by scope — one library's movies, one library's shows, one show's episodes or seasons — and confirms a refresh by the id it produces. Independent detail patches travel 25 to a JSON-RPC array, and only when the scanner's row differs from the desired state: the serializer and the setter policy are one function (`metadata.details`), so a fresh import needs no patch at all. Batching changes little per row — Kodi's own write and the widget refresh behind each setter dominate — but it keeps the pass's bookkeeping in step. Acknowledgements and userdata expectations commit by the batch.

Shows carry their seasons' names and plots through `addSeason`, their episodes' sort numbering through the InfoTag and a `hash` property Kodi skips an unchanged show by. A show-level change with no setter (cast, a season plot) is `RefreshTVShow(refreshepisodes)`, which deletes and re-creates every episode: the episodes' local watched marks and positions are captured as local edits first, then every episode and season of the show is put back to pending and re-mapped in the same pass. Episode cast and stream changes use `RefreshEpisode`.

Collections are filed at import through the tag's set and their title, plot and art patched through `SetMovieSetDetails`; a movie in several collections takes one, chosen stably by name. A collection's membership change invalidates the movies it affects.

## What Kodi decided

Five findings fixed the design during the day, each read from the e513e0ff source and confirmed on the P1D:

- **A plugin folder needs its own binding.** `URIUtils::GetParentPath` takes a plugin path's parent to be the plugin root, so a show folder never finds the `tvshows/` root's binding and the scanner lists every show and imports none ("No (new) information was found"). Every show is bound with `containssingleitem` before the root scan, as the phase 0 probe did, and unbound when it is removed.
- **The API manifest must declare every content type.** The package rewrite kept only `movies`; Kodi then refused the plugin for TV shows outright (`IsMediaLibraryScanningAllowed`).
- **A show's `dateadded` is derived.** `tvshowcounts` takes the newest episode file's date; `SetTVShowDetails` cannot move it, and 68 of 79 shows stayed pending on it until it left the show's desired state.
- **An empty season is invisible.** `season_view` joins episodes, so `GetSeasons` never returns a season with none; fifteen such seasons and nine unnumbered specials — season 0 with no number, which `ProcessItemByVideoInfoTag` cannot file — are applied without a native row and never trigger a scan.
- **Timestamps are local.** Kodi stores and shows `lastplayed` and `dateadded` in local time and writes its own that way; the server's UTC text read an hour off and differed from every value a playback set.

## Measurements

All on the installed catalogue (1,788 movies in the Movies library, 79 shows / 4,325 importable episodes / 351 seasons in Shows, 4 shows / 15 episodes in Documentaries, 54 collections), from the Flatpak's log.

| Step | Wall time | Notes |
|---|---:|---|
| Retire the 0.90.0 layout (1,788 movies) | 55 s | one `RemoveContentForPath`, VACUUM 235 → 25 MB |
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

"Zero patches" is read from the passes' cost, not from announcements — this Kodi build logs none: the apply passes after the imports took 36 s for 1,842 and 4,751 items, while the one pass that did write, the repair, took 11 minutes for 4,316 rows. `native.flush` now logs the rows of every batch it sends.

GUI round trips sampled every three seconds from another host stayed between 180 and 400 ms during the Movies import and reached 465 ms at most (median 233 ms, 90th percentile 339 ms) during the repair's patch pass; the transport's own floor is about 150 ms. Nothing stalled, so the pass is not paced.

## Verification

Unit: the backend's fake Kodi (`tests/unit/apikodi.py`) imports from the pinned listing per scanner directory and keeps the rules above — a show folder imports only through its own binding, `clearmode: "remove"` deletes everything under a path, a refresh replaces the row and its id, `GetSeasons` hides an empty season, `Clean` drops empty sets. The lifecycle suite covers every kind, the one-call removal of one of two libraries of the same type, the show refresh with captured local edits, collections, batching, the change-feed catch-up with and without a companion, websocket events and the first-content reload per kind. black, ruff, mypy and the full suite pass.

Live, on the P1D: a native episode played from the library through the resolver (`DirectStream`), a seek and stop wrote the resume point to Kodi and the server (869 s both sides) and the server's `UserDataChanged` echo left no local edit behind; an episode marked played on the server reached its Kodi row within a second; the in-progress rule returned exactly the episodes and movies with a server position (1 and 5); dynamic browsing listed the root and the Shows library beside the native rows. Screenshots are under `tests/live/results/or-phase4/` (gitignored).

## Known limits

Episode favourites (S14), season plots after import (S18), empty seasons (S17) and empty or shared collections (S23) remain gaps Kodi's public API cannot express; the Kofin views cover them. Music videos are unit-tested only — the server has no music-video library. Kodi merges two shows with one title and premiere across libraries into one native show; a whole-library clear therefore re-reads the other libraries' shows and re-imports a merged victim rather than preventing the merge. The Kofin version chooser named in the plan's work list is not part of this change.
