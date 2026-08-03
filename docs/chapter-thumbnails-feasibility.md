# Chapter thumbnails — feasibility report

Date: 2026-08-03 (supersedes the 2026-07-29 draft; see §8 for what that draft got wrong)
Scope: Can Kofin put Jellyfin's server-generated chapter images into Kodi's **native** chapters/bookmarks dialog (`videobookmarks`, any skin), under Kofin's pure `plugin://` playback model, with no custom UI, no skin patch, no path substitution, and no Kodi core change?
Sources: `ref/kodi-omega-full` and `ref/kodi-piers-full` (dialog, image pipeline, texture cache — read in full), `ref/jellyfin` (chapter DTO, image endpoints, generation), Kofin play/sync/service code, and **live bench probes on both supported Kodi generations** — this box's Omega and the Bravia's Piers (§4).

---

## 1. Verdict

**Feasible, natively, and verified on the bench.** The mechanism is **play-time texture-cache seeding**: at playback start the service writes Jellyfin's chapter images into Kodi's own texture cache (`Textures13.db` + `Thumbnails/`) under the exact cache keys the bookmarks dialog is about to ask for. The stock dialog then renders them through unmodified Kodi machinery, on any skin. Proven end-to-end on 2026-08-03 against Kodi Omega with *8½* (27 chapters): control run showed the native chapter list with blank tiles; after seeding, every tile rendered the Jellyfin image, with zero extraction attempts in the log.

| Requirement | Outcome |
|---|---|
| Native `videobookmarks` dialog, any skin | ✅ stock dialog renders the seeded images |
| Pure `plugin://` model, no path substitution | ✅ keys are built from the resolved stream URL the service already holds |
| No custom UI / skin work | ✅ zero UI code |
| No Kodi core change | ✅ uses the documented-by-source cache contract |
| Works on both supported Kodi generations | ✅ both verified live — Omega (8½, 27 chapters) and Piers on the Bravia (48 Hrs., 15 chapters) |
| Server prerequisites | Jellyfin per-library "extract chapter images" must be enabled (off by default) |
| Coverage | Direct-played files with embedded chapters (transcodes strip chapters; server-only "dummy chapters" never reach the player) |

## 2. Why this works — the Kodi mechanics

### 2.1 The dialog builds art URLs unconditionally; extraction is what fails today

`CGUIDialogVideoBookmarks::OnRefreshList` keys everything off the **resolved** path: `m_filePath = g_application.CurrentFileItem().GetDynPath()` (Omega `xbmc/video/dialogs/GUIDialogVideoBookmarks.cpp:232`, Piers identical). For every player-reported chapter it sets a thumb art URL whenever the `myvideos.extractchapterthumbs` setting is on (**default true** on both generations — `system/settings/settings.xml:1075` Omega / `:1180` Piers):

- Omega (`GUIDialogVideoBookmarks.cpp:288`): the raw string `chapter://{dynPath}/{n}` (1-based chapter number, no encoding).
- Piers (`GUIDialogVideoBookmarks.cpp:300-303`): `IMAGE_FILES::CImageFileURL::FromFile(m_filePath, "video")` + option `chapter=n`, serialized by `ToCacheKey()` → `image://video@{urlencoded-dynPath}/?chapter={n}`.

There is **no** `CanExtract` gate in the dialog. The art URL always goes into the image pipeline; what fails today is the load stage: the special loader (`VideoChapterImageFileLoader.cpp:52` Omega / `VideoGeneratedImageFileLoader.cpp` Piers) calls `CDVDFileInfo::ExtractThumbToTexture`, which bails in `CanExtract` because `IsInternetStream()` is true for any http/https URL — there is no LAN carve-out in `URIUtils::IsInternetStream` (Omega `xbmc/utils/URIUtils.cpp:1037`; the LAN check at `DVDFileInfo.cpp:269-272` is dead for the internet-stream case because line 261 already returned). Result: blank tiles plus one `CreateLoader - unsupported protocol(chapter)` warning per tile.

### 2.2 The cache is consulted before the extractor, and a seeded entry is trusted forever

Both the render path (`CImageLoader::DoWork`, Omega `xbmc/GUILargeTextureManager.cpp`) and the cache job (`CTextureCacheJob::DoWork`, `TextureCacheJob.cpp:55-69`) call `CheckCachedImage(url)` **first** and only fall through to the loader on a miss. Three properties make seeding safe and permanent:

