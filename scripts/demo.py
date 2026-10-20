"""uv run python scripts/demo.py -- each invocation uses fresh idempotency keys."""

import argparse
import json
import os
import time
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    password = os.getenv("CAMPUSPAY_DEMO_PASSWORD", "campuspay-demo-only")
    run = uuid4().hex[:12]
    with httpx.Client(base_url=args.url, timeout=10, trust_env=False) as client:

        def post(path, actor, label, body=None):
            response = client.post(
                path, auth=(actor, password), json=body, headers={"Idempotency-Key": f"demo-{run}-{label}"}
            )
            response.raise_for_status()
            data = response.json()
            print(f"{label}: {json.dumps(data)}")
            return data

        print("CampusPay: SIMULATED FUNDS ONLY. All amounts below are USD cents.")
        original = post("/api/funding", "alice", "fund", {"amount": 10000})
        replay = post("/api/funding", "alice", "fund", {"amount": 10000})
        assert replay == original
        post("/api/transfers", "alice", "transfer", {"recipient": "bob", "amount": 1000})
        intent = post("/api/intents", "cafe", "intent", {"amount": 3000})
        path = f"/api/intents/{intent['id']}"
        post(path + "/authorize", "alice", "authorize")
        post(path + "/capture", "cafe", "capture")
        post(path + "/refunds", "cafe", "refund", {"amount": 500})
        post("/api/settlements", "cafe", "settle", {"amount": 2000})
        for actor in ("alice", "bob", "cafe"):
            response = client.get("/api/wallet", auth=(actor, password))
            response.raise_for_status()
            print(f"{actor}: {response.json()}")
        response = client.get("/api/admin/reconciliation", auth=("admin", password))
        response.raise_for_status()
        assert response.json()["balanced"]
        print("Ledger and holds reconcile.")
        # Wait for worker delivery; fail visibly if the complete flow does not finish.
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            response = client.get("/api/admin/operations", auth=("admin", password))
            response.raise_for_status()
            state = response.json()
            pending = any(
                r["status"] in ("PENDING", "DEAD") for k in ("outbox", "webhooks") for r in state[k]
            )
            if not pending and any(r["status"] == "DELIVERED" for r in state["webhooks"]):
                print("Outbox published; signed merchant webhooks delivered.")
                return
            time.sleep(1)
        raise RuntimeError(f"Workers did not finish: {state}")


if __name__ == "__main__":
    main()
