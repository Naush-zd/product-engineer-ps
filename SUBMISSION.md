# Product Engineering Challenge Submission

## Candidate

- **Name:** Nausheen Noor Zaidi
- **Email:** nausheennoorz16@gmail.com
- **GitHub:** https://github.com/Naush-zd
- **Selected problem:** Problem 2 — Webhook Retry Engine
- **Demo video:** <!-- TODO: Loom/YouTube/Drive link, placed here and near the top -->

## Run the project

**Prerequisites:** Python 3.11+ (developed on 3.14). No other services, no API keys.

```bash
make install     # creates .venv and installs pinned deps from requirements.txt
make test        # runs the full suite: deterministic, no network, ~0.3s
```

Equivalent without make:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

### Triggering the success + failure/recovery scenarios manually

Use two terminals — a configurable test receiver and the service:

```bash
# Terminal 1: test receiver on :9000
make receiver

# Terminal 2: service + background worker on :8000 (short backoff for a snappy demo)
WEBHOOK_URL=http://localhost:9000/hook BASE_BACKOFF_SECONDS=1 make run
```

**Successful delivery:**
```bash
curl -s -X POST localhost:9000/mode -d '{"mode":"always_ok"}' -H 'content-type: application/json'
curl -s -X POST localhost:8000/events -H 'content-type: application/json' \
  -d '{"eventId":"evt_ok","type":"incident.created","occurredAt":"2026-09-15T10:00:00Z","payload":{"severity":"high"}}'
curl -s localhost:8000/events/evt_ok | python3 -m json.tool   # -> state: succeeded
```

**Temporary failure → retry → success:**
```bash
curl -s -X POST localhost:9000/mode -d '{"mode":"fail_then_ok","failures":2}' -H 'content-type: application/json'
curl -s -X POST localhost:8000/events -H 'content-type: application/json' \
  -d '{"eventId":"evt_retry","type":"incident.created","occurredAt":"2026-09-15T10:00:00Z","payload":{}}'
sleep 5
curl -s localhost:8000/events/evt_retry | python3 -m json.tool   # -> 2 failed attempts, then success
```

**Bounded failure (exhaustion):** set mode `always_fail`, submit, and watch the state
settle on `failed_permanent` after `MAX_ATTEMPTS`.

**Idempotency:** submit the same `eventId` twice — the second returns HTTP `200`
(existing) rather than `202`, and `GET /received` on the receiver shows the event
delivered only once on success.

Full command walkthrough is in [`SOLUTION_README.md`](SOLUTION_README.md).

## Run the tests

```bash
make test
# or: pytest -q
```

38 tests, fully deterministic (a `FakeClock` and a scripted in-process deliverer
replace time and the network), no paid services, sub-second runtime. Coverage maps
directly to the required list: successful delivery, temporary failure + retry,
attempt exhaustion, repeated ingestion, plus retry-classification units, a crash-
recovery test, and API-boundary tests.

## Architecture and data flow

The service is one process with a clear pipeline: **ingestion → scheduling →
delivery → storage**. Each stage is a separate module depending on the next
through a narrow interface.

```
        POST /events                          GET /events/{id}
             │                                       │
             ▼                                       ▼
   ┌───────────────────┐   validation, idempotency,  read model
   │   api.py (FastAPI)│───────────────────────────────────────┐
   └─────────┬─────────┘                                        │
             │ create_or_get_event (idempotent on eventId)      │
             ▼                                                   │
   ┌───────────────────┐   durable state: events, deliveries,   │
   │   store.py (SQLite)│◄──── attempts; atomic claim; recovery ─┘
   └─────────┬─────────┘
             ▲ claim_due / record_attempt / update_delivery
             │
   ┌───────────────────┐   one scheduling pass per tick():
   │   worker.py        │   reclaim stale → claim due → deliver →
   │  (background loop) │   classify → succeed | retry | fail
   └─────────┬─────────┘
             │ deliver(url, event_id, body)          classify(outcome)
             ▼                                        ▲
   ┌───────────────────┐                    ┌───────────────────┐
   │ deliverer.py (httpx)│                   │ retry.py (pure)   │
   │  → Outcome          │                   │  backoff + policy │
   └───────────────────┘                     └───────────────────┘

   clock.py: injectable "now" (Real in prod, Fake in tests) used everywhere time matters
```

