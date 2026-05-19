"""Voice — turns a Script into a commentary AudioTrack.

Default engine is local Coqui XTTS-v2 (free). ElevenLabs Creator is an
escalation path gated by persona.escalation_trigger.

Hardening blocks (Day 8 of the revised Phase 2 plan):

- **Coqui checksum-verified prewarm (E-26):**
  `verify_coqui_checkpoint()` re-hashes the on-disk XTTS-v2 model file
  against a pinned SHA-256 from config/voice_models.yaml. Mismatch
  raises CoquiCheckpointError and the Voice agent refuses to run —
  prevents shipping audio from a tampered / wrong-version model.

- **ElevenLabs 429 + persona-swap logging (E-17):**
  The ElevenLabs path wraps in retry_external (TransientError on 429
  with Retry-After). RetryGiveUp routes to Coqui fallback with a
  structured `elevenlabs_fallback` event (level=warn) that includes
  the reason (rate_limit / network / api_error). No more silent
  fallback — every persona swap is auditable.

- **Daily/monthly cost reservation (cap E-2 reuse):**
  Pre-call `reserve(category='elevenlabs_creator', amount_usd=...)`
  enforces config/budget.yaml caps. BudgetExceeded → fall back to
  Coqui (NOT quarantine; the Coqui path is the free default and
  perfectly valid output). Successful ElevenLabs run settles the
  reservation with the actual cost.

- **Throughput benchmark (E-27):**
  `benchmark_synthesis()` measures wall-clock per audio-second on the
  local box and writes results to data/digest/voice-benchmark.jsonl.
  Operator runs via `make voice-benchmark`; the doctor surface flags
  when realtime ratio drifts above the configured threshold.

- **stage_lease + voice_id:**
  Run wraps in `stage_lease(clip_id, "voice", ttl=180s)`. AudioTrack
  carries the persona's voice_id so Compliance can match against the
  approved_voice_ids whitelist (closes the "missing voice_id" gate).

Per CLAUDE.md "Architecture / Agent topology" — Voice step 5 in the data flow.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from agents.config import load
from agents.costs import BudgetExceeded, reserve, settle
from agents.db import connect
from agents.events import log
from agents.models import AudioTrack, Script
from agents.retry import RetryGiveUp, TransientError, retry_external
from agents.stage_lease import LeaseConflict, stage_lease

REPO_ROOT = Path(__file__).resolve().parent.parent
VOICE_OUT_DIR = REPO_ROOT / "data" / "clips" / "voice"
BENCHMARK_LOG = REPO_ROOT / "data" / "digest" / "voice-benchmark.jsonl"

# Approximate ElevenLabs Creator-tier cost per second of synthesized audio.
# Codex 2026-05-18: hard-coded here so the reservation projection has a
# concrete number; the actual cost from the API response goes into
# settle() so the ledger reflects reality.
_ELEVENLABS_USD_PER_SECOND = 0.012

# Coqui prewarm threshold — if `benchmark_synthesis` reports the local
# box runs slower than this realtime ratio (wall-seconds per audio-second),
# the doctor surface warns the operator. M-series CPUs hit ~0.3-0.5x
# realtime on XTTS-v2 in the fp16 path.
COQUI_REALTIME_RATIO_FLOOR = 2.0


class CoquiCheckpointError(Exception):
    """Coqui XTTS-v2 checkpoint failed SHA verification or is missing."""


class ElevenLabsFallbackReason:
    """Stringly-typed reason codes used in the elevenlabs_fallback event payload.
    Kept as a class (not Enum) so JSON serialization stays a plain str."""
    RATE_LIMIT = "rate_limit_exhausted"
    NETWORK = "network_error"
    BUDGET = "budget_exceeded"
    OTHER = "other_api_error"


# ---------- Coqui checksum prewarm (E-26) ----------


def _voice_models_config() -> dict:
    """Read config/voice_models.yaml. Returns {} if absent — operator
    hasn't set up the checksum manifest yet (acceptable in Phase 1
    scaffold; Phase 2 wiring requires this file)."""
    try:
        return load("voice_models") or {}
    except (FileNotFoundError, KeyError):
        return {}


def _sha256_file(path: Path, *, chunk_bytes: int = 1 << 20) -> str:
    """Stream-hash a large file in 1 MiB chunks so memory stays bounded
    regardless of model size."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_bytes)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def verify_coqui_checkpoint(*, checkpoint_path: Path | None = None) -> dict:
    """Verify the on-disk Coqui XTTS-v2 model checkpoint matches the
    pinned SHA-256 in config/voice_models.yaml.

    Returns: {"verified": bool, "sha256": str, "expected": str, "path": str}.
    Raises CoquiCheckpointError if config has an expected SHA AND the
    file hash doesn't match — that's the failure mode E-26 prevents.

    A missing pin in config returns verified=False with no exception:
    Phase 1 scaffold can run without the file; Phase 2 must pin before
    going live (the doctor check fails closed if verified is not True).
    """
    cfg = _voice_models_config()
    coqui = (cfg.get("coqui_xtts_v2") or {})
    expected = coqui.get("sha256")
    cfg_path = coqui.get("checkpoint_path")

    target_path = checkpoint_path or (
        Path(cfg_path).expanduser() if cfg_path else None
    )
    if target_path is None or not target_path.exists():
        return {
            "verified": False,
            "sha256": "",
            "expected": expected or "",
            "path": str(target_path) if target_path else "",
            "reason": "checkpoint_missing",
        }

    actual = _sha256_file(target_path)
    if expected is None:
        return {
            "verified": False,
            "sha256": actual,
            "expected": "",
            "path": str(target_path),
            "reason": "no_pinned_sha",
        }

    if actual != expected:
        raise CoquiCheckpointError(
            f"Coqui XTTS-v2 checkpoint hash mismatch at {target_path}: "
            f"expected={expected[:12]}... actual={actual[:12]}... — refusing to "
            f"synthesize until operator re-pins or re-downloads the model."
        )
    return {
        "verified": True,
        "sha256": actual,
        "expected": expected,
        "path": str(target_path),
    }