- The DB key is the art string itself: raw `chapter://…` on Omega (`UnwrapImageURL` passes non-`image://` strings through, `TextureCache.cpp:81`); the canonical `image://video@…/?chapter=n` round-trip on Piers (`TextureCache.cpp:91` normalizes via `IMAGE_FILES::ToCacheKey`).
- Special-type images are **never hash-revalidated**: `ShouldCheckForChanges` (`TextureCacheJob.cpp:90-100`) marks them not updateable, and `GetCachedTexture` only surfaces a hash when `lasthashcheck` is a valid datetime older than a day — seed with empty `imagehash`/`lasthashcheck` and the entry is served as-is forever.
- A failed load writes **nothing** (`OnCachingComplete` touches the DB only on success, `TextureCache.cpp:299-307`), so the blank-tile control run leaves no negative-cache row to fight.

### 2.3 The exact cache contract (what a seeder must write)

For each chapter `n`:

1. Image file at `<profile>/Thumbnails/{h[0]}/{h}.jpg` where `h = "%08x" % crc32_mpeg2(lowercase(key))` — Kodi's `Crc32` is CRC-32/MPEG-2 (init `0xFFFFFFFF`, poly `0x04C11DB7`, MSB-first, no final xor; `GetCacheFile`, `TextureCache.cpp:284-290`). The `cachedurl` column is authoritative, so the name only needs to be *consistent*, but mimicking Kodi's scheme keeps tooling sane.
2. Row `texture(url=key, cachedurl='{h[0]}/{h}.jpg', imagehash='', lasthashcheck='')`.
3. Row `sizes(idtexture, size=1, width, height, usecount=1, lastusetime=now)` — **mandatory**: the lookup is an INNER JOIN on `sizes.size=1` (`TextureDatabase.cpp`, `GetCachedTexture`).

Schema: `Textures13.db` / schema 13 on Omega; Piers is schema 14 — filename `Textures14.db` **confirmed on the Bravia** — adding `texture.lastlibrarycheck`, which the stock INSERT leaves NULL. Same tables, same lookup SQL otherwise.

Piers key encoding, byte-exact: `image://video@` + encode(dynPath) + `/` + `?chapter=` + n, where encode keeps `[A-Za-z0-9]` and `-._!()` and percent-encodes everything else with **lowercase** hex (`URIUtils::URLEncode`, `URIUtils.h:35`; hostname re-encoded on output because the image protocol `HasEncodedHostname`, `URL.cpp GetWithoutFilename`).

### 2.4 Why per-play seeding (not sync-time)

The dynpath is Kofin's resolved stream URL, `{server}/Videos/{id}/stream.{ext}?static=true&mediaSourceId=…&deviceId=…&playSessionId=…` (`plugin/play.py:84-100`), and `playSessionId` is a fresh server-issued value per play (`core/api.py:268-291`). So the cache key is only knowable once playback resolves — but the service already holds exactly this string at `onPlayBackStarted`: `Player._claim()` pops the play-state entry whose `Path` **is** the dynpath, along with `Id`, `PlayMethod`, `MediaSourceId`, `DeviceId` (`service/player.py:1308-1338`, `core/state.py:100-109`). Seed there, delete in `finalize()`.

## 3. The Jellyfin side

- Chapter metadata: request the item with `Fields=Chapters` (user-scoped route — bare `/Items/{id}` 400s on 10.11). Each `ChapterInfo` carries `StartPositionTicks`, `Name`, and `ImageTag` when an image exists (`MediaBrowser.Model/Entities/ChapterInfo.cs`; tag = MD5 of video path + image mtime, stable across rescans, not per-chapter-unique).
- Images: `GET /Items/{id}/Images/Chapter/{index}?tag=…&maxWidth=…` — **anonymous** (`ImageController.cs:624-646` has no auth attribute and Jellyfin sets no fallback policy), matching Kofin's existing token-free artwork URL policy. `index` is the server's 0-based `ChapterIndex`; Kodi chapters are 1-based, so Kodi chapter `n` ↔ server index `n-1`. JPEG output, sized by `maxWidth`/`maxHeight` on request.
- Generation is **opt-in server-side**: per-library `EnableChapterImageExtraction` defaults to false (plus optional extract-during-scan); otherwise a nightly 02:00 task extracts. When disabled the server actively deletes existing chapter images and `ImageTag` disappears. This is the single deployment prerequisite worth documenting for users.
- "Dummy chapters" (`DummyChapterDuration`, global, default 0/off) are synthesized server-side for files with ≤1 embedded chapter — they get images too, but they do not exist in the stream, so Kodi's player never lists them; they are unreachable by this feature (§6).
- No change signal: regenerating chapter images touches neither `DateLastSaved` nor any websocket event. Play-time fetching sidesteps staleness entirely; sync-time caching would rot invisibly.

