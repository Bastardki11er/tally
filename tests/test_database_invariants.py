"""The database enforces the ledger's rules on its own, even against hand-written SQL.

These tests deliberately bypass the application to prove that a buggy service,
a migration script or a tired engineer at a psql prompt still can't corrupt the books.
"""

import psycopg
import pytest


@pytest.fixture
def raw(migrated):
    with psycopg.connect(migrated) as conn:
        yield conn


@pytest.fixture
def payment(books, wallet):
    alice, bob = wallet("alice", funded=1_000), wallet("bob")
    return books.transfer(alice["id"], bob["id"], 250)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE entries SET amount = amount + 1",
        "DELETE FROM entries",
        "UPDATE transactions SET description = 'edited'",
        "DELETE FROM transactions",
    ],
)
def test_history_is_append_only(raw, payment, statement):
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"):
        raw.execute(statement)


def test_an_unbalanced_transaction_fails_at_commit(raw, wallet):
    alice, bob = wallet("alice"), wallet("bob")
    txn = raw.execute("INSERT INTO transactions DEFAULT VALUES RETURNING id").fetchone()[0]
    raw.execute(
        "INSERT INTO entries (transaction_id, account_id, direction, amount, currency, balance_after) VALUES "
        "(%s, %s, 'debit', 100, 'USD', 0), (%s, %s, 'credit', 99, 'USD', 0)",
        (txn, alice["id"], txn, bob["id"]),
    )

    # The check is deferred: it fires once the whole transaction is in place.
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="does not balance"):
        raw.commit()


def test_a_transaction_needs_at_least_two_entries(raw):
    raw.execute("INSERT INTO transactions DEFAULT VALUES")

    with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="at least two entries"):
        raw.commit()


def test_entries_must_use_their_accounts_currency(raw, wallet):
    alice = wallet("alice")
    txn = raw.execute("INSERT INTO transactions DEFAULT VALUES RETURNING id").fetchone()[0]

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        raw.execute(
            "INSERT INTO entries (transaction_id, account_id, direction, amount, currency, balance_after) "
            "VALUES (%s, %s, 'debit', 100, 'EUR', 0)",
            (txn, alice["id"]),
        )


def test_a_protected_account_can_never_go_negative(raw, wallet):
    alice = wallet("alice", funded=100)

    with pytest.raises(psycopg.errors.CheckViolation, match="accounts_no_overdraft"):
        raw.execute("UPDATE accounts SET balance = -1 WHERE id = %s", (alice["id"],))


def test_a_transaction_cannot_be_reversed_twice_even_by_hand(raw, payment):
    raw.execute("INSERT INTO transactions (reverses_id) VALUES (%s)", (payment["id"],))

    with pytest.raises(psycopg.errors.UniqueViolation):
        raw.execute("INSERT INTO transactions (reverses_id) VALUES (%s)", (payment["id"],))


def test_amounts_must_be_positive(raw, wallet):
    alice = wallet("alice")
    txn = raw.execute("INSERT INTO transactions DEFAULT VALUES RETURNING id").fetchone()[0]

    with pytest.raises(psycopg.errors.CheckViolation):
        raw.execute(
            "INSERT INTO entries (transaction_id, account_id, direction, amount, currency, balance_after) "
            "VALUES (%s, %s, 'debit', -5, 'USD', 0)",
            (txn, alice["id"]),
        )
