#!/usr/bin/env python3
"""make migrate — apply pending schema migrations.

Reads SQL files under `migrations/` (numbered `NNN_description.sql`), compares
each file's version against `schema_version` in the live DB, and applies any
pending migrations in a single transaction per file. Backs up `data/main.db`
before the first apply.

Migration file format
---------------------
Each file MUST have a header comment block:

    -- VERSION: 1
    -- DESCRIPTION: short human-readable summary
    -- ROLLBACK: SQL to undo this migration (informational; not auto-run)

The script parses `VERSION:` to decide ordering and what to write to
`schema_version`. The ROLLBACK block is documentation — `make migrate` never
downgrades automatically. Recovering from a bad migration is a manual
operation against the `.bak` file the script writes.

Why one transaction per file
----------------------------
Atomicity. Either a migration applies fully or not at all. If two
migrations are pending, the second only runs after the first commits — so a
mid-flight failure on migration 2 leaves the DB at the migration-1 version,
not somewhere undefined.

Idempotency
-----------
Re-running `make migrate` is a no-op if no migrations are pending. Apply
order is by VERSION number, not filesystem order — accidental rename or
duplicate-number files raise loudly.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"
DEFAULT_DB_PATH = REPO_ROOT / "data" / "main.db"

VERSION_RE = re.compile(r"^--\s*VERSION:\s*(\d+)\s*$", re.MULTILINE)
DESCRIPTION_RE = re.compile(r"^--\s*DESCRIPTION:\s*(.+)$", re.MULTILINE)


@dataclass
class Migration:
    version: int
    description: str
    path: Path
    sql: str


def _db_path() -> Path:
    """Honor AGENTIC_CLIPPER_DB override (used by tests)."""
    import os
    override = os.environ.get("AGENTIC_CLIPPER_DB")
    return Path(override) if override else DEFAULT_DB_PATH


def discover_migrations(migrations_dir: Path | None = None) -> list[Migration]:
    """Find all *.sql files under migrations/, parse headers, sort by version.

    Raises if two files claim the same version (catches accidental copy-paste)
    or if a file is missing the VERSION header (catches forgotten boilerplate).

    `migrations_dir` defaults to `MIGRATIONS_DIR` at call time (not import
    time) — that's what makes `monkeypatch.setattr(migrate, 'MIGRATIONS_DIR',
    tmp_path)` actually work in tests.
    """
    if migrations_dir is None:
        migrations_dir = MIGRATIONS_DIR
    if not migrations_dir.exists():
        return []
    out: list[Migration] = []
    seen_versions: dict[int, Path] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        text = path.read_text()
        ver_match = VERSION_RE.search(text)
        if not ver_match:
            raise ValueError(
                f"{path.name} missing '-- VERSION: N' header. "
                "Migrations without explicit versioning won't apply."
            )
        version = int(ver_match.group(1))
        if version in seen_versions:
            raise ValueError(
                f"Duplicate VERSION {version} in {path.name} and "
                f"{seen_versions[version].name}. Each migration needs a unique number."
            )
        seen_versions[version] = path
        desc_match = DESCRIPTION_RE.search(text)
        description = desc_match.group(1).strip() if desc_match else "(no description)"
        out.append(Migration(version=version, description=description, path=path, sql=text))
    out.sort(key=lambda m: m.version)
    return out


def current_version(db_path: Path) -> int:
    """Read the latest applied version from schema_version. 0 if missing."""
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except sqlite3.OperationalError:
            # schema_version table itself doesn't exist (very early state)
            return 0


def pending(migrations: list[Migration], current: int) -> list[Migration]:
    return [m for m in migrations if m.version > current]


def backup_db(db_path: Path) -> Path | None:
    """Snapshot data/main.db to data/main.db.bak.<ts> before the first apply.

    Codex finding 2026-05-18: under SQLite WAL, `shutil.copy2` only copies
    the main DB file — the WAL file (`-wal` sibling) holds recent committed
    writes that haven't checkpointed yet. The resulting .bak could be
    missing data, making it unusable as a rollback target.

    Fix: use SQLite's online backup API. It walks both the main DB pages
    AND any pending WAL frames, producing a consistent snapshot regardless
    of WAL state, while the source remains open for concurrent reads.

    Returns the backup path, or None if there's no DB to back up yet.
    """
    if not db_path.exists():
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_suffix(f".db.bak.{ts}")
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(backup_path)
        try:
            # pages=-1 streams the full DB in one call; SQLite's backup API
            # handles WAL automatically (per the sqlite3 docs: the backup
            # operation produces a "consistent snapshot" even with active
            # writers on the source).
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return backup_path


def apply_one(db_path: Path, migration: Migration) -> None:
    """Apply a single migration atomically; bump schema_version on success.

    sqlite3.executescript() issues an implicit COMMIT before running, so an
    outer Python-side BEGIN gets swallowed. We embed BEGIN/COMMIT inside the
    script string itself — that way executescript runs the migration body
    AND the version bump as one atomic transaction.

    `migration.version` is an int parsed from `\\d+` regex; no injection risk.
    """
    combined = (
        "BEGIN;\n"
        f"{migration.sql}\n"
        f"INSERT INTO schema_version (version) VALUES ({migration.version});\n"
        "COMMIT;\n"
    )
    with sqlite3.connect(db_path) as conn:
        conn.isolation_level = None  # we manage transactions explicitly
        try:
            conn.executescript(combined)
        except Exception:
            # If the script failed mid-flight, SQLite may have left a partial
            # transaction open. Try to roll back; if there's nothing to roll
            # back (the failure happened post-COMMIT), the OperationalError
            # below is benign.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise


def migrate(db_path: Path | None = None, *, dry_run: bool = False) -> dict:
    """Apply all pending migrations. Returns a summary dict."""
    target = db_path or _db_path()
    migrations = discover_migrations(MIGRATIONS_DIR)
    current = current_version(target)
    todo = pending(migrations, current)

    summary = {
        "db_path": str(target),
        "current_version": current,
        "discovered": [m.version for m in migrations],
        "pending": [m.version for m in todo],
        "applied": [],
        "backup": None,
        "dry_run": dry_run,
    }

    if not todo:
        return summary

    if dry_run:
        return summary

    if target.exists():
        summary["backup"] = str(backup_db(target))

    for m in todo:
        apply_one(target, m)
        summary["applied"].append(m.version)

    summary["current_version"] = current_version(target)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without applying or backing up.",
    )
    args = parser.parse_args()

    try:
        summary = migrate(dry_run=args.dry_run)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"ERROR: SQLite failure during migration: {exc}", file=sys.stderr)
        print("  The .bak file (if any) is your rollback path.", file=sys.stderr)
        return 1

    db = summary["db_path"]
    current = summary["current_version"]
    discovered = summary["discovered"]
    pending_ids = summary["pending"]
    applied = summary["applied"]

    print(f"=== make migrate ({'dry-run' if args.dry_run else 'live'}) ===")
    print(f"  db:          {db}")
    print(f"  current ver: {current}")
    print(f"  discovered:  {discovered or '(none)'}")
    print(f"  pending:     {pending_ids or '(none)'}")
    if summary["backup"]:
        print(f"  backup:      {summary['backup']}")
    if applied:
        print(f"  applied:     {applied}")
        print(f"  new version: {summary['current_version']}")
        print("  doctor's schema check should now pass; re-run `make doctor` to confirm.")
    elif not pending_ids:
        print("  nothing to do — DB is up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
