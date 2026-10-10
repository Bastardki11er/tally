"""The HTTP layer's contract: problem documents for every failure, safe pagination, request limits."""

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import psycopg_pool
import pytest
from fastapi.testclient import TestClient

from tally.api import MAX_BODY_BYTES, create_app
from tally.config import Settings
from tally.ledger import Ledger
from tally.schemas import MAX_METADATA_BYTES


def key() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


@pytest.fixture
def make_client(migrated):
    """Clients with custom settings, or that turn server exceptions into responses like production does."""
    clients = []

    def make(raise_server_exceptions: bool = True, **settings) -> TestClient:
        app = create_app(
            Settings(database_url=migrated, pool_size=5, **settings), run_migrations=False, maintenance=False
        )
        client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
        clients.append(client.__enter__())
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def assert_problem(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert (body["status"], body["code"]) == (status, code)
    return body


# --- transient failures: 503 with Retry-After ---------------------------------------


def test_a_locked_account_is_a_retryable_503_not_a_500(make_client, wallet, migrated):
    client = make_client(lock_timeout_ms=200)
    alice, bob = wallet("alice", funded=100), wallet("bob")

    with psycopg.connect(migrated) as blocker:
        blocker.execute("SELECT 1 FROM accounts WHERE id = %s FOR UPDATE", (alice["id"],))
        response = client.post(
            "/v1/transfers",
            headers=key(),
            json={"debit_account_id": alice["id"], "credit_account_id": bob["id"], "amount": 10},
        )

    assert_problem(response, 503, "LOCK_TIMEOUT")
    assert response.headers["Retry-After"] == "1"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (psycopg.errors.QueryCanceled("canceling statement due to statement timeout"), "QUERY_TIMEOUT"),
        (psycopg_pool.PoolTimeout("couldn't get a connection after 10.00 sec"), "SERVER_BUSY"),
        (psycopg.OperationalError("connection refused"), "DATABASE_UNAVAILABLE"),
    ],
)
def test_other_transient_database_failures_are_503s(make_client, wallet, monkeypatch, error, code):
    client = make_client()
    alice = wallet("alice")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(Ledger, "get_account", fail)
    response = client.get(f"/v1/accounts/{alice['id']}")

    body = assert_problem(response, 503, code)
    assert "Retry-After" in response.headers
    assert str(error) not in body["detail"]


# --- failures that must not leak internals ------------------------------------------


def test_a_constraint_violation_hides_the_constraint_and_the_row(client, monkeypatch):
    # Switch the API's own size check off, so the database's CHECK is what catches it:
    # the backstop must still answer cleanly, without naming its constraint.
    monkeypatch.setattr("tally.schemas.MAX_METADATA_BYTES", 10**9)
    response = client.post(
        "/v1/accounts", json={"name": "big", "type": "liability", "currency": "USD", "metadata": {"note": "x" * 5000}}
    )

    body = assert_problem(response, 409, "CONSTRAINT_VIOLATION")
    assert "metadata" not in body["detail"] and "accounts" not in body["detail"]


def test_an_unexpected_error_is_a_500_problem_without_details(make_client, wallet, monkeypatch):
    client = make_client(raise_server_exceptions=False)
    alice = wallet("alice")

    def fail(*args, **kwargs):
        raise RuntimeError("/srv/tally/secret.py exploded")

    monkeypatch.setattr(Ledger, "get_account", fail)
    body = assert_problem(client.get(f"/v1/accounts/{alice['id']}"), 500, "INTERNAL_ERROR")

    assert "secret" not in body["detail"]


# --- request limits -----------------------------------------------------------------


def test_an_oversized_body_is_refused_before_it_is_read(client):
    response = client.post(
        "/v1/accounts",
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={"content-type": "application/json"},
    )

    assert_problem(response, 413, "CONTENT_TOO_LARGE")


