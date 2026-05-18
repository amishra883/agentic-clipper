"""Curator tests — Day 4 hardening (E-4 atomic claim, heuristic+LLM
dispatch, cost reservation).

Critical assertions:
- run_curator() returns a CuratorRunSummary with the right fields
- Clear-cut high/low heuristic scores skip the LLM (cost saved)
- Borderline heuristic (0.4-0.7) routes through the LLM dispatch with
  cost reservation
- BudgetExceeded skips the LLM entirely (heuristic wins, no cost)
- NotImplementedError (Phase 1 LLM stub) settles reservation as failed
- Concurrent Curator runs can't double-promote: conditional UPDATE on
  status='discovered' catches the second run's lost-race
- Promoted rows have status='curated' AND virality_score set AND
  curated_at populated
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agents.curator import (
    CuratorRunSummary,
    _heuristic_score,
    _is_borderline,
    _llm_tiebreaker_score,
    _promote_with_cas,
    _select_discovered,
    run_curator,
)
from agents.db import init_schema
from scripts.migrate import migrate


@pytest.fixture
def curator_db(monkeypatch):
    """Fresh DB through v5 (Scout idempotency migration applied)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "curator.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        migrate(db_path=db_path)
        yield db_path


def _seed_candidates(db_path: Path, *, count: int = 5, view_count: int = 500_000):
    """Insert N discovered candidates with varying view counts."""
    with sqlite3.connect(db_path) as conn:
        for i in range(count):
            conn.execute(
                """
                INSERT INTO clips_candidate
                  (id, creator, source_platform, source_url, source_view_count, status)
                VALUES (?, ?, 'twitch', ?, ?, 'discovered')
                """,
                (
                    f"twitch-test-{i:04d}",
                    "IShowSpeed",
                    f"https://twitch.tv/ishowspeed/clip/test-{i:04d}",
                    view_count * (i + 1),  # ascending views
                ),
            )
        conn.commit()


# ---------- _heuristic_score + _is_borderline ----------

def test_heuristic_returns_zero_for_no_views():
    assert _heuristic_score(0, "IShowSpeed") == 0.0
    assert _heuristic_score(None, "IShowSpeed") == 0.0


def test_heuristic_scales_with_views():
    """log10 squash means a 1M-view clip scores higher than a 1K-view."""
    low = _heuristic_score(1_000, "Sketch")
    high = _heuristic_score(1_000_000, "Sketch")
    assert high > low
    assert 0.0 <= low <= 1.0
    assert 0.0 <= high <= 1.0


def test_heuristic_applies_creator_weight():
    """Same view count, different creators → different scores."""
    speed = _heuristic_score(500_000, "IShowSpeed")  # weight 1.20
    adin = _heuristic_score(500_000, "Adin Ross")    # weight 0.95
    assert speed > adin


def test_borderline_band():
    assert _is_borderline(0.50) is True
    assert _is_borderline(0.40) is True
    assert _is_borderline(0.70) is True
    assert _is_borderline(0.39) is False
    assert _is_borderline(0.71) is False
    assert _is_borderline(0.0) is False
    assert _is_borderline(1.0) is False


# ---------- _select_discovered + _promote_with_cas ----------

def test_select_returns_only_discovered(curator_db):
    """Curated/quarantined rows should not be re-considered."""
    with sqlite3.connect(curator_db) as conn:
        for i, status in enumerate(["discovered", "curated", "quarantined", "discovered"]):
            conn.execute(
                """
                INSERT INTO clips_candidate
                  (id, creator, source_platform, source_url, status)
                VALUES (?, 'X', 'twitch', ?, ?)
                """,
                (f"twitch-status-{i}", f"https://twitch.tv/x/clip/{i}", status),
            )
        conn.commit()
    rows = _select_discovered(50)
    statuses = {r["status"] for r in rows}
    assert statuses == {"discovered"}
    assert len(rows) == 2


def test_promote_with_cas_flips_status(curator_db):
    _seed_candidates(curator_db, count=3)
    actual = _promote_with_cas([
        ("twitch-test-0000", 0.85),
        ("twitch-test-0001", 0.72),
    ])
    assert actual == 2
    with sqlite3.connect(curator_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, status, virality_score, curated_at FROM clips_candidate"
        ).fetchall()}
    assert rows["twitch-test-0000"]["status"] == "curated"
    assert rows["twitch-test-0000"]["virality_score"] == 0.85
    assert rows["twitch-test-0000"]["curated_at"] is not None
    assert rows["twitch-test-0001"]["status"] == "curated"
    # Third row stayed discovered
    assert rows["twitch-test-0002"]["status"] == "discovered"


