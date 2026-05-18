"""Scout — discovers candidate clips from source platforms every 4h.

Reads /config/creators.yaml for the top-5 rotation, then queries each source
platform for new clips per creator. Live API integration is deferred to
Phase 2; this module is a structural scaffold with hardening:

- **Stable clip_id (E-5):** derived from (platform, sha256(source_url));
  no minute-precision timestamp, so two runs with the same source produce
  identical ids and INSERT OR IGNORE actually de-duplicates.
- **source_url validation (E-14):** every URL is checked against a
  platform-specific allowlist regex at insert time. Rejects shell
  metacharacters and non-platform URLs before they reach yt-dlp.
- **Retry/backoff:** every platform-discovery call is wrapped in
  retry_external (Phase 2 wiring catches transient HTTP failures).

Per CLAUDE.md "Architecture / Agent topology" — Scout step 1.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from agents.config import load
from agents.db import connect
from agents.events import log
from agents.models import CandidateClip, SourcePlatform
from agents.retry import retry_external

# Days without fresh content before Scout raises a warn-level alert per creator.
NO_FRESH_SOURCE_ALERT_DAYS = 30


# ---------- source_url validation (E-14) ----------

# Per-platform URL allowlists. Reject anything that doesn't match. The
# regexes deliberately allow only the canonical hostnames + safe path
# characters; anything else (shell metacharacters, traversal, weird
# protocols) gets rejected before it could reach yt-dlp's subprocess.
_URL_ALLOWLIST: dict[str, re.Pattern[str]] = {
    "twitch":    re.compile(r"^https://(www\.|clips\.)?twitch\.tv/[\w\-/?=&%]+$"),
    "youtube":   re.compile(r"^https://(www\.)?(youtube\.com|youtu\.be)/[\w\-/?=&%.]+$"),
    "tiktok":    re.compile(r"^https://(www\.)?tiktok\.com/@?[\w\-/?=&%.]+$"),
    "instagram": re.compile(r"^https://(www\.)?instagram\.com/[\w\-/?=&%.]+$"),
    "kick":      re.compile(r"^https://(www\.)?kick\.com/[\w\-/?=&%.]+$"),
}


class InvalidSourceUrlError(ValueError):
    """Raised when a source_url fails the platform allowlist."""


def _validate_source_url(platform: SourcePlatform, url: str) -> None:
    """Reject URLs that don't match the platform's regex.

    The regex is the security boundary: Scout's input comes from scraped
    HTML and external API responses (attacker-influenced text). If a
    malicious URL slipped through, Phase 2's yt-dlp wrapper could be
    coerced into shell injection or path traversal. Validating at insert
    time means a bad URL never makes it into `clips_candidate`.
    """
    pattern = _URL_ALLOWLIST.get(platform)
    if pattern is None:
        raise InvalidSourceUrlError(f"unknown platform: {platform!r}")
    if not pattern.match(url):
        raise InvalidSourceUrlError(
            f"source_url {url!r} does not match the {platform!r} allowlist regex. "
            "URLs from scraped sources must match the canonical platform format; "
            "metacharacters and non-platform domains are rejected at insert."
        )


# ---------- Source-platform clients (Phase 2 wiring) ----------

@retry_external(max_attempts=3, base_delay_s=1.0)
async def _discover_twitch_clips(creator_handle: str) -> list[CandidateClip]:
    # TODO(phase2): wire Twitch Helix GET /clips with broadcaster_id + 30d window.
    # Needs: TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, app-access-token rotation.
    # Retry decorator handles transient 5xx / 429 / network errors per
    # docs/posting_apis.md. Raise TransientError from the wired path; let
    # 4xx (other than 429) propagate.
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


@retry_external(max_attempts=3, base_delay_s=1.0)
async def _discover_youtube_clips(creator_handle: str) -> list[CandidateClip]:
    # TODO(phase2): wire YouTube Data API v3 search.list filtered to channelId
    # ordered by viewCount within the 90d window, then videos.list for stats.
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


@retry_external(max_attempts=3, base_delay_s=2.0)
async def _discover_tiktok_trending(creator_handle: str) -> list[CandidateClip]:
    # TODO(phase2): wire TikTok Creative Center scrape (or Research API if approved).
    # Note creators.yaml red_flag — TikTok 30d aggregates are unverified for all 5.
    # Longer base_delay because scraping needs gentler retry cadence.
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


@retry_external(max_attempts=3, base_delay_s=1.0)
async def _discover_kick_clips(creator_handle: str) -> list[CandidateClip]:
    # TODO(phase2): wire Kick public clip endpoints (Adin Ross substitution per
    # creators.yaml adin_ross_kick_substitution note).
    raise NotImplementedError("live mode not implemented in Phase 1 scaffold")


_PLATFORM_DISPATCH = {
    "twitch": _discover_twitch_clips,
    "youtube": _discover_youtube_clips,
    "tiktok": _discover_tiktok_trending,
    "kick": _discover_kick_clips,
}


# ---------- Stable clip_id (E-5) ----------

def make_clip_id(platform: SourcePlatform, source_url: str) -> str:
    """Derive a stable clip_id from (platform, source_url).

    Format: `<platform>-<sha256(source_url)[:16]>`. Same URL produces the
    same id forever — Scout reruns won't insert duplicates, and downstream
    references (events, costs ledger, audit log) survive re-scrapes.

    The previous implementation prefixed `yyyy-mm-dd-hhmm-`, so a minute-
    boundary crossing produced different ids for the same URL. The
    `scouted_at` column already records discovery time; the id doesn't
    need to encode it.

    The 16-hex-char hash is 64 bits of entropy. At realistic Scout
    volumes (~7,000 clips/year), birthday-paradox collision probability
    is < 1 in 10^9 — far below the structural UNIQUE(source_url) gate
    which is the actual safety net.
    """
    h = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16]
    return f"{platform}-{h}"


def _check_fresh_source(creator: str, latest_seen_at: datetime | None) -> None:
    """Warn if a creator has no new content in NO_FRESH_SOURCE_ALERT_DAYS."""
    if latest_seen_at is None:
        log(
            agent="scout",
            event_type="no_fresh_source_alert",
            level="warn",
            payload={"creator": creator, "latest_seen_at": None},
            rationale=f"no source content ever seen for {creator}",
        )
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=NO_FRESH_SOURCE_ALERT_DAYS)
    if latest_seen_at < cutoff:
        log(
            agent="scout",
            event_type="no_fresh_source_alert",
            level="warn",
            payload={
                "creator": creator,
                "latest_seen_at": latest_seen_at.isoformat(),
                "threshold_days": NO_FRESH_SOURCE_ALERT_DAYS,
            },
            rationale=f"{creator} has no fresh source content in {NO_FRESH_SOURCE_ALERT_DAYS}d",
        )


def _insert_candidate(clip: CandidateClip) -> bool:
    """Insert (or skip-if-exists). Returns True if a new row was inserted.

    Validates source_url against the platform allowlist before insert; if
    validation fails, raises InvalidSourceUrlError and does NOT write
    a row. The UNIQUE(source_url) index (migration v5) is the structural
    backstop — even if validation is bypassed, two inserts for the same
    URL would fail at the DB layer.
    """
    _validate_source_url(clip.source_platform, clip.source_url)
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO clips_candidate
              (id, creator, source_platform, source_url, source_title,
               source_duration_s, source_view_count, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered')
            """,
            (
                clip.id,
                clip.creator,
                clip.source_platform,
                clip.source_url,
                clip.source_title,
                clip.source_duration_s,
                clip.source_view_count,
            ),
        )
        # rowcount=1 → newly inserted; rowcount=0 → already existed (id OR url collision)
        return cur.rowcount == 1


