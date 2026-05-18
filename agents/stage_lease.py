"""Stage-lease helper — race-free pipeline-stage claims.

Closes Eng review finding E-1. Pipeline stages (Editor, Writer, Voice,
Visuals, Compositor) all mutate `clip_artifacts` for the same clip_id.
Without leases, two workers racing the same stage can tear writes; a
worker that read input-artifact-version N can overwrite output that
another worker already wrote at version N+1.

Calling pattern
---------------
    from agents.stage_lease import stage_lease, LeaseConflict

    with stage_lease(clip_id, stage="editor", ttl_seconds=900) as lease:
        # exclusive ownership of (clip_id, editor) for up to 15 minutes;
        # janitor sweeps after that
        input_version = lease.input_artifact_version
        ... # do the work
        lease.output_artifact_version = input_version + 1
    # exit: marks succeeded (or failed if an exception bubbled) and
    # records output_artifact_version atomically

If another worker already holds the lease:
    >>> with stage_lease(clip_id, stage="editor") as lease:
    ...     ...
    LeaseConflict: editor already in_progress for clip 2026-05-18-xyz

The caller can catch and retry / skip / log per its own policy.

Janitor
-------
Expired leases (`lease_expires_at` in the past, status still
`in_progress`) get swept to `status='expired'` by `sweep_expired_leases`.
The orchestrator should call this on a 5-minute cadence — out of scope
for the helper itself but documented here for whoever wires the cron.
"""

from __future__ import annotations

import os
import socket
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Literal

from agents.db import connect

Stage = Literal[
    "scout", "curator", "editor", "writer",
    "voice", "visuals", "compositor", "compliance",
    "publisher", "analyst", "optimizer",
]

DEFAULT_TTL_SECONDS = 900  # 15 min default lease


class LeaseConflict(Exception):
    """Raised when another worker already holds an in_progress lease on
    (clip_id, stage). Caller decides whether to retry or skip."""


@dataclass
class Lease:
    """Live lease handle. Caller mutates `output_artifact_version` before
    the context manager exits; the exit handler persists it."""
    run_id: int
    clip_id: str
    stage: Stage
    input_artifact_version: int
    output_artifact_version: int | None = None


def _claimant_id() -> str:
    """Stable-ish identifier for "who holds this lease." Helps post-mortems
    answer "why didn't my new run pick up clip X" by pointing at the PID
    that's actually holding it."""
    return f"pid={os.getpid()}@{socket.gethostname()}"


def _current_artifact_version(conn: sqlite3.Connection, clip_id: str) -> int:
    """Look up clip_artifacts.artifact_version. Returns 0 when no row
    exists yet (first stage to write the artifact starts at version 1)."""
    row = conn.execute(
        "SELECT artifact_version FROM clip_artifacts WHERE clip_id = ?",
        (clip_id,),
    ).fetchone()
    return int(row["artifact_version"]) if row else 0


