"""Curator — ranks Scout output by predicted view-to-effort ratio.

Reads `clips_candidate` rows where status='discovered', scores each by
virality, and atomically promotes the top N to status='curated' with a
virality_score. Two-tier scoring per CLAUDE.md: cheap heuristic first
(view-count + creator weight, log-squashed to 0..1), LLM tiebreaker only
for borderline scores (0.4-0.7) where the heuristic isn't decisive.

Hardening (Days 3-4 of revised Phase 2 plan):

- **Atomic claim (E-4):** the SELECT-then-UPDATE batch is wrapped in
  BEGIN IMMEDIATE + status-conditional UPDATE so two concurrent Curator
  runs can't double-promote the same clip. Mirrors publisher's
  _pick_next_clip pattern.
- **Cost reservation for LLM calls (E-2 reuse):** every borderline-band
  clip reserves anthropic_api_buffer spend before the LLM call. The
  reservation enforces both per-day and per-month caps; reserve() fails
  fast and the heuristic score wins if the budget is exhausted.

Per CLAUDE.md "Architecture / Agent topology" — Curator step 2.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from agents.config import load
from agents.costs import BudgetExceeded, reserve, settle
from agents.db import connect
from agents.events import log
from agents.models import CandidateClip
from agents.retry import RetryGiveUp
from agents.stage_lease import LeaseConflict, stage_lease


# Creator-weight applied to the log-view-count heuristic. Phase 2 LLM
# scoring runs only for clips whose heuristic lands in the borderline
# band; clear-cut clips skip LLM cost.
_CREATOR_WEIGHTS: dict[str, float] = {
    "IShowSpeed": 1.20,
    "Kai Cenat": 1.15,
    "Sketch": 1.00,
    "Jynxzi": 1.05,
    "Adin Ross": 0.95,   # elevated Compliance scrutiny per creators.yaml drama_override
}

# Borderline band where the heuristic alone isn't decisive — Phase 2
# Curator calls the LLM to break the tie. Outside this band, heuristic
# wins (clear-cut top and bottom skip LLM cost).
_BORDERLINE_LOW = 0.40
_BORDERLINE_HIGH = 0.70

# Cost per Curator LLM tiebreaker call. Tunable in budget.yaml later.
_LLM_COST_PER_TIEBREAKER_USD = 0.03


@dataclass
class CuratorRunSummary:
    """Returned by run_curator for test + audit consumers."""
    considered: int
    promoted: int
    skipped_no_budget: int
    llm_calls_attempted: int
    llm_calls_succeeded: int


# ---------- Scoring ----------

def _heuristic_score(view_count: int | None, creator: str) -> float:
    """Cheap deterministic score for the bulk of candidates. log10(views)
    squashed to 0..1, weighted by creator. Clear-cut clips ride this
    score alone; only borderline scores invoke the LLM."""
    if not view_count or view_count <= 0:
        return 0.0
    weight = _CREATOR_WEIGHTS.get(creator, 1.0)
    raw = math.log10(view_count + 1) / 8.0  # log10(1e8) ≈ 1.0
    return max(0.0, min(1.0, raw * weight))


def _is_borderline(score: float) -> bool:
    return _BORDERLINE_LOW <= score <= _BORDERLINE_HIGH


async def _llm_tiebreaker_score(candidate: CandidateClip, heuristic: float) -> float:
    """Phase 2 LLM call that refines a borderline heuristic. Inputs the
    LLM gets: candidate metadata, transcript snippet (when Editor wires
    it), trending refs (sanitized via trending_sanitizer). Output: a
    refined score in 0..1.

    Phase 1 stub raises NotImplementedError; the caller catches and
    falls back to the heuristic. Cost reservation around the call is
    the testable behavior for now."""
    raise NotImplementedError("LLM tiebreaker not implemented in Phase 1 scaffold")


async def _refine_with_llm_or_fallback(
    candidate: CandidateClip,
    heuristic: float,
    *,
    line_item_cap_usd: float | None,
    daily_cap_usd: float | None,
) -> tuple[float, bool, bool]:
    """Reserve budget, attempt LLM tiebreaker, settle. Returns
    (final_score, llm_attempted, llm_succeeded).

    Race-prevention (Codex 2026-05-18 fix): wrap the LLM call in a
    per-clip `stage_lease(candidate.id, "curator")`. Two concurrent
    Curators on the same borderline clip would otherwise both reserve
    budget, both call the LLM, both pay — even though only one wins
    the eventual _promote_with_cas race. The lease blocks the second
    Curator at acquire time (LeaseConflict before any reservation
    happens) → zero double-pay. The loser falls back to heuristic.

    The lease ttl is 60s — short enough that a crashed/hung Curator
    doesn't block the second worker for long; the janitor sweep
    handles the cleanup. We don't set output_artifact_version on the
    lease because Curator doesn't write a clip artifact (only
    clips_candidate state changes).

    Error handling:
        - LeaseConflict (acquire) → another Curator owns this clip;
          heuristic wins, attempted=False, no cost
        - BudgetExceeded (reserve) → cap protection fired; heuristic
          wins, attempted=False, no cost
        - NotImplementedError (Phase 1 stub) → settle failed (zero
          cost), heuristic wins, attempted=True succeeded=False
        - asyncio.TimeoutError / RetryGiveUp → transient, fall back
          to heuristic, run continues; previously aborted run
        - Other Exception → settle failed, re-raise (programming
          bugs shouldn't be swallowed)
    """
    try:
        with stage_lease(candidate.id, stage="curator", ttl_seconds=60):
            return await _do_llm_tiebreaker(
                candidate, heuristic,
                line_item_cap_usd=line_item_cap_usd,
                daily_cap_usd=daily_cap_usd,
            )
    except LeaseConflict:
        log(
            agent="curator",
            event_type="llm_skipped_concurrent",
            level="info",
            clip_id=candidate.id,
            payload={"heuristic_score": heuristic},
            rationale=(
                "another Curator already holds the curator lease for this clip; "
                "skipping LLM tiebreaker to avoid double-pay"
            ),
        )
        return heuristic, False, False


async def _do_llm_tiebreaker(
    candidate: CandidateClip,
    heuristic: float,
    *,
    line_item_cap_usd: float | None,
    daily_cap_usd: float | None,
) -> tuple[float, bool, bool]:
    """Inner: actually reserve + call + settle. Wrapped by the
    stage_lease in `_refine_with_llm_or_fallback` so concurrent
    Curators on the same clip back off at acquire time."""
    try:
        reservation = reserve(
            category="anthropic_api_buffer",
            amount_usd=_LLM_COST_PER_TIEBREAKER_USD,
            line_item_cap_usd=line_item_cap_usd,
            daily_cap_usd=daily_cap_usd,
            detail=f"curator-tiebreaker:{candidate.id}",
            provider="anthropic",
            clip_id=candidate.id,
        )
    except BudgetExceeded as exc:
        log(
            agent="curator",
            event_type="llm_skipped_no_budget",
            level="warn",
            clip_id=candidate.id,
            payload={"cap": exc.cap_name, "attempted_total": exc.attempted_total},
            rationale="anthropic budget cap would be exceeded; heuristic score used",
        )
        return heuristic, False, False

    try:
        refined = await _llm_tiebreaker_score(candidate, heuristic)
        settle(reservation, actual_amount_usd=_LLM_COST_PER_TIEBREAKER_USD, status="succeeded")
        return max(0.0, min(1.0, refined)), True, True
    except NotImplementedError:
        settle(reservation, actual_amount_usd=0.0, status="failed")
        log(
            agent="curator",
            event_type="phase1_scaffold",
            level="info",
            clip_id=candidate.id,
            payload={"heuristic_score": heuristic},
            rationale="LLM tiebreaker stubbed; heuristic score retained",
        )
        return heuristic, True, False
    except (asyncio.TimeoutError, RetryGiveUp) as exc:
        # Codex 2026-05-18: transient LLM failures (provider 5xx wrapped
        # in RetryGiveUp, asyncio timeout, etc.) previously aborted the
        # entire Curator run, leaving every candidate stuck in
        # 'discovered'. Now: settle failed, log, fall back to heuristic.
        settle(reservation, actual_amount_usd=0.0, status="failed")
        log(
            agent="curator",
            event_type="llm_transient_failure",
            level="warn",
            clip_id=candidate.id,
            payload={
                "error": f"{exc.__class__.__name__}: {exc}",
                "heuristic_score": heuristic,
            },
            rationale="transient LLM failure; heuristic score retained, run continues",
        )
        return heuristic, True, False
    except Exception:
        settle(reservation, actual_amount_usd=0.0, status="failed")
        raise


# ---------- Atomic claim (E-4) ----------

def _select_discovered(batch_scan_limit: int) -> list[dict]:
    """Read up to `batch_scan_limit` discovered candidates. NO lock held
    during the read — scoring (which may call the LLM) happens outside
    any transaction. The atomic claim happens at promote-time via the
    conditional UPDATE in `_promote_with_cas` below.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM clips_candidate
             WHERE status = 'discovered'
             ORDER BY scouted_at ASC
             LIMIT ?
            """,
            (batch_scan_limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _promote_with_cas(
    promoted: list[tuple[str, float]],
) -> tuple[list[str], list[str]]:
    """Atomically flip the promoted clips from 'discovered' → 'curated'.

    Returns `(actually_promoted_ids, lost_to_race_ids)`. Codex 2026-05-18
    finding: the prior implementation returned only an aggregate count,
    so the caller's per-clip `clip_curated` log fired for ALL targets,
    even ones whose conditional UPDATE matched zero rows. Now the caller
    can log promoted events for the winners and a separate
    `clip_lost_to_race` event for the losers — the audit trail no
    longer claims promotions that didn't actually land.
    """
    if not promoted:
        return [], []
    actually_promoted: list[str] = []
    lost: list[str] = []
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for clip_id, score in promoted:
                cur = conn.execute(
                    """
                    UPDATE clips_candidate
                       SET status = 'curated',
                           virality_score = ?,
                           curated_at = datetime('now')
                     WHERE id = ? AND status = 'discovered'
                    """,
                    (score, clip_id),
                )
                if cur.rowcount == 1:
                    actually_promoted.append(clip_id)
                else:
                    lost.append(clip_id)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
    return actually_promoted, lost


# ---------- Helpers ----------

def _row_to_candidate(row: dict) -> CandidateClip:
    return CandidateClip(
        id=row["id"],
        creator=row["creator"],
        source_platform=row["source_platform"],
        source_url=row["source_url"],
        source_title=row.get("source_title"),
        source_duration_s=row.get("source_duration_s"),
        source_view_count=row.get("source_view_count"),
        virality_score=row.get("virality_score"),
        predicted_views=row.get("predicted_views"),
    )


# ---------- Public entry point ----------

def _resolve_llm_caps(
    *,
    line_item_cap_usd: float | None,
    daily_cap_usd: float | None,
    allow_uncapped: bool,
) -> tuple[float | None, float | None]:
    """Resolve LLM cost caps. If a cap is explicitly passed, use it.
    Otherwise load from `config/budget.yaml` (line_items.anthropic_api_buffer.
    monthly_budget_usd + per_call_caps.anthropic_daily_usd_max).

    Codex 2026-05-18 finding: previously the defaults were None, which
    meant "no enforcement" — a caller that forgot to pass caps would
    burn the Anthropic budget without guardrails. Now we look at the
    config; only `allow_uncapped=True` permits None caps.
    """
    if line_item_cap_usd is not None and daily_cap_usd is not None:
        return line_item_cap_usd, daily_cap_usd
    try:
        budget = load("budget")
        line_items = budget.get("line_items") or {}
        per_call = budget.get("per_call_caps") or {}
        cfg_monthly = (line_items.get("anthropic_api_buffer") or {}).get("monthly_budget_usd")
        cfg_daily = per_call.get("anthropic_daily_usd_max")
        resolved_monthly = line_item_cap_usd if line_item_cap_usd is not None else cfg_monthly
        resolved_daily = daily_cap_usd if daily_cap_usd is not None else cfg_daily
    except Exception:
        resolved_monthly = line_item_cap_usd
        resolved_daily = daily_cap_usd

    if not allow_uncapped:
        if resolved_monthly is None:
            raise ValueError(
                "run_curator: no anthropic_api_buffer monthly cap resolved from "
                "config/budget.yaml. Pass llm_line_item_cap_usd explicitly OR set "
                "allow_uncapped=True for test-only unrestricted mode."
            )
    return resolved_monthly, resolved_daily


async def run_curator(
    batch_size: int = 10,
    *,
    batch_scan_limit: int = 50,
    llm_line_item_cap_usd: float | None = None,
    llm_daily_cap_usd: float | None = None,
    allow_uncapped: bool = False,
) -> CuratorRunSummary:
    """Score discovered candidates; promote top `batch_size` to 'curated'.

    Parameters:
        batch_size: how many promotions to make this run
        batch_scan_limit: max discovered rows to consider per run
        llm_line_item_cap_usd: monthly Anthropic cap. None → load from
            config/budget.yaml line_items.anthropic_api_buffer.monthly_budget_usd.
            If config has no value AND caller didn't pass one, ValueError
            unless `allow_uncapped=True`.
        llm_daily_cap_usd: per-day cap. None → load from config (per_call_caps.
            anthropic_daily_usd_max). Optional in production; None is OK.
        allow_uncapped: test-only escape hatch. Production callers must
            either pass caps explicitly or accept the config defaults.

    Returns a CuratorRunSummary. Shape changed in Day 4 — callers that
    used the legacy list return need updating.
    """
    load("optimizer_bounds")  # surface config load errors early
    resolved_monthly, resolved_daily = _resolve_llm_caps(
        line_item_cap_usd=llm_line_item_cap_usd,
        daily_cap_usd=llm_daily_cap_usd,
        allow_uncapped=allow_uncapped,
    )

    summary = CuratorRunSummary(
        considered=0, promoted=0, skipped_no_budget=0,
        llm_calls_attempted=0, llm_calls_succeeded=0,
    )

    # Read discovered rows (no lock held during the read — scoring
    # below may call the LLM, which we never want to do under a write
    # lock). The atomic claim happens at promote-time via _promote_with_cas.
    rows = _select_discovered(batch_scan_limit)
    if not rows:
        log(agent="curator", event_type="run_complete",
            payload={"considered": 0, "promoted": 0},
            rationale="no candidates in 'discovered' state")
        return summary

    summary.considered = len(rows)
    scored: dict[str, float] = {}

    for row in rows:
        candidate = _row_to_candidate(row)
        heuristic = _heuristic_score(candidate.source_view_count, candidate.creator)
        final_score = heuristic

        if _is_borderline(heuristic):
            final_score, attempted, succeeded = await _refine_with_llm_or_fallback(
                candidate, heuristic,
                line_item_cap_usd=resolved_monthly,
                daily_cap_usd=resolved_daily,
            )
            if attempted:
                summary.llm_calls_attempted += 1
            if succeeded:
                summary.llm_calls_succeeded += 1
            else:
                # Either BudgetExceeded (no reservation) or NotImplementedError
                # (reservation settled failed). The "attempted" flag
                # distinguishes them; both fall back to heuristic.
                if not attempted:
                    summary.skipped_no_budget += 1

        scored[candidate.id] = final_score

    # Sort + select top N for promotion.
    ranked = sorted(scored.items(), key=lambda x: x[1], reverse=True)
    target = [(cid, score) for cid, score in ranked[:batch_size]]
    skipped = [(cid, score) for cid, score in ranked[batch_size:]]

    # E-4 atomic claim: conditional UPDATE serialized via BEGIN IMMEDIATE.
    # A concurrent Curator that raced us to any of these rows would have
    # already flipped them to 'curated'; our UPDATE silently no-ops on
    # those (cur.rowcount = 0). Codex 2026-05-18: return both
    # actually-promoted AND lost-to-race ids so per-clip event logs fire
    # only for the rows we actually claimed — no more audit divergence.
    promoted_ids, lost_ids = _promote_with_cas(target)
    summary.promoted = len(promoted_ids)

    if lost_ids:
        # Concurrent contention is expected under multi-worker setups —
        # info-level, not warn. Operators see it in the digest if needed
        # but it doesn't fire the alerts panel.
        log(
            agent="curator",
            event_type="concurrent_claim_lost",
            level="info",
            payload={
                "targeted": len(target),
                "actually_promoted": len(promoted_ids),
                "lost_to_race": lost_ids,
            },
            rationale=(
                "another Curator run claimed some of our targeted rows between "
                "our SELECT and our UPDATE. Lost rows are theirs; we keep the rest."
            ),
        )

    score_lookup = dict(target)
    for cid in promoted_ids:
        log(
            agent="curator",
            event_type="clip_curated",
            clip_id=cid,
            payload={"score": score_lookup[cid]},
            rationale=f"promoted with score {score_lookup[cid]:.3f}",
        )

    for cid, score in skipped:
        log(
            agent="curator",
            event_type="clip_skipped",
            level="debug",
            clip_id=cid,
            payload={"score": score},
            rationale="outside curated batch_size",
        )

    log(agent="curator", event_type="run_complete",
        payload={
            "considered": summary.considered,
            "promoted": summary.promoted,
            "llm_attempted": summary.llm_calls_attempted,
            "llm_succeeded": summary.llm_calls_succeeded,
            "skipped_no_budget": summary.skipped_no_budget,
        },
        rationale=(
            f"curated top {summary.promoted} of {summary.considered} "
            f"(LLM tiebreakers: {summary.llm_calls_attempted})"
        ))
    return summary
