# Webhook Retry Engine

A small, reliable backend service that accepts an event and delivers it to one
configured webhook endpoint — with recorded attempts, bounded exponential-backoff
retries, idempotent ingestion, and inspectable delivery history.

This is a solution to **Problem 2** of the Caygnus Product Engineering Challenge.
See [`SUBMISSION.md`](SUBMISSION.md) for architecture rationale, decisions, and
trade-offs.

## Quickstart (≈2 minutes)

```bash
make install     # create .venv and install deps
make test        # run the full suite (deterministic, no network, ~0.3s)
```

To see it deliver over real HTTP, use two terminals:

```bash
# Terminal 1 — a configurable test receiver on :9000
make receiver

# Terminal 2 — the service + background worker on :8000
WEBHOOK_URL=http://localhost:9000/hook BASE_BACKOFF_SECONDS=1 make run
```

## Try the acceptance scenarios by hand

```bash
# Make the receiver fail twice then succeed (demonstrates retry -> success)
curl -s -X POST localhost:9000/mode -H 'content-type: application/json' \
  -d '{"mode":"fail_then_ok","failures":2}'

# Submit an event
curl -s -X POST localhost:8000/events -H 'content-type: application/json' \
  -d '{"eventId":"evt_123","type":"incident.created",
       "occurredAt":"2026-09-15T10:00:00Z",
       "payload":{"incidentId":"inc_456","severity":"high"}}'

# Submit the SAME id again — idempotent, no second delivery (returns HTTP 200)
curl -i -X POST localhost:8000/events -H 'content-type: application/json' \
  -d '{"eventId":"evt_123","type":"incident.created",
       "occurredAt":"2026-09-15T10:00:00Z","payload":{}}'

# Inspect delivery state + ordered attempt history
curl -s localhost:8000/events/evt_123 | python3 -m json.tool

# Inspect what the receiver actually saw (unique events, delivery counts)
curl -s localhost:9000/received | python3 -m json.tool
```

Receiver modes: `always_ok`, `always_fail` (drives exhaustion → `failed_permanent`),
`fail_then_ok` (drives retry → success).

## API

| Method | Path                 | Purpose                                             |
| ------ | -------------------- | --------------------------------------------------- |
| `POST` | `/events`            | Ingest an event idempotently. `202` new, `200` dupe |
| `GET`  | `/events/{eventId}`  | Current delivery state + ordered attempt history    |
| `GET`  | `/healthz`           | Liveness                                            |

## Layout

```
app/
  models.py     # Event / Delivery / Attempt + DeliveryState machine
  store.py      # SQLite repo: idempotent ingest, atomic claim, crash recovery
  retry.py      # pure retry classification + exponential backoff
  deliverer.py  # HTTP transport boundary (Deliverer protocol + httpx impl)
  worker.py     # the scheduler loop: claim -> deliver -> record -> reschedule
  api.py        # FastAPI ingestion + inspection; owns worker lifecycle
  clock.py      # injectable clock (Real / Fake) for deterministic time
  config.py     # env-driven config
receiver/       # a configurable test receiver for the demo
tests/          # deterministic tests (FakeClock + scripted deliverer)
```

## Configuration

All optional; see [`.env.example`](.env.example) for names and defaults. Key knobs:
`WEBHOOK_URL`, `MAX_ATTEMPTS`, `BASE_BACKOFF_SECONDS`, `POLL_INTERVAL_SECONDS`,
`DB_PATH`.
