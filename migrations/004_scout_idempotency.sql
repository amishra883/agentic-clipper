-- VERSION: 5
-- DESCRIPTION: Scout idempotency (E-5) — UNIQUE on source_url + composite (creator, source_platform)
-- ROLLBACK: DROP INDEX IF EXISTS uq_candidate_source_url;
--           DROP INDEX IF EXISTS idx_candidate_creator_platform;
--
-- Closes E-5 from /autoplan Phase 3 Eng review. The old Scout used a
-- minute-precision timestamp in the clip_id hash, so two scout runs in
-- the same minute produced different ids for the same source_url
-- (duplicates), and two runs straddling a minute boundary also
-- produced duplicates. INSERT OR IGNORE on the id was therefore useless
-- as a dedupe gate.
--
-- The fix is two changes:
--   1. Scout no longer puts the timestamp in clip_id; it derives id
--      stably from (platform, sha256(source_url)).
--   2. This migration enforces UNIQUE(source_url) as the structural
--      guarantee. A future Scout bug that produces a colliding id
--      will fail loudly at INSERT instead of silently double-inserting
--      under a fresh id.
--
-- Backfill: existing rows are assumed to already have unique source_urls.
-- If duplicates exist, this migration will fail at index creation. That's
-- the desired behavior — the operator must reconcile manually.

-- Hard guarantee: one row per source_url across the entire candidates table.
CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_source_url
  ON clips_candidate (source_url);

-- Common lookup the future "stale creator" query needs (fresh-source alert).
CREATE INDEX IF NOT EXISTS idx_candidate_creator_platform
  ON clips_candidate (creator, source_platform, scouted_at DESC);
