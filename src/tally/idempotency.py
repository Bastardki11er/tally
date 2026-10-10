"""Idempotency keys: a retried request returns the original result instead of moving money twice.

The key is claimed inside the *same* database transaction as the operation it
protects. Either both commit or neither does, so there is no window where money
moved but the key wasn't recorded (or the reverse), even if the process crashes.

Two concurrent requests with the same key are serialized by the primary key:
the second INSERT waits until the first transaction finishes, then sees the
committed key and returns the stored response. That relies on READ COMMITTED
(Postgres' default, and what tally.db uses); under REPEATABLE READ the INSERT
would raise a serialization error instead.

Keys are global per operation, because Tally has no notion of separate API
clients yet. Once it does, the client's id must become part of the key, or one
client could receive another client's stored response.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from fastapi.encoders import jsonable_encoder
from psycopg import Connection
from psycopg.types.json import Jsonb

from .errors import conflict, unprocessable

# How long a key is remembered. A retry after this is treated as a new request.
REPLAY_WINDOW_HOURS = 24
PURGE_BATCH_SIZE = 5_000


@dataclass(frozen=True)
class Outcome:
    status_code: int
    body: Any
    replayed: bool


def request_fingerprint(payload: Any) -> str:
    """Hash of the request's meaning: field order and omitted defaults don't change it."""
    canonical = json.dumps(jsonable_encoder(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def run_idempotent(
    conn: Connection, scope: str, key: str, payload: Any, operation: Callable[[], Any], status_code: int = 201
) -> Outcome:
    """Runs ``operation`` once per (scope, key); later calls with the same key replay its response.

    Only successful results are stored. A request that fails (say, insufficient
    funds) rolls back together with its key, so the client may retry it.
    """
    full_key = f"{scope}:{key}"
    fingerprint = request_fingerprint(payload)
    for _ in range(2):
        if _claim(conn, full_key, fingerprint):
            body = jsonable_encoder(operation())
            conn.execute(
                "UPDATE idempotency_keys SET status_code = %s, response = %s WHERE key = %s",
                (status_code, Jsonb(body), full_key),
            )
            return Outcome(status_code, body, replayed=False)
        stored = conn.execute(
            "SELECT request_hash, status_code, response FROM idempotency_keys WHERE key = %s", (full_key,)
        ).fetchone()
        if stored is None:
            # The purge job deleted this expired key between our INSERT and SELECT.
            # It is gone for good, so claim it afresh.
            continue
        if stored["request_hash"] != fingerprint:
            raise unprocessable(
                "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was already used with a different request body"
            )
        return Outcome(stored["status_code"], stored["response"], replayed=True)
    raise AssertionError(f"idempotency key {full_key} kept disappearing")


def _claim(conn: Connection, full_key: str, fingerprint: str) -> bool:
    """Inserts the key and returns True, or False if it already exists.

    If the original request is still running, its uncommitted key makes this INSERT
    wait. The wait is capped by the connection's lock_timeout; when that runs out,
    the client gets a clear "still in progress" conflict instead of a server error.
    """
    try:
        claimed = conn.execute(
            "INSERT INTO idempotency_keys (key, request_hash) VALUES (%s, %s) "
            "ON CONFLICT (key) DO NOTHING RETURNING key",
            (full_key, fingerprint),
        ).fetchone()
    except psycopg.errors.LockNotAvailable as exc:
        raise conflict(
            "IDEMPOTENCY_KEY_IN_USE", "A request with this Idempotency-Key is still being processed; retry shortly"
        ) from exc
    return claimed is not None


def purge_expired(
    conn: Connection, max_age_hours: int = REPLAY_WINDOW_HOURS, batch_size: int = PURGE_BATCH_SIZE
) -> int:
    """Deletes one batch of keys older than the replay window and returns how many it deleted.

    Call it until it returns less than ``batch_size``. A single big DELETE is all or
    nothing: after a long backlog it would exceed statement_timeout, roll back, and
    fail the same way on every run (measured: about 1.7M keys deleted per second, so
    a 30 s timeout caps one DELETE near 50M keys, roughly 14 hours of keys at 1,000
    transfers/s). Small batches always make progress and keep each transaction short.
    SKIP LOCKED lets several instances purge at once without waiting on each other.
    """
    return conn.execute(
        """
        DELETE FROM idempotency_keys
         WHERE key IN (SELECT key FROM idempotency_keys
                        WHERE created_at < now() - make_interval(hours => %s)
                        ORDER BY created_at
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED)
        """,
        (max_age_hours, batch_size),
    ).rowcount
