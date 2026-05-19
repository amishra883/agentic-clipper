"""Voice tests — Day 8 hardening (E-26, E-17, daily cap, E-27, lease).

Critical assertions:
- Coqui checksum prewarm verifies SHA; mismatch raises CoquiCheckpointError
- ElevenLabs 429 / RetryGiveUp → structured fallback to Coqui, NOT silent
- BudgetExceeded on reserve → fallback to Coqui (not quarantine)
- AudioTrack.voice_id populated from persona.approved_voice_ids
- stage_lease("voice") wraps the run + bumps artifact_version
- benchmark_synthesis writes a row to data/digest/voice-benchmark.jsonl
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from agents import voice
from agents.costs import BudgetExceeded
from agents.db import init_schema
from agents.models import Script
from agents.retry import RetryGiveUp, TransientError
from scripts.migrate import migrate


# ---------- Fixtures ----------


def _make_script(runtime_s: float = 25.0) -> Script:
    return Script(
        text="A clean transformative commentary script with some energy.",
        runtime_s=runtime_s,
        substance_tags=["prediction"],
        trending_refs=["meme:test"],
        trending_freshness="hot",
        punch_density=0.12,
        punch_beats=[5.0, 10.0, 15.0],
        shot_list=[],
        hook_template_id="HT-1",
    )


def _make_persona() -> dict:
    return {
        "id": "P-test",
        "voice": {
            "tts_engine_default": "coqui_xtts_v2",
            "tts_engine_escalation": "elevenlabs_creator",
            "escalation_trigger": "curator_score>=0.85",
            "target_loudness_lufs": -14,
            "approved_voice_ids": [
                "manic_reactor_coqui_default_v1",
                "manic_reactor_elevenlabs_v1",
            ],
        },
    }


@pytest.fixture
def voice_db(monkeypatch):
    """Migrated DB with one curated candidate + clip_artifacts row."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "voice.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        migrate(db_path=db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO clips_candidate (id, creator, source_platform, source_url, virality_score, status) "
                "VALUES ('voice-clip-1', 'IShowSpeed', 'twitch', 'https://test', 0.50, 'processing')"
            )
            conn.commit()
        yield db_path


@pytest.fixture
def voice_db_high_score(monkeypatch):
    """Migrated DB with a candidate above the escalation threshold."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "voice.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        migrate(db_path=db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO clips_candidate (id, creator, source_platform, source_url, virality_score, status) "
                "VALUES ('voice-clip-1', 'IShowSpeed', 'twitch', 'https://test', 0.92, 'processing')"
            )
            conn.commit()
        yield db_path


@pytest.fixture
def tmp_voice_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(voice, "VOICE_OUT_DIR", tmp_path / "voice")
    monkeypatch.setattr(voice, "BENCHMARK_LOG", tmp_path / "benchmark.jsonl")
    return tmp_path


def _patch_persona_and_caps(monkeypatch, persona: dict | None = None, caps: dict | None = None):
    persona = persona or _make_persona()
    cfg_load_results = {
        "persona": {"active_persona": "P-test", "personas": [persona]},
        "budget": caps or {
            "line_items": {"elevenlabs_creator": {"monthly_budget_usd": 22}},
            "per_call_caps": {
                "elevenlabs_daily_usd_max": 1.50,
                "elevenlabs_cost_usd_max": 1.00,
            },
        },
        "voice_models": {},
    }
    monkeypatch.setattr(voice, "load", lambda name: cfg_load_results.get(name, {}))


# ---------- E-26: Coqui checksum prewarm ----------


def test_coqui_checkpoint_unverified_when_no_pin(monkeypatch):
    """Phase 1 default — no config/voice_models.yaml entry. The function
    must return verified=False with reason='checkpoint_missing' or
    'no_pinned_sha' rather than raising."""
    monkeypatch.setattr(voice, "load", lambda name: {})
    result = voice.verify_coqui_checkpoint()
    assert result["verified"] is False
    assert "reason" in result


def test_coqui_checkpoint_verified_when_sha_matches(monkeypatch, tmp_path):
    """Hash a real on-disk file, pin its SHA in the config, and verify."""
    model = tmp_path / "xtts-v2.pth"
    model.write_bytes(b"fake-model-bytes-for-test")
    sha = voice._sha256_file(model)
    monkeypatch.setattr(voice, "load", lambda name: {
        "coqui_xtts_v2": {"checkpoint_path": str(model), "sha256": sha},
    } if name == "voice_models" else {})
    result = voice.verify_coqui_checkpoint()
    assert result["verified"] is True
    assert result["sha256"] == sha


def test_coqui_checkpoint_mismatch_raises(monkeypatch, tmp_path):
    """Wrong SHA → CoquiCheckpointError (tampered or wrong-version model)."""
    model = tmp_path / "xtts-v2.pth"
    model.write_bytes(b"some-bytes")
    monkeypatch.setattr(voice, "load", lambda name: {
        "coqui_xtts_v2": {
            "checkpoint_path": str(model),
            "sha256": "0" * 64,  # deliberately wrong
        },
    } if name == "voice_models" else {})
    with pytest.raises(voice.CoquiCheckpointError, match="hash mismatch"):
        voice.verify_coqui_checkpoint()


def test_coqui_checkpoint_missing_file_returns_unverified(monkeypatch):
    """Pin exists but checkpoint file doesn't — reason=checkpoint_missing."""
    monkeypatch.setattr(voice, "load", lambda name: {
        "coqui_xtts_v2": {
            "checkpoint_path": "/nonexistent/xtts-v2.pth",
            "sha256": "abc123" * 8,
        },
    } if name == "voice_models" else {})
    result = voice.verify_coqui_checkpoint()
    assert result["verified"] is False
    assert result["reason"] == "checkpoint_missing"


