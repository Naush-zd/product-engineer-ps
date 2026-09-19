"""AC1: successful delivery marks the event succeeded and records one attempt."""

from __future__ import annotations

from app.models import DeliveryState
from tests.conftest import make_harness, ok


def test_successful_delivery_records_attempt_and_succeeds():
    h = make_harness([ok(200)])
    h.submit()

    processed = h.worker.tick()

    assert processed == 1
    view = h.view()
    assert view.delivery.state is DeliveryState.SUCCEEDED
    assert view.delivery.attempts == 1
    assert len(view.attempts) == 1
    assert view.attempts[0].outcome == "success"
    assert view.attempts[0].status_code == 200
    # Exactly one HTTP call was made.
    assert len(h.deliverer.calls) == 1


def test_succeeded_delivery_is_not_reprocessed():
    h = make_harness([ok(200)])
    h.submit()
    h.worker.tick()

    # A terminal delivery must not be claimed again on subsequent ticks.
    h.clock.advance(3600)
    processed = h.worker.tick()

    assert processed == 0
    assert len(h.deliverer.calls) == 1
