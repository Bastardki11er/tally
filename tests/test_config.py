import pytest

from tally.config import Settings, redact

ALL_SET = {
    "TALLY_DATABASE_URL": "postgresql://app:pw@db:5432/ledger",
    "TALLY_POOL_SIZE": "7",
    "TALLY_MAINTENANCE_INTERVAL": "0.5",
    "TALLY_LOCK_TIMEOUT_MS": "100",
    "TALLY_STATEMENT_TIMEOUT_MS": "2000",
}


def test_reads_typed_values_from_the_environment():
    settings = Settings.from_env(ALL_SET)

    assert settings == Settings("postgresql://app:pw@db:5432/ledger", 7, 0.5, 100, 2000)


def test_unset_variables_fall_back_to_the_defaults():
    assert Settings.from_env({}) == Settings()


def test_a_malformed_value_names_the_variable():
    with pytest.raises(ValueError, match="TALLY_POOL_SIZE must be an integer, got 'abc'"):
        Settings.from_env({"TALLY_POOL_SIZE": "abc"})


@pytest.mark.parametrize(
    "variable", ["TALLY_POOL_SIZE", "TALLY_MAINTENANCE_INTERVAL", "TALLY_LOCK_TIMEOUT_MS", "TALLY_STATEMENT_TIMEOUT_MS"]
)
def test_zero_is_rejected_because_it_would_mean_busy_loop_or_wait_forever(variable):
    with pytest.raises(ValueError, match=f"{variable}.*must be positive"):
        Settings.from_env({variable: "0"})


def test_the_password_never_appears_in_repr():
    shown = repr(Settings(database_url="postgresql://tally:s3cret@db:5432/tally"))

    assert "s3cret" not in shown
    assert "postgresql://tally:***@db:5432/tally" in shown


@pytest.mark.parametrize(
    ("conninfo", "expected"),
    [
        ("postgresql://user:pw@host/db", "postgresql://user:***@host/db"),
        ("host=h user=u password=pw dbname=d", "host=h user=u password=*** dbname=d"),
        ("postgresql://host/db", "postgresql://host/db"),
    ],
)
def test_redact(conninfo, expected):
    assert redact(conninfo) == expected
