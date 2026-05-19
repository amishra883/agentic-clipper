"""Day 14 validation pilot gate.

The pilot is the safety net that gates expansion to a multi-platform ramp.
Per `docs/phase2_plan.md:755`:

  Posts 30 clips to a single platform (Instagram Reels default — least
  claim-prone) and measures three gate criteria:

    1. Content ID claim rate  < 2%
    2. Revenue per view (RPV) > $0.001
    3. Operator time per day  < 45 min

  All three must PASS to unblock Day 15.

This module is the orchestration around what is fundamentally an
operator-driven exercise: the operator runs the pipeline daily, posts to
the chosen platform, records observed claims/revenue/time, and asks for
a verdict when the target clip count is reached.

Why not auto-ingest revenue? YouTube/Instagram/TikTok pay out weekly to
monthly via creator dashboards; there is no real-time revenue API at our
scale. Operator reads the dashboard once a day and runs `make
pilot-record-revenue AMOUNT=...`.

Why not auto-count claims? Content ID claims show up in
platform-specific notification streams that require OAuth + push
listeners we have not built. Operator records each one observed.

Operator time IS auto-tracked from the events table when the operator
runs `make pilot-record-time MINUTES=...` at end of day — but the
intention is for that to become an automatic rollup once we wire end-of-
day events. For Phase 2 Day 14 it is operator-entered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from agents.db import connect
from agents.events import log


# Defaults per docs/phase2_plan.md:755. Operator may override at start time.
DEFAULT_TARGET_CLIP_COUNT = 30
DEFAULT_CLAIM_THRESHOLD_PCT = 2.0
DEFAULT_RPV_THRESHOLD_USD = 0.001
DEFAULT_OPERATOR_MINUTES_THRESHOLD = 45
DEFAULT_TARGET_PLATFORM = "instagram_reels"


PilotStatus = Literal["active", "passed", "failed", "abandoned"]
PilotVerdict = Literal["pass", "fail", "inconclusive"]
RevenueSource = Literal["ad_rev", "affiliate", "creator_fund", "other"]


class PilotError(Exception):
    """Base for pilot-orchestration errors that callers should surface."""


class PilotAlreadyActive(PilotError):
    """`start_pilot` called while another pilot is still active. The
    operator must `finalize` or `abandon` the current pilot first."""


class NoActivePilot(PilotError):
    """An operation requiring an active pilot was attempted with none.
    Run `make pilot-start` to begin a new pilot."""


@dataclass
class PilotRun:
    id: int
    started_at: str
    ended_at: str | None
    target_platform: str
    target_clip_count: int
    claim_threshold_pct: float
    rpv_threshold_usd: float
    operator_minutes_threshold: int
    status: PilotStatus
    verdict_at: str | None
    failed_reasons_json: str | None
    notes: str | None


@dataclass
class PilotProgress:
    """Live counters for the active pilot. All values are observed from
    the DB, not the projected end state — verdict() turns these into a
    pass/fail decision."""
    pilot_run_id: int
    target_platform: str
    clips_posted: int
    target_clip_count: int
    days_elapsed: float
    claim_count: int
    claim_rate_pct: float  # claims / posted * 100 (0 if posted == 0)
    revenue_usd: float
    total_views: int
    rpv_usd: float        # revenue / views (0 if views == 0)
    operator_minutes_total: int
    operator_minutes_per_day: float  # total / days_elapsed (or total if <1 day)


@dataclass
class GateCriterion:
    name: str
    observed: float
    threshold: float
    direction: Literal["<", ">"]  # observed must be < or > threshold
    passed: bool
    detail: str


@dataclass
class GateResult:
    """Outcome of evaluating gate criteria against the live pilot."""
    verdict: PilotVerdict
    criteria: list[GateCriterion] = field(default_factory=list)
    rationale: str = ""


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------

def _ensure_schema() -> None:
    """Ensure pilot_runs + pilot_revenue exist. Migration 006 creates them,
    but tests may use a bare init_schema DB — be tolerant."""
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pilot_runs (
              id                          INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at                  TEXT NOT NULL DEFAULT (datetime('now')),
              ended_at                    TEXT,
              target_platform             TEXT NOT NULL
                CHECK (target_platform IN ('instagram_reels','youtube_shorts','tiktok')),
              target_clip_count           INTEGER NOT NULL CHECK (target_clip_count > 0),
              claim_threshold_pct         REAL NOT NULL CHECK (claim_threshold_pct >= 0),
              rpv_threshold_usd           REAL NOT NULL CHECK (rpv_threshold_usd >= 0),
              operator_minutes_threshold  INTEGER NOT NULL CHECK (operator_minutes_threshold > 0),
              status                      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','passed','failed','abandoned')),
              verdict_at                  TEXT,
              failed_reasons_json         TEXT,
              notes                       TEXT
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pilot_runs_active "
            "ON pilot_runs (status) WHERE status = 'active'"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pilot_revenue (
              id              INTEGER PRIMARY KEY AUTOINCREMENT,
              pilot_run_id    INTEGER NOT NULL REFERENCES pilot_runs(id) ON DELETE CASCADE,
              recorded_at     TEXT NOT NULL DEFAULT (datetime('now')),
              amount_usd      REAL NOT NULL CHECK (amount_usd >= 0),
              source          TEXT NOT NULL,
              detail          TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pilot_revenue_run "
            "ON pilot_revenue (pilot_run_id, recorded_at)"
        )


def _row_to_pilot(row) -> PilotRun:
    return PilotRun(
        id=row["id"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        target_platform=row["target_platform"],
        target_clip_count=row["target_clip_count"],
        claim_threshold_pct=row["claim_threshold_pct"],
        rpv_threshold_usd=row["rpv_threshold_usd"],
        operator_minutes_threshold=row["operator_minutes_threshold"],
        status=row["status"],
        verdict_at=row["verdict_at"],
        failed_reasons_json=row["failed_reasons_json"],
        notes=row["notes"],
    )


def start_pilot(
    *,
    target_platform: str = DEFAULT_TARGET_PLATFORM,
    target_clip_count: int = DEFAULT_TARGET_CLIP_COUNT,
    claim_threshold_pct: float = DEFAULT_CLAIM_THRESHOLD_PCT,
    rpv_threshold_usd: float = DEFAULT_RPV_THRESHOLD_USD,
    operator_minutes_threshold: int = DEFAULT_OPERATOR_MINUTES_THRESHOLD,
    notes: str | None = None,
) -> PilotRun:
    """Open a new pilot run. Refuses if another pilot is already active —
    operator must finalize or abandon first.

    The unique partial index `uq_pilot_runs_active` makes this race-safe:
    two concurrent `start_pilot` calls cannot both succeed."""
    _ensure_schema()
    with connect() as conn:
        existing = conn.execute(
            "SELECT id, started_at FROM pilot_runs WHERE status = 'active'"
        ).fetchone()
        if existing is not None:
            raise PilotAlreadyActive(
                f"pilot run #{existing['id']} is still active "
                f"(started {existing['started_at']}); "
                f"finalize or abandon it before starting another"
            )
        cur = conn.execute(
            """
            INSERT INTO pilot_runs
              (target_platform, target_clip_count, claim_threshold_pct,
               rpv_threshold_usd, operator_minutes_threshold, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                target_platform, target_clip_count, claim_threshold_pct,
                rpv_threshold_usd, operator_minutes_threshold, notes,
            ),
        )
        pilot_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM pilot_runs WHERE id = ?", (pilot_id,)
        ).fetchone()
    log(agent="pilot", event_type="pilot_started",
        payload={"pilot_run_id": pilot_id, "target_platform": target_platform,
                 "target_clip_count": target_clip_count,
                 "claim_threshold_pct": claim_threshold_pct,
                 "rpv_threshold_usd": rpv_threshold_usd,
                 "operator_minutes_threshold": operator_minutes_threshold},
        rationale=(f"opened pilot #{pilot_id}: post {target_clip_count} clips "
                   f"to {target_platform}; gate = "
                   f"claims<{claim_threshold_pct}% "
                   f"AND RPV>${rpv_threshold_usd} "
                   f"AND time<{operator_minutes_threshold}min/day"))
    return _row_to_pilot(row)


