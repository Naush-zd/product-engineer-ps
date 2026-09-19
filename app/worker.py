"""The delivery worker: the scheduler that drives deliveries to completion.

``tick`` is one deterministic pass over due work — reclaim stale claims, claim
due deliveries, deliver each once, classify the outcome, and either finish or
schedule the next retry. Tests call ``tick`` directly and advance a ``FakeClock``,
so the whole retry/backoff/exhaustion behavior is exercised with no real time and
no network. ``run_forever`` is a thin loop that calls ``tick`` on an interval for
production use.

The worker depends only on the ``Store``, a ``Deliverer`` protocol, a
``RetryPolicy``, and a ``Clock`` — none of the concrete transport or storage
details leak into the control flow.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from .clock import Clock
from .config import Config
from .deliverer import Deliverer
from .models import Attempt, Delivery, DeliveryState
from .retry import Classification, RetryPolicy
from .store import Store

log = logging.getLogger("webhook.worker")


class Worker:
    """Claims due deliveries and delivers them under the retry policy."""

    def __init__(
        self,
        store: Store,
        deliverer: Deliverer,
        policy: RetryPolicy,
        clock: Clock,
        config: Config,
    ) -> None:
        self._store = store
        self._deliverer = deliverer
        self._policy = policy
        self._clock = clock
        self._config = config

    def tick(self) -> int:
        """Run one scheduling pass. Returns the number of deliveries processed."""
        now = self._clock.now()
        self._store.reclaim_stale(now, self._config.claim_lease_seconds)
        due = self._store.claim_due(now)
        for delivery in due:
            self._deliver_once(delivery)
        return len(due)

    def _deliver_once(self, delivery: Delivery) -> None:
        """Perform a single attempt for a claimed delivery and persist the result."""
        view = self._store.get_view(delivery.event_id)
        if view is None:  # pragma: no cover - defensive; event always exists here
            return
        event = view.event
        attempt_number = delivery.attempts + 1

        body = {
            "eventId": event.event_id,
            "type": event.type,
            "occurredAt": event.occurred_at,
            "payload": event.payload,
        }
        outcome = self._deliverer.deliver(self._config.webhook_url, event.event_id, body)
        classification = self._policy.classify(outcome)
        now = self._clock.now()

        self._store.record_attempt(
            Attempt(
                event_id=event.event_id,
                attempt_number=attempt_number,
                at=now,
                outcome=classification.value,
                status_code=outcome.status_code,
                detail=outcome.detail,
            )
        )

        if classification is Classification.SUCCESS:
            self._finish(event.event_id, DeliveryState.SUCCEEDED, attempt_number, now)
            log.info("delivery succeeded event_id=%s attempt=%d", event.event_id, attempt_number)
            return

        if classification is Classification.TERMINAL:
            self._finish(
                event.event_id, DeliveryState.FAILED_PERMANENT, attempt_number, now
            )
            log.warning(
                "delivery failed permanently (terminal response) event_id=%s "
                "attempt=%d detail=%s",
                event.event_id,
                attempt_number,
                outcome.detail,
            )
            return

        # Retryable: schedule the next attempt if budget remains, else give up.
        if self._policy.has_attempts_left(attempt_number):
            delay = self._policy.backoff_seconds(attempt_number)
            next_at = now + timedelta(seconds=delay)
            self._store.update_delivery(
                event.event_id, DeliveryState.RETRYING, attempt_number, next_at
            )
            log.info(
                "delivery retry scheduled event_id=%s attempt=%d next_in=%.1fs detail=%s",
                event.event_id,
                attempt_number,
                delay,
                outcome.detail,
            )
        else:
            self._finish(
                event.event_id, DeliveryState.FAILED_PERMANENT, attempt_number, now
            )
            log.warning(
                "delivery failed permanently (attempts exhausted) event_id=%s attempt=%d",
                event.event_id,
                attempt_number,
            )

    def _finish(self, event_id: str, state: DeliveryState, attempts: int, now) -> None:
        # Terminal states park next_attempt_at at "now"; it is never read again.
        self._store.update_delivery(event_id, state, attempts, now)

    def run_forever(self) -> None:  # pragma: no cover - exercised via the app, not tests
        """Continuously process due deliveries. Used by the running service."""
        log.info("worker started poll_interval=%.2fs", self._config.poll_interval_seconds)
        while True:
            try:
                self.tick()
            except Exception:  # keep the loop alive; one bad delivery must not kill it
                log.exception("worker tick failed")
            time.sleep(self._config.poll_interval_seconds)
