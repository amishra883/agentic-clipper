"""Cost reservation layer tests — verifies the race-free budget enforcement from E-2.

Critical assertions:
- reserve() inserts a pending row inside BEGIN IMMEDIATE
- MTD cap enforcement: pending + succeeded both count, not just succeeded
- Daily cap enforcement on top of monthly
- settle() finalizes pending → succeeded/failed; idempotent on double-call
- Concurrent caller would see the pending reservation (modeled as a
  manual pending row in tests)
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from agents.costs import (
    BudgetExceeded,
    mtd_breakdown,
    reserve,
    settle,
)
from agents.db import init_schema
from scripts.migrate import migrate


@pytest.fixture
def migrated_db(monkeypatch):
    """Fresh DB with migrations 002 + 003 applied."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "costs.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        migrate(db_path=db_path)
        yield db_path


# ---------- reserve() happy path ----------

def test_reserve_inserts_pending_row(migrated_db):
    rid = reserve(category="seedance_fast", amount_usd=0.10, detail="test")
    assert isinstance(rid, str) and len(rid) > 10
    with sqlite3.connect(migrated_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM costs WHERE reservation_id = ?", (rid,)
        ).fetchone()
    assert row["status"] == "pending"
    assert row["amount_usd"] == 0.10
    assert row["category"] == "seedance_fast"
    assert row["detail"] == "test"


def test_reserve_rejects_negative_amount(migrated_db):
    with pytest.raises(ValueError, match="must be >= 0"):
        reserve(category="seedance_fast", amount_usd=-1.0)


def test_reserve_zero_amount_is_allowed(migrated_db):
    """A cache hit reserves $0 — still write the row so audit logs the call."""
    rid = reserve(category="seedance_fast", amount_usd=0.0)
    with sqlite3.connect(migrated_db) as conn:
        row = conn.execute(
            "SELECT amount_usd, status FROM costs WHERE reservation_id = ?", (rid,)
        ).fetchone()
    assert row[0] == 0.0
    assert row[1] == "pending"


# ---------- Cap enforcement ----------

def test_reserve_blocks_when_line_item_cap_would_break(migrated_db):
    """Pre-seed MTD with $49.50 (succeeded) and try to reserve $0.60
    against a $50 line item — should raise BudgetExceeded."""
    with sqlite3.connect(migrated_db) as conn:
        conn.execute(
            """
            INSERT INTO costs (ts, category, amount_usd, status)
            VALUES (datetime('now'), 'seedance_fast', 49.50, 'succeeded')
            """
        )
        conn.commit()
    with pytest.raises(BudgetExceeded) as exc_info:
        reserve(
            category="seedance_fast",
            amount_usd=0.60,
            line_item_cap_usd=50.0,
        )
    err = exc_info.value
    assert err.cap_name == "seedance_fast_monthly"
    assert err.cap == 50.0
    assert err.mtd_succeeded == 49.50


def test_reserve_blocks_when_pending_alone_would_break_cap(migrated_db):
    """Pending reservations count toward the cap too. Worker A reserves
    $30, worker B tries $25 against a $50 cap → B fails BECAUSE A's
    pending row is summed in. (Without this, the race E-2 describes
    would slip both through.)"""
    reserve(category="seedance_fast", amount_usd=30.0, line_item_cap_usd=50.0)
    with pytest.raises(BudgetExceeded):
        reserve(category="seedance_fast", amount_usd=25.0, line_item_cap_usd=50.0)


def test_reserve_passes_when_under_cap(migrated_db):
    """Reservation that fits should succeed and leave headroom."""
    rid = reserve(
        category="seedance_fast",
        amount_usd=10.0,
        line_item_cap_usd=50.0,
    )
    assert rid is not None
    # Second reservation that still fits also succeeds
    rid2 = reserve(
        category="seedance_fast",
        amount_usd=15.0,
        line_item_cap_usd=50.0,
    )
    assert rid2 is not None and rid2 != rid


def test_reserve_blocks_daily_cap(migrated_db):
    """Daily cap is independent of monthly. With $0 MTD, but $3 today
    already, a $3 reservation against a $5 daily cap should fail."""
    # Seed today with $3 from a prior call
    with sqlite3.connect(migrated_db) as conn:
        conn.execute(
            """
            INSERT INTO costs (ts, category, amount_usd, status)
            VALUES (datetime('now'), 'atlas_cloud', 3.0, 'succeeded')
            """
        )
        conn.commit()
    with pytest.raises(BudgetExceeded) as exc_info:
        reserve(
            category="atlas_cloud",
            amount_usd=3.0,
            daily_cap_usd=5.0,
        )
    assert exc_info.value.cap_name == "atlas_cloud_daily"