def current_pilot() -> PilotRun | None:
    """Return the currently active pilot, if any."""
    _ensure_schema()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM pilot_runs WHERE status = 'active' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return _row_to_pilot(row) if row else None


def get_pilot(pilot_run_id: int) -> PilotRun:
    """Look up a specific pilot run by id."""
    _ensure_schema()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM pilot_runs WHERE id = ?", (pilot_run_id,)
        ).fetchone()
    if row is None:
        raise PilotError(f"no pilot run with id {pilot_run_id}")
    return _row_to_pilot(row)


def _require_active() -> PilotRun:
    pilot = current_pilot()
    if pilot is None:
        raise NoActivePilot(
            "no active pilot; run `make pilot-start` to begin one"
        )
    return pilot


# ----------------------------------------------------------------------
# Operator data entry
# ----------------------------------------------------------------------

def record_revenue(
    *,
    amount_usd: float,
    source: RevenueSource,
    detail: str | None = None,
) -> int:
    """Append a revenue line item to the active pilot. Returns the row id.

    Revenue is scoped to the active pilot; if there is none, raises
    `NoActivePilot`. The operator reads the platform dashboard and
    records what they see — daily, weekly, whatever the cadence."""
    if amount_usd < 0:
        raise ValueError(f"amount_usd must be >= 0, got {amount_usd}")
    if source not in ("ad_rev", "affiliate", "creator_fund", "other"):
        raise ValueError(
            f"source must be ad_rev|affiliate|creator_fund|other, got {source!r}"
        )
    pilot = _require_active()
    _ensure_schema()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO pilot_revenue (pilot_run_id, amount_usd, source, detail)
            VALUES (?, ?, ?, ?)
            """,
            (pilot.id, amount_usd, source, detail),
        )
        row_id = cur.lastrowid
    log(agent="pilot", event_type="pilot_revenue_recorded",
        payload={"pilot_run_id": pilot.id, "amount_usd": amount_usd,
                 "source": source, "detail": detail},
        rationale=f"+${amount_usd:.4f} from {source}")
    return int(row_id)


def record_operator_time(
    *,
    minutes: int,
    note: str | None = None,
) -> None:
    """Operator records end-of-day minutes spent on the pipeline.
    Appends an event_type='operator_time' row scoped to the active pilot.

    The pilot reads these back at verdict time to compute minutes/day."""
    if minutes < 0:
        raise ValueError(f"minutes must be >= 0, got {minutes}")
    pilot = _require_active()
    log(agent="pilot", event_type="operator_time",
        payload={"pilot_run_id": pilot.id, "minutes": minutes,
                 "note": note},
        rationale=f"{minutes} minutes of operator attention on pilot #{pilot.id}")


def record_claim(
    *,
    clip_id: str | None,
    detail: str | None = None,
) -> None:
    """Operator records a Content ID / copyright claim they observed on
    a pilot-posted clip. Logged as event_type='pilot_claim' scoped to
    the active pilot.

    Note: this is distinct from a strike (policy violation that goes in
    the `strikes` table). A *claim* is a revenue redirect; a *strike*
    is account jeopardy. Both count toward the pilot's safety read but
    we only gate on claim rate here — operator should escalate strikes
    out of band (failover account, kill switch)."""
    pilot = _require_active()
    log(agent="pilot", event_type="pilot_claim", level="warn",
        clip_id=clip_id,
        payload={"pilot_run_id": pilot.id, "detail": detail},
        rationale=f"pilot #{pilot.id} observed a Content ID claim")


# ----------------------------------------------------------------------
# Progress + verdict
# ----------------------------------------------------------------------

def _clips_posted(pilot: PilotRun) -> int:
    """Count clips_ready rows posted to the pilot's target platform
    since the pilot started."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM clips_ready
             WHERE target_platform = ?
               AND status = 'posted'
               AND posted_at IS NOT NULL
               AND strftime('%s', posted_at) >= strftime('%s', ?)
            """,
            (pilot.target_platform, pilot.started_at),
        ).fetchone()
    return int(row["n"]) if row else 0


def _total_views(pilot: PilotRun) -> int:
    """Sum performance_metrics.views across clips published during the
    pilot. NULL views count as 0 (operator/Analyst hasn't pulled yet)."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(pm.views), 0) AS v
              FROM performance_metrics pm
              JOIN feature_records fr ON fr.id = pm.feature_record_id
             WHERE fr.target_platform = ?
               AND strftime('%s', fr.posted_at) >= strftime('%s', ?)
            """,
            (pilot.target_platform, pilot.started_at),
        ).fetchone()
    return int(row["v"]) if row else 0


def _claim_count(pilot: PilotRun) -> int:
    """Count pilot_claim events recorded against this pilot."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM events
             WHERE event_type = 'pilot_claim'
               AND strftime('%s', ts) >= strftime('%s', ?)
            """,
            (pilot.started_at,),
        ).fetchone()
    return int(row["n"]) if row else 0


def _revenue(pilot: PilotRun) -> float:
    """Sum pilot_revenue rows for this pilot."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) AS r FROM pilot_revenue "
            "WHERE pilot_run_id = ?",
            (pilot.id,),
        ).fetchone()
    return float(row["r"]) if row else 0.0


