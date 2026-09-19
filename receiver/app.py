"""Configurable test receiver for demonstrating delivery, retry, and idempotency.

Run with ``python -m receiver.app`` (listens on :9000). It supports three modes,
switchable at runtime so the demo can show each acceptance scenario without
restarting anything:

* ``/hook``            — the delivery target; behavior depends on the current mode.
* ``POST /mode``       — set behavior: ``always_ok`` | ``always_fail`` |
                         ``fail_then_ok`` (with ``failures`` count).
* ``GET  /received``   — inspect what was delivered, including duplicate detection
                         by ``X-Event-Id`` (demonstrates at-least-once + receiver
                         dedup responsibility).

This receiver is intentionally not part of the automated test suite; the engine's
own tests use an in-process fake deliverer instead.
"""

from __future__ import annotations

from collections import defaultdict

from fastapi import FastAPI, Header, Request, Response
from pydantic import BaseModel

app = FastAPI(title="Test Webhook Receiver")


class _State:
    mode: str = "always_ok"
    failures: int = 2  # for fail_then_ok: fail this many times, then succeed
    seen_counts: dict[str, int] = defaultdict(int)  # event_id -> delivery count
    attempts_so_far: dict[str, int] = defaultdict(int)


state = _State()


class ModeIn(BaseModel):
    mode: str
    failures: int | None = None


@app.post("/mode")
def set_mode(body: ModeIn) -> dict[str, object]:
    state.mode = body.mode
    if body.failures is not None:
        state.failures = body.failures
    state.attempts_so_far.clear()
    return {"mode": state.mode, "failures": state.failures}


@app.post("/hook")
async def hook(
    request: Request, response: Response, x_event_id: str | None = Header(default=None)
) -> dict[str, object]:
    key = x_event_id or "unknown"
    state.attempts_so_far[key] += 1

    if state.mode == "always_fail":
        response.status_code = 500
        return {"ok": False, "reason": "forced failure"}

    if state.mode == "fail_then_ok" and state.attempts_so_far[key] <= state.failures:
        response.status_code = 503
        return {"ok": False, "reason": f"forced failure {state.attempts_so_far[key]}"}

    # Success path — record the delivery. A repeated X-Event-Id here is exactly
    # the at-least-once duplicate a receiver must tolerate by deduping on the id.
    state.seen_counts[key] += 1
    duplicate = state.seen_counts[key] > 1
    return {"ok": True, "eventId": key, "deliveryCount": state.seen_counts[key], "duplicate": duplicate}


@app.get("/received")
def received() -> dict[str, object]:
    return {
        "mode": state.mode,
        "uniqueEvents": len(state.seen_counts),
        "deliveryCounts": dict(state.seen_counts),
    }


def main() -> None:  # pragma: no cover - demo helper
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=9000, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
