-- VERSION: 6
-- DESCRIPTION: composite indexes for seedance MTD scans (E-24) and costs daily-cap scans
-- ROLLBACK: DROP INDEX IF EXISTS idx_seedance_tier_status_ts;
--           DROP INDEX IF EXISTS idx_costs_category_status_ts;
--
-- Codex Eng review E-24: the Visuals agent runs MTD-aggregate queries
-- like:
--
--     SELECT SUM(cost_usd) FROM seedance_generations
--      WHERE tier = ? AND status = 'succeeded'
--        AND strftime('%Y-%m', ts) = strftime('%Y-%m', 'now')
--
-- before every generation to enforce the line-item budget cap. With
-- only single-column indexes on (clip_id) and (ts), the query degrades
-- to a full table scan filtered by `tier` and `status`. At 7 clips/day
-- and ~3 shots/clip that's ~630 rows/mo, but the doctor + analyst
-- agents also touch this table for digest aggregates — and the table
-- never deletes, so within a year the scan cost becomes noticeable on
-- low-end hardware.
--
-- The composite index covers the WHERE clauses Visuals runs:
--   (tier, status, ts) — most-selective columns first, then time
--   for range/strftime filtering.
--
-- Effect: SQLite uses the index to seek directly to the (tier, status)
-- partition and then scans only the timestamp range within it.

CREATE INDEX IF NOT EXISTS idx_seedance_tier_status_ts
  ON seedance_generations (tier, status, ts);

-- Also add an index on the cost-reservation ledger's category for the
-- daily-cap MTD scan in agents/costs.py.reserve(). The existing schema
-- has idx_costs_ts (ts only) and idx_costs_category (category only),
-- but the daily-cap query filters on BOTH plus ts.
CREATE INDEX IF NOT EXISTS idx_costs_category_status_ts
  ON costs (category, status, ts);
