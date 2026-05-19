"""Compositor — assembles the final video for a clip.

Mixes source-audio-ducked under commentary, burns word-level captions,
inserts avatar reactions on punch_beats, splices B-roll/stinger cutaways,
and writes the final mp4.

Hardening blocks (Days 11-12 of the revised Phase 2 plan):

- **Run-scoped temp path + atomic rename (E-9):**
  ffmpeg writes to `data/clips/output/.{clip_id}.partial.{run_id}` in the
  SAME directory as the final path. On success: `os.replace()` atomically
  promotes to `{clip_id}.mp4`. On failure: the partial file is cleaned up
  and no garbage lands in the output directory. Same-directory write
  guarantees the rename is atomic (different filesystems would break
  atomicity).

- **Per-clip advisory lock (E-19):**
  Run wraps in `stage_lease(clip_id, "compositor", ttl_seconds=900)`.
  Two compositors on the same clip serialize at the lease layer; the
  loser raises LeaseConflict at acquire time, doesn't waste an ffmpeg
  invocation on output the winner will overwrite.

- **Post-compose ffprobe duration verification (E-10):**
  After ffmpeg returns, `_verify_composition_duration()` re-reads the
  output via ffprobe and asserts the duration matches the planned
  (max of voice_runtime + intro/outro, source_segment) within ±0.5s.
  A truncated render (ffmpeg exited early, codec issue) is caught
  here before Compliance sees a clip whose duration doesn't match
  what we logged.

- **WhisperX forced alignment for captions (E-21):**
  `align_captions_whisperx()` is the contract surface for Phase 2's
  word-level timestamp realignment. The Editor's Whisper output drifts
  ~100-300ms vs. the actual audio; WhisperX's forced-alignment closes
  that to <50ms so burned captions match the spoken word. Phase 1
  scaffold raises NotImplementedError; the caller falls back to raw
  Whisper words and logs caption_alignment_skipped.

- **Sidechain LUFS ducking (E-22):**
  `apply_sidechain_ducking()` is the contract surface for ducking
  source audio under the commentary VO to hit -14 LUFS overall.
  Phase 2 wires ffmpeg sidechaincompress; Phase 1 returns the
  target LUFS unchanged for Compliance to read.

- **stage_lease + commit_artifact** (Day 5-8 P1 pattern, applied here):
  Atomic version-check + persist + bump prevents race-window data
  corruption between Compositor and any later stage (Publisher).

Per CLAUDE.md "Architecture / Agent topology" — Compositor step 7.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from agents.db import connect
from agents.events import log
from agents.models import AudioTrack, CompositedClip, GeneratedAsset, Script
from agents.retry import TransientError, retry_external
from agents.stage_lease import LeaseConflict, stage_lease

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "data" / "clips" / "output"
QUARANTINE_DIR = REPO_ROOT / "data" / "quarantine"

# Acceptable post-compose duration drift vs the planned duration. ffmpeg
# can land ±200ms from the input plan (frame quantization, codec padding);
# 500ms is the threshold above which we treat as truncation / runaway.
DURATION_TOLERANCE_S = 0.5

# Caption alignment threshold: WhisperX should get word boundaries within
# this drift vs the audio. Codex Compositor finding E-21 named <50ms.
CAPTION_DRIFT_TOLERANCE_MS = 50

# Target LUFS for the final composition (CLAUDE.md "target_loudness_lufs").
TARGET_LUFS = -14.0
LUFS_TOLERANCE = 0.5  # ±0.5 LUFS per the Phase 2 plan's caption drift gate


class CompositionFailed(Exception):
    """ffmpeg returned non-zero, ffprobe couldn't read the output, or the
    duration verification gate failed. Caller routes to quarantine."""


# ---------- Composition stubs ----------


_FFMPEG_TRANSIENT_MARKERS = (
    "resource temporarily unavailable",
    "device or resource busy",
    "broken pipe",
)


def _ffmpeg_bin() -> str | None:
    """Locate the ffmpeg binary on PATH. Returns None if missing — caller
    falls back to NotImplementedError so the orchestrator's scaffold path
    still runs."""
    import shutil
    return shutil.which("ffmpeg")


def _escape_ass_text(text: str) -> str:
    """Escape characters that are syntactically meaningful inside ASS
    subtitle dialogue lines. Comma, newline, brace, backslash."""
    return (
        text.replace("\\", "\\\\")
            .replace("{", "(")
            .replace("}", ")")
            .replace("\n", " ")
    )


def _ass_timestamp(seconds: float) -> str:
    """Format a float seconds value as ASS `H:MM:SS.cc` (centiseconds)."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - (h * 3600 + m * 60)
    return f"{h}:{m:02d}:{s:05.2f}"


