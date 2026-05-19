"""Editor — downloads the source clip and identifies the punch segment.

Reads a curated `clips_candidate` row, downloads via yt-dlp, verifies the
download via ffprobe, runs faster-whisper to get a transcript, runs the
music detector on the chosen punch segment, and persists everything to
`clip_artifacts`. Failures route to `/data/clips/quarantine/` instead of
poisoning downstream paid stages.

Hardening blocks (Days 5-6 of the revised Phase 2 plan):

- **ffprobe partial-download validation (E-16):** every download is
  validated by `ffprobe -show_streams -show_format`. If the file is
  missing a video stream, has zero duration, or is dramatically shorter
  than the candidate's `source_duration_s`, the clip is quarantined
  with a `partial_download` reason — never feeds the Whisper / Writer
  pipeline with garbage bytes.

- **Whisper no-speech quarantine (E-15):** if every transcript segment
  reports `no_speech_prob > NO_SPEECH_THRESHOLD`, the source is
  audio-only / instrumental / unintelligible and the clip is
  quarantined. Distinct from "music in source segment" — a clip can
  contain speech AND music; the music gate is separate.

- **Music detection wire-in (E-6 BLOCKER follow-through):** the punch
  segment is passed to `agents.music_detector.detect_music_in_segment`
  and the boolean result is written to
  `clip_artifacts.has_music_in_source_segment`. The Compliance gate
  reads that column and fails closed if it's NULL — so populating it
  here is the hand-off contract.

- **stage_lease (E-1):** the entire run wraps in
  `stage_lease(clip_id, "editor")`. Concurrent Editor invocations on
  the same clip serialize at the lease layer; the loser raises
  LeaseConflict immediately, doesn't burn proxy bandwidth on a
  redundant download.

- **PoToken/SABR fallback (E-8 — PARTIAL):** the yt-dlp invocation
  selects a `player_client` strategy from
  `EDITOR_YT_DLP_PLAYER_CLIENT` (default `tv_simply`, which currently
  side-steps the PoToken requirement for YouTube). If that path fails
  the retry loop, the doctor surfaces it; Phase 3 wires a proper
  secondary (e.g., yt-dlp-impersonate).

Per CLAUDE.md "Architecture / Agent topology" — Editor step 3 in the data flow.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agents.db import connect
from agents.events import log
from agents.models import TranscriptSegment
from agents.music_detector import detect_music_in_segment, DetectorMethod
from agents.retry import RetryGiveUp, retry_external
from agents.stage_lease import LeaseConflict, stage_lease

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_CLIP_DIR = REPO_ROOT / "data" / "clips" / "raw"
# Per CLAUDE.md: failed clips land in /data/quarantine/, sibling to
# /data/clips/, NOT nested under data/clips/. Compliance + analyst read
# this path for the daily digest's "quarantine report" surface.
QUARANTINE_CLIP_DIR = REPO_ROOT / "data" / "quarantine"

# Placeholder bounds; the real picker uses transcript-energy + chat velocity.
PLACEHOLDER_PUNCH_START_S = 0.0
PLACEHOLDER_PUNCH_END_S = 25.0

# Per-segment threshold above which we treat the segment as non-speech.
# faster-whisper reports no_speech_prob in [0,1]; 0.85 leaves room for
# noisy clips with brief speech bursts without quarantining them.
NO_SPEECH_THRESHOLD = 0.85

# Partial-download tolerance: the downloaded clip's duration must be at
# least PARTIAL_DOWNLOAD_TOLERANCE_RATIO of the candidate's source_duration_s.
# Below that, ffprobe-validation rules the file truncated and quarantines.
PARTIAL_DOWNLOAD_TOLERANCE_RATIO = 0.80


class PartialDownloadError(Exception):
    """ffprobe validation failed: file is truncated or malformed."""


class NoSpeechError(Exception):
    """Every transcript segment exceeded NO_SPEECH_THRESHOLD."""


# ---------- yt-dlp + ffprobe + whisper helpers ----------


def _yt_dlp_player_client() -> str:
    """Choose the player_client strategy for yt-dlp. `tv_simply` currently
    avoids the PoToken requirement; the operator can flip this via env
    if YouTube tightens enforcement."""
    return os.environ.get("EDITOR_YT_DLP_PLAYER_CLIENT", "tv_simply")


@retry_external(max_attempts=3, base_delay_s=2.0)
async def _download_source(source_url: str, dest_path: Path) -> None:
    """Run yt-dlp to fetch the source clip.

    Phase 2 wires: yt-dlp invoked via subprocess with the player_client
    from env, mp4+aac format filter, max-duration cap, proxy from
    `config/proxy_pool.yaml`. Transient errors (network 5xx, proxy
    rotation) raise TransientError so retry_external retries; permanent
    errors (age-gate, geo-block, content removed) raise to caller.
    """
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


def _ffprobe_inspect(local_path: Path) -> dict[str, Any]:
    """Run ffprobe to extract duration + stream info. Returns a dict.

    Raises PartialDownloadError if ffprobe exits non-zero (the file is
    so broken ffprobe can't even parse it) or if the output is missing
    the format block.
    """
    if not local_path.exists() or local_path.stat().st_size == 0:
        raise PartialDownloadError(
            f"download missing or zero-bytes at {local_path}"
        )
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_format", "-show_streams",
                "-of", "json", str(local_path),
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except FileNotFoundError as exc:
        raise PartialDownloadError(
            "ffprobe not on PATH — run `make setup`"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PartialDownloadError(
            f"ffprobe timed out inspecting {local_path}"
        ) from exc
    if result.returncode != 0:
        raise PartialDownloadError(
            f"ffprobe rc={result.returncode}: {result.stderr.strip()[:200]}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PartialDownloadError(
            f"ffprobe stdout not JSON: {result.stdout[:200]!r}"
        ) from exc


def _validate_downloaded_clip(
    local_path: Path,
    *,
    expected_duration_s: float | None,
) -> float:
    """Verify the downloaded file is a usable video. Returns the actual
    duration in seconds.

    Raises PartialDownloadError if:
      - ffprobe can't parse the file
      - there's no video stream
      - duration is missing or zero
      - duration is dramatically shorter than expected_duration_s
        (below PARTIAL_DOWNLOAD_TOLERANCE_RATIO)
    """
    probe = _ffprobe_inspect(local_path)
    streams = probe.get("streams") or []
    has_video = any(s.get("codec_type") == "video" for s in streams)
    if not has_video:
        raise PartialDownloadError(
            f"no video stream in {local_path}: codecs found "
            f"{[s.get('codec_type') for s in streams]}"
        )
    fmt = probe.get("format") or {}
    duration_str = fmt.get("duration")
    if not duration_str:
        raise PartialDownloadError(
            f"ffprobe format.duration missing for {local_path}"
        )
    try:
        actual_duration = float(duration_str)
    except ValueError as exc:
        raise PartialDownloadError(
            f"ffprobe duration not float: {duration_str!r}"
        ) from exc
    if actual_duration <= 0:
        raise PartialDownloadError(
            f"ffprobe duration <= 0 for {local_path}: {actual_duration}"
        )
    if expected_duration_s and expected_duration_s > 0:
        ratio = actual_duration / expected_duration_s
        if ratio < PARTIAL_DOWNLOAD_TOLERANCE_RATIO:
            raise PartialDownloadError(
                f"truncated download: actual={actual_duration:.1f}s "
                f"expected≈{expected_duration_s:.1f}s "
                f"(ratio={ratio:.2f} < {PARTIAL_DOWNLOAD_TOLERANCE_RATIO})"
            )
    return actual_duration


@retry_external(max_attempts=2, base_delay_s=1.0)
async def _transcribe(local_path: Path) -> list[TranscriptSegment]:
    """Run faster-whisper on the local clip. Phase 2 wires the actual
    invocation; transient errors (GPU OOM, model loading races) raise
    TransientError so retry_external retries."""
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


def _is_all_no_speech(transcript: list[TranscriptSegment]) -> bool:
    """True iff every segment with a non-null no_speech_prob exceeds the
    threshold. Empty transcript ALSO returns True — that's the "Whisper
    found nothing intelligible" case from E-15."""
    if not transcript:
        return True
    scored = [s for s in transcript if s.no_speech_prob is not None]
    if not scored:
        # Whisper didn't report no_speech_prob at all — can't enforce E-15.
        # Treat as ambiguous (NOT quarantine).
        return False
    return all(s.no_speech_prob > NO_SPEECH_THRESHOLD for s in scored)


def _pick_punch_segment(
    transcript: list[TranscriptSegment],
) -> tuple[float, float]:
    # TODO(phase2): replace with Claude+heuristic scoring of the 25s window
    # whose laugh density + caption-friendliness is highest. `transcript`
    # is the input for that scoring; until then we return placeholder
    # bounds and the param is unused.
    del transcript
    return PLACEHOLDER_PUNCH_START_S, PLACEHOLDER_PUNCH_END_S


def _music_detector_method() -> DetectorMethod | None:
    """Read the configured music-detector method. Returns None when the
    env is not set; the caller treats that as 'detector not wired yet'
    and emits a structural NULL into clip_artifacts.has_music_in_source_segment
    (Compliance gate fails closed on NULL — desired behavior in scaffold)."""
    method = os.environ.get("EDITOR_MUSIC_DETECTOR_METHOD")
    if method in (None, ""):
        return None
    # Validate the env value against the typed union; unrecognized
    # values fail fast instead of silently falling through to None.
    if method not in ("placeholder-energy", "panns-tagging", "spectral-bandwidth"):
        raise ValueError(
            f"EDITOR_MUSIC_DETECTOR_METHOD={method!r} is not a recognized "
            f"DetectorMethod. Valid: placeholder-energy / panns-tagging / "
            f"spectral-bandwidth"
        )
    return method  # type: ignore[return-value]


def _run_music_detection(
    local_path: Path,
    *,
    start_s: float,
    end_s: float,
    method: DetectorMethod | None,
) -> int | None:
    """Run the music detector on the chosen punch segment. Returns 1 if
    music detected, 0 if not, None if the detector isn't wired (no env)
    or the file doesn't exist (scaffold mode).

    Compliance fails closed on NULL → returning None in scaffold mode
    is the intended safety behavior. Once Phase 2 picks a real
    detector and sets EDITOR_MUSIC_DETECTOR_METHOD, this populates
    with an actual boolean.
    """
    if method is None:
        return None
    if not local_path.exists():
        return None
    # placeholder-energy is harness-only — the env explicitly opts in to
    # the unsafe stub for end-to-end smoke tests; production must pick
    # a real detector method.
    allow_unsafe = method == "placeholder-energy"
    result = detect_music_in_segment(
        audio_path=local_path,
        start_s=start_s,
        end_s=end_s,
        method=method,
        allow_unsafe_placeholder=allow_unsafe,
    )
    return 1 if result.has_music else 0


# ---------- DB helpers ----------


def _load_candidate(clip_id: str) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM clips_candidate WHERE id = ?", (clip_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"no clips_candidate row with id={clip_id}")
    return dict(row)


def _persist_artifact(
    lease,
    *,
    source_local_path: str,
    transcript: list[TranscriptSegment],
    start_s: float,
    end_s: float,
    has_music: int | None,
) -> None:
    """Atomic persist via `lease.commit_artifact()`. The data write AND
    the artifact_version bump happen in one BEGIN IMMEDIATE so a
    concurrent stage cannot race past us between data-commit and
    version-bump. Raises StaleArtifactVersion if another stage already
    moved past our input_artifact_version — and crucially, no partial
    data lands when that happens (Codex 2026-05-18 P1#1 fix).

    Editor re-running on a clip that already has downstream artifacts
    (script_text, voice_audio_path, etc.) MUST invalidate those columns:
    a fresh transcript / punch segment makes the prior script and voice
    stale, and shipping that combination would publish a script written
    for a different edit (Codex 2026-05-18 P1#2 fix).
    """
    clip_id = lease.clip_id
    payload = [
        {
            "start_s": s.start_s,
            "end_s": s.end_s,
            "text": s.text,
            "words": s.words,
            "no_speech_prob": s.no_speech_prob,
        }
        for s in transcript
    ]

    def _do_persist(conn, new_version):
        conn.execute(
            """
            INSERT INTO clip_artifacts
              (clip_id, source_local_path, transcript_json,
               punch_segment_start_s, punch_segment_end_s,
               has_music_in_source_segment, artifact_version, updated_at,
               script_text, shot_list_json,
               voice_audio_path, voice_runtime_s,
               visuals_seconds_used, visuals_tier, visuals_cost_usd,
               final_video_path, final_duration_s)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'),
                    NULL, NULL, NULL, NULL, 0, NULL, 0, NULL, NULL)
            ON CONFLICT(clip_id) DO UPDATE SET
              source_local_path = excluded.source_local_path,
              transcript_json   = excluded.transcript_json,
              punch_segment_start_s = excluded.punch_segment_start_s,
              punch_segment_end_s   = excluded.punch_segment_end_s,
              has_music_in_source_segment = excluded.has_music_in_source_segment,
              artifact_version  = excluded.artifact_version,
              updated_at        = datetime('now'),
              -- Downstream invalidation: any prior Writer/Voice/Visuals/
              -- Compositor output was produced from the previous Editor
              -- run; a re-edit makes it stale, so clear it here.
              script_text       = NULL,
              shot_list_json    = NULL,
              voice_audio_path  = NULL,
              voice_runtime_s   = NULL,
              visuals_seconds_used = 0,
              visuals_tier      = NULL,
              visuals_cost_usd  = 0,
              final_video_path  = NULL,
              final_duration_s  = NULL
            """,
            (
                clip_id,
                source_local_path,
                json.dumps(payload),
                start_s,
                end_s,
                has_music,
                new_version,
            ),
        )
        conn.execute(
            "UPDATE clips_candidate SET status = 'processing' WHERE id = ?",
            (clip_id,),
        )

    lease.commit_artifact(_do_persist)


def _quarantine_clip(
    clip_id: str,
    *,
    local_path: Path | None,
    reason: str,
    detail: str,
) -> None:
    """Move the (possibly partial) download to /data/clips/quarantine/,
    flip the candidate row to 'quarantined' status, and log the event
    with enough detail for the digest to surface the failure.
    """
    QUARANTINE_CLIP_DIR.mkdir(parents=True, exist_ok=True)
    if local_path is not None and local_path.exists():
        try:
            target = QUARANTINE_CLIP_DIR / local_path.name
            shutil.move(str(local_path), str(target))
        except OSError as exc:
            # Don't let a filesystem hiccup prevent the DB state change;
            # the file matters less than the audit row.
            log(
                agent="editor",
                event_type="quarantine_move_failed",
                level="warn",
                clip_id=clip_id,
                payload={"src": str(local_path), "error": str(exc)},
                rationale="filesystem move to quarantine failed; DB state still flipped",
            )
    with connect() as conn:
        conn.execute(
            "UPDATE clips_candidate SET status = 'quarantined' WHERE id = ?",
            (clip_id,),
        )
    log(
        agent="editor",
        event_type="clip_quarantined",
        level="warn",
        clip_id=clip_id,
        payload={"reason": reason, "detail": detail[:200]},
        rationale=f"editor quarantined {clip_id}: {reason}",
    )


# ---------- Public entry point ----------


async def run_editor(clip_id: str) -> None:
    """Download + ffprobe-validate + transcribe + segment + music-detect
    one candidate. Persists to clip_artifacts or quarantines on any
    hardening-gate failure.

    Phase 1 scaffold: the live yt-dlp / Whisper calls raise
    NotImplementedError; the scaffold still validates the structural
    flow (lease + quarantine + DB write) so downstream agents can be
    smoke-tested end-to-end.
    """
    candidate = _load_candidate(clip_id)
    RAW_CLIP_DIR.mkdir(parents=True, exist_ok=True)
    dest_path = RAW_CLIP_DIR / f"{clip_id}.mp4"

    try:
        with stage_lease(clip_id, stage="editor", ttl_seconds=600) as lease:
            transcript: list[TranscriptSegment] = []
            scaffold_mode = False

            # ---------- Download ----------
            try:
                await _download_source(candidate["source_url"], dest_path)
            except NotImplementedError:
                scaffold_mode = True
                log(
                    agent="editor",
                    event_type="phase1_scaffold",
                    clip_id=clip_id,
                    payload={
                        "source_url": candidate["source_url"],
                        "player_client": _yt_dlp_player_client(),
                    },
                    rationale="yt-dlp stubbed in Phase 1; skipping ffprobe + whisper",
                )
            except RetryGiveUp as exc:
                _quarantine_clip(
                    clip_id,
                    local_path=dest_path,
                    reason="download_exhausted",
                    detail=f"yt-dlp retries exhausted: {exc}",
                )
                return

            # ---------- ffprobe partial-download validation (E-16) ----------
            if not scaffold_mode:
                try:
                    _validate_downloaded_clip(
                        dest_path,
                        expected_duration_s=candidate.get("source_duration_s"),
                    )
                except PartialDownloadError as exc:
                    _quarantine_clip(
                        clip_id,
                        local_path=dest_path,
                        reason="partial_download",
                        detail=str(exc),
                    )
                    return

            # ---------- Transcribe ----------
            if not scaffold_mode:
                try:
                    transcript = await _transcribe(dest_path)
                except NotImplementedError:
                    # Mixed-mode (download succeeded but whisper stubbed) —
                    # can happen during partial wiring. Treat like scaffold
                    # for the no-speech gate.
                    scaffold_mode = True
                except RetryGiveUp as exc:
                    _quarantine_clip(
                        clip_id,
                        local_path=dest_path,
                        reason="transcribe_exhausted",
                        detail=f"whisper retries exhausted: {exc}",
                    )
                    return

            # ---------- No-speech quarantine (E-15) ----------
            if not scaffold_mode and _is_all_no_speech(transcript):
                _quarantine_clip(
                    clip_id,
                    local_path=dest_path,
                    reason="no_speech",
                    detail=(
                        f"all {len(transcript)} segments exceeded "
                        f"NO_SPEECH_THRESHOLD={NO_SPEECH_THRESHOLD}"
                    ),
                )
                return

            # ---------- Punch segment selection ----------
            start_s, end_s = _pick_punch_segment(transcript)

            # ---------- Music detection wire-in ----------
            has_music = _run_music_detection(
                dest_path,
                start_s=start_s,
                end_s=end_s,
                method=_music_detector_method(),
            )

            # ---------- Persist (atomic via lease.commit_artifact) ----------
            # commit_artifact does version-check + data write + version
            # bump in one BEGIN IMMEDIATE. It also sets
            # lease.output_artifact_version + _artifact_committed so the
            # exit handler skips the now-redundant legacy CAS.
            _persist_artifact(
                lease,
                source_local_path=str(dest_path),
                transcript=transcript,
                start_s=start_s,
                end_s=end_s,
                has_music=has_music,
            )

            log(
                agent="editor",
                event_type="clip_edited",
                clip_id=clip_id,
                payload={
                    "punch_start_s": start_s,
                    "punch_end_s": end_s,
                    "transcript_segments": len(transcript),
                    "has_music_in_source_segment": has_music,
                    "scaffold_mode": scaffold_mode,
                },
                rationale=f"selected punch segment {start_s:.1f}-{end_s:.1f}s",
            )

    except LeaseConflict:
        # Another Editor is already running this clip — back off cleanly.
        # info-level, not warn: normal under concurrent worker setups.
        log(
            agent="editor",
            event_type="editor_lease_conflict",
            level="info",
            clip_id=clip_id,
            payload={},
            rationale="another Editor holds the editor lease; backing off",
        )


# Re-export for tests that need to assert on the raised type
__all__ = [
    "run_editor",
    "PartialDownloadError",
    "NoSpeechError",
    "NO_SPEECH_THRESHOLD",
    "PARTIAL_DOWNLOAD_TOLERANCE_RATIO",
    "QUARANTINE_CLIP_DIR",
    "RAW_CLIP_DIR",
    "_validate_downloaded_clip",
    "_is_all_no_speech",
    "_run_music_detection",
    "_quarantine_clip",
]
