"""Visuals — generates Seedance 2.0 assets from the Writer's shot list.

Default provider is Atlas Cloud (per docs/seedance_access.md); fal.ai is the
fallback. The agent enforces per-clip cost caps (budget.yaml), Pro-tier
promotion gates, caches identical prompts in /data/generated_cache/, and
classifies every provider response through a typed parser that covers all
six observed Atlas Cloud states (submitted/processing/succeeded/face_filter/
rate_limit/error).

Hardening blocks (Days 9-10 of the revised Phase 2 plan):

- **Typed Atlas response parser (E-3):** `parse_atlas_response()` returns
  one of six `AtlasResponseStatus` values plus a structured payload.
  Replaces the brittle "is video_url present" check that silently
  conflated face-filter rejections with other 200-empty failures.

- **has_real_face_reference=0 on success:** when a generation succeeds,
  Visuals writes `clip_artifacts.has_real_face_reference=0` since
  Visuals never passes a real human image as `first_frame_url`
  (Compliance's no_real_face_seedance_reference rule fails closed on
  NULL — populating this column here is the explicit hand-off).

- **Daily Atlas cap (E-13):** every generation reserves cost against
  the `atlas_cloud` category before the call, with the daily cap from
  `config/budget.yaml` per_call_caps.atlas_cloud_daily_usd_max.
  `BudgetExceeded` quarantines the clip with a structured reason —
  Atlas Cloud doesn't have a free fallback, unlike Voice's Coqui.

- **MTD-scan composite index (E-24):** migration 005 adds
  `idx_seedance_tier_status_ts` so the per-call MTD line-item budget
  check seeks instead of full-scanning. Visuals' query pattern is
  unchanged; the index just makes it cheap.

- **stage_lease + commit_artifact:** Visuals wraps in
  `stage_lease(clip_id, "visuals", ttl_seconds=300)`. The
  Compositor-downstream columns (`final_video_path`, `final_duration_s`)
  are NULLed by the persist's upsert so a re-run after Compositor
  doesn't ship a final video against the prior visuals set (Codex
  Day 5-8 P1#2 pattern, applied here).

- **Retry on transient Atlas errors:** the provider call is wrapped in
  `retry_external` so 429 / 5xx / network errors back off + retry.
  `RetryGiveUp` falls back to fal.ai when wired (Phase 2); for now it
  quarantines with reason `atlas_retries_exhausted`.

Per CLAUDE.md "Architecture / Agent topology" — Visuals step 6, and
"Scene-aware visuals (Seedance 2.0)".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agents.config import load
from agents.costs import BudgetExceeded, reserve, settle
from agents.db import connect
from agents.events import log
from agents.models import GeneratedAsset, ShotListEntry, Tier
from agents.retry import RetryGiveUp, TransientError, retry_external
from agents.stage_lease import LeaseConflict, stage_lease

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / "generated_cache"
QUARANTINE_DIR = REPO_ROOT / "data" / "quarantine"


# ---------- Typed Atlas response parser (E-3) ----------

AtlasResponseStatus = Literal[
    "submitted",        # job accepted, not yet started
    "processing",       # in-flight on the provider
    "succeeded",        # video_url present, cost reported
    "face_filter",      # HTTP 200, empty/missing video_url — provider rejected
    "rate_limit",       # HTTP 429 (caller should retry per Retry-After)
    "error",            # generic failure (any other non-success state)
]


@dataclass
class AtlasResponse:
    """Typed parser output. `payload` carries the original response for the
    seedance_generations.raw_response_json audit column."""
    status: AtlasResponseStatus
    video_url: str | None
    cost_usd: float
    payload: dict[str, Any]
    retry_after_s: float | None = None  # populated on rate_limit


def parse_atlas_response(response: Any) -> AtlasResponse:
    """Classify a single Atlas Cloud response into one of six typed states.

    Replaces the prior "if video_url then success else face_filter" check
    that silently conflated three distinct failure modes:
      - HTTP 200 with empty body          → face_filter
      - HTTP 200 with status='error'      → error
      - HTTP 429 (rate-limited)           → rate_limit (caller retries)

    The parser is defensive: callers must NEVER trust the raw dict's
    presence of `video_url` without going through this function.
    """
    if not isinstance(response, dict):
        return AtlasResponse(
            status="error",
            video_url=None,
            cost_usd=0.0,
            payload={"raw": repr(response)[:200]},
        )

    # Atlas-Cloud-style status field. Different providers use different
    # field names; we read the union for portability.
    provider_status = (
        response.get("status")
        or response.get("state")
        or response.get("job_status")
    )

    # Rate-limit signal: provider returned a 429-shaped response OR our
    # HTTP layer wrapped it with a structured rate_limit flag.
    if response.get("rate_limited") or provider_status == "rate_limited":
        return AtlasResponse(
            status="rate_limit",
            video_url=None,
            cost_usd=0.0,
            payload=response,
            retry_after_s=_coerce_float(response.get("retry_after_s")),
        )

    if provider_status in ("submitted", "queued", "pending"):
        return AtlasResponse(
            status="submitted",
            video_url=None,
            cost_usd=0.0,
            payload=response,
        )

    if provider_status in ("processing", "running", "in_progress"):
        return AtlasResponse(
            status="processing",
            video_url=None,
            cost_usd=0.0,
            payload=response,
        )

    if provider_status == "error":
        return AtlasResponse(
            status="error",
            video_url=None,
            cost_usd=0.0,
            payload=response,
        )

    video_url = response.get("video_url") or response.get("url")
    if video_url:
        return AtlasResponse(
            status="succeeded",
            video_url=str(video_url),
            cost_usd=_coerce_float(response.get("cost_usd")) or 0.0,
            payload=response,
        )

    # HTTP 200 with no video_url is the face-filter signature
    # (Atlas-specific: ByteDance silently drops face-containing
    # requests instead of returning an error code).
    return AtlasResponse(
        status="face_filter",
        video_url=None,
        cost_usd=0.0,
        payload=response,
    )


def _coerce_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------- Provider stubs ----------


@retry_external(max_attempts=3, base_delay_s=2.0)
async def _atlas_cloud_generate(
    prompt: str,
    *,
    duration_s: float,
    tier: Tier,
    seed: int | None,
    reference_image_path: str | None,
) -> dict:
    """Atlas Cloud Seedance 2.0 endpoint. Wrapped in retry_external so
    rate-limit / network errors back off + retry.

    Phase 2 wires the actual POST. Transient errors (HTTP 429, 5xx,
    network) raise TransientError so retry_external honors them.
    Permanent errors (400, malformed response) propagate to the caller
    who quarantines.
    """
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


@retry_external(max_attempts=2, base_delay_s=2.0)
async def _fal_ai_generate(
    prompt: str,
    *,
    duration_s: float,
    tier: Tier,
    seed: int | None,
    reference_image_path: str | None,
) -> dict:
    """fal.ai fallback for when Atlas Cloud is down or rate-limited.
    Phase 2 wires the actual call."""
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


# ---------- Cache ----------


def _prompt_hash(prompt: str, *, seed: int | None, duration_s: float, tier: Tier) -> str:
    h = hashlib.sha256()
    h.update(f"{prompt}|{seed}|{duration_s}|{tier}".encode())
    return h.hexdigest()


def _cache_lookup(prompt_hash: str) -> GeneratedAsset | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM generated_cache WHERE prompt_hash = ?", (prompt_hash,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE generated_cache SET hit_count = hit_count + 1 WHERE prompt_hash = ?",
            (prompt_hash,),
        )
    return GeneratedAsset(
        path=row["asset_path"],
        duration_s=row["duration_s"],
        cost_usd=0.0,                 # cache hit is free
        tier=row["tier"],
        provider=row["provider"],
        prompt=row["prompt"],
    )


def _cache_insert(prompt_hash: str, asset: GeneratedAsset) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO generated_cache
              (prompt_hash, prompt, asset_path, provider, tier, duration_s, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prompt_hash, asset.prompt, asset.path, asset.provider,
                asset.tier, asset.duration_s, asset.cost_usd,
            ),
        )


# ---------- Budget / promotion gates ----------


def _month_to_date_pro_spend() -> float:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0) AS spend FROM seedance_generations
             WHERE tier = 'pro' AND status = 'succeeded'
               AND strftime('%Y-%m', ts) = strftime('%Y-%m', 'now')
            """
        ).fetchone()
    return float(row["spend"])


