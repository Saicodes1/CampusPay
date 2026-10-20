# Operations and failure recovery

These commands affect only the local, simulated CampusPay project. Execute from its root.

## Inspect

```sh
docker compose ps
docker compose logs --tail=80 api worker receiver
curl -u admin:campuspay-demo-only http://127.0.0.1:8080/api/admin/operations
curl -u admin:campuspay-demo-only http://127.0.0.1:8080/api/admin/reconciliation
```

`balanced: true` confirms the stored ledger and holds reconcile, not that a real provider has paid anything. Workers log event IDs and outcomes. The API's metrics endpoint requires admin Basic credentials; the worker's `http://127.0.0.1:9100/metrics` is a local-only development metrics listener. Look for `campuspay_worker_operations_total{worker="outbox",outcome="dead"}` and the corresponding webhook/consumer labels. API metrics include route-template request counts and latency histograms. RabbitMQ management on port 15673 shows pending and dead-letter messages.

## Broker outage

1. Stop the broker with `docker compose stop rabbit`.
2. Submit a new simulated funding request with a fresh idempotency key. PostgreSQL commits the money movement and event together. The API can remain ready because its synchronous dependencies are PostgreSQL and Redis.
3. Inspect operations: the event remains PENDING with an increasing attempt count. No event is deleted on a publish failure. After five attempts it becomes DEAD.
4. Run `docker compose start rabbit`. Pending events publish on their scheduled retry; DEAD events require replay after the broker is healthy.

An outbox publish can be confirmed even though the process never records SENT. Recovery publishes again; consumer inbox insertion and webhook-job creation are atomic, preventing a duplicated job. A worker crash also rolls back its claimed row, leaving it available to another worker.

## Webhook outage

Stop the receiver with `docker compose stop receiver`, capture a payment, and inspect worker logs. Webhook attempts retry with a bounded timeout and persisted backoff; after five attempts the delivery becomes DEAD. Restart with `docker compose start receiver`, obtain `event_id` from the dead-job list, and replay it:

```sh
curl -X POST -u admin:campuspay-demo-only \
  http://127.0.0.1:8080/api/admin/replay/webhooks/EVENT_UUID
```

For dead outbox rows, use `/api/admin/replay/outbox/EVENT_UUID`. Replaying a non-DEAD or unknown job returns 409. The actor must be admin. Webhook replay preserves the original event ID and raw payload; a fresh timestamp/signature is generated for each attempt. The receiver must deduplicate because a successful HTTP response may be lost before the worker's database commit.

## Consumer poison messages

Malformed/unsupported events are attempted three times and rejected to `campuspay.notifications.dead` through the durable `campuspay.dead` exchange. A transient consumer database error follows the same bounded retry path. Check the error type, fix the cause, and replay valid events:

```sh
uv run --locked python -m campuspay.replay_dlq --max 10
```

The script validates each event, republishes with publisher confirmation, then acknowledges the DLQ message. A crash between those steps may duplicate it, which is safe through the inbox. Invalid events stay in the DLQ for inspection; the script stops rather than silently discarding them. Use RabbitMQ management to inspect a poison payload and remove it only after deciding it is invalid. There is no automatic infinite replay loop.

## Redis and database outages

Redis rate limiting fails closed with 503. No payment is attempted when authentication's rate-limit check fails. Restoring Redis restores availability. Losing Redis state resets rate-limit counters but cannot change balances, holds, or idempotency results.

PostgreSQL connection, lock, or transaction failures return 503 and roll back the whole transaction. Retry with the same key and payload after backoff; if the first commit succeeded but the response was lost, the idempotency result is returned. Do not invent a new key for an ambiguous payment outcome.

## Expiry and settlement

The worker releases expired authorized intents in batches of 50 using row locks and SKIP LOCKED. While workers are down, expired holds remain reserved; capture still rejects expiry. The payer or merchant can explicitly void the authorization. CREATED intents do not expire automatically in this demo.

Settlement is initiated explicitly by a merchant for an integer amount. There is no calendar batch scheduler or real bank file. A merchant that has settled too much may lack funds for a refund. This is an intentional limitation: there is no lending or negative balance. Do not fix this by editing balances; issue legitimate simulated business operations and preserve the append-only journal.

## Data and secrets

API/worker startup serialize checksummed migrations. Changing an already applied migration fails startup; add a new numbered migration instead. The database credentials in Compose are local demo credentials and own the schema to keep setup small. Production would separate migration and runtime roles and restrict ledger table privileges.

Backups and HA are outside this demo. Named volumes survive `down`; `down -v` erases the simulated demo state. Test database storage uses tmpfs and test schemas are dropped after the suite. Do not point test variables at your demonstration broker/cache: tests purge their dedicated queue and Redis DB 15.
