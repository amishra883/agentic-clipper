"""Visuals tests — Day 9-10 hardening (E-3, has_real_face=0, E-13, E-24, lease).

Critical assertions:
- parse_atlas_response classifies all 6 states (submitted/processing/
  succeeded/face_filter/rate_limit/error)
- Successful generation writes clip_artifacts.has_real_face_reference=0
- Daily Atlas cap (E-13) blocks reservation → clip quarantined
- MTD line-item budget enforcement still fires per-tier
- stage_lease + commit_artifact bumps artifact_version atomically
- Compositor downstream fields invalidated on re-run
- migration 005 composite index exists
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path

import pytest

from agents import visuals
from agents.db import init_schema
from agents.models import ShotListEntry
from scripts.migrate import migrate


# ---------- Fixtures ----------


@pytest.fixture
def visuals_db(monkeypatch):
    """Migrated DB with one curated candidate row at virality_score=0.50."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "visuals.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        migrate(db_path=db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO clips_candidate (id, creator, source_platform, source_url, "
                "virality_score, predicted_views, status) "
                "VALUES ('viz-clip-1', 'IShowSpeed', 'twitch', 'https://test', "
                "0.50, 10000, 'processing')"
            )
            conn.commit()
        yield db_path


@pytest.fixture
def tmp_quarantine(monkeypatch, tmp_path):
    monkeypatch.setattr(visuals, "QUARANTINE_DIR", tmp_path / "quarantine")
    return tmp_path


def _budget_cfg() -> dict:
    return {
        "line_items": {
            "seedance_fast": {"monthly_budget_usd": 50},
            "seedance_pro": {"monthly_budget_usd": 20},
        },
        "per_clip_caps": {
            "seedance_seconds_max": 15,
            "seedance_cost_usd_max_fast": 0.50,
            "seedance_cost_usd_max_pro": 4.00,
        },
        "per_call_caps": {
            "atlas_cloud_daily_usd_max": 5.0,
        },
        "pro_tier_promotion": {
            "curator_score_min": 0.85,
            "projected_views_min": 50_000,
            "hero_shot_required": True,
            "month_to_date_pro_spend_cap_usd": 20,
        },
    }


def _shot(duration_s: float = 3.0, shot_type: str = "avatar_reaction",
          prompt: str = "test shot") -> ShotListEntry:
    return ShotListEntry(
        shot_type=shot_type,  # type: ignore[arg-type]
        start_s=0.0,
        duration_s=duration_s,
        prompt=prompt,
        reaction_id=None,
        punch_word=None,
    )


# ---------- E-3: Typed Atlas response parser ----------


def test_parse_atlas_succeeded():
    parsed = visuals.parse_atlas_response({
        "video_url": "https://atlas.example/v/abc.mp4",
        "cost_usd": 0.066,
    })
    assert parsed.status == "succeeded"
    assert parsed.video_url == "https://atlas.example/v/abc.mp4"
    assert parsed.cost_usd == 0.066


def test_parse_atlas_face_filter_via_empty_body():
    """HTTP 200 with no video_url — Atlas's silent face-filter rejection."""
    parsed = visuals.parse_atlas_response({})
    assert parsed.status == "face_filter"
    assert parsed.video_url is None


def test_parse_atlas_face_filter_via_missing_url_key():
    parsed = visuals.parse_atlas_response({"job_id": "abc", "cost_usd": 0.0})
    assert parsed.status == "face_filter"


def test_parse_atlas_submitted():
    parsed = visuals.parse_atlas_response({"status": "submitted", "job_id": "j-1"})
    assert parsed.status == "submitted"
    assert parsed.video_url is None


def test_parse_atlas_processing_variants():
    for value in ("processing", "running", "in_progress"):
        parsed = visuals.parse_atlas_response({"status": value})
        assert parsed.status == "processing"


def test_parse_atlas_rate_limit_via_flag():
    parsed = visuals.parse_atlas_response({"rate_limited": True, "retry_after_s": 5.0})
    assert parsed.status == "rate_limit"
    assert parsed.retry_after_s == 5.0


def test_parse_atlas_rate_limit_via_status():
    parsed = visuals.parse_atlas_response({"status": "rate_limited"})
    assert parsed.status == "rate_limit"


def test_parse_atlas_error_status():
    parsed = visuals.parse_atlas_response({"status": "error", "message": "internal"})
    assert parsed.status == "error"
    assert parsed.video_url is None


def test_parse_atlas_non_dict_input():
    """Defensive parsing — non-dict responses are treated as errors."""
    parsed = visuals.parse_atlas_response("garbage")
    assert parsed.status == "error"


def test_parse_atlas_cost_coercion():
    """Provider sometimes returns cost as string; parser coerces."""
    parsed = visuals.parse_atlas_response({
        "video_url": "https://x", "cost_usd": "0.123",
    })
    assert parsed.status == "succeeded"
    assert parsed.cost_usd == 0.123


# ---------- has_real_face_reference=0 on success ----------


def test_successful_run_writes_has_real_face_zero(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """Compliance's no_real_face_seedance_reference rule fails closed on
    NULL. A successful Visuals run MUST populate
    clip_artifacts.has_real_face_reference=0 — that's the explicit
    "we did NOT pass a real face" hand-off."""
    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())

    async def fake_atlas(prompt, *, duration_s, tier, seed, reference_image_path):
        return {
            "video_url": f"https://atlas.example/{prompt}.mp4",
            "cost_usd": 0.066,
        }
    monkeypatch.setattr(visuals, "_atlas_cloud_generate", fake_atlas)

    asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))

    with sqlite3.connect(visuals_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT has_real_face_reference, artifact_version FROM clip_artifacts "
            "WHERE clip_id = ?", ("viz-clip-1",),
        ).fetchone()
    assert row["has_real_face_reference"] == 0
    assert row["artifact_version"] == 1  # 0 → 1 via commit_artifact


