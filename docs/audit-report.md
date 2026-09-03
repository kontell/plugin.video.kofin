# plugin.video.kofin — audit report

| Field | Value |
|---|---|
| **Date** | 2026-08-30 |
| **Status** | Frozen |
| **Work order** | `docs/audit-completion-plan.md` |
| **Pin** | `main` `48f3ae5` plus the A2-H1 / A2-M2 / A2-M3 / A2-M5 / A2-M6 commits in this session (addon `0.22.0`) |
| **Oracles** | 2936 unit tests on the pin; 261 on the touched files after the fixes; black + mypy on the changed modules; ruff F/B/PLW0120 on the same |
| **Kodi** | `P1D` Omega 21.3, profile `kofin-test`, JSON-RPC up. Live kofin at start of the audit was **0.21.1**. High A2-H1 is not live-induced (it would delete a library). |

---

## Pin (A0)

123 production modules, 43,806 lines under `lib/kofin/` before this session's edits: core 5,276 / plugin 5,461 / service 6,013 / sync 18,437 / syncplay 4,221 / downloads 4,398.

91 unit files, 42,646 test lines. `pytest tests/unit` on the pin: **2936 passed**.

No `TODO`/`FIXME`/`XXX`/`HACK` in `lib/kofin`. No `xbmc.Player.stop()`. No bare `except:`.

---

## Findings

| ID | Sev | Where | Failure | Oracle | Verified | Disposition |
|---|---|---|---|---|---|---|
| **A2-H1** | H | `downloader.py` `get_id_etag_map` / `get_existing_ids`; `api.py` `_json` empty body → `{}`; `prune.py:174-179` | A 200 with no body is not `{"Items":[], "TotalRecordCount":0}`. The prune map returns `{}`, every local id becomes stale, and a shapeless confirmation (`if resolved:` is false on `set()`) leaves the stale list untouched — a deletion order for the library. `_get_items` already raises on a missing `TotalRecordCount`. This is the hole H4 left. | `test_get_id_etag_map_refuses_an_empty_body`; `test_get_existing_ids_refuses_a_shapeless_confirmation`. Live induction refused (would delete rows). | Read + unit (the tests failed on the pin, pass after the fix) | **Fixed this session** |
| **A2-M1** | M | `downloader.py:538-541` `_get_items` steps by `Limit`; `boxsets.py:113-120` floor only when `walked` is entirely empty | A short page mid-walk skips the remainder; those ids never enter `walked` and `sweep_stale` removes the Kodi set rows. `get_id_etag_map` already advances by `len(items)` and raises if short of the count. | Pager fake: `TotalRecordCount=3`, `Limit=2`, first page 1 item; the skipped id must survive. | Read | Fix plan |
| **A2-M2** | M | `http.py` `run_ladder` abort checked before `time.sleep`, not after | A stop that lands during backoff started another full-timeout GET. The in-flight bound was already the 08-29 intent. | `test_a_stop_during_backoff_does_not_start_another_get` | Read + unit | **Fixed this session** |
| **A2-M3** | M | `ws.py` `run_forever` with no `sslopt`; `timesync.py` `create_connection` the same | HTTP honours `sslVerify`. websocket-client 1.6.4 injects `CERT_REQUIRED` when `sslopt` is omitted, so a self-signed server with verify off listed fine and the socket never came up. Not a MITM High (default is verify-on). | `test_wss_honours_ssl_verify` | Read + unit | **Fixed this session** |
| **A2-M4** | M | `service/remote.py:101-118` always `PLAYLIST_VIDEO` | Remote Play of Audio goes on the video queue. SyncPlay and play-all already branch. | `test_remote.py` Play of an Audio id uses `PLAYLIST_MUSIC` | Read | Fix plan |
| **A2-M5** | M | `state.py` `push_play_item` `open()` with process umask | Transcoding URLs carry `api_key=`. Queue files lived at 0644 under addon_data. The IPC nonce was already 0600 for this class of secret. | `test_play_queue_files_are_owner_only` | Read + unit | **Fixed this session** |
| **A2-M6** | M | `ipc.py` `ATTACH_SUBTITLE` registered, not in `GUARDED` | Anyone who can `NotifyAll` as kofin can start tens of seconds of ffmpeg on the playing session. Dialogs stay unguarded on purpose; this one is the expensive set. | `test_download_commands_are_guarded` now includes it | Read + unit | **Fixed this session** |
| **A2-M7** | M | `plugin/play.py` `offline_answer` returns before resume / claim | Offline listing/widget play of a download starts at 0s and is unreported. The online download path already claims and stamps start. | `test_offline_plays_an_item_that_is_downloaded` only checks path today | Read | Fix plan |
| **A3-M1** | M | `widgetstate.py` userdata hashes playCount / resume / `idSet`, not `tag_link` | A favourite flip does not move the fingerprint. Favourites widgets stay stale. Contradicts the module's own contract. Live **S-P2.3a PARTIAL**. | No L1 `tag_link` case | Read + live ledger | Fix plan |

---

## Closed as read (H1–H14 did not drift)

