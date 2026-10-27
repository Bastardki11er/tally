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

With ``client_id`` the report covers one client's books only: that is what the HTTP
endpoint serves, since a client may not learn anything about anyone else's. The CLI
checks everything, including whether any entry crossed from one client's books into
another's.
"""

from __future__ import annotations

from psycopg import Connection

MAX_FINDINGS = 100

# Signed toward debits (for "do debits equal credits") and toward the account's
# normal side (for "what should the balance be").
DEBIT_SIGNED = "CASE e.direction WHEN 'debit' THEN e.amount ELSE -e.amount END"
BALANCE_SIGNED = "CASE WHEN e.direction = a.normal_side THEN e.amount ELSE -e.amount END"

# Restricts a check to one client when %(client)s is set; NULL means the whole ledger.
MINE = "(%(client)s::bigint IS NULL OR {}.client_id = %(client)s)"

# Each check selects the offending rows; count(*) OVER () is evaluated before LIMIT,
# so it carries the total number of findings even when only the first rows come back.
CHECKS = {
    "unbalanced_transactions": f"""
        SELECT e.transaction_id, e.currency, sum({DEBIT_SIGNED}) AS debits_minus_credits
          FROM entries e
         WHERE {MINE.format("e")}
         GROUP BY e.transaction_id, e.currency
        HAVING sum({DEBIT_SIGNED}) <> 0
    """,
    "incomplete_transactions": f"""
        SELECT t.id AS transaction_id, count(e.id) AS entries
          FROM transactions t LEFT JOIN entries e ON e.transaction_id = t.id
         WHERE {MINE.format("t")}
         GROUP BY t.id
        HAVING count(e.id) < 2
    """,
    "currency_mismatches": f"""
        SELECT e.id AS entry_id, e.account_id, e.currency AS entry_currency, a.currency AS account_currency
          FROM entries e JOIN accounts a ON a.id = e.account_id
         WHERE e.currency <> a.currency AND {MINE.format("e")}
    """,
    "balance_mismatches": f"""
        SELECT a.id AS account_id, a.balance AS stored, coalesce(c.computed, 0) AS computed
          FROM accounts a
          LEFT JOIN (SELECT e.account_id, sum({BALANCE_SIGNED}) AS computed
                       FROM entries e JOIN accounts a ON a.id = e.account_id
                      WHERE {MINE.format("a")}
                      GROUP BY e.account_id) c ON c.account_id = a.id
         WHERE a.balance <> coalesce(c.computed, 0) AND {MINE.format("a")}
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
                 WHERE {MINE.format("a")}
                WINDOW per_account AS (PARTITION BY e.account_id ORDER BY e.id)) s
         WHERE stored_balance_after <> computed_balance_after OR timestamp_out_of_order
    """,
    "hold_mismatches": f"""
        SELECT a.id AS account_id, a.held AS stored, coalesce(h.total, 0) AS computed
          FROM accounts a
          LEFT JOIN (SELECT account_id, sum(amount) AS total FROM holds WHERE status = 'active' GROUP BY account_id) h
                 ON h.account_id = a.id
         WHERE a.held <> coalesce(h.total, 0) AND {MINE.format("a")}
    """,
    # An entry, its transaction and its account all belong to the same client
    # (composite foreign keys, migration 007), and so do a hold and its account.
    "client_mismatches": f"""
        SELECT 'entry' AS kind, e.id::text AS row_id, e.client_id, t.client_id AS transaction_client,
               a.client_id AS account_client
          FROM entries e JOIN transactions t ON t.id = e.transaction_id JOIN accounts a ON a.id = e.account_id
         WHERE (e.client_id <> t.client_id OR e.client_id <> a.client_id) AND {MINE.format("e")}
        UNION ALL
        SELECT 'hold', h.id::text, h.client_id, NULL, a.client_id
          FROM holds h JOIN accounts a ON a.id = h.account_id
         WHERE h.client_id <> a.client_id AND {MINE.format("h")}
    """,
}


def reconcile(conn: Connection, timeout_seconds: int = 600, client_id: int | None = None) -> dict:
    """Runs every check in one consistent snapshot and returns the report (one client's, if given)."""
    # Must come before any query in the transaction.
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    # A full re-derivation scans every entry several times; on a big ledger that
    # outlasts the connection's default statement_timeout, which is sized for requests.
    conn.execute(f"SET LOCAL statement_timeout = '{int(timeout_seconds)}s'")

    report: dict = {"checked_at": conn.execute("SELECT now() AS t").fetchone()["t"]}  # the snapshot's time
    params = {"client": client_id, "limit": MAX_FINDINGS}
    totals = {}
    for name, sql in CHECKS.items():
        rows = conn.execute(
            f"SELECT *, count(*) OVER () AS total_findings FROM ({sql}) findings LIMIT %(limit)s", params
        ).fetchall()
        totals[name] = rows[0]["total_findings"] if rows else 0
        report[name] = [{k: v for k, v in row.items() if k != "total_findings"} for row in rows]

    trial = conn.execute(
        f"""
        SELECT currency, count(*) AS entries,
               coalesce(sum(amount) FILTER (WHERE direction = 'debit'), 0) AS debits,
               coalesce(sum(amount) FILTER (WHERE direction = 'credit'), 0) AS credits
          FROM entries e WHERE {MINE.format("e")} GROUP BY currency ORDER BY currency
        """,
        params,
    ).fetchall()
    counts = conn.execute(
        f"SELECT (SELECT count(*) FROM accounts a WHERE {MINE.format('a')}) AS accounts, "
        f"(SELECT count(*) FROM transactions t WHERE {MINE.format('t')}) AS transactions",
        params,
    ).fetchone()

    return {
        "ok": not any(totals.values()),
        **counts,
        "entries": sum(row["entries"] for row in trial),  # counted by the trial balance's scan
        "trial_balance": [{k: v for k, v in row.items() if k != "entries"} for row in trial],
        "totals": totals,
        **report,
    }
