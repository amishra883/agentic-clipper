"""Publisher queue-claim tests.

Covers the four behaviors hardened in commit 7dfffe6:

  1. Compliance JOIN — only clips whose latest compliance_results row passed
     are claimable. A clip with no row, or whose latest row failed, is skipped.
  2. Atomic claim — _pick_next_clip wraps SELECT+UPDATE in BEGIN IMMEDIATE;
     a second call after the first should not re-claim the same row.
  3. Timezone-safe scheduled_for — strftime('%s', scheduled_for) compare
     against strftime('%s', 'now') handles ISO 8601 with offsets correctly,
     so a clip scheduled in the future is not claimed.
  4. Manual-mode routing — when posting_schedule.yaml platform.mode='manual',
     run_publisher writes the drop directory and flips status to manual_pending.

Under the operator's fair-use-only posture the Compliance gate is the sole
legal defense; if the queue ever picks an uncomplied clip these tests fail
and the build blocks.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents import publisher
from agents.db import init_schema


# ---------- Local fixtures (DB seeded with queue + compliance rows) ----------

def _seed_candidate(conn: sqlite3.Connection, clip_id: str, creator: str = "IShowSpeed") -> None:
    conn.execute(
        """
        INSERT INTO clips_candidate (id, creator, source_platform, source_url, status)
        VALUES (?, ?, 'youtube', 'https://test', 'ready')
        """,
        (clip_id, creator),
    )


def _seed_ready_row(
    conn: sqlite3.Connection,
    clip_id: str,
    *,
    target_platform: str = "tiktok",
    account_id: str = "tiktok_primary_1",
    scheduled_for: str | None = None,
    status: str = "queued",
    description: str = "Commentary on IShowSpeed clip. Includes AI-generated visuals.",
) -> int:
    if scheduled_for is None:
        # Default: 60 seconds in the past, ISO 8601 with explicit UTC offset.
        scheduled_for = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    cur = conn.execute(
        """
        INSERT INTO clips_ready
          (clip_id, target_platform, account_id, scheduled_for, title,
           description, hashtags_json, caption_style, status)
        VALUES (?, ?, ?, ?, 'test title', ?, '["#test"]', 'pop-bold-yellow', ?)
        """,
        (clip_id, target_platform, account_id, scheduled_for, description, status),
    )
    return cur.lastrowid


def _seed_compliance_row(
    conn: sqlite3.Connection,
    clip_id: str,
    *,
    passed: bool,
    checked_at: str | None = None,
) -> None:
    if checked_at is None:
        checked_at = datetime.now(timezone.utc).isoformat(sep=" ", timespec="seconds")
    conn.execute(
        """
        INSERT INTO compliance_results (clip_id, checked_at, passed, rule_results_json, blocked_reason)
        VALUES (?, ?, ?, '{}', ?)
        """,
        (clip_id, checked_at, 1 if passed else 0, None if passed else "test-block"),
    )


@pytest.fixture
def queue_db(monkeypatch):
    """Isolated DB seeded with a candidate clip; tests add ready/compliance rows."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "queue.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        yield db_path


# ---------- 1. Compliance JOIN ----------

def test_clip_with_no_compliance_row_is_not_claimed(queue_db):
    """A clip in clips_ready with NO compliance_results row must not be picked.

    Without the JOIN, the publisher would silently bypass the legal-defense
    gate and post a clip whose compliance was never evaluated.
    """
    clip_id = "2026-05-17-1200-uncomplied"
    with sqlite3.connect(queue_db) as conn:
        _seed_candidate(conn, clip_id)
        _seed_ready_row(conn, clip_id)
        conn.commit()
    assert publisher._pick_next_clip() is None


def test_clip_with_failed_compliance_row_is_not_claimed(queue_db):
    clip_id = "2026-05-17-1200-failed"
    with sqlite3.connect(queue_db) as conn:
        _seed_candidate(conn, clip_id)
        _seed_ready_row(conn, clip_id)
        _seed_compliance_row(conn, clip_id, passed=False)
        conn.commit()
    assert publisher._pick_next_clip() is None


def test_clip_with_old_pass_then_new_fail_is_not_claimed(queue_db):
    """LATEST compliance row determines claimability. An earlier passing row
    cannot rescue a clip whose most recent evaluation failed (e.g. after a
    re-composition that introduced music)."""
    clip_id = "2026-05-17-1200-regressed"
    with sqlite3.connect(queue_db) as conn:
        _seed_candidate(conn, clip_id)
        _seed_ready_row(conn, clip_id)
        # Earlier passing row, then later failing row
        _seed_compliance_row(conn, clip_id, passed=True,  checked_at="2026-05-16 10:00:00")
        _seed_compliance_row(conn, clip_id, passed=False, checked_at="2026-05-17 10:00:00")
        conn.commit()
    assert publisher._pick_next_clip() is None