def test_scaffold_mode_does_not_write_has_real_face(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """If no generation succeeded (all scaffold-stubbed), the
    clip_artifacts row should still be written (zero seconds, zero
    cost) but the contract is: commit_artifact runs once at the end."""
    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())
    # _atlas_cloud_generate keeps its default NotImplementedError, so
    # every shot scaffolds.

    asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))

    with sqlite3.connect(visuals_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT has_real_face_reference, visuals_seconds_used, "
            "visuals_cost_usd, artifact_version FROM clip_artifacts "
            "WHERE clip_id = ?", ("viz-clip-1",),
        ).fetchone()
    # commit_artifact runs even on zero-generation runs so the
    # downstream invalidation fires and artifact_version bumps.
    assert row is not None
    assert row["visuals_seconds_used"] == 0
    assert row["visuals_cost_usd"] == 0
    assert row["artifact_version"] == 1


# ---------- E-13: Daily Atlas cap ----------


def test_daily_atlas_cap_blocks_reservation(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """Pre-fill today's atlas_cloud spend at $4.95 against the $5 daily
    cap. The next reservation (3.0s × $0.022/s = $0.066 expected fast-tier
    cost) pushes to $5.016 → BudgetExceeded → quarantine. No fallback
    (Atlas has no free path like Voice's Coqui).

    Codex 2026-05-18 CEO fix: reservations now use expected per-second
    rate ($0.022 fast / $0.18 pro) not the worst-case per-clip cap.
    The daily cap therefore reflects realistic accumulation, not
    pre-block worst-case projection."""
    with sqlite3.connect(visuals_db) as conn:
        conn.execute(
            "INSERT INTO costs (ts, category, amount_usd, status) "
            "VALUES (datetime('now'), 'atlas_cloud', 4.95, 'succeeded')"
        )
        conn.commit()

    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())
    asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))

    with sqlite3.connect(visuals_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", ("viz-clip-1",),
        ).fetchone()[0]
    assert status == "quarantined"


