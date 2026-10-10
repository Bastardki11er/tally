"""Idempotency keys: a retried request returns the original result instead of moving money twice.

The key is claimed inside the *same* database transaction as the operation it
protects. Either both commit or neither does, so there is no window where money
moved but the key wasn't recorded (or the reverse), even if the process crashes.

Two concurrent requests with the same key are serialized by the primary key:
the second INSERT waits until the first transaction finishes, then sees the
committed key and returns the stored response.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi.encoders import jsonable_encoder
from psycopg import Connection
from psycopg.types.json import Jsonb

from .errors import unprocessable


@dataclass
class Outcome:
    status_code: int
    body: Any
    replayed: bool


def request_fingerprint(payload: Any) -> str:
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
    claimed = conn.execute(
        "INSERT INTO idempotency_keys (key, request_hash) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING RETURNING key",
        (full_key, fingerprint),
    ).fetchone()

    if claimed is None:
        stored = conn.execute(
            "SELECT request_hash, status_code, response FROM idempotency_keys WHERE key = %s", (full_key,)
        ).fetchone()
        if stored["request_hash"] != fingerprint:
            raise unprocessable(
                "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was already used with a different request body"
            )
        return Outcome(stored["status_code"], stored["response"], replayed=True)

    body = jsonable_encoder(operation())
    conn.execute(
        "UPDATE idempotency_keys SET status_code = %s, response = %s WHERE key = %s",
        (status_code, Jsonb(body), full_key),
    )
    return Outcome(status_code, body, replayed=False)


def purge_expired(conn: Connection, max_age_hours: int = 24) -> int:
    """Forgets keys older than the replay window, so the table doesn't grow forever."""
    return conn.execute(
        "DELETE FROM idempotency_keys WHERE created_at < now() - make_interval(hours => %s)", (max_age_hours,)
    ).rowcount
