"""Command line interface.

tally migrate       apply database migrations
tally serve         run the HTTP API (and the demo console at /)
tally reconcile     recompute all balances; exit code 1 if anything disagrees
tally demo          create a small set of demo accounts and transactions
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

from fastapi.encoders import jsonable_encoder

from .config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tally", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply database migrations")
    serve = commands.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--workers", type=int, default=1, help="worker processes")
    commands.add_parser("reconcile", help="verify every balance against the entry history")
    commands.add_parser("demo", help="seed demo accounts and transactions")
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    from .db import Database, migrate

    if args.command == "migrate":
        applied = migrate(settings.database_url)
        print(f"applied {len(applied)} migration(s): {', '.join(applied) or 'none pending'}")
        return 0

    if args.command == "serve":
        import uvicorn

        uvicorn.run("tally.api:create_app", factory=True, host=args.host, port=args.port, workers=args.workers)
        return 0

    db = Database(settings.database_url, min_size=1, max_size=2)
    try:
        if args.command == "reconcile":
            from .reconciliation import reconcile

            report = db.run(reconcile)
            print(json.dumps(jsonable_encoder(report), indent=2))
            return 0 if report["ok"] else 1

        if args.command == "demo":
            migrate(settings.database_url)
            seed_demo(db)
            print("demo data created; open the console at http://localhost:8000/")
            return 0
    finally:
        db.close()
    return 0


def seed_demo(db) -> None:
    """A tiny wallet business: customers deposit cash, pay each other, and the platform takes a fee."""
    from .ledger import Ledger, Leg

    ledger = Ledger()

    def run(fn, *args, **kwargs):
        return db.run(lambda conn: fn(conn, *args, **kwargs))

    cash = run(ledger.create_account, "Bank account (cash)", "asset", "USD")
    fees = run(ledger.create_account, "Fee revenue", "revenue", "USD")
    alice = run(ledger.create_account, "Alice's wallet", "liability", "USD", metadata={"customer": "alice"})
    bob = run(ledger.create_account, "Bob's wallet", "liability", "USD", metadata={"customer": "bob"})
    store = run(ledger.create_account, "Corner Store (merchant)", "liability", "USD", metadata={"merchant": True})

    run(ledger.transfer, cash["id"], alice["id"], 50_000, "Alice deposits $500")
    run(ledger.transfer, cash["id"], bob["id"], 12_000, "Bob deposits $120")
    run(
        ledger.post,
        [Leg(alice["id"], "debit", 2_575), Leg(bob["id"], "credit", 2_500), Leg(fees["id"], "credit", 75)],
        "Alice pays Bob $25 for dinner ($0.75 fee)",
    )
    run(ledger.transfer, bob["id"], cash["id"], 5_000, "Bob withdraws $50")
    mistake = run(ledger.transfer, alice["id"], store["id"], 9_900, "Alice pays Corner Store $99 (wrong amount)")
    run(ledger.reverse, mistake["id"], "Refund: charged the wrong amount")
    run(ledger.transfer, alice["id"], store["id"], 990, "Alice pays Corner Store $9.90")

    hold = run(
        ledger.create_hold, alice["id"], 4_000, datetime.now(UTC) + timedelta(days=3), "Card authorization: gas station"
    )
    run(ledger.capture_hold, hold["id"], store["id"], 3_412, "Gas station: final amount $34.12")
    run(ledger.create_hold, bob["id"], 1_500, datetime.now(UTC) + timedelta(days=1), "Hotel incidentals")


if __name__ == "__main__":
    sys.exit(main())