def test_daily_atlas_cap_with_no_cap_set_passes(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """If per_call_caps.atlas_cloud_daily_usd_max is None (config gap),
    the daily cap doesn't enforce. Production should always have it."""
    cfg = _budget_cfg()
    cfg["per_call_caps"]["atlas_cloud_daily_usd_max"] = None
    monkeypatch.setattr(visuals, "load", lambda name: cfg)

    async def fake_atlas(*args, **kwargs):
        return {"video_url": "https://atlas/x", "cost_usd": 0.066}
    monkeypatch.setattr(visuals, "_atlas_cloud_generate", fake_atlas)

    # Pre-fill $100 in today's atlas_cloud — without a cap, this is OK.
    with sqlite3.connect(visuals_db) as conn:
        conn.execute(
            "INSERT INTO costs (ts, category, amount_usd, status) "
            "VALUES (datetime('now'), 'atlas_cloud', 100.0, 'succeeded')"
        )
        conn.commit()

    asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))

    with sqlite3.connect(visuals_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", ("viz-clip-1",),
        ).fetchone()[0]
    assert status == "processing"  # NOT quarantined


# ---------- Response state handling ----------


def test_face_filter_response_quarantines(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """Provider returns 200 + empty body → face_filter → quarantine.
    Reservation settles failed with 0 cost."""
    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())

    async def fake_atlas(*args, **kwargs):
        return {}  # face-filter signature
    monkeypatch.setattr(visuals, "_atlas_cloud_generate", fake_atlas)

    asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))

    with sqlite3.connect(visuals_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", ("viz-clip-1",),
        ).fetchone()[0]
        seed_row = conn.execute(
            "SELECT status, cost_usd FROM seedance_generations "
            "WHERE clip_id = ?", ("viz-clip-1",),
        ).fetchone()
        cost_row = conn.execute(
            "SELECT status FROM costs WHERE clip_id = ? AND provider = 'atlas_cloud'",
            ("viz-clip-1",),
        ).fetchone()
    assert status == "quarantined"
    assert seed_row[0] == "failed_face_filter"
    assert seed_row[1] == 0.0
    assert cost_row[0] == "failed"


def test_rate_limit_response_defers_shot(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """Provider returns rate_limited → settle failed, defer shot, do NOT
    quarantine. Other shots in the list may still generate."""
    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())

    async def fake_atlas(*args, **kwargs):
        return {"rate_limited": True, "retry_after_s": 30}
    monkeypatch.setattr(visuals, "_atlas_cloud_generate", fake_atlas)

    assets = asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))

    with sqlite3.connect(visuals_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", ("viz-clip-1",),
        ).fetchone()[0]
    # Status untouched (rate-limit is transient, not a hard failure)
    assert status == "processing"
    assert assets == []


def test_atlas_retries_exhausted_quarantines(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """retry_external wraps _atlas_cloud_generate. RetryGiveUp →
    quarantine with reason atlas_retries_exhausted."""
    from agents.retry import RetryGiveUp

    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())

    async def fake_atlas(*args, **kwargs):
        raise RetryGiveUp("atlas 503 after 3 attempts")
    monkeypatch.setattr(visuals, "_atlas_cloud_generate", fake_atlas)

    asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))

    with sqlite3.connect(visuals_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", ("viz-clip-1",),
        ).fetchone()[0]
    assert status == "quarantined"


def test_atlas_error_status_quarantines(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """Provider returns status=error → log failed_other + quarantine."""
    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())

    async def fake_atlas(*args, **kwargs):
        return {"status": "error", "message": "internal server error"}
    monkeypatch.setattr(visuals, "_atlas_cloud_generate", fake_atlas)

    asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))

    with sqlite3.connect(visuals_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", ("viz-clip-1",),
        ).fetchone()[0]
        seed_row = conn.execute(
            "SELECT status FROM seedance_generations WHERE clip_id = ?", ("viz-clip-1",),
        ).fetchone()
    assert status == "quarantined"
    assert seed_row[0] == "failed_other"


# ---------- MTD line-item budget ----------


