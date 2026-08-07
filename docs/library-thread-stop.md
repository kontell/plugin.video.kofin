# Why the library thread sometimes would not stop

## The symptom

Occasionally, bouncing the addon left `CPythonInvoker(...): script didn't stop in 5 seconds - let's kill it` in the log, and Kodi's Python briefly unusable.
It was intermittent — five deliberate bounces in a row produced nothing — so the first pass fixed what could be seen: the teardown's wait for the library thread had no deadline at all, and `abortRequested` (which means Kodi is shutting down, not that the addon is being bounced) never ended it.
Bounding that wait (`service/main.LIBRARY_JOIN_SECONDS`) made the failure survivable and left it unexplained.

## Catching it in the act

`core/diag.py` dumps every thread's stack when the teardown's wait goes long: once at the first slow tick, once at the deadline, with a verdict on whether the thread being waited on moved between the two.
Blocked and merely-slow are indistinguishable from one dump and obvious from two.
It logs at warning level, because the event happens on a box whose owner has no reason to be running with debug logging on.

Reproduced by pointing a real sync-path GET at a socket that accepts and never answers — what a Jellyfin server looks like when its host disappears with the connection established — then bouncing the addon.
The dump named it on the first try:

```
--- kofin-library (id 140066339133120) [4 outer frame(s) omitted]
    File ".../kofin/core/http.py", line 130, in request
      response = self.session().request(
    ...
    File "/usr/lib/python3.14/http/client.py", line 297, in _read_status
      line = str(self.fp.readline(_MAXLINE + 1), "iso-8859-1")
    File "/usr/lib/python3.14/socket.py", line 729, in readinto
      return self._sock.recv_into(b)
```

## The root cause

`Http.request` retried a failed GET three times and nothing in that ladder consulted the stop flag.
Worst case is 4 attempts × (6 s connect + 30 s read) plus backoff — about 147 s of a thread that has already been told to stop. Measured end to end on Omega: **125 s** from the GET starting to the exception, and the library thread exited 5 s after that.

That is also why it was intermittent: on a healthy LAN every page fetch returns in well under a second, so a teardown has to land on a request that is *already* stalled to see it at all.

The library thread reaches that ladder two ways, and the second is easy to miss:

- directly, in its own `service()` tick or a `FullSync` pass — including the download pool's exit, where `ThreadPoolExecutor.shutdown(wait=True)` waits on in-flight page fetches that `abandon_jobs` cannot cancel;
- indirectly, behind `Library.database_lock`. Writer threads hold that lock for their whole drain (`UpdateWorker.run`) and make server calls inside it — trailers, special features, seasons, episodes-by-season, boxset members. A writer stalled in one of those holds the lock, and the library thread's own `acquire()` has neither timeout nor stop check.

Both reduce to the same unstoppable element, so one fix covers both.

## Two things the log corrected

**Kodi's "let's kill it" is not a kill.** It raises `abortRequested`. The teardown sees it on its first tick and gives up there, so on an addon bounce `LIBRARY_JOIN_SECONDS` is never reached — it governs only a soft restart (kofin's own restart IPC), where Kodi is not stopping the script and nothing else bounds the wait.

**A prompt teardown buys less than it looks like.** The service exited cleanly and Kodi went on to log `waiting on thread <id>` — it will not finalise a script while a thread that script created is alive. It does not hold the *replacement* back (a new service came up about a second later), but the stalled thread goes on running beside it for the rest of its retry ladder: two Library object graphs, two sets of database connections, for up to two minutes.

## The second bug, found while measuring the first

A teardown that cannot join its library thread ends with `state.clear_all(keep_stop=True)`, leaving `PROP_SYNC_STOP` raised so the orphan stays paused rather than resuming into a service that has already been rebuilt.
Nothing ever lowered it again.

So one stuck teardown disabled syncing **until Kodi was restarted**: every later library thread started, ran to its first `@stop` guard, and exited on `Should stop flag raised, exiting...` — one warning line, no dialog, no retry. Confirmed live, with the property read back off the running box as `"true"` long after the event.

It surfaced because it invalidated an experiment — the stall could not be armed a second time, because there was no library thread left to arm it in.

`run_forever` now lowers the flag before building each generation. What actually ends the orphan is `Library.stop_thread`, an instance flag the replacement cannot touch, which bounds it to the tick it is already in; the raised property only ever covered the gap before a replacement existed, and building one is the moment that gap closes.

## The fix for the first

`Http` takes an optional `abort` predicate, asked before each *retry* — never before the attempt already in flight, which must still be allowed to answer or a teardown would drop the last write of every session.
The service hands one to every transport it builds, including the per-worker sessions from `_new_api` that the download pool and the writers use.
`core/http.py` keeps its no-Kodi-imports property: the predicate is injected, not read there.

The predicate is a `threading.Event` owned by the service generation, **not** `state.should_stop`, and the difference was measured rather than reasoned about.
The first version read the property, and the A/B run showed no improvement at all: the replacement service lowers that property on its way up, which happens about ten seconds into a teardown, and the orphaned thread's next retry check therefore read "carry on" and rode the whole ladder again — 125 s, identical to the unfixed arm.
An Event owned by the generation that raised it cannot be un-raised by its successor.
Both signals are set on teardown and both are needed: the property is what the sync workers' `@stop` guards read, the Event is what the transports check.

## Measured

Same black-hole reproduction, same bounce; the arms differ only in whether the transport was handed the stop flag.

| | pre-fix | abort on `should_stop` | abort on the generation's Event |
|---|---|---|---|
| stalled GET gave up | t+125 s | t+124 s | **t+29 s** |
| orphaned library thread exited | t+130 s | t+130 s | **t+29 s** |
| replacement service up | t+9 s | t+10 s | t+9 s |
| a foreign addon's listing at t+0 | 5857 ms | 6033 ms | 5889 ms |
| the same listing, healthy | 807 ms | 828 ms | 684 ms |

The middle column is why the doc says *measured*: it looks like the fix and does nothing.

The last two rows are unchanged by any of it, and should be: that ~5 s is Kodi's own grace period before it raises `abortRequested`, not something the transport can shorten. What the fix buys is the row that matters — a thread orphaned by the teardown now lives 29 s instead of 130 s, bounded by the single read already in flight rather than by three replays of it.

An earlier reading of this claimed Kodi was blocked for ~58 s; that was wrong, and the A/B is what corrected it. The window in the first reproduction was the experiment's own polling loop, not Kodi refusing to do anything.

## What this leaves

A stalled request can still hold the thread for one read timeout after the stop flag goes up.
Shortening `DEFAULT_TIMEOUT`'s read budget would cut that further and is not obviously safe — a large `/Items` page on a slow server is legitimately slow — so it is left alone deliberately rather than by omission.
The thread dump stays in: if this recurs with a different stack, the log will say so without anyone having to reproduce it.
