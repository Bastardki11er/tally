"""Reconciliation: re-derive everything from the raw entries and holds, and compare.

The database's triggers keep the books consistent on every write: balanced
transactions, derived balances and running balances, held amounts, timestamps.
But triggers can be bypassed. A superuser who sets session_replication_role =
replica (as bulk loads and some restore tools do) skips all of them, foreign keys
included, and a bug in a trigger would be invisible to everything built on top of
it. So this report checks each of those rules again from first principles, without
trusting any stored total.

Each check is a single statement, so on its own it sees one moment even under
READ COMMITTED. The REPEATABLE READ snapshot is about the report as a whole: all
checks, counts and the trial balance describe the same moment (``checked_at``),
even while transfers keep committing. It lists at most MAX_FINDINGS rows per
check while still reporting the exact number of findings.
"""

from __future__ import annotations

from psycopg import Connection

MAX_FINDINGS = 100

# Signed toward debits (for "do debits equal credits") and toward the account's
# normal side (for "what should the balance be").
DEBIT_SIGNED = "CASE e.direction WHEN 'debit' THEN e.amount ELSE -e.amount END"
BALANCE_SIGNED = "CASE WHEN e.direction = a.normal_side THEN e.amount ELSE -e.amount END"

# Each check selects the offending rows; count(*) OVER () is evaluated before LIMIT,
# so it carries the total number of findings even when only the first rows come back.
CHECKS = {
    "unbalanced_transactions": f"""
        SELECT e.transaction_id, e.currency, sum({DEBIT_SIGNED}) AS debits_minus_credits
          FROM entries e
         GROUP BY e.transaction_id, e.currency
        HAVING sum({DEBIT_SIGNED}) <> 0
    """,
    "incomplete_transactions": """
        SELECT t.id AS transaction_id, count(e.id) AS entries
          FROM transactions t LEFT JOIN entries e ON e.transaction_id = t.id
         GROUP BY t.id
        HAVING count(e.id) < 2
    """,
    "currency_mismatches": """
        SELECT e.id AS entry_id, e.account_id, e.currency AS entry_currency, a.currency AS account_currency
          FROM entries e JOIN accounts a ON a.id = e.account_id
         WHERE e.currency <> a.currency
    """,
    "balance_mismatches": f"""
        SELECT a.id AS account_id, a.balance AS stored, coalesce(c.computed, 0) AS computed
          FROM accounts a
          LEFT JOIN (SELECT e.account_id, sum({BALANCE_SIGNED}) AS computed
                       FROM entries e JOIN accounts a ON a.id = e.account_id
                      GROUP BY e.account_id) c ON c.account_id = a.id
         WHERE a.balance <> coalesce(c.computed, 0)
    """,
    # Statements and "balance as of" read balance_after directly, so it must equal the
    # account's cumulative sum at that entry, and posted_at must never go backwards
    # within the account (point-in-time lookups order by it).
    "entry_mismatches": f"""
        SELECT entry_id, account_id, stored_balance_after, computed_balance_after, timestamp_out_of_order
          FROM (SELECT e.id AS entry_id, e.account_id, e.balance_after AS stored_balance_after,
                       sum({BALANCE_SIGNED}) OVER per_account AS computed_balance_after,
                       coalesce(e.posted_at < lag(e.posted_at) OVER per_account, false) AS timestamp_out_of_order
                  FROM entries e JOIN accounts a ON a.id = e.account_id
                WINDOW per_account AS (PARTITION BY e.account_id ORDER BY e.id)) s
         WHERE stored_balance_after <> computed_balance_after OR timestamp_out_of_order
    """,
    "hold_mismatches": """
        SELECT a.id AS account_id, a.held AS stored, coalesce(h.total, 0) AS computed
          FROM accounts a
          LEFT JOIN (SELECT account_id, sum(amount) AS total FROM holds WHERE status = 'active' GROUP BY account_id) h
                 ON h.account_id = a.id
         WHERE a.held <> coalesce(h.total, 0)
    """,
}


def reconcile(conn: Connection, timeout_seconds: int = 600) -> dict:
    """Runs every check in one consistent snapshot and returns the report."""
    # Must come before any query in the transaction.
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    # A full re-derivation scans every entry several times; on a big ledger that
    # outlasts the connection's default statement_timeout, which is sized for requests.
    conn.execute(f"SET LOCAL statement_timeout = '{int(timeout_seconds)}s'")

    report: dict = {"checked_at": conn.execute("SELECT now() AS t").fetchone()["t"]}  # the snapshot's time
    totals = {}
    for name, sql in CHECKS.items():
        rows = conn.execute(
            f"SELECT *, count(*) OVER () AS total_findings FROM ({sql}) findings LIMIT %s", (MAX_FINDINGS,)
        ).fetchall()
        totals[name] = rows[0]["total_findings"] if rows else 0
        report[name] = [{k: v for k, v in row.items() if k != "total_findings"} for row in rows]

    trial = conn.execute(
        """
        SELECT currency, count(*) AS entries,
               coalesce(sum(amount) FILTER (WHERE direction = 'debit'), 0) AS debits,
               coalesce(sum(amount) FILTER (WHERE direction = 'credit'), 0) AS credits
          FROM entries GROUP BY currency ORDER BY currency
        """
    ).fetchall()
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM accounts) AS accounts, (SELECT count(*) FROM transactions) AS transactions"
    ).fetchone()

    return {
        "ok": not any(totals.values()),
        **counts,
        "entries": sum(row["entries"] for row in trial),  # counted by the trial balance's scan
        "trial_balance": [{k: v for k, v in row.items() if k != "entries"} for row in trial],
        "totals": totals,
        **report,
    }
