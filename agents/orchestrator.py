"""End-to-end pipeline orchestrator — Phase 2 wire-up for `make process`.

Reads from clips_candidate (status='curated'), runs each stage in sequence,
gates on Compliance, and writes clips_ready rows on PASS or quarantines on
FAIL. The orchestrator is the seam that turns 10 independent agents into a
runnable pipeline.

Stage sequence
--------------

  curated candidate
        │
   Editor (download + transcribe + segment + music-detect)
        │  populates clip_artifacts.source_local_path / transcript / segment / has_music
        ↓
   Writer (script + shot list)
        │  populates clip_artifacts.script_text / shot_list_json
        ↓
   Voice (TTS synthesis)
        │  populates clip_artifacts.voice_audio_path / voice_runtime_s
        ↓
   Visuals (Seedance avatars + scene graphics)
        │  populates clip_artifacts.visuals_seconds_used / visuals_cost_usd
        ↓
   Compositor (assemble final MP4 + LUFS + captions)
        │  populates clip_artifacts.final_video_path / final_duration_s
        ↓
   Compliance.gate (hard fail-closed legal defense)
        │  PASS → enqueue clips_ready rows per platform
        │  FAIL → set candidate.status='quarantined'
        ↓
   clips_ready (one row per enabled platform from posting_schedule.yaml)

Failure modes
-------------

Each stage may fail for one of three reasons:

  1. Quarantine (the stage caught a structural problem — partial download,
     no speech, music detected, token budget breached, etc.) — already
     persisted to /data/quarantine/ by the stage itself. The orchestrator
     flips candidate.status to 'quarantined' and moves on to the next clip.

  2. Lease conflict (another orchestrator/agent holds the lease) — skip
     and try again on the next run. Not a permanent failure.

  3. Hard exception (NotImplementedError + scaffold-mode fall-through is
     fine; other exceptions are unexpected and surface in the digest).

The orchestrator does NOT run Scout/Curator — those have their own cost
models and cadences. Operator runs `make scout` / `make curator` separately
to top up the queued bucket. `make process N=5` processes 5 clips that
are already in 'curated' state.

Phase 1 scaffold behavior
-------------------------

Several stages' live integrations (yt-dlp, Whisper, ElevenLabs, Atlas
Cloud) raise NotImplementedError in Phase 1. Each stage CATCHES that and
proceeds in scaffold mode — empty transcripts, placeholder audio,
empty shot lists. The orchestrator end-to-end test exercises this path
so the pipeline structure is verified independently of live wiring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from agents import compliance, editor, voice, writer
from agents.config import load
from agents.db import connect
from agents.events import log
from agents.models import CompositedClip, Script
from agents.publisher import build_description
from agents.stage_lease import LeaseConflict
from agents.visuals import run_visuals
from agents.compositor import run_compositor


StageName = Literal[
    "editor", "writer", "voice", "visuals", "compositor", "compliance", "enqueue",
]

ProcessOutcome = Literal[
    "ready",          # passed compliance, clips_ready rows written
    "quarantined",    # stage quarantined the clip
    "compliance_failed",  # composed but failed compliance gate
    "lease_conflict", # another agent holds a lease; try again later
    "skipped",        # candidate not in 'curated' state
    "error",          # unexpected exception
]


@dataclass
class StageResult:
    stage: StageName
    succeeded: bool
    detail: str = ""


@dataclass
class ClipResult:
    clip_id: str
    outcome: ProcessOutcome
    stages: list[StageResult] = field(default_factory=list)
    error: str | None = None
    clips_ready_ids: list[int] = field(default_factory=list)


@dataclass
class RunSummary:
    requested: int
    processed: int
    ready: int
    quarantined: int
    compliance_failed: int
    lease_conflict: int
    skipped: int
    errored: int
    results: list[ClipResult] = field(default_factory=list)


# ----------------------------------------------------------------------
# Candidate selection + status transitions
# ----------------------------------------------------------------------

def _pick_curated(limit: int) -> list[str]:
    """Atomically claim up to `limit` curated candidates by flipping
    status='curated' → 'processing'. Returns the claimed clip_ids.

    Pattern mirrors curator._promote_with_cas: BEGIN IMMEDIATE + conditional
    UPDATE so two orchestrators running concurrently can't claim the same
    candidate."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT id FROM clips_candidate
             WHERE status = 'curated'
             ORDER BY virality_score DESC NULLS LAST, scouted_at ASC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        claimed: list[str] = []
        for row in rows:
            cur = conn.execute(
                """
                UPDATE clips_candidate
                   SET status = 'processing'
                 WHERE id = ? AND status = 'curated'
                """,
                (row["id"],),
            )
            if cur.rowcount == 1:
                claimed.append(row["id"])
    return claimed


