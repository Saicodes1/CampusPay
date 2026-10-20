import json
import logging
from datetime import UTC, datetime

from prometheus_client import Counter, Histogram

REQUESTS = Counter("campuspay_http_requests", "HTTP responses", ["method", "route", "status"])
LATENCY = Histogram("campuspay_http_seconds", "HTTP duration", ["method", "route"])
WORK = Counter("campuspay_worker_operations", "Worker outcomes", ["worker", "outcome"])


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        data.update(getattr(record, "fields", {}))
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
