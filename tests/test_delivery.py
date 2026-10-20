import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID

import httpx
import pytest

from campuspay import broker
from campuspay.errors import Problem
from campuspay.receiver import verify
from campuspay.workers import Workers
from tests.conftest import balance, captured, scalar

pytestmark = pytest.mark.integration


@pytest.fixture
def channel(settings):
    connection = broker.connect(settings.rabbit_url)
    channel = connection.channel()
    broker.topology(channel)
    channel.queue_purge(broker.QUEUE)
    channel.queue_purge(broker.DEAD_QUEUE)
    yield channel
    if connection.is_open:
        connection.close()


def capture_event(db, payments):
    captured(payments)
    with db.connection() as conn:
        return conn.execute("SELECT * FROM outbox WHERE payload LIKE '%CAPTURED%' LIMIT 1").fetchone()


def force_due(db, table):
    assert table in ("outbox", "webhook_deliveries")
    with db.connection() as conn:
        conn.execute(f"UPDATE {table} SET next_attempt=now()-interval '1 second'")


def test_real_outbox_publish_broker_consume_and_duplicate_delivery(payments, db, settings, channel):
    captured(payments)
    worker = Workers(db, settings)
    assert worker.dispatch_outbox()
    assert worker.dispatch_outbox()
    assert not worker.dispatch_outbox()
    assert worker.consume_one(channel)
    assert worker.consume_one(channel)
    assert scalar(db, "SELECT count(*) FROM inbox") == 2
    assert scalar(db, "SELECT count(*) FROM webhook_deliveries") == 1
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM outbox").fetchall()
    for row in rows:
        broker.publish(settings.rabbit_url, str(row["id"]), row["payload"])
    assert worker.consume_one(channel)
    assert worker.consume_one(channel)
    assert scalar(db, "SELECT count(*) FROM inbox") == 2
    assert scalar(db, "SELECT count(*) FROM webhook_deliveries") == 1


def test_crash_after_publish_before_outbox_commit_is_recovered(payments, db, settings, channel):
    payments.fund("alice", "f", 100)
    worker = Workers(db, settings)

    def crash(url, event_id, payload):
        broker.publish(url, event_id, payload)
        raise SystemExit("injected process death after broker confirmation")

    with pytest.raises(SystemExit):
        worker.dispatch_outbox(crash)
    assert scalar(db, "SELECT status FROM outbox") == "PENDING"
    assert worker.dispatch_outbox()
    assert worker.consume_one(channel)
    assert worker.consume_one(channel)
    assert scalar(db, "SELECT count(*) FROM inbox") == 1
    assert balance(db)["balance"] == 100


def test_broker_unavailable_payment_still_commits_and_outbox_recovers(payments, db, settings, channel):
    payments.fund("alice", "f", 100)
    broken = settings.model_copy(
        update={"rabbit_url": "amqp://campuspay:campuspay-local-only@localhost:1/%2F"}
    )
    assert Workers(db, broken).dispatch_outbox()
    assert balance(db)["balance"] == 100
    assert scalar(db, "SELECT status FROM outbox") == "PENDING"
    assert scalar(db, "SELECT attempts FROM outbox") == 1
    force_due(db, "outbox")
    assert Workers(db, settings).dispatch_outbox()
    assert scalar(db, "SELECT status FROM outbox") == "SENT"


def test_outbox_dead_after_bounded_retries_and_admin_replay(payments, db, settings):
    payments.fund("alice", "f", 100)
    worker = Workers(db, settings)

    def failure(*_):
        raise OSError("injected broker timeout")

    for _ in range(5):
        assert worker.dispatch_outbox(failure)
        force_due(db, "outbox")
    assert scalar(db, "SELECT status FROM outbox") == "DEAD"
    assert not worker.dispatch_outbox(failure)
    event_id = scalar(db, "SELECT id FROM outbox")
    assert payments.replay("outbox", event_id)["status"] == "PENDING"
    assert scalar(db, "SELECT attempts FROM outbox") == 0


def test_consumer_transient_failure_retries_then_acks(payments, db, settings, channel, monkeypatch):
    row = capture_event(db, payments)
    broker.publish(settings.rabbit_url, str(row["id"]), row["payload"])
    worker = Workers(db, settings)
    original, calls = worker.payments.consume, []

    def flaky(body):
        calls.append(1)
        if len(calls) < 3:
            raise OSError("injected transient DB failure")
        original(body)

    monkeypatch.setattr(worker.payments, "consume", flaky)
    assert worker.consume_one(channel)
    assert len(calls) == 3
    assert scalar(db, "SELECT count(*) FROM webhook_deliveries") == 1
    assert channel.queue_declare(broker.QUEUE, passive=True).method.message_count == 0


def test_poison_event_reaches_real_dead_letter_queue(db, settings, channel):
    broker.publish(settings.rabbit_url, "invalid", '{"malformed":true}')
    assert Workers(db, settings).consume_one(channel)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        method, _, body = channel.basic_get(broker.DEAD_QUEUE, auto_ack=True)
        if method:
            assert body == b'{"malformed":true}'
            break
        time.sleep(0.05)
    else:
        pytest.fail("Poison event never arrived in broker DLQ")
    assert scalar(db, "SELECT count(*) FROM inbox") == 0


