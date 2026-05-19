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
    """Count uploads (attempted OR succeeded) in the rolling 24h window.
    Failed attempts also count — a 4xx-class rejection still consumed
    quota on the platform side."""
    _ensure_quota_schema()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS used FROM publishing_quota
             WHERE platform = ? AND account_id = ?
               AND ts >= datetime('now', '-24 hours')
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


def record_attempt(
    platform: str,
    account_id: str,
    *,
    clip_id: str | None,
    status: str,
    platform_post_id: str | None = None,
) -> None:
    """Append a row to publishing_quota. status is one of:
    'attempted' (recorded BEFORE the upload call so a crash mid-upload
    still consumes quota), 'succeeded' (post landed), 'failed' (4xx /
    5xx after retries).

    The 'attempted' row is upgraded to 'succeeded' or 'failed' by the
    caller AFTER the upload resolves — but the attempted row itself is
    NEVER removed. A crashed upload still consumed quota on the
    platform side; we must reflect that locally."""
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
    "record_attempt",
    "used_in_last_24h",
    "QuotaExceeded",
    "QuotaUsage",
    "DEFAULT_DAILY_QUOTAS",
]
