"""Migration engine tests.

Covers `scripts/migrate.py`: discovery, version comparison, backup-before-apply,
atomic transaction (migration + version-bump in one script), idempotent re-run,
and the failure paths that matter for an operator running `make migrate`.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from agents.db import init_schema
from scripts import migrate as migrate_mod


@pytest.fixture
def fresh_db(monkeypatch):
    """Isolated DB seeded from data/schema.sql (which inserts schema_version=1)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        yield db_path


@pytest.fixture
def tmp_migrations_dir(monkeypatch, tmp_path):
    """Point migrate's MIGRATIONS_DIR at a writable temp dir per-test."""
    mig = tmp_path / "migrations"
    mig.mkdir()
    monkeypatch.setattr(migrate_mod, "MIGRATIONS_DIR", mig)
    return mig


def _write_migration(mig_dir: Path, version: int, body: str = "") -> Path:
    path = mig_dir / f"{version:03d}_test.sql"
    path.write_text(
        f"-- VERSION: {version}\n"
        f"-- DESCRIPTION: test migration v{version}\n"
        f"{body}\n"
    )
    return path


# ---------- discover_migrations ----------

def test_discover_empty_dir_returns_empty(tmp_migrations_dir):
    assert migrate_mod.discover_migrations(tmp_migrations_dir) == []


def test_discover_returns_sorted_by_version(tmp_migrations_dir):
    _write_migration(tmp_migrations_dir, 3)
    _write_migration(tmp_migrations_dir, 2)
    _write_migration(tmp_migrations_dir, 5)
    out = migrate_mod.discover_migrations(tmp_migrations_dir)
    assert [m.version for m in out] == [2, 3, 5]


def test_discover_rejects_duplicate_version(tmp_migrations_dir):
    _write_migration(tmp_migrations_dir, 2)
    # Hand-write a second file with the same VERSION header
    (tmp_migrations_dir / "002b_duplicate.sql").write_text(
        "-- VERSION: 2\n-- DESCRIPTION: duplicate\n"
    )
    with pytest.raises(ValueError, match="Duplicate VERSION 2"):
        migrate_mod.discover_migrations(tmp_migrations_dir)


def test_discover_rejects_missing_version_header(tmp_migrations_dir):
    (tmp_migrations_dir / "001_bad.sql").write_text("-- no version here\nCREATE TABLE foo (id INTEGER);\n")
    with pytest.raises(ValueError, match="missing '-- VERSION: N'"):
        migrate_mod.discover_migrations(tmp_migrations_dir)


# ---------- current_version + pending ----------

def test_current_version_reads_max(fresh_db):
    """Fresh DB from init_schema has version=1 baseline."""
    assert migrate_mod.current_version(fresh_db) == 1


def test_current_version_zero_when_db_absent(tmp_path):
    nonexistent = tmp_path / "nothing.db"
    assert migrate_mod.current_version(nonexistent) == 0


def test_pending_excludes_already_applied():
    migrations = [
        migrate_mod.Migration(version=1, description="", path=Path("x"), sql=""),
        migrate_mod.Migration(version=2, description="", path=Path("x"), sql=""),
        migrate_mod.Migration(version=3, description="", path=Path("x"), sql=""),
    ]
    assert [m.version for m in migrate_mod.pending(migrations, current=2)] == [3]


# ---------- backup_db ----------

