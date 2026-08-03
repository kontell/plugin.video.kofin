# New-content notifications

| Field | Value |
|---|---|
| **Date** | 2026-08-03 |
| **Status** | Implemented on `feat/new-content-notifications` (unit-tested; live scenario S-newcontent outstanding) |
| **Addon** | `plugin.video.kofin` |

---

## Overview

Kofin currently tells the user nothing when new content lands. The progress bar (`syncProgressThreshold`) says a sync is *running*; nothing says what arrived. The fork's per-item toast machinery is still in the tree but deliberately dormant — `UpdateWorker.__init__` hard-sets `self.notify = False` with a comment saying the `syncNotification` setting is gone, and `NotifyWorker` drains a queue nobody fills.

**What we do:** collect the additions a sync cycle actually wrote, and raise **one toast per content type per cycle**, naming the item when there is exactly one and counting when there are several. Watched items never notify. The whole feature is behind one toggle in the Library tab, default on.

The dormant machinery is the wrong shape for this: it toasts once *per item*, from a worker thread that cannot see the cycle. Aggregation has to happen where the cycle is visible, which is the library thread. So the plan re-uses the `notify_output` queue as the writers' reporting channel and replaces `NotifyWorker` with a flush on the library thread.

---

## Messages

Six content types produce messages. `%s` throughout (repo convention — see `#30402`), never `%d`.

| Case | String | Example |
|---|---|---|
| 1 movie | `%s movie added to library` | "Blade Runner movie added to library" |
| N movies | `%s movies added to library` | "4 movies added to library" |
| 1 show | `%s show added to library` | "Severance show added to library" |
| N shows | `%s new shows added to library` | "3 new shows added to library" |
| 1 episode, one show | `%s episode of %s added to library` | "1 episode of Severance added to library" |
| N episodes, one show | `%s episodes of %s added to library` | "6 episodes of Severance added to library" |
| N episodes, several shows | `%s episodes added to library` | "11 episodes added to library" |
| 1 music video | `%s music video added to library` | "Bad Guy music video added to library" |
| N music videos | `%s music videos added to library` | "2 music videos added to library" |
| 1 artist | `%s added to music library` | "Kelly Lee Owens added to music library" |
| N artists | `%s artists added to library` | "5 artists added to library" |
| 1 album | `%s added to music library` | "Inner Song added to music library" |
| N albums | `%s albums added to library` | "7 albums added to library" |

Toast order when several fire in one flush: movies → shows → episodes → music videos → artists → albums. Ceiling is six toasts per cycle by construction; at `toast.DEFAULT_TIME_MS` (5 s) that is a 30 s worst case, which needs all six categories to change in a single cycle and is worth the simplicity.

