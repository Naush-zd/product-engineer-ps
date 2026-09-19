"""AC5 + ingestion contract at the HTTP boundary, via FastAPI's TestClient.

These tests exercise the API's own responsibilities — validation, idempotent
ingestion, and inspection of state + history — without asserting on the
background worker's timing. The worker's delivery behavior is covered
deterministically at the unit level in the other test modules.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import build_app
from app.config import Config


def _client() -> TestClient:
    # A long poll interval keeps the background worker from racing the assertions;
    # in-memory DB isolates each test app.
    config = Config(db_path=":memory:", poll_interval_seconds=3600, max_attempts=3)
    return TestClient(build_app(config))


def _event(event_id: str = "evt_api_1") -> dict:
    return {
        "eventId": event_id,
        "type": "incident.created",
        "occurredAt": "2026-09-15T10:00:00Z",
        "payload": {"incidentId": "inc_456", "severity": "high"},
    }


def test_ingest_returns_202_and_pending_state():
    with _client() as client:
        resp = client.post("/events", json=_event())
        assert resp.status_code == 202
        body = resp.json()
        assert body["eventId"] == "evt_api_1"
        assert body["state"] == "pending"
        assert body["attempts"] == 0
        assert body["history"] == []


def test_duplicate_ingest_returns_200_and_same_event():
    with _client() as client:
        first = client.post("/events", json=_event("evt_dupe"))
        second = client.post("/events", json=_event("evt_dupe"))
        assert first.status_code == 202  # newly accepted
        assert second.status_code == 200  # recognized existing id
        assert second.json()["eventId"] == "evt_dupe"


def test_get_event_returns_state_and_history():
    with _client() as client:
        client.post("/events", json=_event("evt_get"))
        resp = client.get("/events/evt_get")
        assert resp.status_code == 200
        body = resp.json()
        assert body["eventId"] == "evt_get"
        assert "state" in body and "history" in body


def test_get_unknown_event_is_404():
    with _client() as client:
        assert client.get("/events/does_not_exist").status_code == 404


def test_invalid_event_is_rejected():
    with _client() as client:
        # Missing required eventId -> 422 from Pydantic validation.
        resp = client.post("/events", json={"type": "t", "occurredAt": "x", "payload": {}})
        assert resp.status_code == 422


def test_healthz():
    with _client() as client:
        assert client.get("/healthz").json() == {"status": "ok"}
