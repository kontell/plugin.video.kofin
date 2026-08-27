"""The one fake of the host port every full-sync test uses -- the slice of
Library a FullSync speaks (see the note above Library.claim): the claim,
the locks, the three enqueue entry points, the refresh and the
bookkeeping, all recorded."""

import threading


class FakeHost:
    def __init__(self, claim_ok=True):
        self.database_lock = threading.Lock()
        self.music_database_lock = threading.Lock()
        self.claim_ok = claim_ok
        self.claimed = False
        self.released = 0
        self.calls = {"removed": [], "added": [], "updated": []}
        self.refreshed = []
        self.forced = []
        self.watermark_stamps = 0
        self.playlist_polls_deferred = 0
        self.sync_failure_toasted = set()

    def claim(self):
        if self.claimed or not self.claim_ok:
            return False
        self.claimed = True
        return True

    def release(self):
        self.claimed = False
        self.released += 1

    def added(self, ids):
        self.calls["added"].extend(ids)

    def updated(self, ids):
        self.calls["updated"].extend(ids)

    def removed(self, ids):
        self.calls["removed"].extend(ids)

    def refresh_libraries(self, databases, force_reload=False):
        self.refreshed.append(set(databases))
        self.forced.append(force_reload)

    def stamp_watermark_if_empty(self):
        self.watermark_stamps += 1

    def defer_playlist_poll(self):
        self.playlist_polls_deferred += 1
