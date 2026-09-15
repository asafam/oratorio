"""Connection pool for the orchestration board's Postgres database.

DSN is read from the ORCH_BOARD_DSN environment variable at pool-creation
time, never hardcoded or embedded in a config file that could end up
committed to git.
"""
from __future__ import annotations

import os

from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        dsn = os.environ.get("ORCH_BOARD_DSN")
        if not dsn:
            raise RuntimeError(
                "ORCH_BOARD_DSN is not set. Point it at the orchestration_board "
                "database on the board-host, e.g. "
                "postgresql://orchestration:<password>@<board-host>:5433/orchestration_board"
            )
        _pool = ConnectionPool(dsn, min_size=1, max_size=10, open=True)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
