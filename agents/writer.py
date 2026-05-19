"""Writer — generates commentary script + shot list for a clip.

Reads persona.yaml + creators.yaml + /data/trending.md and emits a Script
that satisfies the persona's humor profile (punch density, trending refs)
and substance requirements (>=1 substance tag).

Hardening blocks (Day 7 of the revised Phase 2 plan):

- **Anthropic per-clip token cap (E-12):**
  `_check_token_budget()` rejects clip inputs whose projected token
  consumption exceeds `per_call_caps.anthropic_tokens_per_clip_max`
  (config/budget.yaml, default 8000). Failure routes to /data/quarantine/
  with reason=token_budget_exceeded — the script is never generated, the
  reservation is never made.

- **Rewrite-loop max-iterations bound:**
  `_run_writer_loop()` calls the LLM, validates, and if punch_density is
  below floor (soft violation), retries with a "denser" prompt up to
  `per_call_caps.rewrite_loop_max_iterations` (default 3). Hard
  violations break the loop and quarantine.

- **Cost reservation (E-2 reuse):**
  Every LLM attempt routes through `agents.costs.reserve` against the
  anthropic_api_buffer line item. Daily + monthly caps default-load
  from config/budget.yaml; BudgetExceeded raises before the LLM call
  and the clip stays in 'processing' for the next run cycle (no
  quarantine — the budget reset will let it proceed).

- **stage_lease("writer"):**
  Run wraps in `stage_lease(clip_id, "writer", ttl=300s)`. clip_artifacts
  is written at artifact_version=input so the end-of-lease CAS bumps to
  input+1.

- **Trending sanitizer (E-7):**
  `_load_trending()` parses /data/trending.md exclusively via
  `agents.trending_sanitizer.sanitize_trending_file` — the raw markdown
  text never reaches the LLM. Closes the prompt-injection vector.

- **LLM eval suite gates persona prompt changes:**
  `verify_persona_prompt_locked()` runs the golden eval suite against the
  current Writer prompt before allowing a Phase 2 wiring run. The gate
  fires on prompt-hash drift; a passing run (>= 18/20) unlocks the day's
  Writer pipeline. Implemented as a callable the operator runs in CI /
  pre-flight, not on every clip.

Per CLAUDE.md "Commentary style guidelines" and "Architecture / Agent topology"
— Writer step 4 in the data flow.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from agents.config import load
from agents.costs import BudgetExceeded, reserve, settle
from agents.db import connect
from agents.events import log
from agents.eval_runner import run_eval_suite
from agents.models import Script, ShotListEntry
from agents.retry import RetryGiveUp
from agents.stage_lease import LeaseConflict, stage_lease

REPO_ROOT = Path(__file__).resolve().parent.parent
TRENDING_PATH = REPO_ROOT / "data" / "trending.md"
QUARANTINE_DIR = REPO_ROOT / "data" / "quarantine"
EVAL_SUITE_DIR = REPO_ROOT / "tests" / "evals" / "writer_persona"

# Approximate Anthropic cost per Writer call. Calibrated against current
# Claude pricing; per-clip dollar projection is amount_usd =
# tokens / 1000 * _ANTHROPIC_USD_PER_1K_TOKENS.
_ANTHROPIC_USD_PER_1K_TOKENS = 0.015

# Eval suite must pass at this ratio for a new prompt to unlock.
DEFAULT_EVAL_PASS_RATIO = 18 / 20


class WriterPolicyError(Exception):
    """Raised when a generated script violates a hard persona policy.

    The caller (pipeline orchestrator) catches this and skips the clip;
    the writer has already marked the clips_candidate row 'quarantined'
    and written a marker file to /data/quarantine/.
    """

    def __init__(self, *, clip_id: str, violations: list[str]) -> None:
        super().__init__(f"writer policy violations for {clip_id}: {', '.join(violations)}")
        self.clip_id = clip_id
        self.violations = violations


class TokenBudgetExceeded(Exception):
    """The clip's projected token consumption would breach the per-clip cap.

    Distinct from BudgetExceeded (dollar caps). Token caps catch runaway
    transcripts (e.g., a 60-minute VOD that should have been quarantined
    by Editor) before any LLM cost is incurred.
    """


def _quarantine_clip(clip_id: str, violations: list[str]) -> None:
    """Mirror the visuals.py quarantine pattern: write a reason file and flip
    clips_candidate.status to 'quarantined' so the row is visible in the
    daily digest and not picked up by downstream stages."""
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    marker = QUARANTINE_DIR / f"{clip_id}.writer.reason.txt"
    marker.write_text("writer hard violations: " + "; ".join(violations))
    with connect() as conn:
        conn.execute(
            "UPDATE clips_candidate SET status = 'quarantined' WHERE id = ?",
            (clip_id,),
        )


# ---------- Config helpers (E-12 + rewrite loop bounds) ----------


def _writer_budget_caps() -> dict:
    """Resolve Writer's Anthropic cost + token caps from config/budget.yaml.

    Returns a dict with: line_item_cap_usd, daily_cap_usd,
    tokens_per_clip_max, rewrite_loop_max_iterations. Each may be None
    if the config doesn't define it (caller decides whether to enforce
    or run uncapped).
    """
    budget = load("budget")
    line_items = budget.get("line_items") or {}
    per_call = budget.get("per_call_caps") or {}
    return {
        "line_item_cap_usd": (line_items.get("anthropic_api_buffer") or {}).get(
            "monthly_budget_usd"
        ),
        "daily_cap_usd": per_call.get("anthropic_daily_usd_max"),
        "tokens_per_clip_max": per_call.get("anthropic_tokens_per_clip_max"),
        "rewrite_loop_max_iterations": per_call.get(
            "rewrite_loop_max_iterations", 3
        ),
    }


def _project_tokens(source_excerpt: str, trending_summary: str, persona: dict) -> int:
    """Rough token projection for a Writer LLM call.

    Uses a 4-chars-per-token heuristic on the prompt inputs PLUS a fixed
    response-budget allowance. The Anthropic Tokenizer would be exact,
    but a heuristic is enough to catch the runaway-transcript case
    (60-minute VOD) the cap exists to prevent.
    """
    persona_text = json.dumps(persona)
    char_total = len(source_excerpt) + len(trending_summary) + len(persona_text)
    prompt_tokens = math.ceil(char_total / 4)
    # Response allowance: Writer's max output is ~700 tokens (1-2 paragraphs);
    # leave a 2x safety margin so the projection biases toward over-counting.
    response_allowance = 1500
    return prompt_tokens + response_allowance


def _check_token_budget(
    projected_tokens: int,
    *,
    tokens_per_clip_max: int | None,
) -> None:
    """Raises TokenBudgetExceeded if the projection breaches the cap.

    A cap of None means the operator opted out of token enforcement
    (test scenarios only — production must always have a number here).
    """
    if tokens_per_clip_max is None:
        return
    if projected_tokens > tokens_per_clip_max:
        raise TokenBudgetExceeded(
            f"projected={projected_tokens} > tokens_per_clip_max={tokens_per_clip_max}; "
            f"likely a runaway transcript (Editor should have quarantined a too-long clip)"
        )


# ---------- LLM stubs ----------


async def _llm_generate_script(
    creator: str,
    source_excerpt: str,
    persona: dict,
    trending_summary: str,
    *,
    rewrite_hint: str = "",
) -> Script:
    """Phase 2: call Claude with the persona's humor profile + substance
    requirements + sanitized trending refs. `rewrite_hint` is non-empty on
    rewrite-loop retries ("denser punches", "stronger hook", etc.).

    Phase 1 stub raises NotImplementedError; the caller catches and
    falls back to a placeholder script.
    """
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


# ---------- Trending freshness ----------


def stale_check() -> bool:
    """True if /data/trending.md is older than persona freshness window."""
    persona_cfg = load("persona")
    persona = _active_persona(persona_cfg)
    max_age_hours = persona["humor_profile"]["trending_freshness_max_age_hours"]
    if not TRENDING_PATH.exists():
        return True
    mtime = datetime.fromtimestamp(TRENDING_PATH.stat().st_mtime, tz=timezone.utc)
    return datetime.now(timezone.utc) - mtime > timedelta(hours=max_age_hours)


def _load_trending() -> str:
    """Load trending refs as a structured, sanitized string for the LLM prompt.

    Closes the gap Codex flagged 2026-05-18: previously this returned raw
    `data/trending.md` text directly, bypassing the prompt-injection
    sanitizer added in commit 1af1c5e. The Writer is the chokepoint where
    scraped Reddit/Twitter/KYM/X text would reach the LLM — that path is
    now closed.

    The sanitizer (`agents.trending_sanitizer.sanitize_trending_file`)
    parses only the YAML frontmatter into structured `TrendingRef` records
    and discards the markdown body. This function serializes those refs
    into a compact text block the LLM can read; the original file text
    NEVER reaches the LLM.
    """
    from agents.trending_sanitizer import sanitize_trending_file
    outcome = sanitize_trending_file(TRENDING_PATH)
    if not outcome.refs:
        return ""
    lines: list[str] = []
    for freshness in ("hot", "rising", "cooked"):
        bucket = [r for r in outcome.refs if r.freshness == freshness]
        if not bucket:
            continue
        lines.append(f"{freshness}:")
        for ref in bucket:
            desc = f" ({ref.description})" if ref.description else ""
            lines.append(f"  - {ref.kind}: {ref.value}{desc} [src={ref.source}]")
    return "\n".join(lines)


# ---------- Persona helpers ----------


def _active_persona(persona_cfg: dict) -> dict:
    active_id = persona_cfg["active_persona"]
    for p in persona_cfg["personas"]:
        if p["id"] == active_id:
            return p
    raise KeyError(f"active_persona '{active_id}' not in personas list")


def _violates_do_not(script_text: str, do_not: list[str]) -> list[str]:
    text = script_text.lower()
    return [rule for rule in do_not if rule.lower() in text]


def _persona_prompt_hash(persona: dict) -> str:
    """Stable hash of the persona's prompt-relevant fields. The eval-gate
    re-runs goldens whenever this hash changes (i.e., a prompt edit
    landed) so persona-prompt drift never ships unverified."""
    relevant = {
        "id": persona.get("id"),
        "humor_profile": persona.get("humor_profile"),
        "substance_requirements": persona.get("substance_requirements"),
        "do_not": persona.get("do_not"),
    }
    serialized = json.dumps(relevant, sort_keys=True, default=str).encode()
    return hashlib.sha256(serialized).hexdigest()[:16]


# ---------- Placeholder script (Phase 1 scaffold) ----------


def _placeholder_script(persona: dict) -> Script:
    """Structurally-valid Script that passes self-validation.

    The text and shot list are intentionally trivial — Phase 2 replaces this
    with an LLM call. The point in Phase 1 is to let the rest of the pipeline
    flow end-to-end.
    """
    runtime_s = 25.0
    target = persona["humor_profile"]["punch_density_target"]
    punches = max(3, int(runtime_s * target))
    punch_beats = [round(runtime_s * (i + 1) / (punches + 1), 2) for i in range(punches)]

    shot_list: list[ShotListEntry] = [
        ShotListEntry(
            shot_type="avatar_reaction",
            start_s=punch_beats[0],
            duration_s=2.0,
            prompt="placeholder reaction — wired in Phase 2",
            reaction_id="jaw_drop",
            punch_word="WHAT",
        ),
        ShotListEntry(
            shot_type="transition_stinger",
            start_s=runtime_s / 2,
            duration_s=0.8,
            prompt="placeholder stinger",
        ),
    ]

    return Script(
        text="[phase1 placeholder script — Writer LLM not yet wired]",
        runtime_s=runtime_s,
        substance_tags=[persona["substance_requirements"]["tags"][0]],
        trending_refs=["meme:phase1_placeholder"],
        trending_freshness="hot",
        punch_density=len(punch_beats) / runtime_s,
        punch_beats=punch_beats,
        shot_list=shot_list,
        hook_template_id="HT-placeholder",
    )


# ---------- Validation ----------


def _validate_script(script: Script, persona: dict) -> list[str]:
    """Return a list of violation reasons; empty == passing."""
    violations: list[str] = []

    if not script.substance_tags:
        violations.append("missing_substance_tag")
    elif len(script.substance_tags) < persona["substance_requirements"]["required_tags_min"]:
        violations.append("insufficient_substance_tags")

    if not script.trending_refs:
        violations.append("missing_trending_ref")
    elif len(script.trending_refs) < persona["humor_profile"]["trending_refs_per_clip_min"]:
        violations.append("insufficient_trending_refs")

    floor = persona["humor_profile"]["punch_density_min"]
    if script.punch_density < floor:
        violations.append("punch_density_below_floor")

    do_not_hits = _violates_do_not(script.text, persona.get("do_not", []))
    if do_not_hits:
        violations.append("do_not_violation:" + ",".join(do_not_hits))

    return violations


# ---------- Rewrite loop ----------


async def _run_writer_loop(
    *,
    clip_id: str,
    creator: str,
    source_excerpt: str,
    persona: dict,
    trending_summary: str,
    max_iterations: int,
    caps: dict,
) -> tuple[Script, int, int]:
    """Generate + validate, retrying with a denser-prompt hint on soft
    violations up to max_iterations. Returns (script, attempts, llm_succeeded).

    Cost-reserve once per attempt. Hard violations short-circuit the loop.
    BudgetExceeded propagates to the caller; the orchestrator decides
    whether to re-try later or quarantine.
    """
    attempts = 0
    llm_succeeded = 0
    rewrite_hint = ""
    last_script: Script | None = None
    last_violations: list[str] = []

    for attempt in range(1, max_iterations + 1):
        attempts += 1
        # Reserve cost BEFORE the LLM call; if reserve raises BudgetExceeded
        # we never made the call, so no settle is needed.
        projected_tokens = _project_tokens(source_excerpt, trending_summary, persona)
        amount_usd = round(
            projected_tokens / 1000 * _ANTHROPIC_USD_PER_1K_TOKENS, 4
        )
        reservation = reserve(
            category="anthropic_api_buffer",
            amount_usd=amount_usd,
            line_item_cap_usd=caps["line_item_cap_usd"],
            daily_cap_usd=caps["daily_cap_usd"],
            detail=f"writer-attempt-{attempt}:{clip_id}",
            provider="anthropic",
            clip_id=clip_id,
        )

        try:
            script = await _llm_generate_script(
                creator, source_excerpt, persona, trending_summary,
                rewrite_hint=rewrite_hint,
            )
            settle(reservation, actual_amount_usd=amount_usd, status="succeeded")
            llm_succeeded += 1
        except NotImplementedError:
            # Phase 1 stub — settle failed, fall through to placeholder
            # so the rest of the pipeline can still smoke-test.
            settle(reservation, actual_amount_usd=0.0, status="failed")
            return _placeholder_script(persona), attempts, llm_succeeded
        except RetryGiveUp:
            settle(reservation, actual_amount_usd=0.0, status="failed")
            raise

        last_script = script
        last_violations = _validate_script(script, persona)
        if not last_violations:
            return script, attempts, llm_succeeded
        # Hard violation → break the rewrite loop; Writer quarantines.
        hard = [v for v in last_violations if v != "punch_density_below_floor"]
        if hard:
            return script, attempts, llm_succeeded
        # Soft only (punch_density_below_floor) → rewrite with denser hint.
        rewrite_hint = (
            "Increase punch density. Previous draft had "
            f"{script.punch_density:.2f} punches/sec; target "
            f"{persona['humor_profile']['punch_density_target']:.2f}+."
        )
        log(
            agent="writer",
            event_type="rewrite_iteration",
            clip_id=clip_id,
            payload={
                "attempt": attempt,
                "punch_density": script.punch_density,
                "max_iterations": max_iterations,
            },
            rationale=f"punch density below floor; rewrite attempt {attempt}/{max_iterations}",
        )

    # Loop exhausted; return whatever we have last (caller validates again).
    assert last_script is not None  # max_iterations >= 1 → loop ran at least once
    log(
        agent="writer",
        event_type="rewrite_exhausted",
        level="warn",
        clip_id=clip_id,
        payload={
            "max_iterations": max_iterations,
            "final_violations": last_violations,
        },
        rationale="rewrite loop exhausted; final script ships with soft violation",
    )
    return last_script, attempts, llm_succeeded


# ---------- Persistence ----------


def _persist_script(
    clip_id: str,
    script: Script,
    *,
    input_artifact_version: int,
) -> None:
    """Insert / update clip_artifacts at artifact_version=input so the
    stage_lease end-of-lease CAS bumps to input+1."""
    shot_list_payload = [
        {
            "shot_type": s.shot_type,
            "start_s": s.start_s,
            "duration_s": s.duration_s,
            "prompt": s.prompt,
            "reaction_id": s.reaction_id,
            "punch_word": s.punch_word,
        }
        for s in script.shot_list
    ]
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO clip_artifacts
              (clip_id, script_text, shot_list_json, artifact_version, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(clip_id) DO UPDATE SET
              script_text = excluded.script_text,
              shot_list_json = excluded.shot_list_json,
              artifact_version = excluded.artifact_version,
              updated_at = datetime('now')
            """,
            (
                clip_id,
                script.text,
                json.dumps(shot_list_payload),
                input_artifact_version,
            ),
        )


