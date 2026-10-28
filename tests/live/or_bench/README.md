# First-import bench for the API build

Times a first import of one Jellyfin library into an empty Kodi on a real device, phase by phase. Written for the LibreELEC Raspberry Pi 3 (`systemctl`, `/storage/.kodi`, BusyBox), where the numbers below were taken on 11 October 2026.

**It deletes Kodi's music, video and texture databases and kofin's own state on the device.** `box/snapshot.sh` keeps the profile first and `box/restore.sh` puts it back; `box/bench.sh` refuses to run without a snapshot.

Kodi's debug logging must be on: `bench.sh` waits for kofin's service start and pass lines, which kofin logs at debug.

## Use

```sh
export KOFIN_BENCH_HOST=root@<device>          # key-based ssh; never commit the host
tests/live/or_bench/deploy.sh                  # stage this tree's API build and the box/ scripts
tests/live/or_bench/host.sh 'sh /storage/kofin-bench/snapshot.sh'      # once
tests/live/or_bench/host.sh 'cd /storage/kofin-bench && nohup sh bench.sh run1 <library-id> <rows> 10800 delete movies > runs/run1.out 2>&1 &'
tests/live/or_bench/host.sh 'tail -1 /storage/kofin-bench/runs/run1/samples.txt; cat /storage/kofin-bench/runs/run1.out'
tests/live/or_bench/host.sh 'sh /storage/kofin-bench/report.sh run1'   # the phase lines, seconds from Kodi's start
tests/live/or_bench/host.sh 'sh /storage/kofin-bench/restore.sh'       # when done
```

`deploy.sh <tree>` stages another checkout (a `git archive` of the commit to compare against). `bench.sh` takes the journal mode Kodi's databases should be in and whether the companion's name indexes exist, so what `service.kofin.companion` would change is measured without installing it: `delete` is stock Kodi, `wal-idx` is the companion's end state.

