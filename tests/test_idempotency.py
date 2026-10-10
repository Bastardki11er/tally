"""run_idempotent and purge_expired, without HTTP in the way."""

import time

import psycopg
import pytest

from tally.db import Database
from tally.errors import LedgerError
from tally.idempotency import purge_expired, request_fingerprint, run_idempotent


class Counter:
    """An operation that records how often it actually ran."""

    def __init__(self, result=None, error=None):
        self.calls = 0
        self.result = result if result is not None else {"id": "txn-1", "amount": 100}
        self.error = error

    def __call__(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def send(db, key, payload, operation, scope="transfers"):
    return db.run(lambda conn: run_idempotent(conn, scope, key, payload, operation))


def test_the_operation_runs_once_and_retries_replay_its_response(db):
    operation = Counter()

    first = send(db, "k1", {"amount": 100}, operation)
    second = send(db, "k1", {"amount": 100}, operation)

    assert operation.calls == 1
    assert (first.replayed, second.replayed) == (False, True)
    assert second.body == first.body == {"id": "txn-1", "amount": 100}
    assert second.status_code == first.status_code == 201


def test_a_key_reused_for_a_different_request_is_rejected_without_running_it(db):
    send(db, "k1", {"amount": 100}, Counter())
    operation = Counter()

    with pytest.raises(LedgerError) as error:
        send(db, "k1", {"amount": 999}, operation)

    assert error.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert operation.calls == 0


def test_the_same_key_in_different_operations_is_independent(db):
    transfer, hold = Counter(), Counter()

    send(db, "k1", {"amount": 100}, transfer, scope="transfers")
    send(db, "k1", {"amount": 100}, hold, scope="holds")

    assert (transfer.calls, hold.calls) == (1, 1)


def test_a_failed_request_is_not_remembered_so_it_can_be_retried(db):
    with pytest.raises(LedgerError):
        send(db, "k1", {"amount": 100}, Counter(error=LedgerError(409, "INSUFFICIENT_FUNDS", "no")))
    operation = Counter()

    retried = send(db, "k1", {"amount": 100}, operation)

    assert operation.calls == 1
    assert retried.replayed is False


def test_fingerprints_ignore_key_order():
    assert request_fingerprint({"a": 1, "b": [1, 2]}) == request_fingerprint({"b": [1, 2], "a": 1})
    assert request_fingerprint({"a": 1}) != request_fingerprint({"a": 2})


def test_a_retry_while_the_original_is_still_running_gets_a_clear_conflict(migrated):
    database = Database(migrated, min_size=1, max_size=1, lock_timeout_ms=300)
    try:
        with psycopg.connect(migrated) as original:  # claimed the key, not committed yet
            original.execute("INSERT INTO idempotency_keys (key, request_hash) VALUES ('transfers:k1', 'x')")
            started = time.monotonic()

            with pytest.raises(LedgerError) as error:
                send(database, "k1", {"amount": 100}, Counter())

            assert error.value.code == "IDEMPOTENCY_KEY_IN_USE"
            assert error.value.status == 409
            assert time.monotonic() - started < 2
    finally:
        database.close()


def test_a_key_purged_between_insert_and_select_is_claimed_afresh():
    # Script the database's answers for that interleaving: INSERT finds a conflict,
    # the purge job deletes the row, SELECT finds nothing, the second INSERT wins.
    class Result:
        def __init__(self, row=None, rowcount=0):
            self.row, self.rowcount = row, rowcount

        def fetchone(self):
            return self.row

    class ScriptedConnection:
        def __init__(self):
            self.answers = [Result(None), Result(None), Result({"key": "transfers:k1"}), Result(rowcount=1)]
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append(sql.split()[0])
            return self.answers.pop(0)

    conn = ScriptedConnection()
    operation = Counter()

    outcome = run_idempotent(conn, "transfers", "k1", {"amount": 100}, operation)

    assert conn.statements == ["INSERT", "SELECT", "INSERT", "UPDATE"]
    assert operation.calls == 1
    assert outcome.replayed is False


def test_purge_deletes_expired_keys_in_batches_and_keeps_recent_ones(db):
    def seed(conn):
        conn.execute(
            "INSERT INTO idempotency_keys (key, request_hash, created_at) "
            "SELECT 'transfers:old-' || g, 'x', now() - interval '25 hours' FROM generate_series(1, 5) g"
        )
        conn.execute("INSERT INTO idempotency_keys (key, request_hash) VALUES ('transfers:fresh', 'x')")

    db.run(seed)

    batches = []
    while (deleted := db.run(lambda conn: purge_expired(conn, batch_size=2))) > 0:
        batches.append(deleted)

    assert batches == [2, 2, 1]
    remaining = db.run(lambda conn: [r["key"] for r in conn.execute("SELECT key FROM idempotency_keys")])
    assert remaining == ["transfers:fresh"]
