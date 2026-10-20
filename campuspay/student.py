"""Student-facing read models and fixed-price, immediate-capture demo purchases."""

from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from campuspay.errors import require
from campuspay.payments import available, event, lock_accounts, post, result
from campuspay.schemas import Money, StrictModel
from campuspay.security import Actor, role

router = APIRouter(prefix="/api/student", tags=["student dashboard"])
Student = Annotated[Actor, Depends(role("STUDENT"))]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]
WEB = Path(__file__).parent / "web"

# Explicit demo catalog. Prices are controlled by the server, never by the browser.
CATALOG = (
    {
        "id": "cafe-breakfast",
        "merchant_id": "cafe",
        "name": "Coffee & pastry",
        "description": "A coffee and a fresh pastry.",
        "amount": 650,
        "category": "Food & drink",
    },
    {
        "id": "cafe-lunch",
        "merchant_id": "cafe",
        "name": "Lunch bowl",
        "description": "A warm bowl for your lunch break.",
        "amount": 1200,
        "category": "Food & drink",
    },
    {
        "id": "books-notebook",
        "merchant_id": "books",
        "name": "Campus notebook",
        "description": "An everyday notebook for your classes.",
        "amount": 850,
        "category": "Books & supplies",
    },
    {
        "id": "books-study-kit",
        "merchant_id": "books",
        "name": "Study essentials",
        "description": "Notebooks, pens and sticky notes.",
        "amount": 2400,
        "category": "Books & supplies",
    },
)


class Purchase(StrictModel):
    product_id: Annotated[str, Field(min_length=1, max_length=64)]
    expected_amount: Money


@router.get("/catalog")
def catalog(request: Request, actor: Student):
    with request.app.state.db.connection() as conn:
        merchants = {
            r["id"]: r["owner_name"]
            for r in conn.execute("SELECT id,owner_name FROM accounts WHERE kind='MERCHANT'")
        }
    return {
        "items": [
            {**p, "merchant_name": merchants[p["merchant_id"]], "currency": "USD"}
            for p in CATALOG
            if p["merchant_id"] in merchants
        ]
    }


@router.get("/contacts")
def contacts(request: Request, actor: Student):
    with request.app.state.db.connection() as conn:
        rows = conn.execute(
            "SELECT id,owner_name FROM accounts WHERE kind='STUDENT' AND id<>%s ORDER BY owner_name LIMIT 50",
            (actor.name,),
        ).fetchall()
    return {"items": rows}


@router.get("/activity")
def activity(
    request: Request,
    actor: Student,
    before: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
):
    with request.app.state.db.connection() as conn:
        rows = conn.execute(
            """SELECT e.id,e.journal_id,e.side,e.amount,j.kind,j.created_at,
            a.owner_name AS counterparty,a.id AS counterparty_id
            FROM entries e JOIN journals j ON j.id=e.journal_id
            JOIN entries other ON other.journal_id=e.journal_id AND other.account_id<>e.account_id
            JOIN accounts a ON a.id=other.account_id
            WHERE e.account_id=%s AND (%s::bigint IS NULL OR e.id<%s)
            ORDER BY e.id DESC LIMIT %s""",
            (actor.name, before, before, limit + 1),
        ).fetchall()
    more = len(rows) > limit
    items = rows[:limit]
    return {"items": items, "nextCursor": items[-1]["id"] if more else None}


@router.get("/authorizations")
def authorizations(request: Request, actor: Student):
    with request.app.state.db.connection() as conn:
        rows = conn.execute(
            """SELECT i.id,i.amount,i.status,i.expires_at,a.owner_name AS merchant_name
            FROM intents i JOIN accounts a ON a.id=i.merchant_id
            WHERE i.payer_id=%s AND i.status='AUTHORIZED' ORDER BY i.expires_at LIMIT 100""",
            (actor.name,),
        ).fetchall()
    return {"items": rows}


@router.post("/purchases")
def purchase(request: Request, body: Purchase, actor: Student, key: Key):
    def action(conn):
        product = next((p for p in CATALOG if p["id"] == body.product_id), None)
        require(product is not None, "PRODUCT_NOT_FOUND", 404)
        require(body.expected_amount == product["amount"], "PRICE_CHANGED")
        accounts = lock_accounts(conn, actor.name, product["merchant_id"])
        require(
            accounts[actor.name]["kind"] == "STUDENT"
            and accounts[product["merchant_id"]]["kind"] == "MERCHANT",
            "FORBIDDEN",
            403,
        )
        available(accounts[actor.name], product["amount"])
        intent = str(uuid4())
        # Fixed demo merchants accept immediate capture; manual QR intents retain separate authorization/capture.
        conn.execute(
            """INSERT INTO intents(id,merchant_id,payer_id,amount,status,expires_at)
            VALUES (%s,%s,%s,%s,'CAPTURED',now()+interval '15 minutes')""",
            (intent, product["merchant_id"], actor.name, product["amount"]),
        )
        post(
            conn,
            "CAPTURE",
            accounts[actor.name],
            accounts[product["merchant_id"]],
            product["amount"],
            f"capture:{intent}",
        )
        event(conn, intent, "CAPTURED", product["amount"], product["merchant_id"])
        return {
            **result(intent, "CAPTURED", product["amount"]),
            "product": product["name"],
            "merchant": accounts[product["merchant_id"]]["owner_name"],
        }

    return request.app.state.payments.execute(actor.name, key, "catalog_purchase", body.model_dump(), action)


def install(app):
    app.include_router(router)
    app.mount("/assets", StaticFiles(directory=WEB), name="student-assets")

    @app.get("/", include_in_schema=False)
    @app.get("/student", include_in_schema=False)
    def dashboard():
        return FileResponse(
            WEB / "index.html",
            headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                "Referrer-Policy": "no-referrer",
            },
        )
