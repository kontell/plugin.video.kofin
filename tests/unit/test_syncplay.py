"""SyncPlay protocol math (ported fork suite) and the timesync window."""

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


class TestCorrectionLadder:
    def test_neutral_band_when_not_correcting(self):
        assert utils.correction_action(30.0, True) == (None, None, False)
        assert utils.correction_action(-200.0, True) == (None, None, False)
        # Just below the outer engage band: still neutral.
        assert utils.correction_action(340.0, True) == (None, None, False)

    def test_engages_past_outer_band(self):
        action, rate, correcting = utils.correction_action(400.0, True)
        assert action == "tempo"
        assert rate == 1.03  # capped at +3%
        assert correcting is True

        action, rate, correcting = utils.correction_action(-400.0, True)
        assert action == "tempo"
        assert rate == 0.97  # capped at -3%
        assert correcting is True

    def test_hysteresis_holds_until_inner_band(self):
        # Once engaged, keep correcting down through the neutral band...
        action, _, correcting = utils.correction_action(200.0, True, correcting=True)
        assert action == "tempo"
        assert correcting is True
        # ...and only release inside the small inner band.
        assert utils.correction_action(50.0, True, correcting=True) == (
            None,
            None,
            False,
        )

    def test_tempo_rate_is_capped(self):
        action, rate, _ = utils.correction_action(1400.0, True)
        assert action == "tempo"
        assert 0.97 <= rate <= 1.03

    def test_large_drift_tolerated_never_seeks(self):
        # Beyond the tempo band the loop tolerates and leaves any hard
        # re-sync to the server — it never returns a seek.
        assert utils.correction_action(1500.0, True) == (None, None, False)
        assert utils.correction_action(-5000.0, True, correcting=True) == (
            None,
            None,
            False,
        )

    def test_tolerance_override(self):
        # A tighter band engages sooner...
        action, _, _ = utils.correction_action(200.0, True, engage_ms=150.0)
        assert action == "tempo"
        # ...a relaxed band tolerates more before engaging.
        assert utils.correction_action(500.0, True, engage_ms=700.0) == (
            None,
            None,
            False,
        )

    def test_no_tempo_control_tolerates_everything(self):
        # Without rate control there is no correction to make (no seeking).
        assert utils.correction_action(300.0, False) == (None, None, False)
        assert utils.correction_action(900.0, False) == (None, None, False)

    def test_transcode_only_nudges_gross_drift(self):
        # Inside the segment quantum nothing happens even with tempo.
        assert utils.correction_action(1000.0, True, transcoding=True) == (
            None,
            None,
            False,
        )
        action, _, _ = utils.correction_action(2000.0, True, transcoding=True)
        assert action == "tempo"
        # Above the quantum-sized band it tolerates (never seeks).
        assert utils.correction_action(3500.0, True, transcoding=True) == (
            None,
            None,
            False,
        )


class FakeTimesyncManager:
    """get_utc_time provider + update observer for TimeSync (no thread)."""

    def __init__(self):
        self.updates = 0

    def get_utc_time(self):
        return None

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
