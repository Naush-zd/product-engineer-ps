"""Domain model: the data structures and states that flow through the engine.

An **Event** is the caller-supplied thing to deliver, identified by a stable
``event_id``. Each event has exactly one **Delivery** (this service targets one
configured endpoint), and a Delivery accumulates ordered **Attempt** records —
one per HTTP call made to the receiver.

The ``DeliveryState`` enum is the heart of the state machine; see
``docs``/SUBMISSION.md for the transition diagram.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class DeliveryState(str, Enum):
    """Lifecycle of a delivery.

    Transitions::

        PENDING ──claim──> IN_PROGRESS ──2xx────────> SUCCEEDED   (terminal)
                              │
                              ├──retryable, attempts left──> RETRYING ──claim──> IN_PROGRESS
                              │
                              ├──retryable, no attempts left─> FAILED_PERMANENT (terminal)
                              │
                              └──terminal 4xx──────────────> FAILED_PERMANENT  (terminal)

    A crashed IN_PROGRESS delivery is recovered back to RETRYING by lease expiry
    (see ``store.reclaim_stale``), so a process crash mid-delivery never strands
    an event.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED_PERMANENT = "failed_permanent"


#: States from which no further work is done.
TERMINAL_STATES = frozenset({DeliveryState.SUCCEEDED, DeliveryState.FAILED_PERMANENT})

#: States eligible to be claimed by the worker when their next_attempt_at is due.
CLAIMABLE_STATES = frozenset({DeliveryState.PENDING, DeliveryState.RETRYING})


@dataclass(frozen=True)
class Event:
    """A caller-supplied event to be delivered exactly once (logically).

    ``event_id`` is the caller's stable idempotency key.
    """

    event_id: str
    type: str
    occurred_at: str
    payload: dict[str, Any]
    received_at: datetime


@dataclass
class Delivery:
    """The delivery job for an event: current state and scheduling metadata."""

    event_id: str
    state: DeliveryState
    attempts: int
    next_attempt_at: datetime
    claimed_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Attempt:
    """An immutable record of one HTTP call made to the receiver."""

    event_id: str
    attempt_number: int
    at: datetime
    outcome: str  # "success" | "retryable_failure" | "terminal_failure"
    status_code: int | None
    detail: str | None


@dataclass
class EventView:
    """Read model returned by the API: the event plus its ordered history."""

    event: Event
    delivery: Delivery
    attempts: list[Attempt] = field(default_factory=list)
