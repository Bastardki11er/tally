"""Transactions, retries and connection limits in tally.db."""

import time

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from tally.db import Database
from tally.errors import LedgerError


@pytest.fixture
def one_connection(migrated):
    """A pool with a single connection and short timeouts, so the tests below are quick."""
    database = Database(migrated, min_size=1, max_size=1, lock_timeout_ms=300, check_idle_after_seconds=0.2)
    yield database
    database.close()


def account_names(db) -> list[str]:
    return db.run(lambda conn: [row["name"] for row in conn.execute("SELECT name FROM accounts ORDER BY id")])


def insert_account(conn, name: str) -> None:
    conn.execute("INSERT INTO accounts (client_id, name, type, currency) VALUES (1, %s, 'asset', 'USD')", (name,))


def test_work_commits_on_success_and_rolls_back_on_any_error(db):
    db.run(lambda conn: insert_account(conn, "kept"))

    def fail(conn):
        insert_account(conn, "discarded")
        raise LedgerError(409, "SOMETHING", "boom")

    with pytest.raises(LedgerError):
        db.run(fail)
    assert account_names(db) == ["kept"]


def test_deadlocks_are_retried_each_time_in_a_fresh_transaction(db):
    attempts = []

    def work(conn):
        insert_account(conn, f"attempt {len(attempts)}")
        attempts.append(1)
        if len(attempts) < 3:
            raise psycopg.errors.DeadlockDetected("simulated")
        return "done"

    assert db.run(work) == "done"
    assert len(attempts) == 3
    assert account_names(db) == ["attempt 2"]  # the first two attempts were rolled back


def test_retrying_stops_after_the_last_attempt(db):
    attempts = []

    def work(conn):
        attempts.append(1)
        raise psycopg.errors.SerializationFailure("simulated")

    with pytest.raises(psycopg.errors.SerializationFailure):
        db.run(work, attempts=2)
    assert len(attempts) == 2


def test_business_errors_are_not_retried(db):
    attempts = []

    def work(conn):
        attempts.append(1)
        raise LedgerError(409, "INSUFFICIENT_FUNDS", "no")

    with pytest.raises(LedgerError):
        db.run(work)
    assert len(attempts) == 1


def test_every_pooled_connection_has_bounded_waits(one_connection):
    settings = one_connection.run(
        lambda conn: conn.execute(
            "SELECT current_setting('lock_timeout') AS lock, current_setting('statement_timeout') AS statement, "
            "current_setting('idle_in_transaction_session_timeout') AS idle"
        ).fetchone()
    )

    assert settings == {"lock": "300ms", "statement": "30s", "idle": "1min"}


def test_a_stuck_row_lock_fails_fast_instead_of_hanging(one_connection, migrated, books):
    account = books.create_account("contested", "asset", "USD")
    with psycopg.connect(migrated) as someone_else:
        # e.g. an engineer's psql session that took a row lock and went to lunch
        someone_else.execute("SELECT 1 FROM accounts WHERE id = %s FOR UPDATE", (account["id"],))
        started = time.monotonic()

        with pytest.raises(psycopg.errors.LockNotAvailable):
            one_connection.run(
                lambda conn: conn.execute("SELECT 1 FROM accounts WHERE id = %s FOR UPDATE", (account["id"],))
            )

        assert time.monotonic() - started < 2


def test_an_idle_connection_dropped_by_the_server_is_replaced_without_failing_a_request(one_connection, migrated):
    pid = one_connection.run(lambda conn: conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
    with psycopg.connect(migrated, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))  # what a restart or an idle timeout does
    time.sleep(0.3)  # longer than check_idle_after_seconds: the connection now counts as idle

    assert one_connection.run(lambda conn: conn.execute("SELECT 1 AS one").fetchone()["one"]) == 1


def test_only_connections_that_sat_idle_are_pinged(one_connection, monkeypatch):
    one_connection.run(lambda conn: conn.execute("SELECT 1"))  # a brand-new connection is always pinged once
    pings = []
    real_check = ConnectionPool.check_connection
    monkeypatch.setattr(ConnectionPool, "check_connection", lambda conn: (pings.append(1), real_check(conn)))

    for _ in range(3):  # back to back, as under load
        one_connection.run(lambda conn: conn.execute("SELECT 1"))
    busy_pings = len(pings)
    time.sleep(0.3)
    one_connection.run(lambda conn: conn.execute("SELECT 1"))

    assert busy_pings == 0
    assert len(pings) == 1