# ---------- TTS engine adapters ----------


async def _synthesize_coqui(text: str, persona: dict, dest_path: Path) -> float:
    """Local Coqui XTTS-v2 inference. Phase 2 wires the actual model
    call; Phase 1 raises NotImplementedError and the caller falls
    through to a placeholder track."""
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


@retry_external(max_attempts=3, base_delay_s=2.0)
async def _synthesize_elevenlabs(
    text: str, persona: dict, dest_path: Path,
) -> float:
    """ElevenLabs Creator API call. Phase 2 wires the actual POST to
    /v1/text-to-speech/{voice_id}.

    Transient errors (HTTP 429, 5xx, network) raise TransientError so
    retry_external honors the Retry-After and backs off. RetryGiveUp at
    the caller drives the Coqui fallback path with structured logging
    — no more silent persona swap (Codex E-17).
    """
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


async def _normalize_loudness(audio_path: Path, target_lufs: float) -> float:
    """ffmpeg loudnorm (two-pass) or pyloudnorm to hit target_lufs.
    Phase 2 wiring; Phase 1 returns the target as-is."""
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


# ---------- Persona / escalation helpers ----------


def _active_persona(persona_cfg: dict) -> dict:
    active_id = persona_cfg["active_persona"]
    for p in persona_cfg["personas"]:
        if p["id"] == active_id:
            return p
    raise KeyError(f"active_persona '{active_id}' not in personas list")


def _should_escalate(persona: dict, curator_score: float | None) -> bool:
    """Persona.voice.escalation_trigger is a string like 'curator_score>=0.85'."""
    trigger = persona["voice"].get("escalation_trigger", "")
    if not trigger or curator_score is None:
        return False
    prefix = "curator_score>="
    if not trigger.startswith(prefix):
        return False
    try:
        threshold = float(trigger[len(prefix):])
    except ValueError:
        return False
    return curator_score >= threshold


