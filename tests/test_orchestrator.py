"""End-to-end pipeline orchestrator tests.

Exercises the full Editor → Writer → Voice → Visuals → Compositor →
Compliance → enqueue sequence with each stage mocked so the orchestrator
control flow can be tested without spinning up yt-dlp, Whisper,
ElevenLabs, or ffmpeg.

The real agents already have their own coverage; what this file tests is
the *seam* — that quarantine detection, lease conflicts, compliance
failures, and clips_ready writes all wire up correctly.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from agents import orchestrator
from agents.db import init_schema
from agents.models import (
    AudioTrack, CompositedClip, ComplianceResult, GeneratedAsset, Script,
)
from agents.stage_lease import LeaseConflict
from scripts.migrate import migrate


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

@pytest.fixture
def orch_db(monkeypatch):
    """Isolated DB with the full migration chain + a default account
    row + minimal posting_schedule.yaml on disk so _enqueue_for_publish
    has the data it needs."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "orch.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        migrate(db_path=db_path)
        yield db_path


@pytest.fixture
def stub_schedule(monkeypatch):
    """Override config.load('posting_schedule') so tests don't depend
    on the real config/posting_schedule.yaml state."""
    schedule = {
        "timezone": "America/New_York",
        "platforms": {
            "instagram_reels": {
                "mode": "api",
                "posts_per_day": 2,
                "times": ["11:00", "19:00"],
            },
        },
    }

    def _fake_load(name: str):
        if name == "posting_schedule":
            return schedule
        # Other configs the agents reach for — let the real loader handle
        # them (persona, budget, optimizer_bounds etc.).
        from agents import config as real_config
        return real_config._load_with_mtime.__wrapped__(real_config._config_path(name))[0] if False else _real_load(name)

    # We're only patching what the orchestrator itself loads.
    monkeypatch.setattr(orchestrator, "load", lambda name: schedule if name == "posting_schedule" else _real_load(name))
    return schedule


def _real_load(name: str):
    """Bypass the monkeypatch — real config loader."""
    from agents.config import load as raw_load
    return raw_load(name)


def _seed_curated(db_path: Path, clip_id: str, *, creator: str = "IShowSpeed") -> None:
    """Insert a candidate row in 'curated' state so the orchestrator can
    pick it up. _pick_curated will atomically flip it to 'processing'."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO clips_candidate
              (id, creator, source_platform, source_url, source_title,
               source_duration_s, virality_score, status)
            VALUES (?, ?, 'youtube', ?, 'test source title',
                    20.0, 0.85, 'curated')
            """,
            (clip_id, creator, f"https://x/{clip_id}"),
        )
        conn.commit()


def _stage_succeeded(result: orchestrator.ClipResult, stage: str) -> bool:
    return any(s.stage == stage and s.succeeded for s in result.stages)


# ---------- Stage stubs ----------

