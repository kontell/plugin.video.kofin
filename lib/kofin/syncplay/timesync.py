"""NTP-style clock sync against the server, ported from the fork verbatim
(``LazyLogger`` -> ``Logger``, ``manager.get_utc_time()`` now backed by the
kofin ``Api``)."""

import threading
from collections import deque

from kofin.core.log import Logger
from kofin.syncplay import utils

#################################################################################################

LOG = Logger(__name__)

#################################################################################################


class TimeSync(threading.Thread):
    """NTP-style clock sync against the server (SYNCPLAY.md §3).

    Keeps a sliding window of measurements over GET /GetUtcTime and
    trusts the one with the smallest round trip.

    offset_ms is (server clock - local clock); server_now_ms() converts
    the local clock to the server's.
    """

    def __init__(self, manager):
        threading.Thread.__init__(self, name="kofin-syncplay-timesync")
        self.daemon = True
        self.manager = manager
        self.samples = deque(maxlen=utils.TIMESYNC_WINDOW)  # type: deque
        self.offset_ms = 0.0
        self.rtt_ms = None
        self.ping_ms = None
        self._greedy_remaining = utils.TIMESYNC_GREEDY_COUNT
        self._stop_event = threading.Event()
        self._kick_event = threading.Event()
        self._lock = threading.Lock()

    def run(self):
        LOG.info("--->[ syncplay timesync ]")

        while not self._stop_event.is_set():
            try:
                self._measure()
            except Exception as error:  # never kill the loop
                LOG.warning("Time sync measurement failed: %s", error)

            if self._greedy_remaining > 0:
                self._greedy_remaining -= 1
                interval = utils.TIMESYNC_GREEDY_INTERVAL
            else:
                interval = utils.TIMESYNC_INTERVAL

            self._kick_event.wait(interval)
            self._kick_event.clear()

        LOG.info("---<[ syncplay timesync ]")

    def stop(self):
        self._stop_event.set()
        self._kick_event.set()

    def force_update(self, reset=False):
        """Re-measure greedily, e.g. on group join or wake from sleep."""
        if reset:
            with self._lock:
                self.samples.clear()

        self._greedy_remaining = utils.TIMESYNC_GREEDY_COUNT
        self._kick_event.set()

    def server_now_ms(self):
        return utils.local_ms() + self.offset_ms

    def server_now_iso(self):
        return utils.to_iso(self.server_now_ms())

    # --- measurement ---------------------------------------------------

    def _measure(self):
        t0 = utils.local_ms()
        response = self.manager.get_utc_time()
        t3 = utils.local_ms()

        if not response:
            return

        t1 = utils.parse_iso_ms(response.get("RequestReceptionTime"))
        t2 = utils.parse_iso_ms(response.get("ResponseTransmissionTime"))

        if t1 is None or t2 is None:
            LOG.warning("Unusable GetUtcTime response: %s", response)
            return

        offset, rtt = utils.ntp_sample(t0, t1, t2, t3)
        self._add_sample(offset, rtt)

    def _add_sample(self, offset, rtt):
        if rtt < 0:  # nonsense measurement (clock stepped mid-exchange)
            return

        with self._lock:
            self.samples.append((offset, rtt))
            best_offset, best_rtt = min(self.samples, key=lambda s: s[1])

        self.offset_ms = best_offset
        self.rtt_ms = best_rtt
        self.ping_ms = max(best_rtt / 2.0, 0.0)

        LOG.debug(
            "Time sync: offset %.1fms rtt %.1fms (window %s)",
            best_offset,
            best_rtt,
            len(self.samples),
        )
        self.manager.on_timesync_update()
