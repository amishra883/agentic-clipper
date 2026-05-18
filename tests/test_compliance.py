"""Compliance gate unit tests.

These encode the load-bearing fair-use rules from docs/fair_use_position.md.
Under the operator's fair-use-only posture (2026-05-14), the Compliance gate
is the SOLE legal defense — these tests MUST pass on every commit.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agents import compliance


# ---------- Happy path ----------

def test_good_clip_passes_all_rules(good_clip):
    result = compliance.evaluate(good_clip)
    assert result.passed is True
    assert result.blocked_reason is None
    assert all(r["passed"] for r in result.rule_results.values())


# ---------- Hard-cap rules ----------

def test_source_over_30s_blocks(good_clip):
    clip = replace(good_clip, source_segment_duration_s=30.01)
    result = compliance.evaluate(clip)
    assert result.passed is False
    assert result.rule_results["source_max_30s"]["passed"] is False


def test_source_exactly_30s_passes(good_clip):
    clip = replace(good_clip, source_segment_duration_s=30.0)
    result = compliance.evaluate(clip)
    assert result.rule_results["source_max_30s"]["passed"] is True


def test_total_over_60s_blocks(good_clip):
    clip = replace(good_clip, final_duration_s=60.01)
    result = compliance.evaluate(clip)
    assert result.passed is False
    assert result.rule_results["total_max_60s"]["passed"] is False


def test_total_exactly_60s_passes(good_clip):
    clip = replace(good_clip, final_duration_s=60.0, commentary_audio_duration_s=35.0)
    result = compliance.evaluate(clip)
    assert result.rule_results["total_max_60s"]["passed"] is True


# ---------- Commentary ratio ----------

def test_commentary_exactly_50pct_passes(good_clip):
    clip = replace(
        good_clip,
        final_duration_s=50.0,
        commentary_audio_duration_s=25.0,  # exactly 50%
    )
    result = compliance.evaluate(clip)
    assert result.rule_results["commentary_at_least_50pct"]["passed"] is True


def test_commentary_below_50pct_blocks(good_clip):
    clip = replace(
        good_clip,
        final_duration_s=50.0,
        commentary_audio_duration_s=24.99,
    )
    result = compliance.evaluate(clip)
    assert result.passed is False
    assert result.rule_results["commentary_at_least_50pct"]["passed"] is False


def test_zero_commentary_blocks(good_clip):
    clip = replace(good_clip, commentary_audio_duration_s=0.0)
    result = compliance.evaluate(clip)
    assert result.passed is False


def test_zero_duration_blocks(good_clip):
    # finite-durations rule should fail first
    clip = replace(good_clip, final_duration_s=0.0)
    result = compliance.evaluate(clip)
    assert result.passed is False
    assert result.rule_results["finite_durations"]["passed"] is False


# ---------- Music / real-face / voice ----------

def test_music_in_source_blocks(good_clip):
    clip = replace(good_clip, has_music_in_source_segment=True)
    result = compliance.evaluate(clip)
    assert result.passed is False
    assert result.rule_results["no_music_in_source_segment"]["passed"] is False


def test_real_face_reference_blocks(good_clip):
    clip = replace(good_clip, has_real_face_reference=True)
    result = compliance.evaluate(clip)
    assert result.passed is False
    assert result.rule_results["no_real_face_seedance_reference"]["passed"] is False


def test_unknown_voice_engine_blocks(good_clip, good_audio_track):
    bad_audio = replace(good_audio_track, engine="cloned_from_source")
    clip = replace(good_clip, audio_track=bad_audio)
    result = compliance.evaluate(clip)
    assert result.passed is False
    assert result.rule_results["no_voice_clone_of_source_creator"]["passed"] is False


def test_elevenlabs_voice_engine_passes(good_clip, good_audio_track):
    el_audio = replace(good_audio_track, engine="elevenlabs")
    clip = replace(good_clip, audio_track=el_audio)
    result = compliance.evaluate(clip)
    assert result.rule_results["no_voice_clone_of_source_creator"]["passed"] is True


def test_unknown_music_evidence_fails_closed(good_clip):
    """Compositor used to hardcode has_music_in_source_segment=False, which
    masked the absence of a real music detector. Tri-state evidence must fail
    closed when the upstream stage has not actually checked."""
    clip = replace(good_clip, has_music_in_source_segment=None)
    result = compliance.evaluate(clip)
    assert result.passed is False
    assert result.rule_results["no_music_in_source_segment"]["passed"] is False
    assert "unknown" in result.rule_results["no_music_in_source_segment"]["detail"]


def test_unknown_real_face_evidence_fails_closed(good_clip):
    clip = replace(good_clip, has_real_face_reference=None)
    result = compliance.evaluate(clip)
    assert result.passed is False
    assert result.rule_results["no_real_face_seedance_reference"]["passed"] is False
    assert "unknown" in result.rule_results["no_real_face_seedance_reference"]["detail"]


def test_missing_voice_id_blocks(good_clip, good_audio_track):
    """A missing AudioTrack.voice_id is treated as unconfigured Voice stage —
    fail closed, since an empty value can't prove "not a source-creator clone"."""
    bad_audio = replace(good_audio_track, voice_id=None)
    clip = replace(good_clip, audio_track=bad_audio)
    result = compliance.evaluate(clip)
    assert result.passed is False
    assert result.rule_results["no_voice_clone_of_source_creator"]["passed"] is False


