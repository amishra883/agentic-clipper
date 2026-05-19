"""Editor tests — Days 5-6 hardening (E-15, E-16, E-8, music wire-in).

Critical assertions:
- ffprobe missing video stream / zero duration / truncated → PartialDownloadError
- partial download routes clip to /data/clips/quarantine/ + flips
  candidate status to 'quarantined'
- all-no_speech transcript routes to quarantine (E-15)
- music detector populates has_music_in_source_segment when method is wired
- env-unset method leaves has_music_in_source_segment as NULL (Compliance
  fails closed by design)
- stage_lease(editor) prevents double-run on same clip
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.db import init_schema
from agents.editor import (
    NO_SPEECH_THRESHOLD,
    PARTIAL_DOWNLOAD_TOLERANCE_RATIO,
    PartialDownloadError,
    QUARANTINE_CLIP_DIR,
    RAW_CLIP_DIR,
    _is_all_no_speech,
    _quarantine_clip,
    _run_music_detection,
    _validate_downloaded_clip,
    run_editor,
)
from agents.models import TranscriptSegment
from agents.stage_lease import stage_lease
from scripts.migrate import migrate


@pytest.fixture
def editor_db(monkeypatch):
    """Fresh DB through the latest migration."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "editor.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        migrate(db_path=db_path)
        yield db_path


def _seed_candidate(db_path: Path, *, clip_id: str = "twitch-edit-0001",
                    source_duration_s: float = 30.0):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO clips_candidate
              (id, creator, source_platform, source_url, source_duration_s, status)
            VALUES (?, 'IShowSpeed', 'twitch',
                    'https://twitch.tv/ishowspeed/clip/abcdefghij',
                    ?, 'curated')
            """,
            (clip_id, source_duration_s),
        )
        conn.commit()


# ---------- E-16: ffprobe partial-download validation ----------


def _make_fake_probe_output(*, duration: str | None, has_video: bool) -> str:
    """Build a JSON string mimicking ffprobe -of json output."""
    streams = []
    if has_video:
        streams.append({"codec_type": "video", "codec_name": "h264"})
    streams.append({"codec_type": "audio", "codec_name": "aac"})
    payload = {
        "streams": streams,
        "format": {"duration": duration} if duration else {},
    }
    return json.dumps(payload)


def _patch_ffprobe(monkeypatch, *, stdout: str, returncode: int = 0):
    class FakeCompleted:
        def __init__(self):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def fake_run(*args, **kwargs):
        return FakeCompleted()
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_ffprobe_validates_full_download(monkeypatch, tmp_path):
    """A 30s download with a video stream and matching duration passes."""
    fake_file = tmp_path / "clip.mp4"
    fake_file.write_bytes(b"x" * 1024)  # non-empty
    _patch_ffprobe(monkeypatch, stdout=_make_fake_probe_output(
        duration="29.5", has_video=True,
    ))
    actual = _validate_downloaded_clip(fake_file, expected_duration_s=30.0)
    assert actual == 29.5


def test_ffprobe_rejects_missing_file():
    """Nonexistent path → PartialDownloadError (no ffprobe call needed)."""
    with pytest.raises(PartialDownloadError, match="missing or zero-bytes"):
        _validate_downloaded_clip(
            Path("/nonexistent/file.mp4"), expected_duration_s=30.0,
        )


def test_ffprobe_rejects_zero_byte_file(tmp_path):
    """Empty file (yt-dlp gave up before writing anything) → quarantine."""
    fake_file = tmp_path / "empty.mp4"
    fake_file.touch()
    with pytest.raises(PartialDownloadError, match="zero-bytes"):
        _validate_downloaded_clip(fake_file, expected_duration_s=30.0)


def test_ffprobe_rejects_no_video_stream(monkeypatch, tmp_path):
    """ffprobe finds audio but no video — yt-dlp grabbed wrong format."""
    fake_file = tmp_path / "audio_only.mp4"
    fake_file.write_bytes(b"x" * 512)
    _patch_ffprobe(monkeypatch, stdout=_make_fake_probe_output(
        duration="30.0", has_video=False,
    ))
    with pytest.raises(PartialDownloadError, match="no video stream"):
        _validate_downloaded_clip(fake_file, expected_duration_s=30.0)


def test_ffprobe_rejects_truncated_download(monkeypatch, tmp_path):
    """Source was 30s but download came back as 5s — under the 80% tolerance."""
    fake_file = tmp_path / "short.mp4"
    fake_file.write_bytes(b"x" * 512)
    _patch_ffprobe(monkeypatch, stdout=_make_fake_probe_output(
        duration="5.0", has_video=True,
    ))
    with pytest.raises(PartialDownloadError, match="truncated download"):
        _validate_downloaded_clip(fake_file, expected_duration_s=30.0)


def test_ffprobe_accepts_within_tolerance(monkeypatch, tmp_path):
    """At exactly the 80% ratio, the download is borderline-acceptable."""
    fake_file = tmp_path / "borderline.mp4"
    fake_file.write_bytes(b"x" * 512)
    _patch_ffprobe(monkeypatch, stdout=_make_fake_probe_output(
        duration="24.0", has_video=True,  # 24/30 = 0.80 exactly
    ))
    actual = _validate_downloaded_clip(fake_file, expected_duration_s=30.0)
    assert actual == 24.0


def test_ffprobe_rejects_zero_duration(monkeypatch, tmp_path):
    fake_file = tmp_path / "bad.mp4"
    fake_file.write_bytes(b"x" * 256)
    _patch_ffprobe(monkeypatch, stdout=_make_fake_probe_output(
        duration="0.0", has_video=True,
    ))
    with pytest.raises(PartialDownloadError, match="duration <= 0"):
        _validate_downloaded_clip(fake_file, expected_duration_s=30.0)


def test_ffprobe_rejects_nonzero_returncode(monkeypatch, tmp_path):
    """ffprobe rc=1 means the file is so malformed it can't be parsed."""
    fake_file = tmp_path / "garbage.mp4"
    fake_file.write_bytes(b"\x00\x01\x02")

    class FakeCompleted:
        stdout = ""
        stderr = "Invalid data found when processing input"
        returncode = 1
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeCompleted())
    with pytest.raises(PartialDownloadError, match="ffprobe rc=1"):
        _validate_downloaded_clip(fake_file, expected_duration_s=30.0)


