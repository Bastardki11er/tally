"""Connection pool, transactions with retry, and a minimal SQL migration runner."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import TypeVar

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

T = TypeVar("T")

# Postgres asks the client to retry these: the transaction did nothing and can safely run again.
RETRYABLE = (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure)


class Database:
    def __init__(self, url: str, min_size: int = 2, max_size: int = 20) -> None:
        self.pool = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """One database transaction: commits on success, rolls back on any exception."""
        with self.pool.connection() as conn:
            yield conn

    def run(self, work: Callable[[psycopg.Connection], T], attempts: int = 3) -> T:
        """Runs ``work`` in a transaction, retrying if Postgres reports a deadlock or serialization failure."""
        for attempt in range(1, attempts + 1):
            try:
                with self.transaction() as conn:
                    return work(conn)
            except RETRYABLE:
                if attempt == attempts:
                    raise
                log.info("retrying transaction after a deadlock/serialization failure (attempt %d)", attempt)
                time.sleep(0.01 * 2**attempt)
        raise AssertionError("unreachable")


def _migration_files() -> list[tuple[str, str]]:
    source = Path(__file__).resolve().parents[2] / "migrations"
    if source.is_dir():  # running from a checkout
        files = sorted(source.glob("*.sql"))
        return [(f.stem, f.read_text()) for f in files]
    packaged = resources.files("tally") / "migrations"  # installed wheel
    files = sorted((f for f in packaged.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name)
    return [(f.name.removesuffix(".sql"), f.read_text()) for f in files]


MIGRATION_LOCK_ID = 7_301_984_221  # any constant; shared by every app instance


def migrate(url: str) -> list[str]:
    """Applies pending migrations in order, each in its own transaction.

    A Postgres advisory lock makes this safe when several instances start at once:
    one applies the migrations, the others wait and then find nothing to do.
    """
    applied: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, sql in _migration_files():
                if version in done:
                    continue
                with conn.transaction():
                    conn.execute(sql)
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                applied.append(version)
                log.info("applied migration %s", version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))
    return applied