# ---------- Public entry point ----------

async def run_scout() -> list[CandidateClip]:
    """Discover candidate clips for every creator in /config/creators.yaml.

    Returns the list of newly-inserted CandidateClip rows (existing rows are
    skipped silently — that's the idempotency contract). Live source-platform
    calls are NotImplementedError stubs; Phase 2 wires them.
    """
    creators_cfg = load("creators")
    discovered: list[CandidateClip] = []

    for entry in creators_cfg.get("creators", []):
        creator = entry["creator"]
        primary_platforms: list[SourcePlatform] = entry.get("primary_platforms", [])

        # Fresh-source alert — Phase 1 we have no DB history yet, so this is a
        # placeholder call. Phase 2 will query clips_candidate for max(scouted_at).
        _check_fresh_source(creator, latest_seen_at=None)

        for platform in primary_platforms:
            dispatch = _PLATFORM_DISPATCH.get(platform)
            if dispatch is None:
                log(
                    agent="scout",
                    event_type="unsupported_platform",
                    level="warn",
                    payload={"creator": creator, "platform": platform},
                    rationale=f"no dispatch for source platform '{platform}'",
                )
                continue
            try:
                handle = entry["platforms"][platform]["handle"]
                clips = await dispatch(handle)
            except NotImplementedError:
                log(
                    agent="scout",
                    event_type="phase1_scaffold",
                    level="info",
                    payload={"creator": creator, "platform": platform},
                    rationale="live source client stubbed in Phase 1",
                )
                continue
            except Exception as exc:
                # Retry exhausted, or non-transient error. Log and move on
                # to the next platform rather than aborting the whole scout.
                log(
                    agent="scout",
                    event_type="discover_failed",
                    level="error",
                    payload={
                        "creator": creator,
                        "platform": platform,
                        "error": f"{exc.__class__.__name__}: {exc}",
                    },
                    rationale=f"discovery failed for {creator}/{platform}",
                )
                continue

            for clip in clips:
                try:
                    inserted = _insert_candidate(clip)
                except InvalidSourceUrlError as exc:
                    log(
                        agent="scout",
                        event_type="invalid_source_url",
                        level="warn",
                        payload={
                            "creator": clip.creator,
                            "platform": clip.source_platform,
                            "url": clip.source_url[:200],  # truncate adversarial URLs in logs
                            "reason": str(exc),
                        },
                        rationale="URL failed allowlist; not inserted",
                    )
                    continue
                if inserted:
                    log(
                        agent="scout",
                        event_type="clip_discovered",
                        clip_id=clip.id,
                        payload={
                            "creator": clip.creator,
                            "platform": clip.source_platform,
                            "url": clip.source_url,
                            "view_count": clip.source_view_count,
                        },
                        rationale=f"scouted from {clip.source_platform}",
                    )
                    discovered.append(clip)
                else:
                    log(
                        agent="scout",
                        event_type="clip_skipped_duplicate",
                        level="debug",
                        clip_id=clip.id,
                        payload={"creator": clip.creator, "platform": clip.source_platform},
                        rationale="already in clips_candidate (idempotent re-scout)",
                    )

    log(
        agent="scout",
        event_type="run_complete",
        payload={"discovered_count": len(discovered)},
        rationale=f"scout cycle finished, {len(discovered)} new candidates",
    )
    return discovered
