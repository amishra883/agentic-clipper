"""Doctor health-check behavior tests.

Covers the three new DB-backed checks added in commit 7dfffe6:

  - check_monthly_budget_burn: month-to-date sum vs cap / hard_kill
  - check_strike_monitor: any unresolved strike = FAIL
  - check_account_warming: primary>=1 + warm_backups>=2 per platform

The doctor functions read REPO_ROOT-relative config and the AGENTIC_CLIPPER_DB
env var, so tests just point the env var at a temp DB and seed the rows the
checks query.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from agents.db import init_schema


@pytest.fixture
def doctor_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "doctor.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        yield db_path


# ---------- check_monthly_budget_burn ----------

def test_budget_burn_passes_under_cap(doctor_db):
    """Spend at 50% of cap returns a passing CheckResult."""
    from scripts import doctor
    with sqlite3.connect(doctor_db) as conn:
        # 50% of $240 cap = $120
        conn.execute(
            "INSERT INTO costs (category, amount_usd) VALUES ('seedance_fast', 120.0)"
        )
        conn.commit()
    results = doctor.check_monthly_budget_burn()
    assert len(results) == 1
    assert results[0].ok is True
    assert "$120.00" in results[0].detail


def test_budget_burn_warns_near_cap(doctor_db):
    """Spend >= 85% of cap fails the check (warning band)."""
    from scripts import doctor
    with sqlite3.connect(doctor_db) as conn:
        conn.execute(
            "INSERT INTO costs (category, amount_usd) VALUES ('seedance_fast', 210.0)"
        )
        conn.commit()
    results = doctor.check_monthly_budget_burn()
    assert results[0].ok is False
    assert "approaching cap" in results[0].detail


def test_budget_burn_fails_over_cap(doctor_db):
    """Spend >= cap (but < hard_kill) fails with cap-over message."""
    from scripts import doctor
    with sqlite3.connect(doctor_db) as conn:
        conn.execute(
            "INSERT INTO costs (category, amount_usd) VALUES ('seedance_fast', 250.0)"
        )
        conn.commit()
    results = doctor.check_monthly_budget_burn()
    assert results[0].ok is False
    assert "OVER cap" in results[0].detail


def test_budget_burn_fails_over_hard_kill(doctor_db):
    """Spend >= hard_kill_switch_usd ($260) triggers the strongest fail."""
    from scripts import doctor
    with sqlite3.connect(doctor_db) as conn:
        conn.execute(
            "INSERT INTO costs (category, amount_usd) VALUES ('seedance_fast', 300.0)"
        )
        conn.commit()
    results = doctor.check_monthly_budget_burn()
    assert results[0].ok is False
    assert "OVER hard_kill_switch" in results[0].detail
    assert "MUST be paused" in results[0].detail


# ---------- check_strike_monitor ----------

def test_strike_monitor_passes_with_zero_strikes(doctor_db):
    from scripts import doctor
    results = doctor.check_strike_monitor()
    assert results[0].ok is True
    assert "0 unresolved" in results[0].detail


def test_strike_monitor_fails_on_unresolved_strike(doctor_db):
    """Per CLAUDE.md zero-strike posture — even one unresolved strike fails."""
    from scripts import doctor
    with sqlite3.connect(doctor_db) as conn:
        conn.execute(
            """
            INSERT INTO accounts (id, platform, role, active)
            VALUES ('tiktok_primary_1', 'tiktok', 'primary', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO strikes (account_id, platform, struck_at, strike_type, resolved)
            VALUES ('tiktok_primary_1', 'tiktok', datetime('now'), 'copyright', 0)
            """
        )
        conn.commit()
    results = doctor.check_strike_monitor()
    assert results[0].ok is False
    assert "1 unresolved" in results[0].detail


def test_strike_monitor_ignores_resolved_strikes(doctor_db):
    """Resolved strikes (resolved=1) should not fail the check."""
    from scripts import doctor
    with sqlite3.connect(doctor_db) as conn:
        conn.execute(
            """
            INSERT INTO accounts (id, platform, role, active)
            VALUES ('tiktok_primary_1', 'tiktok', 'primary', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO strikes (account_id, platform, struck_at, strike_type, resolved)
            VALUES ('tiktok_primary_1', 'tiktok', datetime('now'), 'copyright', 1)
            """
        )
        conn.commit()
    results = doctor.check_strike_monitor()
    assert results[0].ok is True


# ---------- check_account_warming ----------

def test_account_warming_fails_with_no_accounts(doctor_db):
    """An empty accounts table fails with an actionable message."""
    from scripts import doctor
    results = doctor.check_account_warming()
    assert results[0].ok is False
    assert "no rows in accounts table" in results[0].detail


def test_account_warming_passes_with_one_primary_two_warm_backups(doctor_db):
    from scripts import doctor
    with sqlite3.connect(doctor_db) as conn:
        for aid, role, warm in [
            ("tiktok_primary_1", "primary",     0),
            ("tiktok_backup_1",  "backup_warm", 1),
            ("tiktok_backup_2",  "backup_warm", 1),
        ]:
            conn.execute(
                """
                INSERT INTO accounts (id, platform, role, active, warm_eligible)
                VALUES (?, 'tiktok', ?, 1, ?)
                """,
                (aid, role, warm),
            )
        conn.commit()
    results = doctor.check_account_warming()
    assert len(results) == 1
    assert results[0].ok is True


def test_account_warming_fails_with_only_one_warm_backup(doctor_db):
    """Spec requires >=2 warm backups per platform; one is insufficient."""
    from scripts import doctor
    with sqlite3.connect(doctor_db) as conn:
        conn.execute(
            "INSERT INTO accounts (id, platform, role, active, warm_eligible) "
            "VALUES ('tiktok_primary_1', 'tiktok', 'primary', 1, 0)"
        )
        conn.execute(
            "INSERT INTO accounts (id, platform, role, active, warm_eligible) "
            "VALUES ('tiktok_backup_1', 'tiktok', 'backup_warm', 1, 1)"
        )
        conn.commit()
    results = doctor.check_account_warming()
    assert results[0].ok is False
    assert "warm_backups=1" in results[0].detail
