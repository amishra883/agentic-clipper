-- VERSION: 2
-- DESCRIPTION: smoke-test migration; adds an empty migrations_smoke table and drops it
-- ROLLBACK: DROP TABLE IF EXISTS migrations_smoke;
--
-- This migration proves the make migrate framework works end-to-end without
-- changing any production schema. The baseline `schema_version` already
-- inserts version 1 in data/schema.sql; this file bumps to version 2.
--
-- Day 2's real schema change (Block D: stage leases + artifact_version)
-- will replace this with the actual pipeline_runs table + clip_artifacts
-- columns. Until then, this smoke test verifies the migration engine.

CREATE TABLE IF NOT EXISTS migrations_smoke (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  applied_at  TEXT NOT NULL DEFAULT (datetime('now')),
  note        TEXT NOT NULL DEFAULT 'smoke test migration v2'
);

INSERT INTO migrations_smoke (note) VALUES ('framework verified');
