"""HTTP API.

Money-moving endpoints require an ``Idempotency-Key`` header; creating accounts and
voiding holds accept one. Repeating a request with the same key returns the original
response (with ``Idempotent-Replayed: true``) instead of doing the work again.

Every error is an RFC 9457 problem document with a stable ``code``. Errors that a
retry can fix (a busy row, a slow query, an exhausted pool, an unreachable database)
are 503 with ``Retry-After``; anything unexpected is a 500 that reveals nothing
about the internals.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

import psycopg
import psycopg_pool
from fastapi import FastAPI, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, idempotency
from .config import Settings
from .db import Database, migrate
from .errors import LedgerError, unprocessable
from .idempotency import Outcome, run_idempotent
from .ledger import EXPIRY_BATCH_SIZE, Ledger, Leg
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
    Problem,
    ReversalIn,
    StatementLine,
    TransactionIn,
    TransactionOut,
    TransferIn,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# The largest legitimate request (100 entries plus 4 KB of metadata) is well under this.
MAX_BODY_BYTES = 64 * 1024

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        description="Unique per logical request; retries reuse it",
    ),
]
OptionalIdempotencyKey = Annotated[
    str | None,
    Header(alias="Idempotency-Key", min_length=1, max_length=255, description="Optional: makes retries safe"),
]

# Module level, not inside create_app: with postponed annotations FastAPI resolves
# these names from the module's globals.
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=200, description="next_cursor from the previous page")]

# Failures a retry can fix, as (code, message, seconds to wait). Matched by exception
# class, most specific first, so a lock timeout isn't reported as "database down".
TRANSIENT_ERRORS: dict[type[Exception], tuple[str, str, int]] = {
    psycopg.errors.LockNotAvailable: ("LOCK_TIMEOUT", "An account involved is busy; retry shortly", 1),
    psycopg.errors.QueryCanceled: ("QUERY_TIMEOUT", "The database took too long to answer; retry shortly", 5),
    psycopg_pool.PoolTimeout: ("SERVER_BUSY", "No database connection became free in time; retry shortly", 2),
    psycopg.OperationalError: ("DATABASE_UNAVAILABLE", "The database can't be reached right now; retry shortly", 5),
}


def problem(status: int, code: str, detail: str, headers: dict | None = None, **extra) -> JSONResponse:
    """RFC 9457 problem details, with a stable ``code`` clients can switch on."""
    body = {"type": "about:blank", "title": code, "status": status, "detail": detail, "code": code, **extra}
    return JSONResponse(body, status_code=status, media_type="application/problem+json", headers=headers)


def respond(outcome: Outcome, location: Callable[[dict], str] | None = None) -> JSONResponse:
    headers = {"Idempotent-Replayed": str(outcome.replayed).lower()}
    if location and outcome.status_code == 201:
        headers["Location"] = location(outcome.body)
    return JSONResponse(outcome.body, status_code=outcome.status_code, headers=headers)


def encode_cursor(*values: Any) -> str:
    """An opaque page token. Clients pass it back unchanged, so its contents can change freely."""
    return base64.urlsafe_b64encode(json.dumps(jsonable_encoder(values)).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None, *types: Callable[[Any], Any]) -> tuple | None:
    if cursor is None:
        return None
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if len(values) != len(types):
            raise ValueError("wrong shape")
        return tuple(kind(value) for kind, value in zip(types, values, strict=True))
    except (ValueError, TypeError) as exc:
        raise unprocessable("INVALID_CURSOR", "cursor is not one this API issued") from exc


class BodyTooLarge(StarletteHTTPException):
    # An HTTPException on purpose: FastAPI's body parser turns any other exception
    # raised while reading the body into a generic 400, but re-raises these.
    def __init__(self, max_bytes: int) -> None:
        super().__init__(413, f"Request bodies are limited to {max_bytes} bytes")


class BodySizeLimit:
    """ASGI middleware that refuses request bodies over ``max_bytes`` with 413.

    Checks the declared Content-Length first, so an honest oversized request is
    refused before any of it is read. It also counts the bytes actually received,
    so a chunked request without a length can't get around it.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            await http_problem(None, BodyTooLarge(self.max_bytes))(scope, receive, send)
            return
        received = 0

        async def counted_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > self.max_bytes:
                # Raised inside the app, whose HTTPException handler renders the 413.
                raise BodyTooLarge(self.max_bytes)
            return message

        await self.app(scope, counted_receive, send)


def http_problem(request: Request | None, exc: StarletteHTTPException) -> JSONResponse:
    """Framework errors (unknown route, wrong method, body too large) as problem documents too."""
    return problem(exc.status_code, HTTPStatus(exc.status_code).name, str(exc.detail), headers=exc.headers)


