from datetime import UTC, datetime, timedelta

import pytest

from tally.errors import LedgerError

LATER = timedelta(days=1)


def account(books, a):
    return books.get_account(a["id"])


def test_a_hold_reserves_funds_without_moving_them(books, wallet):
    alice = wallet("alice", funded=1_000)

    books.create_hold(alice["id"], 400, datetime.now(UTC) + LATER)

    a = account(books, alice)
    assert (a["balance"], a["held"], a["available"]) == (1_000, 400, 600)


def test_held_funds_cannot_be_spent_twice(books, wallet):
    alice, bob = wallet("alice", funded=1_000), wallet("bob")
    books.create_hold(alice["id"], 800, datetime.now(UTC) + LATER)

    with pytest.raises(LedgerError) as transfer:
        books.transfer(alice["id"], bob["id"], 300)
    with pytest.raises(LedgerError) as another_hold:
        books.create_hold(alice["id"], 300, datetime.now(UTC) + LATER)

    assert transfer.value.code == another_hold.value.code == "INSUFFICIENT_FUNDS"


def test_a_partial_capture_spends_that_much_and_releases_the_rest(books, wallet):
    alice, store = wallet("alice", funded=1_000), wallet("store")
    hold = books.create_hold(alice["id"], 400, datetime.now(UTC) + LATER, "gas pump authorization")

    result = books.capture_hold(hold["id"], store["id"], 342)

    a = account(books, alice)
    assert (a["balance"], a["held"], a["available"]) == (658, 0, 658)
    assert account(books, store)["balance"] == 342
    assert result["hold"]["status"] == "captured"
    assert result["hold"]["captured_transaction_id"] == result["transaction"]["id"]
    assert result["transaction"]["metadata"] == {"hold_id": str(hold["id"])}


def test_capturing_on_a_debit_normal_account_spends_with_a_credit(books, cash):
    # A company card account is an asset: spending from it is a credit, not a debit.
    owner = books.create_account("Owner equity", "equity", "USD")
    books.transfer(cash["id"], owner["id"], 1_000)
    supplies = books.create_account("Office supplies", "expense", "USD")
    hold = books.create_hold(cash["id"], 250, datetime.now(UTC) + LATER)

    result = books.capture_hold(hold["id"], supplies["id"])

    assert [(e["account_id"], e["direction"]) for e in result["transaction"]["entries"]] == [
        (cash["id"], "credit"),
        (supplies["id"], "debit"),
    ]
    assert account(books, cash)["balance"] == 750
    assert account(books, supplies)["balance"] == 250


def test_void_releases_the_hold(books, wallet):
    alice = wallet("alice", funded=1_000)
    hold = books.create_hold(alice["id"], 400, datetime.now(UTC) + LATER)

    voided = books.void_hold(hold["id"])

    assert voided["status"] == "voided"
    assert account(books, alice)["available"] == 1_000


def test_holds_can_only_be_settled_once_and_not_beyond_their_amount(books, wallet):
    alice, store = wallet("alice", funded=1_000), wallet("store")
    hold = books.create_hold(alice["id"], 400, datetime.now(UTC) + LATER)

    with pytest.raises(LedgerError) as too_much:
        books.capture_hold(hold["id"], store["id"], 401)
    books.capture_hold(hold["id"], store["id"], 400)
    with pytest.raises(LedgerError) as again:
        books.capture_hold(hold["id"], store["id"])
    with pytest.raises(LedgerError) as void_after:
        books.void_hold(hold["id"])

    assert too_much.value.code == "CAPTURE_EXCEEDS_HOLD"
    assert again.value.code == void_after.value.code == "HOLD_NOT_ACTIVE"


def test_expired_holds_cannot_be_captured_and_are_released_by_the_expiry_job(books, db, wallet):
    alice, store = wallet("alice", funded=1_000), wallet("store")
    stale = books.create_hold(alice["id"], 300, datetime.now(UTC) + LATER)
    fresh = books.create_hold(alice["id"], 200, datetime.now(UTC) + LATER)
    # Let one run out, as if a day had passed.
    db.run(
        lambda conn: conn.execute(
            "UPDATE holds SET expires_at = now() - interval '1 second' WHERE id = %s", (stale["id"],)
        )
    )

    with pytest.raises(LedgerError) as error:
        books.capture_hold(stale["id"], store["id"])
    released = books.expire_holds()

    assert error.value.code == "HOLD_EXPIRED"
    assert released == 1
    assert books.get_hold(stale["id"])["status"] == "expired"
    assert books.get_hold(fresh["id"])["status"] == "active"
    assert account(books, alice)["held"] == 200


def test_capturing_a_hold_on_the_last_of_the_funds_works(books, wallet):
    # Available is 0 while the hold exists. The capture must release the reservation
    # before the debit lands, or the database's overdraft check would refuse it.
    alice, store = wallet("alice", funded=400), wallet("store")
    hold = books.create_hold(alice["id"], 400, datetime.now(UTC) + LATER)

    books.capture_hold(hold["id"], store["id"])

    a = account(books, alice)
    assert (a["balance"], a["held"], a["available"]) == (0, 0, 0)


@pytest.mark.parametrize(
    "expires_at",
    [datetime.now() + LATER, datetime.now(UTC) - timedelta(seconds=1)],
    ids=["naive", "in-the-past"],
)
def test_a_hold_needs_an_aware_future_expiry(books, wallet, expires_at):
    alice = wallet("alice", funded=1_000)

    with pytest.raises(LedgerError) as error:
        books.create_hold(alice["id"], 100, expires_at)

    assert error.value.code == "INVALID_EXPIRY"


def test_capture_amounts_and_currencies_are_checked(books, wallet):
    alice, store = wallet("alice", funded=1_000), wallet("store")
    euros = books.create_account("EUR merchant", "liability", "EUR")
    hold = books.create_hold(alice["id"], 400, datetime.now(UTC) + LATER)

    with pytest.raises(LedgerError) as zero:
        books.capture_hold(hold["id"], store["id"], 0)
    with pytest.raises(LedgerError) as other_currency:
        books.capture_hold(hold["id"], euros["id"])

    assert zero.value.code == "INVALID_AMOUNT"
    assert other_currency.value.code == "CURRENCY_MISMATCH"
    assert books.get_hold(hold["id"])["status"] == "active"  # nothing was written
