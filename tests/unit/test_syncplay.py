"""SyncPlay protocol math (ported fork suite) and the timesync window."""

import json

import kofin.syncplay.timesync as timesync_module
from kofin.syncplay import utils
from kofin.syncplay.timesync import TimeSync


class TestTimeConversions:
    def test_parse_iso_z(self):
        ms = utils.parse_iso_ms("1970-01-01T00:00:01.500Z")
        assert ms == 1500.0

    def test_parse_iso_offset(self):
        ms = utils.parse_iso_ms("1970-01-01T01:00:00.000+01:00")
        assert ms == 0.0

    def test_parse_iso_dotnet_seven_digits(self):
        # .NET DateTime serializes 7 fractional digits
        ms = utils.parse_iso_ms("1970-01-01T00:00:01.5000000Z")
        assert ms == 1500.0

    def test_parse_iso_no_fraction(self):
        assert utils.parse_iso_ms("1970-01-01T00:00:02Z") == 2000.0

    def test_parse_iso_naive_is_utc(self):
        assert utils.parse_iso_ms("1970-01-01T00:00:02") == 2000.0

    def test_parse_iso_invalid(self):
        assert utils.parse_iso_ms("not a date") is None
        assert utils.parse_iso_ms(None) is None
        assert utils.parse_iso_ms("") is None

    def test_to_iso_round_trip(self):
        now_ms = 1750000000123.0
        assert abs(utils.parse_iso_ms(utils.to_iso(now_ms)) - now_ms) < 1.0

    def test_ticks(self):
        assert utils.seconds_to_ticks(1.5) == 15000000
        assert utils.ticks_to_ms(15000000) == 1500.0
        assert utils.ms_to_ticks(1500.0) == 15000000


class TestNtpMath:
    def test_symmetric_path(self):
        # Server 100ms ahead, 20ms RTT split evenly.
        offset, rtt = utils.ntp_sample(0.0, 110.0, 112.0, 22.0)
        assert rtt == 20.0
        assert offset == 100.0

    def test_zero_offset(self):
        offset, rtt = utils.ntp_sample(0.0, 5.0, 6.0, 11.0)
        assert rtt == 10.0
        assert offset == 0.0


class TestCommandExtrapolation:
    def test_on_time(self):
        # Command at position 10s scheduled for server time 5000ms,
        # evaluated exactly at 5000ms.
        assert utils.command_position_ms(100000000, 5000.0, 5000.0) == 10000.0

    def test_late(self):
        # Evaluated 750ms late: position advanced accordingly.
        assert utils.command_position_ms(100000000, 5000.0, 5750.0) == 10750.0

    def test_never_negative_elapsed(self):
        # Evaluated before When (scheduled in the future): no rewind.
        assert utils.command_position_ms(100000000, 5000.0, 4000.0) == 10000.0


class FakeTimesyncManager:
    """get_utc_time provider + update observer for TimeSync (no thread)."""

    def __init__(self, ws_target=None):
        self.updates = 0
        self.ws_target = ws_target

    def get_utc_time(self):
        return None

    def can_ws_timesync(self):
        return self.ws_target is not None

    def timesync_ws_target(self):
        return self.ws_target

    def on_timesync_update(self):
        self.updates += 1


