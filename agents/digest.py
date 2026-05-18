"""Daily digest — the single operator surface.

Phase 2 Day 2 DX blocker (DX-1, DX-2). The spec references "daily digest"
11+ times without ever specifying schema. This module IS the spec: seven
sections in fixed order, queried from the live DB.

Sections (the agreed schema):

  1. ALERTS              — kill switch, strikes, budget >85%, stale trending,
                           OAuth token expiring
  2. MANUAL QUEUE         — TikTok clips waiting in data/clips/output/manual_upload/
  3. YESTERDAY'S PUBLISH  — N posted / N quarantined / N failed
  4. PERFORMANCE          — top + worst clips since last digest, anomalies
  5. BUDGET               — MTD spend per line item + projected month-end
  6. AUTO-CHANGES         — Optimizer changes since last digest
  7. WHAT NEEDS YOU       — max 3 items requiring operator action today

Most sections will return placeholder/empty data in Phase 2 Day 2
because the upstream stages (Scout, Curator, Editor, etc.) aren't
wired yet to populate the underlying tables. The structural skeleton
ships today; sections light up as upstream stages produce data.

Calling pattern
---------------
    from agents.digest import build_digest

    digest = build_digest()
    print(digest.render_text())            # for `make morning` stdout
    Path("data/digest/2026-05-18.md").write_text(digest.render_markdown())
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from agents.db import connect

REPO_ROOT = Path(__file__).resolve().parent.parent
MANUAL_UPLOAD_ROOT = REPO_ROOT / "data" / "clips" / "output" / "manual_upload"


AlertSeverity = Literal["red", "amber", "green"]


@dataclass
class Alert:
    severity: AlertSeverity
    title: str
    detail: str


@dataclass
class ManualQueueItem:
    clip_id: str
    platform: str
    scheduled_for: str | None
    drop_path: str


@dataclass
class YesterdayStats:
    posted: int = 0
    quarantined: int = 0
    failed: int = 0
    manual_pending: int = 0


@dataclass
class BudgetLine:
    category: str
    pending_usd: float
    succeeded_usd: float
    monthly_cap_usd: float | None


@dataclass
class AutoChange:
    change_type: str
    rationale: str
    applied_at: str
    rolled_back: bool


@dataclass
class Action:
    """One thing the operator should do TODAY. Max 3 in 'WHAT NEEDS YOU'."""
    priority: int  # 1=most important
    title: str
    detail: str
    command: str | None = None  # exact CLI invocation, if applicable


@dataclass
class Digest:
    generated_at: str
    alerts: list[Alert] = field(default_factory=list)
    manual_queue: list[ManualQueueItem] = field(default_factory=list)
    yesterday: YesterdayStats = field(default_factory=YesterdayStats)
    budget: list[BudgetLine] = field(default_factory=list)
    auto_changes: list[AutoChange] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)

    def render_text(self) -> str:
        """Plain-text digest for stdout. The "make morning" entry point
        wraps this with an interactive prompt; the bare text is what
        `make digest` writes."""
        lines = [f"=== agentic-clipper · {self.generated_at} ===", ""]

        # 1. ALERTS
        lines.append("ALERTS")
        if not self.alerts:
            lines.append("  (none)")
        else:
            for a in self.alerts:
                marker = {"red": "🔴", "amber": "🟡", "green": "🟢"}[a.severity]
                lines.append(f"  {marker} {a.title}: {a.detail}")
        lines.append("")

        # 2. WHAT NEEDS YOU (promoted near the top — the operator's
        # action items shouldn't be buried under stats)
        lines.append(f"WHAT NEEDS YOU TODAY ({len(self.actions)} items)")
        if not self.actions:
            lines.append("  (nothing requires manual attention)")
        else:
            for a in sorted(self.actions, key=lambda x: x.priority):
                lines.append(f"  {a.priority}. {a.title}")
                lines.append(f"     {a.detail}")
                if a.command:
                    lines.append(f"     $ {a.command}")
        lines.append("")

        # 3. MANUAL QUEUE
        lines.append(f"MANUAL TIKTOK QUEUE ({len(self.manual_queue)} pending)")
        for item in self.manual_queue:
            sched = item.scheduled_for or "unscheduled"
            lines.append(f"  • {item.clip_id} [{item.platform}] — slot {sched}")
            lines.append(f"    {item.drop_path}")
        if not self.manual_queue:
            lines.append("  (empty)")
        lines.append("")

        # 4. YESTERDAY
        y = self.yesterday
        lines.append("YESTERDAY")
        lines.append(
            f"  {y.posted} posted · {y.quarantined} quarantined · "
            f"{y.failed} failed · {y.manual_pending} pending"
        )
        lines.append("")

        # 5. BUDGET
        lines.append("BUDGET (month-to-date)")
        if not self.budget:
            lines.append("  (no spend recorded yet)")
        else:
            for b in self.budget:
                cap_str = f"/ ${b.monthly_cap_usd:.2f}" if b.monthly_cap_usd is not None else ""
                total = b.pending_usd + b.succeeded_usd
                pct = (total / b.monthly_cap_usd * 100) if b.monthly_cap_usd else 0
                pending_note = f" (incl. ${b.pending_usd:.2f} pending)" if b.pending_usd > 0 else ""
                lines.append(f"  {b.category}: ${total:.2f}{cap_str} ({pct:.0f}%){pending_note}")
        lines.append("")

        # 6. AUTO-CHANGES
        lines.append("AUTO-CHANGES APPLIED OVERNIGHT")
        if not self.auto_changes:
            lines.append("  (none)")
        else:
            for c in self.auto_changes:
                marker = "[rolled back]" if c.rolled_back else "[active]"
                lines.append(f"  {marker} {c.change_type} @ {c.applied_at}")
                lines.append(f"    {c.rationale}")
        lines.append("")

        return "\n".join(lines)


# ---------- Section builders ----------

def _build_alerts(conn: sqlite3.Connection) -> list[Alert]:
    """Walk the well-known alert sources. Each source returns an Alert or
    None; we collect the non-None ones."""
    alerts: list[Alert] = []

    # Unresolved strikes — zero-strikes posture per CLAUDE.md hard constraint
    row = conn.execute(
        "SELECT COUNT(*) FROM strikes WHERE resolved = 0"
    ).fetchone()
    unresolved_strikes = int(row[0])
    if unresolved_strikes > 0:
        alerts.append(Alert(
            severity="red",
            title=f"{unresolved_strikes} unresolved strike(s)",
            detail="Review accounts, dispute frivolous claims, failover if struck on primary.",
        ))

    # Budget burn — read the budget cap from config/budget.yaml, MTD from costs
    try:
        import yaml
        with (REPO_ROOT / "config" / "budget.yaml").open() as fh:
            budget_cfg = yaml.safe_load(fh)
        monthly_cap = float(budget_cfg.get("monthly_cap_usd", 0))
        hard_kill = float(budget_cfg.get("hard_kill_switch_usd", monthly_cap))
        mtd_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_usd), 0)
              FROM costs
             WHERE status IN ('pending', 'succeeded')
               AND strftime('%Y-%m', ts) = strftime('%Y-%m', 'now')
            """
        ).fetchone()
        spend = float(mtd_row[0])
        if spend >= hard_kill:
            alerts.append(Alert(
                severity="red",
                title="hard kill switch breached",
                detail=f"${spend:.2f} >= ${hard_kill:.2f}; pause paid features now.",
            ))
        elif spend >= monthly_cap:
            alerts.append(Alert(
                severity="red",
                title="monthly cap breached",
                detail=f"${spend:.2f} / ${monthly_cap:.2f}; Optimizer should be auto-pausing.",
            ))
        elif monthly_cap > 0 and spend / monthly_cap >= 0.85:
            alerts.append(Alert(
                severity="amber",
                title="approaching monthly cap",
                detail=f"${spend:.2f} / ${monthly_cap:.2f} ({spend / monthly_cap * 100:.0f}%).",
            ))
    except Exception:
        # If config is unreadable, surface that as a red alert — operator
        # needs to know their cap math is broken.
        alerts.append(Alert(
            severity="red",
            title="budget config unreadable",
            detail="config/budget.yaml could not be parsed; budget enforcement is degraded.",
        ))

    # Trending freshness — if trending.md hasn't been refreshed in >48h,
    # the Writer will block per persona.yaml's freshness rule.
    trending_path = REPO_ROOT / "data" / "trending.md"
    if trending_path.exists():
        from datetime import datetime as dt
        mtime = dt.fromtimestamp(trending_path.stat().st_mtime, tz=timezone.utc)
        age_hours = (dt.now(timezone.utc) - mtime).total_seconds() / 3600
        if age_hours > 48:
            alerts.append(Alert(
                severity="amber",
                title="data/trending.md is stale",
                detail=f"{age_hours:.0f}h old; Writer blocks at >48h. Run `make trending`.",
            ))
    else:
        alerts.append(Alert(
            severity="amber",
            title="data/trending.md missing",
            detail="Writer cannot generate scripts without trending refs. Run `make trending`.",
        ))

    return alerts