**Data model** (`models.py`): an `Event` (caller-supplied, keyed by stable
`event_id`) has exactly one `Delivery` (this service targets one endpoint), and a
`Delivery` accumulates ordered immutable `Attempt` records. Delivery state machine:

```
PENDING ──claim──> IN_PROGRESS ──2xx──────────────> SUCCEEDED        (terminal)
                       │
                       ├── retryable, attempts left ─> RETRYING ──claim──> IN_PROGRESS
                       ├── retryable, none left ─────> FAILED_PERMANENT   (terminal)
                       └── terminal 4xx ────────────> FAILED_PERMANENT   (terminal)

crash while IN_PROGRESS ── lease expiry ─> RETRYING   (recovered by reclaim_stale)
```

**Why a background worker separate from ingestion:** ingestion latency stays
independent of receiver health. A slow or dead endpoint never blocks the API; the
worker owns all delivery I/O and retry scheduling.

## Technology choices

- **Python + FastAPI.** Fastest to write clearly in the time budget; Pydantic gives
  the ingestion contract validation for free; the type-annotated, small-module style
  keeps responsibilities legible for a reviewer. FastAPI's `TestClient` makes the
  HTTP boundary testable without a running server.
- **SQLite (stdlib `sqlite3`).** The brief explicitly rules out a distributed queue.
  SQLite gives real durability (state survives restart), transactional writes, and a
  single-file setup with zero external services — ideal for a 10-minute reviewer
  setup. It is also honest about scope: this is a single-node engine.
- **httpx** for delivery: a clean, timeout-aware client behind a `Deliverer`
  protocol so tests substitute a fake with no sockets.
- **Injectable clock.** The single most important testing decision — see below.

**Alternatives considered:** Redis/RQ or Celery (rejected — a distributed queue is
out of scope and adds heavy setup); Postgres + `SELECT … FOR UPDATE SKIP LOCKED`
(the right production choice, noted below, but overkill and heavier to run here);
real `asyncio` timers for retries (rejected — makes tests depend on wall-clock
sleeps, exactly what the brief warns against).

## Important decisions

1. **Injectable clock, so retries are testable without sleeping.** All time-aware
   logic reads `now` from a `Clock`. Tests use `FakeClock.advance(seconds)` and call
   `worker.tick()` directly, so backoff, "not-yet-due", and exhaustion are asserted
   deterministically in milliseconds. This is what lets the suite avoid arbitrary
   sleep timing entirely.

2. **Retry classification is explicit, not "retry everything".** `retry.py` retries
   transport errors, `5xx`, and the transient `4xx`s (`408`, `425`, `429`); it treats
   other `4xx` (400/401/403/404/422…) as terminal because they will not fix
   themselves. Retrying permanent errors wastes attempts and delays the visible
   final state.

3. **Idempotency at the store, keyed on the caller's `event_id`.** `event_id` is the
   primary key; ingestion is `INSERT OR IGNORE` + read-back under a lock, so a repeat
   submission (sequential *or* concurrent) collapses to one event and one delivery
   job. The API signals this with `202` (newly accepted) vs `200` (already known).

## Assumptions and limitations

- **One configured endpoint**, per the brief. Multiple subscribers are out of scope.
- **Single process / single node.** The shared SQLite connection is serialized with a
  lock; `claim_due` uses a guarded UPDATE so ticks never double-claim, but true
  horizontal scaling would need a different store (below).
- **At-least-once delivery.** A receiver can still observe a duplicate (see next
  section); receivers must dedupe on `eventId`. The `X-Event-Id` header is sent to
  support that.
- **In-memory receiver counters** in the test receiver reset on restart — it is a
  demo aid, not part of the tested engine.
- Deliberately unbuilt: auth, dashboard, request signing, manual replay. The brief
  lists these as optional/secondary; the core delivery behavior was prioritized.

## Production and scale

The first thing I would change is the **store and claim mechanism**. SQLite + a lock
is correct for one node but serializes everything. For many workers I would move to
Postgres and claim with `SELECT … FOR UPDATE SKIP LOCKED` (or a real broker), letting
N workers pull disjoint batches without contention, and add a visibility-timeout
lease (the same idea as `reclaim_stale` here) so a crashed worker's row is retried.

Answers to the brief's questions:

