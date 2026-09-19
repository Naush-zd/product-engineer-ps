"""Retry classification and backoff scheduling — pure, deterministic policy.

This module owns the two decisions that define delivery reliability:

1. **Is an outcome retryable?** We retry transient problems (transport errors and
   a small, explicit set of HTTP statuses) and give up immediately on outcomes
   that will not improve on their own (most 4xx). Retrying everything wastes
   capacity on permanent errors; retrying nothing loses recoverable deliveries.
2. **When is the next attempt due?** Exponential backoff bounded by a cap.

Keeping this free of I/O and time-of-day makes it exhaustively unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .deliverer import Outcome, OutcomeKind


class Classification(str, Enum):
    """What the retry policy decided about an outcome."""

    SUCCESS = "success"
    RETRYABLE = "retryable_failure"
    TERMINAL = "terminal_failure"


#: HTTP statuses worth retrying even though they are 4xx: request timeout,
#: too-early, and rate limiting are all transient.
_RETRYABLE_4XX = frozenset({408, 425, 429})


@dataclass(frozen=True)
class RetryPolicy:
    """Configurable classification + backoff.

    ``max_attempts`` bounds the total number of HTTP calls, guaranteeing the loop
    terminates. ``base_backoff_seconds`` and ``max_backoff_seconds`` shape the
    delay curve.
    """

    max_attempts: int = 5
    base_backoff_seconds: float = 2.0
    max_backoff_seconds: float = 300.0

    def classify(self, outcome: Outcome) -> Classification:
        """Map a raw delivery outcome onto a retry decision."""
        if outcome.kind is OutcomeKind.TRANSPORT_ERROR:
            # Never reached the receiver — always worth another try.
            return Classification.RETRYABLE

        status = outcome.status_code
        assert status is not None  # HTTP_RESPONSE always carries a status
        if 200 <= status < 300:
            return Classification.SUCCESS
        if status in _RETRYABLE_4XX or 500 <= status < 600:
            return Classification.RETRYABLE
        # Other 4xx (400, 401, 403, 404, 422, ...) will not fix themselves.
        return Classification.TERMINAL

    def has_attempts_left(self, attempts_made: int) -> bool:
        """True if another attempt is permitted after ``attempts_made`` calls."""
        return attempts_made < self.max_attempts

    def backoff_seconds(self, attempts_made: int) -> float:
        """Delay before the next attempt, given how many attempts have been made.

        Attempt 1 already happened, so the delay before attempt 2 uses exponent 0,
        yielding ``base``; before attempt 3, ``base * 2``; and so on, capped.
        """
        exponent = max(0, attempts_made - 1)
        delay = self.base_backoff_seconds * (2**exponent)
        return min(delay, self.max_backoff_seconds)
