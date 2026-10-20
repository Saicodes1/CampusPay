# Actual verification report

Verified locally on **20 October 2026 (Asia/Dubai)**. This report distinguishes automated tests, runnable-stack checks, and things that were not tested. No hosted GitHub Actions execution is claimed.

## Automated tests actually run

```sh
uv run --locked pytest -q --cov=campuspay --cov-report=term-missing --cov-report=json:docs/coverage.json --junitxml=docs/test-results.xml
```

**65 Python tests passed, 0 failed, 0 skipped.** The final run executed directly from `E:/Projects/Distributed systems payment application` in 22.19 seconds. Measured statement coverage: **85%** across the `campuspay` Python package. Payment-service and student-route modules reached 100% statement coverage; coverage is not proof of correctness and does not include JavaScript. Machine-readable evidence is in `docs/test-results.xml` and `docs/coverage.json`.

Environment: Windows host, CPython 3.12.14, uv 0.12.18, Docker Desktop Linux engine 29.7.2; PostgreSQL 16-alpine, Redis 7.4-alpine, RabbitMQ 4.1-management-alpine. The application image also ran successfully on its Python 3.12-slim Linux base (CPython 3.12.15 at build time).

| File | Executed tests | What was exercised |
|---|---:|---|
| `tests/test_unit.py` | 16 | Strict integer money boundaries; invalid floats/strings/bools/nulls; extra fields; HMAC tampering and age; short secret and invalid webhook configuration |
| `tests/test_payments.py` | 19 | Full money lifecycle; idempotency and mismatches; business-failure retry; real DB concurrent spends, duplicate keys, opposite transfers, same-intent authorizations, capture/void race, concurrent refunds; expiry; settlement/refund insufficiency; authorization/ownership; rollback and database invariants |
| `tests/test_api.py` | 7 | HTTP authentication/roles/ownership, required headers, strict validation, QR SVG, HTTP lifecycle, ledger cursors, reconciliation, real Redis limit enforcement, injected Redis outage, browser-origin protection, request IDs, OpenAPI security metadata |
| `tests/test_delivery.py` | 14 | Confirmed publication through a real RabbitMQ queue; duplicate consumption; crash after publish; unreachable broker endpoint; bounded outbox retry/dead/replay; transient consumer retry; actual DLQ; actual lost-ack redelivery; inbox/job rollback; real HTTP webhook signatures/500/retry/302/dead/replay; injected timeout; valid/invalid DLQ replay command |
| `tests/test_dashboard.py` | 9 | Static dashboard/CSP/assets; same-origin acceptance and cross/opaque-origin rejection; server-priced checkout and refund; price/payer tampering prevention; failed-purchase rollback; student-only access; descending activity cursors and ownership; authorization visibility; 10 concurrent catalog purchases without overspend |

The concurrency tests launch up to **20 concurrent operations** through separate PostgreSQL connections and service instances. In the overspend test, a 1,000-cent balance receives twenty 100-cent spend attempts: exactly ten succeed, ten fail for insufficient available funds, and the sender ends at zero. Twenty identical concurrent funding requests produce one journal and one event. These are correctness assertions, not throughput measurements.

## Nature of failure tests

Real infrastructure was used for PostgreSQL constraints/locking, Redis rate-limit counters, RabbitMQ publisher confirmations/queues/acknowledgements/DLQ, and an HTTP server verifying received webhook signatures. Failure injection was explicit:

* A `RuntimeError` after inserting an outbox event tests pre-commit atomic rollback.
* A `SystemExit` immediately after a real confirmed RabbitMQ publish tests the publish/commit crash window; this is an in-process crash simulation, not an OS kill.
* Closing a real AMQP channel before acknowledgement tests actual broker redelivery.
* The broker-outage test uses a genuinely unreachable local port, rather than stopping the broker shared by the suite.
* SQL trigger rejection tests atomic inbox/job creation failure.
* Injected consumer exceptions test its three-attempt policy; malformed JSON is actually rejected to the broker DLQ.
* Actual local HTTP responses exercise 500/302 handling; an `httpx.MockTransport` raises a read timeout for that specific network-error branch.
* An injected Redis connection exception exercises the API's fail-closed error path. Redis counter/rate-limit tests use the real cache.

## Other checks actually run

* `uv sync` resolved dependencies and generated `uv.lock`; `uv lock --check` passed.
* `uv run --locked ruff check .` passed; `uv run --locked ruff format --check .` passed.
* `docker compose --profile test up -d --wait test-db test-redis test-rabbit` started healthy isolated dependencies.
* `docker compose up -d --build --wait` built all application images and started the complete stack successfully.
* `uv run --locked python scripts/demo.py` completed against the real container API/worker/receiver: simulated funding, identical-key replay, transfer, intent, authorization, capture, partial refund, settlement, balance checks, reconciliation, and eventual webhook delivery.
* The first clean demo ended with Alice 6,500 cents, Bob 1,000 cents, cafe 500 cents, and simulated settlement clearing 2,000 cents. Later demo runs deliberately accumulate additional simulated funds.
* An HTTP request to the worker's `/metrics` succeeded and included its operation counters.
* OpenAPI was exported from the actual FastAPI application. The DLQ replay command was smoke-tested with `--max 0`, then tested against valid and invalid real broker messages.

One upstream warning remains: the locked Starlette TestClient version deprecates its httpx integration in favor of httpx2. It did not fail tests; this project still uses httpx for API tests and outbound webhooks. No warnings were hidden to make the test run pass.

## Not executed / not claimed

No production load benchmark, real money flow, Binance integration, real bank settlement, card network interaction, multi-host deployment, replicated-broker durability test, network-partition/OS-kill chaos run, restore-from-backup exercise, external security audit, or hosted GitHub CI run was performed. Pytest does not cover all process startup/shutdown and demo receiver branches; the Compose demo separately exercised those processes successfully. The project is a local engineering demonstration.

## Student dashboard extension checks

The final Python run above includes all original 56 backend tests and 9 new dashboard tests. Five separate JavaScript checks passed with `node --test tests/browser-utils.test.mjs` on Node.js v24.13.0: exact cent parsing, invalid amounts, UUID/payment-code validation, actor-scoped retry recovery with route restrictions, and transaction descriptions. Node is needed only for these optional tests, not dashboard operation; there is no npm build.

Browser QA was actually performed against the live Docker stack:

* Signed in as Alice, transferred simulated $1.25 to Bob, and saw the new activity and balance.
* Reviewed and paid a $6.50 coffee/pastry demo purchase; merchant and price were shown before confirmation.
* Entered a real merchant payment-code payload, authorized $5.00, saw total $124.25 / reserved $5.00 / available $119.25, then cancelled and verified available funds returned to $124.25.
* Stopped the local API before a $2.00 funding request; the UI preserved the pending request and blocked new mutations. Restarted the API, reloaded, signed in again, recovered the banner, and checked the original request. The balance increased once from $122.25 to $124.25, with one matching activity entry.
* Searched activity for Bob and saw matching transfers.
* Signed out and signed in as Bob; the UI showed Bob's $21.25 and incoming transfers, not Alice's ledger.
* Inspected desktop layout and a narrow responsive viewport (reported content width 375px). The overview and campus shop had no horizontal overflow. This was browser viewport testing, not a physical-device test or a complete accessibility audit.
* Rebuilt the final Docker images, verified API readiness, and independently checked the live ledger reconciliation endpoint returned `balanced: true` after the UI flows.

The captured desktop preview is `docs/dashboard.png`. No frontend browser actions use real funds. The complete source archive was refreshed after this extension.
