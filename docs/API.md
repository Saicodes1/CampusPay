# API guide

Live interactive documentation: `http://127.0.0.1:8080/docs`. Portable schema: [`openapi.json`](openapi.json). The demo's request/response field names are fixed by Pydantic models; extra request fields are rejected. No real money is involved.

Authenticate using HTTP Basic. Students use `alice` or `bob`, merchants use `cafe` or `books`, and operators use `admin`. The default shared demo password is `campuspay-demo-only`. All payment POSTs require `Idempotency-Key` (1–128 characters, letters/digits/`.`/`_`/`:`/`-`). Keys are scoped to the actor across operations, so use a distinct key for each new action. Admin replay is a conditional operational transition, not a money movement, and does not use this header.

| Method and route | Role | Body / behavior |
|---|---|---|
| `GET /api/wallet` | Student or merchant | Own balance, reserved amount and available amount |
| `GET /api/ledger?after=0&limit=50` | Student or merchant | Own immutable postings; ascending ID cursor, max 100 |
| `POST /api/funding` | Student | `{"amount":10000}`; simulated funding |
| `POST /api/transfers` | Student | `{"recipient":"bob","amount":1000}`; student-to-student only |
| `POST /api/intents` | Merchant | `{"amount":3000}`; returns intent UUID, QR payload and image path |
| `GET /api/intents/{id}` | Owner/payer, or student for CREATED | State, merchant, amount, payer, refunded total and expiry |
| `GET /api/intents/{id}/qr.svg` | Same visibility as intent | Actual SVG QR encoding `campuspay:intent:{uuid}` |
| `POST /api/intents/{id}/authorize` | Student | No body; claims CREATED intent and reserves full amount |
| `POST /api/intents/{id}/capture` | Owning merchant | No body; full capture of unexpired authorization |
| `POST /api/intents/{id}/void` | Owning merchant or payer | No body; CREATED/AUTHORIZED only |
| `POST /api/intents/{id}/refunds` | Owning merchant | `{"amount":500}`; repeated partial refunds up to capture |
| `POST /api/settlements` | Merchant | `{"amount":2000}`; moves funds into simulated settlement clearing |
| `GET /api/student/catalog` | Student | Server-priced demo products and merchant names |
| `GET /api/student/contacts` | Student | Up to 50 seeded student recipients, excluding self |
| `GET /api/student/activity?before=123&limit=25` | Student | Own activity, newest first, counterparty names and nullable next cursor |
| `GET /api/student/authorizations` | Student | Own authorized holds, merchant names and expiry timestamps (max 100) |
| `POST /api/student/purchases` | Student | `{"product_id":"cafe-breakfast","expected_amount":650}`; immediate capture of a fixed demo catalog item |
| `GET /api/admin/reconciliation` | Admin | Independently computed balance/hold comparison |
| `GET /api/admin/operations` | Admin | Delivery state counts and up to 100 dead IDs per queue |
| `POST /api/admin/replay/outbox/{eventId}` | Admin | DEAD → PENDING; resets attempts |
| `POST /api/admin/replay/webhooks/{eventId}` | Admin | DEAD → PENDING; resets attempts |
| `GET /health/live`, `GET /health/ready` | Public | Process and dependency checks |
| `GET /metrics` | Admin | API Prometheus text exposition |

All payment operations return `200` on initial success and replay. Example capture response:

```json
{"id":"17f522dc-6693-4eb6-aa18-420e85db2ca9","status":"CAPTURED","amount":3000,"currency":"USD"}
```

`POST /api/intents` adds `qrPayload` and `qrImage`. A QR is a discoverable payment request, not authorization to spend. A client reads the UUID, fetches the intent for display, and explicitly authorizes as a student. The student dashboard accepts a pasted `campuspay:intent:{uuid}` payload or UUID; it does not use a camera. Swagger and the SVG endpoint expose the merchant side.

Catalog purchases are an additional demo flow: predefined merchants accept immediate capture. Product ID and expected price are validated against a server-side catalog; arbitrary prices, merchants, or payer IDs cannot be supplied. The purchase inserts a captured intent, balanced journal, outbox event, and idempotency result in one transaction, without a lingering hold. The returned intent ID supports the existing merchant refund API. Changed prices return `409 PRICE_CHANGED`; unknown products return `404 PRODUCT_NOT_FOUND`. The usual `Idempotency-Key` contract applies. The dashboard never authenticates as a merchant.

## Quick examples