def test_ffprobe_rejects_missing_binary(monkeypatch, tmp_path):
    """ffprobe not on PATH — clear error pointing operator to `make setup`."""
    fake_file = tmp_path / "any.mp4"
    fake_file.write_bytes(b"x" * 256)
    def raises_fnf(*a, **kw):
        raise FileNotFoundError("ffprobe")
    monkeypatch.setattr(subprocess, "run", raises_fnf)
    with pytest.raises(PartialDownloadError, match="ffprobe not on PATH"):
        _validate_downloaded_clip(fake_file, expected_duration_s=30.0)


# ---------- E-15: Whisper no-speech quarantine ----------


def test_no_speech_quarantine_triggers_when_all_segments_silent():
    """Every segment > NO_SPEECH_THRESHOLD → quarantine."""
    transcript = [
        TranscriptSegment(start_s=0, end_s=10, text="", no_speech_prob=0.92),
        TranscriptSegment(start_s=10, end_s=20, text="", no_speech_prob=0.95),
        TranscriptSegment(start_s=20, end_s=30, text="", no_speech_prob=0.88),
    ]
    assert _is_all_no_speech(transcript) is True


def test_no_speech_quarantine_skipped_when_any_segment_has_speech():
    """Even one segment under threshold is enough to keep the clip."""
    transcript = [
        TranscriptSegment(start_s=0, end_s=10, text="", no_speech_prob=0.99),
        TranscriptSegment(start_s=10, end_s=20, text="hey", no_speech_prob=0.10),
    ]
    assert _is_all_no_speech(transcript) is False


def test_no_speech_quarantine_triggers_on_empty_transcript():
    """Whisper returned nothing at all → treat as silent (quarantine)."""
    assert _is_all_no_speech([]) is True


def test_no_speech_skipped_when_whisper_did_not_report_prob():
    """Older Whisper builds don't report no_speech_prob. If none of the
    segments have it set, we can't enforce E-15 and we DON'T quarantine
    (the caller falls back to the punch-segment selection)."""
    transcript = [
        TranscriptSegment(start_s=0, end_s=10, text="hi", no_speech_prob=None),
        TranscriptSegment(start_s=10, end_s=20, text="bye", no_speech_prob=None),
    ]
    assert _is_all_no_speech(transcript) is False


def test_no_speech_threshold_is_above_typical_speech():
    """Sanity: speech segments typically score ~0.0-0.2; non-speech ~0.85+.
    Threshold sits above the typical-speech band."""
    assert NO_SPEECH_THRESHOLD >= 0.80
    assert NO_SPEECH_THRESHOLD <= 0.95


# ---------- Music detection wire-in ----------


def test_music_detection_returns_none_when_method_unset(tmp_path):
    """Env unset → method=None → wire-in returns None → DB column NULL.
    Compliance fails closed on NULL, which is the desired scaffold behavior."""
    fake_file = tmp_path / "any.mp4"
    fake_file.write_bytes(b"x" * 256)
    result = _run_music_detection(fake_file, start_s=0.0, end_s=25.0, method=None)
    assert result is None


def test_music_detection_returns_none_when_file_missing():
    """No file → can't run the detector → return None (NOT True/False)."""
    result = _run_music_detection(
        Path("/nope.mp4"), start_s=0.0, end_s=25.0, method="placeholder-energy",
    )
    assert result is None


