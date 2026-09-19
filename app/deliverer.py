"""The transport boundary: turning a delivery into one HTTP call.

The worker never touches ``httpx`` directly. It depends on the ``Deliverer``
protocol and receives a structured :class:`Outcome`, so the delivery mechanism
can be swapped for a fake in tests without any network. A ``Deliverer`` must
never raise for an expected transport problem (timeout, refused connection) —
it converts those into a ``TRANSPORT_ERROR`` outcome so the loop stays simple.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

import httpx


class OutcomeKind(str, Enum):
    """Classification-independent shape of what happened on the wire."""

    HTTP_RESPONSE = "http_response"  # we got a status code back
    TRANSPORT_ERROR = "transport_error"  # never reached / no response


@dataclass(frozen=True)
class Outcome:
    """Result of a single delivery attempt, before retry classification."""

    kind: OutcomeKind
    status_code: int | None = None
    detail: str | None = None


class Deliverer(Protocol):
    """Sends one event payload to the configured endpoint."""

    def deliver(
        self, url: str, event_id: str, body: dict[str, Any]
    ) -> Outcome:  # pragma: no cover - protocol
        ...


class HttpDeliverer:
    """Real HTTP deliverer built on httpx.

    Forwards the stable ``event_id`` as an ``X-Event-Id`` header so receivers can
    deduplicate at-least-once deliveries.
    """

    def __init__(self, timeout_seconds: float = 5.0) -> None:
        self._timeout = timeout_seconds

    def deliver(self, url: str, event_id: str, body: dict[str, Any]) -> Outcome:
        try:
            response = httpx.post(
                url,
                json=body,
                headers={"X-Event-Id": event_id},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            return Outcome(OutcomeKind.TRANSPORT_ERROR, detail=f"timeout: {exc}")
        except httpx.HTTPError as exc:
            return Outcome(OutcomeKind.TRANSPORT_ERROR, detail=f"transport: {exc}")
        return Outcome(
            OutcomeKind.HTTP_RESPONSE,
            status_code=response.status_code,
            detail=f"HTTP {response.status_code}",
        )
