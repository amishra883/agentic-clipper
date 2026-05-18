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
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

from agents.config import load
from agents.db import connect
from agents.events import log
from agents.models import CandidateClip, SourcePlatform
from agents.retry import retry_external

# Days without fresh content before Scout raises a warn-level alert per creator.
NO_FRESH_SOURCE_ALERT_DAYS = 30


# ---------- source_url canonicalization (E-14 + canonical-id collision) ----------
#
# Codex 2026-05-18 findings (empirically verified):
#   `https://twitch.tv/foo%3Brm%20-rf%20/` PASSED the regex (encoded ;)
#   `https://www.youtube.com/watch?v=X` vs `https://youtu.be/X` produced
#   DIFFERENT clip_ids despite being the same video.
#
# Fixes (this block):
#   1. urllib.parse + unquote + IDNA — decode the URL before validation
#      so encoded shell metacharacters and path traversal don't slip
#      through.
#   2. Per-platform canonical URL extraction — same YouTube video at
#      youtube.com/watch?v=X / youtu.be/X / youtube.com/shorts/X
#      collapses to one canonical form. UNIQUE(source_url) now actually
#      de-duplicates.

# Shell metacharacters that, if decoded, would be dangerous to pass to
# yt-dlp via subprocess. Reject in decoded path/query.
_DECODED_DANGEROUS_CHARS = re.compile(r"[;|&$<>`\n\r\t\x00\\]")

# Path-traversal in decoded path.
_PATH_TRAVERSAL = re.compile(r"(^|/)\.\.(/|$)")

