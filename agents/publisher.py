"""Publisher — flushes the publish queue, respecting the posting schedule.

Reads `clips_ready` where status='queued' and scheduled_for <= now. Routes
each row according to its target platform's `mode` in posting_schedule.yaml:

  - mode='api'    → live upload via the platform's official API
                    (YouTube Data API v3, Instagram Graph API)
  - mode='manual' → write {video.mp4, caption.txt, hashtags.txt} into the
                    platform's `manual_drop_directory` and set
                    clips_ready.status = 'manual_pending'. Operator uploads
                    via the native app, then runs:
                      make tiktok-confirm CLIP_ID=<id> POST_ID=<id>
                    which flips the row to 'posted' and records platform_post_id.
                    Operator-decided 2026-05-14 for TikTok.

Builds the description from a template that includes:
  - source creator name (attribution — fair-use Factor 1)
  - "commentary" / "reaction" marker (transformative-purpose disclosure)
  - "AI-generated visuals" disclosure if any generated assets were used
  - "#ad" disclosure if affiliate links are present

Live API integrations are deferred to Phase 2; the manual-mode drop path
is also a Phase 2 stub (it currently sits as NotImplementedError below).

Per CLAUDE.md "Architecture / Agent topology" — Publisher step 8.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from agents.config import load
from agents.db import connect
from agents.events import log

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------- Platform-upload stubs ----------

async def _upload_youtube_shorts(*, video_path: str, title: str, description: str,
                                 hashtags: list[str], account_id: str) -> str:
    # TODO(phase2): wire YouTube Data API v3 videos.insert (resumable upload).
    # Needs: OAuth refresh token per account_id; title must include "#Shorts"
    # per posting_schedule.yaml.reformatting.youtube_shorts.title_must_contain.
    # Quota: 1600 units/upload; daily cap 10000 (cap to 6 uploads/day).
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


async def _upload_tiktok(*, video_path: str, title: str, description: str,
                         hashtags: list[str], account_id: str) -> str:
    # TODO(phase2): wire TikTok Content Posting API (SELF_ONLY pre-audit mode).
    # Fallback: Playwright against TikTok Studio Desktop if API access is denied
    # (posting_schedule.yaml.quota_guards.tiktok.fallback).
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


async def _upload_instagram_reels(*, video_path: str, title: str, description: str,
                                  hashtags: list[str], account_id: str) -> str:
    # TODO(phase2): wire Instagram Graph API two-step (create media container,
    # then publish). Needs: long-lived page token; account_id mapped to IG
    # business account id.
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


_DISPATCH = {
    "youtube_shorts": _upload_youtube_shorts,
    "tiktok": _upload_tiktok,
    "instagram_reels": _upload_instagram_reels,
}


# ---------- Description template ----------

def build_description(
    *,
    creator: str,
    visuals_used: bool,
    affiliate_present: bool,
    user_description: str = "",
) -> str:
    """Compose the final description. Compliance verifies the markers below."""
    parts: list[str] = []
    parts.append(f"Commentary on {creator}.")             # attribution + transformative marker
    if visuals_used:
        parts.append("Includes AI-generated visuals.")    # AI-content label
    if affiliate_present:
        parts.append("#ad")                               # FTC disclosure
    if user_description:
        parts.append(user_description)
    return "\n".join(parts)


# ---------- Queue helpers ----------

def _pick_next_clip() -> dict | None:
    """Atomically claim the next clip due to post.

    Three guarantees vs the prior implementation:

    1. **Atomic claim.** Wraps the SELECT + UPDATE in BEGIN IMMEDIATE so a
       concurrent Publisher process blocks (via busy_timeout) instead of
       double-claiming the same row.

    2. **Compliance JOIN.** Only returns clips whose LATEST `compliance_results`
       row for that `clip_id` has `passed = 1`. A clip with no compliance row,
       or whose most recent row failed, is never picked. The Compliance gate
       is the sole legal defense per docs/fair_use_position.md; the queue
       must not bypass it.

    3. **Timezone-safe scheduled_for compare.** Posting schedule times live
       in America/New_York with explicit ISO 8601 offsets (e.g.
       "2026-05-17T07:00:00-04:00"); SQLite's `strftime('%s', ...)` converts
       both sides to UTC epoch seconds so DST boundaries don't fire posts
       early or late.
    """
    with connect() as conn:
        # BEGIN IMMEDIATE grabs the write lock now; concurrent publishers will
        # wait on busy_timeout (set in db.connect()) rather than racing.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT cr.* FROM clips_ready cr
             WHERE cr.status = 'queued'
               AND strftime('%s', cr.scheduled_for) <= strftime('%s', 'now')
               AND EXISTS (
                 SELECT 1 FROM compliance_results c
                  WHERE c.clip_id = cr.clip_id
                    AND c.passed = 1
                    AND c.checked_at = (
                      SELECT MAX(checked_at) FROM compliance_results
                       WHERE clip_id = c.clip_id
                    )
               )
             ORDER BY cr.scheduled_for ASC
             LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        # Conditional UPDATE: only claim if still queued. Belt-and-suspenders
        # since BEGIN IMMEDIATE already prevents the race, but cheap defense
        # against a stray UPDATE in the future.
        cur = conn.execute(
            "UPDATE clips_ready SET status = 'posting' "
            "WHERE id = ? AND status = 'queued'",
            (row["id"],),
        )
        if cur.rowcount != 1:
            return None
    return dict(row)


def _mark_posted(row_id: int, platform_post_id: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE clips_ready
               SET status = 'posted',
                   platform_post_id = ?,
                   posted_at = datetime('now')
             WHERE id = ?
            """,
            (platform_post_id, row_id),
        )