def _build_manual_queue(conn: sqlite3.Connection) -> list[ManualQueueItem]:
    """Pending TikTok (and other manual-mode) clips waiting on the
    operator's native-app upload."""
    rows = conn.execute(
        """
        SELECT clip_id, target_platform, scheduled_for
          FROM clips_ready
         WHERE status = 'manual_pending'
         ORDER BY scheduled_for ASC
        """
    ).fetchall()
    items: list[ManualQueueItem] = []
    for row in rows:
        clip_dir = MANUAL_UPLOAD_ROOT / row["target_platform"] / row["clip_id"]
        items.append(ManualQueueItem(
            clip_id=row["clip_id"],
            platform=row["target_platform"],
            scheduled_for=row["scheduled_for"],
            drop_path=str(clip_dir),
        ))
    return items


def _build_yesterday(conn: sqlite3.Connection) -> YesterdayStats:
    """Count clips_ready transitions and clips_candidate.status='quarantined'
    in the last 24h."""
    stats = YesterdayStats()
    # Posted: rows with posted_at in last 24h
    row = conn.execute(
        """
        SELECT COUNT(*) FROM clips_ready
         WHERE status = 'posted'
           AND posted_at IS NOT NULL
           AND strftime('%s', posted_at) > strftime('%s', 'now', '-1 day')
        """
    ).fetchone()
    stats.posted = int(row[0])
    # Quarantined: candidates flipped to quarantined recently (no timestamp
    # column on clips_candidate; approximate via events table)
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT clip_id) FROM events
         WHERE level = 'blocked'
           AND strftime('%s', ts) > strftime('%s', 'now', '-1 day')
        """
    ).fetchone()
    stats.quarantined = int(row[0])
    row = conn.execute(
        """
        SELECT COUNT(*) FROM clips_ready
         WHERE status = 'failed'
           AND strftime('%s', COALESCE(posted_at, scheduled_for)) > strftime('%s', 'now', '-1 day')
        """
    ).fetchone()
    stats.failed = int(row[0])
    row = conn.execute(
        "SELECT COUNT(*) FROM clips_ready WHERE status = 'manual_pending'"
    ).fetchone()
    stats.manual_pending = int(row[0])
    return stats


def _build_budget(conn: sqlite3.Connection) -> list[BudgetLine]:
    """MTD per-category breakdown, joined with the line-item caps from
    config/budget.yaml. Empty list if config is unreadable (alert
    section already surfaced that)."""
    try:
        import yaml
        with (REPO_ROOT / "config" / "budget.yaml").open() as fh:
            budget_cfg = yaml.safe_load(fh)
        line_items = budget_cfg.get("line_items") or {}
    except Exception:
        line_items = {}

    rows = conn.execute(
        """
        SELECT category,
               COALESCE(SUM(CASE WHEN status='pending' THEN amount_usd ELSE 0 END), 0) AS pending,
               COALESCE(SUM(CASE WHEN status='succeeded' THEN amount_usd ELSE 0 END), 0) AS succeeded
          FROM costs
         WHERE strftime('%Y-%m', ts) = strftime('%Y-%m', 'now')
         GROUP BY category
         ORDER BY pending + succeeded DESC
        """
    ).fetchall()

    out: list[BudgetLine] = []
    for row in rows:
        cap = line_items.get(row["category"], {}).get("monthly_budget_usd")
        out.append(BudgetLine(
            category=row["category"],
            pending_usd=float(row["pending"]),
            succeeded_usd=float(row["succeeded"]),
            monthly_cap_usd=float(cap) if cap is not None else None,
        ))
    return out


def _build_auto_changes(conn: sqlite3.Connection) -> list[AutoChange]:
    """Optimizer auto-changes since the last digest. The digest doesn't
    persist its own "last run at" — we just look at the last 24h."""
    rows = conn.execute(
        """
        SELECT change_type, rationale, applied_at, rolled_back
          FROM auto_changes
         WHERE strftime('%s', applied_at) > strftime('%s', 'now', '-1 day')
         ORDER BY applied_at DESC
        """
    ).fetchall()
    return [
        AutoChange(
            change_type=row["change_type"],
            rationale=row["rationale"],
            applied_at=row["applied_at"],
            rolled_back=bool(row["rolled_back"]),
        )
        for row in rows
    ]


_ACTION_DISPLAY_CAP = 3


def _build_actions(
    alerts: list[Alert],
    manual_queue: list[ManualQueueItem],
) -> list[Action]:
    """Top 3 things the operator should do today. Promotion rules:
      - Red alerts always make the action list, ordered first
      - Manual queue is a single rolled-up action
      - Amber alerts fill remaining slots
      - If >3 reds exist, the cap shows the first 3 PLUS a "N more red
        alerts" spillover action so the operator knows to scroll

    Codex finding 2026-05-18: the prior `actions[:3]` truncation could
    discard red alerts beyond #3. Red alerts must never disappear from
    the operator's view; we now show them all in the ALERTS section
    (the truncation only affects the WHAT NEEDS YOU summary), AND we
    surface a spillover marker if reds exceed the display cap.
    """
    reds = [Action(priority=1, title=a.title, detail=a.detail)
            for a in alerts if a.severity == "red"]
    queue_action = None
    if manual_queue:
        platforms = sorted({m.platform for m in manual_queue})
        queue_action = Action(
            priority=2,
            title=f"Post {len(manual_queue)} clip(s) manually",
            detail=f"Platforms: {', '.join(platforms)}. Drop dirs ready.",
            command="make tiktok-flow",
        )
    ambers = [Action(priority=3, title=a.title, detail=a.detail)
              for a in alerts if a.severity == "amber"]

    # If reds alone exceed the cap, show the first (cap-1) reds PLUS a
    # spillover action — operator sees N total reds existed.
    if len(reds) >= _ACTION_DISPLAY_CAP:
        keep = reds[:_ACTION_DISPLAY_CAP - 1]
        overflow = len(reds) - (_ACTION_DISPLAY_CAP - 1)
        spillover = Action(
            priority=1,
            title=f"{overflow} more red alert(s) above — review ALERTS section",
            detail="Action list capped; scroll up for the full ALERTS list.",
        )
        return keep + [spillover]

    # Normal case: reds (≤cap-1) + queue + ambers, truncated to cap
    out: list[Action] = list(reds)
    if queue_action is not None and len(out) < _ACTION_DISPLAY_CAP:
        out.append(queue_action)
    for a in ambers:
        if len(out) >= _ACTION_DISPLAY_CAP:
            break
        out.append(a)
    return out[:_ACTION_DISPLAY_CAP]


# ---------- Public entrypoint ----------

def build_digest() -> Digest:
    """Build the full digest by querying every section. Returns a Digest
    dataclass the caller can render to text/markdown/JSON.

    Stages the queries in one connection for consistency — the snapshot
    sees the same DB state across all sections."""
    now = datetime.now(timezone.utc).isoformat(sep=" ", timespec="seconds")
    digest = Digest(generated_at=now)

    with connect() as conn:
        digest.alerts = _build_alerts(conn)
        digest.manual_queue = _build_manual_queue(conn)
        digest.yesterday = _build_yesterday(conn)
        digest.budget = _build_budget(conn)
        digest.auto_changes = _build_auto_changes(conn)

    digest.actions = _build_actions(digest.alerts, digest.manual_queue)
    return digest


# ---------- CLI entry points ----------

def _print_digest() -> int:
    digest = build_digest()
    print(digest.render_text())
    return 0


def _print_morning() -> int:
    """`make morning` interactive entry point. Phase 1 just renders the
    digest; Phase 2 adds the per-action keybindings (start tiktok flow,
    review quarantine, undo auto-change) per the magical-moment spec in
    docs/phase2_plan.md."""
    digest = build_digest()
    print(digest.render_text())
    if digest.actions:
        print("Next: ", end="")
        for a in digest.actions:
            if a.command:
                print(f"`{a.command}`", end=" ")
        print()
    else:
        print("Nothing to act on. Cup of coffee.")
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "morning":
        sys.exit(_print_morning())
    sys.exit(_print_digest())