def test_music_detection_returns_int_when_method_wired(tmp_path):
    """Configured detector returns 0/1 (NOT None) — that's what goes into
    clip_artifacts.has_music_in_source_segment."""
    fake_file = tmp_path / "audio.mp4"
    fake_file.write_bytes(b"x" * 1024)
    result = _run_music_detection(
        fake_file, start_s=0.0, end_s=25.0, method="placeholder-energy",
    )
    assert result in (0, 1)


# ---------- _quarantine_clip ----------


def test_quarantine_flips_status_and_moves_file(editor_db, tmp_path):
    _seed_candidate(editor_db)
    fake = tmp_path / "clip.mp4"
    fake.write_bytes(b"partial")
    _quarantine_clip(
        "twitch-edit-0001",
        local_path=fake,
        reason="partial_download",
        detail="ffprobe rc=1",
    )
    # File moved out of source path
    assert not fake.exists()
    # DB status flipped
    with sqlite3.connect(editor_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?",
            ("twitch-edit-0001",),
        ).fetchone()[0]
    assert status == "quarantined"


def test_quarantine_tolerates_missing_file(editor_db):
    """If the download truly never wrote, _quarantine_clip still flips
    the DB and logs."""
    _seed_candidate(editor_db)
    _quarantine_clip(
        "twitch-edit-0001",
        local_path=Path("/nonexistent/file.mp4"),
        reason="download_exhausted",
        detail="yt-dlp killed before writing",
    )
    with sqlite3.connect(editor_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?",
            ("twitch-edit-0001",),
        ).fetchone()[0]
    assert status == "quarantined"


# ---------- run_editor end-to-end (scaffold mode) ----------


def test_run_editor_scaffold_mode_inserts_placeholder_artifact(editor_db):
    """Phase 1 scaffold: yt-dlp/whisper raise NotImplementedError. The
    Editor still wraps in a stage_lease, persists a placeholder artifact
    (empty transcript, NULL has_music), and flips status to 'processing'."""
    _seed_candidate(editor_db)
    asyncio.run(run_editor("twitch-edit-0001"))
    with sqlite3.connect(editor_db) as conn:
        conn.row_factory = sqlite3.Row
        artifact = conn.execute(
            "SELECT * FROM clip_artifacts WHERE clip_id = ?",
            ("twitch-edit-0001",),
        ).fetchone()
        candidate_status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?",
            ("twitch-edit-0001",),
        ).fetchone()[0]
    assert artifact is not None
    assert artifact["punch_segment_start_s"] == 0.0
    assert artifact["punch_segment_end_s"] == 25.0
    # Music detector is unwired in scaffold; column stays NULL → Compliance fails closed.
    assert artifact["has_music_in_source_segment"] is None
    assert candidate_status == "processing"


def test_run_editor_lease_conflict_backs_off_quietly(editor_db):
    """Two Editors on the same clip — second one hits LeaseConflict at
    acquire time, logs info, returns without touching DB state."""
    _seed_candidate(editor_db)
    # Hold the editor lease externally; run_editor must back off.
    with stage_lease("twitch-edit-0001", stage="editor", ttl_seconds=60):
        asyncio.run(run_editor("twitch-edit-0001"))
    # No artifact written; candidate status untouched.
    with sqlite3.connect(editor_db) as conn:
        artifact = conn.execute(
            "SELECT * FROM clip_artifacts WHERE clip_id = ?",
            ("twitch-edit-0001",),
        ).fetchone()
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?",
            ("twitch-edit-0001",),
        ).fetchone()[0]
    assert artifact is None
    assert status == "curated"  # untouched by the conflicting editor


def test_run_editor_unknown_clip_raises(editor_db):
    """A clip_id not in clips_candidate is a programming error, not a
    quarantine case — propagate immediately."""
    with pytest.raises(KeyError):
        asyncio.run(run_editor("twitch-does-not-exist"))


# ---------- run_editor with download + transcribe injected ----------


def test_run_editor_partial_download_quarantines(editor_db, monkeypatch, tmp_path):
    """Download succeeds but ffprobe says the file is truncated →
    clip lands in quarantine, no transcript / artifact / 'processing' flip."""
    _seed_candidate(editor_db)

    # Override RAW_CLIP_DIR so the test doesn't write to the repo
    monkeypatch.setattr("agents.editor.RAW_CLIP_DIR", tmp_path / "raw")
    monkeypatch.setattr("agents.editor.QUARANTINE_CLIP_DIR", tmp_path / "quarantine")

    async def fake_download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"truncated")
    monkeypatch.setattr("agents.editor._download_source", fake_download)
    _patch_ffprobe(monkeypatch, stdout=_make_fake_probe_output(
        duration="3.0", has_video=True,  # 3/30 = 0.10 < 0.80 ratio
    ))

    asyncio.run(run_editor("twitch-edit-0001"))

    with sqlite3.connect(editor_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?",
            ("twitch-edit-0001",),
        ).fetchone()[0]
        artifact = conn.execute(
            "SELECT * FROM clip_artifacts WHERE clip_id = ?",
            ("twitch-edit-0001",),
        ).fetchone()
    assert status == "quarantined"
    assert artifact is None


