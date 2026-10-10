"""Runtime settings, read from TALLY_* environment variables."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields

ENV_VARS = {
    "database_url": "TALLY_DATABASE_URL",
    "pool_size": "TALLY_POOL_SIZE",
    "maintenance_interval_seconds": "TALLY_MAINTENANCE_INTERVAL",
    "lock_timeout_ms": "TALLY_LOCK_TIMEOUT_MS",
    "statement_timeout_ms": "TALLY_STATEMENT_TIMEOUT_MS",
}


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql://tally:tally@localhost:5432/tally"
    pool_size: int = 20
    # How often expired holds are released and old idempotency keys purged.
    maintenance_interval_seconds: float = 5.0
    # Upper bounds on waiting, applied to every pooled connection, so one transaction
    # stuck behind a row lock can't hold a connection forever and drain the pool.
    lock_timeout_ms: int = 5_000
    statement_timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        # A zero interval would turn the maintenance loop into a busy loop against the
        # database; a zero timeout would mean "wait forever" to Postgres.
        for name in ("pool_size", "maintenance_interval_seconds", "lock_timeout_ms", "statement_timeout_ms"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} ({ENV_VARS[name]}) must be positive, got {getattr(self, name)!r}")

    def __repr__(self) -> str:
        # Settings end up in logs and tracebacks; never print the database password.
        values = {f.name: getattr(self, f.name) for f in fields(self)}
        values["database_url"] = redact(self.database_url)
        return f"Settings({', '.join(f'{name}={value!r}' for name, value in values.items())})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        def read(name: str, cast: Callable[[str], object]):
            raw = env.get(ENV_VARS[name])
            if raw is None:
                return getattr(cls, name)
            try:
                return cast(raw)
            except ValueError:
                kind = {"int": "an integer", "float": "a number"}.get(cast.__name__, cast.__name__)
                raise ValueError(f"{ENV_VARS[name]} must be {kind}, got {raw!r}") from None

        return cls(
            database_url=read("database_url", str),
            pool_size=read("pool_size", int),
            maintenance_interval_seconds=read("maintenance_interval_seconds", float),
            lock_timeout_ms=read("lock_timeout_ms", int),
            statement_timeout_ms=read("statement_timeout_ms", int),
        )


def redact(url: str) -> str:
    """Hides the password in a URL (``user:secret@host``) or key=value (``password=secret``) conninfo."""
    url = re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", url)
    return re.sub(r"(password\s*=\s*)\S+", r"\1***", url)