def _operator_minutes_total(pilot: PilotRun) -> int:
    """Sum operator-recorded minutes for this pilot. Each
    event_type='operator_time' carries a `minutes` field in payload_json."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT payload_json FROM events
             WHERE event_type = 'operator_time'
               AND strftime('%s', ts) >= strftime('%s', ?)
            """,
            (pilot.started_at,),
        ).fetchall()
    total = 0
    for row in rows:
        payload = row["payload_json"]
        if not payload:
            continue
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if data.get("pilot_run_id") != pilot.id:
            continue
        minutes = data.get("minutes")
        if isinstance(minutes, int):
            total += minutes
    return total


def _days_elapsed(pilot: PilotRun) -> float:
    """Fractional days since pilot started, never less than 1 (so
    minutes/day doesn't divide by zero on day 0)."""
    started = datetime.fromisoformat(
        pilot.started_at.replace(" ", "T")
        if "T" not in pilot.started_at else pilot.started_at
    )
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = (now - started).total_seconds() / 86_400.0
    return max(delta, 1.0)


def pilot_progress(pilot: PilotRun | None = None) -> PilotProgress:
    """Snapshot live counters. Operator runs `make pilot-status` and gets
    this — it's the daily "are we close to verdict" read."""
    pilot = pilot or _require_active()
    posted = _clips_posted(pilot)
    claims = _claim_count(pilot)
    revenue = _revenue(pilot)
    views = _total_views(pilot)
    minutes = _operator_minutes_total(pilot)
    days = _days_elapsed(pilot)
    return PilotProgress(
        pilot_run_id=pilot.id,
        target_platform=pilot.target_platform,
        clips_posted=posted,
        target_clip_count=pilot.target_clip_count,
        days_elapsed=days,
        claim_count=claims,
        claim_rate_pct=(100.0 * claims / posted) if posted > 0 else 0.0,
        revenue_usd=revenue,
        total_views=views,
        rpv_usd=(revenue / views) if views > 0 else 0.0,
        operator_minutes_total=minutes,
        operator_minutes_per_day=(minutes / days) if days > 0 else float(minutes),
    )