def test_reserve_no_caps_supplied_always_passes(migrated_db):
    """If caller passes no cap parameters, reserve writes the row without
    enforcement. (Caller's policy.)"""
    rid = reserve(category="seedance_fast", amount_usd=10_000.0)
    assert rid is not None


def test_failed_rows_dont_count_toward_cap(migrated_db):
    """A failed reservation (settled with status='failed') has amount=0
    in the ledger and shouldn't block future reserves."""
    rid = reserve(category="seedance_fast", amount_usd=40.0, line_item_cap_usd=50.0)
    settle(rid, actual_amount_usd=40.0, status="failed")
    # Now the slot is free — reserve $40 again should succeed
    rid2 = reserve(category="seedance_fast", amount_usd=40.0, line_item_cap_usd=50.0)
    assert rid2 != rid


# ---------- settle() ----------

def test_settle_succeeds_updates_amount_and_status(migrated_db):
    """settle with actual=0.45 (slightly under reserved 0.50) updates
    both fields and timestamps settled_at."""
    rid = reserve(category="seedance_fast", amount_usd=0.50)
    settle(rid, actual_amount_usd=0.45, status="succeeded")
    with sqlite3.connect(migrated_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT amount_usd, status, settled_at FROM costs WHERE reservation_id = ?",
            (rid,),
        ).fetchone()
    assert row["amount_usd"] == 0.45
    assert row["status"] == "succeeded"
    assert row["settled_at"] is not None


def test_settle_failure_zeroes_amount(migrated_db):
    """settle(failed) zeros amount so the cap reflects reality."""
    rid = reserve(category="seedance_fast", amount_usd=0.50)
    settle(rid, actual_amount_usd=0.0, status="failed")
    with sqlite3.connect(migrated_db) as conn:
        row = conn.execute(
            "SELECT amount_usd, status FROM costs WHERE reservation_id = ?",
            (rid,),
        ).fetchone()
    assert row[0] == 0.0
    assert row[1] == "failed"


def test_settle_is_idempotent(migrated_db):
    """Double-settle should be a silent no-op — the second call must not
    flip status back, count twice, or raise."""
    rid = reserve(category="seedance_fast", amount_usd=0.50)
    settle(rid, actual_amount_usd=0.45, status="succeeded")
    # Second settle attempt: WHERE status='pending' filter means UPDATE
    # affects zero rows. No exception, no state change.
    settle(rid, actual_amount_usd=999.0, status="failed")
    with sqlite3.connect(migrated_db) as conn:
        row = conn.execute(
            "SELECT amount_usd, status FROM costs WHERE reservation_id = ?",
            (rid,),
        ).fetchone()
    assert row[0] == 0.45  # unchanged from first settle
    assert row[1] == "succeeded"


def test_settle_rejects_invalid_status(migrated_db):
    rid = reserve(category="seedance_fast", amount_usd=0.10)
    with pytest.raises(ValueError, match="must be 'succeeded' or 'failed'"):
        settle(rid, actual_amount_usd=0.10, status="weird")


def test_settle_rejects_negative_actual(migrated_db):
    rid = reserve(category="seedance_fast", amount_usd=0.10)
    with pytest.raises(ValueError, match="must be >= 0"):
        settle(rid, actual_amount_usd=-1.0)


# ---------- mtd_breakdown ----------

def test_mtd_breakdown_separates_states(migrated_db):
    """Breakdown returns separate pending/succeeded/failed sums for
    audit visibility (digest will surface this)."""
    rid_p = reserve(category="seedance_fast", amount_usd=5.0)  # pending
    rid_s = reserve(category="seedance_fast", amount_usd=10.0)
    settle(rid_s, actual_amount_usd=9.5, status="succeeded")
    rid_f = reserve(category="seedance_fast", amount_usd=3.0)
    settle(rid_f, actual_amount_usd=0.0, status="failed")
    breakdown = mtd_breakdown("seedance_fast")
    assert breakdown.pending == 5.0
    assert breakdown.succeeded == 9.5
    assert breakdown.failed == 0.0  # failed rows have amount=0 after settle


def test_mtd_breakdown_empty_category_returns_zeros(migrated_db):
    breakdown = mtd_breakdown("never_seen_before")
    assert breakdown.pending == 0.0
    assert breakdown.succeeded == 0.0
    assert breakdown.failed == 0.0
