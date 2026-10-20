from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import psycopg
import pytest

from campuspay.db import migrate
from campuspay.errors import Problem
from campuspay.payments import Payments
from tests.conftest import balance, captured, scalar

pytestmark = pytest.mark.integration


def attempt(call):
    try:
        return call()
    except Problem as exc:
        return exc.code


def race(functions):
    barrier = Barrier(len(functions))

    def run(fn):
        barrier.wait(timeout=15)
        return attempt(fn)

    with ThreadPoolExecutor(max_workers=len(functions)) as executor:
        return list(executor.map(run, functions))


def test_full_payment_refund_settlement_vertical_slice(payments, db):
    payments.fund("alice", "fund", 10000)
    payments.transfer("alice", "transfer", "bob", 1000)
    intent = UUID(payments.create_intent("cafe", "intent", 3000)["id"])
    payments.authorize("alice", "authorize", intent)
    assert balance(db) == {"balance": 9000, "reserved": 3000, "available": 6000}
    payments.capture("cafe", "capture", intent)
    assert balance(db) == {"balance": 6000, "reserved": 0, "available": 6000}
    payments.refund("cafe", "refund", intent, 500)
    payments.settle("cafe", "settle", 2000)
    assert balance(db, "cafe")["balance"] == 500
    assert balance(db, "settled")["balance"] == 2000
    assert balance(db)["balance"] == 6500
    assert balance(db, "bob")["balance"] == 1000
    assert scalar(db, "SELECT count(*) FROM journals") == 5
    assert scalar(db, "SELECT count(*) FROM outbox") == 5
    assert scalar(db, "SELECT SUM(CASE WHEN side='DEBIT' THEN amount ELSE -amount END) FROM entries") == 0


def test_idempotency_replay_and_payload_endpoint_mismatch(payments, db):
    one = payments.fund("alice", "same", 1000)
    assert payments.fund("alice", "same", 1000) == one
    assert attempt(lambda: payments.fund("alice", "same", 2000)) == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    assert attempt(lambda: payments.transfer("alice", "same", "bob", 1000)) == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    assert balance(db)["balance"] == 1000
    assert scalar(db, "SELECT count(*) FROM journals") == 1


def test_failed_request_does_not_claim_key(payments, db):
    assert attempt(lambda: payments.transfer("alice", "retry", "bob", 100)) == "INSUFFICIENT_AVAILABLE_FUNDS"
    assert scalar(db, "SELECT count(*) FROM idempotency") == 0
    payments.fund("alice", "fund", 100)
    assert payments.transfer("alice", "retry", "bob", 100)["status"] == "TRANSFERRED"


def test_concurrent_spends_across_service_instances(payments, db):
    payments.fund("alice", "fund", 1000)
    replicas = [Payments(db) for _ in range(20)]
    outcomes = race([lambda i=i: replicas[i].transfer("alice", f"spend-{i}", "bob", 100) for i in range(20)])
    assert sum(isinstance(o, dict) for o in outcomes) == 10
    assert outcomes.count("INSUFFICIENT_AVAILABLE_FUNDS") == 10
    assert balance(db)["balance"] == 0
    assert balance(db, "bob")["balance"] == 1000
    assert scalar(db, "SELECT count(*) FROM journals") == 11


def test_concurrent_duplicate_key_commits_once(payments, db):
    outcomes = race([lambda: payments.fund("alice", "same", 1000) for _ in range(20)])
    assert len({o["id"] for o in outcomes}) == 1
    assert balance(db)["balance"] == 1000
    assert scalar(db, "SELECT count(*) FROM journals") == 1
    assert scalar(db, "SELECT count(*) FROM outbox") == 1


def test_opposite_direction_transfers_do_not_deadlock(payments, db):
    payments.fund("alice", "fund", 1000)
    payments.fund("bob", "fund", 1000)
    outcomes = race(
        [
            lambda i=i: payments.transfer(
                "alice" if i % 2 else "bob", f"t-{i}", "bob" if i % 2 else "alice", 10
            )
            for i in range(20)
        ]
    )
    assert all(isinstance(o, dict) for o in outcomes)
    assert balance(db)["balance"] == balance(db, "bob")["balance"] == 1000


def test_authorization_reserves_spendable_funds_and_void_releases(payments, db):
    payments.fund("alice", "fund", 1000)
    intent = UUID(payments.create_intent("cafe", "intent", 800)["id"])
    payments.authorize("alice", "authorize", intent)
    assert (
        attempt(lambda: payments.transfer("alice", "transfer", "bob", 201)) == "INSUFFICIENT_AVAILABLE_FUNDS"
    )
    assert scalar(db, "SELECT count(*) FROM journals") == 1  # holds are not money movement
    payments.void("alice", "void", intent)
    assert balance(db)["available"] == 1000
    assert attempt(lambda: payments.capture("cafe", "capture", intent)) == "INVALID_STATE"


def test_two_students_cannot_authorize_same_intent(payments, db):
    for actor in ("alice", "bob"):
        payments.fund(actor, "fund", 1000)
    intent = UUID(payments.create_intent("cafe", "intent", 600)["id"])
    outcomes = race(
        [lambda: payments.authorize("alice", "a", intent), lambda: payments.authorize("bob", "b", intent)]
    )
    assert sum(isinstance(o, dict) for o in outcomes) == 1
    assert outcomes.count("INVALID_STATE") == 1
    assert balance(db)["reserved"] + balance(db, "bob")["reserved"] == 600


