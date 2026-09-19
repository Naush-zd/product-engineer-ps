"""Durable state: a small SQLite-backed repository.

The store is the single source of truth for delivery state, so behavior survives
a process crash or restart. It owns three responsibilities that are easy to get
wrong and therefore concentrated here:

* **Idempotent ingestion** — ``create_or_get_event`` uses the caller's
  ``event_id`` as a primary key, so submitting the same id twice yields one
  logical event and one delivery, even under concurrent requests.
* **Atomic claiming** — ``claim_due`` moves due deliveries to ``IN_PROGRESS`` in a
  single guarded UPDATE, so two worker ticks can never process the same delivery.
* **Crash recovery** — ``reclaim_stale`` returns deliveries whose worker died
  mid-flight (lease expired) back to ``RETRYING``.

SQLite is accessed with ``check_same_thread=False`` and a short busy timeout; all
writes go through small transactions. This is deliberately a single-node design
(see SUBMISSION.md for how it would change with many workers).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from .clock import Clock
from .models import (
    Attempt,
    CLAIMABLE_STATES,
    Delivery,
    DeliveryState,
    Event,
    EventView,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    payload      TEXT NOT NULL,
    received_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    event_id        TEXT PRIMARY KEY REFERENCES events(event_id),
    state           TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    claimed_at      TEXT,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deliveries_due
    ON deliveries(state, next_attempt_at);

CREATE TABLE IF NOT EXISTS attempts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL REFERENCES events(event_id),
    attempt_number INTEGER NOT NULL,
    at             TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    status_code    INTEGER,
    detail         TEXT
);

CREATE INDEX IF NOT EXISTS idx_attempts_event
    ON attempts(event_id, attempt_number);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Store:
    """SQLite repository for events, deliveries, and attempts."""

    def __init__(self, clock: Clock, db_path: str = ":memory:") -> None:
        self._clock = clock
        # One connection is shared by the API request threads and the worker
        # thread. sqlite3 connections are not safe for concurrent use, so every
        # public method serializes on this lock. That also makes multi-statement
        # operations (insert-then-read, select-then-claim) atomic with respect to
        # each other — the property the idempotency and claim logic relies on.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- Ingestion ---------------------------------------------------------

    def create_or_get_event(
        self, event_id: str, type_: str, occurred_at: str, payload: dict
    ) -> tuple[EventView, bool]:
        """Insert an event and its delivery, or return the existing one.

        Returns ``(view, created)`` where ``created`` is False when the event id
        was already known — that is the idempotent path. The ``INSERT OR IGNORE``
        plus read-back is atomic per statement, so concurrent duplicate submits
        collapse to a single event with a single delivery.
        """
        now = self._clock.now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO events "
                "(event_id, type, occurred_at, payload, received_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_id, type_, occurred_at, json.dumps(payload), _iso(now)),
            )
            created = cur.rowcount == 1
            if created:
                self._conn.execute(
                    "INSERT INTO deliveries "
                    "(event_id, state, attempts, next_attempt_at, updated_at) "
                    "VALUES (?, ?, 0, ?, ?)",
                    (event_id, DeliveryState.PENDING.value, _iso(now), _iso(now)),
                )
            return self._view_locked(event_id), created

    # -- Worker scheduling -------------------------------------------------

    def claim_due(self, now: datetime, limit: int = 20) -> list[Delivery]:
        """Atomically claim deliveries that are due, returning them for delivery.

        Each claimed row is flipped to ``IN_PROGRESS`` with a fresh ``claimed_at``
        under a guarded UPDATE, so a concurrent claim cannot pick the same row.
        """
        claimable = [s.value for s in CLAIMABLE_STATES]
        placeholders = ",".join("?" for _ in claimable)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT event_id FROM deliveries "
                f"WHERE state IN ({placeholders}) AND next_attempt_at <= ? "
                f"ORDER BY next_attempt_at LIMIT ?",
                (*claimable, _iso(now), limit),
            ).fetchall()

            claimed: list[Delivery] = []
            for row in rows:
                updated = self._conn.execute(
                    f"UPDATE deliveries SET state = ?, claimed_at = ?, updated_at = ? "
                    f"WHERE event_id = ? AND state IN ({placeholders})",
                    (
                        DeliveryState.IN_PROGRESS.value,
                        _iso(now),
                        _iso(now),
                        row["event_id"],
                        *claimable,
                    ),
                )
                if updated.rowcount == 1:
                    claimed.append(self._get_delivery(row["event_id"]))
            return claimed

    def reclaim_stale(self, now: datetime, lease_seconds: float) -> int:
        """Return deliveries stuck IN_PROGRESS past their lease to RETRYING.

        This is the crash-recovery path: if a worker died after claiming but
        before recording a result, the delivery would otherwise be stranded.
        """
        cutoff = _iso(now)
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, claimed_at FROM deliveries WHERE state = ?",
                (DeliveryState.IN_PROGRESS.value,),
            ).fetchall()
            reclaimed = 0
            for row in rows:
                claimed_at = row["claimed_at"]
                if claimed_at is None:
                    continue
                age = (now - _parse(claimed_at)).total_seconds()
                if age >= lease_seconds:
                    self._conn.execute(
                        "UPDATE deliveries SET state = ?, claimed_at = NULL, "
                        "updated_at = ? WHERE event_id = ? AND state = ?",
                        (
                            DeliveryState.RETRYING.value,
                            cutoff,
                            row["event_id"],
                            DeliveryState.IN_PROGRESS.value,
                        ),
                    )
                    reclaimed += 1
            return reclaimed

    # -- Result recording --------------------------------------------------

    def record_attempt(self, attempt: Attempt) -> None:
        """Append an immutable attempt record."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO attempts "
                "(event_id, attempt_number, at, outcome, status_code, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    attempt.event_id,
                    attempt.attempt_number,
                    _iso(attempt.at),
                    attempt.outcome,
                    attempt.status_code,
                    attempt.detail,
                ),
            )

    def update_delivery(
        self,
        event_id: str,
        state: DeliveryState,
        attempts: int,
        next_attempt_at: datetime,
    ) -> None:
        """Persist the delivery's new state after an attempt.

        Clears ``claimed_at`` because the in-progress lease is over.
        """
        now = self._clock.now()
        with self._lock:
            self._conn.execute(
                "UPDATE deliveries SET state = ?, attempts = ?, next_attempt_at = ?, "
                "claimed_at = NULL, updated_at = ? WHERE event_id = ?",
                (state.value, attempts, _iso(next_attempt_at), _iso(now), event_id),
            )

    # -- Reads -------------------------------------------------------------

    def get_view(self, event_id: str) -> EventView | None:
        """Return an event with its delivery state and ordered attempt history."""
        with self._lock:
            return self._view_locked(event_id)

    def _view_locked(self, event_id: str) -> EventView | None:
        """``get_view`` body; caller must already hold ``self._lock``."""
        event_row = self._conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if event_row is None:
            return None
        event = Event(
            event_id=event_row["event_id"],
            type=event_row["type"],
            occurred_at=event_row["occurred_at"],
            payload=json.loads(event_row["payload"]),
            received_at=_parse(event_row["received_at"]),
        )
        delivery = self._get_delivery(event_id)
        attempt_rows = self._conn.execute(
            "SELECT * FROM attempts WHERE event_id = ? ORDER BY attempt_number",
            (event_id,),
        ).fetchall()
        attempts = [
            Attempt(
                event_id=r["event_id"],
                attempt_number=r["attempt_number"],
                at=_parse(r["at"]),
                outcome=r["outcome"],
                status_code=r["status_code"],
                detail=r["detail"],
            )
            for r in attempt_rows
        ]
        return EventView(event=event, delivery=delivery, attempts=attempts)

    def _get_delivery(self, event_id: str) -> Delivery:
        row = self._conn.execute(
            "SELECT * FROM deliveries WHERE event_id = ?", (event_id,)
        ).fetchone()
        return Delivery(
            event_id=row["event_id"],
            state=DeliveryState(row["state"]),
            attempts=row["attempts"],
            next_attempt_at=_parse(row["next_attempt_at"]),
            claimed_at=_parse(row["claimed_at"]) if row["claimed_at"] else None,
            updated_at=_parse(row["updated_at"]) if row["updated_at"] else None,
        )
