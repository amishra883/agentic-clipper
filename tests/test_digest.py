"""Digest tests — verifies the 7-section spec and the action-promotion rules.

Critical assertions:
- Each section queries from the right table and returns the right shape
- Alert promotion: red → top of "what needs you", amber fills remaining slots
- Manual queue surfaces correctly
- Budget breakdown separates pending vs succeeded
- Empty-DB digest is renderable (no crashes on a fresh setup)
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from agents.db import init_schema
from agents.digest import (
    Alert,
    Digest,
    build_digest,
)
from scripts.migrate import migrate


@pytest.fixture
def migrated_db(monkeypatch):
    """Fresh DB through v4."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "digest.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        migrate(db_path=db_path)
        yield db_path


def _seed_candidate(conn, clip_id: str = "test-clip-1"):
    conn.execute(
        """
        INSERT INTO clips_candidate (id, creator, source_platform, source_url, status)
        VALUES (?, 'IShowSpeed', 'youtube', 'https://t', 'processing')
        """,
        (clip_id,),
    )


# ---------- Empty DB happy path ----------

def test_empty_db_digest_renders_without_crash(migrated_db):
    """Fresh DB, zero clips. Digest should build and render — at minimum
    the section headers + empty placeholders. This is the day-0 operator
    experience (just ran `make setup-apply`)."""
    digest = build_digest()
    text = digest.render_text()
    assert "ALERTS" in text
    assert "MANUAL TIKTOK QUEUE" in text
    assert "YESTERDAY" in text
    assert "BUDGET" in text
    assert "AUTO-CHANGES" in text
    assert "WHAT NEEDS YOU" in text


def test_empty_db_yesterday_stats_are_zero(migrated_db):
    digest = build_digest()
    assert digest.yesterday.posted == 0
    assert digest.yesterday.quarantined == 0
    assert digest.yesterday.failed == 0
    assert digest.yesterday.manual_pending == 0


# ---------- Alerts ----------

def test_unresolved_strike_surfaces_red_alert(migrated_db):
    """One unresolved strike must surface as a red alert. Zero-strikes
    posture per CLAUDE.md hard constraint."""
    with sqlite3.connect(migrated_db) as conn:
        conn.execute(
            """
            INSERT INTO accounts (id, platform, role, active)
            VALUES ('tt_primary', 'tiktok', 'primary', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO strikes (account_id, platform, struck_at, strike_type, resolved)
            VALUES ('tt_primary', 'tiktok', datetime('now'), 'copyright', 0)
            """
        )
        conn.commit()
    digest = build_digest()
    red_alerts = [a for a in digest.alerts if a.severity == "red"]
    assert any("unresolved strike" in a.title for a in red_alerts)


def test_resolved_strike_does_not_alert(migrated_db):
    with sqlite3.connect(migrated_db) as conn:
        conn.execute(
            """
            INSERT INTO accounts (id, platform, role, active)
            VALUES ('tt_primary', 'tiktok', 'primary', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO strikes (account_id, platform, struck_at, strike_type, resolved)
            VALUES ('tt_primary', 'tiktok', datetime('now'), 'copyright', 1)
            """
        )
        conn.commit()
    digest = build_digest()
    assert not any("unresolved strike" in a.title for a in digest.alerts)


def test_budget_above_85pct_surfaces_amber(migrated_db):
    """Spend at 85% of the $240 monthly cap should flag amber."""
    # $240 * 0.86 = $206.40
    with sqlite3.connect(migrated_db) as conn:
        conn.execute(
            """
            INSERT INTO costs (ts, category, amount_usd, status)
            VALUES (datetime('now'), 'seedance_fast', 206.40, 'succeeded')
            """
        )
        conn.commit()
    digest = build_digest()
    amber = [a for a in digest.alerts if a.severity == "amber"]
    assert any("approaching monthly cap" in a.title for a in amber)


def test_budget_over_cap_surfaces_red(migrated_db):
    """Above the $240 cap → red alert."""
    with sqlite3.connect(migrated_db) as conn:
        conn.execute(
            """
            INSERT INTO costs (ts, category, amount_usd, status)
            VALUES (datetime('now'), 'seedance_fast', 250.0, 'succeeded')
            """
        )
        conn.commit()
    digest = build_digest()
    red = [a for a in digest.alerts if a.severity == "red"]
    assert any("monthly cap breached" in a.title for a in red)


