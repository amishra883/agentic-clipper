-- VERSION: 3
-- DESCRIPTION: stage leases + artifact_version per Eng review E-1 (race-condition fix)
-- ROLLBACK: DROP TABLE IF EXISTS pipeline_runs;
--           ALTER TABLE clip_artifacts DROP COLUMN artifact_version;
--           (SQLite ALTER DROP requires recreate-and-copy; see backup .bak file)
--
-- Closes E-1 from /autoplan Phase 3 Eng review. Multiple pipeline stages
-- (Editor, Writer, Voice, Visuals, Compositor) all upsert clip_artifacts
-- with no claim mechanism. Two workers running the same stage on the same
-- clip can tear writes; a stage running on a clip whose upstream artifact
-- changed between read and write produces semantically-wrong output.
--
-- The fix is two changes:
--   1. clip_artifacts.artifact_version INTEGER tracks input-version-at-write
--      so a stage can detect "the input I read was from version N; my
--      output is at version N+1; if anyone else also wrote N+1, that's
--      the race I need to detect."
--   2. pipeline_runs is a lease table. A stage claims a (clip, stage)
--      tuple with a deadline; concurrent workers see the active lease
--      and back off. Expired leases get swept by a janitor (out of scope
--      for this migration; lands in the orchestrator code).
--
-- The Day 2 helper module (agents/stage_lease.py) wraps both pieces in
-- a `with stage_lease(clip_id, stage)` context manager. Stages themselves
-- never touch these tables directly.

-- ------------------------------------------------------------
-- Add artifact_version to clip_artifacts
-- ------------------------------------------------------------
-- Default 1 for backfill: every existing row starts at version 1. Stages
-- writing fresh artifacts increment by 1. Conditional updates check that
-- the version they read matches the version they're overwriting (else
-- ROLLBACK; another worker beat us).
ALTER TABLE clip_artifacts ADD COLUMN artifact_version INTEGER NOT NULL DEFAULT 1;

-- ------------------------------------------------------------
-- pipeline_runs: per-(clip, stage) lease table
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pipeline_runs (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id                 TEXT NOT NULL REFERENCES clips_candidate(id) ON DELETE CASCADE,
  stage                   TEXT NOT NULL CHECK (stage IN (
                            'scout', 'curator', 'editor', 'writer',
                            'voice', 'visuals', 'compositor', 'compliance',
                            'publisher', 'analyst', 'optimizer'
                          )),
  claimed_by              TEXT NOT NULL,                         -- e.g. "pid=12345@hostname"
  claimed_at              TEXT NOT NULL DEFAULT (datetime('now')),  -- UTC ISO 8601
  lease_expires_at        TEXT NOT NULL,                         -- UTC; janitor sweeps past this
  attempt                 INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
  input_artifact_version  INTEGER NOT NULL DEFAULT 0,            -- what version this stage read
  output_artifact_version INTEGER,                                -- written on successful completion
  status                  TEXT NOT NULL DEFAULT 'in_progress'
                            CHECK (status IN ('in_progress', 'succeeded', 'failed', 'expired')),
  failure_reason          TEXT,
  completed_at            TEXT                                   -- UTC; null while in_progress
);

-- One active lease per (clip, stage). The partial UNIQUE index enforces:
-- two workers cannot both hold an in_progress lease on the same (clip,
-- stage). Expired/succeeded/failed leases drop out of the constraint.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_runs_active
  ON pipeline_runs (clip_id, stage)
  WHERE status = 'in_progress';

-- Common queries: "what's stuck?" and "show me runs for this clip"
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status_expires
  ON pipeline_runs (status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_clip
  ON pipeline_runs (clip_id);