def _make_passing_stubs(monkeypatch, db_path: Path):
    """Patch each agent's run_* function with a happy-path stub that
    persists the minimum data downstream stages need."""

    async def fake_editor(clip_id: str) -> None:
        # Populate enough of clip_artifacts that Writer/Compositor can read.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO clip_artifacts
                  (clip_id, source_local_path, punch_segment_start_s,
                   punch_segment_end_s, transcript_json,
                   has_music_in_source_segment)
                VALUES (?, ?, 0.0, 25.0, '[]', 0)
                """,
                (clip_id, f"/tmp/{clip_id}.mp4"),
            )
            conn.commit()

    async def fake_writer(clip_id: str) -> Script:
        shot_list = [{"shot_type": "concept_graphic", "start_s": 1.0,
                      "duration_s": 2.0, "prompt": "stylized chair flies",
                      "reaction_id": None, "punch_word": None}]
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE clip_artifacts
                   SET script_text = ?, shot_list_json = ?,
                       voice_runtime_s = 30.0
                 WHERE clip_id = ?
                """,
                ("Manic reactor commentary script", json.dumps(shot_list), clip_id),
            )
            conn.commit()
        return Script(
            text="Manic reactor commentary script",
            runtime_s=30.0,
            substance_tags=["prediction"],
            trending_refs=["meme:goofy_ahh"],
            trending_freshness="hot",
            punch_density=0.18,
            punch_beats=[2.0, 8.0],
            shot_list=[],
            hook_template_id="HT-04",
        )

    async def fake_voice(clip_id: str, script: Script) -> AudioTrack:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE clip_artifacts SET voice_audio_path = ?, "
                "voice_runtime_s = ? WHERE clip_id = ?",
                (f"/tmp/{clip_id}.wav", script.runtime_s, clip_id),
            )
            conn.commit()
        return AudioTrack(
            path=f"/tmp/{clip_id}.wav",
            runtime_s=script.runtime_s,
            loudness_lufs=-14.0,
            engine="coqui_xtts_v2",
            voice_id="manic_reactor_coqui_default_v1",
        )

    async def fake_visuals(clip_id: str, shot_list):
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE clip_artifacts
                   SET visuals_seconds_used = 4.0,
                       visuals_tier = 'fast',
                       visuals_cost_usd = 0.088,
                       has_real_face_reference = 0
                 WHERE clip_id = ?
                """,
                (clip_id,),
            )
            conn.commit()
        return [GeneratedAsset(
            path=f"/tmp/{clip_id}_shot1.mp4",
            duration_s=2.0, cost_usd=0.044, tier="fast",
            provider="atlas_cloud", prompt="stylized chair flies",
        )]

    async def fake_compositor(clip_id: str) -> CompositedClip:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE clip_artifacts
                   SET final_video_path = ?, final_duration_s = 45.0
                 WHERE clip_id = ?
                """,
                (f"/tmp/{clip_id}_final.mp4", clip_id),
            )
            creator_row = conn.execute(
                "SELECT creator FROM clips_candidate WHERE id = ?", (clip_id,),
            ).fetchone()
            conn.commit()
        creator = creator_row[0] if creator_row else "unknown"
        return CompositedClip(
            clip_id=clip_id,
            final_video_path=f"/tmp/{clip_id}_final.mp4",
            final_duration_s=45.0,
            source_segment_duration_s=22.0,
            commentary_audio_duration_s=30.0,
            audio_track=AudioTrack(
                path=f"/tmp/{clip_id}.wav", runtime_s=30.0,
                loudness_lufs=-14.0, engine="coqui_xtts_v2",
                voice_id="manic_reactor_coqui_default_v1",
            ),
            visuals_used=[GeneratedAsset(
                path=f"/tmp/{clip_id}_shot1.mp4",
                duration_s=2.0, cost_usd=0.044, tier="fast",
                provider="atlas_cloud", prompt="stylized chair flies",
            )],
            description="",
            has_music_in_source_segment=False,
            has_real_face_reference=False,
            source_creator=creator,
        )

    monkeypatch.setattr(orchestrator.editor, "run_editor", fake_editor)
    monkeypatch.setattr(orchestrator.writer, "run_writer", fake_writer)
    monkeypatch.setattr(orchestrator.voice, "run_voice", fake_voice)
    monkeypatch.setattr(orchestrator, "run_visuals", fake_visuals)
    monkeypatch.setattr(orchestrator, "run_compositor", fake_compositor)

    # Compliance.gate is sync, returns a passing result.
    def fake_gate(clip):
        return ComplianceResult(
            passed=True, blocked_reason=None,
            rule_results={"source_max_30s": {"passed": True, "detail": "ok"}},
        )

    monkeypatch.setattr(orchestrator.compliance, "gate", fake_gate)

    return {
        "editor": fake_editor, "writer": fake_writer, "voice": fake_voice,
        "visuals": fake_visuals, "compositor": fake_compositor, "gate": fake_gate,
    }


# ----------------------------------------------------------------------
# Candidate selection
# ----------------------------------------------------------------------

