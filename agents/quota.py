"""Per-platform per-day quota tracking for Publisher uploads.

Each platform has hard caps on uploads-per-day that are independent of
our cost budget:

- **YouTube Shorts (Data API v3):** 10,000 quota units/day. videos.insert
  costs ~1600 units, so the practical ceiling is ~6 uploads/day per
  project (shared across all accounts using the same OAuth project).
- **TikTok Content Posting API:** in pre-audit mode the API allows 6
  posts/24h per account. Post-audit (Spark Ads / DUET allowlist) it's
  ~30/24h.
- **Instagram Graph API:** 25 posts/24h per IG Business account, hard.

This module:
  - Records every successful and failed upload attempt against the
    `publishing_quota` table.
  - Exposes `check_quota_or_block()` which the Publisher calls BEFORE
    dispatching a platform upload. Refuses the dispatch (returns False)
    when the rolling 24h count would breach the cap.
  - Surfaces remaining-quota for the doctor / morning digest.

Quotas are tracked per-(platform, account_id). Two accounts on the same
platform have independent quotas — that's the failover-account pattern
documented in docs/backup_warming.md.

Per-day caps default-load from config/posting_schedule.yaml; operator can
override per-account in that file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from agents.config import load
from agents.db import connect
from agents.events import log


# Default daily quotas per platform. Override via config/posting_schedule.yaml
# `quotas.{platform}.daily_max` for stricter limits during account-warming.
DEFAULT_DAILY_QUOTAS = {
    "youtube_shorts": 6,        # Data API ceiling at ~1600 units/post
    "tiktok": 6,                # pre-audit cap
    "instagram_reels": 25,      # Graph API IG Business cap
}


class QuotaExceeded(Exception):
    """Daily quota for (platform, account) would be exceeded. Caller
    defers the post to the next day (or routes to a backup account
    that still has quota)."""

    def __init__(self, *, platform: str, account_id: str,
                 used_24h: int, daily_max: int) -> None:
        super().__init__(
            f"{platform}/{account_id}: {used_24h}/{daily_max} posts in last 24h"
        )
        self.platform = platform
        self.account_id = account_id
        self.used_24h = used_24h
        self.daily_max = daily_max


@dataclass
class QuotaUsage:
    platform: str
    account_id: str
    used_24h: int
    daily_max: int
    remaining: int


def _daily_max(platform: str, account_id: str) -> int:
    """Resolve the daily cap for (platform, account). Operator overrides
    in posting_schedule.yaml beat the defaults."""
    try:
        cfg = load("posting_schedule")
    except FileNotFoundError:
        return DEFAULT_DAILY_QUOTAS.get(platform, 0)
    quotas = (cfg.get("quotas") or {}).get(platform) or {}
    per_account = (quotas.get("per_account") or {}).get(account_id)
    if per_account is not None:
        return int(per_account)
    daily_max = quotas.get("daily_max")
    if daily_max is not None:
        return int(daily_max)
    return DEFAULT_DAILY_QUOTAS.get(platform, 0)


def _ensure_quota_schema() -> None:
    """Create the publishing_quota table if migrations haven't yet."""
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS publishing_quota (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              platform    TEXT NOT NULL,
              account_id  TEXT NOT NULL,
              ts          TEXT NOT NULL DEFAULT (datetime('now')),
              status      TEXT NOT NULL CHECK (status IN ('attempted','succeeded','failed')),
              clip_id     TEXT,
              platform_post_id TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_publishing_quota_acct_ts "
            "ON publishing_quota (platform, account_id, ts)"
        )