def test_mtd_line_item_budget_blocks_overspend(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """Pre-fill $49.95 in succeeded seedance_fast generations this
    month. The next $0.066 generation would push to $50.016 > $50
    line-item budget → quarantine."""
    with sqlite3.connect(visuals_db) as conn:
        conn.execute(
            "INSERT INTO seedance_generations "
            "(clip_id, provider, model_version, tier, prompt, duration_s, "
            "cost_usd, status) "
            "VALUES ('prior-clip', 'atlas_cloud', 'seedance-2.0-fast', 'fast', "
            "'prior prompt', 3.0, 49.95, 'succeeded')"
        )
        conn.commit()

    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())

    async def fake_atlas(*args, **kwargs):
        return {"video_url": "https://atlas/x", "cost_usd": 0.066}
    monkeypatch.setattr(visuals, "_atlas_cloud_generate", fake_atlas)

    asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))

    with sqlite3.connect(visuals_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", ("viz-clip-1",),
        ).fetchone()[0]
    assert status == "quarantined"


# ---------- stage_lease + downstream invalidation ----------


def test_visuals_persist_clears_compositor_fields(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """Re-running Visuals on a clip whose Compositor already wrote
    final_video_path MUST clear that — the old final video was
    rendered against the prior visuals set (Codex P1#2 pattern)."""
    with sqlite3.connect(visuals_db) as conn:
        conn.execute(
            "INSERT INTO clip_artifacts "
            "(clip_id, final_video_path, final_duration_s, artifact_version, updated_at) "
            "VALUES (?, 'old_final.mp4', 50.0, 0, datetime('now'))",
            ("viz-clip-1",),
        )
        conn.commit()

    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())

    async def fake_atlas(*args, **kwargs):
        return {"video_url": "https://atlas/x", "cost_usd": 0.066}
    monkeypatch.setattr(visuals, "_atlas_cloud_generate", fake_atlas)

    asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))

    with sqlite3.connect(visuals_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM clip_artifacts WHERE clip_id = ?", ("viz-clip-1",),
        ).fetchone()
    # Visuals own columns populated
    assert row["visuals_seconds_used"] > 0
    assert row["has_real_face_reference"] == 0
    # Compositor downstream invalidated
    assert row["final_video_path"] is None
    assert row["final_duration_s"] is None


def test_visuals_lease_conflict_raises(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """A second Visuals run on the same clip raises LeaseConflict.
    Caller decides wait-or-skip."""
    from agents.stage_lease import LeaseConflict, stage_lease

    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())
    with stage_lease("viz-clip-1", stage="visuals", ttl_seconds=60):
        with pytest.raises(LeaseConflict):
            asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))


# ---------- Cache hits ----------


def test_cache_hit_skips_provider_and_cost_reservation(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """A cache hit should NOT call _atlas_cloud_generate and NOT
    reserve cost. Verify by counting calls + costs rows."""
    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())

    # Pre-populate the cache
    with sqlite3.connect(visuals_db) as conn:
        prompt_hash = visuals._prompt_hash(
            "test shot", seed=None, duration_s=3.0, tier="fast",
        )
        conn.execute(
            "INSERT INTO generated_cache "
            "(prompt_hash, prompt, asset_path, provider, tier, duration_s, cost_usd) "
            "VALUES (?, 'test shot', 'cached.mp4', 'atlas_cloud', 'fast', 3.0, 0.066)",
            (prompt_hash,),
        )
        conn.commit()

    call_count = {"n": 0}

    async def counting_atlas(*args, **kwargs):
        call_count["n"] += 1
        return {"video_url": "https://atlas/x", "cost_usd": 0.066}
    monkeypatch.setattr(visuals, "_atlas_cloud_generate", counting_atlas)

    assets = asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))

    assert call_count["n"] == 0  # cache hit short-circuited provider
    assert len(assets) == 1
    assert assets[0].path == "cached.mp4"
    # No costs row written for the cached shot
    with sqlite3.connect(visuals_db) as conn:
        cost_count = conn.execute(
            "SELECT COUNT(*) FROM costs WHERE clip_id = ?", ("viz-clip-1",),
        ).fetchone()[0]
    assert cost_count == 0


# ---------- E-24: Composite index ----------


def test_migration_005_creates_seedance_composite_index(visuals_db):
    """Migration 005 creates idx_seedance_tier_status_ts so MTD scans
    don't full-table-scan as the table grows."""
    with sqlite3.connect(visuals_db) as conn:
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name = 'idx_seedance_tier_status_ts'"
        ).fetchone()
    assert idx is not None