## 4. Bench proofs (2026-08-03)

### 4.1 Omega — this box, kofin-test profile, through the real kofin play path

Procedure and evidence, reproducible with `tools/`-free hand steps:

1. Played *8½* (kodi movieid 1777) via the library entry → Kofin resolved DirectStream (`kofin.plugin.play: play ec40da70… via DirectStream`). The mkv's 27 embedded chapters (verified independently with `ffprobe` over the same URL) appeared **by name** in the native dialog with blank tiles — the current, broken-by-default state. Log per tile: `CreateLoader - unsupported protocol(chapter) in chapter://https/jelly.konell.xyz/Videos/…/stream.mkv?static=true&…&playSessionId=43cfe963…/N` (the `https/` collapse is `CURL::GetRedacted` re-serialization for display; the raw key keeps `https://`).
2. Seeded all 27 chapters per §2.3: images fetched anonymously from `/Items/…/Images/Chapter/{n-1}?tag=…&maxWidth=640` (640×360 JPEGs, 23–148 KB), files + rows written into the **profile's** texture DB while Kodi was running (plain `sqlite3` with a 10 s timeout; no lock contention observed).
3. Reopened the dialog: every visible tile rendered its Jellyfin image, including deep-scrolled chapters 12–16; **zero** new `CreateLoader` warnings — pure cache hits, the extractor never ran. Screenshots archived from the session (`shot-20260803-124033.png` control, `-124730.png` and `-124844.png` seeded).
4. Reverted: rows and files deleted from both texture DBs, playback stopped, `debug.showloginfo` restored.

Probe traps worth recording: (a) this box's Kodi runs the `kofin-test` **profile** with `hasdatabases=true` — the live texture DB is `profiles/kofin-test/Database/Textures13.db`, and seeding the master-profile DB does nothing (in-addon code is immune: `special://profile/` resolves correctly, and `sync/db.py` discovery already handles it); (b) a DV HDR10 title (*After Hours*) transcoded and the HLS stream carried no chapters — transcode playback has nothing to attach thumbs to; (c) `VideoPlayer.ChapterCount` returns empty over JSON-RPC `GetInfoLabels` even while chapters exist — the dialog, not that label, is ground truth.

### 4.2 Piers — the Bravia (Kodi 22.0 beta1, Android), key-encoding validation

Because the seeding surface is kofin-independent, the Piers leg used a **deterministic raw stream URL** (`Player.Open` on `http://192.168.1.167:8096/Videos/{id}/stream.mkv?static=true&mediaSourceId={id}`, no plugin resolve, so no per-play `playSessionId`) with *48 Hrs.* (HEVC SDR mkv, 15 chapters). Control run: native dialog listed all 15 chapters ("Chapter: 01/15" header) with blank tiles. Then 15 Jellyfin chapter images were seeded into the live `Textures14.db` + `Thumbnails/` under keys computed by a Python replica of Kodi's encoder — `image://video@{enc(dynpath)}/?chapter={n}` with the RFC1738 keep-set and lowercase hex — and the reopened dialog rendered every tile from cache (chapter 1's black tile is the server's actual image: a frame grabbed at 0:15 inside the studio logo). Since `CanExtract` forbids extraction from an internet stream, rendered tiles prove the computed keys matched **byte-for-byte**, including the encoder edge cases `:`→`%3a` (host port), `/`→`%2f`, `?`→`%3f`, `=`→`%3d`, `&`→`%26`, preserved letter case. Cleanup deleted exactly 15 rows by exact-URL match (confirming nothing was re-cached over them) and left zero chapter rows.

Piers-specific findings: (a) `Textures14.db` confirmed as the live filename; (b) the Bravia's own cache already held `image://video@…` rows for an SMB-played film — chapter seeding rides the very pathway Kodi itself uses where extraction is allowed; (c) **failed** chapter-thumb loads log *nothing* on Piers even at debug level (Omega at least warned `CreateLoader - unsupported protocol(chapter)`) — a diagnosability regression to remember when debugging blank tiles; (d) Piers's JSON-RPC `Player.GetItem` exposes a **`dynpath`** property returning the exact resolved URL — ground truth for integration tests without log mining; (e) on Android, `/sdcard/Android/data/org.xbmc.kodi/` is ADB-readable but not ADB-writable (scoped storage), so the seed ran *inside* Kodi via EventServer `RunScript` + Kodi's bundled Python `sqlite3` writing the live DB — which is precisely kofin's future write path, exercised successfully while Kodi was running.

## 5. Implementation sketch for Kofin

Small, service-owned, no plugin-process work:

