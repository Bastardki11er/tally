"""Tests run against a real PostgreSQL: the invariants under test live in the database.

Set TALLY_TEST_DATABASE_URL to use an existing server; otherwise a disposable
postgres:17 container is started with Testcontainers.
"""

import os

import psycopg
import pytest
from hypothesis import settings

from tally import auth
from tally.db import Database, migrate
from tally.ledger import Ledger

TABLES = "api_clients, api_keys, audit_log, accounts, transactions, entries, holds, idempotency_keys"
DEFAULT_CLIENT = 1  # created by migration 006, and again by reset()

# Pull requests run 40 random operation sequences; `pytest --hypothesis-profile=thorough`
# runs 500. print_blob makes a failure in CI print a @reproduce_failure line that
# replays the exact sequence locally.
settings.register_profile("ci", max_examples=40, print_blob=True)
settings.register_profile("thorough", max_examples=500, print_blob=True)
settings.load_profile("ci")


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
        conn.execute("INSERT INTO api_clients (name) VALUES ('default')")  # id 1 again

    db.run(wipe)


@pytest.fixture(autouse=True)
def clean_database(db):
    reset(db)


class Books:
    """Calls one client's Ledger methods, each in its own committed transaction: ``books.transfer(1, 2, 500)``."""

    def __init__(self, db: Database, client_id: int = DEFAULT_CLIENT) -> None:
        self.db = db
        self.ledger = Ledger(client_id)

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
def api_key(db):
    """A key for the default client, whose books `books` and `wallet` write to."""
    return db.run(lambda conn: auth.create_key(conn, "default"))[1]


@pytest.fixture
def client(migrated, api_key):
    from fastapi.testclient import TestClient

    from tally.api import create_app
    from tally.config import Settings

    app = create_app(Settings(database_url=migrated, pool_size=10), run_migrations=False, maintenance=False)
    with TestClient(app, headers={"Authorization": f"Bearer {api_key}"}) as test_client:
        yield test_client
