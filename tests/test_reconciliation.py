"""Reconciliation must catch every way the books can go wrong once the triggers are bypassed.

Normal writes can't corrupt the ledger any more, so each test damages it the way a
bulk load or a restore tool would: as a superuser in replica mode, which skips every
trigger, foreign keys included.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from tally.reconciliation import MAX_FINDINGS, reconcile


def tamper(db, sql: str, params: tuple = ()) -> None:
    def run(conn):
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(sql, params)

    db.run(run)


def insert_entry(db, account_id, direction, amount, currency="USD", balance_after=0, posted_at=None):
    """A bare entry in its own bare transaction, written past every trigger."""

    def run(conn):
        conn.execute("SET LOCAL session_replication_role = replica")
        txn = conn.execute("INSERT INTO transactions DEFAULT VALUES RETURNING id").fetchone()["id"]
        conn.execute(
            "INSERT INTO entries (transaction_id, account_id, direction, amount, currency, balance_after, posted_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, coalesce(%s, clock_timestamp()))",
            (txn, account_id, direction, amount, currency, balance_after, posted_at),
        )
        return txn

    return db.run(run)


@pytest.fixture
def ledger(books, wallet):
    """A small healthy ledger: two funded wallets, a payment, an active hold."""
    alice, bob = wallet("alice", funded=1_000), wallet("bob", funded=500)
    books.transfer(alice["id"], bob["id"], 250)
    books.create_hold(bob["id"], 100, datetime.now(UTC) + timedelta(days=1))
    return {"alice": alice, "bob": bob}


def test_a_healthy_ledger_reconciles(db, ledger):
    report = db.run(reconcile)

    assert report["ok"] is True
    assert set(report["totals"].values()) == {0}
    assert (report["accounts"], report["transactions"], report["entries"]) == (3, 3, 6)
    assert report["trial_balance"] == [{"currency": "USD", "debits": 1_750, "credits": 1_750}]
    assert report["checked_at"] is not None


def test_a_stored_balance_that_drifted_is_reported(db, ledger):
    tamper(db, "UPDATE accounts SET balance = balance + 5 WHERE id = %s", (ledger["alice"]["id"],))

    report = db.run(reconcile)

    assert report["ok"] is False
    assert report["balance_mismatches"] == [{"account_id": ledger["alice"]["id"], "stored": 755, "computed": 750}]


def test_a_held_amount_that_drifted_is_reported(db, ledger):
    tamper(db, "UPDATE accounts SET held = 0 WHERE id = %s", (ledger["bob"]["id"],))

    report = db.run(reconcile)

    assert report["hold_mismatches"] == [{"account_id": ledger["bob"]["id"], "stored": 0, "computed": 100}]


def test_unbalanced_and_incomplete_transactions_are_reported(db, ledger):
    txn = insert_entry(db, ledger["alice"]["id"], "debit", 40)

    report = db.run(reconcile)

    assert {"transaction_id": txn, "currency": "USD", "debits_minus_credits": 40} in report["unbalanced_transactions"]
    assert {"transaction_id": txn, "entries": 1} in report["incomplete_transactions"]


def test_a_wrong_running_balance_is_reported(db, ledger):
    # A credit of 10 on a wallet holding 750 should record 760, not 0.
    insert_entry(db, ledger["alice"]["id"], "credit", 10, balance_after=0)

    findings = db.run(reconcile)["entry_mismatches"]

    assert [(f["stored_balance_after"], f["computed_balance_after"]) for f in findings] == [(0, 760)]


def test_an_entry_timestamped_before_its_predecessor_is_reported(db, ledger):
    alice = ledger["alice"]["id"]
    insert_entry(db, alice, "credit", 10, balance_after=760, posted_at=datetime(2000, 1, 1, tzinfo=UTC))

    findings = db.run(reconcile)["entry_mismatches"]

    assert [(f["account_id"], f["timestamp_out_of_order"]) for f in findings] == [(alice, True)]


def test_an_entry_in_the_wrong_currency_is_reported(db, ledger):
    insert_entry(db, ledger["alice"]["id"], "credit", 10, currency="EUR", balance_after=760)

    findings = db.run(reconcile)["currency_mismatches"]

    assert [(f["entry_currency"], f["account_currency"]) for f in findings] == [("EUR", "USD")]


def test_findings_are_capped_but_the_total_is_exact(db, books):
    accounts = [books.create_account(f"a{i}", "liability", "USD")["id"] for i in range(MAX_FINDINGS + 20)]
    tamper(db, "UPDATE accounts SET balance = 1 WHERE id = ANY(%s)", (accounts,))

    report = db.run(reconcile)

    assert len(report["balance_mismatches"]) == MAX_FINDINGS
    assert report["totals"]["balance_mismatches"] == MAX_FINDINGS + 20


def test_the_whole_report_describes_one_moment_even_while_transfers_commit(db, books, wallet):
    # Each check is a single statement, so on its own it already sees one moment, even in
    # READ COMMITTED. The snapshot matters for the report as a whole: in READ COMMITTED
    # the entry count (trial balance query) and the transaction count (a later query)
    # would come from different moments, and a transfer landing in between breaks the
    # "two entries per transfer" relationship. One REPEATABLE READ snapshot keeps it.
    wallets = [wallet(f"w{i}", funded=10_000)["id"] for i in range(10)]

    def transfer(i):
        books.transfer(wallets[i % 10], wallets[(i + 1) % 10], 1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        moving = pool.map(transfer, range(600))
        reports = [db.run(reconcile) for _ in range(25)]
        list(moving)

    assert all(report["ok"] for report in reports)
    assert all(report["entries"] == 2 * report["transactions"] for report in reports)
