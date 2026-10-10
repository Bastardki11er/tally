"""Tests run against a real PostgreSQL: the invariants under test live in the database.

Set TALLY_TEST_DATABASE_URL to use an existing server; otherwise a disposable
postgres:17 container is started with Testcontainers.
"""

import os

import psycopg
import pytest

from tally.db import Database, migrate
from tally.ledger import Ledger

TABLES = "accounts, transactions, entries, holds, idempotency_keys"


@pytest.fixture(scope="session")
def database_url():
    url = os.environ.get("TALLY_TEST_DATABASE_URL")
    if url:
        yield url
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:17-alpine", driver=None) as postgres:
        yield postgres.get_connection_url()


@pytest.fixture(scope="session")
def migrated(database_url):
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    migrate(database_url)
    return database_url


@pytest.fixture(scope="session")
def db(migrated):
    database = Database(migrated, min_size=2, max_size=60)
    yield database
    database.close()


def reset(db: Database) -> None:
    """Wipes every table between tests.

    The ledger refuses TRUNCATE (migration 003). Replica mode skips triggers for this
    one transaction; only a superuser can switch it on, which the test database user
    is, and the application's database user should never be.
    """

    def wipe(conn):
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")

    db.run(wipe)


@pytest.fixture(autouse=True)
def clean_database(db):
    reset(db)


class Books:
    """Calls Ledger methods, each in its own committed transaction: ``books.transfer(1, 2, 500)``."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.ledger = Ledger()

    def __getattr__(self, name):
        method = getattr(self.ledger, name)
        return lambda *args, **kwargs: self.db.run(lambda conn: method(conn, *args, **kwargs))


@pytest.fixture
def books(db):
    return Books(db)


@pytest.fixture
def cash(books):
    """The platform's bank account. Deposits debit it (asset up) and credit a wallet (liability up)."""
    return books.create_account("Cash", "asset", "USD")


@pytest.fixture
def wallet(books, cash):
    """Factory for customer wallets, optionally pre-funded from cash."""

    def make(name: str = "wallet", funded: int = 0, currency: str = "USD") -> dict:
        account = books.create_account(name, "liability", currency)
        if funded:
            books.transfer(cash["id"], account["id"], funded, f"fund {name}")
        return account

    return make


@pytest.fixture
def client(migrated):
    from fastapi.testclient import TestClient

    from tally.api import create_app
    from tally.config import Settings

    app = create_app(Settings(database_url=migrated, pool_size=10), run_migrations=False, maintenance=False)
    with TestClient(app) as test_client:
        yield test_client