def _mark_status(clip_id: str, status: str, *, rationale: str = "") -> None:
    """Update candidate.status. Used at terminal transitions
    ('ready' / 'quarantined' / 'expired')."""
    with connect() as conn:
        conn.execute(
            "UPDATE clips_candidate SET status = ?, rationale = ? WHERE id = ?",
            (status, rationale, clip_id),
        )


def _is_quarantined(clip_id: str) -> bool:
    """A stage quarantines by writing to /data/quarantine/<clip_id>/. The
    quarantine helpers also flip candidate.status — so we just re-read."""
    with connect() as conn:
        row = conn.execute(
            "SELECT status FROM clips_candidate WHERE id = ?", (clip_id,)
        ).fetchone()
    return row is not None and row["status"] == "quarantined"


def _load_script_from_artifact(clip_id: str) -> Script | None:
    """Reconstruct a Script from clip_artifacts after Writer ran. Returns
    None if Writer didn't persist (scaffold mode falls back to empty)."""
    from agents.models import ShotListEntry

    with connect() as conn:
        row = conn.execute(
            "SELECT script_text, shot_list_json, voice_runtime_s "
            "FROM clip_artifacts WHERE clip_id = ?",
            (clip_id,),
        ).fetchone()
    if row is None or not row["script_text"]:
        return None
    shot_list_raw = json.loads(row["shot_list_json"] or "[]")
    shots = [
        ShotListEntry(
            shot_type=s.get("shot_type", "concept_graphic"),
            start_s=float(s.get("start_s", 0.0)),
            duration_s=float(s.get("duration_s", 0.0)),
            prompt=s.get("prompt", ""),
            reaction_id=s.get("reaction_id"),
            punch_word=s.get("punch_word"),
        )
        for s in shot_list_raw
    ]
    return Script(
        text=row["script_text"],
        runtime_s=float(row["voice_runtime_s"] or 0.0),
        substance_tags=[],     # Writer persists tags but we don't need them downstream
        trending_refs=[],
        trending_freshness="hot",
        punch_density=0.0,
        punch_beats=[],
        shot_list=shots,
    )


def _load_creator(clip_id: str) -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT creator FROM clips_candidate WHERE id = ?", (clip_id,)
        ).fetchone()
    return row["creator"] if row else "unknown"


# ----------------------------------------------------------------------
# Publish-queue write
# ----------------------------------------------------------------------

def _next_scheduled_slot(platform_cfg: dict, *, now: datetime | None = None) -> str:
    """Compute the next available slot in `platform_cfg['times']` (HH:MM
    strings in the operator's timezone). Returns ISO 8601 with explicit
    offset, which clips_ready.scheduled_for requires."""
    schedule = load("posting_schedule")
    tz_name = schedule.get("timezone", "America/New_York")
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    times = platform_cfg.get("times") or ["12:00"]
    # Find the first HH:MM today that is still in the future; if none,
    # use the first slot tomorrow.
    today = now.date()
    candidates: list[datetime] = []
    for t in times:
        hh, mm = (int(x) for x in t.split(":"))
        slot = datetime.combine(today, dt_time(hh, mm), tzinfo=tz)
        if slot <= now:
            slot = slot + timedelta(days=1)
        candidates.append(slot)
    pick = min(candidates)
    return pick.isoformat()


