# Tally

[![CI](https://github.com/Bastardki11er/tally/actions/workflows/ci.yml/badge.svg)](https://github.com/Bastardki11er/tally/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)
![PostgreSQL 17](https://img.shields.io/badge/PostgreSQL-17-336791)

A **double-entry ledger API**: the part of a payments or wallet company that records where every cent
is. Accounts, transfers, multi-leg transactions, card-style holds, reversals, statements and
reconciliation.

The interesting part is **where the rules live**. "Debits equal credits", "history is never edited"
and "no overdrafts" are enforced by **PostgreSQL itself** (deferred constraint triggers, CHECK
constraints, composite foreign keys), not just by application code. A buggy service, a bad script or a
hand-typed `UPDATE` can't corrupt the books.

**Stack:** Python 3.13 · FastAPI · PostgreSQL 17 · psycopg 3 (raw SQL, connection pool) · pytest · Hypothesis (stateful, model-based) · Testcontainers · Docker Compose · GitHub Actions

![Tally console](docs/console.jpg)

*The built-in console (`/`): live reconciliation status, a journal-entry editor that totals debits and credits as you type, statements with running balances, holds, and a button that replays the last request with the same idempotency key.*

## Double-entry in 30 seconds

Every movement of money is a **transaction** made of **entries**. Each entry debits or credits one account,
and the debits must equal the credits. Whether a debit makes an account go up or down depends on its type:

| Account type | Examples | Increased by |
|---|---|---|
| Asset | Cash at the bank | debit |
| Liability | Customer wallets (money we owe customers) | credit |
| Revenue | Fees we earn | credit |
| Expense, Equity | Costs; owners' capital | debit, credit |

| What happened | Debit | Credit |
|---|---|---|
| Alice deposits $500 | Cash $500 | Alice's wallet $500 |
| Alice pays Bob $25 with a $0.75 fee | Alice's wallet $25.75 | Bob's wallet $25.00, Fee revenue $0.75 |
| Bob withdraws $50 | Bob's wallet $50 | Cash $50 |

Because every transaction balances, the whole ledger always balances. Any error shows up as a nonzero
sum, which is what the reconciliation report checks.

## Guarantees, and how each is enforced

| Guarantee | Enforced by | Proven by |
|---|---|---|
| Every transaction balances, per currency | `CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED`, checked at `COMMIT` | `test_an_unbalanced_transaction_fails_at_commit` (raw SQL) |
| A transaction has at least two entries | Deferred constraint trigger on `transactions` | `test_a_transaction_needs_at_least_two_entries` |
| History is append-only; fixes are reversals | `BEFORE UPDATE OR DELETE` triggers that raise | `test_history_is_append_only` (4 cases) |
| An entry's currency matches its account | Composite FK `(account_id, currency)` → `accounts (id, currency)` | `test_entries_must_use_their_accounts_currency` |
| Protected accounts never go negative | `CHECK (allow_negative OR balance - held >= 0)`, plus a service-level check under row lock | `test_a_protected_account_can_never_go_negative`, `test_concurrent_withdrawals_never_overdraw` |
| A transaction is reversed at most once | `UNIQUE (reverses_id)`, plus a row lock on the original | `test_a_transaction_reversed_concurrently_is_reversed_once` |
| A retried request moves money once | Idempotency key claimed in the **same** DB transaction as the money movement | `test_concurrent_requests_with_one_idempotency_key_move_money_once` |
| Stored balances equal the sum of entries; `held` equals the sum of active holds | **Derived by triggers**: inserting an entry moves its account's balance and fills in `balance_after`, settling a hold releases its amount, and direct writes to `balance`/`held` are refused (migration 005). `/v1/reconciliation` re-derives both independently | `test_balances_cannot_be_edited_directly`, `test_entries_written_by_hand_still_move_balances_and_reconcile`; the Hypothesis state machine after every step |
| History can't be wiped with `TRUNCATE` either (it skips row triggers) | Statement-level `BEFORE TRUNCATE` triggers, which also fire through `CASCADE` | `test_history_cannot_be_wiped_with_truncate` (4 cases) |
| An account's type and currency never change (changing the type would flip the meaning of its whole balance) | `BEFORE UPDATE OF type, currency` trigger | `test_an_accounts_type_and_currency_are_fixed` |
| Settled holds are final; an active hold keeps its account and amount | Lifecycle trigger on `holds`, plus no `DELETE`/`TRUNCATE` | `test_a_settled_hold_cannot_be_reopened`, `test_holds_cannot_be_deleted` |
| Within an account, entry timestamps never go backwards (point-in-time balances depend on it) | `BEFORE INSERT` trigger stamps `posted_at` itself, never below the account's previous entry, even if the server clock steps back | `test_entry_timestamps_never_go_backwards_within_an_account` |
| `metadata` is a JSON object of at most 4 KB | `CHECK` added `NOT VALID`, then validated in a separate migration so a large table keeps taking writes | `test_metadata_must_be_a_small_json_object` |

## Data model

```mermaid
erDiagram
    accounts ||--o{ entries : "has"
    transactions ||--|{ entries : "2..n balanced"
    transactions |o--o| transactions : "reverses"
    accounts ||--o{ holds : "reserves funds on"
    holds |o--o| transactions : "captured as"

    accounts {
        bigint id PK
        text type "asset | liability | equity | revenue | expense"
        text normal_side "generated: debit or credit"
        char currency
        bigint balance "minor units"
        bigint held "reserved by active holds"
        bigint available "generated: balance - held"
        bool allow_negative
    }
    transactions {
        uuid id PK
        uuid reverses_id UK "at most one reversal"
        timestamptz posted_at
    }
    entries {
        bigint id PK
        uuid transaction_id FK
        bigint account_id FK "with currency"
        text direction "debit | credit"
        bigint amount "> 0"
        bigint balance_after "running balance"
    }
    holds {
        uuid id PK
        bigint account_id FK
        bigint amount
        text status "active | captured | voided | expired"
        timestamptz expires_at "partial index on active"
    }
    idempotency_keys {
        text key PK
        text request_hash
        jsonb response
    }
```

Design choices you can see in the schema ([`migrations/001_ledger.sql`](migrations/001_ledger.sql)):

- **Money is `BIGINT` minor units**, never floats (`0.1 + 0.2 != 0.3`).
- **`balance` is stored, not computed on every read,** so reading it is a primary-key lookup instead of summing the entire history. A trigger on `entries` keeps it in step with every insert, which makes it a cache that can't go stale; reconciliation still re-derives it from scratch as an independent check.
- **`balance_after` on every entry** gives statements a running balance and answers "what was the balance on March 1st?" with one indexed lookup.
- **`clock_timestamp()` instead of `now()`** for `posted_at`. `now()` is the start of the database transaction, which can be earlier than the moment the row lock was acquired. That would let a later entry carry an earlier timestamp and break point-in-time balances.
- **A partial index** (`WHERE status = 'active'`) keeps the hold-expiry scan small no matter how many settled holds pile up.

### Indexes, measured

[`migrations/002_query_indexes.sql`](migrations/002_query_indexes.sql) adds two indexes. I found the slow queries by
reading their plans on a realistic data set rather than guessing, and
[`scripts/index_benchmark.py`](scripts/index_benchmark.py) reproduces the numbers. It loads 1M transactions /
2M entries spread over a year, with one hot merchant account in 20% of them, and runs `EXPLAIN ANALYZE` on the
same data before and after the migration (median of warm runs, PostgreSQL 17 on an Apple M5):

| Query | Before | After |
|---|---|---|
| Balance as of Jan 8, hot account (200k entries) | 112.5 ms: walked the **whole table's primary key** backwards, filtering every row | 0.003 ms: one probe of `(account_id, posted_at, id)` |
| Balance as of Dec 10, hot account | 0.94 ms | 0.002 ms |
| Balance as of Jan 8, typical account (~1.8k entries) | 0.19 ms | 0.002 ms |
| Recent transactions feed (latest 20 of 1M) | 83.6 ms: sequential scan + sort of 1M rows | 0.008 ms: backward scan of `(posted_at, id)` |

The surprise was the first row: Postgres did not use the existing `(account_id, id)` index at all, and scanned
the entire primary key instead. The fix also needed a query change. Within one account, `posted_at` rises with
`id`, because it is taken under the account's row lock. So `ORDER BY posted_at DESC, id DESC` returns the same
row as `ORDER BY id DESC`, and that order matches the new index.

Indexes cost writes, so I re-ran the load test after the migration: spread 3,000–3,130 transfers/s (was
3,050–3,150), hotspot 1,560–1,620/s (was 1,590–1,640). At this data size that is within run-to-run noise.

**Adding indexes to a live ledger.** The migration uses `CREATE INDEX CONCURRENTLY`, which doesn't block writes
but can't run inside a transaction, so the migration runner supports a `-- migration: no-transaction` marker.
That surfaced a real bug: when several app workers booted at once, the ones blocked in `pg_advisory_lock()`
counted as open transactions, and `CREATE INDEX CONCURRENTLY` waits for all open transactions to finish, while
the index builder held the lock they were waiting for. Postgres reported a deadlock and startup failed. The
runner now polls with `pg_try_advisory_lock()`, and
`test_instances_starting_at_the_same_time_migrate_once_without_deadlocking` reproduces the race.

## Concurrency

Every operation locks the account rows it touches with `SELECT ... FOR UPDATE`, **always in ascending
id order**. That ordering is what prevents deadlocks: A→B and B→A transfers at the same time queue up
instead of each holding one lock and waiting for the other (`test_opposite_direction_transfers_do_not_deadlock`).
A balance is checked and changed under the same lock, so no update is lost, and the code doesn't need
`SERIALIZABLE` isolation plus retry loops. Deadlock and serialization errors are still retried
transparently as a safety net.

**The tests found a real bug here.** Ten threads reversing the same transaction always produced exactly
one reversal, but the nine losers reported `INSUFFICIENT_FUNDS` instead of `ALREADY_REVERSED`. They had
passed the "already reversed?" check, waited on the account locks, and by the time they got the locks
the money had already moved back. The fix was to lock the original transaction row first, so reversals
of the same transaction are serialized and later ones see the first.

## Idempotency

Every money-moving endpoint requires an `Idempotency-Key` header. The key is inserted **inside the same
database transaction** as the transfer it protects, so they commit or roll back together. There's no
window where money moved but the key wasn't saved (or the other way round), even if the process
crashes. Two concurrent requests with the same key are serialized by the primary key: the second
`INSERT ... ON CONFLICT DO NOTHING` waits for the first transaction, then returns its stored response with
`Idempotent-Replayed: true`. That wait is capped by `lock_timeout`; if the original request is still running
when it expires, the retry gets `409 IDEMPOTENCY_KEY_IN_USE` (try again shortly) rather than a server error.
Reusing a key with a different body is a `422`. A failed request (say, insufficient funds) is not stored, so it
can be retried with the same key once the account is topped up.

Keys are remembered for 24 hours. A background job deletes expired ones in batches of 5,000. One big `DELETE`
would be all or nothing: Postgres deleted about 1.7M keys per second here, so with a 30-second statement timeout
a backlog of roughly 50M keys (about 14 hours of traffic at 1,000 transfers/s) would time out, roll back, and
fail identically on every later run. Keys are global per operation for now. Tally has no separate API clients
yet; when it does, the client id must become part of the key so one client can never replay another's response.

## Reconciliation

`GET /v1/reconciliation` (or `tally reconcile`, which exits 1 on any finding) re-derives the books from the raw
entries and holds without trusting any stored total. The triggers already enforce every rule on each write,
but triggers can be bypassed: a superuser in `session_replication_role = replica` mode (as bulk loads and some
restore tools use) skips all of them, foreign keys included. So the report re-checks each rule independently:
unbalanced and incomplete transactions, entries in the wrong currency, stored balances and held amounts against
their sources, and every entry's `balance_after` and timestamp order, which statements and point-in-time
balances depend on.

Each check lists up to 100 findings and reports the exact total. Before these checks existed, a benchmark
database loaded past the triggers had 2M wrong running balances, and the report said nothing about them. The
whole report runs in one `REPEATABLE READ` snapshot, so its counts, trial balance and findings all describe the
same moment while transfers keep committing. It has its own 10-minute statement timeout, because the
connection default is sized for requests. On 2M entries it takes about 3.5 s.

## Load test

[`scripts/load_test.py`](scripts/load_test.py) funds 200 wallets, fires 10,000 concurrent transfers in two
patterns, then checks that reconciliation is clean and that the wallets still hold exactly the money
deposited. Measured on an Apple M5 laptop (app with 4 worker processes, Postgres 17, and the load generator
all on the same machine), 100 concurrent clients; throughput over seven runs, latency over the last three:

| Scenario | Throughput | p50 | p99 |
|---|---|---|---|
| **Spread**: random wallet to wallet | 3,200 – 3,560 transfers/s | 27 – 29 ms | 60 – 63 ms |
| **Hotspot**: everyone pays one merchant | 1,570 – 1,820 transfers/s | 27 – 30 ms | 350 – 400 ms |

Every run: 20,000 transfers, reconciliation clean, money conserved. Moving balance updates into the database
(migration 005) made both scenarios faster than before (3,050–3,150 and about 1,580/s), because each transfer
now makes one fewer round trip, and that round trip used to happen while holding the account locks.

**The hotspot is the instructive number.** Spread traffic grows with the number of worker processes (it
roughly doubled going from one worker to four). Hotspot traffic doesn't, because every transfer needs the
merchant's row lock: about 1,600–1,800 transfers/s is that one row's ceiling, and p99 latency is 6x the spread
case as requests queue behind it.
Ways out, depending on the business: split the merchant into N sub-accounts that are summed for
reporting; credit the merchant asynchronously in batches from a settlement queue; or keep the hot balance
in a separate counter and fold it into the ledger periodically. Each one gives up immediate,
per-transaction consistency on that one account.

## API

| Method | Path | |
|---|---|---|
| `POST` | `/v1/accounts` | Open an account (type, currency, `allow_negative`) |
| `GET` | `/v1/accounts`, `/v1/accounts/{id}` | Balance, held and available amounts |
| `GET` | `/v1/accounts/{id}/entries?cursor=` | Statement, newest first, running balance, keyset pagination |
| `GET` | `/v1/accounts/{id}/balance?as_of=` | Balance at any past moment |
| `POST` | `/v1/transfers` 🔑 | Debit one account, credit another |
| `POST` | `/v1/transactions` 🔑 | Any balanced set of 2–100 entries (fees, splits, multi-currency) |
| `GET` | `/v1/transactions`, `/v1/transactions/{id}` | Recent activity; one transaction with its entries |
| `POST` | `/v1/transactions/{id}/reversal` 🔑 | Post the mirror image of a transaction |
| `POST` | `/v1/holds` 🔑 | Reserve funds (card authorization), with expiry |
| `POST` | `/v1/holds/{id}/capture` 🔑 | Settle a hold, optionally for less; the rest is released |
| `POST` | `/v1/holds/{id}/void` | Release a hold |
| `GET` | `/v1/reconciliation` | Re-derive every balance from raw entries and report any mismatch |

🔑 = requires `Idempotency-Key`. Errors are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem details with a stable `code`
(`INSUFFICIENT_FUNDS`, `UNBALANCED`, `ALREADY_REVERSED`, `HOLD_EXPIRED`, `IDEMPOTENCY_KEY_REUSED`, ...).
Interactive docs are at `/docs`.

## Run it

```bash
docker compose up --build -d
docker compose exec app tally demo     # sample accounts and transactions
open http://localhost:8000             # the console; API docs at /docs
```

Without Docker, against your own PostgreSQL (tested on 17):

```bash
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
export TALLY_DATABASE_URL=postgresql://user:pass@localhost:5432/tally
tally migrate && tally demo && tally serve
tally reconcile                        # exit code 1 if anything disagrees
```

| Variable | Default | |
|---|---|---|
| `TALLY_DATABASE_URL` | `postgresql://tally:tally@localhost:5432/tally` | Never printed: `Settings` redacts the password in logs and tracebacks |
| `TALLY_POOL_SIZE` | `20` | Connections per worker process |
| `TALLY_LOCK_TIMEOUT_MS` | `5000` | Give up waiting for a row lock (Postgres `lock_timeout`) |
| `TALLY_STATEMENT_TIMEOUT_MS` | `30000` | Cancel any statement that runs longer (`statement_timeout`) |
| `TALLY_MAINTENANCE_INTERVAL` | `5` | Seconds between hold-expiry / idempotency-cleanup runs |

### Connection handling

Every pooled connection gets `lock_timeout`, `statement_timeout` and `idle_in_transaction_session_timeout`.
Without them, one transaction stuck behind a row lock (an engineer's open psql session, a hung client) waits
forever while holding a pool connection, and enough of those drain the pool. Connections are pinged before
use only if they sat idle for over a second, the trick HikariCP uses. Pinging on every checkout cost about 9%
of transfer throughput in the load test (3,130–3,160 → 2,820–2,880 transfers/s). Skipping recently used
connections brought it back, and still catches connections killed by idle timeouts or a database restart.
The migration runner stores a checksum of each applied file and refuses to start if one was edited afterwards,
because a database that already ran a migration never sees later edits to it.

## Tests

```bash
pytest -q        # starts a throwaway postgres:17 container, or set TALLY_TEST_DATABASE_URL
```

119 tests, all against a real PostgreSQL, because the rules under test live in the database:

| Suite | What it covers |
|---|---|
| `test_transactions.py` (18) | Deposits, multi-leg fees, per-currency balancing, overdrafts, reversals, statements, point-in-time balances, leg validation, entry-by-entry overdraft checks |
| `test_holds.py` (11) | Authorize, partial capture, void, expiry, capture on debit-normal accounts, double settlement, capturing the last of the funds, expiry and amount validation |
| `test_database_invariants.py` (30) | **Raw SQL that bypasses the app** to try to break each rule, and gets rejected |
| `test_concurrency.py` (4) | 50 threads draining one account, opposite-direction transfers, one idempotency key from 20 threads, concurrent reversals |
| `test_api.py` (9) | HTTP contract, idempotent replay, key reuse, problem details, pagination, holds, reconciliation |
| `test_idempotency.py` (8) | Run once and replay, key reuse, per-operation scope, failed requests not remembered, a retry racing the original, a key purged mid-claim, batched purge |
| `test_reconciliation.py` (9) | Damages the ledger past every trigger (replica mode) and checks each kind of damage is reported; capped lists with exact totals; one consistent snapshot while transfers commit |
| `test_migrations.py` (10) | Ordered, idempotent migrations; valid concurrent indexes; the hot queries use them; four instances migrating at once without deadlocking; edited migrations refused |
| `test_db.py` (8), `test_config.py` (11) | Commit/rollback, deadlock retries in fresh transactions, lock timeouts, replacing dropped connections, idle-only pings; config parsing, validation and password redaction |
| `test_invariants.py` | **Model-based testing**: Hypothesis generates 40 random sequences of up to 30 operations (deposits, transfers, holds, captures, voids, reversals). After *every* step it checks that reconciliation is clean, the database matches an in-memory model balance for balance, and no protected account is overdrawn |

To check that the model-based test has teeth, I planted a bug: a partial capture that releases only the
captured amount instead of the whole hold. Hypothesis found a failing sequence in about a second.

CI runs lint and the full suite with Testcontainers, then boots the Docker Compose stack, runs a load test
against it, and runs `tally reconcile` inside the container.

## Project layout

```
migrations/001_ledger.sql   schema, constraints, triggers: the rules of the ledger
migrations/002_*.sql        indexes for the slow queries, built CONCURRENTLY
migrations/003_*, 004_*     hardening: TRUNCATE guard, fixed account identity, hold lifecycle,
                            DB-stamped timestamps, metadata CHECKs (added NOT VALID, then validated)
migrations/005_*            balances and held amounts maintained by triggers; direct writes refused
src/tally/
├── ledger.py               validation, ordered row locks, posting, holds, reversals, statements
├── idempotency.py          idempotency keys in the same transaction as the operation
├── reconciliation.py       re-checks every trigger-enforced rule in one REPEATABLE READ snapshot
├── db.py                   connection pool with timeouts and idle checks, deadlock retry, migration runner
├── api.py, schemas.py      FastAPI routes, Pydantic models, RFC 9457 errors
├── cli.py                  tally migrate | serve | reconcile | demo
└── static/                 the console (plain JS, no build step)
scripts/load_test.py        concurrent load test + conservation check
scripts/index_benchmark.py  EXPLAIN ANALYZE before/after migration 002 on 2M entries
```

## Not built (yet)

- **FX between currencies.** Each currency balances on its own; a conversion would post through FX accounts with a rate.
- **Authentication and multi-tenancy.** This is meant to run as an internal service behind a gateway. Per-tenant isolation would add a `tenant_id` to every table and use row-level security.
- **Scale for very long histories:** partition `entries` by month, archive old partitions, and snapshot balances so point-in-time queries don't touch cold data.
- **Downstream events:** publish `transaction.posted` events through a transactional outbox, so other services learn about money movement without dual writes.

## License

MIT