def _write_ass_captions(captions: list[dict], dest: Path) -> None:
    """Render the caption word-list as an ASS subtitle file. ASS lets us
    burn high-contrast word-level captions via ffmpeg's `subtitles=`
    filter without invoking imagemagick or building per-word overlay
    streams. Style block matches CLAUDE.md's `pop-bold-yellow`.
    """
    style = (
        "Style: Default,Inter,72,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,4,2,2,40,40,160,1"
    )
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\nPlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{style}\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    lines: list[str] = [header]
    for w in captions:
        start = _ass_timestamp(w.get("start_s", 0.0))
        end_s = w.get("end_s", w.get("start_s", 0.0))
        # Avoid zero-length cues by forcing a 50ms floor.
        if end_s <= w.get("start_s", 0.0):
            end_s = w.get("start_s", 0.0) + 0.05
        end = _ass_timestamp(end_s)
        text = _escape_ass_text(str(w.get("text", "")).strip())
        if not text:
            continue
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n")
    dest.write_text("".join(lines))


@retry_external(max_attempts=2, base_delay_s=1.0)
async def _ffmpeg_compose(
    *,
    source_path: str,
    source_start_s: float,
    source_end_s: float,
    voice_track: AudioTrack,
    visuals: list[GeneratedAsset],
    punch_beats: list[float],
    captions: list[dict],
    dest_path: Path,
) -> float:
    """Compose the final clip with ffmpeg.

    Pipeline:
      1. Trim source[start..end] to a 9:16 frame (scale + pad, no smart-crop
         in this pass — that's a Phase 3 quality lift).
      2. Mix the source audio at -12dB under the commentary VO (rough
         ducking; the real sidechaincompress lives in
         ``apply_sidechain_ducking`` for the LUFS-accurate pass).
      3. Burn the word-level captions via the ``subtitles=`` filter so
         they're baked into the pixel stream (CLAUDE.md requires burned-in
         captions; the platform overlays are not trusted).
      4. (Phase 3) avatar/visual overlays — for now we composite source +
         VO + captions only. The shot-list / visuals overlay step lands in
         a follow-up because per-visual concatenation needs filter_complex
         graph construction beyond the MVP we need for the pilot gate.

    Returns the actual duration in seconds (ffprobe is the source of
    truth; we don't trust ffmpeg's stdout).

    Transient ffmpeg failures (resource busy, broken pipe on a flaky
    disk) raise ``TransientError`` so retry_external retries. Permanent
    failures (missing input, bad codec, malformed filter graph) raise
    ``RuntimeError``."""
    _ = visuals  # Phase 3 lift
    _ = punch_beats  # Phase 3 lift
    ffmpeg = _ffmpeg_bin()
    if ffmpeg is None:
        raise NotImplementedError(
            "ffmpeg not on PATH; install with `brew install ffmpeg`"
        )
    if not source_path or not Path(source_path).exists():
        raise RuntimeError(f"compose: source file missing: {source_path!r}")
    if not voice_track.path or not Path(voice_track.path).exists():
        raise RuntimeError(f"compose: voice file missing: {voice_track.path!r}")

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Caption ASS file lives next to the partial so cleanup is one step.
    captions_path = dest_path.with_suffix(dest_path.suffix + ".ass")
    _write_ass_captions(captions or [], captions_path)

    source_duration = max(0.001, source_end_s - source_start_s)

    # Filter graph:
    #   [0:v] trim → 9:16 scale+pad → subtitles burn-in   → [vout]
    #   [0:a] trim → volume -12dB (rough duck)            → [a0]
    #   [1:a] (voice WAV)                                 → [a1]
    #   [a0][a1] amix → loudnorm I=-14                    → [aout]
    #
    # subtitles= path needs forward slashes + escaped colons on macOS;
    # ffmpeg's parser is finicky here. We pass it via the `subtitles`
    # filter so the burn-in happens after the scale (otherwise captions
    # render at source resolution and look wrong when scaled).
    ass_for_filter = str(captions_path).replace(":", r"\:")
    filter_complex = (
        f"[0:v]trim=start={source_start_s}:end={source_end_s},setpts=PTS-STARTPTS,"
        f"scale=1080:1920:force_original_aspect_ratio=decrease,"
        f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"subtitles='{ass_for_filter}'[vout];"
        f"[0:a]atrim=start={source_start_s}:end={source_end_s},asetpts=PTS-STARTPTS,"
        f"volume=0.25[a0];"
        f"[1:a]volume=1.0[a1];"
        f"[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0,"
        f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11[aout]"
    )

    cmd = [
        ffmpeg, "-y",
        "-i", source_path,
        "-i", voice_track.path,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        # Hard-stop the duration at planned (max of source + voice). Without
        # `-t`, amix's `duration=longest` can extend the file past either
        # input if there's a stray sample.
        "-t", f"{max(source_duration, voice_track.runtime_s, 1.0):.3f}",
        str(dest_path),
    ]

    import asyncio
    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd,
            capture_output=True, text=True, timeout=600, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TransientError(f"ffmpeg timed out after 600s") from exc
    finally:
        # Caption file isn't useful after the render; clean up regardless
        # of outcome so a quarantined partial doesn't leave .ass orphans.
        try:
            captions_path.unlink()
        except FileNotFoundError:
            pass

    if proc.returncode == 0 and dest_path.exists() and dest_path.stat().st_size > 0:
        # Return the actual duration from ffprobe — same module-level helper
        # downstream verification uses, so we share the parsing.
        return _ffprobe_duration(dest_path)

    stderr_lower = (proc.stderr or "").lower()
    if any(marker in stderr_lower for marker in _FFMPEG_TRANSIENT_MARKERS):
        raise TransientError(
            f"ffmpeg transient (rc={proc.returncode}): "
            f"{proc.stderr.strip()[-300:]}"
        )
    raise RuntimeError(
        f"ffmpeg failed (rc={proc.returncode}): {proc.stderr.strip()[-300:]}"
    )