def test_pick_curated_atomically_flips_to_processing(orch_db):
    _seed_curated(orch_db, "2026-05-18-1000-a")
    _seed_curated(orch_db, "2026-05-18-1001-b")
    claimed = orchestrator._pick_curated(2)
    assert sorted(claimed) == ["2026-05-18-1000-a", "2026-05-18-1001-b"]
    with sqlite3.connect(orch_db) as conn:
        statuses = dict(conn.execute(
            "SELECT id, status FROM clips_candidate"
        ).fetchall())
    assert statuses["2026-05-18-1000-a"] == "processing"
    assert statuses["2026-05-18-1001-b"] == "processing"


def test_pick_curated_respects_virality_score_ordering(orch_db):
    """Higher-score candidates are claimed first."""
    with sqlite3.connect(orch_db) as conn:
        for score, suffix in [(0.5, "low"), (0.95, "hi"), (0.7, "mid")]:
            conn.execute(
                """
                INSERT INTO clips_candidate
                  (id, creator, source_platform, source_url, virality_score, status)
                VALUES (?, 'C', 'youtube', ?, ?, 'curated')
                """,
                (f"2026-05-18-1100-{suffix}", f"https://x/{suffix}", score),
            )
        conn.commit()
    claimed = orchestrator._pick_curated(2)
    # Highest score first, then mid.
    assert claimed == ["2026-05-18-1100-hi", "2026-05-18-1100-mid"]


def test_pick_curated_ignores_non_curated_status(orch_db):
    with sqlite3.connect(orch_db) as conn:
        conn.execute(
            """
            INSERT INTO clips_candidate (id, creator, source_platform, source_url, status)
            VALUES ('2026-05-18-1200-disc', 'C', 'youtube', 'https://x/disc', 'discovered'),
                   ('2026-05-18-1200-pub', 'C', 'youtube', 'https://x/pub', 'published'),
                   ('2026-05-18-1200-cur', 'C', 'youtube', 'https://x/cur', 'curated')
            """,
        )
        conn.commit()
    claimed = orchestrator._pick_curated(10)
    assert claimed == ["2026-05-18-1200-cur"]


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------

def test_full_pipeline_happy_path_writes_clips_ready(orch_db, monkeypatch, stub_schedule):
    clip_id = "2026-05-18-2000-happy"
    _seed_curated(orch_db, clip_id)
    _make_passing_stubs(monkeypatch, orch_db)

    summary = asyncio.run(orchestrator.run_orchestrator(1))
    assert summary.processed == 1
    assert summary.ready == 1
    assert summary.results[0].outcome == "ready"

    with sqlite3.connect(orch_db) as conn:
        candidate_status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", (clip_id,),
        ).fetchone()[0]
        ready_rows = conn.execute(
            "SELECT target_platform, account_id, description, hashtags_json "
            "FROM clips_ready WHERE clip_id = ?",
            (clip_id,),
        ).fetchall()
    assert candidate_status == "ready"
    assert len(ready_rows) == 1
    assert ready_rows[0][0] == "instagram_reels"
    assert "Commentary on IShowSpeed" in ready_rows[0][2]
    assert "AI-generated" in ready_rows[0][2]  # visuals were used


def test_each_stage_recorded_in_clipresult(orch_db, monkeypatch, stub_schedule):
    clip_id = "2026-05-18-2001-stages"
    _seed_curated(orch_db, clip_id)
    _make_passing_stubs(monkeypatch, orch_db)

    summary = asyncio.run(orchestrator.run_orchestrator(1))
    result = summary.results[0]
    assert _stage_succeeded(result, "editor")
    assert _stage_succeeded(result, "writer")
    assert _stage_succeeded(result, "voice")
    assert _stage_succeeded(result, "visuals")
    assert _stage_succeeded(result, "compositor")
    assert _stage_succeeded(result, "compliance")
    assert _stage_succeeded(result, "enqueue")


