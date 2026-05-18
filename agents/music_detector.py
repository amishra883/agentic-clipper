"""Music-detection module — populates `clip_artifacts.has_music_in_source_segment`.

Phase 1 Compliance is fail-closed on this column (per
`agents/compliance.py:55-69`). If music slips through the detector and gets
labeled False, the Compliance gate ships a clip whose source contains
copyrighted music — direct Content ID strike. The Phase 2 plan's Day 1
BLOCKER (Eng review E-6) requires this detector to achieve **≥95% precision
and ≥90% recall** against a labeled validation set before Editor wiring.

Why a separate module
---------------------
- Editor wires this in, but the detector is independently testable
- The detector implementation will probably swap (PANNs / spectral / MUSDB
  fine-tune) as accuracy data comes in; the calling contract stays stable
- The validation harness in tests/ asserts the precision floor on every
  commit that touches the detector

Calling contract
----------------
    >>> from agents.music_detector import detect_music_in_segment
    >>> result = detect_music_in_segment(
    ...     audio_path=Path("data/clips/raw/2026-05-18-1200-xyz.mp4"),
    ...     start_s=12.5,
    ...     end_s=42.5,
    ... )
    >>> result.has_music
    False
    >>> result.confidence
    0.94
    >>> result.method
    'placeholder-energy'

`has_music` is the boolean that flows into clip_artifacts. `confidence` is
the detector's per-call signal. `method` identifies which detector
implementation ran — useful for A/B-ing detector swaps in the analyst layer.

Current implementation
----------------------
Placeholder energy-based detector (see _detect_placeholder below). NOT
production-ready — exists only so the harness can be tested end-to-end
and the calling stages can be wired. Real implementation lands when the
operator picks between PANNs, the spectral approach, and a fine-tune;
the contract above does not change.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DetectorMethod = Literal["placeholder-energy", "panns-tagging", "spectral-bandwidth"]


@dataclass
class MusicDetectionResult:
    has_music: bool
    confidence: float          # 0.0-1.0, detector's self-reported confidence
    method: DetectorMethod
    notes: str = ""            # free-form, for audit log


# ---------- Detector dispatch ----------

def detect_music_in_segment(
    audio_path: Path,
    start_s: float,
    end_s: float,
    *,
    method: DetectorMethod | None = None,
    allow_unsafe_placeholder: bool = False,
) -> MusicDetectionResult:
    """Public entrypoint. Caller (Editor) gets the result + writes the
    boolean to clip_artifacts.has_music_in_source_segment.

    SAFETY GATE (Codex finding 2026-05-18):
        `method=None` (the default) raises. Callers must explicitly choose
        a detector. The placeholder-energy detector returns has_music=False
        on ~75% of files based on a hash of (path, segment) — using it as
        a silent default would feed false "no music" evidence into the
        Compliance gate, which is the exact failure mode E-6 exists to
        prevent. Phase 2's Editor must wire a real detector OR explicitly
        opt into the placeholder with `allow_unsafe_placeholder=True`
        (test harnesses only).

        The placeholder check requires `allow_unsafe_placeholder=True`
        so a stray test fixture cannot slip into production use; tests
        pass the flag, production code does not.
    """
    if method is None:
        raise ValueError(
            "music_detector: no method specified. Production code must pass a real "
            "detector method (Phase 2: 'panns-tagging' / 'spectral-bandwidth'). "
            "Test code may pass method='placeholder-energy' AND "
            "allow_unsafe_placeholder=True."
        )
    if method == "placeholder-energy" and not allow_unsafe_placeholder:
        raise ValueError(
            "music_detector: placeholder-energy is not safe for production use "
            "(returns False on ~75% of files via deterministic hash, not real "
            "audio analysis). Pass allow_unsafe_placeholder=True from test code."
        )

    if not audio_path.exists():
        return MusicDetectionResult(
            has_music=True,  # fail-CLOSED: assume music if we can't even read the file
            confidence=0.0,
            method=method,
            notes=f"audio_path missing: {audio_path}",
        )
    if end_s <= start_s:
        return MusicDetectionResult(
            has_music=True,
            confidence=0.0,
            method=method,
            notes=f"invalid segment: start={start_s} end={end_s}",
        )

    if method == "placeholder-energy":
        return _detect_placeholder(audio_path, start_s, end_s)
    raise NotImplementedError(f"detector method {method!r} not yet wired (Phase 2)")


# ---------- Placeholder implementation ----------

def _detect_placeholder(audio_path: Path, start_s: float, end_s: float) -> MusicDetectionResult:
    """Stable, deterministic stub. Used ONLY for harness tests so the
    validation framework can be exercised against a labeled fixture set
    without dragging in a real ML dependency.

    Decision rule: classify based on a deterministic hash of (file path,
    segment bounds) so the same fixture always produces the same answer
    across test runs. Real detectors replace this; the harness then
    measures whether they beat the precision/recall floor.

    The placeholder's "decision" has no relation to actual audio content
    — it's there so the test scaffolding has something to call. Tests
    must therefore mark fixtures with explicit ground-truth labels in
    the manifest, not rely on the detector being correct.
    """
    h = hashlib.sha256(f"{audio_path}|{start_s}|{end_s}".encode()).hexdigest()
    # Use the first hex char as a 1-in-16 entropy source for the stub.
    # This makes the placeholder return False on most fixtures (low collision
    # with "music = True" ground truth), so the harness can show a low
    # precision/recall and prove the harness itself works.
    val = int(h[0], 16)
    has_music = val >= 12  # 4/16 = 25% positive rate
    confidence = 0.5 + (val - 8) / 32  # somewhere around 0.4-0.7
    return MusicDetectionResult(
        has_music=has_music,
        confidence=max(0.0, min(1.0, confidence)),
        method="placeholder-energy",
        notes="placeholder; replace before any Phase 2 live run",
    )


# ---------- Validation harness ----------

@dataclass
class FixtureLabel:
    relative_path: str         # path under tests/fixtures/music/
    start_s: float
    end_s: float
    has_music: bool            # ground truth
    note: str = ""             # free-form: what kind of music, what the segment is


def evaluate_detector(
    fixtures: list[FixtureLabel],
    fixtures_root: Path,
    *,
    method: DetectorMethod = "placeholder-energy",
    allow_unsafe_placeholder: bool = True,
) -> dict:
    """Run the detector against every fixture and return precision/recall.

    SAFETY (Codex finding 2026-05-18): preflight every fixture path.
    Without preflight, missing audio files trip the fail-closed path
    inside `detect_music_in_segment` (returns has_music=True). If the
    fixture set is all has_music=True labels and all files are missing,
    every result is a TP and precision/recall reports 1.0 — fake perfect
    score on zero real audio. Preflight makes this impossible to fake.

    Also requires labels to be balanced (≥1 positive AND ≥1 negative)
    so precision and recall both have non-zero denominators. An all-
    positive set would yield recall=N/N=1.0 and undefined precision
    (no FPs possible) — misleading.

    Definitions:
      - TP: ground-truth music AND detector says music
      - FP: ground-truth no-music AND detector says music
      - FN: ground-truth music AND detector says no-music
      - TN: ground-truth no-music AND detector says no-music
      - Precision = TP / (TP + FP)
      - Recall    = TP / (TP + FN)

    Compliance cares about recall MORE than precision: a false negative
    means a music-laced clip ships → Content ID strike. A false positive
    means a music-free clip is needlessly quarantined → operator workload
    but no legal risk.
    """
    # Preflight: every fixture path must exist. Empty fixture list is OK
    # (caller's xfail handles the "no fixtures yet" framework case).
    missing_paths: list[str] = []
    for fx in fixtures:
        audio_path = fixtures_root / fx.relative_path
        if not audio_path.exists():
            missing_paths.append(fx.relative_path)
    if missing_paths:
        raise FileNotFoundError(
            f"evaluate_detector preflight: {len(missing_paths)} fixture file(s) "
            f"missing under {fixtures_root}: {missing_paths[:5]}"
            + (f" ... ({len(missing_paths) - 5} more)" if len(missing_paths) > 5 else "")
        )

    # Require label balance so precision and recall both have meaning.
    if fixtures:
        positives = sum(1 for fx in fixtures if fx.has_music)
        negatives = len(fixtures) - positives
        if positives == 0:
            raise ValueError(
                "evaluate_detector: fixture set has zero positive labels "
                "(has_music=True). Recall is undefined; add some music fixtures."
            )
        if negatives == 0:
            raise ValueError(
                "evaluate_detector: fixture set has zero negative labels "
                "(has_music=False). Precision can't catch false positives; add "
                "some no-music fixtures."
            )

    tp = fp = fn = tn = 0
    per_fixture = []
    for fx in fixtures:
        audio_path = fixtures_root / fx.relative_path
        result = detect_music_in_segment(
            audio_path, fx.start_s, fx.end_s,
            method=method,
            allow_unsafe_placeholder=allow_unsafe_placeholder,
        )
        predicted = result.has_music
        actual = fx.has_music
        if actual and predicted:
            tp += 1
            outcome = "TP"
        elif not actual and predicted:
            fp += 1
            outcome = "FP"
        elif actual and not predicted:
            fn += 1
            outcome = "FN"
        else:
            tn += 1
            outcome = "TN"
        per_fixture.append({
            "fixture": fx.relative_path,
            "ground_truth": actual,
            "predicted": predicted,
            "confidence": result.confidence,
            "outcome": outcome,
        })
    total = tp + fp + fn + tn
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return {
        "total": total,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision,
        "recall": recall,
        "method": method,
        "per_fixture": per_fixture,
    }


def load_fixture_manifest(manifest_path: Path) -> list[FixtureLabel]:
    """Parse tests/fixtures/music/manifest.json (or equivalent path).

    Schema:
      [
        {"relative_path": "with_music_001.wav",
         "start_s": 0.0, "end_s": 30.0,
         "has_music": true,
         "note": "pop song, full mix"},
        ...
      ]
    """
    import json
    if not manifest_path.exists():
        return []
    raw = json.loads(manifest_path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"manifest must be a JSON list, got {type(raw).__name__}")
    return [
        FixtureLabel(
            relative_path=entry["relative_path"],
            start_s=float(entry["start_s"]),
            end_s=float(entry["end_s"]),
            has_music=bool(entry["has_music"]),
            note=str(entry.get("note", "")),
        )
        for entry in raw
    ]