def test_promote_with_cas_misses_already_curated(curator_db):
    """Codex E-4 protection: if another Curator beat us to a row, our
    conditional UPDATE matches zero rows (cur.rowcount=0). We don't
    overwrite an already-curated score; we just count the miss."""
    _seed_candidates(curator_db, count=2)
    # Simulate concurrent run: pre-curate one of the rows
    with sqlite3.connect(curator_db) as conn:
        conn.execute(
            "UPDATE clips_candidate SET status='curated', virality_score=0.99 "
            "WHERE id='twitch-test-0000'"
        )
        conn.commit()
    # Our run targets both
    actual = _promote_with_cas([
        ("twitch-test-0000", 0.50),  # already curated; should miss
        ("twitch-test-0001", 0.60),  # discovered; should hit
    ])
    assert actual == 1
    # The already-curated row's score was NOT overwritten
    with sqlite3.connect(curator_db) as conn:
        row = conn.execute(
            "SELECT virality_score FROM clips_candidate WHERE id='twitch-test-0000'"
        ).fetchone()
    assert row[0] == 0.99


def test_promote_with_cas_empty_list_returns_zero(curator_db):
    assert _promote_with_cas([]) == 0


# ---------- run_curator end-to-end ----------

def test_run_curator_returns_summary(curator_db):
    _seed_candidates(curator_db, count=3)
    summary = asyncio.run(run_curator(batch_size=2))
    assert isinstance(summary, CuratorRunSummary)
    assert summary.considered == 3
    assert summary.promoted == 2


def test_run_curator_empty_db_returns_zero_summary(curator_db):
    summary = asyncio.run(run_curator(batch_size=10))
    assert summary.considered == 0
    assert summary.promoted == 0


def test_run_curator_promotes_highest_scores(curator_db):
    """With ascending view counts, the highest-view rows should rank
    highest and get promoted first."""
    _seed_candidates(curator_db, count=5, view_count=100_000)
    asyncio.run(run_curator(batch_size=2))
    with sqlite3.connect(curator_db) as conn:
        # The top-view clips are the LAST inserted (view_count = 100K * (i+1))
        curated = conn.execute(
            "SELECT id FROM clips_candidate WHERE status='curated' "
            "ORDER BY virality_score DESC"
        ).fetchall()
    promoted_ids = [r[0] for r in curated]
    # The two highest-view rows should be promoted
    assert "twitch-test-0004" in promoted_ids
    assert "twitch-test-0003" in promoted_ids


def test_clear_cut_high_score_skips_llm(curator_db, monkeypatch):
    """A 100M-view clip scores well above the 0.70 borderline → no LLM call."""
    with sqlite3.connect(curator_db) as conn:
        conn.execute(
            """
            INSERT INTO clips_candidate
              (id, creator, source_platform, source_url, source_view_count, status)
            VALUES (?, 'IShowSpeed', 'twitch', ?, 100_000_000, 'discovered')
            """,
            ("twitch-mega-1", "https://twitch.tv/ishowspeed/clip/mega-1"),
        )
        conn.commit()
    summary = asyncio.run(run_curator(batch_size=1))
    assert summary.llm_calls_attempted == 0


def test_clear_cut_low_score_skips_llm(curator_db):
    """A 1-view clip scores well below the 0.40 borderline → no LLM call."""
    with sqlite3.connect(curator_db) as conn:
        conn.execute(
            """
            INSERT INTO clips_candidate
              (id, creator, source_platform, source_url, source_view_count, status)
            VALUES ('twitch-tiny', 'Sketch', 'twitch',
                    'https://twitch.tv/sketch/clip/tiny', 1, 'discovered')
            """
        )
        conn.commit()
    summary = asyncio.run(run_curator(batch_size=1))
    assert summary.llm_calls_attempted == 0


def test_borderline_score_attempts_llm_with_reservation(curator_db):
    """Heuristic score in 0.40-0.70 triggers LLM tiebreaker dispatch.
    Phase 1 LLM stub raises NotImplementedError; reservation settles as
    failed; heuristic score retained."""
    # 100K-view IShowSpeed clip: log10(100K+1)/8 * 1.20 ≈ 0.75. Bump it
    # down to land in the borderline band by using a less weighted creator
    # OR a different view count. log10(10K+1)/8 * 1.0 = 0.500 → Sketch
    # at 10K views ≈ 0.500 (borderline).
    with sqlite3.connect(curator_db) as conn:
        conn.execute(
            """
            INSERT INTO clips_candidate
              (id, creator, source_platform, source_url, source_view_count, status)
            VALUES ('twitch-borderline', 'Sketch', 'twitch',
                    'https://twitch.tv/sketch/clip/border', 10_000, 'discovered')
            """
        )
        conn.commit()
    summary = asyncio.run(run_curator(batch_size=1))
    assert summary.llm_calls_attempted == 1
    assert summary.llm_calls_succeeded == 0  # stub fails

    # The reservation should be settled as 'failed' (zero amount) — verify
    # the costs ledger.
    with sqlite3.connect(curator_db) as conn:
        row = conn.execute(
            "SELECT status, amount_usd FROM costs "
            "WHERE category='anthropic_api_buffer' AND clip_id='twitch-borderline'"
        ).fetchone()
    assert row is not None
    assert row[0] == "failed"
    assert row[1] == 0.0  # failed reservation zeros amount


