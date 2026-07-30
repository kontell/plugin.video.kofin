# Music Playlist Syncing for Kofin

| Field | Value |
|---|---|
| **Date** | 2026-07-29 (revised 2026-07-30) |
| **Status** | Shipped (PR24); path form and refresh cadence revised 2026-07-30 |
| **Addon** | `plugin.video.kofin` |

---

## Overview

Kofin already puts Jellyfin songs into MyMusic. Music playlists on the server never show up under Kodi's **Music → Playlists**.

**What we do:** download each Jellyfin music playlist, write a basic playlist file into Kodi's music playlists folder, and rewrite every track line to the same path Kodi already uses for that song in MyMusic.

That is the whole feature. One-way only (Jellyfin → Kodi). Local edits in Kodi's playlist editor get overwritten next time we refresh.

---

## What "native" means

Kodi music playlists are **files**, not MyMusic tables (confirmed: MyMusic83/84 have no playlist tables).

Write under a **managed subfolder** so ownership is the directory, not a filename prefix:

```
special://profile/playlists/music/Kofin/<Server Playlist Name>.m3u8
```

Kodi's Music → Playlists UI shows folders; the user opens **Kofin** and sees playlists under their **server names**. Party Mode / skins that browse the tree still work. Folder name is fixed: `Kofin`.

**Subfolders confirmed** (Omega, 2026-07-30): `special://musicplaylists/` lists `Kofin` as a directory beside "Party mode playlist" and "New playlist…", the folder opens, and a playlist inside it browses and plays. So the folder stays the ownership boundary — no `Kofin | <name>.m3u` filename prefix is needed, and the server names stay unprefixed.