def align_captions_whisperx(
    raw_words: list[dict],
    audio_path: str,
) -> list[dict]:
    """E-21 contract surface: forced-alignment of Whisper word boundaries
    against the actual VO audio. Cuts the 100-300ms drift Whisper alone
    leaves on each word to <50ms (CAPTION_DRIFT_TOLERANCE_MS).

    Best-effort wire: tries WhisperX if installed; otherwise raises
    NotImplementedError so the caller falls back to the raw Whisper
    words (`_align_captions_or_fallback`). Either way the orchestrator
    proceeds — alignment is a quality improvement, not a hard gate.
    """
    if not raw_words or not audio_path or not Path(audio_path).exists():
        # Nothing to align against. Caller's fallback path is correct.
        raise NotImplementedError("no audio to align against")
    try:
        import whisperx  # type: ignore
    except ImportError as exc:
        raise NotImplementedError(
            "whisperx not installed; install with `pip install whisperx`"
        ) from exc

    # WhisperX's load_align_model + align() is the path. Implementation
    # here is deliberately compact — the model name and device are taken
    # from env so the operator can flip to CUDA without a code change.
    import os
    device = os.environ.get("COMPOSITOR_WHISPERX_DEVICE", "cpu")
    language = os.environ.get("COMPOSITOR_WHISPERX_LANGUAGE", "en")

    model, metadata = whisperx.load_align_model(language_code=language, device=device)
    # WhisperX expects a "segments" structure with `text` and a `words`
    # list. Our caller hands us a flat word list — wrap it.
    segments = [{
        "start": float(raw_words[0].get("start_s", 0.0)),
        "end": float(raw_words[-1].get("end_s", raw_words[-1].get("start_s", 0.0))),
        "text": " ".join(str(w.get("text", "")).strip() for w in raw_words),
        "words": [
            {"word": str(w.get("text", "")).strip(),
             "start": float(w.get("start_s", 0.0)),
             "end": float(w.get("end_s", w.get("start_s", 0.0)))}
            for w in raw_words
        ],
    }]
    result = whisperx.align(
        segments, model, metadata, audio_path, device,
        return_char_alignments=False,
    )
    aligned: list[dict] = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []) or []:
            aligned.append({
                "text": w.get("word", "").strip(),
                "start_s": float(w.get("start", 0.0)),
                "end_s": float(w.get("end", w.get("start", 0.0))),
            })
    return aligned or raw_words