def test_lost_ack_redelivery_is_idempotent(payments, db, settings, channel):
    row = capture_event(db, payments)
    broker.publish(settings.rabbit_url, str(row["id"]), row["payload"])
    method, _, body = channel.basic_get(broker.QUEUE, auto_ack=False)
    assert method is not None
    payments.consume(body)
    channel.close()  # no ack: broker requeues the unacknowledged delivery
    replacement = channel.connection.channel()
    worker = Workers(db, settings)
    deadline = time.monotonic() + 5
    while not worker.consume_one(replacement):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert scalar(db, "SELECT count(*) FROM inbox") == 1
    assert scalar(db, "SELECT count(*) FROM webhook_deliveries") == 1


def test_inbox_rolls_back_if_delivery_creation_fails(payments, db):
    row = capture_event(db, payments)
    with db.connection() as conn:
        conn.execute("""CREATE FUNCTION reject_webhook() RETURNS trigger LANGUAGE plpgsql AS $$
                        BEGIN RAISE EXCEPTION 'injected delivery failure'; END $$;
                        CREATE TRIGGER reject_webhook BEFORE INSERT ON webhook_deliveries FOR EACH ROW EXECUTE FUNCTION reject_webhook()""")
    try:
        with pytest.raises(Exception, match="injected delivery failure"):
            payments.consume(row["payload"].encode())
        assert scalar(db, "SELECT count(*) FROM inbox") == 0
    finally:
        with db.connection() as conn:
            conn.execute("DROP TRIGGER reject_webhook ON webhook_deliveries; DROP FUNCTION reject_webhook()")
    payments.consume(row["payload"].encode())
    assert scalar(db, "SELECT count(*) FROM inbox") == 1


@pytest.fixture
def receiver(settings):
    received, responses = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            assert verify(
                settings.webhook_secret,
                self.headers["X-CampusPay-Timestamp"],
                body,
                self.headers["X-CampusPay-Signature"],
            )
            assert str(UUID(json.loads(body)["eventId"])) == self.headers["X-CampusPay-Event-Id"]
            received.append(body)
            self.send_response(responses.pop(0) if responses else 204)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/events", received, responses
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_real_http_webhook_failure_retry_signature_and_success(payments, db, settings, receiver):
    url, received, responses = receiver
    row = capture_event(db, payments)
    payments.consume(row["payload"].encode())
    responses.append(500)
    worker = Workers(db, settings.model_copy(update={"webhook_url": url}))
    assert worker.deliver_webhook()
    assert scalar(db, "SELECT status FROM webhook_deliveries") == "PENDING"
    assert not worker.deliver_webhook()  # backoff has not elapsed
    force_due(db, "webhook_deliveries")
    assert worker.deliver_webhook()
    assert scalar(db, "SELECT status FROM webhook_deliveries") == "DELIVERED"
    assert len(received) == 2 and received[0] == received[1]


def test_webhook_dead_replay_and_no_redirects(payments, db, settings, receiver):
    url, received, responses = receiver
    row = capture_event(db, payments)
    payments.consume(row["payload"].encode())
    responses.extend([302] * 5)
    worker = Workers(db, settings.model_copy(update={"webhook_url": url}))
    for _ in range(5):
        assert worker.deliver_webhook()
        force_due(db, "webhook_deliveries")
    assert scalar(db, "SELECT status FROM webhook_deliveries") == "DEAD"
    assert not worker.deliver_webhook()
    assert payments.replay("webhook_deliveries", row["id"])["status"] == "PENDING"
    assert worker.deliver_webhook()
    assert scalar(db, "SELECT status FROM webhook_deliveries") == "DELIVERED"
    assert len(received) == 6


def test_webhook_network_timeout_is_retryable(payments, db, settings):
    row = capture_event(db, payments)
    payments.consume(row["payload"].encode())

    def fail(request):
        raise httpx.ReadTimeout("injected read timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        assert Workers(db, settings).deliver_webhook(client)
    assert scalar(db, "SELECT status FROM webhook_deliveries") == "PENDING"
    assert scalar(db, "SELECT last_result FROM webhook_deliveries") == "ReadTimeout"


def test_replay_rejects_pending_and_unknown_targets(payments, db):
    payments.fund("alice", "f", 100)
    event_id = scalar(db, "SELECT id FROM outbox")
    with pytest.raises(Problem, match="NOT_DEAD_OR_NOT_FOUND"):
        payments.replay("outbox", event_id)
    with pytest.raises(Problem, match="INVALID_QUEUE"):
        payments.replay("accounts", event_id)


def test_dlq_command_republishes_valid_event(payments, db, settings, channel, monkeypatch):
    import campuspay.replay_dlq as cli

    row = capture_event(db, payments)
    channel.basic_publish(broker.DEAD_EXCHANGE, "", row["payload"].encode())
    channel.connection.sleep(0.1)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["replay_dlq", "--max", "1"])
    cli.main()
    assert Workers(db, settings).consume_one(channel)
    assert scalar(db, "SELECT count(*) FROM webhook_deliveries") == 1


def test_dlq_command_retains_malformed_event(db, settings, channel, monkeypatch):
    import campuspay.replay_dlq as cli

    channel.basic_publish(broker.DEAD_EXCHANGE, "", b'{"invalid":true}')
    channel.connection.sleep(0.1)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["replay_dlq", "--max", "1"])
    cli.main()
    channel.connection.sleep(0.1)
    method, _, body = channel.basic_get(broker.DEAD_QUEUE, auto_ack=True)
    assert method is not None and body == b'{"invalid":true}'