**Do not rename for branding.** Keep the Jellyfin display name as the file stem. Only strip characters the filesystem cannot store (`/`, `\`, null, etc.).

Same idea as video smart playlists in `views.py` (files under `playlists/…`), but ordered track lists in a subfolder, not flat `kofin*.xsp` rules.

---

## Algorithm

```
1. Ensure special://profile/playlists/music/Kofin/ exists
2. List user-visible Jellyfin playlists with MediaType=Audio
3. For each playlist:
   a. GET /Playlists/{id}/Items  (ordered; not parent /Items)
   b. For each Audio item:
        map Jellyfin id → Kodi song via kofin.db
        resolve playable path = MyMusic path.strPath + song.strFileName
        (same values already written by Music writer / get_song_path_filename)
        if unmapped: skip the line
   c. Write Kofin/<ServerName>.m3u8
4. Remove files in Kofin/ that are not in the current server set
   (optional: keep a small id→filename map so a server rename
    deletes the old file instead of leaving a stale name)
```

Example line (default install, `musicTranscode=false`):

```
#EXTINF:213,04. Artist - Track Title
http://jellyfin:8096/Audio/{id}/stream.mp3?static=true
```

With `musicTranscode=true`, same lines as MyMusic plugin paths.

**Do not invent a second path format.** Read what is already in MyMusic for that song.

---

## Why the line is a MyMusic path and not `musicdb://`

The path *is* the library link, and it is what Kodi itself writes. Verified live (Omega, 2026-07-30) by queueing a library album and using Kodi's own Music playlist → Options → **Save**: the file it wrote carries the same `plugin://…/stream.flac?mode=play&id=…&dbid=…` rows kofin writes, one per song, not `musicdb://` URLs.

That path is enough for Kodi to recognise the song. Playing a kofin playlist line reports `"type": "song"` with the Kodi database id over JSON-RPC, the browse list shows `NN. Artist - Title` with the real duration, the song info dialog shows artwork, genre, play count and last played, and kofin's own claim path reports the play to Jellyfin.

`musicdb://songs/<idSong><ext>` — the URL that looks like the library link — cannot be used:

| Where | Result |
|---|---|
| Browsing an m3u of `musicdb://` lines | Works; full library metadata |
| Playing one, `musicTranscode=true` | **Fails**: `Init: Error opening file musicdb://songs/9469.mp3`, from the GUI and from `Player.Open` alike |

`CMusicDatabaseFile` translates the id back to the song's stored path and re-opens it *at the file layer*, where a `plugin://` path has nothing to resolve it — the DynPath that makes `musicdb://` work inside the library UI cannot be expressed in an m3u file. Direct rows (plain http) would open, but a line form that breaks whenever `musicTranscode` is on is not a line form.

---

## When to run

Keep triggers dumb:

| When | What |
|---|---|
| Full music sync finishes | Refresh all managed playlists |
| Setting `syncMusicPlaylists` turned on | Refresh all |
| Setting turned off | Delete the whole `Kofin/` playlist folder |
| Manual "Update" / repair of music | Refresh all |
| Every `PLAYLIST_POLL_SECONDS` (15 min), and on the library thread's first tick | Refresh all |

**v1 does not need** a dirty set, UpdateWorker branch, or change-feed type. Playlist counts are small; re-writing every managed `.m3u8` after a music full sync is fine.

**There is no websocket trigger to have.** Verified live against 10.11: creating a playlist and adding a track to an existing one produce *no* websocket message at all — no `LibraryChanged`, nothing. `Playlist` is also in `downloader.NON_CONTENT_TYPES`, so the change feed never carries one either. Before the poll, a playlist edited on the server stayed invisible in Kodi until someone ran a full sync.

So the poll is the trigger (`Library.poll_music_playlists`). It costs one request plus one per playlist, holds off while a sync cycle is in flight or the client is offline, and **writes nothing that has not changed** — an untouched playlist keeps its mtime, so skins that sort by date and Kodi's directory cache are left alone.

---

## Naming & bookkeeping

| Concern | Approach |
|---|---|
| Display name | **Server name** as file stem (user-visible) |
| Ownership / cleanup | Everything under `playlists/music/Kofin/` is ours; never touch sibling files/folders |
| Illegal path chars | Minimal sanitize only (`/` `\`, control chars → `_` or strip) |
| Two playlists, same name | Rare; disambiguate only on collision (`Name (2).m3u8` or short id suffix) — do not mangle every name with a GUID |
| Server rename | Track `playlistId → filename` lightly so the old file is deleted; without that, both old and new names can linger until a full prune of unknowns |

Optional: `sync.json` / small table `{ playlistId → filename, checksum }` for rename + skip-if-unchanged. First PR can rewrite the whole folder every time.

No per-track mapping rows. Songs already have Audio→song mappings.

---

## Settings

- `syncMusicPlaylists` (bool, default **false** until we like it)
- Help text: local edits under Music → Playlists are overwritten on refresh

Gate: only meaningful when at least one music library is synced.

---

## Two-way sync

**Not doing it.**

| Idea | Why not |
|---|---|
| Watch local `.m3u8` edits and push to Jellyfin | No reliable Kodi callback; path reverse-map is brittle; echo loops |
| Bulk "sync back" from Kodi editor | Same problems |

**Optional later (separate, tiny):** context menu "Add to Jellyfin playlist" that POSTs an item id and then rewrites the local file. That is not two-way file sync; it is an explicit action.

---

## Out of scope

- Video playlists
- Smart / Instant Mix playlists
- Writing into MyMusic schema
- KofinSyncQueue changes (nice-to-have later, not required)

---

## Implementation sketch

One module, ~one PR for the happy path:

| Piece | Where |
|---|---|
| List + get playlist items | `lib/kofin/core/api.py` |
| Resolve song path from mapping + music DB | small helper next to music writer / materializer |
| Write / prune `kofin-pl-*.m3u8` | e.g. `lib/kofin/sync/playlists.py` |
| Hook after music full sync | `full_sync.py` (after `music()` / end of run when music ran) |
| Setting | `settings.xml` + strings |
| Tests | sanitize name, path rewrite from fake map, skip unmapped, prune orphans |

```python
# conceptual
def refresh_music_playlists(server, jellyfindb, musicdb):
    root = playlists_music_dir() / "Kofin"
    root.mkdir(parents=True, exist_ok=True)
    want = set()
    for pl in server.audio_playlists():
        lines = []
        for item in server.playlist_items(pl["Id"]):
            path = song_path_for_jellyfin_id(jellyfindb, musicdb, item["Id"])
            if path:
                lines.append(m3u_line(item, path))
        name = safe_filename(pl["Name"]) + ".m3u8"  # server name
        write_m3u8(root, name, lines)
        want.add(name)
    prune_except(root, want)  # only inside Kofin/
```


---

## Key decisions

| Decision | Choice |
|---|---|
| Direction | One-way Jellyfin → Kodi |
| Format | Basic `.m3u8` under `playlists/music/Kofin/` |
| Names | **Keep server names**; folder is the ownership boundary |
| Paths | Reuse MyMusic path+filename for each mapped Audio |
| Refresh model | Full rewrite of managed folder (simple) |
| Two-way | No |
| Default | Setting off until smoke-tested |

---

## PR plan (3 small steps)

### PR 1 — Download + write + setting

- API helpers for audio playlists + ordered items
- `playlists.py`: build m3u, rewrite paths, write/prune under `Kofin/`
- `syncMusicPlaylists` setting (default off)
- Unit tests with tmp dir + fake id→path map + name sanitize
- Manual/dev hook or call from full sync when setting on

### PR 2 — Wire into full sync + enable/disable cleanup

- After music library sync (or end of full sync if music present), call refresh
- Disable setting → remove `playlists/music/Kofin/`
- Live check: enable → Music → Playlists → Kofin shows server names and plays

### PR 3 (optional) — Live updates

- ~~On websocket playlist create/update/delete, refresh one or all~~ — no such event exists (see "When to run"); done instead as a 15-minute poll that skips unchanged files
- `#EXTINF` states the real duration and Kodi's own `NN. Artist - Title` label, from the same MyMusic row as the path
- Paging ends on a short page rather than trusting `TotalRecordCount`, with a hard stop for a server that keeps re-emitting pages
- A partial playlist logs how many of its tracks are not in the Kodi library instead of dropping them silently

---

## Risks (short)

| Risk | Mitigation |
|---|---|
| Song not in MyMusic yet | Skip line; next full refresh after music sync fills it |
| Path mode change (`musicTranscode`) | Full refresh rewrites paths from current MyMusic rows |
| Duplicate server names | Disambiguate only on collision |
| Server rename leaves stale file | id→filename map, or wipe-and-rewrite the folder each refresh |
| User edits a managed file | Document overwrite; only touch `Kofin/` |

---

## References

- Song path construction: `lib/kofin/sync/writers/music.py` `get_song_path_filename`
- What a playlist line reads out of MyMusic: `kodidb/music.py` `get_song_playlist_row`
- Video playlist files (prior art for folder writes): `lib/kofin/sync/views.py`, `kodisetup.py`
- Playlist ignored today: `downloader.NON_CONTENT_TYPES` includes `"Playlist"`
- Kodi basic playlists: https://kodi.wiki/view/Basic_playlists
- Jellyfin ordered items: `GET /Playlists/{id}/Items`
