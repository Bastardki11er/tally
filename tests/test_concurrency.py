"""Many threads hitting the same accounts at once, against a real Postgres."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from tally.errors import LedgerError
from tally.idempotency import run_idempotent


def race(n: int, task) -> list:
    """Runs ``task(i)`` on n threads released at the same instant; returns results or error codes."""
    gate = Barrier(n)

    def run(i):
        gate.wait()
        try:
            return task(i)
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


def test_concurrent_withdrawals_never_overdraw(books, wallet):
    alice, bob = wallet("alice", funded=200), wallet("bob")

    results = race(50, lambda i: books.transfer(alice["id"], bob["id"], 10))

    outcomes = Counter("ok" if isinstance(r, dict) else r for r in results)
    assert outcomes == {"ok": 20, "INSUFFICIENT_FUNDS": 30}
    assert books.get_account(alice["id"])["balance"] == 0
    assert books.get_account(bob["id"])["balance"] == 200


def test_opposite_direction_transfers_do_not_deadlock(books, wallet):
    # A->B and B->A at the same time is the classic deadlock. Locking accounts in
    # id order means one simply waits for the other.
    alice, bob = wallet("alice", funded=1_000), wallet("bob", funded=1_000)

    def transfer(i):
        source, target = (alice, bob) if i % 2 else (bob, alice)
        return books.transfer(source["id"], target["id"], 7)

    results = race(60, transfer)

    assert all(isinstance(r, dict) for r in results)
    assert books.get_account(alice["id"])["balance"] == 1_000
    assert books.get_account(bob["id"])["balance"] == 1_000


def test_concurrent_requests_with_one_idempotency_key_move_money_once(books, db, wallet):
    alice, bob = wallet("alice", funded=1_000), wallet("bob")
    payload = {"from": alice["id"], "to": bob["id"], "amount": 100}

    def send(i):
        return db.run(
            lambda conn: run_idempotent(
                conn,
                "transfers",
                "client-retry-42",
                payload,
                lambda: books.ledger.transfer(conn, alice["id"], bob["id"], 100),
            )
        )

    outcomes = race(20, send)

    assert Counter(o.replayed for o in outcomes) == {True: 19, False: 1}
    assert len({o.body["id"] for o in outcomes}) == 1  # everyone got the same transaction
    assert books.get_account(bob["id"])["balance"] == 100


def test_a_transaction_reversed_concurrently_is_reversed_once(books, wallet):
    alice, bob = wallet("alice", funded=1_000), wallet("bob")
    payment = books.transfer(alice["id"], bob["id"], 400)

    results = race(10, lambda i: books.reverse(payment["id"]))

    assert Counter("ok" if isinstance(r, dict) else r for r in results) == {"ok": 1, "ALREADY_REVERSED": 9}
    assert books.get_account(alice["id"])["balance"] == 1_000
