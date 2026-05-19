"""Day 14 validation pilot gate tests.

Three gate criteria must all pass for the verdict to be PASS:
  1. Content ID claim rate < 2%
  2. Revenue per view (RPV) > $0.001
  3. Operator minutes per day < 45

This file covers each gate independently, the combined verdict, the
inconclusive-until-sample-hit rule, lifecycle (start → finalize), the
active-uniqueness invariant, and basic operator-data-entry validation.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agents import pilot
from agents.db import init_schema
from scripts.migrate import migrate


@pytest.fixture
def pilot_db(monkeypatch):
    """Fresh DB with the full migration chain applied (so pilot_runs +
    pilot_revenue exist on disk, not just via the lazy
    `_ensure_schema()` fallback)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "pilot.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        migrate(db_path=db_path)
        yield db_path


def _seed_posted_clips(
    db_path: Path,
    count: int,
    *,
    target_platform: str = "instagram_reels",
    started_at_utc: str | None = None,
    views_each: int | None = None,
    id_prefix: str = "pilot",
) -> list[str]:
    """Insert `count` clips_candidate + clips_ready (status='posted') +
    optionally feature_records + performance_metrics rows. Returns the
    list of clip_ids.

    `started_at_utc` is the pilot.started_at — posted_at is set to just
    after that so the count-since-pilot query picks them up."""
    posted_at = (started_at_utc or
                 datetime.now(timezone.utc).isoformat(sep=" ", timespec="seconds"))
    clip_ids = []
    with sqlite3.connect(db_path) as conn:
        for i in range(count):
            clip_id = f"2026-05-18-{i:04d}-{id_prefix}"
            clip_ids.append(clip_id)
            conn.execute(
                """
                INSERT INTO clips_candidate (id, creator, source_platform, source_url, status)
                VALUES (?, 'TestCreator', 'youtube', ?, 'published')
                """,
                (clip_id, f"https://x/{clip_id}"),
            )
            conn.execute(
                """
                INSERT INTO clips_ready
                  (clip_id, target_platform, account_id, scheduled_for,
                   description, hashtags_json, caption_style, status,
                   posted_at, platform_post_id)
                VALUES (?, ?, 'acct-1', ?, 'desc', '["#x"]', 'pop-bold-yellow',
                        'posted', ?, ?)
                """,
                (clip_id, target_platform, posted_at, posted_at, f"post-{i}"),
            )
            if views_each is not None:
                # Phase 2 Day 14 stub — Analyst hasn't shipped yet but the
                # pilot gate reads through feature_records + performance_metrics.
                cur = conn.execute(
                    """
                    INSERT INTO feature_records
                      (clip_id, target_platform, account_id, posted_at,
                       creator, source_platform)
                    VALUES (?, ?, 'acct-1', ?, 'TestCreator', 'youtube')
                    """,
                    (clip_id, target_platform, posted_at),
                )
                feature_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO performance_metrics (feature_record_id, views)
                    VALUES (?, ?)
                    """,
                    (feature_id, views_each),
                )
        conn.commit()
    return clip_ids


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_start_pilot_creates_active_row(pilot_db):
    run = pilot.start_pilot()
    assert run.status == "active"
    assert run.target_platform == "instagram_reels"
    assert run.target_clip_count == 30
    assert run.claim_threshold_pct == 2.0
    assert run.rpv_threshold_usd == 0.001
    assert run.operator_minutes_threshold == 45


def test_start_pilot_refuses_concurrent_run(pilot_db):
    pilot.start_pilot()
    with pytest.raises(pilot.PilotAlreadyActive):
        pilot.start_pilot()


def test_start_pilot_allowed_after_finalize(pilot_db):
    pilot.start_pilot()
    pilot.finalize_pilot(verdict="abandon")
    # Slot freed — new pilot opens.
    run2 = pilot.start_pilot()
    assert run2.status == "active"


def test_current_pilot_returns_none_when_inactive(pilot_db):
    assert pilot.current_pilot() is None


def test_current_pilot_returns_active_run(pilot_db):
    run = pilot.start_pilot(target_platform="youtube_shorts", target_clip_count=15)
    current = pilot.current_pilot()
    assert current is not None
    assert current.id == run.id
    assert current.target_platform == "youtube_shorts"


def test_record_revenue_requires_active_pilot(pilot_db):
    with pytest.raises(pilot.NoActivePilot):
        pilot.record_revenue(amount_usd=1.0, source="ad_rev")


def test_record_revenue_rejects_negative_amount(pilot_db):
    pilot.start_pilot()
    with pytest.raises(ValueError):
        pilot.record_revenue(amount_usd=-0.50, source="ad_rev")


def test_record_revenue_rejects_unknown_source(pilot_db):
    pilot.start_pilot()
    with pytest.raises(ValueError, match="source must be"):
        pilot.record_revenue(amount_usd=1.0, source="cryptocurrency")  # type: ignore[arg-type]


def test_record_operator_time_requires_active_pilot(pilot_db):
    with pytest.raises(pilot.NoActivePilot):
        pilot.record_operator_time(minutes=30)


def test_record_claim_requires_active_pilot(pilot_db):
    with pytest.raises(pilot.NoActivePilot):
        pilot.record_claim(clip_id="x")


# ---------------------------------------------------------------------------
# Progress counters
# ---------------------------------------------------------------------------

def test_progress_counts_clips_posted_to_target_platform_only(pilot_db):
    run = pilot.start_pilot(target_platform="instagram_reels", target_clip_count=30)
    _seed_posted_clips(pilot_db, count=5,
                       target_platform="instagram_reels",
                       started_at_utc=run.started_at)
    # Off-platform clips MUST NOT count toward the pilot.
    _seed_posted_clips(pilot_db, count=3,
                       target_platform="tiktok",
                       started_at_utc=run.started_at,
                       id_prefix="tiktok")
    progress = pilot.pilot_progress(run)
    assert progress.clips_posted == 5
    assert progress.target_clip_count == 30


def test_progress_revenue_scoped_to_active_pilot(pilot_db):
    pilot.start_pilot()
    pilot.record_revenue(amount_usd=0.50, source="ad_rev")
    pilot.record_revenue(amount_usd=1.25, source="affiliate")
    progress = pilot.pilot_progress()
    assert pytest.approx(progress.revenue_usd) == 1.75


def test_progress_operator_minutes_summed(pilot_db):
    pilot.start_pilot()
    pilot.record_operator_time(minutes=20)
    pilot.record_operator_time(minutes=15)
    progress = pilot.pilot_progress()
    assert progress.operator_minutes_total == 35


def test_progress_rpv_zero_when_no_views(pilot_db):
    pilot.start_pilot()
    pilot.record_revenue(amount_usd=2.00, source="ad_rev")
    # No performance_metrics → views = 0 → RPV = 0, not division-by-zero.
    progress = pilot.pilot_progress()
    assert progress.total_views == 0
    assert progress.rpv_usd == 0.0


def test_progress_claim_rate_zero_when_no_posts(pilot_db):
    pilot.start_pilot()
    # claim recorded with no posted clips — should not divide by zero.
    pilot.record_claim(clip_id="phantom")
    progress = pilot.pilot_progress()
    assert progress.clips_posted == 0
    assert progress.claim_rate_pct == 0.0


# ---------------------------------------------------------------------------
# Gate verdict — each criterion independently
# ---------------------------------------------------------------------------

def test_verdict_inconclusive_when_under_target_count(pilot_db):
    run = pilot.start_pilot(target_clip_count=30)
    _seed_posted_clips(pilot_db, count=10,
                       target_platform="instagram_reels",
                       started_at_utc=run.started_at,
                       views_each=1000)
    pilot.record_revenue(amount_usd=20.0, source="ad_rev")
    pilot.record_operator_time(minutes=10)
    gate = pilot.evaluate_gate()
    # Sample size too small — operator should keep posting.
    assert gate.verdict == "inconclusive"


def test_verdict_pass_when_all_three_gates_clear(pilot_db):
    run = pilot.start_pilot(target_clip_count=30)
    # 30 clips, 0 claims, $30 / 30k views = $0.001/view (just above threshold),
    # 30 minutes/day (below 45).
    _seed_posted_clips(pilot_db, count=30,
                       target_platform="instagram_reels",
                       started_at_utc=run.started_at,
                       views_each=1000)
    pilot.record_revenue(amount_usd=35.0, source="ad_rev")
    pilot.record_operator_time(minutes=30)
    gate = pilot.evaluate_gate()
    assert gate.verdict == "pass", gate.rationale
    names = [c.name for c in gate.criteria]
    assert names == ["claim_rate", "rpv", "operator_time"]
    assert all(c.passed for c in gate.criteria)


def test_verdict_fail_when_claim_rate_breaches(pilot_db):
    """One claim on 30 clips = 3.33% > 2% threshold → FAIL."""
    run = pilot.start_pilot(target_clip_count=30)
    _seed_posted_clips(pilot_db, count=30,
                       target_platform="instagram_reels",
                       started_at_utc=run.started_at,
                       views_each=1000)
    pilot.record_revenue(amount_usd=35.0, source="ad_rev")
    pilot.record_operator_time(minutes=30)
    pilot.record_claim(clip_id="2026-05-18-0000-pilot", detail="music match")
    gate = pilot.evaluate_gate()
    assert gate.verdict == "fail"
    claim_crit = next(c for c in gate.criteria if c.name == "claim_rate")
    assert not claim_crit.passed
    rpv_crit = next(c for c in gate.criteria if c.name == "rpv")
    assert rpv_crit.passed
    op_crit = next(c for c in gate.criteria if c.name == "operator_time")
    assert op_crit.passed


def test_verdict_fail_when_rpv_below_threshold(pilot_db):
    """$10 across 30k views = $0.00033/view < $0.001/view → FAIL."""
    run = pilot.start_pilot(target_clip_count=30)
    _seed_posted_clips(pilot_db, count=30,
                       target_platform="instagram_reels",
                       started_at_utc=run.started_at,
                       views_each=1000)
    pilot.record_revenue(amount_usd=10.0, source="ad_rev")
    pilot.record_operator_time(minutes=30)
    gate = pilot.evaluate_gate()
    assert gate.verdict == "fail"
    rpv_crit = next(c for c in gate.criteria if c.name == "rpv")
    assert not rpv_crit.passed


def test_verdict_fail_when_operator_time_breaches(pilot_db):
    """60 minutes total on a brand-new pilot (days_elapsed clamps to 1.0)
    → 60min/day > 45 threshold → FAIL."""
    run = pilot.start_pilot(target_clip_count=30)
    _seed_posted_clips(pilot_db, count=30,
                       target_platform="instagram_reels",
                       started_at_utc=run.started_at,
                       views_each=1000)
    pilot.record_revenue(amount_usd=35.0, source="ad_rev")
    pilot.record_operator_time(minutes=60)
    gate = pilot.evaluate_gate()
    assert gate.verdict == "fail"
    op_crit = next(c for c in gate.criteria if c.name == "operator_time")
    assert not op_crit.passed


def test_verdict_fail_records_all_failing_gates(pilot_db):
    """Multiple gates can fail simultaneously — verdict reflects every
    failing dimension so the operator sees the full picture, not just
    the first one."""
    run = pilot.start_pilot(target_clip_count=30)
    _seed_posted_clips(pilot_db, count=30,
                       target_platform="instagram_reels",
                       started_at_utc=run.started_at,
                       views_each=1000)
    # Low revenue + high operator time + claim → all three fail.
    pilot.record_revenue(amount_usd=1.0, source="ad_rev")
    pilot.record_operator_time(minutes=90)
    pilot.record_claim(clip_id="x")
    gate = pilot.evaluate_gate()
    assert gate.verdict == "fail"
    failed_names = {c.name for c in gate.criteria if not c.passed}
    assert failed_names == {"claim_rate", "rpv", "operator_time"}


# ---------------------------------------------------------------------------
# Finalize
# ---------------------------------------------------------------------------

def test_finalize_pass_sets_status_and_clears_active_slot(pilot_db):
    pilot.start_pilot()
    run = pilot.finalize_pilot(verdict="pass", notes="all gates green")
    assert run.status == "passed"
    assert run.verdict_at is not None
    # Next pilot can start because the active slot is free.
    pilot.start_pilot()


def test_finalize_fail_persists_failed_reasons(pilot_db):
    run = pilot.start_pilot(target_clip_count=2)
    _seed_posted_clips(pilot_db, count=2,
                       target_platform="instagram_reels",
                       started_at_utc=run.started_at,
                       views_each=100)
    pilot.record_revenue(amount_usd=0.05, source="ad_rev")
    pilot.record_operator_time(minutes=200)
    closed = pilot.finalize_pilot(verdict="fail")
    assert closed.status == "failed"
    assert closed.failed_reasons_json is not None
    assert "rpv" in closed.failed_reasons_json
    assert "operator_time" in closed.failed_reasons_json


def test_finalize_abandon_marks_abandoned(pilot_db):
    pilot.start_pilot()
    run = pilot.finalize_pilot(verdict="abandon")
    assert run.status == "abandoned"


def test_finalize_without_active_raises(pilot_db):
    with pytest.raises(pilot.NoActivePilot):
        pilot.finalize_pilot(verdict="pass")


def test_finalize_rejects_invalid_verdict(pilot_db):
    pilot.start_pilot()
    with pytest.raises(ValueError, match="verdict must be"):
        pilot.finalize_pilot(verdict="probably")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Edge cases the operator hits in practice
# ---------------------------------------------------------------------------

def test_progress_zero_views_does_not_divide_by_zero(pilot_db):
    run = pilot.start_pilot(target_clip_count=2)
    _seed_posted_clips(pilot_db, count=2,
                       target_platform="instagram_reels",
                       started_at_utc=run.started_at)  # views_each=None
    pilot.record_revenue(amount_usd=5.0, source="ad_rev")
    pilot.record_operator_time(minutes=10)
    gate = pilot.evaluate_gate()
    # RPV is 0/0 = 0 → fails the >$0.001 gate, but the operator gets a
    # finite number not an exception.
    assert gate.verdict == "fail"


def test_revenue_from_other_pilot_not_counted(pilot_db):
    """Pilot 1's revenue must not leak into Pilot 2's tally even if
    the operator forgets to finalize cleanly. Each pilot has its own
    pilot_revenue rows linked by pilot_run_id."""
    pilot.start_pilot()
    pilot.record_revenue(amount_usd=100.0, source="ad_rev")
    pilot.finalize_pilot(verdict="abandon")
    # New pilot — revenue starts fresh.
    pilot.start_pilot()
    progress = pilot.pilot_progress()
    assert progress.revenue_usd == 0.0


def test_operator_time_from_other_pilot_not_counted(pilot_db):
    """Operator-time events carry pilot_run_id in their payload; the
    summing query filters on it, so cross-pilot bleed is impossible."""
    pilot.start_pilot()
    pilot.record_operator_time(minutes=200)
    pilot.finalize_pilot(verdict="abandon")
    pilot.start_pilot()
    progress = pilot.pilot_progress()
    assert progress.operator_minutes_total == 0
