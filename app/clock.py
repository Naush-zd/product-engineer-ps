"""Time abstraction.

All time-dependent logic (backoff scheduling, claim leases) reads "now" from a
``Clock`` rather than calling ``time``/``datetime`` directly. Production uses
``RealClock``; tests use ``FakeClock`` to advance time deterministically without
real sleeps, which is what makes retry/backoff behavior testable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """Source of the current UTC time."""

    def now(self) -> datetime:  # pragma: no cover - trivial protocol
        ...


class RealClock:
    """Wall-clock time in UTC."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """Manually-advanced clock for deterministic tests.

    Time only moves when a test calls :meth:`advance`, so retry scheduling can be
    exercised precisely with no real waiting.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)