def _default_account_for(platform: str) -> str:
    """Pick the primary account for a platform. Tries the accounts table
    first; falls back to a deterministic placeholder so the pipeline
    isn't blocked by missing operator setup in Phase 1."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM accounts "
            " WHERE platform = ? AND role = 'primary' AND active = 1 "
            " ORDER BY id LIMIT 1",
            (platform,),
        ).fetchone()
    if row is not None:
        return row["id"]
    # Phase 1 placeholder. Real accounts get inserted during operator
    # onboarding (runbook step 3a/3b/4 plus a future `make accounts-init`).
    return f"{platform}_primary_1"


def _hashtags_for(platform: str, creator: str) -> list[str]:
    """Minimal default hashtag set per platform.

    Phase 2 future: Optimizer rotates these from a pre-approved bank. For
    Phase 1 the set is deterministic so Compliance can verify the
    description shape consistently."""
    base = [f"#{creator.replace(' ', '').lower()}"]
    if platform == "youtube_shorts":
        return base + ["#Shorts"]
    if platform == "instagram_reels":
        return base + ["#reels", "#commentary"]
    return base + ["#fyp", "#commentary"]


def _enqueue_for_publish(
    clip: CompositedClip,
    *,
    now: datetime | None = None,
) -> list[int]:
    """Write one clips_ready row per platform enabled in
    posting_schedule.yaml. Returns the inserted row ids.

    On UNIQUE constraint violation (uq_ready_clip_platform_active) the
    insert is skipped — the row already exists from a prior orchestrator
    run on the same clip. That's idempotent by design: re-processing a
    clip after a partial failure shouldn't double-publish."""
    schedule = load("posting_schedule")
    platforms_cfg = schedule.get("platforms") or {}
    inserted: list[int] = []
    skipped: list[str] = []
    description = clip.description or build_description(
        creator=clip.source_creator,
        visuals_used=bool(clip.visuals_used),
        affiliate_present=False,
    )
    # Pre-resolve all (platform, account_id) tuples and pre-check existing
    # active rows under one connection — then INSERT in a fresh connection
    # block so a UNIQUE collision from a stale parallel run doesn't break
    # the rest of the inserts. The partial unique index on
    # status IN ('queued','posting','manual_pending') is what makes the
    # SELECT a reliable "is it currently active?" check.
    with connect() as conn:
        for platform, cfg in platforms_cfg.items():
            existing = conn.execute(
                """
                SELECT id FROM clips_ready
                 WHERE clip_id = ? AND target_platform = ?
                   AND status IN ('queued','posting','manual_pending')
                """,
                (clip.clip_id, platform),
            ).fetchone()
            if existing is not None:
                skipped.append(platform)
                continue
            scheduled_for = _next_scheduled_slot(cfg, now=now)
            account_id = _default_account_for(platform)
            hashtags = _hashtags_for(platform, clip.source_creator)
            cur = conn.execute(
                """
                INSERT INTO clips_ready
                  (clip_id, target_platform, account_id, scheduled_for,
                   title, description, hashtags_json, caption_style, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pop-bold-yellow', 'queued')
                """,
                (
                    clip.clip_id, platform, account_id, scheduled_for,
                    f"Commentary on {clip.source_creator}",
                    description,
                    json.dumps(hashtags),
                ),
            )
            inserted.append(int(cur.lastrowid))
    # Log outside the write transaction so the log connection isn't
    # contending with the just-released write lock.
    for platform in skipped:
        log(agent="orchestrator", event_type="enqueue_skipped",
            level="info", clip_id=clip.clip_id,
            payload={"platform": platform, "reason": "already_queued"},
            rationale=f"clips_ready row already exists for {platform}")
    return inserted


# ----------------------------------------------------------------------
# Per-clip orchestration
# ----------------------------------------------------------------------