def test_backup_creates_timestamped_copy(fresh_db):
    """Backup must produce a valid SQLite DB containing the same schema +
    rows as the source. Byte-equality is NOT required (the SQLite online
    backup API produces a fresh page layout, intentionally — that's what
    makes it WAL-safe vs `shutil.copy2`)."""
    backup = migrate_mod.backup_db(fresh_db)
    assert backup is not None
    assert backup.exists()
    assert ".bak." in backup.name

    # Open both as SQLite and verify equivalent schema_version contents
    with sqlite3.connect(fresh_db) as src_conn:
        src_rows = src_conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
    with sqlite3.connect(backup) as bak_conn:
        bak_rows = bak_conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
    assert src_rows == bak_rows, "backup schema_version differs from source"

    # Verify all tables present in source are present in backup
    with sqlite3.connect(fresh_db) as src_conn:
        src_tables = {r[0] for r in src_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    with sqlite3.connect(backup) as bak_conn:
        bak_tables = {r[0] for r in bak_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert src_tables == bak_tables, (
        f"backup tables differ: missing {src_tables - bak_tables}, "
        f"extra {bak_tables - src_tables}"
    )


def test_backup_returns_none_when_no_db(tmp_path):
    assert migrate_mod.backup_db(tmp_path / "nothing.db") is None


# ---------- migrate() end-to-end ----------

def test_migrate_applies_pending_and_bumps_version(fresh_db, tmp_migrations_dir):
    _write_migration(
        tmp_migrations_dir,
        2,
        "CREATE TABLE test_v2 (id INTEGER PRIMARY KEY); INSERT INTO test_v2 (id) VALUES (1);",
    )
    summary = migrate_mod.migrate(db_path=fresh_db)
    assert summary["applied"] == [2]
    assert summary["current_version"] == 2
    # Both the schema change and the version bump took effect
    with sqlite3.connect(fresh_db) as conn:
        row = conn.execute("SELECT id FROM test_v2").fetchone()
        assert row[0] == 1
        ver = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert ver == 2


def test_migrate_is_idempotent(fresh_db, tmp_migrations_dir):
    _write_migration(
        tmp_migrations_dir, 2, "CREATE TABLE idempotent_test (id INTEGER);"
    )
    migrate_mod.migrate(db_path=fresh_db)
    # Second call should be a no-op
    summary = migrate_mod.migrate(db_path=fresh_db)
    assert summary["applied"] == []
    assert summary["pending"] == []


def test_migrate_dry_run_does_not_apply(fresh_db, tmp_migrations_dir):
    _write_migration(
        tmp_migrations_dir, 2, "CREATE TABLE should_not_exist (id INTEGER);"
    )
    summary = migrate_mod.migrate(db_path=fresh_db, dry_run=True)
    assert summary["applied"] == []
    assert summary["pending"] == [2]
    assert summary["backup"] is None
    # Table was NOT created
    with sqlite3.connect(fresh_db) as conn:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            conn.execute("SELECT * FROM should_not_exist")


def test_migrate_rolls_back_on_failure(fresh_db, tmp_migrations_dir):
    """If a migration's SQL errors, neither the partial change nor the
    version bump should land. The DB must be at its pre-migration state."""
    _write_migration(
        tmp_migrations_dir,
        2,
        # First statement creates a table; second references a non-existent
        # table — SQLite raises before commit. (Type names like NOT_A_TYPE
        # are accepted by SQLite's type-affinity rules, so we use a no-such-
        # table reference instead — guaranteed-fatal.)
        "CREATE TABLE first_table (id INTEGER); INSERT INTO nonexistent_table VALUES (1);",
    )
    with pytest.raises(sqlite3.OperationalError):
        migrate_mod.migrate(db_path=fresh_db)
    # Version is still 1 (unchanged); the first table was NOT created
    with sqlite3.connect(fresh_db) as conn:
        ver = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert ver == 1
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            conn.execute("SELECT * FROM first_table")


def test_migrate_creates_backup_before_apply(fresh_db, tmp_migrations_dir):
    _write_migration(tmp_migrations_dir, 2, "CREATE TABLE backup_check (id INTEGER);")
    summary = migrate_mod.migrate(db_path=fresh_db)
    assert summary["backup"] is not None
    backup_path = Path(summary["backup"])
    assert backup_path.exists()
    # Backup is from BEFORE the migration — should not have the new table
    with sqlite3.connect(backup_path) as conn:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            conn.execute("SELECT * FROM backup_check")


def test_migrate_applies_multiple_in_order(fresh_db, tmp_migrations_dir):
    _write_migration(tmp_migrations_dir, 3, "CREATE TABLE table_v3 (id INTEGER);")
    _write_migration(tmp_migrations_dir, 2, "CREATE TABLE table_v2 (id INTEGER);")
    summary = migrate_mod.migrate(db_path=fresh_db)
    # Applied in version order, not filesystem order
    assert summary["applied"] == [2, 3]
    with sqlite3.connect(fresh_db) as conn:
        # Both tables exist
        conn.execute("SELECT * FROM table_v2")
        conn.execute("SELECT * FROM table_v3")
        # Both versions recorded
        versions = [r[0] for r in conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()]
        assert versions == [1, 2, 3]
