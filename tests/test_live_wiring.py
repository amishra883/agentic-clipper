"""Tests for the live-wired stages (Editor.yt-dlp, Editor.whisper,
Voice.coqui, Compositor.ffmpeg, Compositor.whisperx,
Compositor.sidechaincompress).

These can't actually invoke the underlying tools in CI — the tools may
not be installed and the calls would touch the network / consume CPU
for tens of seconds. Instead we mock the subprocess boundary and the
optional Python packages and verify the SHAPE of the wiring:

  - Missing binary / missing package → NotImplementedError so the
    orchestrator falls back to scaffold mode (existing tests already
    cover the scaffold path; this asserts the contract).
  - Subprocess success path → returns the expected runtime/duration.
  - Subprocess transient failure → raises TransientError so
    retry_external retries.
  - Subprocess permanent failure → raises RuntimeError (Editor) or
    CompositionFailed (Compositor) without retry.

What we are NOT testing here: actual output quality, codec choices,
filter graph correctness. Those are integration concerns that need a
live runner with the binaries installed.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agents import editor as editor_mod
from agents import voice as voice_mod
from agents import compositor as compositor_mod
from agents.retry import RetryGiveUp, TransientError


# ----------------------------------------------------------------------
# Editor._download_source
# ----------------------------------------------------------------------

def test_download_source_missing_yt_dlp_raises_not_implemented(monkeypatch, tmp_path):
    monkeypatch.setattr(editor_mod.shutil, "which", lambda name: None)
    with pytest.raises(NotImplementedError, match="yt-dlp"):
        asyncio.run(editor_mod._download_source.__wrapped__(
            "https://test/x", tmp_path / "x.mp4",
        ))


def test_download_source_success_returns_silently(monkeypatch, tmp_path):
    """Happy path: subprocess returns rc=0, file exists non-empty."""
    monkeypatch.setattr(editor_mod.shutil, "which", lambda name: "/usr/local/bin/yt-dlp")
    dest = tmp_path / "x.mp4"

    def fake_run(cmd, **kw):
        # Simulate yt-dlp creating the file.
        dest.write_bytes(b"\x00" * 1024)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    # asyncio.to_thread inside the wrapper hops through a real thread —
    # patching subprocess.run is sufficient.

    asyncio.run(editor_mod._download_source.__wrapped__(
        "https://test/x", dest,
    ))
    assert dest.exists()
    assert dest.stat().st_size == 1024


def test_download_source_transient_stderr_marker_raises_transient(monkeypatch, tmp_path):
    monkeypatch.setattr(editor_mod.shutil, "which", lambda name: "/usr/local/bin/yt-dlp")

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, 1, "", "ERROR: HTTP Error 503: Service Unavailable",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TransientError, match="transient"):
        asyncio.run(editor_mod._download_source.__wrapped__(
            "https://test/x", tmp_path / "x.mp4",
        ))


def test_download_source_permanent_failure_raises_runtime(monkeypatch, tmp_path):
    """Age-gate / geo-block / content removed → permanent. Doesn't match
    any transient marker, so it raises RuntimeError and the caller
    quarantines without burning retries."""
    monkeypatch.setattr(editor_mod.shutil, "which", lambda name: "/usr/local/bin/yt-dlp")

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, 1, "", "ERROR: Video unavailable (region-blocked).",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="yt-dlp failed"):
        asyncio.run(editor_mod._download_source.__wrapped__(
            "https://test/x", tmp_path / "x.mp4",
        ))


# ----------------------------------------------------------------------
# Editor._transcribe
# ----------------------------------------------------------------------

def test_transcribe_missing_faster_whisper_raises_not_implemented(monkeypatch, tmp_path):
    monkeypatch.setattr(editor_mod, "_get_whisper_model", lambda: None)
    with pytest.raises(NotImplementedError, match="faster-whisper"):
        asyncio.run(editor_mod._transcribe.__wrapped__(tmp_path / "x.mp4"))


def test_transcribe_happy_path_returns_segments(monkeypatch, tmp_path):
    """faster-whisper returns generator of segments; we flatten + convert."""
    audio = tmp_path / "x.mp4"
    audio.write_bytes(b"\x00" * 64)

    fake_word = MagicMock(start=0.0, end=0.5, word=" hello")
    fake_seg = MagicMock(
        start=0.0, end=0.5, text=" hello",
        words=[fake_word], no_speech_prob=0.1,
    )
    fake_model = MagicMock()
    fake_model.transcribe = MagicMock(return_value=(iter([fake_seg]), MagicMock()))
    monkeypatch.setattr(editor_mod, "_get_whisper_model", lambda: fake_model)

    out = asyncio.run(editor_mod._transcribe.__wrapped__(audio))
    assert len(out) == 1
    assert out[0].text == "hello"
    assert out[0].start_s == 0.0
    assert out[0].end_s == 0.5
    assert out[0].words == [{"start_s": 0.0, "end_s": 0.5, "text": " hello"}]
    assert out[0].no_speech_prob == pytest.approx(0.1)


# ----------------------------------------------------------------------
# Voice._synthesize_coqui
# ----------------------------------------------------------------------

def test_synthesize_coqui_missing_package_raises_not_implemented(monkeypatch, tmp_path):
    monkeypatch.setattr(voice_mod, "_get_coqui_tts", lambda: None)
    with pytest.raises(NotImplementedError, match="Coqui"):
        asyncio.run(voice_mod._synthesize_coqui(
            "hello", {"voice": {"language": "en"}}, tmp_path / "out.wav",
        ))


def test_synthesize_coqui_writes_wav_and_returns_runtime(monkeypatch, tmp_path):
    """Mocked TTS writes a real WAV header so the stdlib reader can give
    us a runtime."""
    import wave

    out = tmp_path / "out.wav"

    def fake_tts_to_file(**kwargs):
        # Write a minimal 1-second WAV (22050Hz mono, 16-bit).
        with wave.open(str(out), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(b"\x00\x00" * 22050)

    fake_tts = MagicMock()
    fake_tts.tts_to_file = fake_tts_to_file
    monkeypatch.setattr(voice_mod, "_get_coqui_tts", lambda: fake_tts)

    runtime = asyncio.run(voice_mod._synthesize_coqui(
        "hello world", {"voice": {"language": "en"}}, out,
    ))
    assert out.exists()
    assert runtime == pytest.approx(1.0, rel=0.01)


def test_synthesize_coqui_empty_output_raises_transient(monkeypatch, tmp_path):
    """An empty file is a TransientError so retry_external retries —
    sometimes Coqui races on first model load."""
    out = tmp_path / "out.wav"

    fake_tts = MagicMock()
    fake_tts.tts_to_file = lambda **kw: out.write_bytes(b"")  # zero bytes
    monkeypatch.setattr(voice_mod, "_get_coqui_tts", lambda: fake_tts)

    with pytest.raises(TransientError, match="empty"):
        asyncio.run(voice_mod._synthesize_coqui(
            "hello", {"voice": {"language": "en"}}, out,
        ))


# ----------------------------------------------------------------------
# Compositor._ffmpeg_compose
# ----------------------------------------------------------------------

def _make_voice_track(path: str, runtime_s: float = 30.0):
    from agents.models import AudioTrack
    return AudioTrack(
        path=path, runtime_s=runtime_s, loudness_lufs=-14.0,
        engine="coqui_xtts_v2", voice_id="manic_reactor_coqui_default_v1",
    )


def test_ffmpeg_compose_missing_binary_raises_not_implemented(monkeypatch, tmp_path):
    monkeypatch.setattr(compositor_mod, "_ffmpeg_bin", lambda: None)
    source = tmp_path / "src.mp4"
    source.write_bytes(b"\x00" * 64)
    voice = tmp_path / "v.wav"
    voice.write_bytes(b"\x00" * 64)

    with pytest.raises(NotImplementedError, match="ffmpeg"):
        asyncio.run(compositor_mod._ffmpeg_compose.__wrapped__(
            source_path=str(source), source_start_s=0.0, source_end_s=20.0,
            voice_track=_make_voice_track(str(voice)),
            visuals=[], punch_beats=[], captions=[],
            dest_path=tmp_path / "out.mp4",
        ))


def test_ffmpeg_compose_happy_path(monkeypatch, tmp_path):
    """ffmpeg returns rc=0, file exists; _ffprobe_duration mocked to a
    known value; compose returns that duration."""
    monkeypatch.setattr(compositor_mod, "_ffmpeg_bin", lambda: "/usr/local/bin/ffmpeg")
    source = tmp_path / "src.mp4"
    source.write_bytes(b"\x00" * 64)
    voice = tmp_path / "v.wav"
    voice.write_bytes(b"\x00" * 64)
    dest = tmp_path / "out.mp4"

    def fake_run(cmd, **kw):
        dest.write_bytes(b"\x00" * 8192)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(compositor_mod, "_ffprobe_duration", lambda p: 42.5)

    duration = asyncio.run(compositor_mod._ffmpeg_compose.__wrapped__(
        source_path=str(source), source_start_s=0.0, source_end_s=20.0,
        voice_track=_make_voice_track(str(voice), runtime_s=30.0),
        visuals=[], punch_beats=[],
        captions=[{"text": "hello", "start_s": 0.0, "end_s": 0.5}],
        dest_path=dest,
    ))
    assert duration == 42.5
    assert dest.exists()


def test_ffmpeg_compose_missing_inputs_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(compositor_mod, "_ffmpeg_bin", lambda: "/usr/local/bin/ffmpeg")
    # Source path doesn't exist.
    with pytest.raises(RuntimeError, match="source file missing"):
        asyncio.run(compositor_mod._ffmpeg_compose.__wrapped__(
            source_path="/no/such/source.mp4", source_start_s=0.0, source_end_s=20.0,
            voice_track=_make_voice_track(str(tmp_path / "v.wav")),
            visuals=[], punch_beats=[], captions=[],
            dest_path=tmp_path / "out.mp4",
        ))


def test_ffmpeg_compose_permanent_failure_raises_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(compositor_mod, "_ffmpeg_bin", lambda: "/usr/local/bin/ffmpeg")
    source = tmp_path / "src.mp4"
    source.write_bytes(b"\x00" * 64)
    voice = tmp_path / "v.wav"
    voice.write_bytes(b"\x00" * 64)

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "Error: malformed filter graph")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        asyncio.run(compositor_mod._ffmpeg_compose.__wrapped__(
            source_path=str(source), source_start_s=0.0, source_end_s=20.0,
            voice_track=_make_voice_track(str(voice)),
            visuals=[], punch_beats=[], captions=[],
            dest_path=tmp_path / "out.mp4",
        ))


def test_ffmpeg_compose_cleans_up_caption_file(monkeypatch, tmp_path):
    """The .ass caption file should be deleted whether the compose
    succeeds or fails — no orphan files in the output dir."""
    monkeypatch.setattr(compositor_mod, "_ffmpeg_bin", lambda: "/usr/local/bin/ffmpeg")
    source = tmp_path / "src.mp4"
    source.write_bytes(b"\x00" * 64)
    voice = tmp_path / "v.wav"
    voice.write_bytes(b"\x00" * 64)
    dest = tmp_path / "out.mp4"

    captured_ass: list[Path] = []

    def fake_run(cmd, **kw):
        # Confirm the .ass file exists during the run.
        for arg in cmd:
            if isinstance(arg, str) and arg.endswith(".ass"):
                captured_ass.append(Path(arg))
        # Look for the captions file the compose helper writes.
        for ass in dest.parent.glob("*.ass"):
            captured_ass.append(ass)
        dest.write_bytes(b"\x00" * 8192)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(compositor_mod, "_ffprobe_duration", lambda p: 10.0)

    asyncio.run(compositor_mod._ffmpeg_compose.__wrapped__(
        source_path=str(source), source_start_s=0.0, source_end_s=10.0,
        voice_track=_make_voice_track(str(voice), runtime_s=10.0),
        visuals=[], punch_beats=[],
        captions=[{"text": "hello", "start_s": 0.0, "end_s": 0.5}],
        dest_path=dest,
    ))
    # The .ass file should be cleaned up after compose returns.
    leftover = list(dest.parent.glob("*.ass"))
    assert leftover == [], f"caption file not cleaned up: {leftover}"


# ----------------------------------------------------------------------
# Compositor.align_captions_whisperx
# ----------------------------------------------------------------------

def test_align_captions_whisperx_no_audio_raises_not_implemented(tmp_path):
    """Empty word list or missing audio file → NotImplementedError so
    caller falls back to raw words."""
    with pytest.raises(NotImplementedError):
        compositor_mod.align_captions_whisperx([], "/no/such/audio.wav")


def test_align_captions_whisperx_missing_package_raises_not_implemented(monkeypatch, tmp_path):
    audio = tmp_path / "v.wav"
    audio.write_bytes(b"\x00" * 64)
    # Pre-load None into sys.modules so the import inside the function fails.
    monkeypatch.setitem(sys.modules, "whisperx", None)
    with pytest.raises(NotImplementedError, match="whisperx"):
        compositor_mod.align_captions_whisperx(
            [{"text": "hi", "start_s": 0.0, "end_s": 0.5}], str(audio),
        )


# ----------------------------------------------------------------------
# Compositor.apply_sidechain_ducking
# ----------------------------------------------------------------------

def test_sidechain_ducking_missing_binary_raises_not_implemented(monkeypatch, tmp_path):
    monkeypatch.setattr(compositor_mod, "_ffmpeg_bin", lambda: None)
    with pytest.raises(NotImplementedError, match="ffmpeg"):
        compositor_mod.apply_sidechain_ducking(
            source_audio_path=str(tmp_path / "a.wav"),
            voice_audio_path=str(tmp_path / "v.wav"),
            dest_path=tmp_path / "out.wav",
        )


def test_sidechain_ducking_missing_inputs_raises_composition_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(compositor_mod, "_ffmpeg_bin", lambda: "/usr/local/bin/ffmpeg")
    with pytest.raises(compositor_mod.CompositionFailed, match="source audio missing"):
        compositor_mod.apply_sidechain_ducking(
            source_audio_path=str(tmp_path / "no_such.wav"),
            voice_audio_path=str(tmp_path / "no_such_v.wav"),
            dest_path=tmp_path / "out.wav",
        )


def test_sidechain_ducking_happy_path_returns_measured_lufs(monkeypatch, tmp_path):
    monkeypatch.setattr(compositor_mod, "_ffmpeg_bin", lambda: "/usr/local/bin/ffmpeg")
    src = tmp_path / "src.wav"
    src.write_bytes(b"\x00" * 64)
    voice = tmp_path / "v.wav"
    voice.write_bytes(b"\x00" * 64)
    dest = tmp_path / "out.wav"

    def fake_run(cmd, **kw):
        dest.write_bytes(b"\x00" * 4096)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(compositor_mod, "_measure_loudness", lambda p: -14.2)

    measured = compositor_mod.apply_sidechain_ducking(
        source_audio_path=str(src), voice_audio_path=str(voice),
        dest_path=dest, target_lufs=-14.0,
    )
    assert measured == -14.2


# ----------------------------------------------------------------------
# ASS caption writer (pure function — tested in isolation)
# ----------------------------------------------------------------------

def test_write_ass_captions_includes_all_words(tmp_path):
    dest = tmp_path / "x.ass"
    compositor_mod._write_ass_captions(
        [
            {"text": "BOOM", "start_s": 0.0, "end_s": 0.4},
            {"text": "what", "start_s": 0.4, "end_s": 0.6},
        ],
        dest,
    )
    text = dest.read_text()
    assert "BOOM" in text
    assert "what" in text
    assert text.count("Dialogue:") == 2


def test_write_ass_captions_escapes_curly_braces(tmp_path):
    """ASS treats {} as override blocks; user-supplied braces would break
    the renderer. Escape to parens."""
    dest = tmp_path / "x.ass"
    compositor_mod._write_ass_captions(
        [{"text": "weird {brace} text", "start_s": 0.0, "end_s": 0.5}],
        dest,
    )
    text = dest.read_text()
    assert "{brace}" not in text
    assert "(brace)" in text


def test_write_ass_captions_skips_empty_text(tmp_path):
    dest = tmp_path / "x.ass"
    compositor_mod._write_ass_captions(
        [
            {"text": "", "start_s": 0.0, "end_s": 0.4},
            {"text": "real word", "start_s": 0.4, "end_s": 0.6},
        ],
        dest,
    )
    text = dest.read_text()
    assert text.count("Dialogue:") == 1
    assert "real word" in text


# ----------------------------------------------------------------------
# _ass_timestamp
# ----------------------------------------------------------------------

def test_ass_timestamp_format():
    assert compositor_mod._ass_timestamp(0.0) == "0:00:00.00"
    assert compositor_mod._ass_timestamp(1.5) == "0:00:01.50"
    assert compositor_mod._ass_timestamp(65.123) == "0:01:05.12"
    assert compositor_mod._ass_timestamp(3661.0) == "1:01:01.00"
    # Negative inputs clamp to 0.
    assert compositor_mod._ass_timestamp(-5.0) == "0:00:00.00"