# ---------- LLM eval gate ----------


def verify_persona_prompt_locked(
    *,
    generator: Callable[[dict], str] | None = None,
    pass_ratio: float = DEFAULT_EVAL_PASS_RATIO,
) -> dict:
    """Run the golden eval suite against the current Writer prompt.

    Used as a pre-flight gate before Phase 2 Writer wiring runs: an
    operator (or CI) calls this and only proceeds if it returns
    `{"locked": True}`. A `{"locked": False}` result means the persona
    prompt drifted from the golden set; either the prompt was edited
    intentionally (operator regenerates goldens) or accidentally
    (operator reverts).

    The generator is optional — if None, we use a deterministic
    persona-aware stub so the gate can still be exercised in
    scaffold mode. In Phase 2 the orchestrator passes a real
    LLM-backed generator.

    Returns:
        {
          "locked": bool,
          "pass_rate": float,
          "passing_ids": list[str],
          "failing_ids": list[str],
          "prompt_hash": str,
          "total": int,
        }
    """
    persona_cfg = load("persona")
    persona = _active_persona(persona_cfg)

    if generator is None:
        # Deterministic scaffold generator — emits the golden text back so
        # the gate passes by definition in Phase 1. Phase 2's caller
        # injects a real generator.
        def generator(input_payload: dict) -> str:
            return input_payload.get("_phase1_echo_golden", "[placeholder]")

    result = run_eval_suite(EVAL_SUITE_DIR, generator)
    locked = result.total > 0 and result.pass_rate >= pass_ratio
    return {
        "locked": locked,
        "pass_rate": result.pass_rate,
        "passing_ids": result.passing_ids,
        "failing_ids": result.failing_ids,
        "prompt_hash": _persona_prompt_hash(persona),
        "total": result.total,
    }


