"""Compositor — assembles the final video for a clip.

Mixes source-audio-ducked under commentary, burns word-level captions,
inserts avatar reactions on punch_beats, splices B-roll/stinger cutaways,
and writes the final mp4. ffmpeg + MoviePy integration is deferred to
Phase 2; Phase 1 returns a structurally-valid CompositedClip and writes a
placeholder path so Compliance can be smoke-tested.

Per CLAUDE.md "Architecture / Agent topology" — Compositor step 7.
"""

from __future__ import annotations

import json
from pathlib import Path

from agents.db import connect
from agents.events import log
from agents.models import AudioTrack, CompositedClip, GeneratedAsset, Script

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "data" / "clips" / "output"


# ---------- Composition stubs ----------

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
    # TODO(phase2): wire ffmpeg + MoviePy composition.
    # Steps the wiring must handle:
    #  1. trim source [start..end], force 9:16 with letterbox or smart-crop
    #  2. duck source audio by ~-12dB under voice; mix at -14 LUFS overall
    #  3. burn word-level captions from `captions` (Whisper output)
    #  4. overlay avatar reactions at each punch_beat (pre-roll 100ms)
    #  5. splice in stingers / concept graphics from `visuals` by start_s
    # Returns the final duration in seconds.
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


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


def _persist_composition(clip_id: str, final_path: str, duration_s: float) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO clip_artifacts (clip_id, final_video_path, final_duration_s, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(clip_id) DO UPDATE SET
              final_video_path = excluded.final_video_path,
              final_duration_s = excluded.final_duration_s,
              updated_at       = datetime('now')
            """,
            (clip_id, final_path, duration_s),
        )


# ---------- Public entry point ----------

async def run_compositor(clip_id: str) -> CompositedClip:
    """Assemble the final video for a clip. Returns a CompositedClip ready
    for Compliance.gate(). Phase 1: skips live composition, returns a
    structurally-valid record so Compliance can be exercised end-to-end.
    """
    artifact = _load_artifact(clip_id)
    creator = _load_creator(clip_id)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dest_path = OUTPUT_DIR / f"{clip_id}.mp4"

    source_start = float(artifact.get("punch_segment_start_s") or 0.0)
    source_end = float(artifact.get("punch_segment_end_s") or 0.0)
    source_duration = max(0.0, source_end - source_start)
    voice_runtime = float(artifact.get("voice_runtime_s") or 0.0)

    # Reconstruct lightweight Script-derived inputs from persisted JSON.
    shot_list = json.loads(artifact.get("shot_list_json") or "[]")
    transcript = json.loads(artifact.get("transcript_json") or "[]")
    punch_beats: list[float] = [float(s.get("start_s", 0.0)) for s in shot_list]
    captions = [w for seg in transcript for w in seg.get("words", [])]

    voice_track = AudioTrack(
        path=artifact.get("voice_audio_path") or "",
        runtime_s=voice_runtime,
        loudness_lufs=-14.0,
        engine="coqui_xtts_v2",
        # Phase 1 scaffold uses the placeholder voice_id. Phase 2's Voice
        # agent must set this from the persona-resolved choice (a member of
        # persona.yaml voice.approved_voice_ids); a missing or unapproved
        # value fails the no_voice_clone compliance rule.
        voice_id="phase1_scaffold_voice",
    )

    visuals: list[GeneratedAsset] = []  # Phase 1 has no generated assets yet
    final_duration = max(voice_runtime, source_duration)

    try:
        final_duration = await _ffmpeg_compose(
            source_path=artifact.get("source_local_path") or "",
            source_start_s=source_start,
            source_end_s=source_end,
            voice_track=voice_track,
            visuals=visuals,
            punch_beats=punch_beats,
            captions=captions,
            dest_path=dest_path,
        )
    except NotImplementedError:
        log(
            agent="compositor",
            event_type="phase1_scaffold",
            clip_id=clip_id,
            rationale="ffmpeg composition stubbed in Phase 1; placeholder duration recorded",
        )

    _persist_composition(clip_id, str(dest_path), final_duration)

    # Pull tri-state compliance evidence from clip_artifacts. The columns are
    # populated by the Editor (music detection) and Visuals (real-face check)
    # stages; if either is unwired the column stays NULL and Compliance fails
    # closed downstream. Compositor must NEVER assert these values itself.
    def _tri(v: object) -> bool | None:
        if v is None:
            return None
        return bool(v)

    composited = CompositedClip(
        clip_id=clip_id,
        final_video_path=str(dest_path),
        final_duration_s=final_duration,
        source_segment_duration_s=source_duration,
        commentary_audio_duration_s=voice_runtime,
        audio_track=voice_track,
        visuals_used=visuals,
        description="",  # Publisher fills the description from its template
        has_music_in_source_segment=_tri(artifact.get("has_music_in_source_segment")),
        has_real_face_reference=_tri(artifact.get("has_real_face_reference")),
        source_creator=creator,
    )

    log(
        agent="compositor",
        event_type="clip_composed",
        clip_id=clip_id,
        payload={
            "final_duration_s": final_duration,
            "source_segment_s": source_duration,
            "commentary_s": voice_runtime,
            "visuals_count": len(visuals),
        },
        rationale=f"composed {clip_id} ({final_duration:.1f}s)",
    )
    return composited


# Re-export Script for callers that want both types from one import.
__all__ = ["run_compositor", "Script"]
