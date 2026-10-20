import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg_pool import ConnectionPool

from campuspay.errors import require

MAX_AMOUNT = 1_000_000  # minor USD units ($10,000); never floats


def amount_ok(amount: int):
    require(type(amount) is int and 1 <= amount <= MAX_AMOUNT, "INVALID_AMOUNT", 400)


def lock_accounts(conn, *ids: str) -> dict:
    accounts = {}
    # One row at a time in canonical order also handles opposite-direction transfers.
    for account_id in sorted(set(ids)):
        row = conn.execute("SELECT * FROM accounts WHERE id=%s FOR UPDATE", (account_id,)).fetchone()
        require(row is not None, "NOT_FOUND", 404)
        accounts[account_id] = row
    return accounts


def available(account, amount: int):
    require(account["balance"] - account["reserved"] >= amount, "INSUFFICIENT_AVAILABLE_FUNDS")


def post(conn, kind: str, debit: dict, credit: dict, amount: int, reference: str | None = None) -> str:
    """Called with both accounts locked, in the same transaction as balances/outbox."""
    require(debit["id"] != credit["id"], "SELF_TRANSFER", 400)
    amount_ok(amount)
    journal = str(uuid4())
    conn.execute(
        "INSERT INTO journals(id,reference,kind) VALUES (%s,%s,%s)",
        (journal, reference or f"{kind}:{journal}", kind),
    )
    for account, side in [(debit, "DEBIT"), (credit, "CREDIT")]:
        delta = amount if account["normal_side"] == side else -amount
        conn.execute("UPDATE accounts SET balance=balance+%s WHERE id=%s", (delta, account["id"]))
        conn.execute(
            "INSERT INTO entries(journal_id,account_id,side,amount) VALUES (%s,%s,%s,%s)",
            (journal, account["id"], side, amount),
        )
    return journal


def result(aggregate_id: str, status: str, amount: int) -> dict:
    return {"id": str(aggregate_id), "status": status, "amount": amount, "currency": "USD"}


def event(conn, aggregate_id: str, status: str, amount: int, merchant: str | None = None):
    event_id = str(uuid4())
    body = {
        "eventId": event_id,
        "type": status,
        "aggregateId": str(aggregate_id),
        "merchantId": merchant,
        "amount": amount,
        "currency": "USD",
    }
    conn.execute(
        "INSERT INTO outbox(id,payload) VALUES (%s,%s)", (event_id, json.dumps(body, sort_keys=True))
    )