1. **Hook**: in `Player.onPlayBackStarted` after `_claim()`, when the claimed item is a video and `PlayMethod != "Transcode"`, hand `{Id, Path, DeviceId}` to a named worker thread (pattern per `plugin/adduser.py`'s service-side workers).
2. **Fetch**: one `GET /Users/{uid}/Items/{Id}?Fields=Chapters`; bail if no chapters or no `ImageTag`s. Sanity-guard the count against the player's chapter count and seed only the overlap (protects against server chapters diverging from the played file, e.g. differing multi-version cuts — Jellyfin chapters are per-item, not per-MediaSource).
3. **Seed**: for each chapter, download `?tag=…&maxWidth=640` (or match Kodi's `imageres`), write the cache file and the two rows. The texture DB scaffolding already exists unused: `sync/db.py` `KINDS` includes `"texture"`, `schema.py` resolves its path with `SUPPORTED["texture"] = None` ("phase 2 never writes it"), and `sync/kodidb/queries_texture.py` sits unimported. Opening the gate means versioning it: `SUPPORTED["texture"] = {13, 14}` plus fixture-backed tests, per the schema-gate rules.
4. **Key builder**: Omega → raw `chapter://{Path}/{n}`; Piers → `image://video@{enc(Path)}/?chapter={n}` with the §2.3 encoder. Key by `sync/schema.py` generation, never inlined (same rule as `EXTRA_ITEM_TYPE`).
5. **Cleanup**: delete the seeded urls/files in `finalize()`; on service start, sweep leftovers from crashes with `DELETE FROM texture WHERE url LIKE '%deviceId={ours}%' AND (url LIKE 'chapter://%' OR url LIKE 'image://video@%')` plus file removal — the deviceId inside every key makes Kofin's rows self-identifying. Piers has a 30-day-unused cache cleaner as backstop (`TextureCache.cpp:440`); Omega has none, so Kofin's own cleanup is the only one.
6. **Cost**: one metadata GET + N image GETs per play (~1–2 MB at 640 px for a 27-chapter film, LAN), landing within a couple of seconds of playback start.

## 6. Limits, risks, edge cases

- **Transcode plays get nothing** — the HLS stream carries no chapters, so the dialog has no chapter rows to decorate. Inert, not broken.
- **Files without embedded chapters get nothing** — Jellyfin dummy chapters never reach the player. The only native vehicle for them would be type-0 `bookmark` rows against the dynpath's `files` row, but those render with generic localized "Bookmark N" labels (`GUIDialogVideoBookmarks.cpp:256`), need a per-play `files` insert, and would duplicate real chapters elsewhere — recommended **out of scope**. (If ever pursued: `queries.py:812` `delete_bookmark` must first become type-scoped, a known sharp edge.)
- **First-seconds race**: opening the dialog before seeding lands shows blank tiles once; closing and reopening re-requests and hits the cache (failed textures are only cached in GUI memory while referenced). If this annoys, pre-fetch images to `addon_data` at sync time so the play-time step is copy+insert only — at the cost of sync payload and invisible staleness (§3); not recommended initially.
- **`myvideos.extractchapterthumbs` off** → the dialog never sets chapter art and seeding is invisible (harmless). Leave the Kodi default alone; do not touch the setting programmatically.
- **Server not extracting images** → no `ImageTag`s, feature silently inert. Document the per-library server setting in the README.
- **Cache churn**: one key-set per play (fresh `playSessionId`); own-cleanup keeps steady state at zero. Orphans only on hard crashes, caught by the startup sweep.
- **Concurrent DB writes**: same live-write posture Kofin already takes for MyVideos/MyMusic; the probe wrote Textures13.db under a running Kodi without contention. Use the standard busy-timeout wrapper.

## 7. Owed verification before shipping

1. ~~Piers bench probe on the Bravia~~ — **done 2026-08-03** (§4.2): `Textures14.db` and the byte-exact wrapped key confirmed live.
2. ~~Re-run through the real service-code path~~ — **done 2026-08-03** on the implementation branch: claim → seed (27/27 within ~0.7 s of play start) → dialog renders → finalize reverts to zero rows/files, repeat play with a fresh session id cycles cleanly, no orphaned Thumbnails files (all recent files DB-referenced). Remaining niceties: a kofin-resolved play on the Bravia, and the documented mid-play service-restart edge (entries are reverted by `stop_threads`, the running playback loses its tiles).
3. ~~L2-style texture fixture tests~~ — **done**: `tests/fixtures/textures13.sql`/`textures14.sql` (dumped from the same live boxes), writer + worker + wiring suite in `tests/unit/test_chapter_thumbs.py` with the bench-verified key/CRC vectors, gate opened as `schema.SUPPORTED["texture"] = {13, 14}`.

## 8. Corrections to the 2026-07-29 draft

The draft's verdict — "blocked; the only any-skin pattern is Emby-style path substitution" — was wrong on three load-bearing points:

1. It treated the `GetDynPath()` lookup as the fatal identity mismatch. The dynpath is not the blocker, it is the **hook**: the service knows the exact dynpath at play time (`state.claim_play_item`), which is early enough because the dialog can only exist during playback.
2. It analyzed only the *extraction* leg (`CanExtract`) and missed that the art URL is set unconditionally and that the texture cache is consulted **before** the extractor, with special images exempt from revalidation — the entire seeding surface.
3. It therefore over-weighted the ecosystem survey ("nobody does native thumbs without path-sub or custom UI"). Still true as an observation about other addons — Emby Next Gen needs its proxy identity, EmbyCon ships a custom dialog, jellyfin-kodi's #718 sits open — but the survey described unexplored territory, not an impossibility. Kofin would be the first to do this natively.

Still valid from the draft and carried forward: the Jellyfin API shape (§3 here), the server-side generation gates, the `delete_bookmark` type-scoping hazard, and the rejection of options A (custom dialog — violates the requirement), B (path substitution — architecture reversal, now also unnecessary), C (type-0 rows alone — invisible or mislabeled), and E (Kodi core change — unnecessary).

## 9. Key code references

| Location | Fact |
|---|---|
| `ref/kodi-omega-full/xbmc/video/dialogs/GUIDialogVideoBookmarks.cpp:232,285-290` | dynpath identity; unconditional `chapter://` art behind `extractchapterthumbs` |
| `ref/kodi-piers-full/xbmc/video/dialogs/GUIDialogVideoBookmarks.cpp:300-303` | Piers wrapped key via `CImageFileURL::ToCacheKey()` |
| `ref/kodi-omega-full/xbmc/TextureCacheJob.cpp:55-69,90-100,218-222` | cache-first; special images never revalidated; `chapter://`→`videochapter` |
| `ref/kodi-omega-full/xbmc/TextureCache.cpp:79-98,284-297,299-307` | lookup/key semantics; CRC cache filename; failure writes nothing |
| `ref/kodi-omega-full/xbmc/TextureDatabase.cpp` (`GetCachedTexture`/`AddCachedTexture`) | required `texture`+`sizes(size=1)` rows; hash honored only with valid day-old `lasthashcheck` |
| `ref/kodi-piers-full/xbmc/TextureDatabase.h:133`, `…/TextureDatabase.cpp UpdateTables` | schema 14 = +`lastlibrarycheck` |
| `ref/kodi-piers-full/xbmc/imagefiles/ImageFileURL.cpp`, `xbmc/URL.cpp`, `xbmc/utils/URIUtils.h:35` | Piers key serialization and RFC1738 lowercase-hex encoding |
| `ref/kodi-omega-full/xbmc/cores/VideoPlayer/DVDFileInfo.cpp:250-275`, `xbmc/utils/URIUtils.cpp:1037` | why extraction fails for any http(s) stream |
| `ref/kodi-piers-full/xbmc/TextureCache.cpp:440-477` | Piers 30-day unused-image cleaner (backstop) |
| `ref/jellyfin/Jellyfin.Api/Controllers/ImageController.cs:624-646` | anonymous indexed chapter image endpoint |
| `ref/jellyfin/Emby.Server.Implementations/Chapters/ChapterManager.cs:73-97` | per-library extraction gate (default off) deletes images when disabled |
| `lib/kofin/plugin/play.py:84-100`, `lib/kofin/core/api.py:268-291` | dynpath template; per-play `playSessionId` |
| `lib/kofin/service/player.py:581,1308-1338,657-694`, `lib/kofin/core/state.py:100-109` | claim (dynpath + ids at start); finalize (cleanup site) |
| `lib/kofin/sync/db.py`, `lib/kofin/sync/schema.py:31-39`, `lib/kofin/sync/kodidb/queries_texture.py` | dormant texture-DB plumbing to activate behind the schema gate |

## 10. Open questions

1. Ship behind a Kofin setting, or always-on when the server provides images? (Cost is per-play and small; always-on with a kill switch seems right.)
2. Image width: fixed 640, or track Kodi's `imageres` advanced setting?
3. Is the chapter-count sanity guard (player vs server) enough for multi-version items, or should positions be compared too?
4. Worth a follow-up upstream issue on jellyfin-kodi #718 documenting the mechanism? It applies to any `plugin://` client.