async def _run_stage(
    name: StageName, coro, results: list[StageResult],
) -> object:
    """Await a stage coroutine; record success/failure. Quarantine-style
    failures raise inside the stage and get caught by the per-clip
    handler — we only record exceptions that bubble out."""
    try:
        out = await coro
        results.append(StageResult(stage=name, succeeded=True))
        return out
    except LeaseConflict:
        results.append(StageResult(stage=name, succeeded=False, detail="lease_conflict"))
        raise
    except Exception as exc:
        results.append(StageResult(stage=name, succeeded=False, detail=repr(exc)))
        raise


async def process_clip(clip_id: str) -> ClipResult:
    """Run the full pipeline on one clip. Returns a ClipResult that captures
    per-stage outcomes plus the terminal verdict (ready / quarantined /
    compliance_failed / lease_conflict / error).

    Caller has already flipped candidate.status to 'processing'; this
    function transitions it to a terminal state ('ready' or 'quarantined')
    unless we hit lease_conflict, in which case the next run retries."""
    stages: list[StageResult] = []
    result = ClipResult(clip_id=clip_id, outcome="error", stages=stages)

    log(agent="orchestrator", event_type="clip_processing_started",
        clip_id=clip_id, rationale="orchestrator picked clip up from 'processing'")

    # ---------- Editor ----------
    try:
        await _run_stage("editor", editor.run_editor(clip_id), stages)
    except LeaseConflict:
        result.outcome = "lease_conflict"
        result.error = "editor lease conflict"
        return result
    except Exception as exc:
        result.error = f"editor:{exc!r}"
        return result
    if _is_quarantined(clip_id):
        result.outcome = "quarantined"
        return result

    # ---------- Writer ----------
    try:
        await _run_stage("writer", writer.run_writer(clip_id), stages)
    except LeaseConflict:
        result.outcome = "lease_conflict"
        result.error = "writer lease conflict"
        return result
    except writer.WriterPolicyError as exc:
        # Writer quarantined for token budget / policy violation.
        result.outcome = "quarantined"
        result.error = f"writer_policy:{exc}"
        return result
    except Exception as exc:
        result.error = f"writer:{exc!r}"
        return result
    if _is_quarantined(clip_id):
        result.outcome = "quarantined"
        return result

    script = _load_script_from_artifact(clip_id)
    if script is None:
        # Writer didn't persist (scaffold mode with empty source). Build a
        # minimal placeholder so downstream stages can run their scaffold
        # paths — the resulting clip will fail compliance (commentary=0)
        # but we want to exercise the pipeline structure end-to-end.
        script = Script(
            text="", runtime_s=0.0, substance_tags=[], trending_refs=[],
            trending_freshness="hot", punch_density=0.0, punch_beats=[],
            shot_list=[],
        )

    # ---------- Voice ----------
    try:
        await _run_stage("voice", voice.run_voice(clip_id, script), stages)
    except LeaseConflict:
        result.outcome = "lease_conflict"
        return result
    except Exception as exc:
        result.error = f"voice:{exc!r}"
        return result
    if _is_quarantined(clip_id):
        result.outcome = "quarantined"
        return result

    # ---------- Visuals ----------
    try:
        await _run_stage(
            "visuals", run_visuals(clip_id, script.shot_list), stages,
        )
    except LeaseConflict:
        result.outcome = "lease_conflict"
        return result
    except Exception as exc:
        result.error = f"visuals:{exc!r}"
        return result
    if _is_quarantined(clip_id):
        result.outcome = "quarantined"
        return result

    # ---------- Compositor ----------
    composited: CompositedClip | None = None
    try:
        composited = await _run_stage(
            "compositor", run_compositor(clip_id), stages,
        )
    except LeaseConflict:
        result.outcome = "lease_conflict"
        return result
    except Exception as exc:
        result.error = f"compositor:{exc!r}"
        return result
    if composited is None or _is_quarantined(clip_id):
        # Compositor returned None (failure path raised CompositionFailed
        # internally and routed to quarantine).
        result.outcome = "quarantined"
        return result

    # Compositor returns description="" — fill it in before Compliance reads it.
    if not composited.description:
        composited.description = build_description(
            creator=composited.source_creator,
            visuals_used=bool(composited.visuals_used),
            affiliate_present=False,
        )

    # ---------- Compliance gate ----------
    verdict = compliance.gate(composited)
    stages.append(StageResult(
        stage="compliance",
        succeeded=verdict.passed,
        detail=verdict.blocked_reason or "all rules passed",
    ))
    if not verdict.passed:
        _mark_status(clip_id, "quarantined",
                     rationale=f"compliance: {verdict.blocked_reason}")
        result.outcome = "compliance_failed"
        result.error = verdict.blocked_reason
        log(agent="orchestrator", event_type="compliance_blocked",
            level="blocked", clip_id=clip_id,
            payload={"blocked_reason": verdict.blocked_reason,
                     "rule_results": verdict.rule_results},
            rationale="clip routed to quarantine — compliance gate failed")
        return result

    # ---------- Enqueue for publish ----------
    try:
        inserted = _enqueue_for_publish(composited)
        stages.append(StageResult(stage="enqueue", succeeded=True,
                                  detail=f"{len(inserted)} clips_ready row(s)"))
    except Exception as exc:
        stages.append(StageResult(stage="enqueue", succeeded=False,
                                  detail=repr(exc)))
        result.error = f"enqueue:{exc!r}"
        return result

    _mark_status(clip_id, "ready",
                 rationale=f"enqueued to {len(inserted)} platform(s)")
    result.outcome = "ready"
    result.clips_ready_ids = inserted
    log(agent="orchestrator", event_type="clip_ready_for_publish",
        clip_id=clip_id,
        payload={"clips_ready_ids": inserted,
                 "platforms": [p for p in (load("posting_schedule").get("platforms") or {})]},
        rationale=f"clip {clip_id} cleared compliance and is queued for publish")
    return result