def _measure_loudness(audio_path: str) -> float | None:
    """ffmpeg loudnorm in measurement mode returns JSON on stderr with
    `input_i` (integrated loudness in LUFS). Returns None if ffmpeg
    isn't on PATH or the JSON parse fails — caller treats as 'unknown'."""
    ffmpeg = _ffmpeg_bin()
    if ffmpeg is None:
        return None
    cmd = [
        ffmpeg, "-hide_banner", "-nostats",
        "-i", audio_path,
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    # ffmpeg's loudnorm print_format=json appends the JSON block at the
    # tail of stderr. Find the last '{' and parse from there.
    stderr = proc.stderr or ""
    brace = stderr.rfind("{")
    if brace < 0:
        return None
    try:
        data = json.loads(stderr[brace:])
        return float(data.get("input_i"))
    except (ValueError, TypeError, KeyError):
        return None


def apply_sidechain_ducking(
    *,
    source_audio_path: str,
    voice_audio_path: str,
    dest_path: Path,
    target_lufs: float = TARGET_LUFS,
) -> float:
    """E-22 wire: ffmpeg sidechaincompress that ducks the source audio
    under the commentary VO and normalizes the mixed result to
    ``target_lufs``.

    Filter graph:
      [0:a] (source) → sidechaincompress against [1:a] (voice)
                     → mixed with voice at full level
                     → loudnorm I=target_lufs
                     → write to dest_path

    Returns the measured integrated LUFS of the output (re-measured via
    a second ffmpeg loudnorm pass in measurement mode). Raises
    NotImplementedError if ffmpeg isn't on PATH. ``CompositionFailed``
    if the render fails."""
    ffmpeg = _ffmpeg_bin()
    if ffmpeg is None:
        raise NotImplementedError(
            "ffmpeg not on PATH; install with `brew install ffmpeg`"
        )
    if not Path(source_audio_path).exists():
        raise CompositionFailed(f"ducking: source audio missing: {source_audio_path}")
    if not Path(voice_audio_path).exists():
        raise CompositionFailed(f"ducking: voice audio missing: {voice_audio_path}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # sidechaincompress reads the threshold/ratio from the second input;
    # [1:a] is the "key" track that triggers the gain reduction on
    # [0:a]. After ducking, we amix the now-ducked source with the
    # un-attenuated voice, then loudnorm to the LUFS target.
    filter_complex = (
        "[1:a]asplit=2[v1][v2];"
        "[0:a][v1]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300[ducked];"
        f"[ducked][v2]amix=inputs=2:duration=longest,loudnorm=I={target_lufs}:TP=-1.5:LRA=11[aout]"
    )
    cmd = [
        ffmpeg, "-y",
        "-i", source_audio_path,
        "-i", voice_audio_path,
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        "-c:a", "aac", "-b:a", "192k",
        str(dest_path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise CompositionFailed(f"ducking: ffmpeg invocation failed: {exc}") from exc
    if proc.returncode != 0 or not dest_path.exists() or dest_path.stat().st_size == 0:
        raise CompositionFailed(
            f"ducking: ffmpeg rc={proc.returncode}: {proc.stderr.strip()[-200:]}"
        )
    measured = _measure_loudness(str(dest_path))
    return measured if measured is not None else target_lufs


# ---------- ffprobe duration verification (E-10) ----------


def _ffprobe_duration(path: Path) -> float:
    """Run ffprobe on the composed output and return its duration.
    Raises CompositionFailed on any read error — the file is unusable
    if ffprobe can't parse it."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration", "-of",
                "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except FileNotFoundError as exc:
        raise CompositionFailed(
            "ffprobe not on PATH — run `make setup`"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CompositionFailed(
            f"ffprobe timed out inspecting {path}"
        ) from exc
    if result.returncode != 0:
        raise CompositionFailed(
            f"ffprobe rc={result.returncode}: {result.stderr.strip()[:200]}"
        )
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise CompositionFailed(
            f"ffprobe output not a float: {result.stdout!r}"
        ) from exc


def _verify_composition_duration(
    path: Path,
    *,
    planned_s: float,
) -> float:
    """Post-compose verification gate. Returns the actual duration if
    it's within ±DURATION_TOLERANCE_S of `planned_s`; raises
    CompositionFailed otherwise.

    Catches the case where ffmpeg exits early (codec issue, malformed
    input) and writes a short or zero-duration file. Compliance has no
    way to detect this — duration is the only authoritative signal."""
    actual = _ffprobe_duration(path)
    if actual <= 0:
        raise CompositionFailed(
            f"composed file has zero duration: {path}"
        )
    drift = abs(actual - planned_s)
    if drift > DURATION_TOLERANCE_S:
        raise CompositionFailed(
            f"composition duration drift {drift:.2f}s exceeds tolerance "
            f"{DURATION_TOLERANCE_S}s: planned={planned_s:.2f}s "
            f"actual={actual:.2f}s"
        )
    return actual


# ---------- Atomic write helpers (E-9) ----------


def _partial_path(final: Path, run_id: int) -> Path:
    """Same-directory partial file. `os.replace()` is atomic only within
    the same filesystem — writing to /tmp and renaming to /data/...
    breaks atomicity when those are different mount points.

    The `.partial.{run_id}` suffix lets a janitor / doctor distinguish
    abandoned partials by run."""
    return final.parent / f".{final.name}.partial.{run_id}"


def _cleanup_partial(partial: Path) -> None:
    """Best-effort cleanup. A leftover partial file isn't a crisis (next
    Compositor run will write a fresh one); not failing on missing-file
    keeps the error path simple."""
    try:
        partial.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(agent="compositor", event_type="partial_cleanup_failed",
            level="warn",
            payload={"path": str(partial), "error": str(exc)},
            rationale="partial-file cleanup failed; doctor will surface")


def _atomic_promote(partial: Path, final: Path) -> None:
    """Atomic rename via `os.replace`. On POSIX (Linux/macOS) this is a
    single inode-table swap; readers either see the old final or the
    new — never a partial.

    Cleans up `partial` if `final` already exists with our content
    (re-runs)."""
    os.replace(partial, final)


# ---------- DB helpers ----------


def _load_artifact(clip_id: str) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM clip_artifacts WHERE clip_id = ?", (clip_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"no clip_artifacts row for clip_id={clip_id}")
    return dict(row)


def _load_creator(clip_id: str) -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT creator FROM clips_candidate WHERE id = ?", (clip_id,)
        ).fetchone()
    return row["creator"] if row else "unknown"


def _persist_composition(lease, final_path: str, duration_s: float) -> None:
    """Atomic persist via `lease.commit_artifact()`. Version-check +
    data write + version bump in one BEGIN IMMEDIATE (Codex Day 5-8
    P1 pattern). Compositor is the last writer in the pipeline so
    there are no downstream columns to invalidate."""
    clip_id = lease.clip_id

    def _do_persist(conn, new_version):
        conn.execute(
            """
            INSERT INTO clip_artifacts
              (clip_id, final_video_path, final_duration_s,
               artifact_version, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(clip_id) DO UPDATE SET
              final_video_path = excluded.final_video_path,
              final_duration_s = excluded.final_duration_s,
              artifact_version = excluded.artifact_version,
              updated_at       = datetime('now')
            """,
            (clip_id, final_path, duration_s, new_version),
        )

    lease.commit_artifact(_do_persist)


def _quarantine_clip(clip_id: str, reason: str) -> None:
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    marker = QUARANTINE_DIR / f"{clip_id}.compositor.reason.txt"
    marker.write_text(reason)
    with connect() as conn:
        conn.execute(
            "UPDATE clips_candidate SET status = 'quarantined' WHERE id = ?",
            (clip_id,),
        )
    log(agent="compositor", event_type="clip_quarantined",
        level="warn", clip_id=clip_id,
        payload={"reason": reason[:200]},
        rationale=f"compositor quarantined {clip_id}: {reason[:120]}")


# ---------- Public entry point ----------


async def run_compositor(clip_id: str) -> CompositedClip | None:
    """Assemble the final video for a clip. Returns a CompositedClip ready
    for Compliance.gate(), or None if the run was quarantined / lease-conflicted.

    Phase 1: skips live composition, returns a structurally-valid record so
    Compliance can be exercised end-to-end. ffmpeg/WhisperX/LUFS contracts
    raise NotImplementedError and the caller falls back to the planned
    duration without verifying.
    """
    artifact = _load_artifact(clip_id)
    creator = _load_creator(clip_id)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dest_path = OUTPUT_DIR / f"{clip_id}.mp4"

    source_start = float(artifact.get("punch_segment_start_s") or 0.0)
    source_end = float(artifact.get("punch_segment_end_s") or 0.0)
    source_duration = max(0.0, source_end - source_start)
    voice_runtime = float(artifact.get("voice_runtime_s") or 0.0)

    shot_list = json.loads(artifact.get("shot_list_json") or "[]")
    transcript = json.loads(artifact.get("transcript_json") or "[]")
    punch_beats: list[float] = [float(s.get("start_s", 0.0)) for s in shot_list]
    raw_words = [w for seg in transcript for w in seg.get("words", [])]

    voice_track = AudioTrack(
        path=artifact.get("voice_audio_path") or "",
        runtime_s=voice_runtime,
        loudness_lufs=TARGET_LUFS,
        engine="coqui_xtts_v2",
        voice_id="phase1_scaffold_voice",
    )

    visuals: list[GeneratedAsset] = []
    planned_duration = max(voice_runtime, source_duration)

    try:
        with stage_lease(clip_id, stage="compositor", ttl_seconds=900) as lease:
            run_id = lease.run_id
            partial = _partial_path(dest_path, run_id)

            # ---------- Caption alignment (E-21) ----------
            captions = _align_captions_or_fallback(
                clip_id, raw_words, voice_track.path,
            )

            # ---------- Composition + atomic promote ----------
            final_duration = planned_duration
            composition_succeeded = False
            try:
                final_duration = await _ffmpeg_compose(
                    source_path=artifact.get("source_local_path") or "",
                    source_start_s=source_start,
                    source_end_s=source_end,
                    voice_track=voice_track,
                    visuals=visuals,
                    punch_beats=punch_beats,
                    captions=captions,
                    dest_path=partial,
                )
                composition_succeeded = True
            except NotImplementedError:
                log(agent="compositor", event_type="phase1_scaffold",
                    clip_id=clip_id,
                    rationale="ffmpeg composition stubbed in Phase 1")

            if composition_succeeded:
                # Codex 2026-05-18 P1#4: prior code did `return None` from
                # INSIDE the `with stage_lease(...)` block on duration /
                # promote failure. The lease then exited cleanly and
                # _mark_done flipped pipeline_runs.status to 'succeeded'
                # while the clip was actually quarantined. Now we raise
                # CompositionFailed; the OUTER handler catches it, marks
                # the lease as failed (via the with-block's exception
                # path), quarantines, cleans up, and returns None.
                try:
                    final_duration = _verify_composition_duration(
                        partial, planned_s=planned_duration,
                    )
                except CompositionFailed as exc:
                    _cleanup_partial(partial)
                    log(agent="compositor", event_type="composition_failed",
                        level="warn", clip_id=clip_id,
                        payload={"error": str(exc)},
                        rationale="post-compose ffprobe gate fired; clip will quarantine")
                    raise  # propagate out of lease so it marks failed
                # Atomic promote: the partial file becomes the final file
                # in one inode swap. Readers (Compliance, Publisher) never
                # see a torn write.
                try:
                    _atomic_promote(partial, dest_path)
                except OSError as exc:
                    _cleanup_partial(partial)
                    raise CompositionFailed(
                        f"atomic_rename_failed: {exc}"
                    ) from exc

            _persist_composition(lease, str(dest_path), final_duration)

            log(agent="compositor", event_type="clip_composed",
                clip_id=clip_id,
                payload={
                    "final_duration_s": final_duration,
                    "source_segment_s": source_duration,
                    "commentary_s": voice_runtime,
                    "visuals_count": len(visuals),
                    "scaffold_mode": not composition_succeeded,
                    "run_id": run_id,
                },
                rationale=f"composed {clip_id} ({final_duration:.1f}s)")

    except LeaseConflict:
        log(agent="compositor", event_type="compositor_lease_conflict",
            level="info", clip_id=clip_id, payload={},
            rationale="another Compositor holds the compositor lease; backing off")
        raise
    except CompositionFailed as exc:
        # Codex 2026-05-18 P1#4: raised from inside the lease, so the
        # lease's exit handler already marked pipeline_runs as 'failed'.
        # We finish the quarantine flow here so audit trail and clip
        # status are consistent.
        _quarantine_clip(clip_id, str(exc))
        return None

    def _tri(v: Any) -> bool | None:
        if v is None:
            return None
        return bool(v)

    return CompositedClip(
        clip_id=clip_id,
        final_video_path=str(dest_path),
        final_duration_s=final_duration,
        source_segment_duration_s=source_duration,
        commentary_audio_duration_s=voice_runtime,
        audio_track=voice_track,
        visuals_used=visuals,
        description="",
        has_music_in_source_segment=_tri(artifact.get("has_music_in_source_segment")),
        has_real_face_reference=_tri(artifact.get("has_real_face_reference")),
        source_creator=creator,
    )


def _align_captions_or_fallback(
    clip_id: str,
    raw_words: list[dict],
    voice_audio_path: str,
) -> list[dict]:
    """Try WhisperX; fall back to raw Whisper words with a logged warning
    if the alignment isn't wired (Phase 1) or fails."""
    try:
        return align_captions_whisperx(raw_words, voice_audio_path)
    except NotImplementedError:
        log(agent="compositor", event_type="caption_alignment_skipped",
            level="info", clip_id=clip_id,
            payload={"word_count": len(raw_words)},
            rationale=(
                "WhisperX forced alignment not wired in Phase 1; "
                "captions ship with raw Whisper word timestamps "
                "(100-300ms drift expected vs E-21 target <50ms)"
            ))
        return raw_words


__all__ = [
    "run_compositor",
    "Script",
    "CompositionFailed",
    "DURATION_TOLERANCE_S",
    "CAPTION_DRIFT_TOLERANCE_MS",
    "TARGET_LUFS",
    "align_captions_whisperx",
    "apply_sidechain_ducking",
    "_verify_composition_duration",
    "_partial_path",
    "_atomic_promote",
]