# ---------- E-17: ElevenLabs fallback logging ----------


def test_elevenlabs_fallback_on_retry_giveup(
    voice_db_high_score, tmp_voice_dirs, monkeypatch,
):
    """ElevenLabs returns 429 repeatedly; retry_external exhausts and
    raises RetryGiveUp. Voice MUST fall back to Coqui AND emit a
    structured elevenlabs_fallback event with reason=rate_limit_exhausted.
    Codex E-17: previously this exception was silently swallowed."""
    _patch_persona_and_caps(monkeypatch)
    # Spy on log calls so we can assert the fallback event
    events = []
    monkeypatch.setattr(voice, "log", lambda **kw: events.append(kw))

    async def fake_eleven(*args, **kwargs):
        raise RetryGiveUp("ElevenLabs 429 after 3 attempts")
    monkeypatch.setattr(voice, "_synthesize_elevenlabs", fake_eleven)

    asyncio.run(voice.run_voice("voice-clip-1", _make_script()))

    fallback_events = [
        e for e in events if e.get("event_type") == "elevenlabs_fallback"
    ]
    assert len(fallback_events) == 1
    assert fallback_events[0]["level"] == "warn"
    assert fallback_events[0]["payload"]["reason"] == voice.ElevenLabsFallbackReason.RATE_LIMIT


def test_elevenlabs_fallback_on_transient_error(
    voice_db_high_score, tmp_voice_dirs, monkeypatch,
):
    """A TransientError that escapes retry_external (e.g., raised directly
    before retries kick in) still produces the structured fallback event."""
    _patch_persona_and_caps(monkeypatch)
    events = []
    monkeypatch.setattr(voice, "log", lambda **kw: events.append(kw))

    async def fake_eleven(*args, **kwargs):
        raise TransientError("connection reset")
    # Bypass the retry decorator by patching the inner function directly
    monkeypatch.setattr(voice, "_synthesize_elevenlabs", fake_eleven)

    asyncio.run(voice.run_voice("voice-clip-1", _make_script()))

    fallback_events = [
        e for e in events if e.get("event_type") == "elevenlabs_fallback"
    ]
    assert len(fallback_events) == 1


def test_voice_records_actual_engine_after_fallback(
    voice_db_high_score, tmp_voice_dirs, monkeypatch,
):
    """After fallback, voice_generated event's actual_engine reflects the
    real engine used (coqui_xtts_v2), not the originally-requested one."""
    _patch_persona_and_caps(monkeypatch)
    events = []
    monkeypatch.setattr(voice, "log", lambda **kw: events.append(kw))

    async def fake_eleven(*args, **kwargs):
        raise RetryGiveUp("rate limited")
    monkeypatch.setattr(voice, "_synthesize_elevenlabs", fake_eleven)

    asyncio.run(voice.run_voice("voice-clip-1", _make_script()))
    gen_events = [e for e in events if e.get("event_type") == "voice_generated"]
    assert gen_events
    payload = gen_events[0]["payload"]
    assert payload["requested_engine"] == "elevenlabs"
    assert payload["actual_engine"] == "coqui_xtts_v2"
    assert payload["fallback_reason"] is not None