# ----------------------------------------------------------------------
# Quarantine flows (one per upstream stage)
# ----------------------------------------------------------------------

def test_editor_quarantine_short_circuits_pipeline(orch_db, monkeypatch, stub_schedule):
    """If Editor flips the candidate to 'quarantined' (no_speech,
    partial_download, etc.), the orchestrator must stop and not run
    downstream stages."""
    clip_id = "2026-05-18-2100-edq"
    _seed_curated(orch_db, clip_id)
    _make_passing_stubs(monkeypatch, orch_db)

    async def quarantining_editor(clip_id_: str) -> None:
        with sqlite3.connect(orch_db) as conn:
            conn.execute(
                "UPDATE clips_candidate SET status = 'quarantined' WHERE id = ?",
                (clip_id_,),
            )
            conn.commit()
    monkeypatch.setattr(orchestrator.editor, "run_editor", quarantining_editor)

    # Spy on writer to verify it's NOT called.
    writer_called = []

    async def spy_writer(clip_id_: str):
        writer_called.append(clip_id_)
    monkeypatch.setattr(orchestrator.writer, "run_writer", spy_writer)

    summary = asyncio.run(orchestrator.run_orchestrator(1))
    assert summary.quarantined == 1
    assert summary.ready == 0
    assert writer_called == []


def test_visuals_quarantine_blocks_compositor(orch_db, monkeypatch, stub_schedule):
    """A quarantine raised by Visuals (e.g., per-clip seconds cap) must
    short-circuit the pipeline."""
    clip_id = "2026-05-18-2110-vq"
    _seed_curated(orch_db, clip_id)
    _make_passing_stubs(monkeypatch, orch_db)

    async def quarantining_visuals(clip_id_: str, shot_list):
        with sqlite3.connect(orch_db) as conn:
            conn.execute(
                "UPDATE clips_candidate SET status = 'quarantined' WHERE id = ?",
                (clip_id_,),
            )
            conn.commit()
        return []
    monkeypatch.setattr(orchestrator, "run_visuals", quarantining_visuals)

    compositor_called = []

    async def spy_compositor(clip_id_: str):
        compositor_called.append(clip_id_)
        return None
    monkeypatch.setattr(orchestrator, "run_compositor", spy_compositor)

    summary = asyncio.run(orchestrator.run_orchestrator(1))
    assert summary.quarantined == 1
    assert compositor_called == []


def test_compositor_returning_none_treated_as_quarantine(orch_db, monkeypatch, stub_schedule):
    """run_compositor returns None on CompositionFailed → orchestrator must
    record quarantine and NOT call Compliance."""
    clip_id = "2026-05-18-2120-cq"
    _seed_curated(orch_db, clip_id)
    _make_passing_stubs(monkeypatch, orch_db)

    async def failing_compositor(clip_id_: str):
        # _quarantine_clip would normally have run inside compositor;
        # simulate the status flip here.
        with sqlite3.connect(orch_db) as conn:
            conn.execute(
                "UPDATE clips_candidate SET status = 'quarantined' WHERE id = ?",
                (clip_id_,),
            )
            conn.commit()
        return None
    monkeypatch.setattr(orchestrator, "run_compositor", failing_compositor)

    compliance_called = []

    def spy_gate(clip):
        compliance_called.append(clip.clip_id)
        return ComplianceResult(passed=True, blocked_reason=None, rule_results={})
    monkeypatch.setattr(orchestrator.compliance, "gate", spy_gate)

    summary = asyncio.run(orchestrator.run_orchestrator(1))
    assert summary.quarantined == 1
    assert compliance_called == []


# ----------------------------------------------------------------------
# Compliance failure
# ----------------------------------------------------------------------

