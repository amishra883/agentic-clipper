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
from typing import Callable, Iterator, Literal

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


class StaleArtifactVersion(Exception):
    """Raised when a lease tries to bump artifact_version but the row's
    current version is already past what we read at lease start. Means
    another stage raced ahead between our read and write — our output
    is stale and must not land.

    Codex finding 2026-05-18: previously the bump silently no-op'd on a
    rowcount=0 UPDATE; the lease still marked succeeded with the (now
    stale) input_artifact_version. Compliance downstream would consume
    artifacts whose version pointer was wrong. This exception forces
    the caller to handle the race instead of silently committing bad
    state."""


@dataclass
class Lease:
    """Live lease handle. Caller mutates `output_artifact_version` before
    the context manager exits; the exit handler persists it.

    `commit_artifact()` is the atomic-persist path (Codex 2026-05-18 P1
    fix). It writes the artifact data AND bumps artifact_version in one
    BEGIN IMMEDIATE so a concurrent stage can't race past us between
    our data commit and the lease's version-bump CAS. Stages that use
    commit_artifact set `_artifact_committed=True`, which makes the
    lease's end-of-block `_mark_done` skip its legacy CAS (the bump
    already happened atomically). Stages that don't write artifacts
    (e.g. Curator, which mutates clips_candidate) use the legacy path
    via `output_artifact_version=None` and `_mark_done` is a no-op
    for the clip_artifacts row."""
    run_id: int
    clip_id: str
    stage: "Stage"
    input_artifact_version: int
    output_artifact_version: int | None = None
    _artifact_committed: bool = False

    def commit_artifact(
        self,
        persist_fn: "Callable[[sqlite3.Connection, int], None]",
    ) -> None:
        """Atomic version-checked persist. Replaces the broken pattern of
        a separate persist transaction followed by a separate lease CAS.

        `persist_fn(conn, new_artifact_version)` is called inside a single
        BEGIN IMMEDIATE that:
          1. Re-reads `clip_artifacts.artifact_version` for this clip.
          2. Raises `StaleArtifactVersion` if it diverged from
             `self.input_artifact_version` (another stage raced past us).
          3. Calls persist_fn with the connection and the bumped version
             so the caller's INSERT/UPDATE writes the new data AND the
             new `artifact_version=input+1` in one statement.
          4. Commits atomically. On any exception, rolls back so no
             partial data lands.

        After successful commit, this method updates `output_artifact_version`
        on the lease and sets `_artifact_committed=True` so the
        context manager's exit handler skips the now-redundant CAS in
        `_mark_done`.

        Stages that previously did `with connect() as conn: conn.execute(
        'INSERT INTO clip_artifacts ...')` now wrap that body in a
        callable and pass it here.
        """
        # Local import to avoid a circular import between stage_lease and db.
        from agents.db import connect

        new_version = self.input_artifact_version + 1
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT artifact_version FROM clip_artifacts WHERE clip_id = ?",
                    (self.clip_id,),
                ).fetchone()
                current = int(row["artifact_version"]) if row else 0
                if current != self.input_artifact_version:
                    conn.execute("ROLLBACK")
                    raise StaleArtifactVersion(
                        f"commit_artifact: clip_id={self.clip_id} "
                        f"stage={self.stage} expected artifact_version="
                        f"{self.input_artifact_version} but DB has {current}. "
                        f"Another stage raced past; output discarded "
                        f"before any data was written."
                    )
                persist_fn(conn, new_version)
                conn.execute("COMMIT")
            except StaleArtifactVersion:
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

        self.output_artifact_version = new_version
        self._artifact_committed = True


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
    # If the caller used `lease.commit_artifact()`, the artifact_version
    # was already bumped atomically with the data write — _mark_done
    # must NOT run its legacy CAS in that case (it would find
    # artifact_version=output instead of input and raise spuriously).
    _mark_done(
        run_id,
        status="succeeded",
        output_version=lease.output_artifact_version,
        failure_reason=None,
        skip_artifact_cas=lease._artifact_committed,
    )


def _mark_done(
    run_id: int,
    *,
    status: Literal["succeeded", "failed"],
    output_version: int | None,
    failure_reason: str | None,
    skip_artifact_cas: bool = False,
) -> None:
    """Flip status to succeeded/failed AND, on success, bump
    clip_artifacts.artifact_version if the caller specified an output
    version. Both writes happen in one BEGIN IMMEDIATE transaction so
    the lease completion and the artifact bump are atomic.

    `skip_artifact_cas=True` means the caller used `lease.commit_artifact()`,
    which already bumped artifact_version atomically with the data write.
    The legacy CAS here would find the row at output_version (not
    output_version - 1) and raise StaleArtifactVersion spuriously.
    Skip it; only update pipeline_runs."""
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

            if (
                status == "succeeded"
                and output_version is not None
                and not skip_artifact_cas
            ):
                # Get the clip_id from the run row so we know which
                # clip_artifacts row to bump.
                row = conn.execute(
                    "SELECT clip_id FROM pipeline_runs WHERE id = ?", (run_id,)
                ).fetchone()
                if row is not None:
                    # Conditional UPDATE: only bump if the artifact_version
                    # we read at lease-start is still the current version.
                    # Race semantics (Codex 2026-05-18): if rowcount != 1
                    # the CAS lost — another stage raced ahead and bumped
                    # past our input_version while we were running. Our
                    # output is stale; do not silently commit it.
                    cur = conn.execute(
                        """
                        UPDATE clip_artifacts
                           SET artifact_version = ?
                         WHERE clip_id = ?
                           AND artifact_version = ?
                        """,
                        (output_version, row["clip_id"], output_version - 1),
                    )
                    if cur.rowcount != 1:
                        # Mark the run failed (we're inside a transaction
                        # that will COMMIT in a moment; need to flip the
                        # run row's status from succeeded → failed AND
                        # then ROLLBACK to undo everything, since the
                        # caller's `with` body believed it succeeded).
                        conn.execute("ROLLBACK")
                        # Re-mark the run as failed in a separate transaction
                        # so the audit trail records what happened.
                        with connect() as fail_conn:
                            fail_conn.execute(
                                """
                                UPDATE pipeline_runs
                                   SET status = 'failed',
                                       failure_reason = 'stale_artifact_version',
                                       completed_at = datetime('now')
                                 WHERE id = ?
                                """,
                                (run_id,),
                            )
                        raise StaleArtifactVersion(
                            f"stage {row['clip_id']} ran on artifact_version={output_version - 1} "
                            f"but DB already moved past that. Output discarded."
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