# ---------- Daily cost cap ----------


def test_budget_exceeded_falls_back_to_coqui(
    voice_db_high_score, tmp_voice_dirs, monkeypatch,
):
    """Pre-fill the ElevenLabs daily cap so the reservation raises
    BudgetExceeded. Voice MUST fall back to Coqui — clip is NOT
    quarantined (Coqui is the free default)."""
    # Pre-fill 1.40 against the 1.50 daily cap so the next ~0.30 reservation breaches
    with sqlite3.connect(voice_db_high_score) as conn:
        conn.execute(
            "INSERT INTO costs (ts, category, amount_usd, status) "
            "VALUES (datetime('now'), 'elevenlabs_creator', 1.40, 'succeeded')"
        )
        conn.commit()

    _patch_persona_and_caps(monkeypatch)
    events = []
    monkeypatch.setattr(voice, "log", lambda **kw: events.append(kw))

    asyncio.run(voice.run_voice("voice-clip-1", _make_script()))

    fallback_events = [
        e for e in events if e.get("event_type") == "elevenlabs_fallback"
    ]
    assert fallback_events
    assert fallback_events[0]["payload"]["reason"] == voice.ElevenLabsFallbackReason.BUDGET

    # Clip status untouched — fallback is not a failure
    with sqlite3.connect(voice_db_high_score) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", ("voice-clip-1",)
        ).fetchone()[0]
    assert status == "processing"


def test_per_clip_cap_blocks_reservation_before_daily(monkeypatch):
    """The per-clip cap fires structurally before the daily check —
    a 30s clip at $0.012/sec = $0.36, well under the daily cap but if
    the per-clip cap is set to $0.20 it MUST block."""
    caps = {
        "line_item_cap_usd": 22,
        "daily_cap_usd": 1.50,
        "per_clip_cap_usd": 0.20,
    }
    with pytest.raises(BudgetExceeded, match="per_clip"):
        voice._reserve_elevenlabs("clip-1", 0.36, caps)


def test_project_elevenlabs_cost_scales_linearly():
    assert voice._project_elevenlabs_cost(10.0) == round(10.0 * 0.012, 4)
    assert voice._project_elevenlabs_cost(0.0) == 0.0


def test_elevenlabs_budget_caps_loads_from_config(monkeypatch):
    """Caps resolver returns the three caps from config/budget.yaml."""
    monkeypatch.setattr(voice, "load", lambda name: {
        "line_items": {"elevenlabs_creator": {"monthly_budget_usd": 22}},
        "per_call_caps": {
            "elevenlabs_daily_usd_max": 1.50,
            "elevenlabs_cost_usd_max": 1.00,
        },
    })
    caps = voice._elevenlabs_budget_caps()
    assert caps["line_item_cap_usd"] == 22
    assert caps["daily_cap_usd"] == 1.50
    assert caps["per_clip_cap_usd"] == 1.00


# ---------- Voice ID + persona whitelist (Compliance contract) ----------


def test_voice_id_populated_for_coqui_path(voice_db, tmp_voice_dirs, monkeypatch):
    """An AudioTrack from the Coqui path carries voice_id from the
    persona's approved_voice_ids list — without it, Compliance blocks."""
    _patch_persona_and_caps(monkeypatch)
    track = asyncio.run(voice.run_voice("voice-clip-1", _make_script()))
    assert track.voice_id == "manic_reactor_coqui_default_v1"


def test_voice_id_populated_for_elevenlabs_path(
    voice_db_high_score, tmp_voice_dirs, monkeypatch,
):
    """ElevenLabs path picks the eleven-labelled approved_voice_id."""
    _patch_persona_and_caps(monkeypatch)
    # Bypass NotImplementedError by stubbing — but keep escalate=True
    async def fake_eleven(*args, **kwargs):
        return 25.0
    monkeypatch.setattr(voice, "_synthesize_elevenlabs", fake_eleven)
    async def fake_norm(path, target_lufs):
        return target_lufs
    monkeypatch.setattr(voice, "_normalize_loudness", fake_norm)

    track = asyncio.run(voice.run_voice("voice-clip-1", _make_script()))
    assert track.engine == "elevenlabs"
    assert "elevenlabs" in track.voice_id.lower()


