"""Visuals — generates Seedance 2.0 assets from the Writer's shot list.

Default provider is Atlas Cloud (per docs/seedance_access.md); fal.ai is the
fallback. The agent enforces per-clip cost caps (budget.yaml) and Pro-tier
promotion gates, caches identical prompts in /data/generated_cache/, and
handles the "HTTP 200 with empty body" face-filter rejection by marking the
generation `failed_face_filter` and routing the clip to /data/quarantine/.

Live provider calls are deferred to Phase 2 — every external call is a
NotImplementedError stub.

Per CLAUDE.md "Architecture / Agent topology" — Visuals step 6, and
"Scene-aware visuals (Seedance 2.0)".
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from agents.config import load
from agents.db import connect
from agents.events import log
from agents.models import GeneratedAsset, ShotListEntry, Tier

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / "generated_cache"
QUARANTINE_DIR = REPO_ROOT / "data" / "quarantine"


# ---------- Provider stubs ----------

async def _atlas_cloud_generate(
    prompt: str,
    *,
    duration_s: float,
    tier: Tier,
    seed: int | None,
    reference_image_path: str | None,
) -> dict:
    # TODO(phase2): wire Atlas Cloud Seedance 2.0 endpoint.
    # Needs: ATLAS_CLOUD_API_KEY, request body per docs/seedance_access.md.
    # CRITICAL: face-filter rejection returns HTTP 200 with empty/missing
    # video_url. Callers must check response["video_url"] before treating
    # the call as successful.
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


async def _fal_ai_generate(
    prompt: str,
    *,
    duration_s: float,
    tier: Tier,
    seed: int | None,
    reference_image_path: str | None,
) -> dict:
    # TODO(phase2): fal.ai fallback when Atlas Cloud is down or rate-limited.
    # Needs: FAL_KEY, model identifier for seedance-2.0-{fast,pro}.
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


# ---------- Pro-tier promotion gate ----------

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
    """Aggregate spend on Seedance fast-tier this calendar month.

    Used to enforce config/budget.yaml line_items.seedance_fast.monthly_budget_usd
    — the missing aggregation Codex flagged: per-clip caps alone allow ~$105/mo
    against a $50 line item at 7 clips/day. The line-item-vs-MTD check below
    fails-closed (quarantines) before a generation that would push over.
    """
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


def _update_artifact_cost(clip_id: str, *, seconds: float, tier_used: str, cost: float) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO clip_artifacts
              (clip_id, visuals_seconds_used, visuals_tier, visuals_cost_usd, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(clip_id) DO UPDATE SET
              visuals_seconds_used = excluded.visuals_seconds_used,
              visuals_tier         = excluded.visuals_tier,
              visuals_cost_usd     = excluded.visuals_cost_usd,
              updated_at           = datetime('now')
            """,
            (clip_id, seconds, tier_used, cost),
        )


def _quarantine_clip(clip_id: str, reason: str) -> None:
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    marker = QUARANTINE_DIR / f"{clip_id}.reason.txt"
    marker.write_text(reason)
    with connect() as conn:
        conn.execute(
            "UPDATE clips_candidate SET status = 'quarantined' WHERE id = ?",
            (clip_id,),
        )


# ---------- Public entry point ----------