def _mark_failed(row_id: int, reason: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE clips_ready
               SET status = 'failed',
                   failure_reason = ?,
                   retry_count = retry_count + 1
             WHERE id = ?
            """,
            (reason, row_id),
        )


def _mark_manual_pending(row_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE clips_ready SET status = 'manual_pending' WHERE id = ?",
            (row_id,),
        )


def _write_manual_drop(
    *,
    drop_root: str,
    clip_id: str,
    video_path: str,
    description: str,
    hashtags: list[str],
) -> Path:
    """Stage a clip for manual upload.

    Writes the final video (when it exists), description, and hashtag list
    into `drop_root/<clip_id>/`. The operator picks them up via the platform's
    native app, then runs `make tiktok-confirm` to flip the row to 'posted'.

    Returns the per-clip drop directory.
    """
    root = (REPO_ROOT / drop_root) if not Path(drop_root).is_absolute() else Path(drop_root)
    clip_dir = root / clip_id
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "caption.txt").write_text(description)
    (clip_dir / "hashtags.txt").write_text("\n".join(hashtags) + ("\n" if hashtags else ""))
    src = Path(video_path) if video_path else None
    if src and src.is_file():
        # Preserve the source extension; Phase 1 compositor writes .mp4 paths
        # but Phase 2 may emit alternatives (.mov, .webm).
        dest = clip_dir / f"video{src.suffix or '.mp4'}"
        shutil.copy2(src, dest)
    return clip_dir


def _artifact_for(clip_id: str) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM clip_artifacts WHERE clip_id = ?", (clip_id,)
        ).fetchone()
    return dict(row) if row else {}


def _creator_for(clip_id: str) -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT creator FROM clips_candidate WHERE id = ?", (clip_id,)
        ).fetchone()
    return row["creator"] if row else "unknown"


# ---------- Public entry point ----------

async def run_publisher() -> None:
    """Flush the ready queue until empty or until the next clip's scheduled_for
    is in the future. Each iteration posts at most one clip.
    """
    schedule = load("posting_schedule")
    platforms_cfg = schedule.get("platforms") or {}

    while True:
        clip = _pick_next_clip()
        if clip is None:
            log(agent="publisher", event_type="queue_empty",
                rationale="no clips due to post (or none have a passing latest compliance result)")
            return

        platform = clip["target_platform"]
        clip_id = clip["clip_id"]
        artifact = _artifact_for(clip_id)
        creator = _creator_for(clip_id)
        hashtags = json.loads(clip.get("hashtags_json") or "[]")
        visuals_used = bool(artifact.get("visuals_seconds_used"))
        # Affiliate detection is a placeholder — Phase 2 reads the affiliate
        # tracker DB to determine whether a short link was attached.
        affiliate_present = "#ad" in (clip.get("description") or "")

        description = clip.get("description") or build_description(
            creator=creator,
            visuals_used=visuals_used,
            affiliate_present=affiliate_present,
        )

        # Resolve the platform's posting mode from posting_schedule.yaml.
        # The mode controls dispatch: 'manual' writes to a drop directory
        # and waits for an operator-driven `make tiktok-confirm`; 'api'
        # calls the platform's upload stub. Anything else fails closed.
        platform_cfg = platforms_cfg.get(platform) or {}
        mode = (platform_cfg.get("mode") or "").strip().lower()

        if mode == "manual":
            drop_root = platform_cfg.get("manual_drop_directory")
            if not drop_root:
                _mark_failed(clip["id"], f"manual_mode_missing_drop_dir:{platform}")
                log(agent="publisher", event_type="post_failed", level="error",
                    clip_id=clip_id, payload={"platform": platform},
                    rationale="posting_schedule.yaml mode=manual but no manual_drop_directory configured")
                continue
            try:
                clip_dir = _write_manual_drop(
                    drop_root=drop_root,
                    clip_id=clip_id,
                    video_path=artifact.get("final_video_path") or "",
                    description=description,
                    hashtags=hashtags,
                )
            except Exception as exc:  # pragma: no cover — defensive only
                _mark_failed(clip["id"], f"manual_drop_error:{exc!r}")
                log(agent="publisher", event_type="post_failed", level="error",
                    clip_id=clip_id, payload={"platform": platform, "error": repr(exc)},
                    rationale="manual-drop write failed")
                continue
            _mark_manual_pending(clip["id"])
            log(agent="publisher", event_type="manual_pending",
                clip_id=clip_id,
                payload={"platform": platform, "drop_path": str(clip_dir),
                         "account_id": clip["account_id"]},
                rationale=(
                    f"staged for manual upload at {clip_dir}; operator runs "
                    f"`make tiktok-confirm CLIP_ID={clip_id} POST_ID=...` after posting"
                ))
            continue

        if mode != "api":
            _mark_failed(clip["id"], f"unsupported_mode:{mode or 'unset'}:{platform}")
            log(agent="publisher", event_type="post_failed", level="error",
                clip_id=clip_id, payload={"platform": platform, "mode": mode},
                rationale=f"posting_schedule.yaml mode='{mode}' for {platform} not recognized")
            continue

        dispatch = _DISPATCH.get(platform)
        if dispatch is None:
            _mark_failed(clip["id"], f"unsupported_platform:{platform}")
            log(agent="publisher", event_type="post_failed", level="error",
                clip_id=clip_id, payload={"platform": platform},
                rationale=f"no dispatch for target platform '{platform}'")
            continue

        try:
            platform_post_id = await dispatch(
                video_path=artifact.get("final_video_path") or "",
                title=clip.get("title") or "",
                description=description,
                hashtags=hashtags,
                account_id=clip["account_id"],
            )
        except NotImplementedError:
            _mark_failed(clip["id"], "phase1_scaffold:live_upload_not_wired")
            log(agent="publisher", event_type="phase1_scaffold",
                clip_id=clip_id, payload={"platform": platform},
                rationale="upload call stubbed in Phase 1; row marked failed for retry")
            continue
        except Exception as exc:  # pragma: no cover — defensive only
            _mark_failed(clip["id"], f"upload_error:{exc!r}")
            log(agent="publisher", event_type="post_failed", level="error",
                clip_id=clip_id, payload={"platform": platform, "error": repr(exc)},
                rationale="upload raised")
            continue

        _mark_posted(clip["id"], platform_post_id)
        log(agent="publisher", event_type="post_published",
            clip_id=clip_id,
            payload={"platform": platform, "platform_post_id": platform_post_id,
                     "account_id": clip["account_id"],
                     "posted_at": datetime.now(timezone.utc).isoformat()},
            rationale=f"posted to {platform} as {platform_post_id}")


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint for `make publish`
    import asyncio
    asyncio.run(run_publisher())
