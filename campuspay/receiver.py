"""Demo merchant receiver: verify signatures, deduplicate events durably in SQLite."""

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID

from campuspay.config import Settings
from campuspay.observability import configure_logging
from campuspay.workers import signature

log = logging.getLogger(__name__)


def verify(secret: str, timestamp: str, body: bytes, supplied: str) -> bool:
    try:
        return abs(time.time() - int(timestamp)) <= 300 and hmac.compare_digest(
            signature(secret, timestamp, body), supplied
        )
    except (ValueError, TypeError):
        return False


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/events":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 8192:
                self.send_error(413)
                return
            body = self.rfile.read(length)
            if not verify(
                self.server.secret,
                self.headers.get("X-CampusPay-Timestamp", ""),
                body,
                self.headers.get("X-CampusPay-Signature", ""),
            ):
                self.send_error(401)
                return
            event = json.loads(body)
            event_id = str(UUID(event["eventId"]))
            if event_id != self.headers.get("X-CampusPay-Event-Id"):
                self.send_error(400)
                return
            with sqlite3.connect(self.server.database) as conn:
                digest = hashlib.sha256(body).hexdigest()
                conn.execute("INSERT OR IGNORE INTO inbox(event_id,digest) VALUES (?,?)", (event_id, digest))
                old = conn.execute("SELECT digest FROM inbox WHERE event_id=?", (event_id,)).fetchone()[0]
                if old != digest:
                    self.send_error(409)
                    return
            log.info(
                "Merchant event accepted", extra={"fields": {"eventId": event_id, "type": event["type"]}}
            )
            self.send_response(204)
            self.end_headers()
        except (ValueError, KeyError):
            self.send_error(400)

    def log_message(self, *_):
        pass


def main():
    configure_logging()
    settings = Settings()
    database = os.getenv("RECEIVER_DATABASE", "work/receiver.sqlite")
    from pathlib import Path

    Path(database).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS inbox(event_id TEXT PRIMARY KEY,digest TEXT NOT NULL)")
    server = ThreadingHTTPServer(("0.0.0.0", 8090), Handler)
    server.secret, server.database = settings.webhook_secret, database
    server.serve_forever()


if __name__ == "__main__":
    main()
