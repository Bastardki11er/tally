#!/usr/bin/env python3
"""Measures the queries that migration 002's indexes exist for, before and after.

Builds a synthetic ledger directly in SQL: 1,000 accounts, 1,000,000 transactions
and 2,000,000 entries spread over a year, with one hot account (a big merchant)
in 20% of all transactions. Then it runs each query with EXPLAIN ANALYZE on the
schema as of migration 001, applies the remaining migrations, and runs the queries
again on the same data.

    python scripts/index_benchmark.py postgresql://user:pass@localhost:5432/tally_bench

The target database is WIPED. The bulk load skips triggers (session_replication_role
= replica), which needs a superuser; that is fine for throwaway benchmark data,
and the generated transactions balance anyway.
"""

import statistics
import sys
import time

import psycopg

from tally.db import migrate

HOT_ACCOUNT = 1
TYPICAL_ACCOUNT = 500

LOAD = """
SET session_replication_role = replica;
INSERT INTO accounts (name, type, currency, allow_negative)
SELECT 'account ' || g, 'liability', 'USD', true FROM generate_series(1, 1000) g;
INSERT INTO transactions (description, posted_at)
SELECT 'synthetic', timestamptz '2025-01-01 00:00:00+00' + g * interval '30 seconds'
  FROM generate_series(1, 1000000) g;
INSERT INTO entries (transaction_id, account_id, direction, amount, currency, balance_after, posted_at)
SELECT t.id, leg.account_id, leg.direction, 100, 'USD', 0, t.posted_at
  FROM (SELECT id, posted_at, row_number() OVER (ORDER BY posted_at) AS n FROM transactions) t
 CROSS JOIN LATERAL (VALUES
        (CASE WHEN t.n % 5 = 0 THEN 1 ELSE 2 + abs(hashint8(t.n)::bigint) % 999 END, 'debit'),
        (2 + abs(hashint8(t.n * 7919)::bigint) % 999, 'credit')
       ) AS leg (account_id, direction)
 ORDER BY t.n;
SET session_replication_role = origin;
"""

# What ledger.balance_at ran before migration 002, and what it runs now. Within one
# account, posted_at increases with id (timestamps are taken under the account's
# row lock), so ordering by (posted_at, id) returns the same row and can use the new index.
BALANCE_AT_BEFORE = (
    "SELECT balance_after FROM entries WHERE account_id = %s AND posted_at <= %s ORDER BY id DESC LIMIT 1"
)
BALANCE_AT_AFTER = (
    "SELECT balance_after FROM entries WHERE account_id = %s AND posted_at <= %s "
    "ORDER BY posted_at DESC, id DESC LIMIT 1"
)
RECENT_FEED = (
    "SELECT id, description, posted_at, (SELECT r.id FROM transactions r WHERE r.reverses_id = t.id) AS reversed_by "
    "FROM transactions t ORDER BY posted_at DESC, id DESC LIMIT 20"
)

CASES = [
    ("Balance as of Jan 8, hot account (200k entries)", "balance", (HOT_ACCOUNT, "2025-01-08")),
    ("Balance as of Dec 10, hot account", "balance", (HOT_ACCOUNT, "2025-12-10")),
    ("Balance as of Jan 8, typical account (~1.8k entries)", "balance", (TYPICAL_ACCOUNT, "2025-01-08")),
    ("Recent transactions feed (latest 20 of 1M)", "feed", ()),
]


def measure(conn, sql: str, params: tuple, runs: int = 7) -> tuple[float, str]:
    """Median execution time in ms over several warm runs, and a one-line plan summary."""
    times, plan = [], None
    for _ in range(runs):
        result = conn.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}", params).fetchone()[0][0]
        times.append(result["Execution Time"])
        plan = result["Plan"]
    return statistics.median(times[1:]), summarize(plan)


def summarize(plan: dict) -> str:
    nodes = []

    def walk(node):
        label = node["Node Type"]
        if "Index Name" in node:
            label += f" on {node['Index Name']}"
        nodes.append(label)
        for child in node.get("Plans", []):
            if child.get("Parent Relationship") != "SubPlan":
                walk(child)

    walk(plan)
    return " → ".join(n for n in nodes if n != "Limit")


def run_cases(conn, balance_sql: str) -> list[tuple[float, str]]:
    results = []
    for _, kind, params in CASES:
        sql = balance_sql if kind == "balance" else RECENT_FEED
        results.append(measure(conn, sql, params))
    return results


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    url = sys.argv[1]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    migrate(url, until="001_ledger")

    with psycopg.connect(url, autocommit=True) as conn:
        started = time.perf_counter()
        conn.execute(LOAD)
        conn.execute("VACUUM ANALYZE")
        counts = conn.execute("SELECT (SELECT count(*) FROM transactions), (SELECT count(*) FROM entries)").fetchone()
        hot = conn.execute("SELECT count(*) FROM entries WHERE account_id = %s", (HOT_ACCOUNT,)).fetchone()[0]
        print(
            f"loaded {counts[0]:,} transactions, {counts[1]:,} entries ({hot:,} on the hot account) "
            f"in {time.perf_counter() - started:.0f}s\n"
        )
        before = run_cases(conn, BALANCE_AT_BEFORE)

    applied = migrate(url)
    print(f"applied {', '.join(applied)}\n")

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("ANALYZE")
        after = run_cases(conn, BALANCE_AT_AFTER)

    print("| Query | Before (migration 001) | After (migration 002) | Speedup |")
    print("|---|---|---|---|")
    for (label, _, _), (t0, p0), (t1, p1) in zip(CASES, before, after, strict=True):
        print(f"| {label} | {t0:.2f} ms<br/>`{p0}` | {t1:.3f} ms<br/>`{p1}` | {t0 / t1:,.0f}x |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