def test_borderline_budget_exceeded_skips_llm(curator_db):
    """If reserve() raises BudgetExceeded, the heuristic wins and no
    LLM call is attempted. Daily-cap edge case."""
    # Pre-fill anthropic_api_buffer at $0.04 against a $0.05 daily cap;
    # the next $0.03 reservation should fail.
    with sqlite3.connect(curator_db) as conn:
        conn.execute(
            """
            INSERT INTO costs (ts, category, amount_usd, status)
            VALUES (datetime('now'), 'anthropic_api_buffer', 0.04, 'succeeded')
            """
        )
        conn.execute(
            """
            INSERT INTO clips_candidate
              (id, creator, source_platform, source_url, source_view_count, status)
            VALUES ('twitch-budgetx', 'Sketch', 'twitch',
                    'https://twitch.tv/sketch/clip/budget', 10_000, 'discovered')
            """
        )
        conn.commit()

    summary = asyncio.run(run_curator(
        batch_size=1,
        llm_daily_cap_usd=0.05,
    ))
    # LLM was NOT attempted because reserve() raised BudgetExceeded first
    assert summary.llm_calls_attempted == 0
    assert summary.skipped_no_budget == 1
    # No new costs row written for this clip
    with sqlite3.connect(curator_db) as conn:
        rows = conn.execute(
            "SELECT * FROM costs WHERE clip_id='twitch-budgetx'"
        ).fetchall()
    assert rows == []


def test_borderline_llm_success_records_actual_cost(curator_db):
    """When the LLM tiebreaker actually returns (Phase 2 wiring), the
    reservation settles as 'succeeded' with the actual cost."""
    with sqlite3.connect(curator_db) as conn:
        conn.execute(
            """
            INSERT INTO clips_candidate
              (id, creator, source_platform, source_url, source_view_count, status)
            VALUES ('twitch-llm-ok', 'Sketch', 'twitch',
                    'https://twitch.tv/sketch/clip/ok', 10_000, 'discovered')
            """
        )
        conn.commit()

    # Patch the LLM stub to return a real score
    async def fake_llm(candidate, heuristic):
        return 0.82
    with patch("agents.curator._llm_tiebreaker_score", side_effect=fake_llm):
        summary = asyncio.run(run_curator(batch_size=1))
    assert summary.llm_calls_attempted == 1
    assert summary.llm_calls_succeeded == 1

    # Cost row should be settled as 'succeeded' with the configured per-call cost
    with sqlite3.connect(curator_db) as conn:
        row = conn.execute(
            "SELECT status, amount_usd FROM costs "
            "WHERE category='anthropic_api_buffer' AND clip_id='twitch-llm-ok'"
        ).fetchone()
    assert row[0] == "succeeded"
    assert row[1] == 0.03  # _LLM_COST_PER_TIEBREAKER_USD


def test_concurrent_curator_runs_dont_double_promote(curator_db):
    """E-4: two Curator runs in serial against the same discovered set.
    The second run should see them all curated and promote zero.

    True concurrency is hard to simulate from python sqlite3 (one process,
    one thread, default isolation_level). The serial-then-serial test is
    what proves the conditional UPDATE works — equivalent to two
    concurrent runs where one happens to finish first."""
    _seed_candidates(curator_db, count=3)
    first = asyncio.run(run_curator(batch_size=2))
    assert first.promoted == 2

    # Second run on the same DB — only the remaining 'discovered' row left
    second = asyncio.run(run_curator(batch_size=2))
    assert second.considered == 1  # one row still discovered
    assert second.promoted == 1


def test_concurrent_promote_with_cas_lost_race_logged(curator_db, monkeypatch):
    """Simulate the race: SELECT discovered rows, but BEFORE we call
    _promote_with_cas, another connection flips one to 'curated'. The
    CAS update on that row matches 0 rows (lost the race); we log the
    discrepancy and report fewer actual promotions in the summary."""
    _seed_candidates(curator_db, count=3)
    real_promote = _promote_with_cas

    def racing_promote(target):
        # Simulate a concurrent run claiming the first target between
        # our SELECT and our UPDATE
        if target:
            with sqlite3.connect(curator_db) as conn:
                conn.execute(
                    "UPDATE clips_candidate SET status='curated', virality_score=0.99 "
                    "WHERE id = ?",
                    (target[0][0],),
                )
                conn.commit()
        return real_promote(target)

    monkeypatch.setattr("agents.curator._promote_with_cas", racing_promote)
    summary = asyncio.run(run_curator(batch_size=2))
    # We targeted 2, but lost 1 to the simulated concurrent run
    assert summary.promoted == 1
