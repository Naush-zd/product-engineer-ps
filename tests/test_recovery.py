"""Crash recovery: a delivery stranded IN_PROGRESS is reclaimed after its lease.

Simulates a worker that claimed a delivery and then died before recording a
result (the process crashed mid-flight). A later tick past the lease window must
return the delivery to RETRYING so it is not lost.
"""

from __future__ import annotations

from app.models import DeliveryState
from tests.conftest import make_harness, ok


def test_stale_in_progress_delivery_is_reclaimed():
    h = make_harness([ok(200)], claim_lease_seconds=30.0)
    h.submit()

    # Simulate a claim that never completed: claim the row but don't deliver.
    claimed = h.store.claim_due(h.clock.now())
    assert len(claimed) == 1
    assert h.view().delivery.state is DeliveryState.IN_PROGRESS
    assert len(h.deliverer.calls) == 0  # "crashed" before delivering

    # Before the lease expires, nothing is reclaimed.
    h.clock.advance(10)
    assert h.store.reclaim_stale(h.clock.now(), h.config.claim_lease_seconds) == 0

    # After the lease expires, the next tick reclaims and then delivers it.
    h.clock.advance(25)  # total 35s > 30s lease
    processed = h.worker.tick()

    assert processed == 1
    view = h.view()
    assert view.delivery.state is DeliveryState.SUCCEEDED
    assert len(h.deliverer.calls) == 1
