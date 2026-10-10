"""Reconciliation: independently re-derive every balance from the raw entries and compare.

The database already prevents unbalanced transactions, but stored balances are
a cache of the entry history, and caches can drift (a bug, a manual UPDATE, a
restore from backup). This report recomputes everything from first principles.
It runs in a REPEATABLE READ snapshot, so transfers committing mid-report can't
produce false alarms.
"""

from __future__ import annotations

from psycopg import Connection

SIGNED = "CASE direction WHEN 'debit' THEN amount ELSE -amount END"


def reconcile(conn: Connection) -> dict:
    # Must be the first statement of the transaction.
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

    unbalanced = conn.execute(
        f"""
        SELECT transaction_id, currency, sum({SIGNED}) AS debits_minus_credits
          FROM entries GROUP BY transaction_id, currency
        HAVING sum({SIGNED}) <> 0
         LIMIT 100
        """
    ).fetchall()

    balance_mismatches = conn.execute(
        """
        SELECT a.id AS account_id, a.balance AS stored, coalesce(c.computed, 0) AS computed
          FROM accounts a
          LEFT JOIN (
                SELECT e.account_id,
                       sum(CASE WHEN e.direction = acc.normal_side THEN e.amount ELSE -e.amount END) AS computed
                  FROM entries e JOIN accounts acc ON acc.id = e.account_id
                 GROUP BY e.account_id
               ) c ON c.account_id = a.id
         WHERE a.balance <> coalesce(c.computed, 0)
         ORDER BY a.id
         LIMIT 100
        """
    ).fetchall()

    hold_mismatches = conn.execute(
        """
        SELECT a.id AS account_id, a.held AS stored, coalesce(h.total, 0) AS computed
          FROM accounts a
          LEFT JOIN (SELECT account_id, sum(amount) AS total FROM holds WHERE status = 'active' GROUP BY account_id) h
                 ON h.account_id = a.id
         WHERE a.held <> coalesce(h.total, 0)
         ORDER BY a.id
         LIMIT 100
        """
    ).fetchall()

    trial_balance = conn.execute(
        """
        SELECT currency,
               coalesce(sum(amount) FILTER (WHERE direction = 'debit'), 0) AS debits,
               coalesce(sum(amount) FILTER (WHERE direction = 'credit'), 0) AS credits
          FROM entries GROUP BY currency ORDER BY currency
        """
    ).fetchall()

    counts = conn.execute(
        "SELECT (SELECT count(*) FROM accounts) AS accounts, (SELECT count(*) FROM transactions) AS transactions, "
        "(SELECT count(*) FROM entries) AS entries"
    ).fetchone()

    return {
        "ok": not (unbalanced or balance_mismatches or hold_mismatches),
        **counts,
        "trial_balance": trial_balance,
        "unbalanced_transactions": unbalanced,
        "balance_mismatches": balance_mismatches,
        "hold_mismatches": hold_mismatches,
    }
