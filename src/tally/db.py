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

NO_TRANSACTION = "-- migration: no-transaction"


def _statements(sql: str) -> list[str]:
    """Splits a simple DDL file on semicolons (no function bodies allowed in such files)."""
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [statement.strip() for statement in code.split(";") if statement.strip()]


def _acquire_migration_lock(conn: psycopg.Connection, timeout_seconds: float = 600) -> None:
    """Waits for the migration lock by polling rather than blocking in pg_advisory_lock().

    A session blocked inside pg_advisory_lock() is mid-statement, so it counts as an
    open transaction, and CREATE INDEX CONCURRENTLY waits for every open transaction
    to finish. The instance building the index would wait for the waiters while they
    wait for its lock: a deadlock. pg_try_advisory_lock() returns immediately, so
    between attempts there is nothing for the index build to wait on.
    """
    deadline = time.monotonic() + timeout_seconds
    while not conn.execute("SELECT pg_try_advisory_lock(%s)", (MIGRATION_LOCK_ID,)).fetchone()[0]:
        if time.monotonic() > deadline:
            raise TimeoutError("timed out waiting for another instance to finish migrating")
        time.sleep(0.2)


def migrate(url: str, until: str | None = None) -> list[str]:
    """Applies pending migrations in order, stopping after version ``until`` if given.

    Each migration runs in its own transaction, so a failure leaves no half-applied
    schema. The exception is a file starting with ``-- migration: no-transaction``:
    its statements run one by one in autocommit mode, because ``CREATE INDEX
    CONCURRENTLY`` (building an index without blocking writes) refuses to run inside
    a transaction. Such files must be safe to re-run after a crash.

    A Postgres advisory lock makes this safe when several instances start at once:
    one applies the migrations, the others wait and then find nothing to do.
    """
    applied: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        _acquire_migration_lock(conn)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, sql in _migration_files():
                if version not in done:
                    if sql.lstrip().startswith(NO_TRANSACTION):
                        for statement in _statements(sql):
                            conn.execute(statement)
                        conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                    else:
                        with conn.transaction():
                            conn.execute(sql)
                            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                    applied.append(version)
                    log.info("applied migration %s", version)
                if version == until:
                    break
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))
    return applied
