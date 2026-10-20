from uuid import uuid4

import pytest
import redis

from campuspay.api import create_app
from tests.conftest import captured

pytestmark = pytest.mark.integration
PASSWORD = "campuspay-demo-only"


def test_api_authentication_roles_and_validation(client):
    assert client.get("/api/wallet").status_code == 401
    assert client.get("/api/wallet", auth=("alice", "wrong")).status_code == 401
    assert client.get("/api/wallet", auth=("unknown", PASSWORD)).status_code == 401
    assert (
        client.post(
            "/api/funding", json={"amount": 100}, auth=("cafe", PASSWORD), headers={"Idempotency-Key": "x"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/intents", json={"amount": 100}, auth=("alice", PASSWORD), headers={"Idempotency-Key": "x"}
        ).status_code
        == 403
    )
    assert client.post("/api/funding", json={"amount": 100}, auth=("alice", PASSWORD)).status_code == 422
    assert (
        client.post(
            "/api/funding", json={"amount": "100"}, auth=("alice", PASSWORD), headers={"Idempotency-Key": "x"}
        ).status_code
        == 422
    )
    assert client.get("/api/admin/reconciliation", auth=("alice", PASSWORD)).status_code == 403
    assert client.get("/metrics", auth=("alice", PASSWORD)).status_code == 403
    assert client.get("/metrics", auth=("admin", PASSWORD)).status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_http_flow_qr_and_idempotency(client):
    def post(path, actor, key, body=None):
        response = client.post(path, json=body, auth=(actor, PASSWORD), headers={"Idempotency-Key": key})
        assert response.status_code == 200, response.text
        return response.json()

    one = post("/api/funding", "alice", "fund", {"amount": 5000})
    assert post("/api/funding", "alice", "fund", {"amount": 5000}) == one
    mismatch = client.post(
        "/api/funding", json={"amount": 5001}, auth=("alice", PASSWORD), headers={"Idempotency-Key": "fund"}
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    intent = post("/api/intents", "cafe", "intent", {"amount": 1000})
    qr = client.get(intent["qrImage"], auth=("alice", PASSWORD))
    assert qr.status_code == 200 and b"<svg" in qr.content
    assert intent["qrPayload"] == "campuspay:intent:" + intent["id"]
    post(f"/api/intents/{intent['id']}/authorize", "alice", "auth")
    post(f"/api/intents/{intent['id']}/capture", "cafe", "capture")
    post(f"/api/intents/{intent['id']}/refunds", "cafe", "refund", {"amount": 200})
    post("/api/settlements", "cafe", "settle", {"amount": 800})
    assert client.get("/api/admin/reconciliation", auth=("admin", PASSWORD)).json()["balanced"]
    entries = client.get("/api/ledger?limit=1", auth=("alice", PASSWORD)).json()
    assert len(entries["items"]) == 1
    next_page = client.get(f"/api/ledger?after={entries['nextCursor']}", auth=("alice", PASSWORD)).json()
    assert all(e["id"] > entries["nextCursor"] for e in next_page["items"])


def test_intent_and_wallet_access_are_scoped(client, payments):
    intent = captured(payments)
    assert client.get(f"/api/intents/{intent}", auth=("bob", PASSWORD)).status_code == 403
    assert client.get(f"/api/intents/{intent}", auth=("books", PASSWORD)).status_code == 403
    assert client.get(f"/api/intents/{intent}", auth=("alice", PASSWORD)).status_code == 200
    assert client.get(f"/api/intents/{uuid4()}", auth=("alice", PASSWORD)).status_code == 404
    assert client.get("/api/wallet", auth=("bob", PASSWORD)).json()["balance"] == 0
    assert client.get("/api/ledger", auth=("bob", PASSWORD)).json()["items"] == []


def test_redis_rate_limit_is_enforced(settings, db, cache):
    from fastapi.testclient import TestClient

    limited = settings.model_copy(update={"rate_limit": 2})
    with TestClient(create_app(limited, db, cache)) as client:
        assert client.get("/api/wallet", auth=("alice", PASSWORD)).status_code == 200
        assert client.get("/api/wallet", auth=("alice", PASSWORD)).status_code == 200
        response = client.get("/api/wallet", auth=("alice", PASSWORD))
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"


def test_redis_outage_fails_closed_without_writing(client, monkeypatch):
    from tests.conftest import scalar

    def unavailable(*_):
        raise redis.ConnectionError("injected cache outage")

    monkeypatch.setattr(client.app.state.redis, "eval", unavailable)
    response = client.post(
        "/api/funding", json={"amount": 100}, auth=("alice", PASSWORD), headers={"Idempotency-Key": "fund"}
    )
    assert response.status_code == 503
    assert scalar(client.app.state.db, "SELECT count(*) FROM journals") == 0


def test_browser_origin_and_request_id_handling(client):
    response = client.post(
        "/api/funding",
        json={"amount": 100},
        auth=("alice", PASSWORD),
        headers={"Origin": "https://malicious.example", "Idempotency-Key": "x"},
    )
    assert response.status_code == 403
    response = client.get("/health/live", headers={"X-Request-Id": "my-request-1"})
    assert response.headers["X-Request-Id"] == "my-request-1"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_openapi_describes_idempotency_and_basic_auth(client):
    spec = client.get("/openapi.json").json()
    assert spec["components"]["securitySchemes"]["HTTPBasic"]["scheme"] == "basic"
    params = spec["paths"]["/api/funding"]["post"]["parameters"]
    assert any(p["name"] == "Idempotency-Key" and p["required"] for p in params)
