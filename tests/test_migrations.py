"""The migration runner and the indexes it creates."""

import uuid

import psycopg
import pytest

from tally.db import _statements, migrate

INDEXES = ("entries_by_account_time", "transactions_by_posted_at")


@pytest.fixture
def scratch_database(migrated):
    """An empty database next to the test one, for exercising the runner from scratch."""
    name = f"tally_migrations_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(migrated, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = psycopg.conninfo.make_conninfo(migrated, dbname=name)
    yield url
    with psycopg.connect(migrated, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def applied(url: str) -> list[str]:
    with psycopg.connect(url) as conn:
        return [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def test_migrations_apply_in_order_stop_where_asked_and_are_idempotent(scratch_database):
    assert migrate(scratch_database, until="001_ledger") == ["001_ledger"]
    assert migrate(scratch_database) == ["002_query_indexes"]
    assert migrate(scratch_database) == []
    assert applied(scratch_database) == ["001_ledger", "002_query_indexes"]


def test_concurrently_built_indexes_exist_and_are_valid(migrated):
    # A failed CREATE INDEX CONCURRENTLY leaves an index that exists but is INVALID.
    with psycopg.connect(migrated) as conn:
        rows = conn.execute(
            "SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = ANY(%s)",
            (list(INDEXES),),
        ).fetchall()
    assert dict(rows) == {name: True for name in INDEXES}


@pytest.mark.parametrize(
    ("query", "index"),
    [
        (
            "SELECT balance_after FROM entries WHERE account_id = 1 AND posted_at <= now() "
            "ORDER BY posted_at DESC, id DESC LIMIT 1",
            "entries_by_account_time",
        ),
        ("SELECT id FROM transactions ORDER BY posted_at DESC, id DESC LIMIT 20", "transactions_by_posted_at"),
    ],
)
def test_hot_queries_can_use_their_index(migrated, query, index):
    # On a near-empty table a sequential scan is cheaper, so take that option away
    # and check that the index fits the query's shape (filter and sort order).
    with psycopg.connect(migrated) as conn:
        conn.execute("SET enable_seqscan = off")
        plan = "\n".join(row[0] for row in conn.execute(f"EXPLAIN {query}"))
    assert index in plan
    assert "Sort" not in plan


def test_no_transaction_files_are_split_into_statements():
    sql = """-- migration: no-transaction
    -- a comment; with a semicolon
    DROP INDEX CONCURRENTLY IF EXISTS a;
    CREATE INDEX CONCURRENTLY a ON t (x);
    """
    assert _statements(sql) == ["DROP INDEX CONCURRENTLY IF EXISTS a", "CREATE INDEX CONCURRENTLY a ON t (x)"]


def test_instances_starting_at_the_same_time_migrate_once_without_deadlocking(scratch_database):
    # Several app workers boot together and each runs migrate(). The ones waiting for
    # the migration lock must not block CREATE INDEX CONCURRENTLY, which waits for every
    # open transaction to finish, while the index builder holds the lock they want.
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    gate = Barrier(4)

    def boot(_):
        gate.wait()
        return migrate(scratch_database)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(boot, range(4)))

    assert sorted(len(r) for r in results) == [0, 0, 0, 2]  # one instance did all the work
    assert applied(scratch_database) == ["001_ledger", "002_query_indexes"]
