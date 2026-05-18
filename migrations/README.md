# Schema Migrations

Every change to `data/schema.sql` after the v1 baseline ships as a numbered migration here. The migration engine lives in `scripts/migrate.py`; the operator runs it via `make migrate`.

## File naming

`NNN_short_description.sql` where `NNN` is the version number (zero-padded for sort, but the engine parses the `-- VERSION: N` header — filename is for humans only). Examples:

- `001_smoke_test.sql` — verifies the migration framework end-to-end
- `002_artifact_version.sql` — Day 2 stage-lease schema change (planned)

## File format

Every migration file MUST have this header:

```sql
-- VERSION: N
-- DESCRIPTION: one-line human summary
-- ROLLBACK: SQL needed to undo this migration (informational; not auto-applied)
```

Then the migration SQL. Each file is applied in a single transaction. If it fails, the transaction rolls back and `schema_version` is unchanged. The `make migrate` script also writes a timestamped `data/main.db.bak.<ts>` before the first apply per run — that's your manual rollback path.

## Why no auto-downgrade

Downgrades are a footgun for a single-operator project with no staging environment. The `ROLLBACK:` header is documentation only. If a migration ships and is wrong, the operator:

1. Reverts the code commit that added the migration file.
2. Restores `data/main.db.bak.<ts>` over `data/main.db`.
3. Re-runs `make migrate` to confirm `schema_version` matches the rolled-back code.

The `.bak` file is the safety net. Treat the `schema_version` table as append-only.

## Running

```bash
make migrate              # apply all pending
python3 scripts/migrate.py --dry-run    # show what would happen
```

The doctor (`scripts/doctor.py`) checks that `schema_version.version` matches the highest-numbered migration file. If they diverge, doctor fails with the exact command to run.