class Payments:
    def __init__(self, db: ConnectionPool):
        self.db = db

    def execute(self, actor: str, key: str, operation: str, payload: dict, action: Callable) -> dict:
        require(bool(re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key)), "INVALID_IDEMPOTENCY_KEY", 400)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"operation": operation, "payload": payload}, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        with self.db.connection() as conn:
            # A competing INSERT waits for the first transaction's commit, across all replicas.
            inserted = conn.execute(
                """INSERT INTO idempotency(actor,key,fingerprint) VALUES (%s,%s,%s)
                                     ON CONFLICT DO NOTHING RETURNING key""",
                (actor, key, fingerprint),
            ).fetchone()
            if not inserted:
                cached = conn.execute(
                    "SELECT * FROM idempotency WHERE actor=%s AND key=%s", (actor, key)
                ).fetchone()
                require(cached["fingerprint"] == fingerprint, "IDEMPOTENCY_PAYLOAD_MISMATCH")
                return json.loads(cached["response"])
            response = action(conn)
            conn.execute(
                "UPDATE idempotency SET response=%s WHERE actor=%s AND key=%s",
                (json.dumps(response, sort_keys=True), actor, key),
            )
        # Return only AFTER commit and deferred ledger checks have succeeded.
        return response

    def fund(self, actor: str, key: str, amount: int):
        def action(conn):
            amount_ok(amount)
            a = lock_accounts(conn, actor, "funding")
            require(a[actor]["kind"] == "STUDENT", "FORBIDDEN", 403)
            journal = post(conn, "FUNDING", a["funding"], a[actor], amount)
            event(conn, journal, "FUNDED", amount)
            return result(journal, "FUNDED", amount)

        return self.execute(actor, key, "fund", {"amount": amount}, action)

    def transfer(self, actor: str, key: str, recipient: str, amount: int):
        def action(conn):
            amount_ok(amount)
            require(actor != recipient, "SELF_TRANSFER", 400)
            a = lock_accounts(conn, actor, recipient)
            require(a[actor]["kind"] == a[recipient]["kind"] == "STUDENT", "FORBIDDEN", 403)
            available(a[actor], amount)
            journal = post(conn, "TRANSFER", a[actor], a[recipient], amount)
            event(conn, journal, "TRANSFERRED", amount)
            return result(journal, "TRANSFERRED", amount)

        return self.execute(actor, key, "transfer", {"recipient": recipient, "amount": amount}, action)

    def create_intent(self, actor: str, key: str, amount: int):
        def action(conn):
            amount_ok(amount)
            require(lock_accounts(conn, actor)[actor]["kind"] == "MERCHANT", "FORBIDDEN", 403)
            intent = str(uuid4())
            conn.execute(
                "INSERT INTO intents(id,merchant_id,amount,status) VALUES (%s,%s,%s,'CREATED')",
                (intent, actor, amount),
            )
            return {
                **result(intent, "CREATED", amount),
                "qrPayload": f"campuspay:intent:{intent}",
                "qrImage": f"/api/intents/{intent}/qr.svg",
            }

        return self.execute(actor, key, "create_intent", {"amount": amount}, action)

    @staticmethod
    def intent(conn, intent_id: UUID | str):
        row = conn.execute("SELECT * FROM intents WHERE id=%s FOR UPDATE", (intent_id,)).fetchone()
        require(row is not None, "NOT_FOUND", 404)
        return row

    def authorize(self, actor: str, key: str, intent_id: UUID):
        def action(conn):
            intent = self.intent(conn, intent_id)
            require(intent["status"] == "CREATED", "INVALID_STATE")
            account = lock_accounts(conn, actor)[actor]
            require(account["kind"] == "STUDENT", "FORBIDDEN", 403)
            available(account, intent["amount"])
            conn.execute("UPDATE accounts SET reserved=reserved+%s WHERE id=%s", (intent["amount"], actor))
            conn.execute(
                """UPDATE intents SET payer_id=%s,status='AUTHORIZED',expires_at=now()+interval '15 minutes'
                            WHERE id=%s""",
                (actor, intent_id),
            )
            return result(intent_id, "AUTHORIZED", intent["amount"])

        return self.execute(actor, key, "authorize", {"intent": str(intent_id)}, action)

    def capture(self, actor: str, key: str, intent_id: UUID):
        def action(conn):
            intent = self.intent(conn, intent_id)
            require(intent["merchant_id"] == actor, "FORBIDDEN", 403)
            require(intent["status"] == "AUTHORIZED", "INVALID_STATE")
            require(intent["expires_at"] > datetime.now(UTC), "AUTHORIZATION_EXPIRED")
            a = lock_accounts(conn, actor, intent["payer_id"])
            conn.execute(
                "UPDATE accounts SET reserved=reserved-%s WHERE id=%s", (intent["amount"], intent["payer_id"])
            )
            post(conn, "CAPTURE", a[intent["payer_id"]], a[actor], intent["amount"], f"capture:{intent_id}")
            conn.execute("UPDATE intents SET status='CAPTURED' WHERE id=%s", (intent_id,))
            event(conn, str(intent_id), "CAPTURED", intent["amount"], actor)
            return result(intent_id, "CAPTURED", intent["amount"])

        return self.execute(actor, key, "capture", {"intent": str(intent_id)}, action)

    @staticmethod
    def release(conn, intent):
        if intent["status"] == "AUTHORIZED":
            lock_accounts(conn, intent["payer_id"])
            conn.execute(
                "UPDATE accounts SET reserved=reserved-%s WHERE id=%s", (intent["amount"], intent["payer_id"])
            )
        conn.execute("UPDATE intents SET status='VOIDED' WHERE id=%s", (intent["id"],))

    def void(self, actor: str, key: str, intent_id: UUID):
        def action(conn):
            intent = self.intent(conn, intent_id)
            require(actor in (intent["merchant_id"], intent["payer_id"]), "FORBIDDEN", 403)
            require(intent["status"] in ("CREATED", "AUTHORIZED"), "INVALID_STATE")
            self.release(conn, intent)
            return result(intent_id, "VOIDED", intent["amount"])

        return self.execute(actor, key, "void", {"intent": str(intent_id)}, action)

    def refund(self, actor: str, key: str, intent_id: UUID, amount: int):
        def action(conn):
            amount_ok(amount)
            intent = self.intent(conn, intent_id)
            require(intent["merchant_id"] == actor, "FORBIDDEN", 403)
            require(intent["status"] == "CAPTURED", "INVALID_STATE")
            require(amount <= intent["amount"] - intent["refunded"], "REFUND_EXCEEDS_CAPTURE")
            a = lock_accounts(conn, actor, intent["payer_id"])
            available(a[actor], amount)
            post(conn, "REFUND", a[actor], a[intent["payer_id"]], amount)
            status = "REFUNDED" if amount + intent["refunded"] == intent["amount"] else "CAPTURED"
            conn.execute(
                "UPDATE intents SET refunded=refunded+%s,status=%s WHERE id=%s", (amount, status, intent_id)
            )
            outcome = "REFUNDED" if status == "REFUNDED" else "PARTIALLY_REFUNDED"
            event(conn, str(intent_id), outcome, amount, actor)
            return result(intent_id, outcome, amount)

        return self.execute(actor, key, "refund", {"intent": str(intent_id), "amount": amount}, action)

    def settle(self, actor: str, key: str, amount: int):
        def action(conn):
            amount_ok(amount)
            a = lock_accounts(conn, actor, "settled")
            require(a[actor]["kind"] == "MERCHANT", "FORBIDDEN", 403)
            available(a[actor], amount)
            journal = post(conn, "SETTLEMENT", a[actor], a["settled"], amount)
            event(conn, journal, "SETTLED", amount, actor)
            return result(journal, "SETTLED", amount)

        return self.execute(actor, key, "settle", {"amount": amount}, action)

    def expire(self) -> int:
        with self.db.connection() as conn:
            rows = conn.execute("""SELECT * FROM intents WHERE status='AUTHORIZED' AND expires_at < now()
                                   ORDER BY id LIMIT 50 FOR UPDATE SKIP LOCKED""").fetchall()
            for row in rows:
                self.release(conn, row)
        return len(rows)

    def consume(self, body: bytes):
        # Validate before recording inbox. Malformed / unknown events go through broker retries and DLQ.
        from campuspay.schemas import PaymentEvent

        evt = PaymentEvent.model_validate_json(body)
        with self.db.connection() as conn:
            inserted = conn.execute(
                "INSERT INTO inbox(event_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING event_id",
                (evt.eventId,),
            ).fetchone()
            if inserted and evt.merchantId:
                conn.execute(
                    "INSERT INTO webhook_deliveries(event_id,payload) VALUES (%s,%s)",
                    (evt.eventId, body.decode()),
                )

    def replay(self, table: str, event_id: UUID):
        # The identifier is selected from a hard-coded allowlist, never from user input.
        from psycopg import sql

        require(table in ("outbox", "webhook_deliveries"), "INVALID_QUEUE", 400)
        column = "id" if table == "outbox" else "event_id"
        with self.db.connection() as conn:
            row = conn.execute(
                sql.SQL(
                    "UPDATE {} SET status='PENDING',attempts=0,next_attempt=now() WHERE {}=%s AND status='DEAD' RETURNING *"
                ).format(sql.Identifier(table), sql.Identifier(column)),
                (event_id,),
            ).fetchone()
            require(row is not None, "NOT_DEAD_OR_NOT_FOUND", 409)
        return {"id": str(event_id), "status": "PENDING"}
