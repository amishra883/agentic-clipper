"""Cost reservation layer — race-free budget enforcement.

Closes Eng review E-2. Today (pre-Phase-2) costs are post-hoc: the agent
makes a paid call, then writes a row. The MTD budget check runs BEFORE the
call and reads SUM of historical rows. Two concurrent workers can both pass
the check (both see SUM=$48 / $50 cap) and both spend, ending at $50+each
call.

The fix is reservation-then-settle:

    reservation_id = reserve(category="seedance_fast", amount_usd=0.50)
    try:
        actual_cost, output = call_paid_api(...)
        settle(reservation_id, actual_amount_usd=actual_cost, status="succeeded")
    except Exception:
        settle(reservation_id, actual_amount_usd=0.0, status="failed")
        raise

Concurrent workers see the reservation (status='pending', summed into
MTD) and bail. Failure zeroes the row's amount so the cap reflects
reality post-mortem.

Calling pattern (typical)
-------------------------
    from agents.costs import reserve, settle, BudgetExceeded

    try:
        rid = reserve(
            category="seedance_fast",
            amount_usd=shot.estimated_cost,
            line_item_cap_usd=budget_cfg.line_items["seedance_fast"]["monthly_budget_usd"],
            daily_cap_usd=budget_cfg.per_call_caps.get("seedance_daily_usd_max"),
            detail=f"clip={clip_id} shot={shot.shot_type}",
            provider="atlas_cloud",
            clip_id=clip_id,
        )
    except BudgetExceeded as exc:
        log_quarantine(clip_id, str(exc))
        raise

    try:
        actual = await atlas_cloud_call(...)
        settle(rid, actual_amount_usd=actual["cost_usd"], status="succeeded")
    except Exception as exc:
        settle(rid, actual_amount_usd=0.0, status="failed")
        raise

`reserve` raises BudgetExceeded if any of the cap parameters is supplied
AND already-pending-plus-succeeded MTD would exceed it. The caller handles
quarantine routing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from agents.db import connect

# Operator timezone for daily / monthly budget boundaries. The operator's
# mental model is "midnight ET to midnight ET", not "midnight UTC to midnight
# UTC". config/posting_schedule.yaml hard-codes America/New_York as the
# operator default; we use the same here. If a future operator wants a
# different tz the value flows in from config (Day 3+ wires that).
_OPERATOR_TZ = ZoneInfo("America/New_York")


def _local_now() -> datetime:
    return datetime.now(_OPERATOR_TZ)


def _local_day_start_utc(local_now: datetime | None = None) -> str:
    """Return today's local-midnight as a UTC ISO string suitable for
    SQLite comparison via strftime('%s', ...). Used as the daily cap
    boundary."""
    local_now = local_now or _local_now()
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).isoformat(sep=" ", timespec="seconds")


def _local_month_start_utc(local_now: datetime | None = None) -> str:
    """Return the start of the local month as a UTC ISO string."""
    local_now = local_now or _local_now()
    local_month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return local_month_start.astimezone(timezone.utc).isoformat(sep=" ", timespec="seconds")


class BudgetExceeded(Exception):
    """Raised when reserve() would push MTD past a cap.

    `cap_name` tells the caller which cap fired (monthly line-item vs
    daily); `attempted_total` is the MTD+amount that triggered. Useful
    for structured logging."""

    def __init__(self, cap_name: str, attempted_total: float, cap: float, mtd_pending: float, mtd_succeeded: float):
        self.cap_name = cap_name
        self.attempted_total = attempted_total
        self.cap = cap
        self.mtd_pending = mtd_pending
        self.mtd_succeeded = mtd_succeeded
        super().__init__(
            f"{cap_name} cap exceeded: MTD pending=${mtd_pending:.2f} + "
            f"succeeded=${mtd_succeeded:.2f} + new=${attempted_total - mtd_pending - mtd_succeeded:.2f} "
            f"= ${attempted_total:.2f} > ${cap:.2f}"
        )


CostStatus = Literal["pending", "succeeded", "failed"]


@dataclass
class MtdBreakdown:
    pending: float
    succeeded: float
    failed: float


def mtd_breakdown(category: str) -> MtdBreakdown:
    """Sum month-to-date costs by status for a single category.

    "Month-to-date" means since the start of THIS calendar month in the
    operator's timezone (America/New_York), converted to UTC for the
    `ts` comparison. Codex finding 2026-05-18: using SQLite's bare
    strftime('%Y-%m', 'now') compares UTC months — an ET-evening expense
    on the 31st would count toward "next month" because UTC has already
    rolled over.

    Failed rows are excluded from cap math elsewhere but reported here
    for audit visibility. Pending + succeeded is the relevant total.
    """
    month_start = _local_month_start_utc()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT status, COALESCE(SUM(amount_usd), 0) AS total
              FROM costs
             WHERE category = ?
               AND strftime('%s', ts) >= strftime('%s', ?)
             GROUP BY status
            """,
            (category, month_start),
        ).fetchall()
    out = MtdBreakdown(pending=0.0, succeeded=0.0, failed=0.0)
    for row in rows:
        if row["status"] == "pending":
            out.pending = float(row["total"])
        elif row["status"] == "succeeded":
            out.succeeded = float(row["total"])
        elif row["status"] == "failed":
            out.failed = float(row["total"])
    return out


