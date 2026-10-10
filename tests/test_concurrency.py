"""Many threads hitting the same accounts at once, against a real Postgres."""

import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tally.db import Database
from tally.errors import LedgerError
from tally.idempotency import run_idempotent

from .conftest import Books


@pytest.fixture
def race(migrated, caplog):
    """``race(n, task)`` runs ``task(books, i)`` on n threads released at once; returns results or error codes.

    Two things keep the race honest:

    * The race gets its own pool, filled with n connections before the start.
      Otherwise the threads mostly queue for connections while a pool grows
      (measured: 18 of 60 ran at once), and the race tests the pool's queue instead
      of the database's locks. A separate pool, closed afterwards, also gives those
      connections back: a resized pool keeps them for 10 minutes, and CI's Postgres
      allows only 100.
    * No transaction may have been retried. db.run retries deadlocks without a word,
      so a lock-ordering bug that deadlocks only now and then would still pass
      (measured: one in six runs of such a bug passed every test here).
    """

    def run(n: int, task) -> list:
        racing = Database(migrated, min_size=n, max_size=n)
        try:
            racing.pool.wait(timeout=30)
            books = Books(racing)
            gate = Barrier(n)

            def attempt(i):
                gate.wait()
                try:
                    return task(books, i)
                except LedgerError as error:
                    return error.code

            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="tally.db"), ThreadPoolExecutor(max_workers=n) as pool:
                results = list(pool.map(attempt, range(n)))
        finally:
            racing.close()
        retried = [r.getMessage() for r in caplog.records if r.name == "tally.db" and "retrying" in r.getMessage()]
        assert not retried, f"{len(retried)} transaction(s) were retried, the first: {retried[0]}"
        return results

    return run


def test_concurrent_withdrawals_never_overdraw(books, wallet, race):
    alice, bob = wallet("alice", funded=200), wallet("bob")

    results = race(50, lambda racer, i: racer.transfer(alice["id"], bob["id"], 10))

    outcomes = Counter("ok" if isinstance(r, dict) else r for r in results)
    assert outcomes == {"ok": 20, "INSUFFICIENT_FUNDS": 30}
    assert books.get_account(alice["id"])["balance"] == 0
    assert books.get_account(bob["id"])["balance"] == 200


def test_opposite_direction_transfers_do_not_deadlock(books, wallet, race):
    # A->B and B->A at the same time is the classic deadlock. Locking accounts in
    # id order means one simply waits for the other: no deadlock, so no retry either.
    alice, bob = wallet("alice", funded=1_000), wallet("bob", funded=1_000)

    def transfer(racer, i):
        source, target = (alice, bob) if i % 2 else (bob, alice)
        return racer.transfer(source["id"], target["id"], 7)

    results = race(60, transfer)

    assert all(isinstance(r, dict) for r in results)
    assert books.get_account(alice["id"])["balance"] == 1_000
    assert books.get_account(bob["id"])["balance"] == 1_000


def test_concurrent_requests_with_one_idempotency_key_move_money_once(books, wallet, race):
    alice, bob = wallet("alice", funded=1_000), wallet("bob")
    payload = {"from": alice["id"], "to": bob["id"], "amount": 100}

    def send(racer, i):
        return racer.db.run(
            lambda conn: run_idempotent(
                conn,
                "transfers",
                "client-retry-42",
                payload,
                lambda: racer.ledger.transfer(conn, alice["id"], bob["id"], 100),
            )
        )

    outcomes = race(20, send)

    assert Counter(o.replayed for o in outcomes) == {True: 19, False: 1}
    assert len({o.body["id"] for o in outcomes}) == 1  # everyone got the same transaction
    assert books.get_account(bob["id"])["balance"] == 100


def test_a_transaction_reversed_concurrently_is_reversed_once(books, wallet, race):
    alice, bob = wallet("alice", funded=1_000), wallet("bob")
    payment = books.transfer(alice["id"], bob["id"], 400)

    results = race(10, lambda racer, i: racer.reverse(payment["id"]))

    assert Counter("ok" if isinstance(r, dict) else r for r in results) == {"ok": 1, "ALREADY_REVERSED": 9}
    assert books.get_account(alice["id"])["balance"] == 1_000