class TestTimesyncWindow:
    """The min-RTT-of-8 sliding window: the sample with the smallest round
    trip wins, negative RTTs are discarded, and force_update(reset=True)
    drops history (a stale offset after sleep must not survive)."""

    def make(self):
        return TimeSync(FakeTimesyncManager())

    def test_min_rtt_sample_wins(self):
        sync = self.make()
        sync._add_sample(100.0, 40.0)
        sync._add_sample(250.0, 90.0)  # worse RTT: ignored for the offset
        sync._add_sample(120.0, 20.0)  # best RTT: trusted
        assert sync.offset_ms == 120.0
        assert sync.rtt_ms == 20.0
        assert sync.ping_ms == 10.0

    def test_negative_rtt_discarded(self):
        sync = self.make()
        sync._add_sample(100.0, 40.0)
        sync._add_sample(999.0, -5.0)  # clock stepped mid-exchange
        assert sync.offset_ms == 100.0
        assert len(sync.samples) == 1

    def test_window_is_bounded(self):
        sync = self.make()
        for i in range(20):
            sync._add_sample(float(i), 100.0 + i)
        assert len(sync.samples) == utils.TIMESYNC_WINDOW

    def test_old_best_sample_ages_out(self):
        sync = self.make()
        sync._add_sample(50.0, 10.0)  # the best, but will age out
        for i in range(utils.TIMESYNC_WINDOW):
            sync._add_sample(200.0 + i, 60.0 + i)
        assert sync.offset_ms == 200.0
        assert sync.rtt_ms == 60.0

    def test_updates_reported_to_manager(self):
        sync = self.make()
        sync._add_sample(100.0, 40.0)
        assert sync.manager.updates == 1

    def test_force_update_reset_clears_window(self):
        sync = self.make()
        sync._add_sample(100.0, 40.0)
        sync.force_update(reset=True)
        assert len(sync.samples) == 0
        assert sync._greedy_remaining == utils.TIMESYNC_GREEDY_COUNT
        assert sync._kick_event.is_set()

    def test_measure_uses_ntp_shape(self, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(utils, "local_ms", lambda: clock["t"])

        class Manager(FakeTimesyncManager):
            def get_utc_time(self):
                clock["t"] += 20.0  # 20ms to reach the server
                return {
                    "RequestReceptionTime": utils.to_iso(clock["t"] + 100.0),
                    "ResponseTransmissionTime": utils.to_iso(clock["t"] + 101.0),
                }

        sync = TimeSync(Manager())
        clock["t"] += 0  # t0 = 1000
        sync._measure()
        # RTT ~= 40ms wall (t3-t0) minus 1ms server hold; offset ~= +100ms.
        assert sync.rtt_ms is not None
        assert abs(sync.offset_ms - 100.0) < 25.0

    def test_unusable_response_ignored(self):
        class Manager(FakeTimesyncManager):
            def get_utc_time(self):
                return {"RequestReceptionTime": "garbage"}

        sync = TimeSync(Manager())
        sync._measure()
        assert len(sync.samples) == 0


class TestVersionGating:
    def test_v1_none_is_never_stale(self):
        assert not utils.is_stale_version(None, 5)
        assert not utils.is_stale_version(3, None)

    def test_lower_is_stale(self):
        assert utils.is_stale_version(4, 5)

    def test_equal_and_higher_are_fresh(self):
        assert not utils.is_stale_version(5, 5)
        assert not utils.is_stale_version(6, 5)


class FakeTimesyncSocket:
    """Echoes the TimeSync exchange (T1/T2 = t0 + server_ahead_ms)."""

    def __init__(self, server_ahead_ms=100, fail=False):
        self.server_ahead_ms = server_ahead_ms
        self.fail = fail
        self.sent = []
        self.closed = False

    def send(self, raw):
        if self.fail:
            raise OSError("socket gone")

        self.sent.append(json.loads(raw))

    def recv(self):
        t0 = self.sent[-1]["Data"]
        stamp = t0 + self.server_ahead_ms
        return json.dumps(
            {"MessageType": "TimeSync", "Data": {"T0": t0, "T1": stamp, "T2": stamp}}
        )

    def close(self):
        self.closed = True


class TestTimesyncWebSocket:
    """The dedicated-socket exchange (plugin binding): preferred when the
    server advertises it, HTTP fallback on any failure, one connection
    reused across measurements and closed on stop."""

    def make(self, monkeypatch, sock):
        manager = FakeTimesyncManager(
            ws_target=("ws://server/SyncPlay/TimeSync", "auth")
        )
        connects = []

        def create_connection(url, timeout, header):
            connects.append((url, timeout, header))
            return sock

        monkeypatch.setattr(
            timesync_module.websocket, "create_connection", create_connection
        )
        return TimeSync(manager), connects

    def test_ws_exchange_produces_a_sample(self, monkeypatch):
        sync, connects = self.make(monkeypatch, FakeTimesyncSocket(server_ahead_ms=250))

        sync._measure()

        assert len(connects) == 1
        assert len(sync.samples) == 1
        assert abs(sync.offset_ms - 250) < 50  # loopback rtt is ~0

    def test_connection_reused_across_measurements(self, monkeypatch):
        sync, connects = self.make(monkeypatch, FakeTimesyncSocket())

        sync._measure()
        sync._measure()

        assert len(connects) == 1
        assert len(sync.samples) == 2

    def test_exchange_failure_falls_back_to_http_and_closes(self, monkeypatch):
        sock = FakeTimesyncSocket(fail=True)
        sync, connects = self.make(monkeypatch, sock)
        http_calls = []
        sync.manager.get_utc_time = lambda: http_calls.append(1)

        sync._measure()

        assert sock.closed
        assert http_calls  # fell back to GET /GetUtcTime

    def test_an_incomplete_reply_falls_back_to_http(self, monkeypatch):
        """A reply that echoes T0 and omits T1/T2 — a partial server-side
        implementation — raised KeyError out of _measure, and the HTTP
        fallback that exists for a socket that is not working was never
        reached that cycle (audit R9). The clock offset then stayed at 0.0
        on a fresh join, the worst moment for it."""

        class Partial(FakeTimesyncSocket):
            def recv(self):
                t0 = self.sent[-1]["Data"]
                return json.dumps({"MessageType": "TimeSync", "Data": {"T0": t0}})

        sync, _ = self.make(monkeypatch, Partial())
        http_calls = []
        sync.manager.get_utc_time = lambda: http_calls.append(1)

        sync._measure()

        assert http_calls  # fell back to GET /GetUtcTime this cycle

    def test_no_transport_uses_http(self, monkeypatch):
        manager = FakeTimesyncManager()  # no ws target advertised
        http_calls = []
        manager.get_utc_time = lambda: http_calls.append(1)

        TimeSync(manager)._measure()

        assert http_calls

    def test_stop_closes_the_socket(self, monkeypatch):
        sock = FakeTimesyncSocket()
        sync, _ = self.make(monkeypatch, sock)

        sync._measure()
        sync.stop()

        assert sock.closed