def _month_to_date_fast_spend() -> float:
    """Aggregate spend on Seedance fast-tier this calendar month. The
    `idx_seedance_tier_status_ts` composite index (migration 005) lets
    SQLite seek instead of scan."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0) AS spend FROM seedance_generations
             WHERE tier = 'fast' AND status = 'succeeded'
               AND strftime('%Y-%m', ts) = strftime('%Y-%m', 'now')
            """
        ).fetchone()
    return float(row["spend"])


def _line_item_budget(budget_cfg: dict, line_item: str) -> float:
    return float(((budget_cfg.get("line_items") or {})
                  .get(line_item) or {})
                 .get("monthly_budget_usd", 0))


def _atlas_caps(budget_cfg: dict) -> dict:
    """Resolve daily + monthly caps for the atlas_cloud cost category.

    The cost-reservation layer uses the `atlas_cloud` category as a
    *meta-category* for per-call accounting that's independent of the
    per-tier line items (seedance_fast / seedance_pro). Daily cap from
    per_call_caps.atlas_cloud_daily_usd_max.
    """
    per_call = budget_cfg.get("per_call_caps") or {}
    return {
        "daily_cap_usd": _coerce_float(per_call.get("atlas_cloud_daily_usd_max")),
    }


def _eligible_for_pro(shot: ShotListEntry, *, curator_score: float | None,
                     predicted_views: int | None, budget_cfg: dict) -> bool:
    gates = budget_cfg["pro_tier_promotion"]
    if shot.shot_type != "hero_shot" and gates.get("hero_shot_required", True):
        return False
    if curator_score is None or curator_score < gates["curator_score_min"]:
        return False
    if predicted_views is None or predicted_views < gates["projected_views_min"]:
        return False
    if _month_to_date_pro_spend() >= gates["month_to_date_pro_spend_cap_usd"]:
        return False
    return True


