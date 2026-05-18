"""LLM eval runner — regression-tests persona prompt changes against golden outputs.

Day 1 BLOCKER per the revised Phase 2 plan (Eng review "LLM eval suite").
Before any Writer LLM-prompt change ships, the suite asserts the new prompt
matches ≥18 of 20 locked golden outputs within the similarity threshold.
Without this, every persona tweak is uncontrolled blast radius.

Calling contract
----------------
    >>> from agents.eval_runner import run_eval_suite, EvalCase
    >>> result = run_eval_suite(
    ...     suite_dir=REPO_ROOT / "tests" / "evals" / "writer_persona",
    ...     generator=my_writer_callable,  # input dict → str
    ... )
    >>> result.pass_rate
    0.92  # 0.0-1.0
    >>> result.passing_ids
    ['001-streamer-fail', '002-dunk-reaction', ...]

The runner is generator-agnostic. The caller injects a callable that
takes the eval case's `input` dict and returns text. For Phase 1 this is
a stub (the placeholder script generator); Phase 2 swaps in the real
Writer's LLM call.

Similarity
----------
Phase 1: character-overlap Jaccard (cheap, deterministic, no deps).
Phase 2 swap-in: sentence-embedding cosine similarity. Contract stays the
same — `0.0..1.0` score, threshold comparison in `run_eval_suite`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Phase 1 default; tightened when real similarity (embeddings) lands.
# Word-Jaccard is stricter than character-Jaccard for the same threshold —
# this 0.4 catches "similar but reordered" as similar, but rejects
# "same alphabet, completely different words" which character-Jaccard
# missed (Codex finding 2026-05-18).
DEFAULT_SIMILARITY_THRESHOLD = 0.4

# Tokenizer for similarity scoring. Splits on whitespace + punctuation
# boundaries, lowercases, drops empty tokens. Cheap and deterministic.
_WORD_RE = re.compile(r"[A-Za-z0-9']+")


@dataclass
class EvalCase:
    id: str
    label: str
    input_payload: dict
    expected: dict
    golden_text: str
    is_placeholder: bool = False


@dataclass
class EvalCaseResult:
    case_id: str
    passed: bool
    similarity: float
    structural_failures: list[str] = field(default_factory=list)


@dataclass
class EvalSuiteResult:
    total: int
    passed: int
    failed: int
    pass_rate: float
    passing_ids: list[str]
    failing_ids: list[str]
    case_results: list[EvalCaseResult]


# ---------- Similarity ----------

def _tokenize(text: str) -> set[str]:
    return {tok.lower() for tok in _WORD_RE.findall(text or "")}


def word_jaccard(a: str, b: str) -> float:
    """Word-level Jaccard similarity. Tokenizes on word boundaries,
    lowercases, returns |intersection| / |union|.

    Replaces the prior character-set Jaccard (Codex finding 2026-05-18:
    char-set ignored order AND frequency, so "abc def ghi" and "ghi cba fed"
    scored 1.0). Word-set still ignores order and frequency but the unit
    of comparison is meaningful — sentences with the same words in any
    order DO carry the same persona content; the persona-stability check
    cares about content overlap, not exact phrasing.

    Phase 2 replaces this with embedding cosine; the function signature
    (two strings → float) stays the same so call sites don't move.
    """
    if not a and not b:
        return 1.0
    set_a = _tokenize(a)
    set_b = _tokenize(b)
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union > 0 else 0.0


# Back-compat alias; existing tests use this name. Delete after Phase 2
# embedding swap when call sites update.
character_jaccard = word_jaccard


# ---------- Suite loader ----------

def load_eval_cases(suite_dir: Path) -> list[EvalCase]:
    """Discover golden_*.json files in suite_dir. Skips files starting
    with `_` (notes / READMEs masquerading as JSON, if any)."""
    if not suite_dir.exists():
        return []
    cases: list[EvalCase] = []
    for path in sorted(suite_dir.glob("golden_*.json")):
        if path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(f"eval case {path.name}: malformed JSON: {exc}") from exc
        is_placeholder = (
            data.get("_status") == "PLACEHOLDER"
            or data.get("id", "").startswith("000-placeholder")
        )
        cases.append(EvalCase(
            id=data["id"],
            label=data.get("label", ""),
            input_payload=data.get("input", {}),
            expected=data.get("expected", {}),
            golden_text=data.get("_golden_output_text", ""),
            is_placeholder=is_placeholder,
        ))
    return cases


# ---------- Structural checks ----------

def _check_structural(generated_text: str, expected: dict) -> list[str]:
    """Return list of failure descriptions. Empty list = all structural
    expectations passed.

    These are the persona-stability invariants: substance tags, trending
    refs used, must-contain / must-not-contain phrases. Phase 1 only
    enforces the substring checks because we don't have the Script
    metadata (substance_tags etc.) on a plain str return. Phase 2 swaps
    `generated_text` for a full Script and adds the metadata checks.
    """
    failures: list[str] = []
    must_contain_any = expected.get("must_contain_any") or []
    if must_contain_any:
        text_lower = generated_text.lower()
        hits = [w for w in must_contain_any if w.lower() in text_lower]
        if not hits:
            failures.append(
                f"must_contain_any expected one of {must_contain_any}; got none"
            )
    must_not_contain_any = expected.get("must_not_contain_any") or []
    if must_not_contain_any:
        text_lower = generated_text.lower()
        bad_hits = [w for w in must_not_contain_any if w.lower() in text_lower]
        if bad_hits:
            failures.append(f"must_not_contain_any was violated by: {bad_hits}")
    return failures


# ---------- Runner ----------

def run_eval_suite(
    suite_dir: Path,
    generator: Callable[[dict], str],
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    skip_placeholders: bool = True,
) -> EvalSuiteResult:
    """Apply `generator` to every eval case's input, score it, return summary.

    `generator(input_dict) -> str` is the function under test. Errors raised
    by the generator are caught and recorded as a structural failure, not
    propagated — one bad case shouldn't tank the whole suite.
    """
    cases = load_eval_cases(suite_dir)
    if skip_placeholders:
        cases = [c for c in cases if not c.is_placeholder]

    case_results: list[EvalCaseResult] = []
    for case in cases:
        try:
            generated_text = generator(case.input_payload)
        except Exception as exc:  # pragma: no cover — defensive
            case_results.append(EvalCaseResult(
                case_id=case.id,
                passed=False,
                similarity=0.0,
                structural_failures=[f"generator raised: {exc.__class__.__name__}: {exc}"],
            ))
            continue

        structural_failures = _check_structural(generated_text, case.expected)
        similarity = word_jaccard(generated_text, case.golden_text)
        passed = not structural_failures and similarity >= similarity_threshold
        case_results.append(EvalCaseResult(
            case_id=case.id,
            passed=passed,
            similarity=similarity,
            structural_failures=structural_failures,
        ))

    total = len(case_results)
    passing = [r.case_id for r in case_results if r.passed]
    failing = [r.case_id for r in case_results if not r.passed]
    return EvalSuiteResult(
        total=total,
        passed=len(passing),
        failed=len(failing),
        pass_rate=len(passing) / total if total > 0 else 0.0,
        passing_ids=passing,
        failing_ids=failing,
        case_results=case_results,
    )
