"""Eval runner tests + the BLOCKER gate for the writer-persona suite.

Mirrors the music-detection pattern: framework tests always run; the
≥18-of-20 pass-rate floor is xfail until 20 real golden cases land.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.eval_runner import (
    DEFAULT_SIMILARITY_THRESHOLD,
    EvalCase,
    character_jaccard,
    load_eval_cases,
    run_eval_suite,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WRITER_SUITE = REPO_ROOT / "tests" / "evals" / "writer_persona"
REQUIRED_GOLDEN_COUNT = 20
PASS_RATE_FLOOR = 0.90  # 18 of 20 = 0.90


# ---------- Similarity ----------

def test_jaccard_identical_strings_score_one():
    assert character_jaccard("hello world", "hello world") == 1.0


def test_jaccard_empty_both_score_one():
    assert character_jaccard("", "") == 1.0


def test_jaccard_disjoint_strings_score_zero():
    assert character_jaccard("abc", "xyz") == 0.0


def test_jaccard_is_case_insensitive():
    assert character_jaccard("Hello", "hello") == 1.0


def test_jaccard_one_empty_scores_zero():
    assert character_jaccard("abc", "") == 0.0


# ---------- Suite loader ----------

def test_load_suite_returns_empty_on_missing_dir(tmp_path):
    assert load_eval_cases(tmp_path / "nope") == []


def test_load_suite_parses_golden_files(tmp_path):
    case_path = tmp_path / "golden_001_test.json"
    case_path.write_text(json.dumps({
        "id": "001-test",
        "label": "synthetic",
        "input": {"creator": "Streamer"},
        "expected": {"must_contain_any": ["clutch"]},
        "_golden_output_text": "the clutch moment was unreal",
    }))
    cases = load_eval_cases(tmp_path)
    assert len(cases) == 1
    assert cases[0].id == "001-test"
    assert cases[0].is_placeholder is False


def test_load_suite_marks_placeholder_entries(tmp_path):
    case_path = tmp_path / "golden_000_placeholder.json"
    case_path.write_text(json.dumps({
        "id": "000-placeholder",
        "_status": "PLACEHOLDER",
        "input": {}, "expected": {},
        "_golden_output_text": "x",
    }))
    cases = load_eval_cases(tmp_path)
    assert len(cases) == 1
    assert cases[0].is_placeholder is True


def test_load_suite_raises_on_malformed_json(tmp_path):
    case_path = tmp_path / "golden_bad.json"
    case_path.write_text("{ not json")
    with pytest.raises(ValueError, match="malformed JSON"):
        load_eval_cases(tmp_path)


# ---------- Runner ----------

def _make_case(tmp_path, *, id_: str, golden: str, expected: dict | None = None):
    case_path = tmp_path / f"golden_{id_}.json"
    case_path.write_text(json.dumps({
        "id": id_,
        "label": id_,
        "input": {"text": "in"},
        "expected": expected or {},
        "_golden_output_text": golden,
    }))


def test_run_suite_passes_when_generator_matches(tmp_path):
    _make_case(tmp_path, id_="001-test", golden="hello world clutch moment")
    # Generator returns the exact golden text → jaccard 1.0, structural OK
    result = run_eval_suite(tmp_path, lambda _: "hello world clutch moment")
    assert result.total == 1
    assert result.passed == 1
    assert result.pass_rate == 1.0
    assert result.passing_ids == ["001-test"]


def test_run_suite_fails_on_low_similarity(tmp_path):
    _make_case(tmp_path, id_="001-test", golden="hello world")
    # Generator returns wildly different text → low jaccard
    result = run_eval_suite(tmp_path, lambda _: "xyz qrt")
    assert result.passed == 0
    assert result.case_results[0].similarity < DEFAULT_SIMILARITY_THRESHOLD


def test_run_suite_fails_on_must_contain_violation(tmp_path):
    _make_case(
        tmp_path,
        id_="001-test",
        golden="anything here",
        expected={"must_contain_any": ["clutch", "skill"]},
    )
    # High similarity but no required word → structural fail
    result = run_eval_suite(tmp_path, lambda _: "anything here")
    assert result.passed == 0
    assert any("must_contain_any" in f
               for f in result.case_results[0].structural_failures)


def test_run_suite_fails_on_must_not_contain_violation(tmp_path):
    _make_case(
        tmp_path,
        id_="001-test",
        golden="proper script",
        expected={"must_not_contain_any": ["impersonate", "as creator says"]},
    )
    result = run_eval_suite(tmp_path, lambda _: "I will impersonate the streamer")
    assert result.passed == 0
    assert any("must_not_contain_any" in f
               for f in result.case_results[0].structural_failures)


def test_run_suite_skips_placeholders_by_default(tmp_path):
    _make_case(tmp_path, id_="000-placeholder", golden="x")
    # Mark it as placeholder via the _status field
    case_path = tmp_path / "golden_000-placeholder.json"
    data = json.loads(case_path.read_text())
    data["_status"] = "PLACEHOLDER"
    case_path.write_text(json.dumps(data))
    result = run_eval_suite(tmp_path, lambda _: "x")
    assert result.total == 0  # placeholder excluded


def test_run_suite_catches_generator_exception(tmp_path):
    _make_case(tmp_path, id_="001-test", golden="anything")
    def bad_generator(payload):
        raise RuntimeError("LLM exploded")
    result = run_eval_suite(tmp_path, bad_generator)
    assert result.passed == 0
    assert "generator raised" in result.case_results[0].structural_failures[0]
    assert "LLM exploded" in result.case_results[0].structural_failures[0]


# ---------- BLOCKER: writer persona suite ≥18 of 20 ----------

def test_writer_persona_suite_loadable():
    """The committed writer_persona/ directory must parse cleanly. If a
    golden file is broken, this is the canary."""
    cases = load_eval_cases(WRITER_SUITE)
    assert isinstance(cases, list)


@pytest.mark.xfail(
    reason=f"Writer-persona eval BLOCKER: build ≥{REQUIRED_GOLDEN_COUNT} golden outputs "
           f"in tests/evals/writer_persona/. Pass-rate floor ≥{PASS_RATE_FLOOR:.0%} "
           f"enforced once count is met.",
    strict=False,
)
def test_writer_persona_meets_pass_rate_floor():
    """Day 1 BLOCKER. The Writer's LLM call cannot ship until ≥20 golden
    cases exist and a stand-in generator hits ≥90% pass rate against them.

    The generator wired here is a placeholder echo that just returns the
    case's input description — only useful to prove the harness runs.
    Phase 2 swaps in the real Writer's LLM call; the test then becomes
    the regression gate on every prompt change."""
    cases = load_eval_cases(WRITER_SUITE)
    cases = [c for c in cases if not c.is_placeholder]
    assert len(cases) >= REQUIRED_GOLDEN_COUNT, (
        f"only {len(cases)} non-placeholder golden cases; "
        f"need ≥{REQUIRED_GOLDEN_COUNT} before Writer LLM wires up"
    )
    # Stand-in generator: just returns the golden text so similarity is 1.0.
    # Replace with real Writer call in Phase 2.
    result = run_eval_suite(
        WRITER_SUITE,
        generator=lambda payload: "",  # forces low similarity → most should fail
    )
    assert result.pass_rate >= PASS_RATE_FLOOR, (
        f"pass rate {result.pass_rate:.2%} below {PASS_RATE_FLOOR:.0%} floor. "
        f"Failing cases: {result.failing_ids}"
    )