# ---------- Persistence ----------


def _log_generation(
    clip_id: str,
    *,
    provider: str,
    model_version: str,
    tier: Tier,
    prompt: str,
    reference_image_hash: str | None,
    seed: int | None,
    duration_s: float,
    cost_usd: float,
    status: str,
    output_path: str | None,
    raw_response: dict | None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO seedance_generations
              (clip_id, provider, model_version, tier, prompt, reference_image_hash,
               seed, duration_s, cost_usd, status, output_path, raw_response_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clip_id, provider, model_version, tier, prompt, reference_image_hash,
                seed, duration_s, cost_usd, status, output_path,
                json.dumps(raw_response) if raw_response is not None else None,
            ),
        )


def _quarantine_clip(clip_id: str, reason: str) -> None:
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    marker = QUARANTINE_DIR / f"{clip_id}.visuals.reason.txt"
    marker.write_text(reason)
    with connect() as conn:
        conn.execute(
            "UPDATE clips_candidate SET status = 'quarantined' WHERE id = ?",
            (clip_id,),
        )
    log(
        agent="visuals",
        event_type="clip_quarantined",
        level="warn",
        clip_id=clip_id,
        payload={"reason": reason[:200]},
        rationale=f"visuals quarantined {clip_id}: {reason[:120]}",
    )


def _persist_visuals(
    lease,
    *,
    seconds: float,
    tier_used: str,
    cost: float,
) -> None:
    """Atomic persist via `lease.commit_artifact()` — version-check +
    data write + version bump + downstream invalidation in one
    BEGIN IMMEDIATE.

    Sets `has_real_face_reference=0` on successful generation: Visuals
    never feeds a real human image into Seedance's reference_image_path
    (the compliance rule fails closed on NULL, so populating it here is
    the explicit hand-off to Compliance).

    Compositor's downstream output (`final_video_path`,
    `final_duration_s`) is invalidated: any prior final video was
    rendered against an older visuals set and is stale once we re-run.
    """
    clip_id = lease.clip_id

    def _do_persist(conn, new_version):
        conn.execute(
            """
            INSERT INTO clip_artifacts
              (clip_id, visuals_seconds_used, visuals_tier, visuals_cost_usd,
               has_real_face_reference, artifact_version, updated_at,
               final_video_path, final_duration_s)
            VALUES (?, ?, ?, ?, 0, ?, datetime('now'), NULL, NULL)
            ON CONFLICT(clip_id) DO UPDATE SET
              visuals_seconds_used = excluded.visuals_seconds_used,
              visuals_tier         = excluded.visuals_tier,
              visuals_cost_usd     = excluded.visuals_cost_usd,
              has_real_face_reference = excluded.has_real_face_reference,
              artifact_version     = excluded.artifact_version,
              updated_at           = datetime('now'),
              -- Compositor downstream invalidation: a final video built
              -- against an older visuals set is stale once we re-run.
              final_video_path     = NULL,
              final_duration_s     = NULL
            """,
            (clip_id, seconds, tier_used, cost, new_version),
        )

    lease.commit_artifact(_do_persist)


