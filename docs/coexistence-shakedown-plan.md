# Kofin beside a local library: shakedown plan

| Field | Value |
|---|---|
| **Date** | 2026-11-03 |
| **Status** | Rounds 1 to 3 run. Results and findings in section 8; finding 4 open |
| **Started by** | #389, fixed for TV shows in #401 |
| **Rig** | `ref/build/kodi-dev` instance 5, headless, the jf12 test server as `kofin-test` |
| **Builds** | Kodi 23 with the kontell changes (`app-release`, `71c9d22187`) and stock Piers (`app-piers150`, `846211c81d`) |
| **Driver** | `ref/notes/kofin-side-by-side-2026-11-03/coexist.py`, beside `side_by_side.py` and `verify.py` |

---

## 1. Why

Kodi keeps one library per profile and matches some items across sources by identity, not by source. Kofin files its rows through the scanner like any other source, so any item Kodi matches by identity can end up shared between Kofin and a local library of the same media. #389 found that for TV shows, and the rig then showed three faults:
- a local show scanned after Kofin took Kofin's ids off the shared row, and every pass then failed;
- a local show scanned before Kofin left Kofin's first import of it failing;
- removing Kofin's library deleted the local show and its episodes.

#401 fixes those three. This plan checks the rest of what Kodi shares, in both orders, on both builds, ranked by what is at stake for the other source's data.

A scenario passes when all of these hold:
- **The other source keeps its rows.** Its rows and watched states are intact, or back once Kofin has asked Kodi to rescan it, with nothing lost.
- **Kofin keeps its rows.** Kofin still owns its rows, and the next pass compares them as usual.
- **No pass keeps failing.** The library thread never repeats a failure; a failure that repeats is backed off, not looped.
- **No foreign row is written.** Kofin writes nothing to a row only the other source filed.

## 2. What Kodi shares between sources

| Item | Matched by | Seen so far |
|---|---|---|
| TV show | default unique id, then title and premiere date (`CVideoDatabase::GetMatchingTvShow`) | merged into one row, both orders (2026-11-03). Ownership loops fixed in #401 |
| Season | its show and number | follows its show's row |
| Movie | file path | a row of its own on Kodi 23 (2026-11-03). Kodi 23's scan logs "grouping of similar videos is automatic": check that a local copy is never grouped as a version of Kofin's movie (B8) |
| Episode, song, music video | file path | a row of its own (by reading) |
| Album | MusicBrainz album id, else artist and title among albums with no MusicBrainz id (`CMusicDatabase::AddAlbum`) | not checked. Kofin already handles two of its own albums on one row (`MusicPass.album_owner`) |
| Artist | MusicBrainz artist id, else name among artists with no MusicBrainz id (`CMusicDatabase::AddArtist`) | not checked. Kofin finds an artist by name alone |
| Set | name | not checked. `CLAUDE.md` says a set shared by name takes Kofin's art and plot, and holds a collection's removal pending while a movie of theirs lists it |
| Genre, actor, studio, tag | name | shared by design. A Kofin tag on a shared row is visible to both |

## 3. The other sources

Each scenario runs against one of these, added to Kodi as a video or music source (`sources.xml`), as a viewer would add it:

- **S1, local with NFOs** (`metadata.local`). A folder per title, with `tvshow.nfo`, `movie.nfo` and episode NFOs carrying the IMDb id the server gives the title. Short `testsrc2` clips from ffmpeg stand in for the media. This is the #389 rig's source.
- **S2, local and scraped.** The same folders without NFOs, bound to the TMDb scrapers both builds ship (`metadata.tvshows.themoviedb.org.python`, `metadata.themoviedb.org.python`). A scraper writes its own default id and its own art, which is a different merge from an NFO. It needs the rig to reach TMDb.
- **S3, local music.** A copy of the scratch album Kofin files from the server (section 6), with the same tags and MusicBrainz ids, under file names of its own. A second, untagged copy covers the artist-and-title match.

Each scenario runs twice where the order matters: **K-first**, with Kofin's import and then the other source's scan, and **L-first**, the other way round. A scenario's state is read back from Kodi over JSON-RPC and from a read-only copy of `MyVideos`/`MyMusic`, for what JSON-RPC doesn't show: `tvshowlinkpath`, `album_artist`, the `files` row holding a play count.

## 4. Scenarios