def used_in_last_24h(platform: str, account_id: str) -> int:
    """Count uploads in the rolling 24h window. One upload = one row
    (status is updated in place, never appended).

    Codex 2026-05-18 P1#1: the prior query did lexical `ts >= datetime(...)`
    which compared an ISO `'2026-05-17T12:00:00+00:00'` against a SQLite
    `'2026-05-17 13:00:00'` — `T` > space lexically, so the 12pm row
    counted as fresh against the 1pm threshold (it isn't). Switched to
    `strftime('%s', ts)` Unix-epoch compare, which parses BOTH formats
    correctly.

    Codex 2026-05-18 P1#3: prior code appended a separate `succeeded`
    row after the `attempted` row, so each upload counted twice. Now
    `resolve_attempt()` UPDATEs the same row in place; one row per
    upload regardless of outcome.
    """
    _ensure_quota_schema()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS used FROM publishing_quota
             WHERE platform = ? AND account_id = ?
               AND strftime('%s', ts) >= strftime('%s', 'now', '-24 hours')
               AND status IN ('attempted','succeeded','failed')
            """,
            (platform, account_id),
        ).fetchone()
    return int(row["used"]) if row else 0


def current_usage(platform: str, account_id: str) -> QuotaUsage:
    """Snapshot used + remaining quota for (platform, account). Used by
    the doctor / morning digest to show capacity before dispatch
    decisions."""
    used = used_in_last_24h(platform, account_id)
    cap = _daily_max(platform, account_id)
    return QuotaUsage(
        platform=platform,
        account_id=account_id,
        used_24h=used,
        daily_max=cap,
        remaining=max(0, cap - used),
    )


def check_quota_or_block(
    platform: str,
    account_id: str,
    *,
    clip_id: str | None = None,
) -> None:
    """Pre-dispatch gate. Raises QuotaExceeded if (platform, account)
    has used >= daily_max in the last 24h.

    Caller (Publisher) catches and either:
      - Defers the post to the next cycle (clip stays 'pending')
      - Routes to a backup account on the same platform
      - Surfaces in the digest if no backup has quota
    """
    usage = current_usage(platform, account_id)
    if usage.used_24h >= usage.daily_max:
        log(agent="publisher", event_type="quota_exceeded",
            level="warn", clip_id=clip_id,
            payload={"platform": platform, "account_id": account_id,
                     "used_24h": usage.used_24h, "daily_max": usage.daily_max},
            rationale=(
                f"{platform}/{account_id} quota at "
                f"{usage.used_24h}/{usage.daily_max}; deferring post"
            ))
        raise QuotaExceeded(
            platform=platform, account_id=account_id,
            used_24h=usage.used_24h, daily_max=usage.daily_max,
        )


def start_attempt(
    platform: str,
    account_id: str,
    *,
    clip_id: str | None,
) -> int:
    """Insert a single 'attempted' row in publishing_quota and return its
    rowid. The Publisher calls this BEFORE the upload so a mid-upload
    crash still consumes quota (the platform side already saw the
    request). After the upload resolves, `resolve_attempt()` UPDATEs
    this same row to 'succeeded' or 'failed' — one row per upload
    regardless of outcome.

    Codex 2026-05-18 P1#3: prior `record_attempt('attempted')` +
    `record_attempt('succeeded')` appended TWO rows. Cap of 6 was
    exhausted at 3 real posts. Now upload = single in-place row.
    """
    _ensure_quota_schema()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO publishing_quota
              (platform, account_id, ts, status, clip_id, platform_post_id)
            VALUES (?, ?, ?, 'attempted', ?, NULL)
            """,
            (
                platform, account_id,
                datetime.now(timezone.utc).isoformat(),
                clip_id,
            ),
        )
        return int(cur.lastrowid)


def resolve_attempt(
    attempt_id: int,
    *,
    status: str,
    platform_post_id: str | None = None,
) -> None:
    """UPDATE the 'attempted' row to 'succeeded' or 'failed' in place.
    Idempotent: if the row was already resolved by a prior call (e.g.,
    retry path), the second UPDATE is a no-op (WHERE status='attempted'
    filters it out)."""
    if status not in ("succeeded", "failed"):
        raise ValueError(
            f"resolve_attempt: status must be 'succeeded' or 'failed', "
            f"got {status!r}"
        )
    _ensure_quota_schema()
    with connect() as conn:
        conn.execute(
            """
            UPDATE publishing_quota
               SET status = ?, platform_post_id = ?
             WHERE id = ? AND status = 'attempted'
            """,
            (status, platform_post_id, attempt_id),
        )


# Back-compat shim. Tests that pre-date the split-API still use the
# single-call form; production code (publisher.py) uses start_attempt +
# resolve_attempt directly so the row-per-upload invariant holds.
def record_attempt(
    platform: str,
    account_id: str,
    *,
    clip_id: str | None,
    status: str,
    platform_post_id: str | None = None,
) -> None:
    """Compat shim — appends one row at the requested status. Used by
    tests that simulate prior rows directly; production callers must
    use start_attempt + resolve_attempt for the row-per-upload
    invariant."""
    _ensure_quota_schema()
    if status not in ("attempted", "succeeded", "failed"):
        raise ValueError(f"invalid status {status!r}")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO publishing_quota
              (platform, account_id, ts, status, clip_id, platform_post_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                platform, account_id,
                datetime.now(timezone.utc).isoformat(),
                status, clip_id, platform_post_id,
            ),
        )


__all__ = [
    "check_quota_or_block",
    "current_usage",
    "start_attempt",
    "resolve_attempt",
    "record_attempt",
    "used_in_last_24h",
    "QuotaExceeded",
    "QuotaUsage",
    "DEFAULT_DAILY_QUOTAS",
]