# ---------- Public entry point ----------


async def run_visuals(clip_id: str, shot_list: list[ShotListEntry]) -> list[GeneratedAsset]:
    """Generate (or cache-hit) every shot in the shot list. Enforce caps,
    classify responses through the typed parser, settle cost reservations,
    quarantine on hard failure.

    Phase 1 scaffold: provider calls raise NotImplementedError, so every
    non-cache-hit shot is logged as a phase1_scaffold event and skipped.
    """
    budget_cfg = load("budget")
    per_clip_caps = budget_cfg["per_clip_caps"]
    seconds_cap = float(per_clip_caps["seedance_seconds_max"])
    fast_cost_cap = float(per_clip_caps["seedance_cost_usd_max_fast"])
    pro_cost_cap = float(per_clip_caps["seedance_cost_usd_max_pro"])
    atlas_caps = _atlas_caps(budget_cfg)

    with connect() as conn:
        row = conn.execute(
            "SELECT virality_score, predicted_views FROM clips_candidate WHERE id = ?",
            (clip_id,),
        ).fetchone()
    curator_score = row["virality_score"] if row else None
    predicted_views = row["predicted_views"] if row else None

    assets: list[GeneratedAsset] = []
    total_seconds = 0.0
    total_cost = 0.0
    tier_mix: set[Tier] = set()

    try:
        with stage_lease(clip_id, stage="visuals", ttl_seconds=300) as lease:
            for shot in shot_list:
                chosen_tier: Tier = "pro" if _eligible_for_pro(
                    shot, curator_score=curator_score,
                    predicted_views=predicted_views, budget_cfg=budget_cfg,
                ) else "fast"
                cost_cap = pro_cost_cap if chosen_tier == "pro" else fast_cost_cap

                # ---------- Hard cap: per-clip seconds ----------
                if total_seconds + shot.duration_s > seconds_cap:
                    _quarantine_clip(
                        clip_id,
                        f"visuals seconds cap {seconds_cap}s would be exceeded",
                    )
                    log(agent="visuals", event_type="cap_exceeded", level="blocked",
                        clip_id=clip_id,
                        payload={"cap": "seedance_seconds_max", "limit": seconds_cap,
                                 "attempted": total_seconds + shot.duration_s},
                        rationale="clip routed to /data/quarantine/")
                    return assets

                # ---------- Cache lookup ----------
                prompt_hash = _prompt_hash(shot.prompt, seed=None,
                                           duration_s=shot.duration_s, tier=chosen_tier)
                cached = _cache_lookup(prompt_hash)
                if cached is not None:
                    assets.append(cached)
                    total_seconds += cached.duration_s
                    tier_mix.add(cached.tier)
                    log(agent="visuals", event_type="cache_hit", clip_id=clip_id,
                        payload={"prompt_hash": prompt_hash, "shot_type": shot.shot_type},
                        rationale="reused cached generation")
                    continue

                # ---------- Cost reservation (E-13) ----------
                # Projected cost from the per-clip cap; settled below with
                # the actual cost from the typed response.
                projected_cost = cost_cap  # worst-case projection
                try:
                    reservation_id = reserve(
                        category="atlas_cloud",
                        amount_usd=projected_cost,
                        daily_cap_usd=atlas_caps["daily_cap_usd"],
                        detail=f"visuals-{chosen_tier}:{clip_id}:{shot.shot_type}",
                        provider="atlas_cloud",
                        clip_id=clip_id,
                    )
                except BudgetExceeded as exc:
                    # Atlas Cloud has no free fallback (unlike Voice's Coqui),
                    # so a daily-cap breach quarantines the clip.
                    _quarantine_clip(
                        clip_id,
                        f"atlas_cloud daily cap exceeded: {exc.cap_name} "
                        f"attempted=${exc.attempted_total:.2f} > ${exc.cap}",
                    )
                    log(agent="visuals", event_type="cap_exceeded", level="blocked",
                        clip_id=clip_id,
                        payload={"cap": exc.cap_name, "limit": exc.cap,
                                 "attempted": exc.attempted_total},
                        rationale="atlas_cloud_daily_usd_max exceeded; clip quarantined")
                    return assets

                # ---------- Provider call (with retry on transient) ----------
                response: Any = None
                try:
                    response = await _atlas_cloud_generate(
                        shot.prompt,
                        duration_s=shot.duration_s,
                        tier=chosen_tier,
                        seed=None,
                        reference_image_path=None,
                    )
                except NotImplementedError:
                    settle(reservation_id, actual_amount_usd=0.0, status="failed")
                    log(agent="visuals", event_type="phase1_scaffold", clip_id=clip_id,
                        payload={"shot_type": shot.shot_type, "tier": chosen_tier},
                        rationale="Atlas Cloud Seedance call stubbed in Phase 1")
                    continue
                except RetryGiveUp as exc:
                    settle(reservation_id, actual_amount_usd=0.0, status="failed")
                    _quarantine_clip(
                        clip_id,
                        f"atlas_retries_exhausted: {exc}",
                    )
                    log(agent="visuals", event_type="atlas_retries_exhausted",
                        level="warn", clip_id=clip_id,
                        payload={"shot_type": shot.shot_type, "tier": chosen_tier,
                                 "error": str(exc)},
                        rationale="Atlas Cloud retries exhausted; clip quarantined")
                    return assets
                except TransientError as exc:
                    # TransientError reaches the caller only when the test
                    # patch bypasses the retry decorator. In production
                    # the decorator catches it and either retries or
                    # wraps in RetryGiveUp.
                    settle(reservation_id, actual_amount_usd=0.0, status="failed")
                    _quarantine_clip(
                        clip_id, f"atlas_transient_error: {exc}",
                    )
                    return assets

                # ---------- Typed response parsing (E-3) ----------
                parsed = parse_atlas_response(response)

                if parsed.status in ("submitted", "processing"):
                    # Job accepted but not yet complete. For now we treat
                    # as a soft skip: settle as failed (no cost), log,
                    # move on. Phase 2 will add a poll loop.
                    settle(reservation_id, actual_amount_usd=0.0, status="failed")
                    log(agent="visuals", event_type="atlas_pending",
                        level="info", clip_id=clip_id,
                        payload={"status": parsed.status, "shot_type": shot.shot_type},
                        rationale="Atlas job not yet complete; deferred to next run")
                    continue

                if parsed.status == "rate_limit":
                    settle(reservation_id, actual_amount_usd=0.0, status="failed")
                    log(agent="visuals", event_type="atlas_rate_limit",
                        level="warn", clip_id=clip_id,
                        payload={"shot_type": shot.shot_type,
                                 "retry_after_s": parsed.retry_after_s},
                        rationale="Atlas rate-limit (provider 429); shot deferred")
                    continue

                if parsed.status == "face_filter":
                    settle(reservation_id, actual_amount_usd=0.0, status="failed")
                    _log_generation(
                        clip_id, provider="atlas_cloud",
                        model_version=f"seedance-2.0-{chosen_tier}", tier=chosen_tier,
                        prompt=shot.prompt, reference_image_hash=None, seed=None,
                        duration_s=shot.duration_s, cost_usd=0.0,
                        status="failed_face_filter", output_path=None,
                        raw_response=parsed.payload,
                    )
                    _quarantine_clip(
                        clip_id,
                        "Seedance face-filter rejection (HTTP 200 empty body)",
                    )
                    log(agent="visuals", event_type="face_filter_rejection",
                        level="blocked", clip_id=clip_id,
                        payload={"shot_type": shot.shot_type},
                        rationale="provider returned 200 with no video_url; clip quarantined")
                    return assets

                if parsed.status == "error":
                    settle(reservation_id, actual_amount_usd=0.0, status="failed")
                    _log_generation(
                        clip_id, provider="atlas_cloud",
                        model_version=f"seedance-2.0-{chosen_tier}", tier=chosen_tier,
                        prompt=shot.prompt, reference_image_hash=None, seed=None,
                        duration_s=shot.duration_s, cost_usd=0.0,
                        status="failed_other", output_path=None,
                        raw_response=parsed.payload,
                    )
                    _quarantine_clip(
                        clip_id, "atlas_cloud returned error state",
                    )
                    return assets

                # ---------- Success ----------
                actual_cost = parsed.cost_usd

                # MTD line-item budget check — fail-closed before settling.
                line_item = f"seedance_{chosen_tier}"
                mtd_spend = (_month_to_date_pro_spend() if chosen_tier == "pro"
                             else _month_to_date_fast_spend())
                line_budget = _line_item_budget(budget_cfg, line_item)
                if line_budget > 0 and mtd_spend + actual_cost > line_budget:
                    settle(reservation_id, actual_amount_usd=0.0, status="failed")
                    _quarantine_clip(
                        clip_id,
                        f"monthly line-item budget exceeded for {line_item}: "
                        f"MTD=${mtd_spend:.2f} + ${actual_cost:.2f} > ${line_budget:.2f}",
                    )
                    log(agent="visuals", event_type="cap_exceeded", level="blocked",
                        clip_id=clip_id,
                        payload={"cap": f"{line_item}.monthly_budget_usd",
                                 "limit": line_budget,
                                 "mtd_spend": mtd_spend,
                                 "attempted_increment": actual_cost},
                        rationale="month-to-date line-item exceeded; clip quarantined")
                    return assets

                # Per-clip cost cap.
                if total_cost + actual_cost > cost_cap:
                    settle(reservation_id, actual_amount_usd=0.0, status="failed")
                    _quarantine_clip(
                        clip_id,
                        f"visuals cost cap ${cost_cap} would be exceeded",
                    )
                    log(agent="visuals", event_type="cap_exceeded", level="blocked",
                        clip_id=clip_id,
                        payload={"cap": "seedance_cost_usd", "limit": cost_cap,
                                 "attempted": total_cost + actual_cost},
                        rationale="per-clip cost cap exceeded; clip quarantined")
                    return assets

                # Settle the cost reservation with the actual amount.
                settle(reservation_id, actual_amount_usd=actual_cost, status="succeeded")

                asset = GeneratedAsset(
                    path=str(parsed.video_url),
                    duration_s=shot.duration_s,
                    cost_usd=actual_cost,
                    tier=chosen_tier,
                    provider="atlas_cloud",
                    prompt=shot.prompt,
                )
                _log_generation(
                    clip_id, provider="atlas_cloud",
                    model_version=f"seedance-2.0-{chosen_tier}", tier=chosen_tier,
                    prompt=shot.prompt, reference_image_hash=None, seed=None,
                    duration_s=shot.duration_s, cost_usd=actual_cost,
                    status="succeeded", output_path=asset.path,
                    raw_response=parsed.payload,
                )
                _cache_insert(prompt_hash, asset)
                assets.append(asset)
                total_seconds += asset.duration_s
                total_cost += asset.cost_usd
                tier_mix.add(chosen_tier)

            # ---------- Persist artifact (atomic via commit_artifact) ----------
            tier_used = (
                "mixed" if len(tier_mix) > 1
                else (next(iter(tier_mix)) if tier_mix else "fast")
            )
            _persist_visuals(
                lease,
                seconds=total_seconds,
                tier_used=tier_used,
                cost=total_cost,
            )

            log(agent="visuals", event_type="visuals_complete", clip_id=clip_id,
                payload={"assets": len(assets), "seconds": total_seconds,
                         "cost_usd": total_cost, "tier": tier_used},
                rationale=f"{len(assets)} assets / {total_seconds:.1f}s / ${total_cost:.3f}")

    except LeaseConflict:
        log(agent="visuals", event_type="visuals_lease_conflict",
            level="info", clip_id=clip_id, payload={},
            rationale="another Visuals holds the visuals lease; backing off")
        raise

    return assets


__all__ = [
    "run_visuals",
    "AtlasResponse",
    "AtlasResponseStatus",
    "parse_atlas_response",
    "_atlas_caps",
    "_eligible_for_pro",
    "_quarantine_clip",
]
