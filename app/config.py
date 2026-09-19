"""Runtime configuration, sourced from environment variables.

No secrets live here; ``.env.example`` documents every variable name. Defaults
are chosen so the service runs locally with zero configuration, and tests
construct ``Config`` directly with small/deterministic values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """All tunables for the engine."""

    #: The single webhook endpoint deliveries are POSTed to.
    webhook_url: str = "http://localhost:9000/hook"
    #: Maximum number of HTTP attempts before a delivery becomes permanently failed.
    max_attempts: int = 5
    #: Base backoff in seconds; delay for attempt N is base * 2**(N-1).
    base_backoff_seconds: float = 2.0
    #: Cap on any single backoff delay.
    max_backoff_seconds: float = 300.0
    #: How often the worker polls the store for due deliveries.
    poll_interval_seconds: float = 0.5
    #: Per-request timeout when calling the receiver.
    request_timeout_seconds: float = 5.0
    #: How long a claim is valid before a crashed worker's delivery is reclaimed.
    claim_lease_seconds: float = 30.0
    #: SQLite database path. ":memory:" is used by tests.
    db_path: str = "webhooks.db"

    @classmethod
    def from_env(cls) -> "Config":
        """Build config from environment variables, falling back to defaults."""
        return cls(
            webhook_url=os.getenv("WEBHOOK_URL", cls.webhook_url),
            max_attempts=int(os.getenv("MAX_ATTEMPTS", cls.max_attempts)),
            base_backoff_seconds=float(
                os.getenv("BASE_BACKOFF_SECONDS", cls.base_backoff_seconds)
            ),
            max_backoff_seconds=float(
                os.getenv("MAX_BACKOFF_SECONDS", cls.max_backoff_seconds)
            ),
            poll_interval_seconds=float(
                os.getenv("POLL_INTERVAL_SECONDS", cls.poll_interval_seconds)
            ),
            request_timeout_seconds=float(
                os.getenv("REQUEST_TIMEOUT_SECONDS", cls.request_timeout_seconds)
            ),
            claim_lease_seconds=float(
                os.getenv("CLAIM_LEASE_SECONDS", cls.claim_lease_seconds)
            ),
            db_path=os.getenv("DB_PATH", cls.db_path),
        )
