-- VERSION: 4
-- DESCRIPTION: cost reservations per Eng review E-2 (check-then-spend race fix)
-- ROLLBACK: ALTER TABLE costs DROP COLUMN status;
--           ALTER TABLE costs DROP COLUMN reservation_id;
--           ALTER TABLE costs DROP COLUMN settled_at;
--           (SQLite recreate-and-copy; see .bak)
--
-- Closes E-2. Today's costs ledger is post-hoc: the agent makes a paid
-- call, then writes a row. The MTD budget check runs BEFORE the call and
-- reads the historical SUM — but two concurrent workers can each pass
-- the check (both see SUM=$48) and both spend, ending at $50+each_call
-- against a $50 cap.
--
-- The fix is reservation-then-settlement:
--   1. Before the paid call, write a pending costs row inside BEGIN
--      IMMEDIATE. The MTD check sums pending + succeeded rows, so a
--      second concurrent worker sees the reservation and bails.
--   2. After the call returns, update the same row to succeeded with
--      the actual cost (often slightly different from the reserved
--      estimate). Or mark it failed and zero out the amount.
--
-- The helper module (agents/costs.py) wraps both in reserve() / settle().
-- Callers (Visuals, Voice's ElevenLabs path, Writer's Anthropic path)
-- never touch the table directly.

-- ------------------------------------------------------------
-- Add status + reservation columns to costs
-- ------------------------------------------------------------
-- Backfill: every existing row is 'succeeded' (pre-reservation system,
-- so retroactively that's their state).
ALTER TABLE costs ADD COLUMN status TEXT NOT NULL DEFAULT 'succeeded'
  CHECK (status IN ('pending', 'succeeded', 'failed'));

-- reservation_id: opaque UUID-ish string the helper generates so the
-- caller can `settle(reservation_id, actual_amount, status)` without
-- needing to remember the integer PK. NULL on pre-existing rows.
ALTER TABLE costs ADD COLUMN reservation_id TEXT;

-- settled_at: UTC timestamp when the reservation was finalized. NULL
-- while pending; populated by settle().
ALTER TABLE costs ADD COLUMN settled_at TEXT;

-- ------------------------------------------------------------
-- Indexes
-- ------------------------------------------------------------
-- The MTD check sums by (category, ts-month). Adding status to that
-- composite is the actual win: pending and succeeded rows both count
-- against the cap; failed rows do not.
CREATE INDEX IF NOT EXISTS idx_costs_category_status_ts
  ON costs (category, status, ts);

-- Settle-by-reservation-id is the hot path during a paid call's lifecycle.
CREATE UNIQUE INDEX IF NOT EXISTS uq_costs_reservation
  ON costs (reservation_id)
  WHERE reservation_id IS NOT NULL;