def _maintenance_loop(db: Database, ledger: Ledger, interval: float, stop: threading.Event) -> None:
    while not stop.wait(interval):
        try:
            # A full batch means there may be more: keep going until one comes back short.
            while db.run(lambda conn: ledger.expire_holds(conn)) == EXPIRY_BATCH_SIZE:
                pass
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
        # Every operation documents the error shape in /docs.
        responses={"default": {"model": Problem, "description": "Error, as RFC 9457 problem details"}},
    )
    app.add_middleware(BodySizeLimit, max_bytes=MAX_BODY_BYTES)

    app.add_exception_handler(StarletteHTTPException, http_problem)

    @app.exception_handler(LedgerError)
    async def ledger_error(request: Request, exc: LedgerError) -> JSONResponse:
        return problem(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return problem(422, "VALIDATION_ERROR", "The request is invalid", errors=errors)

    def transient_error(code: str, detail: str, retry_after: int):
        async def handle(request: Request, exc: Exception) -> JSONResponse:
            log.warning("%s on %s %s: %s", code, request.method, request.url.path, exc)
            return problem(503, code, detail, headers={"Retry-After": str(retry_after)})

        return handle

    for error_type, (code, detail, retry_after) in TRANSIENT_ERRORS.items():
        app.add_exception_handler(error_type, transient_error(code, detail, retry_after))

    @app.exception_handler(psycopg.errors.IntegrityError)
    async def integrity_error(request: Request, exc: psycopg.errors.IntegrityError) -> JSONResponse:
        # Should be unreachable: the service validates first. If it happens, the database
        # caught a bug before it could corrupt the books. The constraint's name and the
        # failing row go to the log, not to the client.
        log.error("database constraint rejected a write: %s", exc)
        return problem(409, "CONSTRAINT_VIOLATION", "The request conflicts with a ledger rule")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return problem(500, "INTERNAL_ERROR", "Something went wrong on our side")

    def db(request: Request) -> Database:
        return request.app.state.db

    def ledger(request: Request) -> Ledger:
        return request.app.state.ledger

    def page(rows: list[dict], limit: int, key: Callable[[dict], tuple]) -> dict:
        return {"data": rows, "next_cursor": encode_cursor(*key(rows[-1])) if len(rows) == limit else None}

    # --- accounts -----------------------------------------------------------------

    @app.post("/v1/accounts", status_code=201, response_model=AccountOut, tags=["accounts"])
    def create_account(body: AccountIn, request: Request, key: OptionalIdempotencyKey = None):
        def create(conn):
            return ledger(request).create_account(conn, **body.model_dump())

        def work(conn):
            if key is None:
                return Outcome(201, jsonable_encoder(create(conn)), replayed=False)
            return run_idempotent(conn, "accounts", key, body, lambda: create(conn))

        return respond(db(request).run(work), location=lambda account: f"/v1/accounts/{account['id']}")

    @app.get("/v1/accounts", response_model=Page[AccountOut], tags=["accounts"])
    def list_accounts(request: Request, limit: Limit = 50, cursor: Cursor = None):
        after = decode_cursor(cursor, int)
        rows = db(request).run(lambda conn: ledger(request).list_accounts(conn, limit, after[0] if after else None))
        return page(rows, limit, key=lambda account: (account["id"],))

    @app.get("/v1/accounts/{account_id}", response_model=AccountOut, tags=["accounts"])
    def get_account(account_id: int, request: Request):
        return db(request).run(lambda conn: ledger(request).get_account(conn, account_id))

    @app.get("/v1/accounts/{account_id}/entries", response_model=Page[StatementLine], tags=["accounts"])
    def statement(account_id: int, request: Request, limit: Limit = 50, cursor: Cursor = None):
        before = decode_cursor(cursor, int)
        rows = db(request).run(
            lambda conn: ledger(request).statement(conn, account_id, limit, before[0] if before else None)
        )
        return page(rows, limit, key=lambda entry: (entry["id"],))

    @app.get("/v1/accounts/{account_id}/balance", response_model=BalanceOut, tags=["accounts"])
    def balance_at(
        account_id: int,
        request: Request,
        # Aware only: a time without a zone would be read in the database session's
        # time zone and silently answer for a different moment.
        as_of: Annotated[AwareDatetime | None, Query(description="ISO 8601 with a time zone; defaults to now")] = None,
    ):
        moment = as_of or datetime.now(UTC)
        balance = db(request).run(lambda conn: ledger(request).balance_at(conn, account_id, moment))
        return {"account_id": account_id, "as_of": moment, "balance": balance}

    @app.get("/v1/accounts/{account_id}/holds", response_model=Page[HoldOut], tags=["holds"])
    def account_holds(
        account_id: int,
        request: Request,
        status: Literal["active", "captured", "voided", "expired"] | None = None,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        before = decode_cursor(cursor, datetime.fromisoformat, UUID)
        rows = db(request).run(lambda conn: ledger(request).list_holds(conn, account_id, status, limit, before))
        return page(rows, limit, key=lambda hold: (hold["created_at"], hold["id"]))

    # --- transactions -------------------------------------------------------------

    def transaction_location(transaction: dict) -> str:
        return f"/v1/transactions/{transaction['id']}"

    @app.post("/v1/transactions", status_code=201, response_model=TransactionOut, tags=["transactions"])
    def post_transaction(body: TransactionIn, key: IdempotencyKey, request: Request):
        legs = [Leg(e.account_id, e.direction, e.amount) for e in body.entries]
        outcome = db(request).run(
            lambda conn: run_idempotent(
                conn,
                "transactions",
                key,
                body,
                lambda: ledger(request).post(conn, legs, body.description, body.metadata),
            )
        )
        return respond(outcome, transaction_location)

    @app.post("/v1/transfers", status_code=201, response_model=TransactionOut, tags=["transactions"])
    def transfer(body: TransferIn, key: IdempotencyKey, request: Request):
        def work(conn):
            return ledger(request).transfer(
                conn, body.debit_account_id, body.credit_account_id, body.amount, body.description, body.metadata
            )

        outcome = db(request).run(lambda conn: run_idempotent(conn, "transfers", key, body, lambda: work(conn)))
        return respond(outcome, transaction_location)

    @app.get("/v1/transactions", response_model=Page[TransactionOut], tags=["transactions"])
    def recent_transactions(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20, cursor: Cursor = None):
        """Most recent transactions first."""
        before = decode_cursor(cursor, datetime.fromisoformat, UUID)
        rows = db(request).run(lambda conn: ledger(request).recent_transactions(conn, limit, before))
        return page(rows, limit, key=lambda transaction: (transaction["posted_at"], transaction["id"]))

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
        outcome = db(request).run(
            lambda conn: run_idempotent(
                conn, "reversals", key, payload, lambda: ledger(request).reverse(conn, transaction_id, body.description)
            )
        )
        return respond(outcome, transaction_location)

    # --- holds --------------------------------------------------------------------

    @app.post("/v1/holds", status_code=201, response_model=HoldOut, tags=["holds"])
    def create_hold(body: HoldIn, key: IdempotencyKey, request: Request):
        def create(conn):
            expires_at = datetime.now(UTC) + timedelta(seconds=body.expires_in_seconds)
            return ledger(request).create_hold(conn, body.account_id, body.amount, expires_at, body.description)

        outcome = db(request).run(lambda conn: run_idempotent(conn, "holds", key, body, lambda: create(conn)))
        return respond(outcome, location=lambda hold: f"/v1/holds/{hold['id']}")

    @app.get("/v1/holds/{hold_id}", response_model=HoldOut, tags=["holds"])
    def get_hold(hold_id: UUID, request: Request):
        return db(request).run(lambda conn: ledger(request).get_hold(conn, hold_id))

    @app.post("/v1/holds/{hold_id}/capture", response_model=CaptureOut, tags=["holds"])
    def capture(hold_id: UUID, body: CaptureIn, key: IdempotencyKey, request: Request):
        payload = {"hold_id": hold_id, **body.model_dump()}

        def work(conn):
            return ledger(request).capture_hold(
                conn, hold_id, body.destination_account_id, body.amount, body.description
            )

        outcome = db(request).run(
            lambda conn: run_idempotent(conn, "captures", key, payload, lambda: work(conn), status_code=200)
        )
        return respond(outcome)

    @app.post("/v1/holds/{hold_id}/void", response_model=HoldOut, tags=["holds"])
    def void(hold_id: UUID, request: Request, key: OptionalIdempotencyKey = None):
        # Without a key, a retried void whose first attempt succeeded gets HOLD_NOT_ACTIVE.
        def work(conn):
            if key is None:
                return Outcome(200, jsonable_encoder(ledger(request).void_hold(conn, hold_id)), replayed=False)
            return run_idempotent(
                conn, "voids", key, {"hold_id": hold_id}, lambda: ledger(request).void_hold(conn, hold_id), 200
            )

        return respond(db(request).run(work))

    # --- operations ---------------------------------------------------------------

    @app.get("/v1/reconciliation", tags=["operations"])
    def reconciliation(request: Request):
        """Re-derives the books from raw entries and holds and reports any disagreement.

        Synchronous: fine for small and medium ledgers. For big ones run ``tally
        reconcile`` on a schedule instead of holding an HTTP request open.
        """
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
