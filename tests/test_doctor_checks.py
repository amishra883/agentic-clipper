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


# ---------- _ping_https / check_live_apis ----------

def _fake_urlopen_factory(status_code: int):
    """Return a fake urlopen that yields a context manager with .status."""
    class _FakeResp:
        def __init__(self, code: int) -> None:
            self.status = code
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def _fake(req, timeout=None):
        return _FakeResp(status_code)
    return _fake


def test_ping_https_passes_on_2xx(monkeypatch):
    """200/204 from the host means TLS + edge reachable."""
    from scripts import doctor
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_factory(200))
    ok, detail = doctor._ping_https("example.com")
    assert ok is True
    assert "HTTP 200" in detail


def test_ping_https_passes_on_401(monkeypatch):
    """401 = the request got to the API and got rejected for missing auth.
    For a liveness check that's exactly what we want — the path the request
    is taking is healthy, the auth simply isn't attached."""
    from scripts import doctor
    import urllib.request, urllib.error
    def _raises_401(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", _raises_401)
    ok, detail = doctor._ping_https("api.example.com")
    assert ok is True
    assert "HTTP 401" in detail


def test_ping_https_fails_on_unexpected_5xx(monkeypatch):
    """500 is not in the accept_status default; FAIL."""
    from scripts import doctor
    import urllib.request, urllib.error
    def _raises_500(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Internal", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", _raises_500)
    ok, detail = doctor._ping_https("api.example.com")
    assert ok is False
    assert "HTTP 500" in detail


def test_ping_https_fails_on_network_error(monkeypatch):
    """DNS / socket / TLS failures yield a network-error FAIL."""
    from scripts import doctor
    import urllib.request, socket
    def _raises_gaierror(req, timeout=None):
        raise socket.gaierror(8, "nodename nor servname provided")
    monkeypatch.setattr(urllib.request, "urlopen", _raises_gaierror)
    ok, detail = doctor._ping_https("nonexistent.invalid")
    assert ok is False
    assert "network error" in detail
    assert "gaierror" in detail


def test_check_live_apis_returns_five_results(monkeypatch):
    """All five providers report (YouTube, Instagram, Atlas, fal.ai, TikTok-manual)."""
    from scripts import doctor
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_factory(200))
    results = doctor.check_live_apis()
    names = [r.name for r in results]
    assert any("YouTube" in n for n in names)
    assert any("Instagram" in n for n in names)
    assert any("Atlas Cloud" in n for n in names)
    assert any("fal.ai" in n for n in names)
    assert any("TikTok manual-mode" in n for n in names)
    # TikTok manual-mode is always informational-pass; the rest are network-driven
    tiktok = next(r for r in results if "TikTok" in r.name)
    assert tiktok.ok is True


def test_check_live_apis_marks_unreachable_as_fail(monkeypatch):
    """If every HEAD raises a network error, four of the five fail (TikTok
    manual-mode stays informational-pass)."""
    from scripts import doctor
    import urllib.request, socket
    def _always_fails(req, timeout=None):
        raise socket.gaierror(8, "DNS lookup failed")
    monkeypatch.setattr(urllib.request, "urlopen", _always_fails)
    results = doctor.check_live_apis()
    failed = [r for r in results if not r.ok]
    assert len(failed) == 4  # YouTube, Instagram, Atlas, fal.ai
    assert all("network error" in r.detail for r in failed)


# ---------- check_schema_version ----------

def test_schema_version_passes_when_db_matches_latest_migration(doctor_db):
    """Fresh DB has schema_version=1 baseline. With migrations/001_smoke_test.sql
    on disk declaring VERSION 2, doctor should FAIL (db v1 < migrations v2) until
    `make migrate` runs."""
    from scripts import doctor
    # Fresh DB is at v1; the live migrations/ dir has a smoke-test at v2.
    # So this should report FAIL with the "run make migrate" message.
    results = doctor.check_schema_version()
    assert len(results) == 1
    assert results[0].ok is False
    assert "run `make migrate`" in results[0].detail


def test_schema_version_passes_after_migrate(doctor_db):
    """After applying the smoke-test migration, doctor should be green."""
    from scripts import doctor
    from scripts.migrate import migrate
    # Apply the live migrations
    migrate(db_path=doctor_db)
    results = doctor.check_schema_version()
    assert len(results) == 1
    assert results[0].ok is True
    assert "matches latest migration" in results[0].detail


def test_schema_version_fails_when_db_missing(monkeypatch, tmp_path):
    """If data/main.db doesn't exist, the check should fail with an
    actionable message pointing at init-db + migrate."""
    monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(tmp_path / "nope.db"))
    from scripts import doctor
    results = doctor.check_schema_version()
    assert results[0].ok is False
    assert "make init-db" in results[0].detail


def test_schema_version_fails_when_db_ahead_of_code(doctor_db, monkeypatch, tmp_path):
    """If the DB has been migrated past the highest file in migrations/
    (operator on an older code commit), doctor should warn — this is the
    scenario where someone rolls back a deploy but forgets the DB."""
    from scripts import doctor
    import sqlite3
    # Hand-write a fake "v99" version into the DB
    with sqlite3.connect(doctor_db) as conn:
        conn.execute("INSERT INTO schema_version (version) VALUES (99)")
        conn.commit()
    results = doctor.check_schema_version()
    assert results[0].ok is False
    assert "older code commit" in results[0].detail