### Group A: Kofin's actions deleting the other source's rows

| # | What | Expected | Known |
|---|---|---|---|
| A1 | Kofin's TV library removed, show shared (S1, S2; both orders) | the other source's show back under its own folder with its episodes and watched states, after Kofin's scan of its source | passes on Kodi 23 with #401, S1 K-first |
| A2 | One shared show leaves the server: Kofin removes it by id (`VideoLibrary.RemoveTVShow` → `DeleteTvShow`) | as A1 | **expected to fail**: #401's rescan only follows a whole-library clear |
| A3 | One of Kofin's episodes leaves, show shared | only Kofin's episode goes (`RemoveEpisode` by id) | by reading |
| A4 | A collection leaves while a local movie is in a set of the same name (S1 with `<set>`) | the set stays for the local movie, and Kofin's removal stays pending without looping | `CLAUDE.md` rule, not run |
| A5 | Songs of an album shared with local songs leave (S3) | the local songs and the album row stay, and Kofin's songs go | not checked |
| A6 | Kofin's music library removed, album shared (S3) | as A5, for every album | not checked |
| A7 | Kodi's Clean library with a shared show, a shared album and a local movie beside Kofin's | the other source's rows stay, and so do Kofin's | **differs by build**: Kodi 23 asks the add-on about its folders (kontell/xbmc #1, #2); stock Piers deletes the path row of a bound folder left unanswered. Run on both |
| A8 | The viewer removes the other source (Remove source, content cleared) | Kofin's show, deleted with the shared row, comes back: the pass finds it missing, scans, and its watched states come from the server | by reading: `RemoveContentForPath` on their path reaches the shared row through their link |

### Group B: ownership and loops

| # | What | Expected | Known |
|---|---|---|---|
| B1 | Show, K-first (S1, S2) | Kofin's ids restored by a show-only refresh; passes succeed | passes on Kodi 23 with #401, S1 |
| B2 | Show, L-first (S1, S2) | Kofin's first import passes; Kofin's ids on the shared row | passes on Kodi 23 with #401, S1 |
| B3 | The other source rescanned after a change to its folder (new episode file), K-first | the ids taken off again, and put back by one refresh; no loop | not checked |
| B4 | Album shared by MusicBrainz id (S3; both orders) | the album row owned by Kofin's album or by the other source's, without a pass failing; Kofin's lost-song check counts only Kofin's songs | not checked |
| B5 | Album shared by artist and title, without MusicBrainz ids (S3, untagged): Kodi matches that way only an album filed without an id, so this depends on what Kofin's listing gives the album | as B4, or two rows | not checked |
| B6 | Artist shared by name (S3) | Kofin's art and description on it, the other source's songs unaffected | not checked |
| B7 | Set shared by name (S1 with `<set>`) | one set row, Kofin's art and plot, no pass failing | not checked |
| B8 | A local movie with Kofin's movie's title, year and IMDb id in the same naming (S1, S2), Kodi 23 | a row of its own, never grouped as a version of Kofin's movie | a row of its own with S1, 2026-11-03 |
| B9 | Music video with the same artist and title as Kofin's | a row of its own | by reading: `GetMatchingMusicVideo` has no caller in the scanner |

### Group C: Kofin acting on rows that aren't its own

| # | What | Expected |
|---|---|---|
| C1 | The viewer marks the other source's episode watched, inside a shared show | nothing sent to the server; Kofin's play state for its own episodes unchanged |
| C2 | A Kofin episode played to the end on a shared show | its play count on the server; the other source's episodes unchanged |
| C3 | A download of a Kofin episode on a shared show, then its removal | the badge on the shared row and off again; with "Export NFO files and artwork" on and the download folder a source, the #389 sequence on a real download |
| C4 | Repair with shared rows (show, album, set) | rows compared and patched; nothing of the other source's deleted |
| C5 | A manual Repair that rebuilds a stale TV library with a shared show (`Library.rebuild_stale`) | as A1 |

### Group D: listings

| # | What | Expected |
|---|---|---|
| D1 | Kofin's node tree and smart playlists with a shared show, album and movie | only Kofin's rows (their rules filter on Kofin's folders) |
| D2 | Kodi's own nodes and the home widgets | the movie twice (one row each), the show once with both sources' episodes, the in-progress row correct for both |

## 5. Rounds

1. **Round 1, Kodi 23 (`app-release`):** groups A and B, S1 and S3. That's about fifteen scenarios, with A1, B1 and B2 repeated as a regression check. Each is a fresh profile, a Kofin import of Movies, Shows and a small Music library from jf12, and one change: about 1 to 3 minutes each, around 45 minutes in all, music the slowest. Report before going on.
2. **Round 2, stock Piers (`app-piers150`):** the same scenarios. A7 is the one expected to differ.
3. **Round 3:** S2 on the scenarios that pass with S1, then groups C and D.

Each finding goes to an issue, or straight to a PR where the fix is as small as #401's parts. A scenario whose expected outcome is wrong here gets its row in this plan corrected, not just the result.

## 6. The driver

`coexist.py` grew out of `side_by_side.py` and `verify.py`. Kofin syncs three scratch libraries on jf12 rather than jf12's own: Coexist Shows, Coexist Movies and Coexist Music, made of synthetic clips and tagged tones under jf12's state folder (`coexist.py setup`, `setup-s2`). A title leaves the server by having its file moved aside and its library refreshed, and comes back the same way. Each park uses a folder of its own: moving into one that already exists nests the title a level down, which once cost the show its NFO, and its IMDb id with it. The rest of the driver:
- `fresh(order, sources)`: reset instance 5, write `sources.xml` and the selection, and import, either Kofin first or the other source first;
- one builder per source (S1, S2, S3);
- snapshot readers for each shared kind: rows, linked paths, unique ids, play counts, art owners;
- one function per scenario.

Evidence goes to `evidence/<scenario>-<build>-<order>.json` with the instance's `kodi.log` beside it.

A scenario that changes jf12 (a played mark, a collection, a removed item) undoes it in a `finally` and reads it back. `kofin-test` had one Better Call Saul episode, "Hit and Run", played on 2026-09-01 before any of this, and that is not a leftover. jf12 is usually stopped: the driver starts it and leaves it as it found it, and does the same with instance 5.

## 7. Out of scope

- Another add-on as the other source (a second Jellyfin, Emby or Plex add-on): its rows are plugin URLs like Kofin's, which is a different shape of sharing.
- MySQL-backed libraries.
- Kodi 21 and the SQL build, which writes the databases directly and is frozen.

## 8. Results

Kofin under test: #401 (`e9f24b6`, then `a45d071` with round 1's fixes). Every scenario is a fresh profile on instance 5; evidence is in `ref/notes/kofin-side-by-side-2026-11-03/evidence/coexist-<scenario>-<build>.json`.

### Round 1: Kodi 23 (`app-release`, `71c9d22187`)

| # | Result |
|---|---|
| A1 K-first, L-first | **pass**. Kofin's TV library removed; the other source's show back under its own folder with its episode, scanned from its source after the clear |
| A2 | **failed, fixed in `a45d071`**. The shared show leaving the server alone was removed by id (`RemoveTVShow`), taking the local show and its episode, and nothing scanned their source. The removal now scans it, as the library clear does. Rerun: pass |
| A3 | **pass**. Only Kofin's episode went; the local one stayed in the show |
| A4, B7 K-first, L-first | **B7 pass**: one set row for Kofin's two movies and the local one. **A4 failed, fixed in `a45d071`**: with the collection gone from the server, its removal stayed pending for good ("collection still has a member filed"), because the local movie never leaves the set, and it failed 133 passes in five minutes (finding 2). It now ends once no movie of Kofin's files the set. Rerun: pass, the local movie keeps the set |
| A5 | **pass**. A song leaving the server went; the local songs and the album row stayed |
| A6 | **pass**. Kofin's music library removed; the local album, its songs and the artist stayed |
| A7 | **pass**. Kodi's Clean library removed nothing of either source's |
| A8 | **partial, open**. The viewer's removal of their source deleted the shared row, Kofin's show with it, and Kofin noticed only at its next complete listing: Update library, or the scheduled full enumeration, which is off by default (`fullUpdateInterval`) (finding 4) |
| B1, B2 | **pass** (in A1's first half). The stripped row got its ids back from one show-only refresh; the L-first import filed Kofin's show on the local row |
| B3 | **pass**. A new episode file in the other source's folder, rescanned: Kodi added the episode to the show it holds without rewriting the show, so Kofin's ids stayed |
| B4 L-first, B6 | **pass**. One album row shared by MusicBrainz id, both sources' songs on it, and one artist row; repairs clean |
| B5 | **pass**. Without MusicBrainz ids the local album is a row of its own; the artist is shared by name |
| B8 | **pass**. The local movie with Kofin's movie's title, year and IMDb id is a row of its own, never grouped as a version |
| B9 | not run: the scratch server has no music video library |

### Round 2: stock Piers (`app-piers150`, `846211c81d`)

Every scenario passed as on Kodi 23, with `a45d071`, A2 and A4 included. A7 too: Kodi's Clean removed nothing of either source's on stock Piers either, with Kofin running to answer it. The show scenarios (A1, A2, A3, A7, A8, B3) were run again after a driver fault had nested the scratch show's folder and left it without its IMDb id, so that the first run merged nothing.

### Round 3: Kodi 23, S2 and groups C and D

| # | Result |
|---|---|
| S2: A1 K-first, L-first | **pass**. The TMDb scraper's show merged by its TMDb, TVDB and IMDb ids; the local show came back under its own folder after the removal |
| S2: A2 | **pass** |
| S2: B8 K-first, L-first | **failed, fixed in #401**. K-first, the scraped local copy of a movie Kofin has became a version of Kofin's movie, and the row lists the local file. Kofin then no longer recognised its movie, and its passes failed ("scanner did not import Movie"), held to five in two minutes by the backoff (finding 6). Rerun on both builds: the passes succeed, and after the movie library's removal the local movie is back on a row of its own. L-first, Kodi groups nothing: Kofin's import does not go through the grouping, and the two stay apart |
| C1 | **pass**. The local episode marked watched sent nothing to the server |
| C2 | **pass**. Kofin's episode marked watched reached the server; the local episode was untouched |
| C3 | **pass**. The #389 sequence on a real download, with NFO export on: the download folder merged into Kofin's row, and once the download left, the ids came back without a failed pass. The downloaded episode's row stays, as a row of the folder the viewer scanned, until Kodi's Clean |
| C4 | **pass**. Repair with a shared show, album and set left every row of both sources |
| C5 | **pass**. The rebuild a stale scanner format forces cleared the library, scanned the local source again, and imported Kofin's show back onto a shared row |
| D1 | **partial**. Kofin's node tree is written; the titles inside the nodes were not read |
| D2 | **as expected**. Kodi's own nodes list both copies of the movie, one row for the shared show, and the local episode among Kofin's |

### Findings

1. **A shared show leaving alone took the other source's show with it** (A2). Fixed in #401 (`removal.sources_of`, `removal._row`).
2. **A collection held by another source's movie looped** (A4). Its removal never finished, and every pass failed. Fixed in #401 (`removal._ours_in_set`).
3. **A failing repair skipped the backoff** (A4). A complete listing makes the next pass a repair, the flag stays set until one succeeds, and #401's backoff left repairs out. Fixed in #401: a repair backs off like any pass, and a command of the viewer's runs the pass at once.
4. **Kofin doesn't notice Kodi deleting its rows** (A8). When the viewer removes the other source, Kodi deletes the shared row and Kofin's episodes on it, and nothing pending names them. A complete listing repairs it. Watching Kodi's removals of rows Kofin owns would repair it at once. Open.
5. **Kofin gives Kodi no artist MusicBrainz id** (B5, B6). A song's track, album and release-group ids reach Kodi, its artists' don't, so Kofin's artists are matched by name alone, and the id an artist row carries came from the local files. Not a coexistence fault; noted.
6. **A scraped local copy of a movie Kofin has becomes a version of Kofin's movie** (S2 B8). Kodi's "grouping of similar videos" (`videolibrary.similarvideoaction`, automatic by default on both builds) groups a scraped movie with any movie sharing a unique id (`CVideoDatabase::GetSameVideoItems`), and the row then lists the local file as the movie's. Kofin finds a movie only by its own URL (`Readback.owns`), so every pass fails. The NFO source never reached the grouping (B8 with S1 passed). Fixed in #401: a movie missing from the listing is found by Kofin's own file (`Readback.movie`, `Files.GetFileDetails`, which Kodi resolves through `videoversion`), never refreshed while the row lists their file, and removed by its id, since the library clear cannot reach it, with their source scanned after.
