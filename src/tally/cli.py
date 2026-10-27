"""Command line interface.

tally migrate       apply database migrations
tally serve         run the HTTP API (and the demo console at /)
tally reconcile     recompute all balances from the raw entries
tally demo          seed demo accounts and transactions into an empty ledger

Exit codes, so scripts and schedulers can tell the cases apart:
  0  done (for reconcile: the books agree)
  1  reconcile found discrepancies
  2  the command couldn't run: bad arguments or configuration, database
     unreachable or not migrated, migration history doesn't match the code,
     or demo asked to seed a ledger that already has accounts
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta

import psycopg
from fastapi.encoders import jsonable_encoder
from psycopg.rows import dict_row

from .config import Settings
from .db import MigrationError, migrate, session_limits
from .ledger import Ledger, Leg
from .reconciliation import reconcile

EXIT_OK, EXIT_DISCREPANCIES, EXIT_ERROR = 0, 1, 2
EXIT_INTERRUPTED = 130  # the shell's convention for "stopped by Ctrl-C"

DEMO_LOCK_ID = 7_301_984_222  # any constant other than the migration lock's


class CommandError(Exception):
    """An expected failure, reported in one line instead of a traceback."""


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)  # bad arguments exit with 2 here
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        return fail(f"configuration: {exc}")
    # Failures an operator can fix get one line. Anything else is a bug and keeps its traceback.
    try:
        return args.handler(args, settings)
    except psycopg.errors.UndefinedTable as exc:
        return fail(f"{exc.diag.message_primary}: has this database been migrated? Run `tally migrate`.")
    except psycopg.Error as exc:
        return fail(f"database: {str(exc).strip()}")
    except (CommandError, MigrationError, TimeoutError) as exc:
        return fail(str(exc))
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


def fail(message: str) -> int:
    print(f"tally: error: {message}", file=sys.stderr)
    return EXIT_ERROR


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tally", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("migrate", help="apply database migrations").set_defaults(handler=migrate_command)

    serve = commands.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: this machine only)")
    serve.add_argument("--port", type=positive_int, default=8000)
    serve.add_argument("--workers", type=positive_int, default=1, help="worker processes")
    serve.set_defaults(handler=serve_command)

    check = commands.add_parser("reconcile", help="verify every balance against the entry history")
    check.add_argument(
        "--timeout", type=positive_int, default=600, metavar="SECONDS", help="give up after this long (default: 600)"
    )
    check.set_defaults(handler=reconcile_command)

    commands.add_parser("demo", help="seed demo data into an empty ledger").set_defaults(handler=demo_command)
    return parser


def connect(settings: Settings) -> psycopg.Connection:
    """One plain connection for a one-shot command.

    Not a pool: a pool connects in the background and keeps retrying, so with the
    database down a command would hang for the pool timeout and then say only
    "couldn't get a connection". A direct connection fails at once, with the reason.
    """
    return psycopg.connect(
        settings.database_url,
        row_factory=dict_row,
        connect_timeout=10,
        options=session_limits(settings.lock_timeout_ms, settings.statement_timeout_ms),
    )


def migrate_command(args: argparse.Namespace, settings: Settings) -> int:
    applied = migrate(settings.database_url)
    print(f"applied {len(applied)} migration(s): {', '.join(applied) or 'none pending'}")
    return EXIT_OK


def serve_command(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn  # only this command needs it

    if args.workers > 1:
        # Each worker process keeps its own metrics; they write them here, and /metrics
        # in any worker reports the sum. Must be set before the workers start.
        os.environ["PROMETHEUS_MULTIPROC_DIR"] = tempfile.mkdtemp(prefix="tally-metrics-")
    # Each worker builds its own app and reads the environment again. main() read it
    # once already, so bad configuration fails here, before any worker starts.
    # The app logs one JSON line per request itself, so uvicorn's own logging is off.
    uvicorn.run(
        "tally.api:serve_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_config=None,
        access_log=False,
    )
    return EXIT_OK


def reconcile_command(args: argparse.Namespace, settings: Settings) -> int:
    with connect(settings) as conn:
        report = reconcile(conn, timeout_seconds=args.timeout)
    # The full report goes to stdout for machines, the verdict to stderr for people.
    print(json.dumps(jsonable_encoder(report), indent=2))
    problems = {check: count for check, count in report["totals"].items() if count}
    if problems:
        found = ", ".join(f"{check}={count}" for check, count in problems.items())
        print(f"reconciliation FAILED: {found}", file=sys.stderr)
        return EXIT_DISCREPANCIES
    print(
        f"reconciliation ok: {report['accounts']} accounts, {report['transactions']} transactions, "
        f"{report['entries']} entries",
        file=sys.stderr,
    )
    return EXIT_OK


def demo_command(args: argparse.Namespace, settings: Settings) -> int:
    migrate(settings.database_url)
    # One transaction: if any step fails, or someone presses Ctrl-C, nothing is left
    # half-seeded, so the command can simply be run again.
    with connect(settings) as conn, conn.transaction():
        # Two demos started at once would both see an empty ledger and both seed it.
        # With the lock, the second waits for the first and then finds its accounts.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (DEMO_LOCK_ID,))
        if conn.execute("SELECT EXISTS (SELECT 1 FROM accounts) AS used").fetchone()["used"]:
            raise CommandError("the ledger already has accounts; demo data only goes into an empty database")
        seed_demo(conn)
    print("demo data created; the console is at / on the API server (http://localhost:8000/ by default)")
    return EXIT_OK


def seed_demo(conn: psycopg.Connection) -> None:
    """A tiny wallet business: customers deposit cash, pay each other, and the platform takes a fee."""
    ledger = Ledger()

    cash = ledger.create_account(conn, "Bank account (cash)", "asset", "USD")
    fees = ledger.create_account(conn, "Fee revenue", "revenue", "USD")
    alice = ledger.create_account(conn, "Alice's wallet", "liability", "USD", metadata={"customer": "alice"})
    bob = ledger.create_account(conn, "Bob's wallet", "liability", "USD", metadata={"customer": "bob"})
    store = ledger.create_account(conn, "Corner Store (merchant)", "liability", "USD", metadata={"merchant": True})

    ledger.transfer(conn, cash["id"], alice["id"], 50_000, "Alice deposits $500")
    ledger.transfer(conn, cash["id"], bob["id"], 12_000, "Bob deposits $120")
    ledger.post(
        conn,
        [Leg(alice["id"], "debit", 2_575), Leg(bob["id"], "credit", 2_500), Leg(fees["id"], "credit", 75)],
        "Alice pays Bob $25 for dinner ($0.75 fee)",
    )
    ledger.transfer(conn, bob["id"], cash["id"], 5_000, "Bob withdraws $50")
    mistake = ledger.transfer(conn, alice["id"], store["id"], 9_900, "Alice pays Corner Store $99 (wrong amount)")
    ledger.reverse(conn, mistake["id"], "Refund: charged the wrong amount")
    ledger.transfer(conn, alice["id"], store["id"], 990, "Alice pays Corner Store $9.90")

    soon = datetime.now(UTC)
    hold = ledger.create_hold(conn, alice["id"], 4_000, soon + timedelta(days=3), "Card authorization: gas station")
    ledger.capture_hold(conn, hold["id"], store["id"], 3_412, "Gas station: final amount $34.12")
    ledger.create_hold(conn, bob["id"], 1_500, soon + timedelta(days=1), "Hotel incidentals")


if __name__ == "__main__":
    sys.exit(main())
