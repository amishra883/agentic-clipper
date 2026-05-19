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
from agents.retry import retry_external
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
    """ffmpeg + MoviePy composition. Wrapped in retry_external for
    transient I/O failures.

    Phase 2 wires:
      1. trim source [start..end], force 9:16 with letterbox or smart-crop
      2. duck source audio by ~-12dB under voice; mix at -14 LUFS overall
         (see `apply_sidechain_ducking` for the LUFS contract)
      3. burn word-level captions from `captions` (aligned via WhisperX)
      4. overlay avatar reactions at each punch_beat (pre-roll 100ms)
      5. splice in stingers / concept graphics from `visuals` by start_s

    Returns the final duration in seconds.
    """
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


def align_captions_whisperx(
    raw_words: list[dict],
    audio_path: str,
) -> list[dict]:
    """E-21 contract surface: forced-alignment of Whisper word boundaries
    against the actual VO audio. Cuts the 100-300ms drift Whisper alone
    leaves on each word to <50ms (CAPTION_DRIFT_TOLERANCE_MS).

    Returns the same word-list shape with `start_s`/`end_s` realigned.
    Phase 2 wires the WhisperX call; Phase 1 raises NotImplementedError
    and the caller falls back to the raw words with a logged warning.
    """
    raise NotImplementedError("WhisperX alignment not wired in Phase 1")


def apply_sidechain_ducking(
    *,
    source_audio_path: str,
    voice_audio_path: str,
    dest_path: Path,
    target_lufs: float = TARGET_LUFS,
) -> float:
    """E-22 contract surface: ffmpeg sidechaincompress that ducks the
    source audio under the commentary VO to hit `target_lufs` overall.

    Returns the measured LUFS of the mixed output. Phase 2 wires the
    ffmpeg filter chain; Phase 1 returns `target_lufs` unchanged so
    Compliance can be smoke-tested.
    """
    raise NotImplementedError("LUFS ducking not wired in Phase 1")


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
                try:
                    final_duration = _verify_composition_duration(
                        partial, planned_s=planned_duration,
                    )
                except CompositionFailed as exc:
                    _cleanup_partial(partial)
                    _quarantine_clip(clip_id, f"duration_verification_failed: {exc}")
                    log(agent="compositor", event_type="composition_failed",
                        level="warn", clip_id=clip_id,
                        payload={"error": str(exc)},
                        rationale="post-compose ffprobe gate fired; clip quarantined")
                    return None
                # Atomic promote: the partial file becomes the final file
                # in one inode swap. Readers (Compliance, Publisher) never
                # see a torn write.
                try:
                    _atomic_promote(partial, dest_path)
                except OSError as exc:
                    _cleanup_partial(partial)
                    _quarantine_clip(
                        clip_id, f"atomic_rename_failed: {exc}",
                    )
                    return None

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
