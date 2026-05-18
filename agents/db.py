"""SQLite connection helper. WAL-mode, FK-on, row factory returns dict-like rows."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "main.db"
SCHEMA_PATH = REPO_ROOT / "data" / "schema.sql"


def _db_path() -> Path:
    override = os.environ.get("AGENTIC_CLIPPER_DB")
    return Path(override) if override else DEFAULT_DB_PATH


def init_schema(db_path: Path | None = None) -> None:
    target = db_path or _db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as conn:
        with SCHEMA_PATH.open() as fh:
            conn.executescript(fh.read())


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    # Multiple pipeline agents (Scout, Curator, Visuals, Publisher) hit the same
    # SQLite file concurrently. Without WAL the writers block readers; without
    # busy_timeout concurrent writers get an immediate "database is locked"
    # error. PRAGMA journal_mode is per-database (sticky) but PRAGMA
    # foreign_keys + busy_timeout are per-connection, so set them every time.
    target = db_path or _db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30.0, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")  # ms; matches sqlite3.connect timeout
    conn.execute("PRAGMA synchronous = NORMAL")  # WAL-safe, faster than FULL
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
