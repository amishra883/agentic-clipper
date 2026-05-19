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
from datetime import datetime, timedelta, timezone
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


# ---------- Codex 2026-05-18 P1 fixes ----------


def test_quota_window_uses_epoch_compare_not_lexical(quota_db):
    """Codex P1#1: prior code did `ts >= datetime('now','-24 hours')`
    which lexically compared ISO `'2026-05-17T12:00:00+00:00'` against
    `'2026-05-17 13:00:00'`. `T` > space, so the 12pm row counted as
    fresh against a 1pm threshold. Verify ISO-formatted rows that
    are OLDER than 24h drop out of the count.

    The fix: `strftime('%s', ts)` parses BOTH formats to Unix epoch,
    making the comparison time-correct."""
    import sqlite3
    quota._ensure_quota_schema()
    with sqlite3.connect(quota_db) as conn:
        # Row from 25 hours ago, in production's ISO-T-offset format
        conn.execute(
            """
            INSERT INTO publishing_quota (platform, account_id, ts, status, clip_id)
            VALUES ('tiktok', 'acct-iso', ?, 'succeeded', 'old-iso')
            """,
            ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),),
        )
        # Row from 12 hours ago, also ISO format — SHOULD count
        conn.execute(
            """
            INSERT INTO publishing_quota (platform, account_id, ts, status, clip_id)
            VALUES ('tiktok', 'acct-iso', ?, 'succeeded', 'fresh-iso')
            """,
            ((datetime.now(timezone.utc) - timedelta(hours=12)).isoformat(),),
        )
        conn.commit()
    # 25h row dropped, 12h row counted = 1
    assert quota.used_in_last_24h("tiktok", "acct-iso") == 1


def test_start_attempt_then_resolve_yields_one_row(quota_db):
    """Codex P1#3: prior `record_attempt('attempted') +
    record_attempt('succeeded')` appended TWO rows for ONE upload.
    A cap of 6 was exhausted at 3 real posts. Verify the new
    start_attempt → resolve_attempt path produces exactly one row
    per upload regardless of outcome."""
    import sqlite3

    aid = quota.start_attempt("tiktok", "acct-1", clip_id="c1")
    assert isinstance(aid, int)
    quota.resolve_attempt(aid, status="succeeded", platform_post_id="post-123")

    with sqlite3.connect(quota_db) as conn:
        rows = conn.execute(
            "SELECT id, status, platform_post_id FROM publishing_quota "
            "WHERE platform = 'tiktok' AND account_id = 'acct-1' "
            "ORDER BY id"
        ).fetchall()
    # Exactly ONE row — start_attempt inserted, resolve_attempt updated in place
    assert len(rows) == 1
    assert rows[0][0] == aid
    assert rows[0][1] == "succeeded"
    assert rows[0][2] == "post-123"
    # And it counts as ONE toward the rolling window
    assert quota.used_in_last_24h("tiktok", "acct-1") == 1


def test_start_attempt_failure_path_one_row(quota_db):
    """A failed upload also produces exactly one row."""
    aid = quota.start_attempt("tiktok", "acct-1", clip_id="c1")
    quota.resolve_attempt(aid, status="failed")
    assert quota.used_in_last_24h("tiktok", "acct-1") == 1


def test_six_successful_uploads_consume_exactly_six(quota_db):
    """End-to-end: 6 uploads (start + resolve each) → 6 rows → cap
    exhausted. Prior bug: 12 rows → cap exhausted at 3 uploads."""
    for i in range(6):
        aid = quota.start_attempt("tiktok", "acct-1", clip_id=f"c{i}")
        quota.resolve_attempt(aid, status="succeeded", platform_post_id=f"p{i}")
    assert quota.used_in_last_24h("tiktok", "acct-1") == 6
    # 7th would breach
    with pytest.raises(quota.QuotaExceeded):
        quota.check_quota_or_block("tiktok", "acct-1")


def test_resolve_attempt_rejects_invalid_status(quota_db):
    aid = quota.start_attempt("tiktok", "acct-1", clip_id="c1")
    with pytest.raises(ValueError, match="must be 'succeeded' or 'failed'"):
        quota.resolve_attempt(aid, status="garbage")


def test_resolve_attempt_idempotent_on_already_resolved_row(quota_db):
    """Second resolve_attempt on the same id is a no-op (WHERE status=
    'attempted' filters it out). Used by retry paths that might
    double-call."""
    import sqlite3
    aid = quota.start_attempt("tiktok", "acct-1", clip_id="c1")
    quota.resolve_attempt(aid, status="succeeded", platform_post_id="p1")
    # Second call must NOT change the row
    quota.resolve_attempt(aid, status="failed")
    with sqlite3.connect(quota_db) as conn:
        row = conn.execute(
            "SELECT status, platform_post_id FROM publishing_quota WHERE id = ?",
            (aid,),
        ).fetchone()
    assert row[0] == "succeeded"
    assert row[1] == "p1"
