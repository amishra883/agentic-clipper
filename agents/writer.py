"""Writer — generates commentary script + shot list for a clip.

Reads persona.yaml + creators.yaml + /data/trending.md and emits a Script
that satisfies the persona's humor profile (punch density, trending refs)
and substance requirements (>=1 substance tag). LLM generation is deferred
to Phase 2; the Phase 1 scaffold produces a structurally-valid placeholder.

Per CLAUDE.md "Commentary style guidelines" and "Architecture / Agent topology"
— Writer step 4 in the data flow.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents.config import load
from agents.db import connect
from agents.events import log
from agents.models import Script, ShotListEntry

REPO_ROOT = Path(__file__).resolve().parent.parent
TRENDING_PATH = REPO_ROOT / "data" / "trending.md"
QUARANTINE_DIR = REPO_ROOT / "data" / "quarantine"


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


# ---------- LLM stubs ----------

async def _llm_generate_script(
    creator: str,
    source_excerpt: str,
    persona: dict,
    trending_summary: str,
) -> Script:
    # TODO(phase2): call Claude (sub-agent / inside-session) with the
    # persona's humor profile + substance requirements + /data/trending.md.
    # Must enforce: hook in first 1.5s, 45-55s target, persona.do_not list,
    # >=1 substance tag, >=1 trending ref, punch_density >= floor.
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
            # Structured rendering: kind: value (description if present, source-tagged)
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


# ---------- Placeholder script (Phase 1 scaffold) ----------

def _placeholder_script(persona: dict) -> Script:
    """Structurally-valid Script that passes self-validation.

    The text and shot list are intentionally trivial — Phase 2 replaces this
    with an LLM call. The point in Phase 1 is to let the rest of the pipeline
    flow end-to-end.
    """
    runtime_s = 25.0
    # Enough beats to clear the persona's punch_density floor.
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


# ---------- Persistence ----------

def _persist_script(clip_id: str, script: Script) -> None:
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
            INSERT INTO clip_artifacts (clip_id, script_text, shot_list_json, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(clip_id) DO UPDATE SET
              script_text = excluded.script_text,
              shot_list_json = excluded.shot_list_json,
              updated_at = datetime('now')
            """,
            (clip_id, script.text, json.dumps(shot_list_payload)),
        )


# ---------- Public entry point ----------

async def run_writer(clip_id: str) -> Script:
    """Generate (or stub) a Script for a clip, validate, persist, return."""
    persona_cfg = load("persona")
    persona = _active_persona(persona_cfg)
    creators_cfg = load("creators")  # used to look up source_creator context

    if stale_check():
        log(
            agent="writer",
            event_type="trending_stale",
            level="warn",
            clip_id=clip_id,
            rationale=f"/data/trending.md older than {persona['humor_profile']['trending_freshness_max_age_hours']}h",
        )

    trending_summary = _load_trending()

    # Phase 1 — skip the LLM call and emit a placeholder.
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT creator FROM clips_candidate WHERE id = ?", (clip_id,)
            ).fetchone()
        creator = row["creator"] if row else "unknown"
        _ = creators_cfg  # noqa: keep reference; Phase 2 will use it
        script = await _llm_generate_script(creator, "", persona, trending_summary)
    except NotImplementedError:
        log(
            agent="writer",
            event_type="phase1_scaffold",
            clip_id=clip_id,
            rationale="LLM stubbed in Phase 1; using structurally-valid placeholder",
        )
        script = _placeholder_script(persona)

    violations = _validate_script(script, persona)

    # Soft violation — punch_density below floor triggers a Phase 2 rewrite
    # loop. The script is still persisted but flagged so the rewrite loop
    # (when wired) can replace it.
    if "punch_density_below_floor" in violations:
        log(
            agent="writer",
            event_type="self_rewrite_triggered",
            level="warn",
            clip_id=clip_id,
            payload={"punch_density": script.punch_density,
                     "floor": persona["humor_profile"]["punch_density_min"]},
            rationale="punch density below persona floor — rewrite required",
        )
        # TODO(phase2): loop into _llm_generate_script with a "denser" prompt.

    # Hard violations — substance/trending gaps and persona do_not hits make
    # the script non-shippable on fair-use grounds (substance keeps the clip
    # transformative; do_not entries are the defamation / harassment / "no
    # voice clone" guardrails from persona.yaml). Quarantine the clip rather
    # than persist a known-bad script and rely on downstream stages to
    # somehow recover. Compliance has no visibility into the script text so
    # it cannot catch these — Writer is the only chokepoint.
    SOFT_VIOLATIONS = {"punch_density_below_floor"}
    hard_violations = [v for v in violations if v not in SOFT_VIOLATIONS]
    if hard_violations:
        _quarantine_clip(clip_id, hard_violations)
        log(
            agent="writer",
            event_type="script_blocked",
            level="blocked",
            clip_id=clip_id,
            payload={"violations": hard_violations,
                     "soft_violations": [v for v in violations if v in SOFT_VIOLATIONS]},
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
            payload={"violations": violations},
            rationale="; ".join(violations),
        )

    _persist_script(clip_id, script)

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
        },
        rationale="script + shot list persisted",
    )
    return script