def test_pending_costs_count_toward_alert_threshold(migrated_db):
    """Pending reservations + succeeded should trigger the 85% alert
    even if only succeeded would be under threshold."""
    with sqlite3.connect(migrated_db) as conn:
        conn.execute(
            """
            INSERT INTO costs (ts, category, amount_usd, status)
            VALUES (datetime('now'), 'seedance_fast', 100.0, 'succeeded')
            """
        )
        conn.execute(
            """
            INSERT INTO costs (ts, category, amount_usd, status, reservation_id)
            VALUES (datetime('now'), 'seedance_fast', 110.0, 'pending', 'pending-test-1')
            """
        )
        conn.commit()
    digest = build_digest()
    amber = [a for a in digest.alerts if a.severity == "amber"]
    # 100 + 110 = 210, which is 87.5% of 240 → above 85% threshold
    assert any("approaching monthly cap" in a.title for a in amber)


# ---------- Manual queue ----------

def test_manual_queue_lists_pending_clips(migrated_db):
    with sqlite3.connect(migrated_db) as conn:
        _seed_candidate(conn, "test-clip-mq-1")
        conn.execute(
            """
            INSERT INTO clips_ready
              (clip_id, target_platform, account_id, scheduled_for,
               description, hashtags_json, caption_style, status)
            VALUES ('test-clip-mq-1', 'tiktok', 'tt_primary_1',
                    '2026-05-18T07:00:00-04:00',
                    'Commentary on test', '["#test"]', 'pop-bold-yellow',
                    'manual_pending')
            """
        )
        conn.commit()
    digest = build_digest()
    assert len(digest.manual_queue) == 1
    assert digest.manual_queue[0].clip_id == "test-clip-mq-1"
    assert digest.manual_queue[0].platform == "tiktok"
    assert "manual_upload/tiktok/test-clip-mq-1" in digest.manual_queue[0].drop_path


def test_posted_clips_not_in_manual_queue(migrated_db):
    with sqlite3.connect(migrated_db) as conn:
        _seed_candidate(conn, "test-clip-posted")
        conn.execute(
            """
            INSERT INTO clips_ready
              (clip_id, target_platform, account_id, scheduled_for,
               description, hashtags_json, caption_style, status)
            VALUES ('test-clip-posted', 'tiktok', 'tt_primary_1',
                    datetime('now'),
                    'Commentary', '["#test"]', 'pop', 'posted')
            """
        )
        conn.commit()
    digest = build_digest()
    assert digest.manual_queue == []


# ---------- Yesterday stats ----------

def test_yesterday_counts_recently_posted(migrated_db):
    with sqlite3.connect(migrated_db) as conn:
        _seed_candidate(conn, "yesterday-1")
        conn.execute(
            """
            INSERT INTO clips_ready
              (clip_id, target_platform, account_id, scheduled_for,
               description, hashtags_json, caption_style, status, posted_at)
            VALUES ('yesterday-1', 'tiktok', 'tt_primary_1',
                    datetime('now', '-2 hours'),
                    'Commentary', '["#test"]', 'pop', 'posted',
                    datetime('now', '-2 hours'))
            """
        )
        conn.commit()
    digest = build_digest()
    assert digest.yesterday.posted == 1


# ---------- Budget breakdown ----------

def test_budget_breakdown_separates_pending_and_succeeded(migrated_db):
    with sqlite3.connect(migrated_db) as conn:
        conn.execute(
            """
            INSERT INTO costs (ts, category, amount_usd, status, reservation_id)
            VALUES (datetime('now'), 'seedance_fast', 10.0, 'pending', 'r1')
            """
        )
        conn.execute(
            """
            INSERT INTO costs (ts, category, amount_usd, status)
            VALUES (datetime('now'), 'seedance_fast', 20.0, 'succeeded')
            """
        )
        conn.commit()
    digest = build_digest()
    fast = next(b for b in digest.budget if b.category == "seedance_fast")
    assert fast.pending_usd == 10.0
    assert fast.succeeded_usd == 20.0
    # Joined with config/budget.yaml — seedance_fast has a $50 line item cap
    assert fast.monthly_cap_usd == 50.0


