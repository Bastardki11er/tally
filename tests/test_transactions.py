from datetime import UTC, datetime

import pytest

from tally.errors import LedgerError
from tally.ledger import Leg


def balance(books, account) -> int:
    return books.get_account(account["id"])["balance"]


def test_a_deposit_increases_both_sides_of_the_books(books, cash, wallet):
    alice = wallet("alice")

    transaction = books.transfer(cash["id"], alice["id"], 10_000, "deposit")

    # Cash is an asset (debit-normal), the wallet a liability (credit-normal):
    # one debit and one credit of the same amount raise both.
    assert balance(books, cash) == 10_000
    assert balance(books, alice) == 10_000
    assert [(e["account_id"], e["direction"], e["amount"], e["balance_after"]) for e in transaction["entries"]] == [
        (cash["id"], "debit", 10_000, 10_000),
        (alice["id"], "credit", 10_000, 10_000),
    ]


def test_a_multi_leg_payment_with_a_fee(books, wallet):
    alice, bob = wallet("alice", funded=5_000), wallet("bob")
    fees = books.create_account("Fee revenue", "revenue", "USD")

    books.post(
        [Leg(alice["id"], "debit", 2_575), Leg(bob["id"], "credit", 2_500), Leg(fees["id"], "credit", 75)],
        "dinner",
    )

    assert balance(books, alice) == 2_425
    assert balance(books, bob) == 2_500
    assert balance(books, fees) == 75


def test_unbalanced_transactions_are_rejected_and_nothing_is_written(books, wallet):
    alice, bob = wallet("alice", funded=1_000), wallet("bob")

    with pytest.raises(LedgerError) as error:
        books.post([Leg(alice["id"], "debit", 500), Leg(bob["id"], "credit", 499)])

    assert error.value.code == "UNBALANCED"
    assert balance(books, alice) == 1_000
    assert books.statement(bob["id"]) == []


def test_each_currency_must_balance_on_its_own(books, wallet):
    usd_a, usd_b = wallet("usd a", funded=1_000), wallet("usd b")
    eur_cash = books.create_account("EUR cash", "asset", "EUR")
    eur_a, eur_b = books.create_account("eur a", "liability", "EUR"), books.create_account("eur b", "liability", "EUR")
    books.transfer(eur_cash["id"], eur_a["id"], 1_000)

    # Balanced per currency: fine, even in one transaction.
    books.post(
        [
            Leg(usd_a["id"], "debit", 100),
            Leg(usd_b["id"], "credit", 100),
            Leg(eur_a["id"], "debit", 90),
            Leg(eur_b["id"], "credit", 90),
        ]
    )
    # Nets to zero overall, but USD and EUR each don't balance: rejected.
    with pytest.raises(LedgerError) as error:
        books.post([Leg(usd_a["id"], "debit", 100), Leg(eur_b["id"], "credit", 100)])
    assert error.value.code == "UNBALANCED"


def test_overdrafts_are_refused(books, wallet):
    alice, bob = wallet("alice", funded=1_000), wallet("bob")

    with pytest.raises(LedgerError) as error:
        books.transfer(alice["id"], bob["id"], 1_001)

    assert error.value.code == "INSUFFICIENT_FUNDS"
    assert error.value.status == 409
    assert balance(books, alice) == 1_000


def test_accounts_flagged_allow_negative_can_go_below_zero(books, wallet):
    # e.g. a settlement account with a counterparty that may owe us money
    counterparty = books.create_account("Card network", "liability", "USD", allow_negative=True)
    alice = wallet("alice")

    books.transfer(counterparty["id"], alice["id"], 700)

    assert balance(books, counterparty) == -700


def test_unknown_accounts_and_self_transfers_are_rejected(books, wallet):
    alice = wallet("alice", funded=100)

    with pytest.raises(LedgerError) as missing:
        books.transfer(alice["id"], 999_999, 10)
    with pytest.raises(LedgerError) as same:
        books.transfer(alice["id"], alice["id"], 10)

    assert missing.value.code == "ACCOUNT_NOT_FOUND"
    assert same.value.code == "SAME_ACCOUNT"


def test_reversal_posts_the_mirror_image_and_links_both_transactions(books, wallet):
    alice, bob = wallet("alice", funded=1_000), wallet("bob")
    payment = books.transfer(alice["id"], bob["id"], 300, "oops")

    reversal = books.reverse(payment["id"], "refund")

    assert balance(books, alice) == 1_000
    assert balance(books, bob) == 0
    assert reversal["reverses_id"] == payment["id"]
    assert [(e["account_id"], e["direction"]) for e in reversal["entries"]] == [
        (alice["id"], "credit"),
        (bob["id"], "debit"),
    ]
    assert books.get_transaction(payment["id"])["reversed_by"] == reversal["id"]


def test_a_transaction_can_be_reversed_only_once_and_reversals_cannot_be_reversed(books, wallet):
    alice, bob = wallet("alice", funded=1_000), wallet("bob")
    payment = books.transfer(alice["id"], bob["id"], 300)
    reversal = books.reverse(payment["id"])

    with pytest.raises(LedgerError) as twice:
        books.reverse(payment["id"])
    with pytest.raises(LedgerError) as of_reversal:
        books.reverse(reversal["id"])

    assert twice.value.code == "ALREADY_REVERSED"
    assert of_reversal.value.code == "CANNOT_REVERSE_REVERSAL"


def test_a_reversal_needs_the_funds_to_still_be_there(books, wallet):
    alice, bob, carol = wallet("alice", funded=500), wallet("bob"), wallet("carol")
    payment = books.transfer(alice["id"], bob["id"], 500)
    books.transfer(bob["id"], carol["id"], 500)  # bob already spent it

    with pytest.raises(LedgerError) as error:
        books.reverse(payment["id"])

    assert error.value.code == "INSUFFICIENT_FUNDS"


def test_statement_is_newest_first_with_running_balances_and_keyset_pages(books, wallet):
    alice, bob = wallet("alice", funded=1_000), wallet("bob")
    for amount in (100, 200, 300):
        books.transfer(alice["id"], bob["id"], amount)

    first = books.statement(alice["id"], limit=2)
    second = books.statement(alice["id"], limit=2, before_id=first[-1]["id"])

    assert [(line["amount"], line["balance_after"]) for line in first] == [(300, 400), (200, 700)]
    assert [(line["amount"], line["balance_after"]) for line in second] == [(100, 900), (1_000, 1_000)]


def test_balance_as_of_a_past_moment(books, db, wallet):
    alice, bob = wallet("alice", funded=1_000), wallet("bob")
    between = db.run(lambda conn: conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"])
    books.transfer(alice["id"], bob["id"], 400)

    assert books.balance_at(alice["id"], between) == 1_000
    assert books.balance_at(alice["id"], datetime.now(UTC)) == 600
    assert books.balance_at(alice["id"], datetime(2000, 1, 1, tzinfo=UTC)) == 0
