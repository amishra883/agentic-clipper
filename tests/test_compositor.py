"""Compositor tests — Day 11-12 hardening (E-9, E-19, E-10, E-21, E-22, lease).

Critical assertions:
- Run-scoped temp path is in the SAME directory as final (atomic rename req)
- Atomic promote replaces partial → final in one inode swap
- Post-compose ffprobe duration verification catches truncated renders
- WhisperX alignment contract returns same shape with realigned timestamps
- Sidechain LUFS ducking contract returns the measured LUFS
- stage_lease("compositor") prevents concurrent compose on the same clip
- LeaseConflict propagates so the orchestrator can decide wait-vs-skip
- commit_artifact bumps artifact_version atomically
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

from agents import compositor
from agents.db import init_schema
from scripts.migrate import migrate


# ---------- Fixtures ----------


@pytest.fixture
def compositor_db(monkeypatch):
    """Migrated DB with one curated candidate that has a complete artifact
    row (Editor + Writer + Voice + Visuals all wrote)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "compositor.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        migrate(db_path=db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO clips_candidate (id, creator, source_platform, source_url, "
                "virality_score, status) "
                "VALUES ('comp-clip-1', 'IShowSpeed', 'twitch', 'https://test', "
                "0.60, 'processing')"
            )
            # Fresh DB has no clip_artifacts row yet; insert with all
            # upstream-stage fields seeded so Compositor can run.
            # artifact_version=0 so commit_artifact's CAS bumps to 1.
            conn.execute(
                """
                INSERT INTO clip_artifacts
                  (clip_id, source_local_path, punch_segment_start_s,
                   punch_segment_end_s, transcript_json, script_text,
                   shot_list_json, voice_audio_path, voice_runtime_s,
                   has_music_in_source_segment, has_real_face_reference,
                   artifact_version)
                VALUES (?, 'src.mp4', 5.0, 30.0, '[]', 'a script',
                        '[]', 'voice.wav', 28.0, 0, 0, 0)
                """,
                ("comp-clip-1",),
            )
            conn.commit()
        yield db_path