# ---------- Auto-changes ----------

def test_auto_changes_lists_recent(migrated_db):
    with sqlite3.connect(migrated_db) as conn:
        conn.execute(
            """
            INSERT INTO auto_changes
              (id, applied_at, change_type, before_json, after_json, rationale, rolled_back)
            VALUES ('ac-1', datetime('now', '-2 hours'),
                    'posting_time_shift', '{}', '{}',
                    'engagement +14% vs baseline', 0)
            """
        )
        conn.commit()
    digest = build_digest()
    assert len(digest.auto_changes) == 1
    assert digest.auto_changes[0].change_type == "posting_time_shift"
    assert digest.auto_changes[0].rolled_back is False


def test_auto_changes_excludes_old_entries(migrated_db):
    """Changes from >24h ago should not appear (they were in yesterday's
    digest, not today's)."""
    with sqlite3.connect(migrated_db) as conn:
        conn.execute(
            """
            INSERT INTO auto_changes
              (id, applied_at, change_type, before_json, after_json, rationale)
            VALUES ('ac-old', datetime('now', '-3 days'),
                    'old_change', '{}', '{}', 'old rationale')
            """
        )
        conn.commit()
    digest = build_digest()
    assert digest.auto_changes == []


# ---------- Action promotion ----------

def test_red_alert_promotes_to_actions(migrated_db):
    """Red alerts must always appear in 'what needs you'."""
    with sqlite3.connect(migrated_db) as conn:
        conn.execute(
            """
            INSERT INTO costs (ts, category, amount_usd, status)
            VALUES (datetime('now'), 'seedance_fast', 250.0, 'succeeded')
            """
        )
        conn.commit()
    digest = build_digest()
    assert len(digest.actions) >= 1
    assert any("monthly cap" in a.title for a in digest.actions)


def test_manual_queue_promotes_to_actions(migrated_db):
    with sqlite3.connect(migrated_db) as conn:
        _seed_candidate(conn, "mq-action-1")
        conn.execute(
            """
            INSERT INTO clips_ready
              (clip_id, target_platform, account_id, scheduled_for,
               description, hashtags_json, caption_style, status)
            VALUES ('mq-action-1', 'tiktok', 'tt_primary_1',
                    datetime('now'), 'Commentary', '["#t"]', 'pop',
                    'manual_pending')
            """
        )
        conn.commit()
    digest = build_digest()
    tiktok_action = next((a for a in digest.actions if "manually" in a.title), None)
    assert tiktok_action is not None
    assert tiktok_action.command == "make tiktok-flow"


def test_actions_capped_at_three(migrated_db):
    """Even if many alerts fire, the 'what needs you' list is capped at 3
    items — that's the cognitive-load policy."""
    with sqlite3.connect(migrated_db) as conn:
        # Two red strikes + a budget red
        conn.execute(
            """
            INSERT INTO accounts (id, platform, role, active)
            VALUES ('tt_primary', 'tiktok', 'primary', 1)
            """
        )
        for i in range(5):
            conn.execute(
                """
                INSERT INTO strikes (account_id, platform, struck_at, strike_type, resolved)
                VALUES ('tt_primary', 'tiktok', datetime('now'), 'copyright', 0)
                """
            )
        # Plus an over-cap budget
        conn.execute(
            """
            INSERT INTO costs (ts, category, amount_usd, status)
            VALUES (datetime('now'), 'seedance_fast', 300.0, 'succeeded')
            """
        )
        # Plus a manual queue clip
        _seed_candidate(conn, "mq-cap-1")
        conn.execute(
            """
            INSERT INTO clips_ready
              (clip_id, target_platform, account_id, scheduled_for,
               description, hashtags_json, caption_style, status)
            VALUES ('mq-cap-1', 'tiktok', 'tt_primary_1',
                    datetime('now'), 'Commentary', '["#t"]', 'pop',
                    'manual_pending')
            """
        )
        conn.commit()
    digest = build_digest()
    assert len(digest.actions) <= 3
