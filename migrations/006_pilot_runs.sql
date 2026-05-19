-- VERSION: 7
-- DESCRIPTION: Day 14 validation pilot gate. Adds `pilot_runs` (one row per
--              pilot attempt) and `pilot_revenue` (operator-entered revenue
--              line items scoped to a pilot). Gate criteria thresholds live
--              on the pilot_runs row so a future pilot can be re-evaluated
--              against its own (possibly tighter) thresholds.
-- ROLLBACK:    DROP TABLE pilot_revenue; DROP TABLE pilot_runs;
--
-- See docs/phase2_plan.md line 755 — "Day 14 Validation gate: 30-clip pilot,
-- posts to single platform, measures Content ID claim rate + RPV +
-- operator time. <2% claim rate AND >$0.001 RPV AND <45min/day → unblock
-- Day 15."

CREATE TABLE IF NOT EXISTS pilot_runs (
  id                          INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at                  TEXT NOT NULL DEFAULT (datetime('now')),
  ended_at                    TEXT,
  target_platform             TEXT NOT NULL
                                CHECK (target_platform IN ('instagram_reels','youtube_shorts','tiktok')),
  target_clip_count           INTEGER NOT NULL CHECK (target_clip_count > 0),
  claim_threshold_pct         REAL NOT NULL CHECK (claim_threshold_pct >= 0),
  rpv_threshold_usd           REAL NOT NULL CHECK (rpv_threshold_usd >= 0),
  operator_minutes_threshold  INTEGER NOT NULL CHECK (operator_minutes_threshold > 0),
  status                      TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active','passed','failed','abandoned')),
  verdict_at                  TEXT,
  failed_reasons_json         TEXT,  -- JSON array of strings ('claim_rate','rpv','operator_time')
  notes                       TEXT
);

-- At most one active pilot at a time. Closing the pilot (status != 'active')
-- frees the slot for the next one. Enforced by partial unique index rather
-- than CHECK so the WHERE clause excludes terminal-state rows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pilot_runs_active
  ON pilot_runs (status) WHERE status = 'active';

-- ===========================================================================
-- pilot_revenue — operator-entered revenue line items, scoped to a pilot.
-- Phase 2 has no automatic revenue ingestion (YT/IG/TikTok pay out weekly
-- to monthly via dashboards, not real-time APIs at our scale). Operator
-- records what they see in their creator dashboards.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS pilot_revenue (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  pilot_run_id    INTEGER NOT NULL REFERENCES pilot_runs(id) ON DELETE CASCADE,
  recorded_at     TEXT NOT NULL DEFAULT (datetime('now')),
  amount_usd      REAL NOT NULL CHECK (amount_usd >= 0),
  source          TEXT NOT NULL,  -- ad_rev | affiliate | creator_fund | other
  detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_pilot_revenue_run
  ON pilot_revenue (pilot_run_id, recorded_at);