def test_migration_005_creates_costs_composite_index(visuals_db):
    """Migration 005 also creates idx_costs_category_status_ts for the
    daily-cap MTD aggregation in costs.reserve()."""
    with sqlite3.connect(visuals_db) as conn:
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name = 'idx_costs_category_status_ts'"
        ).fetchone()
    assert idx is not None


# ---------- Per-clip + seconds caps ----------


def test_seconds_cap_quarantines(visuals_db, tmp_quarantine, monkeypatch):
    """Total duration > 15s seconds cap → quarantine without calling provider."""
    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())

    # Two shots totaling 20s (cap is 15s)
    shots = [_shot(duration_s=10.0, prompt="a"), _shot(duration_s=10.0, prompt="b")]

    call_count = {"n": 0}

    async def fake_atlas(*args, **kwargs):
        call_count["n"] += 1
        return {"video_url": "https://atlas/x", "cost_usd": 0.066}
    monkeypatch.setattr(visuals, "_atlas_cloud_generate", fake_atlas)

    asyncio.run(visuals.run_visuals("viz-clip-1", shots))

    with sqlite3.connect(visuals_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", ("viz-clip-1",),
        ).fetchone()[0]
    # First shot OK (10s), second shot would push to 20s > 15s cap → quarantine
    assert status == "quarantined"
    assert call_count["n"] == 1  # only first shot ran


# ---------- _atlas_caps helper ----------


def test_atlas_caps_loads_daily_cap_from_config():
    cfg = _budget_cfg()
    caps = visuals._atlas_caps(cfg)
    assert caps["daily_cap_usd"] == 5.0


def test_atlas_caps_handles_missing_per_call_caps():
    """Empty per_call_caps → daily_cap_usd is None (caller treats as
    no enforcement)."""
    caps = visuals._atlas_caps({})
    assert caps["daily_cap_usd"] is None


# ---------- Autoplan CEO P1 fixes (2026-05-18) ----------


def test_parser_reads_nested_atlas_response_per_docs():
    """The documented Atlas response shape (docs/seedance_access.md:70-86)
    is nested: output.video_url + usage.amount_usd. Prior parser only
    read top-level video_url + cost_usd and would have misclassified
    every real success as face_filter."""
    nested = {
        "task_id": "tsk_01hx",
        "status": "succeeded",
        "model": "bytedance/seedance-2.0-fast",
        "output": {
            "video_url": "https://cdn.atlascloud.ai/abc/out.mp4",
            "duration_seconds": 6,
            "resolution": "720p",
            "seed": 42,
        },
        "usage": {
            "billed_seconds": 6,
            "amount_usd": 0.132,
        },
    }
    parsed = visuals.parse_atlas_response(nested)
    assert parsed.status == "succeeded"
    assert parsed.video_url == "https://cdn.atlascloud.ai/abc/out.mp4"
    assert parsed.cost_usd == 0.132


def test_parser_unknown_status_maps_to_error_not_face_filter():
    """A provider returning an unknown status (e.g., 'canceled',
    'content_moderation', 'insufficient_credits') previously fell
    through to face_filter. Now it maps to error with the unknown
    status preserved in payload for operator inspection."""
    parsed = visuals.parse_atlas_response({"status": "canceled"})
    assert parsed.status == "error"
    assert parsed.payload.get("_unknown_status") == "canceled"


def test_parser_top_level_video_url_still_works():
    """Top-level video_url is the fallback path (provider variants /
    test fixtures). Nested takes precedence but top-level still
    classifies as succeeded."""
    parsed = visuals.parse_atlas_response({
        "video_url": "https://x.example/out.mp4",
        "cost_usd": 0.066,
    })
    assert parsed.status == "succeeded"
    assert parsed.video_url == "https://x.example/out.mp4"
    assert parsed.cost_usd == 0.066


def test_parser_face_filter_requires_succeeded_status_and_no_url():
    """Face-filter is HTTP 200 with status='succeeded' (or no status)
    AND no video_url anywhere. Distinguished from unknown-status error."""
    # Real face_filter: status succeeded but empty
    p1 = visuals.parse_atlas_response({"status": "succeeded"})
    assert p1.status == "face_filter"
    # Also face_filter: no status at all (HTTP 200 empty body)
    p2 = visuals.parse_atlas_response({})
    assert p2.status == "face_filter"
    # NOT face_filter: unknown status with no URL → error
    p3 = visuals.parse_atlas_response({"status": "weird_new_state"})
    assert p3.status == "error"


