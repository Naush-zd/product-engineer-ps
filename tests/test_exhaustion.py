"""AC3: repeated failure stops at the attempt limit — retries do not run forever.

Also covers the terminal-4xx short-circuit: a non-retryable response fails
permanently on the first attempt without consuming the retry budget.
"""

from __future__ import annotations

from app.models import DeliveryState
from tests.conftest import make_harness, http_fail


def _run_until_settled(h, max_ticks: int = 50) -> None:
    """Advance the clock generously and tick until the delivery is terminal."""
    for _ in range(max_ticks):
        h.worker.tick()
        state = h.view().delivery.state
        if state in (DeliveryState.SUCCEEDED, DeliveryState.FAILED_PERMANENT):
            return
        h.clock.advance(10_000)  # jump past any backoff
    raise AssertionError("delivery never settled")


def test_bounded_failure_stops_at_max_attempts():
    # Always fails with a retryable status; max_attempts = 3.
    h = make_harness([http_fail(500)], max_attempts=3, base_backoff_seconds=10.0)
    h.submit()

    _run_until_settled(h)

    view = h.view()
    assert view.delivery.state is DeliveryState.FAILED_PERMANENT
    assert view.delivery.attempts == 3
    # Exactly max_attempts HTTP calls — no more, no fewer.
    assert len(h.deliverer.calls) == 3
    assert len(view.attempts) == 3
    assert all(a.outcome == "retryable_failure" for a in view.attempts)


def test_no_further_attempts_after_permanent_failure():
    h = make_harness([http_fail(500)], max_attempts=2, base_backoff_seconds=10.0)
    h.submit()
    _run_until_settled(h)
    assert h.view().delivery.state is DeliveryState.FAILED_PERMANENT

    # Further ticks, even far in the future, do nothing.
    h.clock.advance(1_000_000)
    assert h.worker.tick() == 0
    assert len(h.deliverer.calls) == 2


def test_terminal_response_fails_immediately_without_retry():
    # 400 is non-retryable: one attempt, straight to permanent failure.
    h = make_harness([http_fail(400)], max_attempts=5)
    h.submit()

    h.worker.tick()

    view = h.view()
    assert view.delivery.state is DeliveryState.FAILED_PERMANENT
    assert view.delivery.attempts == 1
    assert len(h.deliverer.calls) == 1
    assert view.attempts[0].outcome == "terminal_failure"