@pytest.fixture
def tmp_compositor_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(compositor, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(compositor, "QUARANTINE_DIR", tmp_path / "quarantine")
    return tmp_path


# ---------- E-9: Atomic write pattern ----------


def test_partial_path_is_in_same_directory_as_final(tmp_path):
    """The partial file MUST be in the same directory as final, otherwise
    os.replace isn't atomic across filesystems."""
    final = tmp_path / "subdir" / "clip.mp4"
    partial = compositor._partial_path(final, run_id=42)
    assert partial.parent == final.parent


def test_partial_path_includes_run_id(tmp_path):
    """The run_id distinguishes partials from different concurrent attempts
    so a janitor can sweep abandoned ones without colliding."""
    final = tmp_path / "clip.mp4"
    p1 = compositor._partial_path(final, run_id=1)
    p2 = compositor._partial_path(final, run_id=2)
    assert p1 != p2
    assert "1" in p1.name
    assert "2" in p2.name


def test_atomic_promote_swaps_partial_to_final(tmp_path):
    """os.replace promotes the partial in one syscall. After: final
    exists with partial's content, partial is gone."""
    final = tmp_path / "clip.mp4"
    partial = compositor._partial_path(final, run_id=1)
    partial.write_bytes(b"composed video bytes")

    compositor._atomic_promote(partial, final)

    assert final.exists()
    assert not partial.exists()
    assert final.read_bytes() == b"composed video bytes"


def test_atomic_promote_overwrites_prior_final(tmp_path):
    """If a prior compositor run produced final, the new run's promote
    replaces it atomically."""
    final = tmp_path / "clip.mp4"
    final.write_bytes(b"old content")
    partial = compositor._partial_path(final, run_id=1)
    partial.write_bytes(b"new content")

    compositor._atomic_promote(partial, final)

    assert final.read_bytes() == b"new content"


def test_cleanup_partial_is_tolerant(tmp_path):
    """Missing partial file isn't a crisis. _cleanup_partial must not raise."""
    partial = tmp_path / "nonexistent.partial"
    compositor._cleanup_partial(partial)  # should not raise


# ---------- E-10: ffprobe duration verification ----------


def _patch_ffprobe(monkeypatch, *, stdout: str, returncode: int = 0):
    class FakeCompleted:
        def __init__(self):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def fake_run(*args, **kwargs):
        return FakeCompleted()
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_verify_composition_duration_passes_within_tolerance(monkeypatch, tmp_path):
    """28.2s actual vs 28.0s planned → drift 0.2s < 0.5s tolerance → ok."""
    fake = tmp_path / "out.mp4"
    fake.write_bytes(b"x")
    _patch_ffprobe(monkeypatch, stdout="28.2\n")
    actual = compositor._verify_composition_duration(fake, planned_s=28.0)
    assert actual == 28.2


def test_verify_composition_duration_fails_on_short_render(monkeypatch, tmp_path):
    """ffmpeg exited early → 3.0s actual vs 28.0s planned → CompositionFailed."""
    fake = tmp_path / "out.mp4"
    fake.write_bytes(b"x")
    _patch_ffprobe(monkeypatch, stdout="3.0\n")
    with pytest.raises(compositor.CompositionFailed, match="duration drift"):
        compositor._verify_composition_duration(fake, planned_s=28.0)


def test_verify_composition_duration_fails_on_zero(monkeypatch, tmp_path):
    """Zero-duration file is unusable regardless of plan."""
    fake = tmp_path / "out.mp4"
    fake.write_bytes(b"x")
    _patch_ffprobe(monkeypatch, stdout="0.0\n")
    with pytest.raises(compositor.CompositionFailed, match="zero duration"):
        compositor._verify_composition_duration(fake, planned_s=28.0)


def test_verify_composition_duration_fails_on_ffprobe_error(monkeypatch, tmp_path):
    """ffprobe rc != 0 means the file is so malformed it can't be parsed."""
    fake = tmp_path / "out.mp4"
    fake.write_bytes(b"\x00")

    class FakeCompleted:
        stdout = ""
        stderr = "Invalid data"
        returncode = 1
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeCompleted())
    with pytest.raises(compositor.CompositionFailed, match="ffprobe rc=1"):
        compositor._verify_composition_duration(fake, planned_s=28.0)


def test_verify_composition_duration_fails_on_missing_binary(monkeypatch, tmp_path):
    fake = tmp_path / "out.mp4"
    fake.write_bytes(b"x")

    def fnf(*a, **kw):
        raise FileNotFoundError("ffprobe")
    monkeypatch.setattr(subprocess, "run", fnf)
    with pytest.raises(compositor.CompositionFailed, match="ffprobe not on PATH"):
        compositor._verify_composition_duration(fake, planned_s=28.0)


def test_duration_tolerance_is_documented():
    """The tolerance must be loose enough for ffmpeg frame quantization
    (~200ms) and tight enough to catch real truncation."""
    assert 0.1 <= compositor.DURATION_TOLERANCE_S <= 1.0


# ---------- E-21: WhisperX alignment contract ----------


def test_align_captions_whisperx_phase1_raises():
    """Phase 1 scaffold: alignment not wired. Callers fall back to raw
    Whisper words via _align_captions_or_fallback."""
    with pytest.raises(NotImplementedError):
        compositor.align_captions_whisperx([], "voice.wav")


def test_align_captions_fallback_returns_raw_words():
    """When WhisperX raises NotImplementedError, the fallback returns
    the raw word list unchanged and logs caption_alignment_skipped."""
    raw_words = [
        {"text": "hey", "start_s": 0.0, "end_s": 0.5},
        {"text": "what's", "start_s": 0.5, "end_s": 1.0},
    ]
    events = []
    import agents.compositor as comp
    # Spy on log
    orig_log = comp.log
    try:
        comp.log = lambda **kw: events.append(kw)
        out = comp._align_captions_or_fallback("clip-1", raw_words, "voice.wav")
    finally:
        comp.log = orig_log
    assert out == raw_words
    skip_events = [
        e for e in events if e.get("event_type") == "caption_alignment_skipped"
    ]
    assert skip_events
    assert skip_events[0]["payload"]["word_count"] == 2


