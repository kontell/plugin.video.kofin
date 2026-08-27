"""One deferred-action primitive for the library thread's clocks (P2.3).

The service tick used to keep seven ``*_at`` / ``*_delay`` pairs, each
with its own arming, its own "is it time" test and its own backoff
arithmetic. They are all the same three ideas: a moment the action is due,
a delay that climbs a ladder between a floor and a ceiling while the
action keeps failing, and -- for the refresh settle -- a hold cap that
stops re-arming from postponing the action forever. ``Deferred`` is those
three ideas once; the clock is injected so a test can drive it.
"""

from datetime import datetime, timedelta
from typing import Callable, Optional


class Deferred:
    def __init__(
        self,
        floor: float,
        ceiling: Optional[float] = None,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.floor = floor
        self.ceiling = floor if ceiling is None else ceiling
        self.delay = floor
        self.due_at: Optional[datetime] = None
        self.hold_until: Optional[datetime] = None
        self._now = now

    # -- arming --------------------------------------------------------------

    def arm(self, delay: Optional[float] = None) -> None:
        """Due ``delay`` seconds from now (the current ladder rung by
        default)."""
        self.due_at = self._now() + timedelta(
            seconds=self.delay if delay is None else delay
        )

    def settle(self, window: float, cap: float) -> None:
        """Push the due moment out by ``window``; stamp the hold cap once,
        on the first settle, so a steady stream of re-arms cannot postpone
        the action past ``cap`` from that first one."""
        now = self._now()
        self.due_at = now + timedelta(seconds=window)

        if self.hold_until is None:
            self.hold_until = now + timedelta(seconds=cap)

    def disarm(self) -> None:
        self.due_at = None
        self.hold_until = None

    # -- the ladder ----------------------------------------------------------

    def escalate(self) -> None:
        """Climb one rung: double, capped at the ceiling."""
        self.delay = min(self.delay * 2, self.ceiling)

    def reset(self) -> None:
        self.delay = self.floor

    # -- asking --------------------------------------------------------------

    @property
    def armed(self) -> bool:
        return self.due_at is not None

    def due(self) -> bool:
        """Armed and the moment has come."""
        return self.due_at is not None and self._now() >= self.due_at

    def waiting(self) -> bool:
        """Armed and the moment has not come: the action must hold."""
        return self.due_at is not None and self._now() < self.due_at

    def capped(self) -> bool:
        """The hold cap has passed: fire whether or not the settle is out."""
        return self.hold_until is not None and self._now() >= self.hold_until
