import uuid

import pytest


def key() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


@pytest.fixture
def accounts(client):
    def create(name, type_="liability", **extra):
        response = client.post("/v1/accounts", json={"name": name, "type": type_, "currency": "USD", **extra})
        assert response.status_code == 201, response.text
        return response.json()

    cash, alice, bob = create("Cash", "asset"), create("Alice"), create("Bob")
    deposit = client.post(
        "/v1/transfers",
        headers=key(),
        json={"debit_account_id": cash["id"], "credit_account_id": alice["id"], "amount": 10_000},
    )
    assert deposit.status_code == 201, deposit.text
    return {"cash": cash, "alice": alice, "bob": bob}


def test_transfer_and_read_back(client, accounts):
    alice, bob = accounts["alice"], accounts["bob"]

    response = client.post(
        "/v1/transfers",
        headers=key(),
        json={"debit_account_id": alice["id"], "credit_account_id": bob["id"], "amount": 2_500, "description": "rent"},
    )

    assert response.status_code == 201
    assert response.headers["Idempotent-Replayed"] == "false"
    transaction = response.json()
    assert client.get(f"/v1/transactions/{transaction['id']}").json()["description"] == "rent"
    assert client.get(f"/v1/accounts/{alice['id']}").json()["balance"] == 7_500
    assert client.get(f"/v1/accounts/{bob['id']}").json()["available"] == 2_500


def test_replaying_a_request_returns_the_original_response(client, accounts):
    body = {"debit_account_id": accounts["alice"]["id"], "credit_account_id": accounts["bob"]["id"], "amount": 100}
    headers = key()

    first = client.post("/v1/transfers", headers=headers, json=body)
    second = client.post("/v1/transfers", headers=headers, json=body)

    assert second.status_code == 201
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()
    assert client.get(f"/v1/accounts/{accounts['bob']['id']}").json()["balance"] == 100


def test_reusing_a_key_for_a_different_request_is_an_error(client, accounts):
    headers = key()
    base = {"debit_account_id": accounts["alice"]["id"], "credit_account_id": accounts["bob"]["id"]}
    client.post("/v1/transfers", headers=headers, json={**base, "amount": 100})

    response = client.post("/v1/transfers", headers=headers, json={**base, "amount": 999})

    assert response.status_code == 422
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_a_failed_request_can_be_retried_with_the_same_key(client, accounts):
    headers = key()
    body = {"debit_account_id": accounts["bob"]["id"], "credit_account_id": accounts["alice"]["id"], "amount": 500}

    refused = client.post("/v1/transfers", headers=headers, json=body)
    client.post(
        "/v1/transfers",
        headers=key(),
        json={"debit_account_id": accounts["cash"]["id"], "credit_account_id": accounts["bob"]["id"], "amount": 500},
    )
    retried = client.post("/v1/transfers", headers=headers, json=body)

    assert refused.status_code == 409 and refused.json()["code"] == "INSUFFICIENT_FUNDS"
    assert retried.status_code == 201 and retried.headers["Idempotent-Replayed"] == "false"


def test_errors_are_problem_details(client, accounts):
    missing = client.get("/v1/accounts/987654")
    no_key = client.post("/v1/transfers", json={"debit_account_id": 1, "credit_account_id": 2, "amount": 1})
    bad_currency = client.post("/v1/accounts", json={"name": "x", "type": "asset", "currency": "usd"})
    zero = client.post(
        "/v1/transfers", headers=key(), json={"debit_account_id": 1, "credit_account_id": 2, "amount": 0}
    )

    assert missing.status_code == 404
    assert missing.headers["content-type"] == "application/problem+json"
    assert missing.json()["code"] == "ACCOUNT_NOT_FOUND"
    for response in (no_key, bad_currency, zero):
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"


def test_general_transactions_reversals_and_statements(client, accounts):
    alice, bob, cash = accounts["alice"], accounts["bob"], accounts["cash"]
    fees = client.post("/v1/accounts", json={"name": "Fees", "type": "revenue", "currency": "USD"}).json()

    payment = client.post(
        "/v1/transactions",
        headers=key(),
        json={
            "description": "payment with fee",
            "entries": [
                {"account_id": alice["id"], "direction": "debit", "amount": 1_030},
                {"account_id": bob["id"], "direction": "credit", "amount": 1_000},
                {"account_id": fees["id"], "direction": "credit", "amount": 30},
            ],
        },
    ).json()
    reversal = client.post(f"/v1/transactions/{payment['id']}/reversal", headers=key(), json={"description": "undo"})

    assert reversal.status_code == 201
    statement = client.get(f"/v1/accounts/{alice['id']}/entries", params={"limit": 2}).json()
    assert [line["description"] for line in statement["data"]] == ["undo", "payment with fee"]
    assert statement["next_cursor"] == statement["data"][-1]["id"]
    rest = client.get(f"/v1/accounts/{alice['id']}/entries", params={"limit": 2, "cursor": statement["next_cursor"]})
    assert [line["amount"] for line in rest.json()["data"]] == [10_000]
    assert client.get(f"/v1/accounts/{cash['id']}/balance").json()["balance"] == 10_000


def test_hold_lifecycle_over_http(client, accounts):
    alice, bob = accounts["alice"], accounts["bob"]

    hold = client.post("/v1/holds", headers=key(), json={"account_id": alice["id"], "amount": 4_000}).json()
    assert client.get(f"/v1/accounts/{alice['id']}").json()["available"] == 6_000
    captured = client.post(
        f"/v1/holds/{hold['id']}/capture", headers=key(), json={"destination_account_id": bob["id"], "amount": 3_500}
    )

    assert captured.status_code == 200
    assert captured.json()["hold"]["status"] == "captured"
    assert client.get(f"/v1/accounts/{alice['id']}").json()["available"] == 6_500
    assert client.get(f"/v1/accounts/{alice['id']}/holds").json()["data"][0]["status"] == "captured"


def test_reconciliation_and_health(client, accounts):
    report = client.get("/v1/reconciliation").json()

    assert report["ok"] is True
    assert report["trial_balance"] == [{"currency": "USD", "debits": 10_000, "credits": 10_000}]
    assert client.get("/health").json() == {"status": "ok"}


def test_recent_transactions_feed(client, accounts):
    alice, bob = accounts["alice"], accounts["bob"]
    paid = client.post(
        "/v1/transfers",
        headers=key(),
        json={"debit_account_id": alice["id"], "credit_account_id": bob["id"], "amount": 5},
    ).json()
    client.post(f"/v1/transactions/{paid['id']}/reversal", headers=key(), json={})

    feed = client.get("/v1/transactions", params={"limit": 3}).json()["data"]

    assert [len(t["entries"]) for t in feed] == [2, 2, 2]
    assert feed[0]["reverses_id"] == paid["id"]
    assert feed[1]["id"] == paid["id"] and feed[1]["reversed_by"] == feed[0]["id"]