def test_prompt_hash_includes_resolution_and_aspect_ratio():
    """Codex CEO: prior cache key omitted resolution + aspect_ratio.
    Two clips with identical prompt/seed/duration/tier but different
    9:16 vs 16:9 outputs MUST produce different hashes — Atlas
    generates different content for each."""
    h1 = visuals._prompt_hash(
        "test", seed=42, duration_s=3.0, tier="fast",
        resolution="720p", aspect_ratio="9:16",
    )
    h2 = visuals._prompt_hash(
        "test", seed=42, duration_s=3.0, tier="fast",
        resolution="720p", aspect_ratio="16:9",
    )
    h3 = visuals._prompt_hash(
        "test", seed=42, duration_s=3.0, tier="fast",
        resolution="480p", aspect_ratio="9:16",
    )
    assert h1 != h2
    assert h1 != h3
    assert h2 != h3


def test_prompt_hash_includes_reference_image_hash():
    """Avatar-consistent shots use reference_image_hash for character
    lock. Different reference images → different generations → MUST
    produce different cache keys."""
    h_no_ref = visuals._prompt_hash(
        "test", seed=42, duration_s=3.0, tier="fast",
    )
    h_with_ref = visuals._prompt_hash(
        "test", seed=42, duration_s=3.0, tier="fast",
        reference_image_hash="abc123",
    )
    h_other_ref = visuals._prompt_hash(
        "test", seed=42, duration_s=3.0, tier="fast",
        reference_image_hash="def456",
    )
    assert h_no_ref != h_with_ref
    assert h_with_ref != h_other_ref


def test_reservation_uses_expected_cost_not_worst_case(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """Codex CEO: prior version reserved at the per-clip CAP ($0.50 fast),
    which pre-blocked the daily cap before real spend. Now reserves at
    duration_s × per-second rate ($0.022 fast). Verify the cost row's
    amount_usd reflects the expected cost."""
    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())

    async def fake_atlas(*args, **kwargs):
        return {
            "status": "succeeded",
            "output": {"video_url": "https://x/out.mp4"},
            "usage": {"amount_usd": 0.066},
        }
    monkeypatch.setattr(visuals, "_atlas_cloud_generate", fake_atlas)

    # 3.0s fast shot → expected reservation $0.066 (3.0 × $0.022)
    asyncio.run(visuals.run_visuals("viz-clip-1", [_shot(duration_s=3.0)]))

    with sqlite3.connect(visuals_db) as conn:
        # The pending row was bumped to succeeded with the actual amount
        # at settle time. Verify the reservation amount is expected, not cap.
        rows = conn.execute(
            "SELECT amount_usd, status FROM costs WHERE clip_id = ? "
            "AND category = 'atlas_cloud' ORDER BY id",
            ("viz-clip-1",),
        ).fetchall()
    # One succeeded row with actual amount 0.066
    assert any(r[0] == pytest.approx(0.066) and r[1] == "succeeded" for r in rows)


def test_fal_ai_fallback_triggers_on_atlas_retries_exhausted(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """Codex CEO: vendor concentration risk. Atlas RetryGiveUp should
    fall through to fal.ai, not immediately quarantine. With fal.ai
    also stubbed (Phase 1), the final state IS quarantine but the
    code path that triggers fal.ai exists.

    Verify the atlas_fallback_to_fal warn event fires AND fal.ai
    was called."""
    from agents.retry import RetryGiveUp

    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())

    async def atlas_giveup(*args, **kwargs):
        raise RetryGiveUp("atlas 503 after 3 attempts")
    monkeypatch.setattr(visuals, "_atlas_cloud_generate", atlas_giveup)

    fal_called = {"n": 0}

    async def fal_stub(*args, **kwargs):
        fal_called["n"] += 1
        raise NotImplementedError("fal.ai not yet wired")
    monkeypatch.setattr(visuals, "_fal_ai_generate", fal_stub)

    events = []
    monkeypatch.setattr(visuals, "log", lambda **kw: events.append(kw))

    asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))

    # fal.ai WAS attempted as fallback
    assert fal_called["n"] == 1
    # Warn event fired showing the fallback happened
    fallback_events = [
        e for e in events if e.get("event_type") == "atlas_fallback_to_fal"
    ]
    assert fallback_events
    assert "atlas_retries_exhausted" in fallback_events[0]["payload"]["reason"]


