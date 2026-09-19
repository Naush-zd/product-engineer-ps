"""HTTP API — the ingestion boundary and inspection surface.

Responsibilities kept deliberately thin:

* validate the incoming event contract (Pydantic),
* delegate idempotent ingestion to the store,
* expose delivery state + ordered attempt history for inspection,
* own the worker's lifecycle when the service runs (background thread).

The API does not perform deliveries itself; that is the worker's job. This keeps
ingestion latency independent of receiver health.
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from .clock import RealClock
from .config import Config
from .deliverer import HttpDeliverer
from .models import EventView
from .retry import RetryPolicy
from .store import Store
from .worker import Worker


class EventIn(BaseModel):
    """Incoming event contract. ``eventId`` is the caller's idempotency key."""

    eventId: str = Field(min_length=1)
    type: str = Field(min_length=1)
    occurredAt: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class AttemptOut(BaseModel):
    attemptNumber: int
    at: datetime
    outcome: str
    statusCode: int | None
    detail: str | None


class EventOut(BaseModel):
    """Read model: current delivery state plus full attempt history."""

    eventId: str
    type: str
    state: str
    attempts: int
    nextAttemptAt: datetime
    history: list[AttemptOut]


def _to_out(view: EventView) -> EventOut:
    return EventOut(
        eventId=view.event.event_id,
        type=view.event.type,
        state=view.delivery.state.value,
        attempts=view.delivery.attempts,
        nextAttemptAt=view.delivery.next_attempt_at,
        history=[
            AttemptOut(
                attemptNumber=a.attempt_number,
                at=a.at,
                outcome=a.outcome,
                statusCode=a.status_code,
                detail=a.detail,
            )
            for a in view.attempts
        ],
    )


def build_app(config: Config | None = None) -> FastAPI:
    """Construct the FastAPI app with a real clock, store, and background worker.

    Wiring lives here so tests can build their own app/worker with fakes instead.
    """
    config = config or Config.from_env()
    clock = RealClock()
    store = Store(clock, config.db_path)
    policy = RetryPolicy(
        max_attempts=config.max_attempts,
        base_backoff_seconds=config.base_backoff_seconds,
        max_backoff_seconds=config.max_backoff_seconds,
    )
    worker = Worker(store, HttpDeliverer(config.request_timeout_seconds), policy, clock, config)

    stop = threading.Event()

    def _run() -> None:
        # Wait on the stop event rather than sleeping blindly, so shutdown is
        # immediate instead of blocking for a full poll interval.
        while not stop.is_set():
            try:
                worker.tick()
            except Exception:  # pragma: no cover - resilience, logged by worker
                pass
            stop.wait(config.poll_interval_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        thread = threading.Thread(target=_run, name="webhook-worker", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2.0)

    app = FastAPI(title="Webhook Retry Engine", lifespan=lifespan)
    app.state.store = store
    app.state.config = config

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/events", status_code=202)
    def ingest(event: EventIn, response: Response) -> EventOut:
        view, created = store.create_or_get_event(
            event.eventId, event.type, event.occurredAt, event.payload
        )
        # 202 for a newly-accepted event, 200 when we recognized an existing id.
        response.status_code = 202 if created else 200
        assert view is not None
        return _to_out(view)

    @app.get("/events/{event_id}")
    def get_event(event_id: str) -> EventOut:
        view = store.get_view(event_id)
        if view is None:
            raise HTTPException(status_code=404, detail="event not found")
        return _to_out(view)

    return app