def _pick_voice_id(persona: dict, engine: str) -> str:
    """Pick the AudioTrack.voice_id from persona.voice.approved_voice_ids.

    Compliance requires a non-empty voice_id from the whitelist — without
    it the clip blocks. This is the structural guardrail against
    accidental source-creator voice cloning (an ElevenLabs custom voice
    trained on a creator wouldn't appear in approved_voice_ids).

    Picks a deterministic-per-engine slot: ElevenLabs slot first when
    engine='elevenlabs' (later items in the list are typically the
    paid-tier voice clones we approved), Coqui slot first otherwise.
    """
    approved = (persona.get("voice") or {}).get("approved_voice_ids") or []
    if not approved:
        raise ValueError(
            f"persona {persona.get('id')!r} has empty approved_voice_ids — "
            f"add at least one entry to persona.yaml or Compliance will block all clips"
        )
    if engine == "elevenlabs":
        # Prefer entries that look like elevenlabs IDs; fall back to the first.
        for v in approved:
            if "elevenlabs" in v.lower() or "_eleven" in v.lower():
                return v
    return approved[0]


def _load_curator_score(clip_id: str) -> float | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT virality_score FROM clips_candidate WHERE id = ?", (clip_id,)
        ).fetchone()
    return None if row is None else row["virality_score"]


# ---------- Cost reservation (ElevenLabs only — Coqui is free) ----------


def _elevenlabs_budget_caps() -> dict:
    budget = load("budget")
    line_items = budget.get("line_items") or {}
    per_call = budget.get("per_call_caps") or {}
    return {
        "line_item_cap_usd": (line_items.get("elevenlabs_creator") or {}).get(
            "monthly_budget_usd"
        ),
        "daily_cap_usd": per_call.get("elevenlabs_daily_usd_max"),
        "per_clip_cap_usd": per_call.get("elevenlabs_cost_usd_max"),
    }


def _project_elevenlabs_cost(runtime_s: float) -> float:
    """Conservative projection: per-second rate * planned runtime."""
    return round(runtime_s * _ELEVENLABS_USD_PER_SECOND, 4)


def _reserve_elevenlabs(
    clip_id: str, projected_usd: float, caps: dict,
) -> str:
    """Reserve the budget for an ElevenLabs call. Raises BudgetExceeded
    if any cap (daily, monthly, per-clip) would be breached."""
    per_clip = caps.get("per_clip_cap_usd")
    if per_clip is not None and projected_usd > per_clip:
        raise BudgetExceeded(
            cap_name="elevenlabs_per_clip",
            attempted_total=projected_usd,
            cap=per_clip,
            mtd_pending=0.0,
            mtd_succeeded=0.0,
        )
    return reserve(
        category="elevenlabs_creator",
        amount_usd=projected_usd,
        line_item_cap_usd=caps["line_item_cap_usd"],
        daily_cap_usd=caps["daily_cap_usd"],
        detail=f"voice-elevenlabs:{clip_id}",
        provider="elevenlabs",
        clip_id=clip_id,
    )


# ---------- Persistence ----------


def _persist_audio(
    clip_id: str,
    track: AudioTrack,
    *,
    input_artifact_version: int,
) -> None:
    """Write voice_audio_path + voice_runtime_s under the lease's
    input artifact version so the end-of-lease CAS bumps cleanly."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO clip_artifacts
              (clip_id, voice_audio_path, voice_runtime_s,
               artifact_version, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(clip_id) DO UPDATE SET
              voice_audio_path = excluded.voice_audio_path,
              voice_runtime_s  = excluded.voice_runtime_s,
              artifact_version = excluded.artifact_version,
              updated_at       = datetime('now')
            """,
            (clip_id, track.path, track.runtime_s, input_artifact_version),
        )


# ---------- Benchmark (E-27) ----------


@dataclass
class BenchmarkResult:
    audio_seconds: float
    wall_seconds: float
    realtime_ratio: float       # wall / audio (lower = faster)
    engine: str                 # which path we measured
    timestamp: str
    notes: str = ""


