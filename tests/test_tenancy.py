"""API keys, tenant isolation and the audit log, through the HTTP API."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tally import auth
from tally.reconciliation import reconcile

from .conftest import Books


def key() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


@pytest.fixture
def other(db):
    """A second client with its own key and its own funded wallet."""
    record, api_key = db.run(lambda conn: auth.create_client(conn, "other"))
    books = Books(db, record["client_id"])
    cash = books.create_account("Other cash", "asset", "USD")
    wallet = books.create_account("Other wallet", "liability", "USD")
    books.transfer(cash["id"], wallet["id"], 1_000)
    return {"headers": {"Authorization": f"Bearer {api_key}"}, "books": books, "cash": cash, "wallet": wallet}


@pytest.fixture
def mine(books, wallet):
    """The default client's books, which the `client` fixture's key reaches."""
    alice, bob = wallet("alice", funded=1_000), wallet("bob")
    payment = books.transfer(alice["id"], bob["id"], 100)
    hold = books.create_hold(alice["id"], 50, datetime.now(UTC) + timedelta(days=1))
    return {"alice": alice, "bob": bob, "payment": payment, "hold": hold}


def balance(books, account) -> int:
    return books.get_account(account["id"])["balance"]


# --- authentication -----------------------------------------------------------------


@pytest.mark.parametrize("authorization", ["", "Basic dXNlcjpwYXNz", "Bearer tly_not-a-real-key"])
def test_a_request_without_a_valid_key_is_401(client, authorization):
    response = client.get("/v1/accounts", headers={"Authorization": authorization})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["code"] == "UNAUTHENTICATED"


def test_health_metrics_and_the_console_need_no_key(client):
    for url in ("/health", "/metrics", "/"):
        assert client.get(url, headers={"Authorization": ""}).status_code == 200, url


def test_keys_rotate_without_downtime(client, db, api_key):
    record, new_key = db.run(lambda conn: auth.create_key(conn, "default"))
    old = {"Authorization": f"Bearer {api_key}"}
    new = {"Authorization": f"Bearer {new_key}"}
    assert client.get("/v1/accounts", headers=old).status_code == 200
    assert client.get("/v1/accounts", headers=new).status_code == 200

    old_id = db.run(lambda conn: auth.find_key(conn, api_key)).key_id
    db.run(lambda conn: auth.revoke_key(conn, old_id))

    assert client.get("/v1/accounts", headers=old).status_code == 401
    assert client.get("/v1/accounts", headers=new).status_code == 200


def test_only_a_hash_of_each_key_is_stored(db, api_key):
    stored = db.run(lambda conn: conn.execute("SELECT key_hash, key_prefix FROM api_keys").fetchone())

    assert bytes(stored["key_hash"]) == auth.hash_key(api_key)
    assert api_key.startswith(stored["key_prefix"]) and len(stored["key_prefix"]) < len(api_key)


# --- isolation ----------------------------------------------------------------------


def test_another_clients_books_look_like_they_dont_exist(client, mine, other):
    as_other = other["headers"]
    for url in (
        f"/v1/accounts/{mine['alice']['id']}",
        f"/v1/accounts/{mine['alice']['id']}/entries",
        f"/v1/accounts/{mine['alice']['id']}/balance",
        f"/v1/accounts/{mine['alice']['id']}/holds",
        f"/v1/transactions/{mine['payment']['id']}",
        f"/v1/holds/{mine['hold']['id']}",
    ):
        response = client.get(url, headers=as_other)
        assert response.status_code == 404, url

    accounts = client.get("/v1/accounts", headers=as_other).json()["data"]
    feed = client.get("/v1/transactions", headers=as_other).json()["data"]
    assert {a["id"] for a in accounts} == {other["cash"]["id"], other["wallet"]["id"]}
    assert mine["payment"]["id"] not in {t["id"] for t in feed}


def test_another_clients_money_cannot_be_moved(client, books, mine, other):
    as_other = {**other["headers"], **key()}
    attempts = [
        (
            "/v1/transfers",
            {"debit_account_id": mine["alice"]["id"], "credit_account_id": other["wallet"]["id"], "amount": 1},
        ),
        (
            "/v1/transfers",
            {"debit_account_id": other["wallet"]["id"], "credit_account_id": mine["bob"]["id"], "amount": 1},
        ),
        (f"/v1/transactions/{mine['payment']['id']}/reversal", {}),
        (f"/v1/holds/{mine['hold']['id']}/capture", {"destination_account_id": other["wallet"]["id"]}),
        ("/v1/holds", {"account_id": mine["alice"]["id"], "amount": 1}),
    ]
    for url, body in attempts:
        response = client.post(url, json=body, headers={**as_other, **key()})
        assert response.status_code == 404, (url, response.text)
    response = client.post(f"/v1/holds/{mine['hold']['id']}/void", headers=as_other)
    assert response.status_code == 404

    assert (balance(books, mine["alice"]), balance(books, mine["bob"])) == (900, 100)
    assert books.get_hold(mine["hold"]["id"])["status"] == "active"
    assert balance(other["books"], other["wallet"]) == 1_000


def test_idempotency_keys_are_per_client(client, mine, other):
    shared = {"Idempotency-Key": "order-1"}
    mine_body = {"debit_account_id": mine["alice"]["id"], "credit_account_id": mine["bob"]["id"], "amount": 5}
    other_body = {"debit_account_id": other["wallet"]["id"], "credit_account_id": other["cash"]["id"], "amount": 5}

    first = client.post("/v1/transfers", json=mine_body, headers=shared)
    second = client.post("/v1/transfers", json=other_body, headers={**shared, **other["headers"]})

    # Not a replay of the first client's transfer, and not a "key reused" error either.
    assert (first.status_code, second.status_code) == (201, 201)
    assert second.headers["Idempotent-Replayed"] == "false"
    assert first.json()["id"] != second.json()["id"]


def test_reconciliation_over_http_covers_only_the_callers_books(client, db, mine, other):
    def damage(conn):
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute("UPDATE accounts SET balance = balance + 1 WHERE id = %s", (other["wallet"]["id"],))

    db.run(damage)

    mine_report = client.get("/v1/reconciliation").json()
    other_report = client.get("/v1/reconciliation", headers=other["headers"]).json()
    everything = db.run(reconcile)

    assert mine_report["ok"] is True and mine_report["accounts"] == 3
    assert other_report["balance_mismatches"] == [
        {"account_id": other["wallet"]["id"], "stored": 1_001, "computed": 1_000}
    ]
    assert everything["totals"]["balance_mismatches"] == 1 and everything["accounts"] == 5


# --- audit log ----------------------------------------------------------------------


def audit(db, *, through_api: bool = True) -> list[dict]:
    rows = db.run(lambda conn: conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall())
    return [r for r in rows if (r["api_key_id"] is not None) == through_api]


def test_every_change_is_audited_with_who_and_which_request(client, db, api_key, mine):
    body = {"debit_account_id": mine["alice"]["id"], "credit_account_id": mine["bob"]["id"], "amount": 5}
    headers = {**key(), "X-Request-ID": "req-123"}

    created = client.post("/v1/transfers", json=body, headers=headers).json()
    client.post("/v1/transfers", json=body, headers=headers)  # a replay: nothing happened, nothing recorded
    hold = client.post("/v1/holds", json={"account_id": mine["alice"]["id"], "amount": 7}, headers=key()).json()
    client.post(f"/v1/holds/{hold['id']}/void")
    account = client.post("/v1/accounts", json={"name": "Carol", "type": "liability", "currency": "USD"}).json()

    rows = audit(db)
    key_id = db.run(lambda conn: auth.find_key(conn, api_key)).key_id
    assert [(r["action"], r["resource_id"]) for r in rows] == [
        ("transaction.posted", created["id"]),
        ("hold.created", hold["id"]),
        ("hold.voided", hold["id"]),
        ("account.created", str(account["id"])),
    ]
    assert {(r["client_id"], r["api_key_id"]) for r in rows} == {(1, key_id)}
    assert rows[0]["request_id"] == "req-123"


def test_a_failed_change_leaves_no_audit_record(client, db, mine):
    body = {"debit_account_id": mine["bob"]["id"], "credit_account_id": mine["alice"]["id"], "amount": 10_000}

    response = client.post("/v1/transfers", json=body, headers=key())

    assert response.json()["code"] == "INSUFFICIENT_FUNDS"
    assert audit(db) == []


def test_changes_made_around_the_api_are_audited_too_with_no_key(db, books, mine):
    # The fixtures wrote through the ledger directly, as a script would; expiry is the system's doing.
    db.run(lambda conn: conn.execute("UPDATE holds SET expires_at = now() - interval '1 second'"))
    books.expire_holds()

    actions = [r["action"] for r in audit(db, through_api=False)]
    assert actions.count("account.created") == 3  # cash, alice, bob
    assert actions.count("transaction.posted") == 2  # funding, payment
    assert actions[-2:] == ["hold.created", "hold.expired"]