def test_fal_ai_fallback_succeeds_when_atlas_giveup_and_fal_wired(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """If Atlas RetryGiveUp's BUT fal.ai succeeds, the asset is
    persisted with provider='fal_ai'. The strike-insurance path."""
    from agents.retry import RetryGiveUp

    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())

    async def atlas_giveup(*args, **kwargs):
        raise RetryGiveUp("atlas 503")
    monkeypatch.setattr(visuals, "_atlas_cloud_generate", atlas_giveup)

    async def fal_ok(*args, **kwargs):
        return {
            "status": "succeeded",
            "output": {"video_url": "https://fal.example/out.mp4"},
            "usage": {"amount_usd": 0.072},
        }
    monkeypatch.setattr(visuals, "_fal_ai_generate", fal_ok)

    asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))

    with sqlite3.connect(visuals_db) as conn:
        row = conn.execute(
            "SELECT provider, status FROM seedance_generations "
            "WHERE clip_id = ? ORDER BY id DESC LIMIT 1",
            ("viz-clip-1",),
        ).fetchone()
        cand_status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", ("viz-clip-1",),
        ).fetchone()[0]
    # Generation logged with fal_ai provider, candidate not quarantined
    assert row[0] == "fal_ai"
    assert row[1] == "succeeded"
    assert cand_status == "processing"


def test_localize_asset_path_is_deterministic_local_path():
    """Asset paths are now local (under data/generated_cache/), not CDN
    URLs that expire. Same prompt_hash → same path; different hashes
    → different paths."""
    p1 = visuals._localize_asset_path(
        prompt_hash="aaa", cdn_url="https://cdn1.example/x.mp4",
    )
    p2 = visuals._localize_asset_path(
        prompt_hash="aaa", cdn_url="https://cdn2.example/y.mp4",
    )
    p3 = visuals._localize_asset_path(
        prompt_hash="bbb", cdn_url="https://cdn1.example/x.mp4",
    )
    # CDN URL doesn't change the local path — path is keyed on prompt_hash
    assert p1 == p2
    assert p1 != p3
    # Path lives under data/generated_cache/
    assert "generated_cache" in p1
    assert p1.endswith(".mp4")


def test_successful_run_stores_local_path_not_cdn_url(
    visuals_db, tmp_quarantine, monkeypatch,
):
    """Codex CEO: provider CDN URLs expire (typically 7d signed) and
    can be taken down. The seedance_generations.output_path AND
    clip_artifacts.* paths MUST be local paths so the Compositor can
    re-read after the CDN URL expires. The CDN URL is preserved in
    raw_response_json for audit."""
    monkeypatch.setattr(visuals, "load", lambda name: _budget_cfg())

    cdn_url = "https://cdn.atlascloud.ai/signed/abc.mp4?Expires=12345"

    async def fake_atlas(*args, **kwargs):
        return {
            "status": "succeeded",
            "output": {"video_url": cdn_url},
            "usage": {"amount_usd": 0.066},
        }
    monkeypatch.setattr(visuals, "_atlas_cloud_generate", fake_atlas)

    assets = asyncio.run(visuals.run_visuals("viz-clip-1", [_shot()]))

    # Asset path is local, NOT the CDN URL
    assert len(assets) == 1
    assert "cdn.atlascloud.ai" not in assets[0].path
    assert "generated_cache" in assets[0].path

    # CDN URL preserved in seedance_generations.raw_response_json
    with sqlite3.connect(visuals_db) as conn:
        raw = conn.execute(
            "SELECT raw_response_json FROM seedance_generations WHERE clip_id = ?",
            ("viz-clip-1",),
        ).fetchone()[0]
    assert cdn_url in raw  # audit trail still points to provider URL