# Per-platform allowed host set. Compared case-insensitively after IDNA.
_HOST_ALLOWLIST: dict[str, set[str]] = {
    "twitch":    {"twitch.tv", "www.twitch.tv", "clips.twitch.tv", "m.twitch.tv"},
    "youtube":   {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"},
    "tiktok":    {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com"},
    "instagram": {"instagram.com", "www.instagram.com", "m.instagram.com"},
    "kick":      {"kick.com", "www.kick.com"},
}

# YouTube video_id is `[A-Za-z0-9_-]{11}` per the spec.
_YT_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Twitch clip slug shape: title-cased words joined by hyphens.
_TWITCH_CLIP_SLUG = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


class InvalidSourceUrlError(ValueError):
    """Raised when a source_url fails canonicalization."""


def _idna_host(raw_host: str) -> str:
    """IDNA-encode the host and lowercase. Unicode confusables (e.g.,
    Cyrillic 'h' that looks like Latin 'h' — `twitchһ.tv`) either get
    normalized to their punycode form (which fails the allowlist) or
    raise UnicodeError outright."""
    try:
        return raw_host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InvalidSourceUrlError(f"host {raw_host!r} fails IDNA encoding: {exc}")


def _reject_dangerous_decoded(decoded: str, context: str) -> None:
    """After URL-decoding the path+query, look for shell metacharacters
    or path traversal. Raises on any hit."""
    m = _DECODED_DANGEROUS_CHARS.search(decoded)
    if m:
        raise InvalidSourceUrlError(
            f"decoded {context} contains shell metacharacter "
            f"{m.group(0)!r} at position {m.start()}: {decoded[:80]!r}"
        )
    if _PATH_TRAVERSAL.search(decoded):
        raise InvalidSourceUrlError(
            f"decoded {context} contains path-traversal segment '..': {decoded[:80]!r}"
        )


def _canonical_youtube(parsed) -> str:
    """Collapse every YouTube URL shape to https://www.youtube.com/watch?v=<id>.

    Supports: /watch?v=ID, youtu.be/ID, /shorts/ID, /embed/ID. Each yields
    the same canonical string for the same video, so UNIQUE(source_url)
    catches duplicate inserts."""
    host = parsed.hostname or ""
    path = parsed.path or ""
    video_id = ""
    if host == "youtu.be":
        video_id = path.lstrip("/").split("/")[0]
    elif path == "/watch":
        qs = parse_qs(parsed.query)
        video_id = (qs.get("v") or [""])[0]
    elif path.startswith("/shorts/"):
        video_id = path[len("/shorts/"):].split("/")[0]
    elif path.startswith("/embed/"):
        video_id = path[len("/embed/"):].split("/")[0]
    if not _YT_VIDEO_ID.match(video_id):
        raise InvalidSourceUrlError(
            f"could not extract a valid 11-char YouTube video_id from "
            f"host={host!r} path={path!r}; got {video_id!r}"
        )
    return f"https://www.youtube.com/watch?v={video_id}"


def _canonical_twitch(parsed) -> str:
    """Collapse Twitch URLs:
      clips.twitch.tv/<SLUG>            → https://clips.twitch.tv/<SLUG>
      twitch.tv/<ch>/clip/<SLUG>        → https://clips.twitch.tv/<SLUG>
      twitch.tv/videos/<NUMERIC_ID>     → kept as-is (VOD, different shape)
    """
    host = parsed.hostname or ""
    path = parsed.path or ""
    slug = ""
    if host == "clips.twitch.tv":
        slug = path.lstrip("/").split("/")[0]
    elif "/clip/" in path:
        slug = path.split("/clip/", 1)[1].split("/")[0]
    elif "/videos/" in path:
        vod_id = path.split("/videos/", 1)[1].split("/")[0]
        if not vod_id.isdigit():
            raise InvalidSourceUrlError(
                f"twitch VOD path expects numeric id; got {vod_id!r}"
            )
        return f"https://www.twitch.tv/videos/{vod_id}"
    if not slug or not _TWITCH_CLIP_SLUG.match(slug):
        raise InvalidSourceUrlError(
            f"could not extract a valid Twitch clip slug from "
            f"host={host!r} path={path!r}; got {slug!r}"
        )
    return f"https://clips.twitch.tv/{slug}"


def _canonical_generic(parsed, allowed_hosts: set[str]) -> str:
    """For TikTok/Instagram/Kick — no strict id extractor yet. Strip
    query + fragment + canonicalize host casing. De-dupes trivial
    variants (e.g., ?utm_source= on a TikTok URL)."""
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        raise InvalidSourceUrlError(f"host {host!r} not in allowlist {allowed_hosts}")
    canonical_host = sorted(allowed_hosts, key=len)[0]
    path = parsed.path or "/"
    return f"https://{canonical_host}{path}"


def canonicalize_source_url(platform: SourcePlatform, url: str) -> str:
    """Validate + canonicalize. Returns the form Scout stores in
    `clips_candidate.source_url` and `make_clip_id` derives from.

    Raises InvalidSourceUrlError on:
      - scheme != https
      - host not in allowlist (IDNA-normalized; rejects Unicode confusables)
      - decoded path/query has shell metachars or `../`
      - platform canonical extraction fails (missing video_id / clip slug)
    """
    if not isinstance(url, str) or len(url) > 2048:
        raise InvalidSourceUrlError(
            f"url not a str or exceeds 2KB: type={type(url).__name__}"
        )
    if platform not in _HOST_ALLOWLIST:
        raise InvalidSourceUrlError(f"unknown platform: {platform!r}")

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise InvalidSourceUrlError(f"urlparse failed: {exc}")

    if parsed.scheme != "https":
        raise InvalidSourceUrlError(
            f"scheme must be https; got {parsed.scheme!r} in {url[:80]!r}"
        )

    raw_host = parsed.hostname or ""
    if not raw_host:
        raise InvalidSourceUrlError(f"missing host in {url[:80]!r}")
    canonical_host = _idna_host(raw_host)
    allowed = _HOST_ALLOWLIST[platform]
    if canonical_host not in allowed:
        raise InvalidSourceUrlError(
            f"host {canonical_host!r} not in {platform} allowlist {allowed}"
        )
    parsed = parsed._replace(netloc=canonical_host)

    # The bit the old regex missed: decode percent-encoding and check the
    # DECODED bytes for shell metacharacters and traversal. Otherwise
    # `%3B` (encoded `;`) sails through.
    decoded_path = unquote(parsed.path or "")
    _reject_dangerous_decoded(decoded_path, "path")
    if parsed.query:
        decoded_query = unquote(parsed.query)
        _reject_dangerous_decoded(decoded_query, "query")

    if platform == "youtube":
        return _canonical_youtube(parsed)
    if platform == "twitch":
        return _canonical_twitch(parsed)
    return _canonical_generic(parsed, allowed)


def _validate_source_url(platform: SourcePlatform, url: str) -> None:
    """Back-compat shim: canonicalize and discard. Raises on invalid input."""
    canonicalize_source_url(platform, url)


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
    """Canonicalize source_url, derive clip_id from the canonical form,
    INSERT OR IGNORE. Returns True iff a new row was inserted.

    Why canonicalize before insert: youtube.com/watch?v=X and youtu.be/X
    are the same video. If Scout stores both raw, they'd produce two
    rows with two different clip_ids (the UNIQUE constraint on
    source_url only catches byte-for-byte duplicates). Canonicalizing
    BEFORE storage means both inputs collapse to the same source_url,
    the same clip_id, and UNIQUE actually de-duplicates.

    Raises InvalidSourceUrlError if canonicalization fails — does NOT
    write a row in that case.
    """
    canonical_url = canonicalize_source_url(clip.source_platform, clip.source_url)
    canonical_id = make_clip_id(clip.source_platform, canonical_url)
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO clips_candidate
              (id, creator, source_platform, source_url, source_title,
               source_duration_s, source_view_count, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered')
            """,
            (
                canonical_id,
                clip.creator,
                clip.source_platform,
                canonical_url,
                clip.source_title,
                clip.source_duration_s,
                clip.source_view_count,
            ),
        )
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