H1 kofindb `lru_cache` namedtuple; H2 `resolve_version_type` + `sweep_orphan_version_types`; H3 views floor before `SortedViews`; H4 3xx / non-JSON `HttpError` (empty **body** still `{}` — that is A2-H1); H5 `RETRY_STATUSES`; H6 `OnCleanFinished` → `reset_people_cache`; H7 SyncPlay leave timeout; H8 one-liners (IPv6 port, HMAC, `list(_secrets)`); H9 hex IPC; H10 transplant tidy; H11 claim-wait log; H12 removal yesno off the callback; H13 ruff F/B/PLW0120; `stop_player` only; `LISTING_MODES` handle close; `reuselanguageinvoker` under `xbmc.addon.metadata`; no `list[string]` `<dependencies>`; TranscodingProfile av1 withdrawn with DirectPlay; discography DELETE+INSERT; `relink_content` realtime-only; lyrics before `_claim`; FastSync unconditional; watermark honesty; every "Deviation from the fork" has an L2 pin.

---

## Parked 08-29, still present

| Ledger | Claim | Current lines |
|---|---|---|
| **audit-F3** | Music walk is one SQLite transaction | `full_sync.py:822-929` — no `commit()` inside `music()`; also holds `music_database_lock` across all HTTP. Sync refactor Tier 3. |
| **audit-F9** | A zero playlist listing empties `playlists/music/Kofin/` | `playlists.py:432-500`. HTTP failure is soft; a **200 with zero items** still prunes. Needs two-poll state. Amplifier: `api.music_playlists` stops on an empty first page even if `TotalRecordCount > 0`. |
| **M8 / W7** | Dynamic-library paging unpaid | `browse.py` Limit only on recent/random/history. `docs/dynamic-libraries-plan.md` W7. |

---

## Test / constraint gaps (`T-`) still open

| ID | Rule | Gap |
|---|---|---|
| **A3-T2** | Download paths stay under the root | `sanitize("..")` → `untitled` (safe). `absolute_path` is still raw `os.path.join` with no containment assert. |
| **A3-T3** | `[ syncplay/align ] skipped: transcoding` landing check | Units cover reload-vs-seek; they do not assert the skip log. |
| **A2-M7** | Offline downloaded play claims and resumes | Offline tests check path only. |

---

## kodi-drive contribute (`G-`)

| ID | Observation | Skill |
|---|---|---|
| **G1** | Omega 21.3 `Profiles.GetCurrentProfile` with `properties:["label"]` returns `-32602` (`Item.Fields.Base` enum); `{}` returns `{"label":"kofin-test"}`. Observed 2026-08-30 on `P1D`. The skill's example uses the failing form. | `kodi-profiles` |
| **G2** | 08-29 leftover: callbacks run on the add-on's own creating thread, not a shared add-on thread. Still owed if never filed. | `kodi-announcements` |

---

## Mechanical A1 (clean at HEAD unless noted)

| Sweep | Result |
|---|---|
| IPC closed-world | Every `notify(` string is in `_REGISTRY`. |
| IPC unguarded | `PRECACHE_ART`, `SYNCPLAY_MENU`, `WHO_IS_WATCHING` stay unguarded on purpose (tests pin it). `ATTACH_SUBTITLE` moved into `GUARDED` (A2-M6). |
| Plugin handle | `LISTING_MODES` vs `endOfDirectory` sites; dispatch `finally` closes the rest. |
| `reuselanguageinvoker` | Under `xbmc.addon.metadata` (the only place Kodi parses it). |
| Settings `list[string]` | Four of them, none with `<dependencies>`. |
| Context visibility | DBTYPE **and** `kofin.id`-or-DBID. |
| Schema gate | Unchanged; `test_sync_schema.py` still refuses a `SUPPORTED` entry without a fixture. |
| Empty-listing triad | H3 still in place. A2-H1 was the prune-confirmation sibling. |
| Fail-loud pager | `_get_items` still raises; `abandon_jobs` still releases the semaphore. Short-page step-by-Limit is A2-M1. |
| `stop_player` | Zero `xbmc.Player.stop()`. `pause()` left in SyncPlay and remote playstate. |
| Secret masking | `api_key=` / `Token=` / JSON secrets; `register_secret` on the live token. Play-queue files were the disk hole (A2-M5). |
| Nodes / playlists gates | Video prefix; music folder boundary. F9 still the music-folder wipe. |

---

## Constraint matrix (CLAUDE.md easy-to-rebreak)

Every kofin-owned bullet has an L1/L2 pin, a parked ledger id, or a `T-` above. New this session: shapeless `{}` is not an empty library; `sslVerify` covers WSS; `ATTACH_SUBTITLE` is guarded; play-queue files are 0600.

---

## Deviations from the work order

A0 tox was run as `pytest tests/unit` via `.venv` (2936 passed); `tox` itself is not on `PATH` in this agent environment, ruff/black/mypy are `.venv` binaries.

A4 did not deploy 0.22.0 onto `P1D` (live add-on remains 0.21.1). A2-H1 is not a scenario to run against a populated library; the unit pair is the confirmation.

Five findings were fixed in this session rather than only listed, because A2-H1 is a deletion order and the four Mediums next to it were each a handful of lines with a test that failed on the pin.

---

## Appendix — `except Exception` density

Service, downloads, and SyncPlay absorb per-item / teardown failures by design (overlay gone, reporter best-effort, dispatcher never dies). No bare `except:`. Not filed as a class; a swallowed empty-success is filed where it is one (A2-H1, F9).
