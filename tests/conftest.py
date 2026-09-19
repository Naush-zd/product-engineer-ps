"""Shared test fixtures and fakes.

Tests are deterministic: a ``FakeClock`` replaces wall-clock time and a
``ScriptedDeliverer`` replaces the network. Together they let us drive the full
retry/backoff/exhaustion behavior by calling ``worker.tick()`` and advancing the
clock, with no sleeps and no sockets.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.clock import FakeClock
from app.config import Config
from app.deliverer import Outcome, OutcomeKind
from app.retry import RetryPolicy
from app.store import Store
from app.worker import Worker


class ScriptedDeliverer:
    """A fake ``Deliverer`` that returns pre-programmed outcomes in order.

    Once the script is exhausted, it keeps returning the last outcome, which is
    convenient for "always fails" scenarios. It records every call so tests can
    assert exactly how many HTTP attempts were made.
    """

    def __init__(self, script: list[Outcome]) -> None:
        self._script = script
        self.calls: list[dict] = []

    def deliver(self, url: str, event_id: str, body: dict) -> Outcome:
        self.calls.append({"url": url, "event_id": event_id, "body": body})
        index = min(len(self.calls) - 1, len(self._script) - 1)
        return self._script[index]


def ok(status: int = 200) -> Outcome:
    return Outcome(OutcomeKind.HTTP_RESPONSE, status_code=status, detail=f"HTTP {status}")


def http_fail(status: int = 500) -> Outcome:
    return Outcome(OutcomeKind.HTTP_RESPONSE, status_code=status, detail=f"HTTP {status}")


def transport_fail() -> Outcome:
    return Outcome(OutcomeKind.TRANSPORT_ERROR, detail="connection refused")


@dataclass
class Harness:
    """Everything a worker-level test needs, wired with fakes."""

    clock: FakeClock
    store: Store
    deliverer: ScriptedDeliverer
    worker: Worker
    config: Config

    def submit(self, event_id: str = "evt_1", type_: str = "incident.created") -> None:
        self.store.create_or_get_event(
            event_id, type_, "2026-01-01T00:00:00Z", {"incidentId": "inc_1"}
        )

    def view(self, event_id: str = "evt_1"):
        return self.store.get_view(event_id)


def make_harness(script: list[Outcome], **config_overrides) -> Harness:
    """Build a fully wired worker + store + fakes for a test."""
    clock = FakeClock()
    defaults = {
        "max_attempts": 3,
        "base_backoff_seconds": 10.0,
        "max_backoff_seconds": 1000.0,
        "claim_lease_seconds": 30.0,
        "db_path": ":memory:",
    }
    defaults.update(config_overrides)
    config = Config(**defaults)
    store = Store(clock, config.db_path)
    deliverer = ScriptedDeliverer(script)
    policy = RetryPolicy(
        max_attempts=config.max_attempts,
        base_backoff_seconds=config.base_backoff_seconds,
        max_backoff_seconds=config.max_backoff_seconds,
    )
    worker = Worker(store, deliverer, policy, clock, config)
    return Harness(clock=clock, store=store, deliverer=deliverer, worker=worker, config=config)


@pytest.fixture
def make():
    """Fixture returning the harness factory."""
    return make_harness
