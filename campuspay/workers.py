import hashlib
import hmac
import logging
import os
import signal
import threading
import time

import httpx
from prometheus_client import start_http_server

from campuspay import broker
from campuspay.config import Settings
from campuspay.db import migrate, pool
from campuspay.observability import WORK, configure_logging
from campuspay.payments import Payments

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


def signature(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


class Workers:
    def __init__(self, db, settings: Settings):
        self.db, self.settings = db, settings
        self.payments = Payments(db)

    def dispatch_outbox(self, publisher=None) -> bool:
        """One bounded item; SKIP LOCKED supports multiple worker replicas.

        Broker I/O is intentionally inside the DB transaction in this small demo.
        A crash after confirmation but before commit republishes safely through the inbox.
        """
        publisher = publisher or broker.publish
        with self.db.connection() as conn:
            row = conn.execute("""SELECT * FROM outbox WHERE status='PENDING' AND next_attempt <= now()
                                   ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
            if not row:
                return False
            attempts = row["attempts"] + 1
            try:
                publisher(self.settings.rabbit_url, str(row["id"]), row["payload"])
            except Exception as exc:
                state = "DEAD" if attempts >= MAX_ATTEMPTS else "PENDING"
                conn.execute(
                    "UPDATE outbox SET status=%s,attempts=%s,next_attempt=now()+%s*interval '1 second' WHERE id=%s",
                    (state, attempts, min(2**attempts, 60), row["id"]),
                )
                WORK.labels("outbox", state.lower()).inc()
                log.warning(
                    "Outbox publish failed",
                    extra={
                        "fields": {
                            "eventId": str(row["id"]),
                            "attempt": attempts,
                            "errorType": type(exc).__name__,
                            "outcome": state,
                        }
                    },
                )
            else:
                conn.execute(
                    "UPDATE outbox SET status='SENT',attempts=%s,sent_at=now() WHERE id=%s",
                    (attempts, row["id"]),
                )
                WORK.labels("outbox", "sent").inc()
        return True

    def deliver_webhook(self, client=None) -> bool:
        with self.db.connection() as conn:
            row = conn.execute("""SELECT * FROM webhook_deliveries WHERE status='PENDING' AND next_attempt <= now()
                                   ORDER BY event_id LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
            if not row:
                return False
            body = row["payload"].encode()
            timestamp = str(int(time.time()))
            headers = {
                "Content-Type": "application/json",
                "X-CampusPay-Event-Id": str(row["event_id"]),
                "X-CampusPay-Timestamp": timestamp,
                "X-CampusPay-Signature": signature(self.settings.webhook_secret, timestamp, body),
            }
            attempts = row["attempts"] + 1
            # Destination comes only from operator configuration; no merchant-supplied URLs or redirects.
            owned = client is None
            client = client or httpx.Client(
                timeout=httpx.Timeout(3, connect=2), follow_redirects=False, trust_env=False
            )
            try:
                with client.stream(
                    "POST", self.settings.webhook_url, content=body, headers=headers
                ) as response:
                    delivered = 200 <= response.status_code < 300
                    outcome = str(response.status_code)
            except httpx.HTTPError as exc:
                delivered, outcome = False, type(exc).__name__
            finally:
                if owned:
                    client.close()
            state = "DELIVERED" if delivered else ("DEAD" if attempts >= MAX_ATTEMPTS else "PENDING")
            conn.execute(
                """UPDATE webhook_deliveries SET status=%s,attempts=%s,last_result=%s,
                            next_attempt=now()+%s*interval '1 second',delivered_at=CASE WHEN %s THEN now() ELSE NULL END
                            WHERE event_id=%s""",
                (state, attempts, outcome, min(2**attempts, 60), delivered, row["event_id"]),
            )
            WORK.labels("webhook", state.lower()).inc()
            log.info(
                "Webhook attempt",
                extra={
                    "fields": {
                        "eventId": str(row["event_id"]),
                        "attempt": attempts,
                        "outcome": state,
                        "httpResult": outcome,
                    }
                },
            )
        return True

    def consume_one(self, channel) -> bool:
        method, properties, body = channel.basic_get(broker.QUEUE, auto_ack=False)
        if method is None:
            return False
        # Bounded immediate retries. A process crash is re-delivered by the broker; inbox prevents duplicates.
        for attempt in range(3):
            try:
                self.payments.consume(body)
            except Exception as exc:
                WORK.labels("consumer", "retry").inc()
                log.warning(
                    "Consumer attempt failed",
                    extra={"fields": {"attempt": attempt + 1, "errorType": type(exc).__name__}},
                )
                if attempt < 2:
                    channel.connection.sleep(0.1 * 2**attempt)
            else:
                # Database commit happens before acknowledgement. Crash here => safe redelivery.
                channel.basic_ack(method.delivery_tag)
                WORK.labels("consumer", "acked").inc()
                return True
        channel.basic_nack(method.delivery_tag, requeue=False)
        WORK.labels("consumer", "dead").inc()
        return True


def main():
    configure_logging()
    start_http_server(9100, addr=os.getenv("CAMPUSPAY_METRICS_HOST", "127.0.0.1"))
    settings = Settings()
    db = pool(settings.database_url)
    db.wait()
    migrate(db)
    workers = Workers(db, settings)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    def consumer_loop():
        while not stop.is_set():
            connection = None
            try:
                connection = broker.connect(settings.rabbit_url)
                channel = connection.channel()
                broker.topology(channel)
                channel.basic_qos(prefetch_count=1)
                while not stop.is_set():
                    if not workers.consume_one(channel):
                        connection.sleep(0.2)
            except Exception:
                log.exception("Consumer reconnecting")
                stop.wait(2)
            finally:
                if connection and connection.is_open:
                    connection.close()

    thread = threading.Thread(target=consumer_loop, name="consumer", daemon=True)
    thread.start()
    try:
        while not stop.is_set():
            try:
                workers.dispatch_outbox()
                workers.deliver_webhook()
                workers.payments.expire()
            except Exception:
                log.exception("Worker iteration failed")
            stop.wait(0.2)
    finally:
        thread.join(timeout=8)
        db.close()


if __name__ == "__main__":
    main()
