"""The deferred-action primitive (P2.3, sync/clock.py)."""

from datetime import datetime, timedelta

from kofin.sync.clock import Deferred


class Clock:
    def __init__(self):
        self.now = datetime(2026, 8, 27, 12, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def test_unarmed_is_neither_due_nor_waiting():
    clock = Clock()
    deferred = Deferred(60, now=clock)

    assert not deferred.armed
    assert deferred.due() is False
    assert deferred.waiting() is False
    assert deferred.capped() is False


def test_arm_then_due_at_the_rung():
    clock = Clock()
    deferred = Deferred(60, now=clock)

    deferred.arm()
    assert deferred.waiting() and not deferred.due()

    clock.advance(59)
    assert deferred.waiting()

    clock.advance(1)
    assert deferred.due() and not deferred.waiting()

    deferred.disarm()
    assert not deferred.armed


def test_the_ladder_doubles_to_the_ceiling_and_resets_to_the_floor():
    deferred = Deferred(60, 1800, now=Clock())

    rungs = []
    for _ in range(7):
        rungs.append(deferred.delay)
        deferred.escalate()

    assert rungs == [60, 120, 240, 480, 960, 1800, 1800]

    deferred.reset()
    assert deferred.delay == 60


def test_arm_with_an_explicit_delay_leaves_the_ladder_alone():
    clock = Clock()
    deferred = Deferred(60, 1800, now=clock)
    deferred.escalate()

    deferred.arm(5)

    assert deferred.due_at == clock.now + timedelta(seconds=5)
    assert deferred.delay == 120


def test_settle_pushes_the_due_moment_and_stamps_the_cap_once():
    clock = Clock()
    deferred = Deferred(4, now=clock)

    deferred.settle(4, 15)
    first_cap = deferred.hold_until
    assert deferred.due_at == clock.now + timedelta(seconds=4)

    clock.advance(3)
    deferred.settle(4, 15)  # re-armed inside the window
    assert deferred.due_at == clock.now + timedelta(seconds=4)
    assert deferred.hold_until == first_cap  # the cap does not move

    clock.advance(3)
    assert deferred.waiting() and not deferred.capped()

    clock.advance(20)
    assert deferred.capped()

    deferred.disarm()
    assert deferred.hold_until is None


def test_a_single_floor_is_its_own_ceiling():
    deferred = Deferred(900, now=Clock())
    deferred.escalate()
    assert deferred.delay == 900