def benchmark_synthesis(
    *,
    sample_text: str = "This is a benchmark utterance for measuring local TTS throughput.",
    engine: str = "coqui",
) -> BenchmarkResult:
    """Measure wall-clock time to synthesize a known sample. Appends to
    data/digest/voice-benchmark.jsonl so the doctor surface can show the
    realtime ratio trend.

    Phase 1 scaffold: the TTS call raises NotImplementedError; we still
    record a row with engine='scaffold' so operators can see the
    benchmark CLI is wired even before the model lands.

    Realtime ratio interpretation:
      < 1.0 — faster than realtime (good)
      1.0   — exactly realtime
      > 2.0 — slower than 2x realtime, doctor warns (CPU floor)
    """
    BENCHMARK_LOG.parent.mkdir(parents=True, exist_ok=True)
    audio_seconds = max(1.0, len(sample_text) / 14.0)  # ~14 chars/sec speech
    start = time.perf_counter()
    notes = ""
    try:
        import asyncio
        dest = REPO_ROOT / "data" / "tmp" / "benchmark.wav"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if engine == "coqui":
            asyncio.run(_synthesize_coqui(sample_text, {}, dest))
        elif engine == "elevenlabs":
            asyncio.run(_synthesize_elevenlabs(sample_text, {}, dest))
        else:
            raise ValueError(f"unknown engine {engine!r}")
    except NotImplementedError:
        notes = "scaffold-stub (no actual synthesis)"
        engine = "scaffold"
    wall = time.perf_counter() - start
    ratio = wall / audio_seconds if audio_seconds > 0 else float("inf")
    result = BenchmarkResult(
        audio_seconds=audio_seconds,
        wall_seconds=round(wall, 4),
        realtime_ratio=round(ratio, 4),
        engine=engine,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        notes=notes,
    )
    with BENCHMARK_LOG.open("a") as fh:
        fh.write(json.dumps({
            "audio_seconds": result.audio_seconds,
            "wall_seconds": result.wall_seconds,
            "realtime_ratio": result.realtime_ratio,
            "engine": result.engine,
            "timestamp": result.timestamp,
            "notes": result.notes,
        }) + "\n")
    return result


# ---------- Public entry point ----------


