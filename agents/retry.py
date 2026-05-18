"""Retry/backoff helper for external-API calls.

Phase 2 introduces five external-API surfaces (Twitch Helix, YouTube Data
API, TikTok Creative Center scrape, Anthropic LLM, Atlas Cloud Seedance)
that all need the same shape of retry: transient errors (network failures,
HTTP 5xx, rate-limit 429) retry with exponential backoff + jitter;
permanent errors (4xx other than 429, malformed responses) raise.

Codex's Phase 3 review flagged "no fallback when extractor breaks" and
"ElevenLabs 429 silent fallback" as separate-but-related findings — both
indicate the pipeline doesn't distinguish transient from permanent failure.
This module is the chokepoint where that distinction lives.

Usage
-----
    from agents.retry import retry_external, RetryGiveUp

    @retry_external(max_attempts=3, base_delay_s=1.0)
    async def fetch_twitch_clips(handle: str) -> list[dict]:
        async with session.get(...) as resp:
            if resp.status == 429:
                raise TransientError(f"rate limited: {resp.headers['Retry-After']}")
            if resp.status >= 500:
                raise TransientError(f"upstream {resp.status}")
            resp.raise_for_status()
            return await resp.json()

The decorator catches `TransientError` (and configurable other types),
sleeps `base_delay_s * 2^attempt + jitter`, and retries up to
`max_attempts`. If all attempts exhaust, it raises `RetryGiveUp` with
the final exception attached.

Non-transient exceptions propagate immediately — bugs in our code or
permanent upstream rejections shouldn't be hidden behind 3 silent retries.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import random
import time
from typing import Callable, TypeVar


class TransientError(Exception):
    """Raise from the decorated function for errors that should be retried.

    Examples that should map to TransientError:
    - HTTP 429 (Rate Limited) — honor Retry-After if present
    - HTTP 503 (Service Unavailable) / 502 / 504
    - Network errors (DNS, connection refused, timeout)
    - Specific upstream errors marked as retryable

    Examples that should NOT be TransientError (just let them raise):
    - HTTP 400 / 401 / 403 / 404 (caller fault, no point retrying)
    - JSON decode error (upstream contract violation; retry won't help)
    - AssertionError / KeyError / TypeError (bugs in our code)
    """


class RetryGiveUp(Exception):
    """All retry attempts exhausted. Holds the last underlying exception
    on `.__cause__` for caller inspection."""


F = TypeVar("F", bound=Callable)


def retry_external(
    *,
    max_attempts: int = 3,
    base_delay_s: float = 1.0,
    max_delay_s: float = 30.0,
    jitter_factor: float = 0.25,
    retry_on: tuple[type[BaseException], ...] = (TransientError,),
) -> Callable[[F], F]:
    """Decorator: retry transient errors with exponential backoff + jitter.

    Backoff schedule for default params (1.0, 30.0):
        attempt 1 fails → wait 1.0s + jitter
        attempt 2 fails → wait 2.0s + jitter
        attempt 3 fails → wait 4.0s + jitter
        attempt 4+      → cap at max_delay_s + jitter
        after max_attempts → raise RetryGiveUp

    Works on both sync and async callables. Async functions get an
    awaitable wrapper using asyncio.sleep; sync functions use time.sleep.

    `retry_on` lets callers add their own retryable exception classes
    (e.g., httpx.ConnectError, asyncio.TimeoutError) without subclassing
    TransientError. Permanent errors (anything not in `retry_on`)
    propagate immediately without retry.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1; got {max_attempts}")
    if base_delay_s <= 0:
        raise ValueError(f"base_delay_s must be > 0; got {base_delay_s}")
    # Codex 2026-05-18 finding: prior code only validated max_attempts and
    # base_delay_s. max_delay_s=0 produces a tight retry loop with no sleep
    # (the min(...) clamps every backoff to 0); jitter_factor<0 yields
    # negative delays → asyncio.sleep / time.sleep raise.
    if max_delay_s <= 0:
        raise ValueError(f"max_delay_s must be > 0; got {max_delay_s}")
    if jitter_factor < 0:
        raise ValueError(f"jitter_factor must be >= 0; got {jitter_factor}")

    def _compute_delay(attempt: int) -> float:
        # attempt is 1-indexed for human readability
        backoff = base_delay_s * (2 ** (attempt - 1))
        capped = min(backoff, max_delay_s)
        jitter = random.uniform(0, capped * jitter_factor)
        return capped + jitter

    def decorator(fn: F) -> F:
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                last_exc: BaseException | None = None
                for attempt in range(1, max_attempts + 1):
                    try:
                        return await fn(*args, **kwargs)
                    except retry_on as exc:
                        last_exc = exc
                        if attempt == max_attempts:
                            break
                        await asyncio.sleep(_compute_delay(attempt))
                raise RetryGiveUp(
                    f"{fn.__name__}: gave up after {max_attempts} attempts"
                ) from last_exc
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except retry_on as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        break
                    time.sleep(_compute_delay(attempt))
            raise RetryGiveUp(
                f"{fn.__name__}: gave up after {max_attempts} attempts"
            ) from last_exc

        return sync_wrapper  # type: ignore[return-value]

    return decorator