# ----------------------------------------------------------------------
# Batch entry point
# ----------------------------------------------------------------------

async def run_orchestrator(n: int) -> RunSummary:
    """Pick up to `n` curated clips and run each through the full pipeline.

    Each clip is processed independently — one clip's quarantine does
    not affect the next. The summary aggregates outcomes for the digest /
    operator-visible report."""
    if n <= 0:
        raise ValueError(f"n must be positive, got {n}")

    claimed = _pick_curated(n)
    summary = RunSummary(
        requested=n, processed=0, ready=0, quarantined=0,
        compliance_failed=0, lease_conflict=0, skipped=0, errored=0,
    )

    if not claimed:
        log(agent="orchestrator", event_type="run_complete",
            payload={"requested": n, "processed": 0},
            rationale="no candidates in 'curated' state — run `make curator` first")
        return summary

    for clip_id in claimed:
        try:
            result = await process_clip(clip_id)
        except Exception as exc:  # pragma: no cover — defensive
            result = ClipResult(
                clip_id=clip_id, outcome="error", error=repr(exc),
            )
        summary.results.append(result)
        summary.processed += 1
        if result.outcome == "ready":
            summary.ready += 1
        elif result.outcome == "quarantined":
            summary.quarantined += 1
        elif result.outcome == "compliance_failed":
            summary.compliance_failed += 1
        elif result.outcome == "lease_conflict":
            summary.lease_conflict += 1
        elif result.outcome == "skipped":
            summary.skipped += 1
        else:
            summary.errored += 1

    log(agent="orchestrator", event_type="run_complete",
        payload={"requested": n, "processed": summary.processed,
                 "ready": summary.ready, "quarantined": summary.quarantined,
                 "compliance_failed": summary.compliance_failed,
                 "lease_conflict": summary.lease_conflict,
                 "errored": summary.errored},
        rationale=f"orchestrator processed {summary.processed}/{n} clips")
    return summary


__all__ = [
    "StageName",
    "ProcessOutcome",
    "StageResult",
    "ClipResult",
    "RunSummary",
    "process_clip",
    "run_orchestrator",
]