On Windows, use `curl.exe` if PowerShell aliases `curl`. A repeatable, cross-platform end-to-end example is `uv run --locked python scripts/demo.py`.

```sh
curl -u alice:campuspay-demo-only -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: funding-001' -d '{"amount":10000}' \
  http://127.0.0.1:8080/api/funding

curl -u cafe:campuspay-demo-only -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cafe-order-001' -d '{"amount":3000}' \
  http://127.0.0.1:8080/api/intents

# Replace INTENT_UUID with the id returned above.
curl -u alice:campuspay-demo-only -X POST -H 'Idempotency-Key: authorize-001' \
  http://127.0.0.1:8080/api/intents/INTENT_UUID/authorize
curl -u cafe:campuspay-demo-only -X POST -H 'Idempotency-Key: capture-001' \
  http://127.0.0.1:8080/api/intents/INTENT_UUID/capture
```

Amounts are strict JSON integers in USD cents, between 1 and 1,000,000. No currency request field exists. A new idempotency key does not bypass the state machine: capturing an already captured intent with a different key returns a conflict, and replaying the original key returns the original capture response even after later refunds.

## Errors and retries

Domain errors use `{"code":"INSUFFICIENT_AVAILABLE_FUNDS","requestId":"..."}`. Validation errors return only failing field locations, not raw input. `X-Request-Id` is returned on responses; callers may supply a short safe request ID for correlation.

| Status | Examples | Client action |
|---|---|---|
| 400 | `INVALID_IDEMPOTENCY_KEY`, `SELF_TRANSFER` | Correct the request |
| 401 | `AUTHENTICATION_REQUIRED`, `INVALID_CREDENTIALS` | Authenticate; `WWW-Authenticate: Basic` is returned |
| 403 | `FORBIDDEN`, `BROWSER_ORIGIN_NOT_ALLOWED` | Correct actor/ownership; cross-origin and opaque-origin mutations are blocked |
| 404 | `NOT_FOUND`, `ADMIN_HAS_NO_WALLET` | Verify ID/endpoint |
| 409 | `IDEMPOTENCY_PAYLOAD_MISMATCH`, `INVALID_STATE`, `AUTHORIZATION_EXPIRED`, `REFUND_EXCEEDS_CAPTURE`, `INSUFFICIENT_AVAILABLE_FUNDS` | Resolve the business condition; do not change a committed key's payload |
| 413 | `BODY_TOO_LARGE` | Reduce declared body size; production ingress must enforce a full streaming limit |
| 422 | `VALIDATION_ERROR` | Supply valid fields, UUIDs, and required headers |
| 429 | `RATE_LIMITED` | Honor `Retry-After: 60` |
| 503 | `RATE_LIMIT_SERVICE_UNAVAILABLE`, `DATABASE_UNAVAILABLE_OR_CONFLICT` | Retry with the **same** key and payload after backoff |

If a client loses the HTTP response after a commit, the same key retrieves the committed result. If the transaction rolled back, the key was not retained, and the same request can execute again. A PostgreSQL deadlock/lock timeout is returned as retryable 503; canonical account lock ordering prevents the common opposite-transfer deadlock. Infrastructure readiness never substitutes for transaction-level correctness.

## Webhook contract

Only completed money movements emit events. Merchant deliveries are created for `CAPTURED`, `PARTIALLY_REFUNDED`, `REFUNDED`, and `SETTLED`; funding/transfer events pass through the inbox without merchant delivery. Authorization and voiding do not emit merchant webhooks in this slice.

```json
{
  "eventId": "ea713fb2-02b0-44f5-8a64-56a0df3af8b7",
  "type": "CAPTURED",
  "aggregateId": "17f522dc-6693-4eb6-aa18-420e85db2ca9",
  "merchantId": "cafe",
  "amount": 3000,
  "currency": "USD"
}
```

Headers are `X-CampusPay-Event-Id`, `X-CampusPay-Timestamp` (Unix seconds), and `X-CampusPay-Signature` (hex HMAC-SHA256). Verify against the exact raw HTTP bytes:

```python
expected = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
valid = abs(time.time() - int(timestamp)) <= 300 and hmac.compare_digest(expected, supplied_signature)
```

Use a unique event-ID constraint and record the side effect atomically with the deduplication row. Return 2xx only after durable processing; duplicate valid events may return 2xx. Reject the same event ID with a different body. No global delivery ordering is promised; reconcile current payment state when order matters. The included receiver persists ID/body hashes in SQLite as a merchant-side example.