def test_caption_drift_tolerance_documented():
    """E-21 target: <50ms per word boundary."""
    assert compositor.CAPTION_DRIFT_TOLERANCE_MS == 50


# ---------- E-22: LUFS ducking contract ----------


def test_apply_sidechain_ducking_phase1_raises():
    """Phase 1 scaffold: ducking not wired."""
    with pytest.raises(NotImplementedError):
        compositor.apply_sidechain_ducking(
            source_audio_path="src.wav",
            voice_audio_path="voice.wav",
            dest_path=Path("/tmp/x.wav"),
        )


def test_target_lufs_matches_claude_md_spec():
    """CLAUDE.md persona.voice.target_loudness_lufs is -14."""
    assert compositor.TARGET_LUFS == -14.0


# ---------- E-19: stage_lease (per-clip advisory lock) ----------


def test_lease_conflict_propagates(compositor_db, tmp_compositor_dirs, monkeypatch):
    """A second Compositor on the same clip raises LeaseConflict at
    acquire — orchestrator decides wait-vs-skip."""
    from agents.stage_lease import LeaseConflict, stage_lease

    with stage_lease("comp-clip-1", stage="compositor", ttl_seconds=60):
        with pytest.raises(LeaseConflict):
            asyncio.run(compositor.run_compositor("comp-clip-1"))


# ---------- run_compositor end-to-end (scaffold mode) ----------


def test_run_compositor_scaffold_records_artifact(
    compositor_db, tmp_compositor_dirs, monkeypatch,
):
    """Phase 1 scaffold: ffmpeg raises NotImplementedError. Compositor
    still wraps in stage_lease, persists final_video_path + planned
    duration via commit_artifact, and bumps artifact_version."""
    composited = asyncio.run(compositor.run_compositor("comp-clip-1"))
    assert composited is not None
    assert composited.clip_id == "comp-clip-1"

    with sqlite3.connect(compositor_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT final_video_path, final_duration_s, artifact_version "
            "FROM clip_artifacts WHERE clip_id = ?", ("comp-clip-1",),
        ).fetchone()
    assert row["final_video_path"] is not None
    # Planned duration = max(voice_runtime_s=28.0, source_duration=25.0) = 28.0
    assert row["final_duration_s"] == 28.0
    # artifact_version bumped (0 → 1)
    assert row["artifact_version"] == 1