Types that produce **no** message: `Audio` (songs — an album's tracks would drown everything else), `BoxSet`, `Season`. They are dropped at entry-build time, not filtered later, so they never occupy the accumulator.

Episodes belonging to a show announced as new **in the same flush** are dropped from the episode line: "Severance show added to library" followed by "8 episodes of Severance added to library" says the same thing twice. If that leaves no episodes, no episode toast fires (decision D3).

---

## Where the aggregation lives

Three moving parts, in the order data flows.

### 1. `UpdateWorker` reports each addition it wrote

`worker_updates` already builds added-writers with `notify_enabled=source == "added"`, so **metadata-only updates cannot notify by construction** — which is half of "watched, either new or updated, gets no notification" for free. The other half is the `Played` filter below.

Change `self.notify = False` to `self.notify = notify_enabled`, and replace the payload:

```python
if self.notify:
    entry = newcontent.entry_for(item)
    if entry is not None:
        self.notify_output.put(entry)
```

`entry_for` returns `None` for a watched item, an unhandled type, or a payload missing a name — so the worker holds no policy and the whole rule set is unit-testable without threads.

### 2. `newcontent.py` — the pure part

New module `lib/kofin/sync/newcontent.py`. Not transplant code: it gets the strict-mypy override `kofin.sync.changefeed` already has (`mypy.ini`), and current-idiom typing.

```python
class Entry(NamedTuple):
    type: str        # Movie | Series | Episode | MusicVideo | MusicArtist | MusicAlbum
    item_id: str
    name: str
    series: str = ""      # SeriesName, episodes only — what a message says
    series_id: str = ""   # SeriesId, episodes only — what groups them


def entry_for(item: dict) -> Entry | None: ...
def summarize(entries: Iterable[Entry]) -> list[str]: ...
```

`entry_for` drops:

- any item whose `UserData/Played` is true — the watched rule, applied uniformly (a "played" album or artist is rare, and silence there is the same intent);
- any type outside the six;
- any item with an empty `Name` or `Id`, or an `Episode` with no `SeriesName`/`SeriesId` (the display name and the grouping key). All defensive: `SeriesName` and `SeriesId` are both in the episode field set (`obj_map.json:278`, `:244`) and `UserData/Played` is mapped for every type, so none should be absent. An absent `UserData` block is *not* read as watched.

`summarize` dedupes by `item_id` first — the change feed and a repair prune can both offer the same id inside one cycle — then applies the table above.

### 3. `Library` collects and flushes

New state in `__init__`: `self.new_content = []`. New method, called from `service()` **outside** the playback-gated block that wraps `worker_updates`/`refresh_added`:

```python
def notify_new_content(self):
    """One toast per content type for the additions this cycle wrote."""
    while True:
        try:
            self.new_content.append(self.notify_output.get_nowait())
        except queue.Empty:
            break

    if not self.new_content:
        return

    if not settings.get_bool("notifyNewContent"):
        self.new_content = []
        return

    if self.added_pending():
        return          # more additions still landing this cycle

    if self.player.isPlayingVideo() and not xbmc.getCondVisibility(
        "VideoPlayer.Content(livetv)"
    ):
        return          # hold; the next tick after playback ends flushes it

    pending, self.new_content = self.new_content, []

    try:
        messages = newcontent.summarize(pending)
    except Exception:
        LOG.exception("could not summarize %s new item(s)", len(pending))
        return

    for message in messages:
        notification(message, time_ms=NEW_CONTENT_TIME)
```

The guard around `summarize` is not decoration: `service()` is called from `run()` under an `except Exception: break`, so an exception raised building a message would end the library thread until Kodi restarts. A toast is cosmetic; the sync behind it is not.

Why each gate is where it is:

**Queue drained unconditionally, first.** The accumulator is the thing that waits, never the queue — a held summary must not leave writer output sitting in a `Queue` that the next cycle then re-reads out of order.

**`added_pending()` is the cycle boundary**, and it is the same predicate `refresh_added` uses to decide new content is visible. So the toast lands with the content, not after the metadata backlog queued behind it drains — which on a large catch-up is minutes later. It is deliberately *not* the end-of-cycle `pending_refresh` block for that reason.

**Outside the playback gate.** Toasting over fullscreen video is exactly the intrusion the fork's `NotifyWorker` avoided. With `syncDuringPlay` off no additions get written during playback anyway; with it on, the summary sits in `new_content` and the first tick after playback ends flushes it. Because the call site is outside the `not isPlayingVideo()` block, that tick actually happens — inside it, a summary accumulated during playback would be stuck until the *next* sync cycle.

**Setting read at flush time**, so toggling it off mid-sync silences the cycle in flight, and toggling it on does not resurrect a cycle already gone.

### Deletions

`NotifyWorker`, `worker_notify`, `self.notify_threads` and the `NEW_MUSIC_TIME` constant all go: nothing else references them once the flush lives on the library thread, and `toast.show` never blocks or raises, so no worker thread is needed to carry it. `NEW_VIDEO_TIME` is renamed `NEW_CONTENT_TIME` (still 5000). Removing a fork class is in keeping with the file — `library.py` is the orchestrator, already heavily adapted, not the writer/kodidb transplant the "do not improve semantics" rule protects.

---

## What does *not* notify, and why that is right

- **A first full sync, or a newly selected library.** `FullSync` drives the writers directly and never calls `Library.added()`, so nothing reaches `notify_output`. A user who just added a 4 000-item library does not need "1247 movies added to library".
- **Metadata-only updates, artwork-only touches, userdata changes, removals.** Only the `added` writers are built with `notify_enabled`.
- **Watched items**, per the `Played` filter.
- **The recovery paths that *do* notify**: the update-mode prune's `self.library.added(missing_ids)` (`full_sync.py:877`) and websocket `LibraryChanged` `ItemsAdded` (`service/main.py:430`) both route through the added queue, so both toast. That is correct — from Kodi's side those items are new — and aggregation keeps a large repair to one line per type.

---

## Setting

`resources/settings.xml`, category `library`, group 1, **last** setting (after `syncStatus`):

```xml
<setting id="notifyNewContent" type="boolean" label="30622" help="30623">
  <level>0</level>
  <default>true</default>
  <control type="toggle"/>
</setting>
```

No `<dependencies>` — nothing to gate it on, and the Omega `list[string]` trap does not apply to a boolean, but the habit of not adding conditions without a reason is the cheap way to stay clear of it.

---

## Strings

`resources/language/resource.language.en_gb/strings.po`, appended after `#30621` in the existing comment-block style. Duplicate `msgid` text across `#30633`/`#30635` is legal (distinct `msgctxt`) and kept separate so translators can word artist and album lines differently.

| id | text |
|---|---|
| 30622 | Notify about new content |
| 30623 | Show a notification when new films, shows, episodes, music videos, artists or albums are added to your library. One notification per kind of content per sync; anything you have already watched is never announced, and a library you have just selected syncs quietly. |
| 30624 | %s movie added to library |
| 30625 | %s movies added to library |
| 30626 | %s show added to library |
| 30627 | %s new shows added to library |
| 30628 | %s episode of %s added to library |
| 30629 | %s episodes of %s added to library |
| 30630 | %s episodes added to library |
| 30631 | %s music video added to library |
| 30632 | %s music videos added to library |
| 30633 | %s added to music library |
| 30634 | %s artists added to library |
| 30635 | %s added to music library |
| 30636 | %s albums added to library |

New ids need a **full Kodi restart** to render, not a `dev-install.sh` bounce (string cache).

---

## Files touched

| File | Change |
|---|---|
| `lib/kofin/sync/newcontent.py` | **new** — `Entry`, `entry_for`, `summarize` |
| `lib/kofin/sync/library.py` | `new_content` accumulator, `notify_new_content()`, `service()` call site, `UpdateWorker.notify` re-enabled with the new payload, `NotifyWorker`/`worker_notify`/`notify_threads`/`NEW_MUSIC_TIME` removed, `NEW_VIDEO_TIME` → `NEW_CONTENT_TIME` |
| `resources/settings.xml` | `notifyNewContent` toggle, last line of the Library tab |
| `resources/language/…/strings.po` | ids 30622–30634 |
| `mypy.ini` | strict override for `kofin.sync.newcontent` |
| `tests/unit/test_new_content.py` | **new** — the whole message matrix, checked against the shipped strings |
| `tests/unit/test_sync_library.py` | collection, gating and flush behaviour |
| `tests/unit/test_sync_writers.py` | the notify payload as the real writers produce it |
| `docs/testing-plan.md` | live scenario (below) |
| `changelog.txt` | release bullet at cut time |

---

## Tests

### L1 — `tests/unit/test_new_content.py` (pure, no threads)

- one movie names it; two movies count; same for shows, artists, albums
- one episode of one show → the singular "episode of" line; six → the plural; episodes across two shows → the bare count
- one music video names it; two count
- a watched item (`UserData.Played` true) yields no entry, for each of the six types
- `Audio`, `BoxSet`, `Season` and an unknown type yield no entry, and a song never speaks for its album
- an item with no `UserData` block at all is an addition, not a watched one
- the same id offered twice counts once
- episodes of a show announced in the same batch are dropped from the episode line, and drop the episode line entirely when they were all of it
- an `Episode` with no `SeriesName`, and an item with an empty `Name`, yield no entry rather than a malformed toast
- message order across a mixed batch is movies → shows → episodes → music videos → artists → albums, at most one line per type
- every message id ships a string, and each template carries exactly the number of `%s` its call site passes (parsed out of `strings.po`)

### L1 — `tests/unit/test_sync_library.py` additions

- `notify_new_content` drains the queue but does not toast while `added_pending()` is true, and toasts once it goes false
- two writers reporting a movie each, across two calls, is still one message
- `notifyNewContent` off → queue drained, accumulator cleared, no toast
- playing video → held; the next call with playback stopped flushes it; `VideoPlayer.Content(livetv)` does not hold
- a second cycle's additions do not re-toast the first cycle's (accumulator cleared on flush)
- a `summarize` that raises costs the toast and not the library thread

### L2 — `tests/unit/test_sync_writers.py` additions

Against real Kodi schemas, both generations, so the payload under test is the one the writers actually hand over:

- an added writer (`notify_enabled=True`) writes both a watched and an unwatched movie and reports only the unwatched one
- a writer built the way the metadata path builds it reports nothing at all

Existing `UpdateWorker` constructions there are unaffected: `notify_enabled` keeps its `False` default.

### Live (`docs/testing-plan.md`, Phase 2 gate)

Against the test Jellyfin server, with the whitelist synced and the addon idle:

1. Add one unwatched film on the server → within a sync cycle, exactly one toast naming it.
2. Add three films → one toast, "3 movies added to library".
3. Add a film and mark it watched on the server before the cycle runs → no toast.
4. Add two episodes of one existing show → "2 episodes of X added to library"; add episodes across two shows → the bare count.
5. Add an album → the album line; confirm its tracks raise nothing.
6. Toggle the setting off, repeat (1) → nothing.
7. Start playback with `syncDuringPlay` on, add a film → nothing during playback, one toast shortly after it stops.

Evidence to `tests/live/results/` per the live-test conventions.

---

## Decisions

**D1 — the wording is deliberate.** The plural show line says "new shows" where the other plurals do not, and the music singulars say "music library" where their plurals say "library". Confirmed intentional and implemented as written.

**D2 — music videos are announced.** Two more strings (`#30631`/`#30632`) and a sixth line in the display order. Songs stay silent: one album is a dozen additions and its own line already says it arrived.

**D3 — episodes of a brand-new show are suppressed.** The show line is the one that says it is new; the episode line behind it would be the same news twice, and when its episodes were the only ones no episode line is raised at all.

**D4 — 5 s per toast**, all six lines, against the fork's 5 s video / 2 s music split. The short music toast existed because music notified per song; aggregation ends that. Worst case is six sequential toasts in one cycle.

---

## Live verification outstanding

`docs/testing-plan.md` carries this as **S-newcontent** in the Phase 2 gate, unrun. Everything above is covered by unit tests; what only a real Kodi can show is that the toasts appear when they should, read correctly, and stay out of the way during playback.
