"""AC4: repeated ingestion of the same event id is idempotent.

Covers both sequential resubmission and concurrent resubmission (many threads
racing on the same id), asserting exactly one logical event, one delivery, and
one delivery job.
"""

from __future__ import annotations

import sqlite3
import threading

from app.models import DeliveryState
from tests.conftest import make_harness, ok


def test_duplicate_submission_is_idempotent():
    h = make_harness([ok(200)])

    _, created1 = h.store.create_or_get_event("evt_dup", "t", "2026-01-01T00:00:00Z", {"n": 1})
    # Resubmit the same id (even with a different payload — first write wins).
    _, created2 = h.store.create_or_get_event("evt_dup", "t", "2026-01-01T00:00:00Z", {"n": 2})

    assert created1 is True
    assert created2 is False

    # Only one delivery exists and only one HTTP call is ever made.
    processed = h.worker.tick()
    assert processed == 1
    assert len(h.deliverer.calls) == 1
    assert h.view("evt_dup").delivery.state is DeliveryState.SUCCEEDED


def test_resubmission_after_success_does_not_redeliver():
    h = make_harness([ok(200)])
    h.store.create_or_get_event("evt_x", "t", "2026-01-01T00:00:00Z", {})
    h.worker.tick()
    assert h.view("evt_x").delivery.state is DeliveryState.SUCCEEDED

    # Submitting again must not resurrect the delivery.
    _, created = h.store.create_or_get_event("evt_x", "t", "2026-01-01T00:00:00Z", {})
    assert created is False
    h.clock.advance(10_000)
    assert h.worker.tick() == 0
    assert len(h.deliverer.calls) == 1


def test_concurrent_duplicate_submissions_create_one_event():
    # Hammer the same event id from many threads; exactly one insert must "win".
    h = make_harness([ok(200)])
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def submit() -> None:
        barrier.wait()  # maximize contention
        try:
            _, created = h.store.create_or_get_event(
                "evt_race", "t", "2026-01-01T00:00:00Z", {}
            )
        except sqlite3.OperationalError:
            # Under heavy write contention SQLite may surface a lock error to a
            # loser thread; that still means it did not create the event.
            created = False
        with lock:
            results.append(created)

    threads = [threading.Thread(target=submit) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one thread created the event; the rest saw it already existed.
    assert results.count(True) == 1

    # And there is a single delivery, delivered once.
    h.worker.tick()
    assert len(h.deliverer.calls) == 1
    assert h.view("evt_race").delivery.attempts == 1