def test_capture_races_void_without_double_spend(payments, db):
    payments.fund("alice", "fund", 1000)
    intent = UUID(payments.create_intent("cafe", "intent", 600)["id"])
    payments.authorize("alice", "a", intent)
    outcomes = race(
        [lambda: payments.capture("cafe", "c", intent), lambda: payments.void("alice", "v", intent)]
    )
    assert sum(isinstance(o, dict) for o in outcomes) == 1
    assert balance(db)["reserved"] == 0
    assert balance(db)["balance"] + balance(db, "cafe")["balance"] == 1000


def test_expired_authorization_rejects_capture_and_worker_releases(payments, db):
    payments.fund("alice", "fund", 1000)
    intent = UUID(payments.create_intent("cafe", "intent", 600)["id"])
    payments.authorize("alice", "a", intent)
    with db.connection() as conn:
        conn.execute("UPDATE intents SET expires_at=now()-interval '1 second' WHERE id=%s", (intent,))
    assert attempt(lambda: payments.capture("cafe", "c", intent)) == "AUTHORIZATION_EXPIRED"
    assert payments.expire() == 1
    assert payments.expire() == 0
    assert balance(db)["reserved"] == 0


def test_refunds_are_bounded_and_idempotent(payments, db):
    intent = captured(payments)
    one = payments.refund("cafe", "r1", intent, 400)
    assert one["status"] == "PARTIALLY_REFUNDED"
    assert payments.refund("cafe", "r1", intent, 400) == one
    assert attempt(lambda: payments.refund("cafe", "too-much", intent, 601)) == "REFUND_EXCEEDS_CAPTURE"
    assert payments.refund("cafe", "r2", intent, 600)["status"] == "REFUNDED"
    assert attempt(lambda: payments.refund("cafe", "r3", intent, 1)) == "INVALID_STATE"
    assert balance(db)["balance"] == 1000


def test_concurrent_refunds_never_exceed_capture(payments, db):
    intent = captured(payments)
    outcomes = race([lambda i=i: payments.refund("cafe", f"r-{i}", intent, 100) for i in range(20)])
    assert sum(isinstance(o, dict) for o in outcomes) == 10
    assert outcomes.count("INVALID_STATE") == 10
    assert balance(db)["balance"] == 1000


def test_settlement_prevents_unfunded_refund(payments, db):
    intent = captured(payments)
    payments.settle("cafe", "settle", 1000)
    assert attempt(lambda: payments.refund("cafe", "refund", intent, 1)) == "INSUFFICIENT_AVAILABLE_FUNDS"
    assert balance(db, "settled")["balance"] == 1000


def test_ownership_and_illegal_transitions(payments, db):
    intent = UUID(payments.create_intent("cafe", "intent", 100)["id"])
    assert attempt(lambda: payments.capture("books", "c", intent)) == "FORBIDDEN"
    assert attempt(lambda: payments.capture("cafe", "c", intent)) == "INVALID_STATE"
    assert attempt(lambda: payments.void("bob", "v", intent)) == "FORBIDDEN"
    assert attempt(lambda: payments.transfer("alice", "self", "alice", 1)) == "SELF_TRANSFER"
    assert attempt(lambda: payments.authorize("alice", "not-found", uuid4())) == "NOT_FOUND"


def test_crash_after_outbox_insert_rolls_back_entire_payment(payments, db, monkeypatch):
    import campuspay.payments as module

    original = module.event

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated crash before commit")

    monkeypatch.setattr(module, "event", fail)
    with pytest.raises(RuntimeError):
        payments.fund("alice", "fund", 1000)
    assert balance(db)["balance"] == 0
    for table in ("journals", "entries", "outbox", "idempotency"):
        assert scalar(db, f"SELECT count(*) FROM {table}") == 0


def test_database_rejects_unbalanced_journal(db):
    with pytest.raises(psycopg.Error, match="unbalanced journal"):
        with db.connection() as conn:
            conn.execute("INSERT INTO journals(id,reference,kind) VALUES (%s,'bad','TEST')", (uuid4(),))
    assert scalar(db, "SELECT count(*) FROM journals") == 0


def test_database_rejects_balance_and_hold_projection_tampering(db):
    with pytest.raises(psycopg.Error, match="balance projection mismatch"):
        with db.connection() as conn:
            conn.execute("UPDATE accounts SET balance=10 WHERE id='alice'")
    with pytest.raises(psycopg.Error):
        with db.connection() as conn:
            conn.execute("UPDATE accounts SET reserved=10 WHERE id='alice'")


def test_ledger_append_only_and_no_late_entries(payments, db):
    payments.fund("alice", "fund", 1000)
    with pytest.raises(psycopg.Error, match="append only"):
        with db.connection() as conn:
            conn.execute("UPDATE entries SET amount=1")
    with pytest.raises(psycopg.Error, match="append only"):
        with db.connection() as conn:
            conn.execute("DELETE FROM journals")
    with pytest.raises(psycopg.Error, match="cannot append"):
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO entries(journal_id,account_id,side,amount) SELECT id,'alice','CREDIT',1 FROM journals"
            )


def test_migrations_are_repeatable(db):
    migrate(db)
    migrate(db)
    assert scalar(db, "SELECT count(*) FROM schema_migrations") == 3