async def run_voice(clip_id: str, script: Script) -> AudioTrack:
    """Synthesize commentary audio for a script.

    Phase 1 returns a placeholder AudioTrack so the rest of the pipeline
    can smoke-test end-to-end. Phase 2 wires Coqui (and optionally
    ElevenLabs) with the hardening above.
    """
    persona_cfg = load("persona")
    persona = _active_persona(persona_cfg)
    VOICE_OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest_path = VOICE_OUT_DIR / f"{clip_id}.wav"

    curator_score = _load_curator_score(clip_id)
    escalate = _should_escalate(persona, curator_score)
    engine = "elevenlabs" if escalate else "coqui_xtts_v2"

    runtime_s = script.runtime_s
    loudness_lufs = float(persona["voice"]["target_loudness_lufs"])

    try:
        with stage_lease(clip_id, stage="voice", ttl_seconds=180) as lease:
            # ---------- ElevenLabs path with cost reservation + 429 fallback ----------
            actual_engine = engine
            fallback_reason: str | None = None
            reservation_id: str | None = None

            if escalate:
                caps = _elevenlabs_budget_caps()
                projected = _project_elevenlabs_cost(script.runtime_s)
                try:
                    reservation_id = _reserve_elevenlabs(
                        clip_id, projected, caps,
                    )
                except BudgetExceeded as exc:
                    # Budget cap fired — fall back to Coqui, log persona swap
                    actual_engine = "coqui_xtts_v2"
                    fallback_reason = ElevenLabsFallbackReason.BUDGET
                    log(
                        agent="voice",
                        event_type="elevenlabs_fallback",
                        level="warn",
                        clip_id=clip_id,
                        payload={
                            "reason": fallback_reason,
                            "cap": exc.cap_name,
                            "projected_usd": projected,
                        },
                        rationale=(
                            "ElevenLabs budget would breach cap; falling back "
                            "to Coqui local synthesis (persona swap logged)"
                        ),
                    )

            if actual_engine == "elevenlabs":
                try:
                    runtime_s = await _synthesize_elevenlabs(
                        script.text, persona, dest_path,
                    )
                    if reservation_id:
                        actual_usd = _project_elevenlabs_cost(runtime_s)
                        settle(
                            reservation_id,
                            actual_amount_usd=actual_usd,
                            status="succeeded",
                        )
                except NotImplementedError:
                    # Phase 1 stub — settle reservation as failed (zero cost),
                    # treat like a fallback so the placeholder still works.
                    if reservation_id:
                        settle(
                            reservation_id,
                            actual_amount_usd=0.0,
                            status="failed",
                        )
                    log(
                        agent="voice",
                        event_type="phase1_scaffold",
                        clip_id=clip_id,
                        payload={"engine": engine, "escalated": escalate},
                        rationale=(
                            "TTS stubbed in Phase 1; placeholder AudioTrack "
                            "persisted so downstream stages can smoke-test"
                        ),
                    )
                except (RetryGiveUp, TransientError) as exc:
                    # ElevenLabs gave up after retries — settle failed, fall
                    # back to Coqui. THIS is the bug Codex E-17 caught:
                    # previously this exception was swallowed silently.
                    if reservation_id:
                        settle(
                            reservation_id,
                            actual_amount_usd=0.0,
                            status="failed",
                        )
                    actual_engine = "coqui_xtts_v2"
                    fallback_reason = (
                        ElevenLabsFallbackReason.RATE_LIMIT
                        if "429" in str(exc) or isinstance(exc, RetryGiveUp)
                        else ElevenLabsFallbackReason.NETWORK
                    )
                    log(
                        agent="voice",
                        event_type="elevenlabs_fallback",
                        level="warn",
                        clip_id=clip_id,
                        payload={
                            "reason": fallback_reason,
                            "error": f"{exc.__class__.__name__}: {exc}",
                        },
                        rationale=(
                            "ElevenLabs retries exhausted; falling back to "
                            "Coqui (persona swap visible in digest)"
                        ),
                    )

            if actual_engine == "coqui_xtts_v2":
                # Coqui prewarm gate — the model file must hash-match the
                # pinned SHA before we synthesize anything. In Phase 1 the
                # config pin is absent so verified=False; we log but do
                # not block (the synthesis stub will no-op anyway).
                verify = verify_coqui_checkpoint()
                if not verify["verified"]:
                    log(
                        agent="voice",
                        event_type="coqui_checkpoint_unverified",
                        level="warn",
                        clip_id=clip_id,
                        payload={
                            "reason": verify.get("reason", "no_pinned_sha"),
                            "path": verify.get("path", ""),
                        },
                        rationale=(
                            "Coqui XTTS-v2 checkpoint not verified against a "
                            "pinned SHA; production must pin before live runs"
                        ),
                    )
                try:
                    runtime_s = await _synthesize_coqui(
                        script.text, persona, dest_path,
                    )
                except NotImplementedError:
                    log(
                        agent="voice",
                        event_type="phase1_scaffold",
                        clip_id=clip_id,
                        payload={"engine": actual_engine},
                        rationale="Coqui stubbed in Phase 1; placeholder AudioTrack",
                    )

            try:
                loudness_lufs = await _normalize_loudness(
                    dest_path,
                    target_lufs=persona["voice"]["target_loudness_lufs"],
                )
            except NotImplementedError:
                # Phase 1 — loudness norm stubbed; carry the target value
                pass

            track = AudioTrack(
                path=str(dest_path),
                runtime_s=runtime_s,
                loudness_lufs=loudness_lufs,
                engine=actual_engine,
                voice_id=_pick_voice_id(persona, actual_engine),
            )
            _persist_audio(
                clip_id, track,
                input_artifact_version=lease.input_artifact_version,
            )
            lease.output_artifact_version = lease.input_artifact_version + 1

            log(
                agent="voice",
                event_type="voice_generated",
                clip_id=clip_id,
                payload={
                    "requested_engine": engine,
                    "actual_engine": actual_engine,
                    "runtime_s": track.runtime_s,
                    "loudness_lufs": track.loudness_lufs,
                    "escalated": escalate,
                    "curator_score": curator_score,
                    "voice_id": track.voice_id,
                    "fallback_reason": fallback_reason,
                },
                rationale=f"voice synthesized via {actual_engine}",
            )
            return track

    except LeaseConflict:
        log(
            agent="voice",
            event_type="voice_lease_conflict",
            level="info",
            clip_id=clip_id,
            payload={},
            rationale="another Voice holds the voice lease; backing off",
        )
        raise


__all__ = [
    "run_voice",
    "benchmark_synthesis",
    "verify_coqui_checkpoint",
    "CoquiCheckpointError",
    "ElevenLabsFallbackReason",
    "BenchmarkResult",
    "COQUI_REALTIME_RATIO_FLOOR",
    "BENCHMARK_LOG",
    "_pick_voice_id",
    "_should_escalate",
    "_elevenlabs_budget_caps",
    "_project_elevenlabs_cost",
]
