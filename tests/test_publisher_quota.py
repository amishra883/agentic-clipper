"""Per-day quota tracking tests — Day 13 hardening.

Critical assertions:
- check_quota_or_block raises QuotaExceeded at the cap, not before
- Attempted + succeeded + failed rows all count toward 24h window
- Old rows (>24h) drop out of the count
- Default caps per platform match YouTube/TikTok/IG reality
- config/posting_schedule.yaml overrides default caps per account
- record_attempt validates status enum
- Quota gate fires BEFORE the upload call so deferred posts stay queued
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from agents import quota
from agents.db import init_schema
from scripts.migrate import migrate


@pytest.fixture
def quota_db(monkeypatch):
    """Migrated DB. quota module creates publishing_quota table on first use."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "quota.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        migrate(db_path=db_path)
        yield db_path


# ---------- Default caps match reality ----------


def test_default_caps_match_platform_documented_limits():
    """YouTube ~6/day, TikTok pre-audit 6/24h, IG 25/24h."""
    assert quota.DEFAULT_DAILY_QUOTAS["youtube_shorts"] == 6
    assert quota.DEFAULT_DAILY_QUOTAS["tiktok"] == 6
    assert quota.DEFAULT_DAILY_QUOTAS["instagram_reels"] == 25


def test_unknown_platform_returns_zero_cap(quota_db):
    """A platform with no documented cap defaults to 0 — fail closed."""
    usage = quota.current_usage("myspace", "acct-1")
    assert usage.daily_max == 0
    assert usage.remaining == 0


# ---------- record_attempt ----------


def test_record_attempt_validates_status(quota_db):
    with pytest.raises(ValueError, match="invalid status"):
        quota.record_attempt(
            "tiktok", "acct-1", clip_id="c1", status="garbage",
        )


def test_record_attempt_persists_to_db(quota_db):
    quota.record_attempt(
        "tiktok", "acct-1", clip_id="c1", status="attempted",
    )
    quota.record_attempt(
        "tiktok", "acct-1", clip_id="c1", status="succeeded",
        platform_post_id="post-123",
    )
    with sqlite3.connect(quota_db) as conn:
        rows = conn.execute(
            "SELECT status, platform_post_id FROM publishing_quota "
            "WHERE platform = ? AND account_id = ? ORDER BY id",
            ("tiktok", "acct-1"),
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "attempted"
    assert rows[1][0] == "succeeded"
    assert rows[1][1] == "post-123"


# ---------- used_in_last_24h + cap enforcement ----------


def test_quota_blocks_when_at_cap(quota_db):
    """6 attempted rows on TikTok → next check raises QuotaExceeded."""
    for i in range(6):
        quota.record_attempt(
            "tiktok", "acct-1", clip_id=f"clip-{i}", status="attempted",
        )
    with pytest.raises(quota.QuotaExceeded) as exc_info:
        quota.check_quota_or_block("tiktok", "acct-1", clip_id="clip-7")
    assert exc_info.value.used_24h == 6
    assert exc_info.value.daily_max == 6


def test_quota_passes_below_cap(quota_db):
    """5/6 used — gate lets one more through."""
    for i in range(5):
        quota.record_attempt(
            "tiktok", "acct-1", clip_id=f"clip-{i}", status="attempted",
        )
    # No exception
    quota.check_quota_or_block("tiktok", "acct-1", clip_id="clip-6")


def test_failed_attempts_still_count(quota_db):
    """A 4xx-rejected post still consumed quota platform-side; the
    local ledger must reflect that."""
    quota.record_attempt(
        "tiktok", "acct-1", clip_id="c1", status="attempted",
    )
    quota.record_attempt(
        "tiktok", "acct-1", clip_id="c1", status="failed",
    )
    # Both attempted AND failed count → 2 used
    assert quota.used_in_last_24h("tiktok", "acct-1") == 2


def test_quota_per_account_isolated(quota_db):
    """Two accounts on the same platform have independent quotas —
    backup-account failover pattern."""
    for i in range(6):
        quota.record_attempt(
            "tiktok", "acct-primary", clip_id=f"c{i}", status="attempted",
        )
    # Primary is capped, backup is fresh
    with pytest.raises(quota.QuotaExceeded):
        quota.check_quota_or_block("tiktok", "acct-primary")
    quota.check_quota_or_block("tiktok", "acct-backup")  # no exception


def test_quota_window_drops_old_rows(quota_db):
    """Rows older than 24h drop out of used_in_last_24h."""
    # Manually insert a row dated 25h ago.
    quota._ensure_quota_schema()
    with sqlite3.connect(quota_db) as conn:
        conn.execute(
            "INSERT INTO publishing_quota (platform, account_id, ts, status, clip_id) "
            "VALUES ('tiktok', 'acct-1', datetime('now','-25 hours'), 'succeeded', 'old')"
        )
        conn.commit()
    assert quota.used_in_last_24h("tiktok", "acct-1") == 0


def test_current_usage_snapshot(quota_db):
    """current_usage returns a QuotaUsage with used/cap/remaining."""
    for i in range(3):
        quota.record_attempt(
            "instagram_reels", "ig-1", clip_id=f"c{i}", status="succeeded",
        )
    usage = quota.current_usage("instagram_reels", "ig-1")
    assert usage.platform == "instagram_reels"
    assert usage.account_id == "ig-1"
    assert usage.used_24h == 3
    assert usage.daily_max == 25
    assert usage.remaining == 22


# ---------- Config override ----------


def test_per_account_override_via_posting_schedule(quota_db, monkeypatch):
    """An account-specific cap in posting_schedule.yaml beats the
    platform default. Used during account-warming when we want a
    softer ceiling."""
    from agents import quota as q

    monkeypatch.setattr(q, "load", lambda name: {
        "quotas": {
            "tiktok": {
                "daily_max": 4,  # platform-wide override
                "per_account": {"acct-warming": 2},  # account-specific
            },
        },
    })
    # Warming account: 2 cap
    assert q._daily_max("tiktok", "acct-warming") == 2
    # Other account on same platform: 4 cap
    assert q._daily_max("tiktok", "acct-other") == 4
    # Different platform: default (6 for tiktok, but this is YT)
    assert q._daily_max("youtube_shorts", "acct-other") == 6


def test_remaining_quota_never_negative(quota_db):
    """Even if somehow used_24h > daily_max, remaining stays 0."""
    for i in range(10):
        quota.record_attempt(
            "tiktok", "acct-1", clip_id=f"c{i}", status="attempted",
        )
    usage = quota.current_usage("tiktok", "acct-1")
    assert usage.used_24h == 10
    assert usage.daily_max == 6
    assert usage.remaining == 0
