import io
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

import psycopg
import qrcode
import qrcode.image.svg
import redis
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from psycopg_pool import PoolTimeout

from campuspay.config import Settings
from campuspay.db import migrate, pool
from campuspay.errors import Problem, require
from campuspay.observability import LATENCY, REQUESTS
from campuspay.payments import Payments
from campuspay.schemas import Amount, OperationResult, Transfer
from campuspay.security import Actor, authenticate, role
from campuspay.student import install as install_student_dashboard

log = logging.getLogger(__name__)
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]
Student = Annotated[Actor, Depends(role("STUDENT"))]
Merchant = Annotated[Actor, Depends(role("MERCHANT"))]
Admin = Annotated[Actor, Depends(role("ADMIN"))]
User = Annotated[Actor, Depends(authenticate)]


def create_app(settings: Settings | None = None, database=None, redis_client=None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.db = database or pool(settings.database_url)
        app.state.db.wait()
        migrate(app.state.db)
        app.state.payments = Payments(app.state.db)
        app.state.redis = redis_client or redis.Redis.from_url(
            settings.redis_url, socket_timeout=0.5, socket_connect_timeout=0.5
        )
        app.state.settings = settings
        yield
        if database is None:
            app.state.db.close()
        if redis_client is None:
            app.state.redis.close()

    app = FastAPI(
        title="CampusPay",
        version="1.0.0",
        lifespan=lifespan,
        description="SIMULATED FUNDS ONLY. University wallet and distributed payment processing demo. "
        "All amounts are integer USD cents. Every payment write requires Idempotency-Key. "
        "HTTP Basic is for a loopback-only demo. No Binance integration or affiliation.",
    )

    @app.middleware("http")
    async def observe(request: Request, call_next):
        candidate = request.headers.get("X-Request-Id", "")
        request.state.request_id = (
            candidate if re.fullmatch(r"[A-Za-z0-9-]{1,64}", candidate) else str(uuid4())
        )
        # Permit the dashboard's same-origin writes; reject cross-origin and opaque-origin mutations.
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != expected_origin:
            response = JSONResponse({"code": "BROWSER_ORIGIN_NOT_ALLOWED"}, status_code=403)
        elif (
            request.headers.get("content-length", "0").isdigit()
            and int(request.headers.get("content-length", "0")) > 8192
        ):
            response = JSONResponse({"code": "BODY_TOO_LARGE"}, status_code=413)
        else:
            started = time.monotonic()
            response = await call_next(request)
            route = getattr(request.scope.get("route"), "path", "unmatched")
            LATENCY.labels(request.method, route).observe(time.monotonic() - started)
            REQUESTS.labels(request.method, route, str(response.status_code)).inc()
            log.info(
                "HTTP response",
                extra={
                    "fields": {
                        "requestId": request.state.request_id,
                        "method": request.method,
                        "route": route,
                        "status": response.status_code,
                    }
                },
            )
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        headers = {"WWW-Authenticate": "Basic"} if exc.status == 401 else {}
        if exc.status == 429:
            headers["Retry-After"] = "60"
        return JSONResponse(
            {"code": exc.code, "requestId": request.state.request_id}, status_code=exc.status, headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        # Omit input values to avoid reflecting secrets or large bodies into errors/logs.
        return JSONResponse(
            {
                "code": "VALIDATION_ERROR",
                "fields": [list(e["loc"]) for e in exc.errors()],
                "requestId": request.state.request_id,
            },
            status_code=422,
        )

    @app.exception_handler(psycopg.Error)
    @app.exception_handler(PoolTimeout)
    async def db_error(request, exc):
        log.error(
            "Database operation rolled back",
            extra={"fields": {"requestId": request.state.request_id, "errorType": type(exc).__name__}},
        )
        return JSONResponse(
            {"code": "DATABASE_UNAVAILABLE_OR_CONFLICT", "requestId": request.state.request_id},
            status_code=503,
        )

    @app.get("/health/live", tags=["operations"])
    def live():
        return {"status": "up", "funds": "simulated"}

    @app.get("/health/ready", tags=["operations"])
    def ready(request: Request):
        with request.app.state.db.connection() as conn:
            conn.execute("SELECT 1")
        try:
            request.app.state.redis.ping()
        except redis.RedisError as exc:
            raise Problem(503, "REDIS_UNAVAILABLE") from exc
        # RabbitMQ intentionally not required to accept payments: outbox absorbs an outage.
        return {"status": "ready"}

    @app.get("/metrics", tags=["operations"])
    def metrics(actor: Admin):
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/wallet", tags=["wallets"])
    def wallet(request: Request, actor: User):
        require(actor.role != "ADMIN", "ADMIN_HAS_NO_WALLET", 404)
        with request.app.state.db.connection() as conn:
            row = conn.execute(
                "SELECT id,owner_name,kind,currency,balance,reserved,balance-reserved AS available FROM accounts WHERE id=%s",
                (actor.name,),
            ).fetchone()
        return row

    @app.get("/api/ledger", tags=["wallets"])
    def ledger(
        request: Request,
        actor: User,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        require(actor.role != "ADMIN", "FORBIDDEN", 403)
        with request.app.state.db.connection() as conn:
            rows = conn.execute(
                """SELECT e.id,e.journal_id,e.side,e.amount,j.kind,j.created_at FROM entries e
                                   JOIN journals j ON j.id=e.journal_id WHERE e.account_id=%s AND e.id>%s
                                   ORDER BY e.id LIMIT %s""",
                (actor.name, after, limit),
            ).fetchall()
        return {"items": rows, "nextCursor": rows[-1]["id"] if rows else after}

    @app.post(
        "/api/funding", response_model=OperationResult, response_model_exclude_none=True, tags=["wallets"]
    )
    def fund(request: Request, body: Amount, actor: Student, key: Key):
        return request.app.state.payments.fund(actor.name, key, body.amount)

    @app.post(
        "/api/transfers", response_model=OperationResult, response_model_exclude_none=True, tags=["wallets"]
    )
    def transfer(request: Request, body: Transfer, actor: Student, key: Key):
        return request.app.state.payments.transfer(actor.name, key, body.recipient, body.amount)

    @app.post(
        "/api/intents", response_model=OperationResult, response_model_exclude_none=True, tags=["payments"]
    )
    def create_intent(request: Request, body: Amount, actor: Merchant, key: Key):
        return request.app.state.payments.create_intent(actor.name, key, body.amount)

    def visible_intent(request, actor, intent_id):
        with request.app.state.db.connection() as conn:
            row = conn.execute("SELECT * FROM intents WHERE id=%s", (intent_id,)).fetchone()
        require(row is not None, "NOT_FOUND", 404)
        require(
            actor.name in (row["merchant_id"], row["payer_id"])
            or (actor.role == "STUDENT" and row["status"] == "CREATED"),
            "FORBIDDEN",
            403,
        )
        return row

    @app.get("/api/intents/{intent_id}", tags=["payments"])
    def intent(request: Request, intent_id: UUID, actor: User):
        return visible_intent(request, actor, intent_id)

    @app.get("/api/intents/{intent_id}/qr.svg", tags=["payments"], response_class=Response)
    def qr(request: Request, intent_id: UUID, actor: User):
        visible_intent(request, actor, intent_id)
        image = qrcode.make(f"campuspay:intent:{intent_id}", image_factory=qrcode.image.svg.SvgPathImage)
        output = io.BytesIO()
        image.save(output)
        return Response(output.getvalue(), media_type="image/svg+xml")

    @app.post(
        "/api/intents/{intent_id}/authorize",
        response_model=OperationResult,
        response_model_exclude_none=True,
        tags=["payments"],
    )
    def authorize(request: Request, intent_id: UUID, actor: Student, key: Key):
        return request.app.state.payments.authorize(actor.name, key, intent_id)

    @app.post(
        "/api/intents/{intent_id}/capture",
        response_model=OperationResult,
        response_model_exclude_none=True,
        tags=["payments"],
    )
    def capture(request: Request, intent_id: UUID, actor: Merchant, key: Key):
        return request.app.state.payments.capture(actor.name, key, intent_id)

    @app.post(
        "/api/intents/{intent_id}/void",
        response_model=OperationResult,
        response_model_exclude_none=True,
        tags=["payments"],
    )
    def void(request: Request, intent_id: UUID, actor: User, key: Key):
        return request.app.state.payments.void(actor.name, key, intent_id)

    @app.post(
        "/api/intents/{intent_id}/refunds",
        response_model=OperationResult,
        response_model_exclude_none=True,
        tags=["payments"],
    )
    def refund(request: Request, intent_id: UUID, body: Amount, actor: Merchant, key: Key):
        return request.app.state.payments.refund(actor.name, key, intent_id, body.amount)

    @app.post(
        "/api/settlements",
        response_model=OperationResult,
        response_model_exclude_none=True,
        tags=["settlement"],
    )
    def settle(request: Request, body: Amount, actor: Merchant, key: Key):
        return request.app.state.payments.settle(actor.name, key, body.amount)

    @app.get("/api/admin/operations", tags=["operations"])
    def operations(request: Request, actor: Admin):
        with request.app.state.db.connection() as conn:
            return {
                "outbox": conn.execute("SELECT status,count(*) FROM outbox GROUP BY status").fetchall(),
                "webhooks": conn.execute(
                    "SELECT status,count(*) FROM webhook_deliveries GROUP BY status"
                ).fetchall(),
                "deadOutbox": conn.execute(
                    "SELECT id,attempts FROM outbox WHERE status='DEAD' ORDER BY created_at LIMIT 100"
                ).fetchall(),
                "deadWebhooks": conn.execute(
                    "SELECT event_id,attempts,last_result FROM webhook_deliveries WHERE status='DEAD' ORDER BY event_id LIMIT 100"
                ).fetchall(),
            }

    @app.get("/api/admin/reconciliation", tags=["operations"])
    def reconciliation(request: Request, actor: Admin):
        with request.app.state.db.connection() as conn:
            # Read-only single snapshot: projection vs journal and hold totals.
            rows = conn.execute("""SELECT a.id,a.balance,a.reserved,
                COALESCE((SELECT SUM(CASE WHEN e.side=a.normal_side THEN e.amount::numeric ELSE -e.amount::numeric END)
                          FROM entries e WHERE e.account_id=a.id),0) AS ledger_balance,
                COALESCE((SELECT SUM(i.amount) FROM intents i WHERE i.payer_id=a.id AND i.status='AUTHORIZED'),0) AS ledger_reserved
                FROM accounts a ORDER BY a.id""").fetchall()
        return {
            "balanced": all(
                r["balance"] == r["ledger_balance"] and r["reserved"] == r["ledger_reserved"] for r in rows
            ),
            "accounts": rows,
        }

    @app.post("/api/admin/replay/{queue}/{event_id}", tags=["operations"])
    def replay(request: Request, queue: str, event_id: UUID, actor: Admin):
        require(queue in ("outbox", "webhooks"), "INVALID_QUEUE", 400)
        return request.app.state.payments.replay(
            "outbox" if queue == "outbox" else "webhook_deliveries", event_id
        )

    install_student_dashboard(app)
    return app


app = create_app()