def test_clip_with_latest_passing_compliance_is_claimed(queue_db):
    clip_id = "2026-05-17-1200-clean"
    with sqlite3.connect(queue_db) as conn:
        _seed_candidate(conn, clip_id)
        _seed_ready_row(conn, clip_id)
        _seed_compliance_row(conn, clip_id, passed=True)
        conn.commit()
    claimed = publisher._pick_next_clip()
    assert claimed is not None
    assert claimed["clip_id"] == clip_id


# ---------- 2. Atomic claim ----------

def test_claim_flips_status_to_posting(queue_db):
    clip_id = "2026-05-17-1200-claim"
    with sqlite3.connect(queue_db) as conn:
        _seed_candidate(conn, clip_id)
        row_id = _seed_ready_row(conn, clip_id)
        _seed_compliance_row(conn, clip_id, passed=True)
        conn.commit()

    publisher._pick_next_clip()

    with sqlite3.connect(queue_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_ready WHERE id = ?", (row_id,)
        ).fetchone()[0]
    assert status == "posting"


def test_second_claim_returns_none_after_first(queue_db):
    """Once a clip is picked it must not be re-picked. The status change to
    'posting' is what blocks a concurrent publisher from racing onto the
    same row — the test simulates the second call after the first."""
    clip_id = "2026-05-17-1200-once"
    with sqlite3.connect(queue_db) as conn:
        _seed_candidate(conn, clip_id)
        _seed_ready_row(conn, clip_id)
        _seed_compliance_row(conn, clip_id, passed=True)
        conn.commit()

    first = publisher._pick_next_clip()
    second = publisher._pick_next_clip()
    assert first is not None
    assert second is None


# ---------- 3. Timezone-safe scheduled_for ----------

def test_future_scheduled_clip_is_not_claimed(queue_db):
    """Clips scheduled in the future must not be picked, regardless of the
    timezone offset embedded in scheduled_for. strftime('%s', ...) converts
    ISO 8601 with offset to UTC epoch on both sides of the compare."""
    clip_id = "2026-05-17-1200-future"
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    with sqlite3.connect(queue_db) as conn:
        _seed_candidate(conn, clip_id)
        _seed_ready_row(conn, clip_id, scheduled_for=future)
        _seed_compliance_row(conn, clip_id, passed=True)
        conn.commit()
    assert publisher._pick_next_clip() is None


def test_eastern_offset_clip_one_hour_ago_is_claimed(queue_db):
    """A clip with a non-UTC offset (America/New_York-ish -04:00) scheduled
    one hour ago must be picked. Direct string compare without epoch
    conversion would compare "2026-05-17T...:00-04:00" to "2026-05-17 ..."
    and produce wrong results — the strftime trick avoids that."""
    clip_id = "2026-05-17-1200-eastern"
    now_utc = datetime.now(timezone.utc)
    one_hour_ago_utc = now_utc - timedelta(hours=1)
    # Re-express as a -04:00 offset to match posting_schedule.yaml's tz.
    eastern_offset = timezone(timedelta(hours=-4))
    sched = one_hour_ago_utc.astimezone(eastern_offset).isoformat()
    assert sched.endswith("-04:00")  # sanity: confirm offset preserved
    with sqlite3.connect(queue_db) as conn:
        _seed_candidate(conn, clip_id)
        _seed_ready_row(conn, clip_id, scheduled_for=sched)
        _seed_compliance_row(conn, clip_id, passed=True)
        conn.commit()
    claimed = publisher._pick_next_clip()
    assert claimed is not None and claimed["clip_id"] == clip_id


# ---------- 4. Manual-mode routing ----------

def test_manual_mode_writes_drop_directory_and_marks_pending(queue_db, tmp_path, monkeypatch):
    """TikTok is mode='manual' in posting_schedule.yaml. run_publisher must
    write {caption.txt, hashtags.txt} into manual_drop_directory/<clip_id>/
    and flip the row to manual_pending — not call _upload_tiktok (which
    raises NotImplementedError)."""
    # Point manual_drop_directory at a temp path so the test stays sandboxed.
    drop_root = tmp_path / "manual_drop" / "tiktok"

    def _fake_load(name: str) -> dict:
        if name == "posting_schedule":
            return {
                "platforms": {
                    "tiktok": {
                        "mode": "manual",
                        "manual_drop_directory": str(drop_root),
                    },
                },
            }
        raise KeyError(name)

    monkeypatch.setattr(publisher, "load", _fake_load)

    clip_id = "2026-05-17-1200-manual"
    with sqlite3.connect(queue_db) as conn:
        _seed_candidate(conn, clip_id)
        row_id = _seed_ready_row(conn, clip_id, target_platform="tiktok")
        _seed_compliance_row(conn, clip_id, passed=True)
        conn.commit()

    asyncio.run(publisher.run_publisher())

    # Drop directory must exist with caption + hashtags
    clip_dir = drop_root / clip_id
    assert clip_dir.is_dir(), f"missing drop dir {clip_dir}"
    assert (clip_dir / "caption.txt").exists()
    assert (clip_dir / "hashtags.txt").exists()
    assert "test" in (clip_dir / "hashtags.txt").read_text()

    # Status must be manual_pending, NOT failed (the prior code's behavior)
    with sqlite3.connect(queue_db) as conn:
        row = conn.execute(
            "SELECT status, failure_reason FROM clips_ready WHERE id = ?", (row_id,)
        ).fetchone()
    assert row[0] == "manual_pending", f"expected manual_pending, got status={row[0]} reason={row[1]}"