A run settles when Kodi's row count has reached `<rows>`, nothing is pending in kofin's store and `native pass done` is in the log, three samples running. `samples.txt` has a line every ten seconds (rows, pending, Kodi's RSS, CPU ticks, temperature, throttle flags) and `threads.txt` the threads that used more than 2 % of a core in that interval.

`box/sqltrace.sh <out> [seconds]` switches Kodi's `database` log component on for a while during a run and saves what it logged; `kodi-drive`'s `kodi-performance` says how to read it. `box/index-survey.py` times Kodi's name lookups on copies of the snapshot's databases with and without a case-insensitive index.

## Things that cost a run

- BusyBox has no `timeout`, no `grep --include`, no `dmesg -w` (`cat /dev/kmsg`).
- Replace a script the device is running with `mv`, never by writing into it: the shell reads it as it goes.
- The Pi reaches 80 °C and caps its clock late in a long video scan (`throttled=0x20002` in `samples.txt`). Runs compared here all did.
- The SD card dropped out once under load: ssh refused new logins while the device still answered pings. `host.sh` keeps one authenticated connection for that reason.

## 11 October 2026, LibreELEC Pi 3, Kodi 22.0b2

Pass time is `native pass done in`; "from start" is service start to the end of the pass. `63bd091` is the tip of `feat/or-companion-c4`, the commit this branch started from.

| Library | Build | Kodi's databases | Scan | Pass | From start |
|---|---|---|---|---|---|
| Music-Alt, 1,353 songs | `63bd091` | stock | — | 357.5 s | 373 s |
| | this branch | stock | 20.6 s | 37.0 s | 51 s |
| | this branch | WAL | 12.0 s | 21.7 s | 35 s |
| Music, 22,381 songs | this branch | WAL | 292.7 s | 957.9 s | 17.7 min |
| | this branch | stock | 493.8 s | about 3,020 s | about 52 min |
| Movies, 1,788 | `63bd091` | stock | 1,300.6 s | 1,489.6 s | 26.0 min |
| | this branch | stock | 1,078.4 s | 1,260.7 s | 21.8 min |
| | this branch | WAL + indexes | 258.9 s | 424.3 s | 7.9 min |
| Shows, 4,316 episodes in 79 shows | `63bd091` | stock | 1,501.1 s | 1,918.2 s | 33.5 min |
| | this branch | stock | 1,334.4 s | 1,472.0 s | 25.7 min |
| | this branch | WAL + indexes | 299.2 s | 435.7 s | 8.5 min |

"WAL + indexes" is what `service.kofin.companion` does to Kodi's databases on its `perf/journal-wal` branch; the bench makes that state itself. "Stock" is a database Kodi created, which is a rollback journal; a profile the SQL build has written is already WAL and behaves like the WAL rows. The Music-Alt rows and the Music WAL row predate the last video commits, which do not touch music.

The stock Music row is a projection: the run was stopped ten minutes into the last kind, with the library and its artwork complete 14 minutes after the service started, at a steady 3.47 `SetSongDetails` a second for the 7,873 played songs (0.29 s each, seven autocommitted statements for a song of four genres). In WAL the same kind took 512 s.

What each step was worth, same library and device:

| Change | Measured |
|---|---|
| A first music import is not a repair write | Music-Alt pass 357.5 s to about 45 s |
| The websocket thread holds no Monitor | Music scan decaying from 72 to 13 songs/s and a listing from 0.14 s to 0.8 s; afterwards 69 songs/s on average and 0.04 s a listing |
| Thirty-two album directories to a scan | Music-Alt scan 27.2 s to 20.6 s; Music 323 s to 293 s |
| Three pages at a time | 22,381 songs enumerated in 45 s, was 113 s |
| Kodi's actor thumbnail download held off | Movies scan 1,265 s to 1,078 s; 446 s to 259 s with WAL + indexes |
| Small scopes read in one listing | Shows episode kind 119 s to 83 s |
| Season art on the show's listing, with the row above | Shows season kind 125 s to 26 s (235 s on `63bd091`) |
| WAL (companion) | `SetSongDetails` 133 ms to 17 ms; Music-Alt pass 37.0 s to 21.7 s |
| Name indexes (companion) | `actor` lookup 28.2 ms to 0.15 ms at 38,000 rows |

## Removing a library, 15 October 2026

The question #308 left open: does removing a library a piece at a time, so Kodi's callback queue can drain between pieces, beat the one `SetSourceContent remove` on the library root? Movies, 1,791 rows, removed with `RemoveLibrary` from a profile on this stack (P1D: Piers flatpak with service.kofin.companion 0.1.2; Pi: LibreELEC Pi 3, no companion). "Pieces" is fifty bound movie folders to a call, a second apart, then the root.

Three measures besides the time. `box/heartbeat.py`, run inside Kodi with `RunScript`, sleeps 100 ms at a time and records how late it wakes: a Python thread starved by the callback queue shows there. The clear's own log line gives the longest of the library thread's 3 s waits, the symptom #308 saw (47 s). A host-side poll times `XBMC.GetInfoLabels` every half second. And Kodi's log counts the widget refreshes (`CDirectoryProvider[…]: refreshing`).

| Device | Removal | On screen | Clear | Widget refreshes | Heartbeat, latest wake | Longest 3 s wait | JSON-RPC, slowest |
|---|---|---|---|---|---|---|---|
| P1D | one call | Music window, no movie widgets | 69.5 s | 31 | 0.05 s | 3.0 s | 0.37 s |
| P1D | one call | Home, movie widgets | 109.0 s | 1,172 | 0.12 s | 3.0 s | 0.15 s |
| P1D | pieces | Home, movie widgets | 185.8 s | 938 | 0.08 s | 3.0 s | 0.21 s |
| Pi | one call | Settings, no widgets | 480.2 s | 0 | 0.09 s | 3.0 s | 0.21 s |
| Pi | one call | Home, movie widgets | 803.4 s | 2,448 | 0.32 s | 3.1 s | 0.26 s |
| Pi | pieces | Home, movie widgets | 1,363.8 s | 3,531 | 0.28 s | 3.1 s | 0.98 s |

Nothing stalled on either device, with or without pieces, so the pieces have nothing to buy, and they cost the pauses, a call per folder and more widget refreshes besides: the removal stays one call. What does cost a clear is the home widgets. Every row a clear deletes is a `VideoLibrary.OnRemove` with no `transaction` flag (`CVideoDatabase::DeleteMovie`; Kodi sets the flag only for its own clean), and every visible widget that holds video items is invalidated by it and lists its path again, back to back, for as long as the clear runs: each movie widget about 400 times on the Pi, two thirds more time for the clear.