def test_unapproved_voice_id_blocks(good_clip, good_audio_track):
    """voice_id outside the active persona's approved_voice_ids whitelist
    blocks. This is the structural guard against an ElevenLabs custom voice
    trained on a source creator slipping through (because its ID would never
    be in the whitelist)."""
    bad_audio = replace(good_audio_track, voice_id="ishowspeed_clone_v1")
    clip = replace(good_clip, audio_track=bad_audio)
    result = compliance.evaluate(clip)
    assert result.passed is False
    assert result.rule_results["no_voice_clone_of_source_creator"]["passed"] is False


# ---------- Attribution + transformative purpose ----------

def test_missing_attribution_blocks(good_clip):
    clip = replace(good_clip, description="Just some clip with no creator name.")
    result = compliance.evaluate(clip)
    # Attribution check requires the creator name in the description
    assert result.rule_results["attribution_present"]["passed"] is False


def test_missing_transformative_marker_blocks(good_clip):
    clip = replace(
        good_clip,
        description="IShowSpeed had a moment here, watch this.",  # no marker word
    )
    result = compliance.evaluate(clip)
    assert result.rule_results["transformative_purpose_disclosed"]["passed"] is False


@pytest.mark.parametrize("marker", ["commentary", "reaction", "analysis", "critique", "review"])
def test_each_transformative_marker_satisfies_rule(good_clip, marker):
    clip = replace(
        good_clip,
        description=f"IShowSpeed clip {marker} — AI-generated visuals included.",
    )
    result = compliance.evaluate(clip)
    assert result.rule_results["transformative_purpose_disclosed"]["passed"] is True


# ---------- AI-content label ----------

def test_no_generated_visuals_skips_ai_label_rule(good_clip):
    clip = replace(
        good_clip,
        visuals_used=[],
        description="Commentary on IShowSpeed's stream moment.",
    )
    result = compliance.evaluate(clip)
    assert result.rule_results["ai_content_label_when_applicable"]["passed"] is True


def test_generated_visuals_without_ai_label_blocks(good_clip):
    clip = replace(
        good_clip,
        description="Commentary on IShowSpeed's stream moment.",  # no AI marker
    )
    result = compliance.evaluate(clip)
    assert result.rule_results["ai_content_label_when_applicable"]["passed"] is False


@pytest.mark.parametrize(
    "marker",
    ["AI-generated", "AI generated", "synthetic", "AI visuals", "AI-assisted", "AI assisted"],
)
def test_ai_label_markers(good_clip, marker):
    clip = replace(
        good_clip,
        description=f"Commentary on IShowSpeed clip — {marker} visuals.",
    )
    result = compliance.evaluate(clip)
    assert result.rule_results["ai_content_label_when_applicable"]["passed"] is True


# ---------- Persistence ----------

def test_gate_records_passing_result_to_db(good_clip, temp_db):
    import sqlite3
    result = compliance.gate(good_clip)
    assert result.passed is True

    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM compliance_results WHERE clip_id = ?",
            (good_clip.clip_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["passed"] == 1
    assert rows[0]["blocked_reason"] is None


def test_gate_records_failing_result_to_db(good_clip, temp_db):
    import sqlite3
    bad_clip = replace(good_clip, source_segment_duration_s=45.0)
    result = compliance.gate(bad_clip)
    assert result.passed is False

    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM compliance_results WHERE clip_id = ?",
            (bad_clip.clip_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["passed"] == 0
    assert "source_max_30s" in (rows[0]["blocked_reason"] or "")


def test_gate_writes_event_log(good_clip, temp_db):
    import sqlite3
    compliance.gate(good_clip)

    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        events = conn.execute(
            "SELECT * FROM events WHERE clip_id = ? AND agent = 'compliance'",
            (good_clip.clip_id,),
        ).fetchall()
    assert len(events) == 1
    assert events[0]["event_type"] == "clip_evaluated"
    assert events[0]["level"] == "info"


def test_gate_event_level_blocked_on_failure(good_clip, temp_db):
    import sqlite3
    bad_clip = replace(good_clip, has_music_in_source_segment=True)
    compliance.gate(bad_clip)

    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        events = conn.execute(
            "SELECT level FROM events WHERE clip_id = ? AND agent = 'compliance'",
            (bad_clip.clip_id,),
        ).fetchall()
    assert events[0]["level"] == "blocked"


# ---------- Multiple failures aggregate ----------

def test_multiple_failures_listed_in_blocked_reason(good_clip):
    clip = replace(
        good_clip,
        source_segment_duration_s=45.0,         # fails source_max_30s
        has_music_in_source_segment=True,       # fails no_music
        has_real_face_reference=True,           # fails no_real_face
    )
    result = compliance.evaluate(clip)
    assert result.passed is False
    assert "source_max_30s" in result.blocked_reason
    assert "no_music_in_source_segment" in result.blocked_reason
    assert "no_real_face_seedance_reference" in result.blocked_reason