def test_compliance_failure_marks_quarantined_and_writes_no_clips_ready(
    orch_db, monkeypatch, stub_schedule,
):
    clip_id = "2026-05-18-2200-cf"
    _seed_curated(orch_db, clip_id)
    _make_passing_stubs(monkeypatch, orch_db)

    def failing_gate(clip):
        return ComplianceResult(
            passed=False,
            blocked_reason="commentary_at_least_50pct",
            rule_results={"commentary_at_least_50pct": {
                "passed": False, "detail": "commentary_ratio=0.30",
            }},
        )
    monkeypatch.setattr(orchestrator.compliance, "gate", failing_gate)

    summary = asyncio.run(orchestrator.run_orchestrator(1))
    assert summary.compliance_failed == 1
    assert summary.ready == 0

    with sqlite3.connect(orch_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", (clip_id,),
        ).fetchone()[0]
        ready_count = conn.execute(
            "SELECT COUNT(*) FROM clips_ready WHERE clip_id = ?", (clip_id,),
        ).fetchone()[0]
    assert status == "quarantined"
    assert ready_count == 0


# ----------------------------------------------------------------------
# Lease conflict
# ----------------------------------------------------------------------

def test_lease_conflict_during_editor_returns_lease_conflict(
    orch_db, monkeypatch, stub_schedule,
):
    clip_id = "2026-05-18-2300-lc"
    _seed_curated(orch_db, clip_id)
    _make_passing_stubs(monkeypatch, orch_db)

    async def conflicting_editor(clip_id_: str):
        raise LeaseConflict(f"another agent holds the editor lease for {clip_id_}")
    monkeypatch.setattr(orchestrator.editor, "run_editor", conflicting_editor)

    summary = asyncio.run(orchestrator.run_orchestrator(1))
    assert summary.lease_conflict == 1
    assert summary.ready == 0
    # Candidate is still 'processing' — the next run picks it up after
    # the lease expires (no state transition under lease conflict).


# ----------------------------------------------------------------------
# Empty queue
# ----------------------------------------------------------------------

def test_run_orchestrator_with_no_curated_returns_zero(orch_db, monkeypatch, stub_schedule):
    _make_passing_stubs(monkeypatch, orch_db)
    summary = asyncio.run(orchestrator.run_orchestrator(5))
    assert summary.requested == 5
    assert summary.processed == 0
    assert summary.results == []


def test_run_orchestrator_zero_n_raises(orch_db):
    with pytest.raises(ValueError):
        asyncio.run(orchestrator.run_orchestrator(0))


# ----------------------------------------------------------------------
# Enqueue idempotency
# ----------------------------------------------------------------------

def test_enqueue_writes_one_row_per_platform(orch_db, monkeypatch):
    """Each platform in posting_schedule.yaml gets one clips_ready row."""
    schedule = {
        "timezone": "America/New_York",
        "platforms": {
            "instagram_reels": {"mode": "api", "times": ["11:00", "19:00"]},
            "youtube_shorts": {"mode": "api", "times": ["12:00", "21:00"]},
            "tiktok": {"mode": "manual", "times": ["07:00", "12:00", "20:00"]},
        },
    }
    monkeypatch.setattr(orchestrator, "load",
                        lambda name: schedule if name == "posting_schedule" else _real_load(name))

    clip_id = "2026-05-18-2400-multi"
    _seed_curated(orch_db, clip_id)
    # Need a clips_candidate row already; just promote to processing first
    # since _enqueue_for_publish reads creator from the row.
    composited = CompositedClip(
        clip_id=clip_id, final_video_path=f"/tmp/{clip_id}.mp4",
        final_duration_s=45.0, source_segment_duration_s=20.0,
        commentary_audio_duration_s=30.0,
        audio_track=AudioTrack(path="/tmp/v.wav", runtime_s=30.0,
                                loudness_lufs=-14.0, engine="coqui_xtts_v2",
                                voice_id="manic_reactor_coqui_default_v1"),
        visuals_used=[], description="Commentary on IShowSpeed.",
        has_music_in_source_segment=False, has_real_face_reference=False,
        source_creator="IShowSpeed",
    )
    inserted = orchestrator._enqueue_for_publish(composited)
    assert len(inserted) == 3
    with sqlite3.connect(orch_db) as conn:
        platforms = sorted(p for (p,) in conn.execute(
            "SELECT target_platform FROM clips_ready WHERE clip_id = ?",
            (clip_id,),
        ).fetchall())
    assert platforms == ["instagram_reels", "tiktok", "youtube_shorts"]


