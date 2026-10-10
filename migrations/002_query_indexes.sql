-- migration: no-transaction
--
-- Indexes for two queries that slowed down as history grew (measured with
-- scripts/index_benchmark.py; numbers in the README).
--
-- CONCURRENTLY builds each index without blocking writes, so this can run against
-- a live ledger. It can't run inside a transaction, hence the marker above. If a
-- build fails halfway it leaves an INVALID index behind; dropping first makes a
-- re-run after a crash start clean.

-- "Balance as of T": the newest entry of one account at or before T. Without this
-- index the planner walked the whole table's primary key backwards, filtering every
-- row on account_id and posted_at: 112 ms for a hot account on 2M entries.
DROP INDEX CONCURRENTLY IF EXISTS entries_by_account_time;
CREATE INDEX CONCURRENTLY entries_by_account_time ON entries (account_id, posted_at, id);

-- Recent activity feed: ORDER BY posted_at DESC LIMIT n sorted the whole table.
DROP INDEX CONCURRENTLY IF EXISTS transactions_by_posted_at;
CREATE INDEX CONCURRENTLY transactions_by_posted_at ON transactions (posted_at, id);