def evaluate_gate(pilot: PilotRun | None = None) -> GateResult:
    """Score the three gate criteria. Returns 'inconclusive' if the
    pilot has not yet hit its target clip count — the operator should
    keep posting until the sample is real.

    Three independent criteria:
      1. claim_rate_pct < threshold   (operator-reported claims)
      2. rpv_usd        > threshold   (operator-reported revenue / Analyst views)
      3. minutes/day    < threshold   (operator-reported minutes)

    The verdict is PASS only if all three pass AND the sample size has
    been hit. Otherwise FAIL (one+ criterion failed) or INCONCLUSIVE
    (sample too small to draw a conclusion)."""
    pilot = pilot or _require_active()
    p = pilot_progress(pilot)

    criteria = [
        GateCriterion(
            name="claim_rate",
            observed=p.claim_rate_pct,
            threshold=pilot.claim_threshold_pct,
            direction="<",
            passed=p.claim_rate_pct < pilot.claim_threshold_pct,
            detail=(f"{p.claim_count} claims on {p.clips_posted} clips "
                    f"= {p.claim_rate_pct:.2f}% "
                    f"(threshold <{pilot.claim_threshold_pct}%)"),
        ),
        GateCriterion(
            name="rpv",
            observed=p.rpv_usd,
            threshold=pilot.rpv_threshold_usd,
            direction=">",
            passed=p.rpv_usd > pilot.rpv_threshold_usd,
            detail=(f"${p.revenue_usd:.4f} on {p.total_views} views "
                    f"= ${p.rpv_usd:.6f}/view "
                    f"(threshold >${pilot.rpv_threshold_usd})"),
        ),
        GateCriterion(
            name="operator_time",
            observed=p.operator_minutes_per_day,
            threshold=float(pilot.operator_minutes_threshold),
            direction="<",
            passed=p.operator_minutes_per_day < pilot.operator_minutes_threshold,
            detail=(f"{p.operator_minutes_total}min over {p.days_elapsed:.1f}d "
                    f"= {p.operator_minutes_per_day:.1f}min/day "
                    f"(threshold <{pilot.operator_minutes_threshold}min/day)"),
        ),
    ]

    if p.clips_posted < pilot.target_clip_count:
        return GateResult(
            verdict="inconclusive",
            criteria=criteria,
            rationale=(
                f"only {p.clips_posted}/{pilot.target_clip_count} clips posted; "
                "verdict deferred until the full sample lands"
            ),
        )

    all_passed = all(c.passed for c in criteria)
    if all_passed:
        return GateResult(
            verdict="pass",
            criteria=criteria,
            rationale=(
                f"all 3 gates green at {p.clips_posted}/{pilot.target_clip_count}"
                " clips — Day 15 unblocked"
            ),
        )
    failed = [c.name for c in criteria if not c.passed]
    return GateResult(
        verdict="fail",
        criteria=criteria,
        rationale=(
            f"failed gate(s): {', '.join(failed)} — do NOT ramp to "
            "multi-platform; revisit the failing dimension(s) before retrying"
        ),
    )