def test_enqueue_idempotent_on_existing_queued_row(orch_db, monkeypatch):
    """Re-running the orchestrator on the same clip must not duplicate
    clips_ready rows — the partial unique index guards it and the
    orchestrator silently skips."""
    schedule = {
        "timezone": "America/New_York",
        "platforms": {
            "instagram_reels": {"mode": "api", "times": ["11:00", "19:00"]},
        },
    }
    monkeypatch.setattr(orchestrator, "load",
                        lambda name: schedule if name == "posting_schedule" else _real_load(name))

    clip_id = "2026-05-18-2410-idem"
    _seed_curated(orch_db, clip_id)
    composited = CompositedClip(
        clip_id=clip_id, final_video_path=f"/tmp/{clip_id}.mp4",
        final_duration_s=45.0, source_segment_duration_s=20.0,
        commentary_audio_duration_s=30.0,
        audio_track=AudioTrack(path="/tmp/v.wav", runtime_s=30.0,
                                loudness_lufs=-14.0, engine="coqui_xtts_v2",
                                voice_id="manic_reactor_coqui_default_v1"),
        visuals_used=[], description="Commentary on IShowSpeed.",
        has_music_in_source_segment=False, has_real_face_reference=False,
        source_creator="IShowSpeed",
    )
    first = orchestrator._enqueue_for_publish(composited)
    second = orchestrator._enqueue_for_publish(composited)
    assert len(first) == 1
    assert second == []  # already queued, skipped
    with sqlite3.connect(orch_db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM clips_ready WHERE clip_id = ?", (clip_id,),
        ).fetchone()[0]
    assert count == 1


# ----------------------------------------------------------------------
# Scheduled-slot picker
# ----------------------------------------------------------------------

def test_next_scheduled_slot_picks_future_time_today(orch_db, monkeypatch):
    """If now is 09:00 ET and times are [11:00, 19:00], pick 11:00 today."""
    schedule = {"timezone": "America/New_York", "platforms": {}}
    monkeypatch.setattr(orchestrator, "load", lambda name: schedule)

    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 5, 18, 9, 0, tzinfo=ZoneInfo("America/New_York"))
    slot = orchestrator._next_scheduled_slot(
        {"times": ["11:00", "19:00"]}, now=now,
    )
    assert "2026-05-18T11:00:00" in slot


def test_next_scheduled_slot_wraps_to_tomorrow(orch_db, monkeypatch):
    """If now is past all of today's slots, pick the first slot tomorrow."""
    schedule = {"timezone": "America/New_York", "platforms": {}}
    monkeypatch.setattr(orchestrator, "load", lambda name: schedule)

    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 5, 18, 22, 0, tzinfo=ZoneInfo("America/New_York"))
    slot = orchestrator._next_scheduled_slot(
        {"times": ["11:00", "19:00"]}, now=now,
    )
    assert "2026-05-19T11:00:00" in slot


# ----------------------------------------------------------------------
# Account resolution
# ----------------------------------------------------------------------

def test_default_account_prefers_accounts_table(orch_db):
    with sqlite3.connect(orch_db) as conn:
        conn.execute(
            """
            INSERT INTO accounts (id, platform, role, active)
            VALUES ('tiktok_real_acct_1', 'tiktok', 'primary', 1)
            """,
        )
        conn.commit()
    assert orchestrator._default_account_for("tiktok") == "tiktok_real_acct_1"


def test_default_account_falls_back_to_placeholder(orch_db):
    # No accounts table rows for instagram_reels → placeholder.
    assert orchestrator._default_account_for("instagram_reels") == "instagram_reels_primary_1"