def test_run_editor_all_no_speech_quarantines(editor_db, monkeypatch, tmp_path):
    """Download + ffprobe pass, but Whisper sees only silence/instrumental
    → quarantine before downstream agents pay for an unworkable clip."""
    _seed_candidate(editor_db)

    monkeypatch.setattr("agents.editor.RAW_CLIP_DIR", tmp_path / "raw")
    monkeypatch.setattr("agents.editor.QUARANTINE_CLIP_DIR", tmp_path / "quarantine")

    async def fake_download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 4096)
    monkeypatch.setattr("agents.editor._download_source", fake_download)
    _patch_ffprobe(monkeypatch, stdout=_make_fake_probe_output(
        duration="28.0", has_video=True,
    ))

    async def fake_transcribe(path):
        return [
            TranscriptSegment(start_s=0, end_s=10, text="", no_speech_prob=0.95),
            TranscriptSegment(start_s=10, end_s=20, text="", no_speech_prob=0.91),
        ]
    monkeypatch.setattr("agents.editor._transcribe", fake_transcribe)

    asyncio.run(run_editor("twitch-edit-0001"))

    with sqlite3.connect(editor_db) as conn:
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?",
            ("twitch-edit-0001",),
        ).fetchone()[0]
    assert status == "quarantined"


def test_run_editor_full_path_with_music_detection(
    editor_db, monkeypatch, tmp_path,
):
    """Download + ffprobe + transcribe + music-detection all succeed; the
    artifact row carries has_music_in_source_segment AND the candidate
    flips to 'processing'."""
    _seed_candidate(editor_db)
    monkeypatch.setattr("agents.editor.RAW_CLIP_DIR", tmp_path / "raw")
    monkeypatch.setattr("agents.editor.QUARANTINE_CLIP_DIR", tmp_path / "quarantine")
    monkeypatch.setenv("EDITOR_MUSIC_DETECTOR_METHOD", "placeholder-energy")

    async def fake_download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 4096)
    monkeypatch.setattr("agents.editor._download_source", fake_download)
    _patch_ffprobe(monkeypatch, stdout=_make_fake_probe_output(
        duration="28.5", has_video=True,
    ))

    async def fake_transcribe(path):
        return [
            TranscriptSegment(
                start_s=0, end_s=10, text="hey what's up", no_speech_prob=0.05,
            ),
        ]
    monkeypatch.setattr("agents.editor._transcribe", fake_transcribe)

    asyncio.run(run_editor("twitch-edit-0001"))

    with sqlite3.connect(editor_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM clip_artifacts WHERE clip_id = ?",
            ("twitch-edit-0001",),
        ).fetchone()
        status = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?",
            ("twitch-edit-0001",),
        ).fetchone()[0]
    assert row is not None
    # placeholder-energy returns 0 or 1 — both valid; just verify it's not NULL
    assert row["has_music_in_source_segment"] in (0, 1)
    assert status == "processing"


def test_invalid_music_detector_method_raises():
    """A bogus EDITOR_MUSIC_DETECTOR_METHOD env value should fail fast,
    not silently fall through to None (which would leave the column
    NULL and quietly bypass the music gate)."""
    import os
    from agents.editor import _music_detector_method
    saved = os.environ.get("EDITOR_MUSIC_DETECTOR_METHOD")
    try:
        os.environ["EDITOR_MUSIC_DETECTOR_METHOD"] = "wishful-thinking"
        with pytest.raises(ValueError, match="not a recognized DetectorMethod"):
            _music_detector_method()
    finally:
        if saved is None:
            os.environ.pop("EDITOR_MUSIC_DETECTOR_METHOD", None)
        else:
            os.environ["EDITOR_MUSIC_DETECTOR_METHOD"] = saved


def test_partial_download_tolerance_ratio_documented():
    """The tolerance must stay strict enough to catch true truncations
    (10s into a 30s clip = 0.33 ratio, MUST quarantine) but loose enough
    that mp4 container overhead doesn't trip it."""
    assert 0.50 <= PARTIAL_DOWNLOAD_TOLERANCE_RATIO <= 0.95


def test_raw_and_quarantine_dirs_distinct():
    """Don't accidentally point both at the same path — that'd let
    a quarantined clip get re-picked up by the next Editor run."""
    assert RAW_CLIP_DIR.resolve() != QUARANTINE_CLIP_DIR.resolve()