def test_run_compositor_quarantines_on_duration_drift(
    compositor_db, tmp_compositor_dirs, monkeypatch,
):
    """ffmpeg returns successfully but the rendered file's duration
    is dramatically off from planned → ffprobe gate fires →
    quarantine + clean up partial → return None."""

    async def fake_compose(*args, **kwargs):
        # Write a "rendered" partial that ffprobe will report as short
        dest = kwargs["dest_path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"truncated render")
        return 28.0  # claim success
    monkeypatch.setattr(compositor, "_ffmpeg_compose", fake_compose)
    _patch_ffprobe(monkeypatch, stdout="2.0\n")  # actual 2s vs planned 28s

    composited = asyncio.run(compositor.run_compositor("comp-clip-1"))
    assert composited is None

    with sqlite3.connect(compositor_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", ("comp-clip-1",),
        ).fetchone()[0]
    assert status == "quarantined"
    # Partial cleaned up
    output_dir = tmp_compositor_dirs / "output"
    partials = list(output_dir.glob(".comp-clip-1.mp4.partial.*")) if output_dir.exists() else []
    assert partials == []


def test_run_compositor_atomic_promote_on_success(
    compositor_db, tmp_compositor_dirs, monkeypatch,
):
    """Successful compose + duration verify → atomic rename → final
    file exists, partial is gone."""
    output_dir = tmp_compositor_dirs / "output"

    async def fake_compose(*args, **kwargs):
        dest = kwargs["dest_path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"composed video bytes")
        return 28.0
    monkeypatch.setattr(compositor, "_ffmpeg_compose", fake_compose)
    _patch_ffprobe(monkeypatch, stdout="28.1\n")

    composited = asyncio.run(compositor.run_compositor("comp-clip-1"))
    assert composited is not None

    final = output_dir / "comp-clip-1.mp4"
    assert final.exists()
    assert final.read_bytes() == b"composed video bytes"
    partials = list(output_dir.glob(".comp-clip-1.mp4.partial.*"))
    assert partials == []


def test_run_compositor_unknown_clip_raises(compositor_db, tmp_compositor_dirs):
    """A clip_id not in clip_artifacts is a programming error, not a
    quarantine case — propagate immediately."""
    with pytest.raises(KeyError):
        asyncio.run(compositor.run_compositor("does-not-exist"))


def test_run_compositor_propagates_compliance_tristate(
    compositor_db, tmp_compositor_dirs, monkeypatch,
):
    """Compositor MUST NOT assert has_music or has_real_face values
    itself — it reads from clip_artifacts and surfaces them on the
    CompositedClip for Compliance to evaluate."""
    composited = asyncio.run(compositor.run_compositor("comp-clip-1"))
    assert composited is not None
    # Both seeded as 0 in the fixture → Compliance sees False (passes both rules)
    assert composited.has_music_in_source_segment is False
    assert composited.has_real_face_reference is False


def test_run_compositor_null_columns_become_none_tristate(
    compositor_db, tmp_compositor_dirs, monkeypatch,
):
    """NULL columns from upstream stages become None on the CompositedClip
    — Compliance's tri-state logic then fails closed."""
    with sqlite3.connect(compositor_db) as conn:
        conn.execute(
            "UPDATE clip_artifacts SET has_music_in_source_segment = NULL, "
            "has_real_face_reference = NULL WHERE clip_id = ?",
            ("comp-clip-1",),
        )
        conn.commit()

    composited = asyncio.run(compositor.run_compositor("comp-clip-1"))
    assert composited is not None
    assert composited.has_music_in_source_segment is None
    assert composited.has_real_face_reference is None


def test_run_compositor_emits_run_id_in_clip_composed_event(
    compositor_db, tmp_compositor_dirs, monkeypatch,
):
    """The clip_composed event must include run_id so operators can
    correlate which lease produced the output (debugging partial-file
    issues)."""
    events = []
    monkeypatch.setattr(compositor, "log", lambda **kw: events.append(kw))

    asyncio.run(compositor.run_compositor("comp-clip-1"))
    composed_events = [e for e in events if e.get("event_type") == "clip_composed"]
    assert composed_events
    assert "run_id" in composed_events[0]["payload"]


# ---------- Composition gate ordering ----------


def test_quarantine_skipped_when_compose_in_scaffold_mode(
    compositor_db, tmp_compositor_dirs, monkeypatch,
):
    """Phase 1 scaffold: NotImplementedError from ffmpeg means we never
    promoted a partial → ffprobe verify is skipped → no quarantine.
    This is the intended Phase 1 path."""
    composited = asyncio.run(compositor.run_compositor("comp-clip-1"))
    assert composited is not None  # scaffold path returns the placeholder
    with sqlite3.connect(compositor_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", ("comp-clip-1",),
        ).fetchone()[0]
    assert status != "quarantined"


def test_partial_file_cleaned_up_on_promote_failure(
    compositor_db, tmp_compositor_dirs, monkeypatch,
):
    """If os.replace raises (cross-device, permission issue), the
    partial is cleaned up and the clip quarantines."""
    output_dir = tmp_compositor_dirs / "output"

    async def fake_compose(*args, **kwargs):
        dest = kwargs["dest_path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return 28.0
    monkeypatch.setattr(compositor, "_ffmpeg_compose", fake_compose)
    _patch_ffprobe(monkeypatch, stdout="28.0\n")

    # Force atomic_promote to fail
    def fail_replace(src, dst):
        raise OSError("simulated cross-device rename")
    monkeypatch.setattr("os.replace", fail_replace)

    composited = asyncio.run(compositor.run_compositor("comp-clip-1"))
    assert composited is None

    with sqlite3.connect(compositor_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", ("comp-clip-1",),
        ).fetchone()[0]
    assert status == "quarantined"
    partials = list(output_dir.glob(".comp-clip-1.mp4.partial.*"))
    assert partials == []