def test_unknown_mode_marks_failed_not_silent(queue_db, monkeypatch):
    """An unrecognized mode in posting_schedule.yaml must fail the clip with
    a clear reason. The old code would silently dispatch to platform stubs
    even if the mode field was missing or garbage."""
    def _fake_load(name: str) -> dict:
        return {"platforms": {"tiktok": {"mode": "carrier_pigeon"}}}

    monkeypatch.setattr(publisher, "load", _fake_load)

    clip_id = "2026-05-17-1200-badmode"
    with sqlite3.connect(queue_db) as conn:
        _seed_candidate(conn, clip_id)
        row_id = _seed_ready_row(conn, clip_id, target_platform="tiktok")
        _seed_compliance_row(conn, clip_id, passed=True)
        conn.commit()

    asyncio.run(publisher.run_publisher())

    with sqlite3.connect(queue_db) as conn:
        row = conn.execute(
            "SELECT status, failure_reason FROM clips_ready WHERE id = ?", (row_id,)
        ).fetchone()
    assert row[0] == "failed"
    assert "carrier_pigeon" in (row[1] or "")


# ---------- Codex 2026-05-18 P1#2: quota-exceeded clip stays in queue ----------


def test_quota_exceeded_returns_clip_to_queued_status(queue_db, monkeypatch):
    """Codex P1#2: prior flow let _pick_next_clip flip the row to
    'posting' and then `continue` on QuotaExceeded, leaving the clip
    permanently in 'posting' (never re-picked because the SELECT
    filters on status='queued'). The fix calls _requeue_clip() in
    the QuotaExceeded path so the next run can pick the clip again
    once the rolling 24h window opens or a backup account is used."""
    from agents import quota

    def _fake_load(name: str) -> dict:
        return {"platforms": {"tiktok": {"mode": "api"}}}
    monkeypatch.setattr(publisher, "load", _fake_load)

    clip_id = "2026-05-17-1200-quotablock"
    # Create publishing_quota schema BEFORE opening the test's connection,
    # otherwise quota._ensure_quota_schema deadlocks against our held write lock.
    quota._ensure_quota_schema()
    with sqlite3.connect(queue_db) as conn:
        _seed_candidate(conn, clip_id)
        row_id = _seed_ready_row(conn, clip_id, target_platform="tiktok",
                                 account_id="tiktok-acct-cap")
        _seed_compliance_row(conn, clip_id, passed=True)
        # Pre-fill tiktok quota for this account so the next attempt breaches.
        for i in range(6):
            conn.execute(
                "INSERT INTO publishing_quota (platform, account_id, ts, status, clip_id) "
                "VALUES ('tiktok', 'tiktok-acct-cap', ?, 'succeeded', ?)",
                (datetime.now(timezone.utc).isoformat(), f"prior-{i}"),
            )
        conn.commit()

    asyncio.run(publisher.run_publisher())

    with sqlite3.connect(queue_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_ready WHERE id = ?", (row_id,)
        ).fetchone()[0]
    # Codex P1#2 fix: clip MUST be 'queued', not 'posting' (the broken state)
    # and not 'failed' (which would remove it from the queue entirely).
    assert status == "queued"


def test_requeue_clip_reverts_posting_to_queued(queue_db):
    """The _requeue_clip helper used by the quota path. Verifies the
    state transition is conditional (only flips 'posting' rows)."""
    clip_id = "2026-05-17-1200-requeue"
    with sqlite3.connect(queue_db) as conn:
        _seed_candidate(conn, clip_id)
        row_id = _seed_ready_row(conn, clip_id, status="posting")
        conn.commit()

    publisher._requeue_clip(row_id)

    with sqlite3.connect(queue_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_ready WHERE id = ?", (row_id,)
        ).fetchone()[0]
    assert status == "queued"


def test_requeue_clip_is_no_op_on_already_posted_row(queue_db):
    """If a clip has moved past 'posting' (succeeded → 'posted',
    or operator-cancelled), requeue MUST NOT silently flip it back
    to 'queued'. Belt-and-suspenders against a race."""
    clip_id = "2026-05-17-1200-posted"
    with sqlite3.connect(queue_db) as conn:
        _seed_candidate(conn, clip_id)
        row_id = _seed_ready_row(conn, clip_id, status="posted")
        conn.commit()

    publisher._requeue_clip(row_id)

    with sqlite3.connect(queue_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_ready WHERE id = ?", (row_id,)
        ).fetchone()[0]
    assert status == "posted"  # untouched
