"""Stage-lease tests — verifies the race-free claim mechanism from E-1.

Critical assertions:
- Two workers can't both hold an active lease on (clip, stage)
- Successful exit bumps clip_artifacts.artifact_version atomically
- Exception inside the `with` block marks the lease 'failed' and re-raises
- Expired leases get swept by sweep_expired_leases
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from agents.db import init_schema
from agents.stage_lease import (
    Lease,
    LeaseConflict,
    StaleArtifactVersion,
    stage_lease,
    sweep_expired_leases,
)
from scripts.migrate import migrate


@pytest.fixture
def migrated_db(monkeypatch):
    """Fresh DB seeded from schema.sql + migrations applied through v4.
    Inserts a clips_candidate row so FK constraints don't trip."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "lease.db"
        monkeypatch.setenv("AGENTIC_CLIPPER_DB", str(db_path))
        init_schema(db_path)
        migrate(db_path=db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO clips_candidate (id, creator, source_platform, source_url, status)
                VALUES ('test-clip-1', 'IShowSpeed', 'youtube', 'https://t', 'processing')
                """
            )
            # Seed a clip_artifacts row at artifact_version=1 so the
            # output_artifact_version bump (1→2) can apply.
            conn.execute(
                """
                INSERT INTO clip_artifacts (clip_id, artifact_version)
                VALUES ('test-clip-1', 1)
                """
            )
            conn.commit()
        yield db_path


# ---------- Basic claim/release ----------

def test_lease_succeeds_and_bumps_artifact_version(migrated_db):
    with stage_lease("test-clip-1", stage="editor", ttl_seconds=60) as lease:
        assert lease.clip_id == "test-clip-1"
        assert lease.stage == "editor"
        assert lease.input_artifact_version == 1
        lease.output_artifact_version = 2
    # Verify both writes happened
    with sqlite3.connect(migrated_db) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT * FROM pipeline_runs WHERE clip_id = 'test-clip-1' AND stage = 'editor'"
        ).fetchone()
        assert run["status"] == "succeeded"
        assert run["output_artifact_version"] == 2
        assert run["completed_at"] is not None
        artifact = conn.execute(
            "SELECT artifact_version FROM clip_artifacts WHERE clip_id = 'test-clip-1'"
        ).fetchone()
        assert artifact["artifact_version"] == 2


def test_lease_marks_failed_on_exception(migrated_db):
    class BoomError(Exception):
        pass
    with pytest.raises(BoomError):
        with stage_lease("test-clip-1", stage="writer", ttl_seconds=60) as lease:
            lease.output_artifact_version = 2
            raise BoomError("editor died mid-run")
    with sqlite3.connect(migrated_db) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT * FROM pipeline_runs WHERE clip_id = 'test-clip-1' AND stage = 'writer'"
        ).fetchone()
        assert run["status"] == "failed"
        assert "BoomError" in (run["failure_reason"] or "")
        assert "editor died" in (run["failure_reason"] or "")
        # artifact_version stayed at 1 (failed run didn't bump)
        artifact = conn.execute(
            "SELECT artifact_version FROM clip_artifacts WHERE clip_id = 'test-clip-1'"
        ).fetchone()
        assert artifact["artifact_version"] == 1


def test_lease_without_output_version_doesnt_bump(migrated_db):
    """If the caller never sets lease.output_artifact_version, the lease
    completes successfully but artifact_version stays put (some stages
    don't write a new artifact)."""
    with stage_lease("test-clip-1", stage="voice", ttl_seconds=60):
        pass  # no output_artifact_version set
    with sqlite3.connect(migrated_db) as conn:
        conn.row_factory = sqlite3.Row
        artifact = conn.execute(
            "SELECT artifact_version FROM clip_artifacts WHERE clip_id = 'test-clip-1'"
        ).fetchone()
        assert artifact["artifact_version"] == 1


# ---------- Concurrent claim conflict ----------

def test_second_concurrent_claim_raises_conflict(migrated_db):
    """Open one lease; try to open another for the same (clip, stage)
    while the first is still in_progress. The UNIQUE-active index fires
    inside BEGIN IMMEDIATE → LeaseConflict."""
    with stage_lease("test-clip-1", stage="editor", ttl_seconds=60):
        with pytest.raises(LeaseConflict, match="editor already in_progress"):
            with stage_lease("test-clip-1", stage="editor", ttl_seconds=60):
                pass  # never reached
    # After the outer lease completes, a fresh claim succeeds
    with stage_lease("test-clip-1", stage="editor", ttl_seconds=60) as lease:
        # The new lease is a separate run
        assert isinstance(lease, Lease)


def test_different_stages_can_run_concurrently(migrated_db):
    """Editor and Writer leases on the same clip don't conflict — only
    same-stage races are blocked."""
    with stage_lease("test-clip-1", stage="editor", ttl_seconds=60):
        with stage_lease("test-clip-1", stage="writer", ttl_seconds=60):
            pass  # both held simultaneously, no conflict


def test_stale_artifact_version_raises_and_marks_failed(migrated_db):
    """Codex 2026-05-18 fix: if another stage bumped artifact_version
    between our lease-start read and our lease-end write, the conditional
    UPDATE has rowcount=0 and we must raise (was: silently committed
    succeeded with the stale version pointer).

    Simulate the race: hold a lease, then have another connection bump
    artifact_version externally. When we try to bump on lease exit,
    the CAS fails."""
    with pytest.raises(StaleArtifactVersion, match="moved past"):
        with stage_lease("test-clip-1", stage="editor", ttl_seconds=60) as lease:
            # Simulate another stage racing ahead: directly bump artifact_version
            # to 2 while we hold the editor lease at input_version=1
            with sqlite3.connect(migrated_db) as conn:
                conn.execute(
                    "UPDATE clip_artifacts SET artifact_version = 2 WHERE clip_id = ?",
                    ("test-clip-1",),
                )
                conn.commit()
            lease.output_artifact_version = 2  # would-be 1→2, but DB is already 2

    # Verify the run row was marked failed (audit trail)
    with sqlite3.connect(migrated_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, failure_reason FROM pipeline_runs "
            "WHERE clip_id = 'test-clip-1' AND stage = 'editor' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["status"] == "failed"
        assert "stale_artifact_version" in (row["failure_reason"] or "")


def test_different_clips_can_run_same_stage_concurrently(migrated_db):
    """Two editor runs on different clips don't conflict."""
    # Seed a second clip
    with sqlite3.connect(migrated_db) as conn:
        conn.execute(
            """
            INSERT INTO clips_candidate (id, creator, source_platform, source_url, status)
            VALUES ('test-clip-2', 'IShowSpeed', 'youtube', 'https://t2', 'processing')
            """
        )
        conn.execute(
            "INSERT INTO clip_artifacts (clip_id, artifact_version) VALUES ('test-clip-2', 1)"
        )
        conn.commit()
    with stage_lease("test-clip-1", stage="editor", ttl_seconds=60):
        with stage_lease("test-clip-2", stage="editor", ttl_seconds=60):
            pass


# ---------- Janitor ----------

def test_sweep_expired_marks_in_progress_past_deadline(migrated_db):
    """A lease whose lease_expires_at is in the past should be swept to
    status='expired' by the janitor — this is what frees stuck rows."""
    # Manually insert an expired lease
    with sqlite3.connect(migrated_db) as conn:
        conn.execute(
            """
            INSERT INTO pipeline_runs
              (clip_id, stage, claimed_by, claimed_at, lease_expires_at,
               input_artifact_version, status)
            VALUES ('test-clip-1', 'editor', 'pid=999@host',
                    '2020-01-01 00:00:00', '2020-01-01 00:01:00',
                    1, 'in_progress')
            """
        )
        conn.commit()
    swept = sweep_expired_leases()
    assert swept == 1
    with sqlite3.connect(migrated_db) as conn:
        row = conn.execute(
            "SELECT status FROM pipeline_runs WHERE claimed_by = 'pid=999@host'"
        ).fetchone()
        assert row[0] == "expired"


def test_sweep_leaves_fresh_leases_alone(migrated_db):
    """A lease whose deadline is in the future should not be swept."""
    with stage_lease("test-clip-1", stage="editor", ttl_seconds=900):
        swept = sweep_expired_leases()
        assert swept == 0


# ---------- Codex 2026-05-18 P1#1: atomic commit_artifact ----------


def test_commit_artifact_writes_data_and_bumps_version_atomically(migrated_db):
    """The lease.commit_artifact() path replaces the broken pattern of
    'persist data in conn A, then bump version in conn B'. Now both
    happen in one BEGIN IMMEDIATE."""
    with stage_lease("test-clip-1", stage="editor", ttl_seconds=60) as lease:
        assert lease.input_artifact_version == 1

        def _persist(conn, new_version):
            conn.execute(
                """
                UPDATE clip_artifacts
                   SET artifact_version = ?,
                       transcript_json = ?,
                       updated_at = datetime('now')
                 WHERE clip_id = ?
                """,
                (new_version, '[{"text":"hi"}]', "test-clip-1"),
            )
        lease.commit_artifact(_persist)
        assert lease._artifact_committed is True
        assert lease.output_artifact_version == 2

    # Final state: artifact_version=2 AND transcript persisted in ONE write
    with sqlite3.connect(migrated_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT artifact_version, transcript_json FROM clip_artifacts WHERE clip_id = 'test-clip-1'"
        ).fetchone()
    assert row["artifact_version"] == 2
    assert "hi" in row["transcript_json"]


def test_commit_artifact_raises_stale_and_does_not_write_data(migrated_db):
    """The atomicity fix: when StaleArtifactVersion is detected, NO data
    lands. Codex's P1#1 finding was that the prior implementation
    committed the bytes in connection A before connection B's CAS
    discovered the race — so stale data piled up in clip_artifacts even
    though the lease was marked failed.

    Verify that after a stale-version raise, transcript_json is still
    NULL — the persist callback's UPDATE was rolled back atomically."""
    with pytest.raises(StaleArtifactVersion, match="raced past"):
        with stage_lease("test-clip-1", stage="editor", ttl_seconds=60) as lease:
            # Simulate concurrent stage bumping artifact_version past us
            with sqlite3.connect(migrated_db) as conn:
                conn.execute(
                    "UPDATE clip_artifacts SET artifact_version = 99 WHERE clip_id = ?",
                    ("test-clip-1",),
                )
                conn.commit()

            def _persist(conn, new_version):
                # This SHOULD NOT execute — version check fires first
                conn.execute(
                    "UPDATE clip_artifacts SET transcript_json = ? WHERE clip_id = ?",
                    ('"stale_data_that_must_not_land"', "test-clip-1"),
                )
            lease.commit_artifact(_persist)

    # Verify the stale data did NOT land
    with sqlite3.connect(migrated_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT transcript_json, artifact_version FROM clip_artifacts WHERE clip_id = 'test-clip-1'"
        ).fetchone()
    assert row["transcript_json"] is None
    # artifact_version was bumped by the simulated race, NOT by our (failed) commit
    assert row["artifact_version"] == 99


def test_commit_artifact_skips_legacy_cas_in_mark_done(migrated_db):
    """When commit_artifact runs, the lease's exit handler must NOT
    re-attempt the legacy CAS in _mark_done (which would find
    artifact_version=output, not output-1, and raise spuriously)."""
    with stage_lease("test-clip-1", stage="editor", ttl_seconds=60) as lease:
        def _persist(conn, new_version):
            conn.execute(
                "UPDATE clip_artifacts SET artifact_version = ? WHERE clip_id = ?",
                (new_version, "test-clip-1"),
            )
        lease.commit_artifact(_persist)
        # output_artifact_version is set; without skip_artifact_cas, the
        # exit handler would try to bump 1→2 a SECOND time and find no
        # row at version 1 (it's already 2) — StaleArtifactVersion would
        # fire spuriously. The skip_artifact_cas flag prevents that.

    # Verify the run completed succeeded (no spurious stale-version raise)
    with sqlite3.connect(migrated_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, output_artifact_version FROM pipeline_runs "
            "WHERE clip_id = 'test-clip-1' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["status"] == "succeeded"
    assert row["output_artifact_version"] == 2


def test_commit_artifact_rollback_on_persist_exception(migrated_db):
    """If persist_fn raises after the version-check passes but during
    the UPDATE, the whole transaction rolls back. No partial write."""
    with pytest.raises(RuntimeError, match="boom"):
        with stage_lease("test-clip-1", stage="editor", ttl_seconds=60) as lease:
            def _persist(conn, new_version):
                conn.execute(
                    "UPDATE clip_artifacts SET transcript_json = ? WHERE clip_id = ?",
                    ('"partial_write_must_rollback"', "test-clip-1"),
                )
                raise RuntimeError("boom — caller's fault")
            lease.commit_artifact(_persist)

    with sqlite3.connect(migrated_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT transcript_json, artifact_version FROM clip_artifacts WHERE clip_id = 'test-clip-1'"
        ).fetchone()
    assert row["transcript_json"] is None
    assert row["artifact_version"] == 1  # untouched


def test_sweep_after_already_expired_is_idempotent(migrated_db):
    """A second sweep should not flip already-expired leases again."""
    with sqlite3.connect(migrated_db) as conn:
        conn.execute(
            """
            INSERT INTO pipeline_runs
              (clip_id, stage, claimed_by, claimed_at, lease_expires_at,
               input_artifact_version, status)
            VALUES ('test-clip-1', 'editor', 'pid=999@host',
                    '2020-01-01 00:00:00', '2020-01-01 00:01:00',
                    1, 'in_progress')
            """
        )
        conn.commit()
    sweep_expired_leases()
    second = sweep_expired_leases()
    assert second == 0
