"""Shared pytest fixtures."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from agents.db import init_schema
from agents.models import AudioTrack, CompositedClip, GeneratedAsset


@pytest.fixture
def temp_db(monkeypatch):
    """Isolated SQLite DB per test, with a seed clips_candidate row matching
    the good_clip fixture so persistence tests don't trip the FK."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO clips_candidate (id, creator, source_platform, source_url, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("2026-05-14-1200-test", "IShowSpeed", "youtube", "https://test", "processing"),
            )
            conn.commit()
        yield db_path


@pytest.fixture
def good_audio_track() -> AudioTrack:
    return AudioTrack(
        path="/tmp/fixture/voice.wav",
        runtime_s=30.0,
        loudness_lufs=-14.0,
        engine="coqui_xtts_v2",
        # Must match an entry in persona.yaml active persona's
        # voice.approved_voice_ids. Compliance gate fails closed otherwise.
        voice_id="manic_reactor_coqui_default_v1",
    )


@pytest.fixture
def good_generated_asset() -> GeneratedAsset:
    return GeneratedAsset(
        path="/tmp/fixture/avatar_jawdrop.mp4",
        duration_s=2.5,
        cost_usd=0.06,
        tier="fast",
        provider="atlas_cloud",
        prompt="The character's jaw drops cartoonishly low...",
        seed=8376739915435003287,
    )


@pytest.fixture
def good_clip(good_audio_track, good_generated_asset) -> CompositedClip:
    """A CompositedClip that should pass every Compliance rule."""
    return CompositedClip(
        clip_id="2026-05-14-1200-test",
        final_video_path="/tmp/fixture/final.mp4",
        final_duration_s=50.0,                 # within 60s cap
        source_segment_duration_s=25.0,        # within 30s cap
        commentary_audio_duration_s=28.0,      # 56% commentary, > 50%
        audio_track=good_audio_track,
        visuals_used=[good_generated_asset],
        description=(
            "Reaction commentary on IShowSpeed's stream moment. "
            "Includes AI-generated avatar visuals. #ad"
        ),
        has_music_in_source_segment=False,
        has_real_face_reference=False,
        source_creator="IShowSpeed",
    )
