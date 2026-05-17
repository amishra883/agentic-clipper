-- agentic-clipper — SQLite schema
-- Single source of truth for pipeline state.
-- Apply with: sqlite3 data/main.db < data/schema.sql

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- Event log — every autonomous action writes a row here with rationale.
-- Source of truth for the daily digest and post-mortems.
-- ============================================================
CREATE TABLE IF NOT EXISTS events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT    NOT NULL DEFAULT (datetime('now')),  -- UTC
  agent           TEXT    NOT NULL,  -- scout, curator, editor, ... compliance, publisher, ...
  level           TEXT    NOT NULL CHECK (level IN ('debug','info','warn','error','blocked','autopaused','autorolledback')),
  event_type      TEXT    NOT NULL,  -- e.g. clip_ingested, clip_blocked, post_published, budget_warning
  clip_id         TEXT,
  payload_json    TEXT,              -- structured payload
  rationale       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_agent ON events (agent);
CREATE INDEX IF NOT EXISTS idx_events_clip ON events (clip_id);

-- ============================================================
-- Scout output — discovered candidate clips
-- ============================================================
CREATE TABLE IF NOT EXISTS clips_candidate (
  id                    TEXT PRIMARY KEY,        -- yyyy-mm-dd-hhmm-<short_hash>
  scouted_at            TEXT NOT NULL DEFAULT (datetime('now')),
  creator               TEXT NOT NULL,
  source_platform       TEXT NOT NULL CHECK (source_platform IN ('twitch','youtube','tiktok','kick','instagram')),
  source_url            TEXT NOT NULL,
  source_title          TEXT,
  source_duration_s     REAL,
  source_view_count     INTEGER,
  source_published_at   TEXT,
  virality_score        REAL,                    -- curator score, 0..1
  predicted_views       INTEGER,
  status                TEXT NOT NULL DEFAULT 'discovered'
                          CHECK (status IN ('discovered','curated','processing','blocked','ready','published','quarantined','expired')),
  curated_at            TEXT,
  rationale             TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON clips_candidate (status);
CREATE INDEX IF NOT EXISTS idx_candidates_creator ON clips_candidate (creator);
CREATE INDEX IF NOT EXISTS idx_candidates_score ON clips_candidate (virality_score DESC);

-- ============================================================
-- Pipeline artifacts per clip
-- ============================================================
CREATE TABLE IF NOT EXISTS clip_artifacts (
  clip_id               TEXT PRIMARY KEY REFERENCES clips_candidate(id) ON DELETE CASCADE,
  source_local_path     TEXT,            -- yt-dlp download
  punch_segment_start_s REAL,
  punch_segment_end_s   REAL,
  transcript_json       TEXT,            -- faster-whisper output
  script_text           TEXT,            -- Writer output
  shot_list_json        TEXT,            -- Writer shot list
  voice_audio_path      TEXT,            -- Voice output
  voice_runtime_s       REAL,
  visuals_seconds_used  REAL DEFAULT 0,
  visuals_tier          TEXT CHECK (visuals_tier IN ('fast','pro','mixed')),
  visuals_cost_usd      REAL DEFAULT 0,
  final_video_path      TEXT,            -- Compositor output
  final_duration_s      REAL,
  updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- Compliance gate log — every clip's pass/fail per rule
-- ============================================================
CREATE TABLE IF NOT EXISTS compliance_results (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id           TEXT NOT NULL REFERENCES clips_candidate(id),
  checked_at        TEXT NOT NULL DEFAULT (datetime('now')),
  passed            INTEGER NOT NULL CHECK (passed IN (0,1)),
  rule_results_json TEXT NOT NULL,     -- {rule_id: {passed: bool, detail: ...}}
  blocked_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_compliance_clip ON compliance_results (clip_id);

-- ============================================================
-- Seedance generation log — provenance + cost audit
-- ============================================================
CREATE TABLE IF NOT EXISTS seedance_generations (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id             TEXT REFERENCES clips_candidate(id),
  ts                  TEXT NOT NULL DEFAULT (datetime('now')),
  provider            TEXT NOT NULL,   -- atlas_cloud | fal_ai | ...
  model_version       TEXT NOT NULL,   -- seedance-2.0-fast etc.
  tier                TEXT NOT NULL CHECK (tier IN ('fast','pro')),
  prompt              TEXT NOT NULL,
  reference_image_hash TEXT,
  seed                INTEGER,
  duration_s          REAL NOT NULL,
  cost_usd            REAL NOT NULL,
  status              TEXT NOT NULL CHECK (status IN ('succeeded','failed_face_filter','failed_other')),
  output_path         TEXT,
  raw_response_json   TEXT             -- store for face-filter "200 with empty body" detection
);
CREATE INDEX IF NOT EXISTS idx_seedance_clip ON seedance_generations (clip_id);
CREATE INDEX IF NOT EXISTS idx_seedance_ts ON seedance_generations (ts);

-- ============================================================
-- Generated-asset cache (concept graphics, stingers, etc.)
-- ============================================================
CREATE TABLE IF NOT EXISTS generated_cache (
  prompt_hash       TEXT PRIMARY KEY,    -- sha256(prompt + seed + duration + tier)
  prompt            TEXT NOT NULL,
  asset_path        TEXT NOT NULL,
  provider          TEXT NOT NULL,
  tier              TEXT NOT NULL,
  duration_s        REAL NOT NULL,
  cost_usd          REAL NOT NULL,
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  hit_count         INTEGER NOT NULL DEFAULT 0
);

-- ============================================================
-- Publish queue
-- ============================================================
CREATE TABLE IF NOT EXISTS clips_ready (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id             TEXT NOT NULL REFERENCES clips_candidate(id),
  target_platform     TEXT NOT NULL CHECK (target_platform IN ('tiktok','instagram_reels','youtube_shorts')),
  account_id          TEXT NOT NULL,   -- which of our accounts (primary or backup)
  scheduled_for       TEXT NOT NULL,
  title               TEXT,
  description         TEXT NOT NULL,   -- includes attribution + #ad if applicable + AI-content note
  hashtags_json       TEXT NOT NULL,
  caption_style       TEXT NOT NULL,
  hook_template_id    TEXT,
  experiment_arm      TEXT,
  status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','posting','manual_pending','posted','failed','cancelled')),
  posted_at           TEXT,
  platform_post_id    TEXT,
  failure_reason      TEXT,
  retry_count         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ready_scheduled ON clips_ready (status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_ready_account ON clips_ready (account_id);

-- ============================================================
-- Feature records (per-clip features for the learning loop)
-- See CLAUDE.md "Self-improvement loop" for the shape.
-- ============================================================
CREATE TABLE IF NOT EXISTS feature_records (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id                 TEXT NOT NULL REFERENCES clips_candidate(id),
  target_platform         TEXT NOT NULL,
  account_id              TEXT NOT NULL,
  posted_at               TEXT NOT NULL,
  creator                 TEXT NOT NULL,
  source_platform         TEXT NOT NULL,
  hook_template_id        TEXT,
  persona_id              TEXT,
  avatar_variant          TEXT,
  avatar_reactions_used_json   TEXT,
  script_substance_tags_json   TEXT,
  trending_refs_used_json      TEXT,
  trending_freshness      TEXT CHECK (trending_freshness IN ('hot','rising','cooked')),
  punch_density           REAL,
  seedance_seconds_used   REAL,
  seedance_tier           TEXT,
  b_roll_density          REAL,
  caption_style           TEXT,
  hashtags_json           TEXT,
  experiment_arm          TEXT
);
CREATE INDEX IF NOT EXISTS idx_features_clip ON feature_records (clip_id);
CREATE INDEX IF NOT EXISTS idx_features_posted ON feature_records (posted_at);

-- ============================================================
-- Performance metrics (Analyst joins with feature_records after 48h)
-- ============================================================
CREATE TABLE IF NOT EXISTS performance_metrics (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  feature_record_id     INTEGER NOT NULL REFERENCES feature_records(id),
  pulled_at             TEXT NOT NULL DEFAULT (datetime('now')),
  views                 INTEGER,
  watch_time_pct        REAL,
  likes                 INTEGER,
  shares                INTEGER,
  follows_gained        INTEGER,
  click_throughs        INTEGER,
  comments              INTEGER,
  hours_since_post      REAL
);
CREATE INDEX IF NOT EXISTS idx_perf_feature ON performance_metrics (feature_record_id);

-- ============================================================
-- Accounts (primary + backups) with strike monitor
-- ============================================================
CREATE TABLE IF NOT EXISTS accounts (
  id                    TEXT PRIMARY KEY,            -- e.g. tiktok_primary_1, tiktok_backup_1
  platform              TEXT NOT NULL,
  role                  TEXT NOT NULL CHECK (role IN ('primary','backup_warm','backup_cold','retired')),
  warming_started_at    TEXT,
  warming_complete_at   TEXT,
  warm_eligible         INTEGER NOT NULL DEFAULT 0 CHECK (warm_eligible IN (0,1)),
  active                INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  strike_count          INTEGER NOT NULL DEFAULT 0,
  last_strike_at        TEXT,
  notes                 TEXT,
  created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- Strikes log (one row per copyright/community-guidelines strike)
-- ============================================================
CREATE TABLE IF NOT EXISTS strikes (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id            TEXT NOT NULL REFERENCES accounts(id),
  clip_id               TEXT REFERENCES clips_candidate(id),
  platform              TEXT NOT NULL,
  struck_at             TEXT NOT NULL,
  strike_type           TEXT NOT NULL,    -- copyright | guidelines | other
  detail                TEXT,
  resolved              INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0,1)),
  resolved_at           TEXT,
  resolution_action     TEXT              -- disputed | accepted | removed_content
);

-- ============================================================
-- Cost ledger (every external-spend line item)
-- ============================================================
CREATE TABLE IF NOT EXISTS costs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL DEFAULT (datetime('now')),
  category        TEXT NOT NULL,   -- matches keys in config/budget.yaml line_items
  amount_usd      REAL NOT NULL,
  clip_id         TEXT REFERENCES clips_candidate(id),
  provider        TEXT,
  detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_costs_ts ON costs (ts);
CREATE INDEX IF NOT EXISTS idx_costs_category ON costs (category);

-- ============================================================
-- Optimizer auto-changes (audit trail; mirrors /data/auto_changes.jsonl)
-- ============================================================
CREATE TABLE IF NOT EXISTS auto_changes (
  id              TEXT PRIMARY KEY,
  applied_at      TEXT NOT NULL DEFAULT (datetime('now')),
  change_type     TEXT NOT NULL,   -- posting_time_shift | hashtag_rotation | ...
  before_json     TEXT NOT NULL,
  after_json      TEXT NOT NULL,
  rationale       TEXT NOT NULL,
  rolled_back     INTEGER NOT NULL DEFAULT 0 CHECK (rolled_back IN (0,1)),
  rolled_back_at  TEXT,
  rollback_reason TEXT
);

-- ============================================================
-- Experiments registry
-- ============================================================
CREATE TABLE IF NOT EXISTS experiments (
  id                TEXT PRIMARY KEY,
  name              TEXT NOT NULL,
  status            TEXT NOT NULL CHECK (status IN ('open','closed','aborted')),
  started_at        TEXT NOT NULL DEFAULT (datetime('now')),
  ended_at          TEXT,
  arms_json         TEXT NOT NULL,
  current_state_json TEXT,
  termination_conditions_json TEXT NOT NULL,
  winner_arm        TEXT
);

-- ============================================================
-- Schema version (for future migrations)
-- ============================================================
CREATE TABLE IF NOT EXISTS schema_version (
  version       INTEGER PRIMARY KEY,
  applied_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