def _today_total(conn, category: str) -> float:
    """Pending + succeeded since the start of today in OPERATOR time
    (America/New_York), converted to UTC for the ts compare. The previous
    implementation used SQLite's date('now') (UTC) — daily cap would fire
    at 8pm ET (00:00 UTC) instead of midnight ET, surprising the operator
    by 4-5 hours every day."""
    day_start = _local_day_start_utc()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount_usd), 0) AS total
          FROM costs
         WHERE category = ?
           AND status IN ('pending', 'succeeded')
           AND strftime('%s', ts) >= strftime('%s', ?)
        """,
        (category, day_start),
    ).fetchone()
    return float(row["total"]) if row else 0.0


def reserve(
    *,
    category: str,
    amount_usd: float,
    line_item_cap_usd: float | None = None,
    daily_cap_usd: float | None = None,
    detail: str = "",
    provider: str | None = None,
    clip_id: str | None = None,
) -> str:
    """Atomically write a pending costs row after asserting both caps.

    Returns a reservation_id the caller passes to `settle`. The check
    and the insert run in one BEGIN IMMEDIATE so concurrent workers
    can't both pass an edge-of-cap check.

    Raises BudgetExceeded if either cap is supplied and would be breached.
    A cap of None means "don't enforce this cap" (caller's policy).
    """
    if amount_usd < 0:
        raise ValueError(f"amount_usd must be >= 0; got {amount_usd}")

    reservation_id = uuid.uuid4().hex

    month_start = _local_month_start_utc()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # MTD pending + succeeded — operator-tz month, not UTC month
            row = conn.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN status='pending' THEN amount_usd ELSE 0 END), 0) AS pending,
                       COALESCE(SUM(CASE WHEN status='succeeded' THEN amount_usd ELSE 0 END), 0) AS succeeded
                  FROM costs
                 WHERE category = ?
                   AND strftime('%s', ts) >= strftime('%s', ?)
                """,
                (category, month_start),
            ).fetchone()
            mtd_pending = float(row["pending"])
            mtd_succeeded = float(row["succeeded"])
            mtd_total_new = mtd_pending + mtd_succeeded + amount_usd

            if line_item_cap_usd is not None and mtd_total_new > line_item_cap_usd:
                conn.execute("ROLLBACK")
                raise BudgetExceeded(
                    cap_name=f"{category}_monthly",
                    attempted_total=mtd_total_new,
                    cap=line_item_cap_usd,
                    mtd_pending=mtd_pending,
                    mtd_succeeded=mtd_succeeded,
                )

            if daily_cap_usd is not None:
                today_total = _today_total(conn, category)
                today_new = today_total + amount_usd
                if today_new > daily_cap_usd:
                    conn.execute("ROLLBACK")
                    raise BudgetExceeded(
                        cap_name=f"{category}_daily",
                        attempted_total=today_new,
                        cap=daily_cap_usd,
                        mtd_pending=mtd_pending,
                        mtd_succeeded=mtd_succeeded,
                    )

            # All caps pass; write the reservation
            conn.execute(
                """
                INSERT INTO costs
                  (ts, category, amount_usd, clip_id, provider, detail,
                   status, reservation_id)
                VALUES (datetime('now'), ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (category, amount_usd, clip_id, provider, detail, reservation_id),
            )
            conn.execute("COMMIT")
        except BudgetExceeded:
            raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    return reservation_id


def settle(
    reservation_id: str,
    *,
    actual_amount_usd: float,
    status: CostStatus = "succeeded",
) -> None:
    """Finalize a reservation. `status='succeeded'` records actual cost;
    `status='failed'` zeroes the amount so the cap reflects reality.

    Idempotent: settling a non-pending row is a silent no-op (caller may
    have crashed and retried; we don't want to double-count).
    """
    if status not in ("succeeded", "failed"):
        raise ValueError(f"status must be 'succeeded' or 'failed'; got {status!r}")
    if actual_amount_usd < 0:
        raise ValueError(f"actual_amount_usd must be >= 0; got {actual_amount_usd}")
    amount_to_record = actual_amount_usd if status == "succeeded" else 0.0

    with connect() as conn:
        conn.execute(
            """
            UPDATE costs
               SET amount_usd = ?,
                   status = ?,
                   settled_at = datetime('now')
             WHERE reservation_id = ?
               AND status = 'pending'
            """,
            (amount_to_record, status, reservation_id),
        )