def finalize_pilot(
    *,
    verdict: PilotVerdict | Literal["abandon"],
    notes: str | None = None,
) -> PilotRun:
    """Close the active pilot. Records the verdict + ended_at and frees
    the active-slot for the next pilot.

    Accepts 'pass', 'fail', 'inconclusive' (operator chooses to abandon
    a stalled pilot), or 'abandon' (explicit abort)."""
    pilot = _require_active()
    if verdict not in ("pass", "fail", "inconclusive", "abandon"):
        raise ValueError(f"verdict must be pass|fail|inconclusive|abandon, got {verdict!r}")
    status_map: dict[str, PilotStatus] = {
        "pass": "passed",
        "fail": "failed",
        "inconclusive": "abandoned",
        "abandon": "abandoned",
    }
    new_status = status_map[verdict]
    # Capture failed gate names for the audit trail when we fail.
    failed_reasons: list[str] | None = None
    if verdict == "fail":
        gate = evaluate_gate(pilot)
        failed_reasons = [c.name for c in gate.criteria if not c.passed]
    with connect() as conn:
        conn.execute(
            """
            UPDATE pilot_runs
               SET status = ?,
                   ended_at = datetime('now'),
                   verdict_at = datetime('now'),
                   failed_reasons_json = ?,
                   notes = COALESCE(?, notes)
             WHERE id = ?
            """,
            (
                new_status,
                json.dumps(failed_reasons) if failed_reasons else None,
                notes,
                pilot.id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM pilot_runs WHERE id = ?", (pilot.id,)
        ).fetchone()
    log(agent="pilot", event_type="pilot_finalized",
        level="info" if verdict == "pass" else "warn",
        payload={"pilot_run_id": pilot.id, "verdict": verdict,
                 "failed_reasons": failed_reasons, "notes": notes},
        rationale=f"pilot #{pilot.id} closed: {new_status}")
    return _row_to_pilot(row)


__all__ = [
    "DEFAULT_TARGET_CLIP_COUNT",
    "DEFAULT_CLAIM_THRESHOLD_PCT",
    "DEFAULT_RPV_THRESHOLD_USD",
    "DEFAULT_OPERATOR_MINUTES_THRESHOLD",
    "DEFAULT_TARGET_PLATFORM",
    "PilotError",
    "PilotAlreadyActive",
    "NoActivePilot",
    "PilotRun",
    "PilotProgress",
    "GateCriterion",
    "GateResult",
    "start_pilot",
    "current_pilot",
    "get_pilot",
    "record_revenue",
    "record_operator_time",
    "record_claim",
    "pilot_progress",
    "evaluate_gate",
    "finalize_pilot",
]
