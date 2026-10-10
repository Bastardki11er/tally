from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql://tally:tally@localhost:5432/tally"
    pool_size: int = 20
    # How often expired holds are released and old idempotency keys purged.
    maintenance_interval_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            database_url=env.get("TALLY_DATABASE_URL", cls.database_url),
            pool_size=int(env.get("TALLY_POOL_SIZE", cls.pool_size)),
            maintenance_interval_seconds=float(env.get("TALLY_MAINTENANCE_INTERVAL", cls.maintenance_interval_seconds)),
        )