@contextmanager
def stage_lease(
    clip_id: str,
    stage: Stage,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> Iterator[Lease]:
    """Claim (clip_id, stage) exclusively. Yields a Lease the caller can
    mutate; on clean exit marks succeeded; on exception marks failed.

    Atomicity: the claim INSERT and the version-read are wrapped in
    BEGIN IMMEDIATE so concurrent workers serialize at the SQLite locking
    layer. The partial UNIQUE index on pipeline_runs (active rows only)
    is the structural guarantee; BEGIN IMMEDIATE just makes the failure
    fast and explicit instead of letting two workers both try the INSERT.

    On lease expiry mid-run (janitor flips status from in_progress to
    expired while we're still working), the exit handler will fail to
    update the row — the UPDATE's WHERE clause requires status='in_progress'.
    That's the race tell. Caller's exception handling sees the rowcount=0
    and can decide whether to retry or surface a conflict.
    """
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=ttl_seconds)
    claimant = _claimant_id()

    with connect() as conn:
        # Open an immediate transaction so concurrent claimants serialize.
        conn.execute("BEGIN IMMEDIATE")
        try:
            input_version = _current_artifact_version(conn, clip_id)
            try:
                cur = conn.execute(
                    """
                    INSERT INTO pipeline_runs
                      (clip_id, stage, claimed_by, claimed_at, lease_expires_at,
                       attempt, input_artifact_version, status)
                    VALUES (?, ?, ?, ?, ?, 1, ?, 'in_progress')
                    """,
                    (
                        clip_id,
                        stage,
                        claimant,
                        now.isoformat(sep=" ", timespec="seconds"),
                        expires.isoformat(sep=" ", timespec="seconds"),
                        input_version,
                    ),
                )
                run_id = cur.lastrowid
            except sqlite3.IntegrityError as exc:
                # The UNIQUE-active index fired: another worker is already
                # running this stage. Roll back our open transaction and
                # surface the conflict.
                conn.execute("ROLLBACK")
                raise LeaseConflict(
                    f"{stage} already in_progress for clip {clip_id}"
                ) from exc
            conn.execute("COMMIT")
        except LeaseConflict:
            raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

    lease = Lease(
        run_id=run_id,
        clip_id=clip_id,
        stage=stage,
        input_artifact_version=input_version,
        output_artifact_version=None,
    )

    try:
        yield lease
    except Exception as exc:
        _mark_done(
            run_id,
            status="failed",
            output_version=None,
            failure_reason=f"{exc.__class__.__name__}: {exc}",
        )
        raise

    # Successful exit. Persist output_artifact_version if the caller set it.
    _mark_done(
        run_id,
        status="succeeded",
        output_version=lease.output_artifact_version,
        failure_reason=None,
    )


def _mark_done(
    run_id: int,
    *,
    status: Literal["succeeded", "failed"],
    output_version: int | None,
    failure_reason: str | None,
) -> None:
    """Flip status to succeeded/failed AND, on success, bump
    clip_artifacts.artifact_version if the caller specified an output
    version. Both writes happen in one BEGIN IMMEDIATE transaction so
    the lease completion and the artifact bump are atomic."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Only update if still in_progress — protects against the case
            # where the janitor already swept this lease to expired.
            cur = conn.execute(
                """
                UPDATE pipeline_runs
                   SET status = ?,
                       output_artifact_version = ?,
                       failure_reason = ?,
                       completed_at = datetime('now')
                 WHERE id = ? AND status = 'in_progress'
                """,
                (status, output_version, failure_reason, run_id),
            )
            if cur.rowcount == 0:
                # Lease was already moved (expired by janitor, or a manual
                # cleanup). Nothing else to do; the artifact bump below is
                # also skipped because we're no longer the authoritative
                # writer for this run.
                conn.execute("ROLLBACK")
                return

            if status == "succeeded" and output_version is not None:
                # Get the clip_id from the run row so we know which
                # clip_artifacts row to bump.
                row = conn.execute(
                    "SELECT clip_id FROM pipeline_runs WHERE id = ?", (run_id,)
                ).fetchone()
                if row is not None:
                    # Conditional UPDATE: only bump if the artifact_version
                    # we read at lease-start is still the current version.
                    # If another stage raced ahead, our output is stale —
                    # skip the bump and let the caller's exception handler
                    # (if any) discover the race.
                    conn.execute(
                        """
                        UPDATE clip_artifacts
                           SET artifact_version = ?
                         WHERE clip_id = ?
                           AND artifact_version = ?
                        """,
                        (output_version, row["clip_id"], output_version - 1),
                    )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise


def sweep_expired_leases() -> int:
    """Janitor: move leases past their deadline from in_progress to expired.

    Returns the count swept. Intended to be called on a 5-min cron / inside
    the orchestrator's tick loop. Out-of-scope here to actually schedule
    it — that's the orchestrator's job.
    """
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE pipeline_runs
               SET status = 'expired',
                   completed_at = datetime('now')
             WHERE status = 'in_progress'
               AND strftime('%s', lease_expires_at) < strftime('%s', 'now')
            """
        )
        return cur.rowcount
