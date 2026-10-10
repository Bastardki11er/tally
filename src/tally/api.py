"""HTTP API.

Money-moving endpoints require an ``Idempotency-Key`` header. Repeating a request
with the same key returns the original response (with ``Idempotent-Replayed: true``)
instead of moving the money again.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from fastapi import FastAPI, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, idempotency
from .config import Settings
from .db import Database, migrate
from .errors import LedgerError
from .idempotency import Outcome, run_idempotent
from .ledger import Ledger, Leg
from .reconciliation import reconcile
from .schemas import (
    AccountIn,
    AccountOut,
    BalanceOut,
    CaptureIn,
    CaptureOut,
    HoldIn,
    HoldOut,
    Page,
    ReversalIn,
    StatementLine,
    TransactionIn,
    TransactionOut,
    TransferIn,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        description="Unique per logical request; retries reuse it",
    ),
]


def problem(status: int, code: str, detail: str, **extra) -> JSONResponse:
    """RFC 9457 problem details, with a stable ``code`` clients can switch on."""
    body = {"type": "about:blank", "title": code, "status": status, "detail": detail, "code": code, **extra}
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def replayable(outcome: Outcome) -> JSONResponse:
    return JSONResponse(
        outcome.body,
        status_code=outcome.status_code,
        headers={"Idempotent-Replayed": str(outcome.replayed).lower()},
    )


def _maintenance_loop(db: Database, ledger: Ledger, interval: float, stop: threading.Event) -> None:
    while not stop.wait(interval):
        try:
            while db.run(lambda conn: ledger.expire_holds(conn)) == 500:
                pass  # a full batch: there may be more
            while db.run(idempotency.purge_expired) == idempotency.PURGE_BATCH_SIZE:
                pass
        except Exception:  # keep the loop alive; the next tick retries
            log.exception("maintenance run failed")


def create_app(settings: Settings | None = None, *, run_migrations: bool = True, maintenance: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_migrations:
            migrate(settings.database_url)
        db = Database(
            settings.database_url,
            max_size=settings.pool_size,
            lock_timeout_ms=settings.lock_timeout_ms,
            statement_timeout_ms=settings.statement_timeout_ms,
        )
        app.state.db = db
        app.state.ledger = Ledger()
        stop = threading.Event()
        if maintenance:
            threading.Thread(
                target=_maintenance_loop,
                args=(db, app.state.ledger, settings.maintenance_interval_seconds, stop),
                daemon=True,
                name="tally-maintenance",
            ).start()
        yield
        stop.set()
        db.close()

    app = FastAPI(
        title="Tally",
        version=__version__,
        summary="A double-entry ledger API where the database guarantees the books always balance.",
        lifespan=lifespan,
    )

    @app.exception_handler(LedgerError)
    async def ledger_error(request: Request, exc: LedgerError) -> JSONResponse:
        return problem(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return problem(422, "VALIDATION_ERROR", "The request is invalid", errors=errors)

    @app.exception_handler(psycopg.errors.IntegrityError)
    async def integrity_error(request: Request, exc: psycopg.errors.IntegrityError) -> JSONResponse:
        # Should be unreachable: the service checks first. If it happens, the database
        # caught a bug before it could corrupt the books.
        log.error("database constraint rejected a write: %s", exc)
        return problem(409, "CONSTRAINT_VIOLATION", exc.diag.message_primary or "A ledger invariant was violated")

    def db(request: Request) -> Database:
        return request.app.state.db

    def ledger(request: Request) -> Ledger:
        return request.app.state.ledger

    def page(rows: list[dict], limit: int, cursor_field: str = "id") -> dict:
        return {"data": rows, "next_cursor": rows[-1][cursor_field] if len(rows) == limit else None}

    # --- accounts -----------------------------------------------------------------

    @app.post("/v1/accounts", status_code=201, response_model=AccountOut, tags=["accounts"])
    def create_account(body: AccountIn, request: Request):
        return db(request).run(lambda conn: ledger(request).create_account(conn, **body.model_dump()))

    @app.get("/v1/accounts", response_model=Page[AccountOut], tags=["accounts"])
    def list_accounts(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[int | None, Query(description="next_cursor from the previous page")] = None,
    ):
        rows = db(request).run(lambda conn: ledger(request).list_accounts(conn, limit, cursor))
        return page(rows, limit)

    @app.get("/v1/accounts/{account_id}", response_model=AccountOut, tags=["accounts"])
    def get_account(account_id: int, request: Request):
        return db(request).run(lambda conn: ledger(request).get_account(conn, account_id))

    @app.get("/v1/accounts/{account_id}/entries", response_model=Page[StatementLine], tags=["accounts"])
    def statement(
        account_id: int,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[int | None, Query(description="next_cursor from the previous page")] = None,
    ):
        rows = db(request).run(lambda conn: ledger(request).statement(conn, account_id, limit, cursor))
        return page(rows, limit)

    @app.get("/v1/accounts/{account_id}/balance", response_model=BalanceOut, tags=["accounts"])
    def balance_at(
        account_id: int,
        request: Request,
        as_of: Annotated[datetime | None, Query(description="ISO 8601; defaults to now")] = None,
    ):
        moment = as_of or datetime.now(UTC)
        balance = db(request).run(lambda conn: ledger(request).balance_at(conn, account_id, moment))
        return {"account_id": account_id, "as_of": moment, "balance": balance}

    @app.get("/v1/accounts/{account_id}/holds", response_model=Page[HoldOut], tags=["holds"])
    def account_holds(
        account_id: int,
        request: Request,
        status: Literal["active", "captured", "voided", "expired"] | None = None,
    ):
        rows = db(request).run(lambda conn: ledger(request).list_holds(conn, account_id, status))
        return {"data": rows, "next_cursor": None}

    # --- transactions -------------------------------------------------------------

    @app.post("/v1/transactions", status_code=201, response_model=TransactionOut, tags=["transactions"])
    def post_transaction(body: TransactionIn, key: IdempotencyKey, request: Request):
        legs = [Leg(e.account_id, e.direction, e.amount) for e in body.entries]
        return replayable(
            db(request).run(
                lambda conn: run_idempotent(
                    conn,
                    "transactions",
                    key,
                    body,
                    lambda: ledger(request).post(conn, legs, body.description, body.metadata),
                )
            )
        )

    @app.post("/v1/transfers", status_code=201, response_model=TransactionOut, tags=["transactions"])
    def transfer(body: TransferIn, key: IdempotencyKey, request: Request):
        return replayable(
            db(request).run(
                lambda conn: run_idempotent(
                    conn,
                    "transfers",
                    key,
                    body,
                    lambda: ledger(request).transfer(
                        conn,
                        body.debit_account_id,
                        body.credit_account_id,
                        body.amount,
                        body.description,
                        body.metadata,
                    ),
                )
            )
        )

    @app.get("/v1/transactions", response_model=Page[TransactionOut], tags=["transactions"])
    def recent_transactions(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20):
        """Most recent transactions first. Per-account history is paginated under /v1/accounts/{id}/entries."""
        rows = db(request).run(lambda conn: ledger(request).recent_transactions(conn, limit))
        return {"data": rows, "next_cursor": None}

    @app.get("/v1/transactions/{transaction_id}", response_model=TransactionOut, tags=["transactions"])
    def get_transaction(transaction_id: UUID, request: Request):
        return db(request).run(lambda conn: ledger(request).get_transaction(conn, transaction_id))

    @app.post(
        "/v1/transactions/{transaction_id}/reversal",
        status_code=201,
        response_model=TransactionOut,
        tags=["transactions"],
    )
    def reverse(transaction_id: UUID, body: ReversalIn, key: IdempotencyKey, request: Request):
        payload = {"transaction_id": transaction_id, **body.model_dump()}
        return replayable(
            db(request).run(
                lambda conn: run_idempotent(
                    conn,
                    "reversals",
                    key,
                    payload,
                    lambda: ledger(request).reverse(conn, transaction_id, body.description),
                )
            )
        )

    # --- holds --------------------------------------------------------------------

    @app.post("/v1/holds", status_code=201, response_model=HoldOut, tags=["holds"])
    def create_hold(body: HoldIn, key: IdempotencyKey, request: Request):
        def create(conn):
            expires_at = datetime.now(UTC) + timedelta(seconds=body.expires_in_seconds)
            return ledger(request).create_hold(conn, body.account_id, body.amount, expires_at, body.description)

        return replayable(db(request).run(lambda conn: run_idempotent(conn, "holds", key, body, lambda: create(conn))))

    @app.get("/v1/holds/{hold_id}", response_model=HoldOut, tags=["holds"])
    def get_hold(hold_id: UUID, request: Request):
        return db(request).run(lambda conn: ledger(request).get_hold(conn, hold_id))

    @app.post("/v1/holds/{hold_id}/capture", response_model=CaptureOut, tags=["holds"])
    def capture(hold_id: UUID, body: CaptureIn, key: IdempotencyKey, request: Request):
        payload = {"hold_id": hold_id, **body.model_dump()}
        return replayable(
            db(request).run(
                lambda conn: run_idempotent(
                    conn,
                    "captures",
                    key,
                    payload,
                    lambda: ledger(request).capture_hold(
                        conn, hold_id, body.destination_account_id, body.amount, body.description
                    ),
                    status_code=200,
                )
            )
        )

    @app.post("/v1/holds/{hold_id}/void", response_model=HoldOut, tags=["holds"])
    def void(hold_id: UUID, request: Request):
        return db(request).run(lambda conn: ledger(request).void_hold(conn, hold_id))

    # --- operations ---------------------------------------------------------------

    @app.get("/v1/reconciliation", tags=["operations"])
    def reconciliation(request: Request):
        """Recomputes every balance from the raw entries and reports any disagreement."""
        return jsonable_encoder(db(request).run(reconcile))

    @app.get("/health", tags=["operations"])
    def health(request: Request):
        db(request).run(lambda conn: conn.execute("SELECT 1"))
        return {"status": "ok"}

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def console():
            return FileResponse(STATIC_DIR / "index.html")

    return app
