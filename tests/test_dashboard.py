from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest

from campuspay.errors import Problem
from tests.conftest import balance, scalar

pytestmark = pytest.mark.integration
AUTH = ("alice", "campuspay-demo-only")
MERCHANT = ("cafe", "campuspay-demo-only")


def buy(client, key="purchase", **overrides):
    return client.post(
        "/api/student/purchases",
        auth=AUTH,
        headers={"Idempotency-Key": key},
        json={"product_id": "cafe-breakfast", "expected_amount": 650, **overrides},
    )


def test_dashboard_and_local_assets_are_served_with_security_headers(client):
    page = client.get("/")
    assert page.status_code == 200 and "Your student wallet" in page.text
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert client.get("/student").status_code == 200
    for asset in ("styles.css", "app.js", "utils.mjs", "icons.svg", "favicon.svg"):
        assert client.get("/assets/" + asset).status_code == 200
    assert client.get("/assets/../config.py").status_code == 404


def test_same_origin_writes_work_but_cross_and_null_origins_are_rejected(client, db):
    response = client.post(
        "/api/funding",
        auth=AUTH,
        headers={"Origin": "http://testserver", "Idempotency-Key": "same-origin"},
        json={"amount": 100},
    )
    assert response.status_code == 200
    for origin in (
        "null",
        "https://malicious.example",
        "http://testserver.evil.example",
        "http://testserver:9999",
    ):
        response = client.post(
            "/api/funding",
            auth=AUTH,
            headers={"Origin": origin, "Idempotency-Key": "cross-origin"},
            json={"amount": 100},
        )
        assert response.status_code == 403
    assert balance(db)["balance"] == 100


def test_catalog_purchase_captures_once_and_can_be_refunded(client, payments, db):
    payments.fund("alice", "fund", 1000)
    products = client.get("/api/student/catalog", auth=AUTH).json()["items"]
    assert len(products) == 4
    assert products[0]["merchant_name"] == "Campus Cafe"
    first = buy(client)
    assert first.status_code == 200 and first.json()["status"] == "CAPTURED"
    assert buy(client).json() == first.json()
    assert balance(db)["balance"] == 350
    assert balance(db, "cafe")["balance"] == 650
    assert scalar(db, "SELECT count(*) FROM intents") == 1
    assert scalar(db, "SELECT count(*) FROM outbox WHERE payload LIKE %s", ("%CAPTURED%",)) == 1
    payments.refund("cafe", "refund", UUID(first.json()["id"]), 650)
    assert balance(db)["balance"] == 1000


def test_catalog_price_and_payload_cannot_be_overridden(client, payments, db):
    payments.fund("alice", "fund", 1000)
    assert buy(client, expected_amount=1).json()["code"] == "PRICE_CHANGED"
    assert buy(client, product_id="not-a-product").status_code == 404
    assert buy(client, actor="bob").status_code == 422
    response = buy(client)
    assert response.status_code == 200
    mismatch = buy(client, product_id="books-notebook", expected_amount=850)
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    assert balance(db)["balance"] == 350


def test_failed_purchase_rolls_back_without_orphan_intent(client, db):
    response = buy(client)
    assert response.status_code == 409 and response.json()["code"] == "INSUFFICIENT_AVAILABLE_FUNDS"
    for table in ("intents", "outbox", "journals", "idempotency"):
        assert scalar(db, f"SELECT count(*) FROM {table}") == 0


def test_student_endpoints_reject_merchant_and_anonymous_access(client):
    for path in ("catalog", "contacts", "activity", "authorizations"):
        assert client.get("/api/student/" + path).status_code == 401
        assert client.get("/api/student/" + path, auth=MERCHANT).status_code == 403
    assert (
        client.post(
            "/api/student/purchases",
            auth=MERCHANT,
            headers={"Idempotency-Key": "k"},
            json={"product_id": "cafe-breakfast", "expected_amount": 650},
        ).status_code
        == 403
    )


def test_activity_is_newest_first_paginated_and_scoped(client, payments):
    payments.fund("alice", "fund", 1000)
    payments.transfer("alice", "transfer", "bob", 100)
    buy(client)
    first = client.get("/api/student/activity?limit=1", auth=AUTH).json()
    assert first["items"][0]["kind"] == "CAPTURE"
    assert first["items"][0]["counterparty"] == "Campus Cafe"
    second = client.get(f"/api/student/activity?before={first['nextCursor']}&limit=1", auth=AUTH).json()
    assert second["items"][0]["kind"] == "TRANSFER"
    assert second["items"][0]["id"] < first["items"][0]["id"]
    third = client.get(f"/api/student/activity?before={second['nextCursor']}&limit=1", auth=AUTH).json()
    assert third["items"][0]["kind"] == "FUNDING" and third["nextCursor"] is None
    bob = client.get("/api/student/activity", auth=("bob", AUTH[1])).json()["items"]
    assert len(bob) == 1 and bob[0]["counterparty"] == "Alice Student" and bob[0]["side"] == "CREDIT"
    assert client.get("/api/student/contacts", auth=AUTH).json()["items"] == [
        {"id": "bob", "owner_name": "Bob Student"}
    ]


def test_authorization_list_is_scoped_to_payer(client, payments):
    payments.fund("alice", "fund", 1000)
    intent = UUID(payments.create_intent("cafe", "intent", 600)["id"])
    payments.authorize("alice", "auth", intent)
    holds = client.get("/api/student/authorizations", auth=AUTH).json()["items"]
    assert len(holds) == 1 and holds[0]["merchant_name"] == "Campus Cafe" and holds[0]["amount"] == 600
    assert client.get("/api/student/authorizations", auth=("bob", AUTH[1])).json()["items"] == []
    payments.void("alice", "cancel", intent)
    assert client.get("/api/student/authorizations", auth=AUTH).json()["items"] == []


def test_concurrent_catalog_purchases_do_not_overspend(client, payments, db):
    from campuspay.student import Purchase, purchase

    payments.fund("alice", "fund", 1000)
    barrier = Barrier(10)

    def run(i):
        from types import SimpleNamespace

        from campuspay.security import Actor

        barrier.wait(timeout=10)
        try:
            return purchase(
                SimpleNamespace(app=client.app),
                Purchase(product_id="cafe-breakfast", expected_amount=650),
                Actor("alice", "STUDENT"),
                f"parallel-{i}",
            )
        except Problem as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(run, range(10)))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert results.count("INSUFFICIENT_AVAILABLE_FUNDS") == 9
    assert balance(db)["balance"] == 350
    assert scalar(db, "SELECT count(*) FROM intents") == 1