def test_an_oversized_chunked_body_without_a_length_is_refused_too(client):
    def chunks():
        for _ in range(MAX_BODY_BYTES // 1024 + 2):
            yield b"x" * 1024

    response = client.post("/v1/accounts", content=chunks(), headers={"content-type": "application/json"})

    assert_problem(response, 413, "CONTENT_TOO_LARGE")


def test_oversized_metadata_is_a_validation_error_not_a_database_error(client):
    response = client.post(
        "/v1/accounts",
        json={"name": "big", "type": "liability", "currency": "USD", "metadata": {"note": "é" * MAX_METADATA_BYTES}},
    )

    body = assert_problem(response, 422, "VALIDATION_ERROR")
    assert body["errors"][0]["loc"] == ["body", "metadata"]


def test_a_time_without_a_zone_is_rejected(client, wallet):
    alice = wallet("alice", funded=100)

    response = client.get(f"/v1/accounts/{alice['id']}/balance", params={"as_of": "2026-01-01T00:00:00"})

    assert_problem(response, 422, "VALIDATION_ERROR")
    zoned = client.get(f"/v1/accounts/{alice['id']}/balance", params={"as_of": "2999-01-01T00:00:00Z"})
    assert zoned.json()["balance"] == 100


# --- pagination ---------------------------------------------------------------------


def walk(client, url: str, limit: int) -> list[list[dict]]:
    pages, cursor = [], None
    while True:
        params = {"limit": limit} | ({"cursor": cursor} if cursor else {})
        response = client.get(url, params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append(body["data"])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages


def test_holds_page_past_the_first_page_even_when_they_share_a_timestamp(client, db, wallet):
    alice = wallet("alice", funded=1_000)
    expires = datetime.now(UTC) + timedelta(days=1)
    # One transaction, so all 120 holds get the same created_at; only the id breaks ties.
    db.run(lambda conn: [Ledger().create_hold(conn, alice["id"], 1, expires) for _ in range(120)])

    pages = walk(client, f"/v1/accounts/{alice['id']}/holds", limit=50)

    assert [len(page) for page in pages] == [50, 50, 20]
    assert len({hold["id"] for page in pages for hold in page}) == 120


def test_the_transaction_feed_pages_through_everything_once(client, db, wallet, cash):
    alice = wallet("alice", funded=1_000)
    db.run(lambda conn: [Ledger().transfer(conn, alice["id"], cash["id"], 1) for _ in range(25)])

    pages = walk(client, "/v1/transactions", limit=10)

    ids = [t["id"] for page in pages for t in page]
    assert len(ids) == len(set(ids)) == 26  # the funding transfer plus 25


def test_accounts_page_with_an_opaque_cursor(client, books):
    for i in range(5):
        books.create_account(f"a{i}", "liability", "USD")

    pages = walk(client, "/v1/accounts", limit=2)

    assert [len(page) for page in pages] == [2, 2, 1]


@pytest.mark.parametrize("cursor", ["not-base64!", "WzEsMiwzXQ", "eyJhIjogMX0"])
def test_a_cursor_the_api_never_issued_is_rejected(client, wallet, cursor):
    alice = wallet("alice")

    response = client.get(f"/v1/accounts/{alice['id']}/holds", params={"cursor": cursor})

    assert_problem(response, 422, "INVALID_CURSOR")


# --- idempotency on the endpoints where it's optional, and Location headers ----------


def test_creating_an_account_with_a_key_is_safe_to_retry(client):
    body = {"name": "Alice", "type": "liability", "currency": "USD"}
    headers = key()

    first = client.post("/v1/accounts", json=body, headers=headers)
    retry = client.post("/v1/accounts", json=body, headers=headers)
    unkeyed = client.post("/v1/accounts", json=body)

    assert first.json()["id"] == retry.json()["id"] != unkeyed.json()["id"]
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert first.headers["Location"] == f"/v1/accounts/{first.json()['id']}"


def test_a_retried_void_with_a_key_replays_instead_of_failing(client, wallet):
    alice = wallet("alice", funded=100)
    hold = client.post("/v1/holds", headers=key(), json={"account_id": alice["id"], "amount": 50}).json()
    headers = key()

    first = client.post(f"/v1/holds/{hold['id']}/void", headers=headers)
    retry = client.post(f"/v1/holds/{hold['id']}/void", headers=headers)
    unkeyed = client.post(f"/v1/holds/{hold['id']}/void")

    assert (first.status_code, retry.status_code) == (200, 200)
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert_problem(unkeyed, 409, "HOLD_NOT_ACTIVE")


def test_created_resources_say_where_they_live(client, wallet, cash):
    alice = wallet("alice", funded=100)

    transfer = client.post(
        "/v1/transfers",
        headers=key(),
        json={"debit_account_id": alice["id"], "credit_account_id": cash["id"], "amount": 1},
    )
    hold = client.post("/v1/holds", headers=key(), json={"account_id": alice["id"], "amount": 1})

    assert client.get(transfer.headers["Location"]).json()["id"] == transfer.json()["id"]
    assert client.get(hold.headers["Location"]).json()["id"] == hold.json()["id"]


def test_the_openapi_document_describes_the_error_shape(client):
    spec = client.get("/openapi.json").json()

    assert "Problem" in spec["components"]["schemas"]
    assert "default" in spec["paths"]["/v1/transfers"]["post"]["responses"]


def test_every_framework_status_has_a_fixed_code():
    # Codes are part of the API contract. Deriving them from HTTPStatus names made 413
    # read CONTENT_TOO_LARGE on Python 3.13 but REQUEST_ENTITY_TOO_LARGE on 3.11.
    from tally.api import FRAMEWORK_CODES

    assert FRAMEWORK_CODES[413] == "CONTENT_TOO_LARGE"
    assert all(code.isupper() and " " not in code for code in FRAMEWORK_CODES.values())


def test_framework_errors_are_problem_documents_too(client):
    assert_problem(client.get("/v1/no-such-thing"), 404, "NOT_FOUND")
    wrong_method = client.delete("/v1/accounts")
    assert_problem(wrong_method, 405, "METHOD_NOT_ALLOWED")
    assert wrong_method.headers["Allow"] == "POST"