- **What could still cause a receiver to observe a duplicate?** Delivery is
  at-least-once: if the receiver processes the request and returns 200 but the
  response is lost (crash/timeout after receipt), the worker sees a failure and
  retries, so the receiver gets the payload twice. Receivers must dedupe on
  `eventId`; we forward it as `X-Event-Id`.
- **How would you operate this with many workers?** Postgres + `SKIP LOCKED` claiming,
  a lease/visibility timeout for crash recovery, and a dead-letter state for
  exhausted deliveries. Workers become stateless and horizontally scalable.
- **How would you prevent one failing endpoint from consuming all capacity?** Per-
  endpoint concurrency limits and a circuit breaker: after consecutive failures, back
  the endpoint off / open the breaker so its retries don't starve healthy traffic.
  With one endpoint here it doesn't arise, but the RetryPolicy/worker split is where
  that logic would live.
- **What metrics and alerts in production?** Delivery success rate and attempt counts
  per endpoint, retry queue depth and age of oldest pending delivery, p50/p95
  delivery latency, and a count/rate of `failed_permanent` (dead-letter) with an alert
  when it climbs — plus receiver-side 5xx rate to catch a failing endpoint early.

## AI usage

<!-- TODO: adjust to reflect your actual usage before submitting. -->
I used Claude Code (Anthropic) as a pair-programming assistant to scaffold modules,
draft tests, and iterate on this document. I directed the architecture and decisions,
reviewed all generated code, and verified behavior by running the test suite and an
end-to-end HTTP demo (retry → success, idempotent resubmission, durability across a
process restart). I can explain and modify any part of the submission.

## Credibility note

**An AI-augmented API virtualization & resilience-testing platform**, shipped by my
team at **Warner Bros. Discovery**. Internal specifics are kept general; I was a
**core contributor** on the team that built and shipped it. I later rebuilt the same
system independently as a public reference implementation (see Evidence), so I can
speak to and demonstrate every part of the design.

**The problem it solved.** In an API/microservice-heavy environment, two failure
modes cost real engineering time: teams were blocked or had flaky tests when
upstream/third-party APIs were unavailable or unstable in non-prod, and
"worked in test, broke in prod" regressions slipped through because tests only
covered happy paths — a provider would quietly change a field's type, return `null`,
or empty an array and consumers would break. The platform lets a team point any spec
(GraphQL SDL, OpenAPI, AsyncAPI, Postman) at it and get a live mock for GraphQL,
REST, and event/async APIs, then uses an AI agent to (1) generate realistic mock
data, (2) inject production-like failure scenarios, and (3) auto-generate runnable
consumer contract tests that catch upstream breaking changes before production. The
core insight: the schema is a machine-readable contract, so one artifact yields three
outputs — a faithful mock, failure injection, and an executable test suite.

**My contribution.** As a core contributor I worked across the stack — a
TypeScript/React (Next.js App Router) frontend, a Node/Express JSON API, integration
with a mocking engine (Microcks) for multi-protocol playback, the LLM-backed data/
failure-injection agent, and the contract-test generator that emits runnable suites
(`node-test`/`jest`/`vitest`) — plus the Dockerized deployment and a benchmark
harness for coverage, generation latency, and regression-detection metrics.

**Scale / operational complexity.** A full-stack, multi-service system (frontend +
API + mocking engine + LLM agent) supporting three API protocols, per-user and
per-workspace scoping via request headers, and stateful scenario overrides persisted
across restarts — used to unblock development and harden integration testing across
multiple engineering teams. It runs an external LLM dependency in production safely by
treating it as optional rather than load-bearing (see below).

**One difficult engineering decision.** Making the AI features **deterministic-first
with the LLM strictly optional**. Both mock-data and contract-test generation run
fully offline with zero API keys using deterministic schema-walking + faker; the LLM
key only *enhances* output. The reasoning: demos, tests, and CI must never depend on a
third-party LLM being up, fast, or in budget, and LLM calls carry latency, cost, and
failure modes (timeouts, malformed JSON, rate limits). Gating on key availability and
degrading gracefully means a bad LLM day costs *quality*, not *availability* — the
same reliability principle behind the injectable-clock and fake-deliverer design in
this challenge submission.

**Evidence.** Public reference implementation I built independently, demonstrating the
same architecture and features: https://github.com/Naush-zd (Unified Mockserver).