async def run_visuals(clip_id: str, shot_list: list[ShotListEntry]) -> list[GeneratedAsset]:
    """Generate (or cache-hit) every shot in the shot list. Enforce caps.

    Phase 1 scaffold: provider calls raise NotImplementedError, so every
    non-cache-hit shot is logged as a phase1_scaffold event and skipped.
    """
    budget_cfg = load("budget")
    per_clip_caps = budget_cfg["per_clip_caps"]
    seconds_cap = float(per_clip_caps["seedance_seconds_max"])
    fast_cost_cap = float(per_clip_caps["seedance_cost_usd_max_fast"])
    pro_cost_cap = float(per_clip_caps["seedance_cost_usd_max_pro"])

    # Read clip metadata for the Pro-tier promotion gate.
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

    for shot in shot_list:
        chosen_tier: Tier = "pro" if _eligible_for_pro(
            shot, curator_score=curator_score,
            predicted_views=predicted_views, budget_cfg=budget_cfg,
        ) else "fast"
        cost_cap = pro_cost_cap if chosen_tier == "pro" else fast_cost_cap

        # Hard caps — fail-closed.
        if total_seconds + shot.duration_s > seconds_cap:
            _quarantine_clip(clip_id, f"visuals seconds cap {seconds_cap}s would be exceeded")
            log(agent="visuals", event_type="cap_exceeded", level="blocked",
                clip_id=clip_id,
                payload={"cap": "seedance_seconds_max", "limit": seconds_cap,
                         "attempted": total_seconds + shot.duration_s},
                rationale="clip routed to /data/quarantine/")
            return assets

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

        # Live provider call — Phase 1 stub.
        try:
            response = await _atlas_cloud_generate(
                shot.prompt,
                duration_s=shot.duration_s,
                tier=chosen_tier,
                seed=None,
                reference_image_path=None,
            )
        except NotImplementedError:
            log(agent="visuals", event_type="phase1_scaffold", clip_id=clip_id,
                payload={"shot_type": shot.shot_type, "tier": chosen_tier},
                rationale="Atlas Cloud Seedance call stubbed in Phase 1")
            continue

        # Face-filter rejection: HTTP 200 with empty body / no video_url.
        video_url = response.get("video_url") if isinstance(response, dict) else None
        if not video_url:
            _log_generation(
                clip_id, provider="atlas_cloud",
                model_version=f"seedance-2.0-{chosen_tier}", tier=chosen_tier,
                prompt=shot.prompt, reference_image_hash=None, seed=None,
                duration_s=shot.duration_s, cost_usd=0.0,
                status="failed_face_filter", output_path=None,
                raw_response=response,
            )
            _quarantine_clip(clip_id, "Seedance face-filter rejection (HTTP 200 empty body)")
            log(agent="visuals", event_type="face_filter_rejection", level="blocked",
                clip_id=clip_id, payload={"shot_type": shot.shot_type},
                rationale="provider returned 200 with no video_url; clip quarantined")
            return assets

        # Successful generation (Phase 2 wiring would populate these fields).
        cost = float(response.get("cost_usd", 0.0))

        # Month-to-date line-item budget enforcement (Codex P1 fix). Fast and
        # pro tiers have separate budgets in config/budget.yaml; the per-clip
        # cap above doesn't aggregate across clips. Fail-closed if this
        # generation would push the month over the line-item budget.
        line_item = f"seedance_{chosen_tier}"
        mtd_spend = (_month_to_date_pro_spend() if chosen_tier == "pro"
                     else _month_to_date_fast_spend())
        line_budget = _line_item_budget(budget_cfg, line_item)
        if line_budget > 0 and mtd_spend + cost > line_budget:
            _quarantine_clip(
                clip_id,
                f"monthly line-item budget exceeded for {line_item}: "
                f"MTD=${mtd_spend:.2f} + ${cost:.2f} > ${line_budget:.2f}",
            )
            log(agent="visuals", event_type="cap_exceeded", level="blocked",
                clip_id=clip_id,
                payload={"cap": f"{line_item}.monthly_budget_usd",
                         "limit": line_budget,
                         "mtd_spend": mtd_spend,
                         "attempted_increment": cost},
                rationale=(
                    f"month-to-date {line_item} would exceed line item; "
                    "clip routed to /data/quarantine/"
                ))
            return assets

        if total_cost + cost > cost_cap:
            _quarantine_clip(clip_id, f"visuals cost cap ${cost_cap} would be exceeded")
            log(agent="visuals", event_type="cap_exceeded", level="blocked",
                clip_id=clip_id,
                payload={"cap": "seedance_cost_usd", "limit": cost_cap,
                         "attempted": total_cost + cost},
                rationale="clip routed to /data/quarantine/")
            return assets

        asset = GeneratedAsset(
            path=str(video_url),
            duration_s=shot.duration_s,
            cost_usd=cost,
            tier=chosen_tier,
            provider="atlas_cloud",
            prompt=shot.prompt,
        )
        _log_generation(
            clip_id, provider="atlas_cloud",
            model_version=f"seedance-2.0-{chosen_tier}", tier=chosen_tier,
            prompt=shot.prompt, reference_image_hash=None, seed=None,
            duration_s=shot.duration_s, cost_usd=cost,
            status="succeeded", output_path=asset.path, raw_response=response,
        )
        _cache_insert(_prompt_hash(shot.prompt, seed=None,
                                   duration_s=shot.duration_s, tier=chosen_tier), asset)
        assets.append(asset)
        total_seconds += asset.duration_s
        total_cost += asset.cost_usd
        tier_mix.add(chosen_tier)

    tier_used = "mixed" if len(tier_mix) > 1 else (next(iter(tier_mix)) if tier_mix else "fast")
    _update_artifact_cost(clip_id, seconds=total_seconds, tier_used=tier_used, cost=total_cost)

    log(agent="visuals", event_type="visuals_complete", clip_id=clip_id,
        payload={"assets": len(assets), "seconds": total_seconds,
                 "cost_usd": total_cost, "tier": tier_used},
        rationale=f"{len(assets)} assets / {total_seconds:.1f}s / ${total_cost:.3f}")
    # shutil is imported in case Phase 2 wiring needs to relocate cache assets.
    _ = shutil
    return assets
