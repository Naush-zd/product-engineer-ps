"""AC2: a temporary failure is retried and the eventual outcome is recorded.

Drives time with the fake clock: the retry only becomes due after the backoff
delay elapses, which we assert explicitly (a too-early tick does nothing).
"""

from __future__ import annotations

from app.models import DeliveryState
from tests.conftest import make_harness, http_fail, ok, transport_fail


def test_temporary_failure_then_success():
    # First call fails (503, retryable), second call succeeds.
    h = make_harness([http_fail(503), ok(200)], base_backoff_seconds=10.0)
    h.submit()

    # Tick 1: attempt fails, delivery is scheduled for retry.
    h.worker.tick()
    view = h.view()
    assert view.delivery.state is DeliveryState.RETRYING
    assert view.delivery.attempts == 1
    assert view.attempts[0].outcome == "retryable_failure"

    # Not yet due: a tick before the backoff elapses must not deliver.
    h.clock.advance(5)
    assert h.worker.tick() == 0
    assert len(h.deliverer.calls) == 1

    # Now due: advance past the 10s backoff and the retry succeeds.
    h.clock.advance(6)
    h.worker.tick()
    view = h.view()
    assert view.delivery.state is DeliveryState.SUCCEEDED
    assert view.delivery.attempts == 2
    assert [a.outcome for a in view.attempts] == ["retryable_failure", "success"]
    assert len(h.deliverer.calls) == 2


def test_transport_error_is_retried():
    h = make_harness([transport_fail(), ok(200)], base_backoff_seconds=10.0)
    h.submit()

    h.worker.tick()
    assert h.view().delivery.state is DeliveryState.RETRYING

    h.clock.advance(11)
    h.worker.tick()
    assert h.view().delivery.state is DeliveryState.SUCCEEDED
