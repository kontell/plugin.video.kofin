# Audit fixes: 2026-08-30 completion pass

| Field | Value |
|---|---|
| **Date** | 2026-08-30 |
| **Source** | `docs/audit-report.md` against `48f3ae5` plus this session's fixes |
| **Scope** | A2-H1 and the four Mediums that landed with it; then the remaining Mediums, one commit each |
| **Rule** | Transplant changes keep L2 dumps identical. Nothing is deleted through jf12. |

---

## 1. Landed this session (against the pin)

| # | Closes | What | Oracle |
|---|---|---|---|
| L1 | A2-H1 | `get_id_etag_map` refuses a shapeless first page; `get_existing_ids` refuses a body without `Items` | `test_get_id_etag_map_refuses_an_empty_body`; `test_get_existing_ids_refuses_a_shapeless_confirmation` |
| L2 | A2-M2 | `run_ladder` checks abort after backoff sleep | `test_a_stop_during_backoff_does_not_start_another_get` |
| L3 | A2-M3 | `WSClient` and time-sync `create_connection` pass `sslopt` on `wss://` from `sslVerify` | `test_wss_honours_ssl_verify` |
| L4 | A2-M5 | Play-queue files created 0600 | `test_play_queue_files_are_owner_only` |
| L5 | A2-M6 | `ATTACH_SUBTITLE` in `ipc.GUARDED` | `test_download_commands_are_guarded` |

CLAUDE.md gained the four constraints those fixes exist to keep.

---

## 2. Remaining, blast radius first

| # | Closes | What | Oracle |
|---|---|---|---|
| R1 | A2-M1 | `_get_items` advances `StartIndex` by `len(items)` like `get_id_etag_map`, and raises if the walk ends short of `TotalRecordCount`. Boxsets then cannot sweep ids the pager skipped. | Pager fake: count 3, limit 2, first page 1 item; the skipped boxset survives `sweep_stale` |
| R2 | A2-M7 | `offline_answer` stamps resume and pushes the same claim shape as `resolve_downloaded` | Offline play with `resume=True` / `startticks` → `setResumePoint` and a queued claim |
| R3 | A2-M4 | Remote Play of Audio uses `PLAYLIST_MUSIC` | `test_remote.py` |
| R4 | A3-M1 | Hash `tag_link` in the video userdata fingerprint, or document the Favourites-widget gap as deliberate and pin it with an L1 that the digest holds | Invert S-P2.3a or pin the gap |
| R5 | A3-T2 | `absolute_path` asserts the result stays under `root` (reject absolute `rel_path`) | `test_downloads_files.py` |
| R6 | A3-T3 | Assert `_prealign_unpause` does not seek when `is_transcoding()` | `test_syncplay_playback.py` |
| R7 | audit-F9 | Two-poll floor before emptying `playlists/music/Kofin/` — needs state in `_maybe_refresh_music_playlists`. Do not ship a bare "refuse when zero" (a user's last playlist deletion would stick). | Unit + live playlist poll |
| R8 | audit-F3 | Music walk commits per page — sync refactor Tier 3, not this branch | L2 music dumps identical; interrupted music walk resumes |

---

## 3. Not in this branch

kodi-drive G1 (`Profiles.GetCurrentProfile` properties) and G2 (callback thread) are contribute PRs, not kofin commits.

Dynamic-libraries W7 (M8) stays in `docs/dynamic-libraries-plan.md`.

---

## 4. Exit

`pytest tests/unit` green. L2 dumps byte-identical for any `_get_items` change (R1). `changelog.txt` names A2-H1 for viewers ("an interrupted prune can no longer empty a library") when this ships.
