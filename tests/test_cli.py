"""The command line: exit codes a scheduler can act on, and a demo seed that's safe to rerun."""

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
import uvicorn

from tally import cli
from tally.cli import EXIT_DISCREPANCIES, EXIT_ERROR, EXIT_INTERRUPTED, EXIT_OK, main
from tally.ledger import Ledger


@pytest.fixture(autouse=True)
def env(monkeypatch, migrated):
    monkeypatch.setenv("TALLY_DATABASE_URL", migrated)


@pytest.fixture
def fresh_database(migrated, monkeypatch):
    """An empty database of its own, for tests that need one without migrations."""
    name = f"tally_cli_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(migrated, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    url = migrated.rsplit("/", 1)[0] + f"/{name}"
    monkeypatch.setenv("TALLY_DATABASE_URL", url)
    yield url
    with psycopg.connect(migrated, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE {name} WITH (FORCE)")


def count_accounts(db) -> int:
    return db.run(lambda conn: conn.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"])


# --- migrate ------------------------------------------------------------------------


def test_migrate_reports_nothing_pending_on_a_current_database(capsys):
    assert main(["migrate"]) == EXIT_OK
    assert "none pending" in capsys.readouterr().out


def test_an_edited_migration_is_one_line_and_exit_2(fresh_database, capsys):
    assert main(["migrate"]) == EXIT_OK
    with psycopg.connect(fresh_database, autocommit=True) as conn:
        conn.execute("UPDATE schema_migrations SET checksum = 'edited' WHERE version = '001_ledger'")

    assert main(["migrate"]) == EXIT_ERROR
    assert capsys.readouterr().err.startswith("tally: error: migration 001_ledger was changed")


# --- reconcile ----------------------------------------------------------------------


def test_reconcile_exits_0_with_the_report_on_stdout(capsys, books, wallet):
    wallet("alice", funded=500)

    assert main(["reconcile"]) == EXIT_OK
    out, err = capsys.readouterr()
    assert json.loads(out)["ok"] is True
    assert err.startswith("reconciliation ok: 2 accounts, 1 transactions")


def test_reconcile_exits_1_only_when_the_books_disagree(capsys, db, wallet):
    alice = wallet("alice", funded=500)

    def damage(conn):
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute("UPDATE accounts SET balance = balance + 1 WHERE id = %s", (alice["id"],))

    db.run(damage)

    assert main(["reconcile"]) == EXIT_DISCREPANCIES
    out, err = capsys.readouterr()
    assert json.loads(out)["balance_mismatches"][0]["account_id"] == alice["id"]
    assert "balance_mismatches=1" in err


def test_an_unreachable_database_is_exit_2_at_once_not_1(monkeypatch, capsys):
    # Exit 1 means "the books are wrong". A scheduler must not raise that alarm
    # because the database was down, and shouldn't wait out a pool timeout to learn it.
    monkeypatch.setenv("TALLY_DATABASE_URL", "postgresql://tally:tally@127.0.0.1:1/tally")

    started = time.monotonic()
    assert main(["reconcile"]) == EXIT_ERROR
    assert time.monotonic() - started < 5
    assert capsys.readouterr().err.startswith("tally: error: database: connection failed")


def test_an_unmigrated_database_says_to_migrate(fresh_database, capsys):
    assert main(["reconcile"]) == EXIT_ERROR
    assert "Run `tally migrate`" in capsys.readouterr().err


# --- demo ---------------------------------------------------------------------------


def test_demo_seeds_a_ledger_that_reconciles(db, capsys):
    assert main(["demo"]) == EXIT_OK
    assert main(["reconcile"]) == EXIT_OK

    report = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert (report["accounts"], report["transactions"], report["entries"]) == (5, 8, 17)


def test_demo_refuses_a_ledger_that_already_has_accounts(db, books, capsys):
    books.create_account("Real customer", "liability", "USD")

    assert main(["demo"]) == EXIT_ERROR
    assert "already has accounts" in capsys.readouterr().err
    assert count_accounts(db) == 1


def test_demo_is_all_or_nothing(db, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("crashed halfway through the seed")

    monkeypatch.setattr(Ledger, "capture_hold", fail)

    with pytest.raises(RuntimeError):  # a bug, so it keeps its traceback
        main(["demo"])
    assert count_accounts(db) == 0


def test_two_demos_at_once_seed_only_once(db):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(lambda _: main(["demo"]), range(2)))

    assert results == [EXIT_OK, EXIT_ERROR]
    assert count_accounts(db) == 5


# --- serve, configuration, interrupts -----------------------------------------------


def test_serve_hands_its_options_to_uvicorn(monkeypatch):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: calls.append((app, options)))
    # setenv records that the variable was absent, so the value serve sets is removed afterwards.
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", "")

    assert main(["serve", "--port", "9000", "--workers", "3"]) == EXIT_OK
    options = {
        "factory": True,
        "host": "127.0.0.1",
        "port": 9000,
        "workers": 3,
        "log_config": None,
        "access_log": False,
    }
    assert calls == [("tally.api:serve_app", options)]
    # Several worker processes: their metrics are summed through a shared directory.
    assert os.path.isdir(os.environ["PROMETHEUS_MULTIPROC_DIR"])


@pytest.mark.parametrize(
    "argv", [["serve", "--workers", "0"], ["serve", "--port", "-1"], ["reconcile", "--timeout", "x"]]
)
def test_bad_arguments_are_usage_errors(argv):
    with pytest.raises(SystemExit) as exit_:
        main(argv)
    assert exit_.value.code == EXIT_ERROR


def test_bad_configuration_is_one_line_before_anything_runs(monkeypatch, capsys):
    monkeypatch.setenv("TALLY_POOL_SIZE", "abc")
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(True))

    assert main(["serve"]) == EXIT_ERROR
    assert capsys.readouterr().err == "tally: error: configuration: TALLY_POOL_SIZE must be an integer, got 'abc'\n"
    assert started == []


def test_ctrl_c_exits_130_without_a_traceback(monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "migrate", interrupted)

    assert main(["migrate"]) == EXIT_INTERRUPTED