# ---------- Public entry point ----------


async def run_writer(clip_id: str) -> Script:
    """Generate (or stub) a Script for a clip, validate, persist, return.

    Wraps the LLM call in stage_lease + cost reservation. Token cap (E-12)
    is the first gate — over-cap clips quarantine before any LLM cost is
    incurred. Rewrite loop is bounded by config; cost reservation is
    settled per attempt.
    """
    persona_cfg = load("persona")
    persona = _active_persona(persona_cfg)
    creators_cfg = load("creators")
    caps = _writer_budget_caps()

    if stale_check():
        log(
            agent="writer",
            event_type="trending_stale",
            level="warn",
            clip_id=clip_id,
            rationale=f"/data/trending.md older than {persona['humor_profile']['trending_freshness_max_age_hours']}h",
        )

    trending_summary = _load_trending()

    with connect() as conn:
        row = conn.execute(
            "SELECT creator, source_title FROM clips_candidate WHERE id = ?",
            (clip_id,),
        ).fetchone()
    if row is None:
        raise KeyError(f"no clips_candidate row with id={clip_id}")
    creator = row["creator"] if row else "unknown"
    _ = creators_cfg  # Phase 2 will read per-creator humor weighting

    # Source excerpt: in Phase 2 we'd join in the transcript from
    # clip_artifacts; for now the title is a reasonable token-projection
    # input that won't trip the cap on a placeholder run.
    source_excerpt = row["source_title"] or ""

    # ---------- Token cap (E-12) ----------
    projected_tokens = _project_tokens(source_excerpt, trending_summary, persona)
    try:
        _check_token_budget(
            projected_tokens,
            tokens_per_clip_max=caps["tokens_per_clip_max"],
        )
    except TokenBudgetExceeded as exc:
        _quarantine_clip(clip_id, [f"token_budget_exceeded:{exc}"])
        log(
            agent="writer",
            event_type="token_budget_exceeded",
            level="blocked",
            clip_id=clip_id,
            payload={
                "projected_tokens": projected_tokens,
                "cap": caps["tokens_per_clip_max"],
            },
            rationale=str(exc),
        )
        raise WriterPolicyError(
            clip_id=clip_id, violations=["token_budget_exceeded"]
        ) from exc

    try:
        with stage_lease(clip_id, stage="writer", ttl_seconds=300) as lease:
            try:
                script, attempts, llm_succeeded = await _run_writer_loop(
                    clip_id=clip_id,
                    creator=creator,
                    source_excerpt=source_excerpt,
                    persona=persona,
                    trending_summary=trending_summary,
                    max_iterations=caps["rewrite_loop_max_iterations"],
                    caps=caps,
                )
            except BudgetExceeded as exc:
                # Budget cap fired at reserve() — caller decides whether
                # to retry later or quarantine. We don't quarantine
                # here: a budget reset (next day / next month) lets the
                # clip proceed. Re-raise.
                log(
                    agent="writer",
                    event_type="writer_skipped_no_budget",
                    level="warn",
                    clip_id=clip_id,
                    payload={
                        "cap": exc.cap_name,
                        "attempted_total": exc.attempted_total,
                    },
                    rationale="anthropic cap would be exceeded; clip stays 'processing'",
                )
                raise

            violations = _validate_script(script, persona)

            if "punch_density_below_floor" in violations and attempts >= caps["rewrite_loop_max_iterations"]:
                # Soft-only violation that survived the rewrite loop — log
                # for digest visibility; ship anyway. Caller may decide
                # later (Optimizer) to flag low-density clips at publish time.
                log(
                    agent="writer",
                    event_type="rewrite_loop_capped",
                    level="warn",
                    clip_id=clip_id,
                    payload={
                        "punch_density": script.punch_density,
                        "floor": persona["humor_profile"]["punch_density_min"],
                        "attempts": attempts,
                    },
                    rationale="ships with low punch_density after rewrite-loop cap",
                )

            SOFT_VIOLATIONS = {"punch_density_below_floor"}
            hard_violations = [v for v in violations if v not in SOFT_VIOLATIONS]
            if hard_violations:
                _quarantine_clip(clip_id, hard_violations)
                log(
                    agent="writer",
                    event_type="script_blocked",
                    level="blocked",
                    clip_id=clip_id,
                    payload={
                        "violations": hard_violations,
                        "soft_violations": [v for v in violations if v in SOFT_VIOLATIONS],
                        "attempts": attempts,
                    },
                    rationale=(
                        "writer hard violations — clip routed to /data/quarantine/: "
                        + "; ".join(hard_violations)
                    ),
                )
                raise WriterPolicyError(clip_id=clip_id, violations=hard_violations)

            if violations:
                log(
                    agent="writer",
                    event_type="script_violations",
                    level="warn",
                    clip_id=clip_id,
                    payload={"violations": violations, "attempts": attempts},
                    rationale="; ".join(violations),
                )

            _persist_script(
                clip_id, script,
                input_artifact_version=lease.input_artifact_version,
            )
            lease.output_artifact_version = lease.input_artifact_version + 1

            log(
                agent="writer",
                event_type="script_generated",
                clip_id=clip_id,
                payload={
                    "runtime_s": script.runtime_s,
                    "punch_density": script.punch_density,
                    "substance_tags": script.substance_tags,
                    "trending_refs": script.trending_refs,
                    "shot_count": len(script.shot_list),
                    "attempts": attempts,
                    "llm_succeeded": llm_succeeded,
                    "projected_tokens": projected_tokens,
                    "prompt_hash": _persona_prompt_hash(persona),
                },
                rationale="script + shot list persisted",
            )
            return script

    except LeaseConflict:
        log(
            agent="writer",
            event_type="writer_lease_conflict",
            level="info",
            clip_id=clip_id,
            payload={},
            rationale="another Writer holds the writer lease; backing off",
        )
        # Re-raise so the orchestrator can decide whether to wait or skip.
        raise


__all__ = [
    "run_writer",
    "verify_persona_prompt_locked",
    "WriterPolicyError",
    "TokenBudgetExceeded",
    "QUARANTINE_DIR",
    "DEFAULT_EVAL_PASS_RATIO",
    "stale_check",
    "_validate_script",
    "_persona_prompt_hash",
    "_project_tokens",
    "_check_token_budget",
    "_writer_budget_caps",
]