def test_pick_voice_id_rejects_empty_whitelist():
    """Empty approved_voice_ids must raise — defense in depth against
    a persona.yaml edit that accidentally clears the whitelist."""
    persona = _make_persona()
    persona["voice"]["approved_voice_ids"] = []
    with pytest.raises(ValueError, match="empty approved_voice_ids"):
        voice._pick_voice_id(persona, "coqui_xtts_v2")


# ---------- Escalation logic ----------


def test_should_escalate_above_threshold():
    persona = _make_persona()
    assert voice._should_escalate(persona, 0.90) is True


def test_should_escalate_below_threshold():
    persona = _make_persona()
    assert voice._should_escalate(persona, 0.50) is False


def test_should_escalate_handles_missing_score():
    persona = _make_persona()
    assert voice._should_escalate(persona, None) is False


def test_should_escalate_handles_malformed_trigger():
    persona = _make_persona()
    persona["voice"]["escalation_trigger"] = "garbage>=banana"
    assert voice._should_escalate(persona, 0.99) is False


# ---------- stage_lease + artifact_version ----------


def test_voice_run_writes_artifact_with_lease_version(
    voice_db, tmp_voice_dirs, monkeypatch,
):
    """A successful Voice run writes clip_artifacts.voice_audio_path
    AND bumps artifact_version via the stage_lease CAS."""
    _patch_persona_and_caps(monkeypatch)
    asyncio.run(voice.run_voice("voice-clip-1", _make_script()))
    with sqlite3.connect(voice_db) as conn:
        row = conn.execute(
            "SELECT voice_audio_path, artifact_version FROM clip_artifacts "
            "WHERE clip_id = ?", ("voice-clip-1",),
        ).fetchone()
    assert row[0] is not None  # voice_audio_path populated
    assert row[1] == 1  # lease bumped 0 → 1


def test_voice_lease_conflict_raises(voice_db, tmp_voice_dirs, monkeypatch):
    """A second Voice on the same clip raises LeaseConflict — caller
    decides wait-or-skip."""
    from agents.stage_lease import LeaseConflict, stage_lease

    _patch_persona_and_caps(monkeypatch)
    with stage_lease("voice-clip-1", stage="voice", ttl_seconds=60):
        with pytest.raises(LeaseConflict):
            asyncio.run(voice.run_voice("voice-clip-1", _make_script()))


# ---------- E-27: benchmark ----------


def test_benchmark_writes_jsonl_row(tmp_voice_dirs, monkeypatch):
    """benchmark_synthesis must append exactly one JSONL row per call."""
    _patch_persona_and_caps(monkeypatch)
    result = voice.benchmark_synthesis(sample_text="Quick benchmark line.")
    assert result.engine in ("coqui", "scaffold")
    assert result.realtime_ratio >= 0
    assert voice.BENCHMARK_LOG.exists()
    rows = voice.BENCHMARK_LOG.read_text().strip().split("\n")
    assert len(rows) == 1
    parsed = json.loads(rows[0])
    assert "wall_seconds" in parsed
    assert "audio_seconds" in parsed
    assert "realtime_ratio" in parsed


def test_benchmark_appends_not_overwrites(tmp_voice_dirs, monkeypatch):
    """Repeated calls append additional rows so the doctor surface can
    plot the realtime-ratio trend."""
    _patch_persona_and_caps(monkeypatch)
    voice.benchmark_synthesis(sample_text="one")
    voice.benchmark_synthesis(sample_text="two")
    rows = voice.BENCHMARK_LOG.read_text().strip().split("\n")
    assert len(rows) == 2


def test_benchmark_floors_audio_seconds_at_one(tmp_voice_dirs, monkeypatch):
    """A trivially-short text shouldn't divide by ~zero and produce
    infinity ratios — audio_seconds floors at 1.0."""
    _patch_persona_and_caps(monkeypatch)
    result = voice.benchmark_synthesis(sample_text="hi")
    assert result.audio_seconds >= 1.0


def test_realtime_ratio_floor_is_documented():
    """Doctor surface flags when ratio > this floor."""
    assert voice.COQUI_REALTIME_RATIO_FLOOR >= 1.0
    assert voice.COQUI_REALTIME_RATIO_FLOOR <= 3.0
