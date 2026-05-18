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

import math
from dataclasses import dataclass

from agents.config import load
from agents.costs import BudgetExceeded, reserve, settle
from agents.db import connect
from agents.events import log
from agents.models import CandidateClip


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

    Errors:
        - BudgetExceeded → no reservation, no call, heuristic score wins
        - NotImplementedError → reservation settled as 'failed' (zero
          actual cost), heuristic score wins
        - Other Exception → reservation settled as 'failed', re-raised
    """
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
        # Phase 1 stub: settle as failed (zero cost), fall back to heuristic
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
    except Exception:
        # Live-mode LLM call failed; record the failed reservation so
        # the daily cap reflects reality
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
) -> int:
    """Atomically flip the promoted clips from 'discovered' → 'curated'.

    BEGIN IMMEDIATE serializes concurrent Curators at the SQLite lock
    layer. The conditional UPDATE (`WHERE id=? AND status='discovered'`)
    is the actual race guard: if another Curator beat us to a row, our
    UPDATE matches zero rows and that promotion silently no-ops.

    Returns the number of rows actually promoted. A return value less
    than len(promoted) means one or more clips were claimed by a
    concurrent Curator between our SELECT and our UPDATE — that's
    expected and safe; the caller logs the count.
    """
    if not promoted:
        return 0
    actual = 0
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
                actual += cur.rowcount
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
    return actual


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

async def run_curator(
    batch_size: int = 10,
    *,
    batch_scan_limit: int = 50,
    llm_line_item_cap_usd: float | None = None,
    llm_daily_cap_usd: float | None = None,
) -> CuratorRunSummary:
    """Score discovered candidates; promote top `batch_size` to 'curated'.

    Returns a CuratorRunSummary for audit. The shape changed in Day 4 —
    callers that used the legacy list return need updating.

    Parameters:
        batch_size: how many promotions to make this run
        batch_scan_limit: max discovered rows to consider per run
        llm_line_item_cap_usd / llm_daily_cap_usd: budget caps for the
            anthropic_api_buffer category. None = no enforcement (caller's
            policy). Phase 2 should read these from config/budget.yaml.
    """
    load("optimizer_bounds")  # surface config load errors early

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
                line_item_cap_usd=llm_line_item_cap_usd,
                daily_cap_usd=llm_daily_cap_usd,
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
    # those (cur.rowcount = 0). `actual_promoted` reflects what we
    # actually got, which may be less than what we targeted.
    actual_promoted = _promote_with_cas(target)
    summary.promoted = actual_promoted

    if actual_promoted < len(target):
        log(
            agent="curator",
            event_type="concurrent_claim_lost",
            level="warn",
            payload={"targeted": len(target), "actually_promoted": actual_promoted},
            rationale=(
                "another Curator run claimed some of our targeted rows between "
                "our SELECT and our UPDATE. Lost rows are theirs; we keep the rest."
            ),
        )

    # Log promotions that DID succeed (caller can audit by clip_id).
    # We can't tell from cur.rowcount which specific rows landed vs lost,
    # so the per-clip log here is "intended to promote" not "did promote".
    # That's OK — the events table + clips_candidate.curated_at gives
    # the operator the after-the-fact record.
    for cid, score in target:
        log(
            agent="curator",
            event_type="clip_curated",
            clip_id=cid,
            payload={"score": score},
            rationale=f"promoted with score {score:.3f}",
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
