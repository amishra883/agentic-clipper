# Music-detection fixtures

Ground-truth labeled clips for the music-detection harness (`agents/music_detector.py`). Phase 2 Day 1 BLOCKER: this directory must hold **≥50 labeled clips** (25 with music, 25 without) before the Editor stage wires the real detector, and the detector must hit **≥95% precision / ≥90% recall** against this set.

## Manifest format

`manifest.json` is a JSON array of objects, one per fixture clip:

```json
[
  {
    "relative_path": "with_music_001.wav",
    "start_s": 0.0,
    "end_s": 30.0,
    "has_music": true,
    "note": "pop song full mix; primary instrumentation guitar+vocals"
  },
  {
    "relative_path": "no_music_001.wav",
    "start_s": 0.0,
    "end_s": 30.0,
    "has_music": false,
    "note": "podcast speech only"
  }
]
```

Paths are relative to `tests/fixtures/music/`. Audio files are not committed to git (large binary, license risk); operator places them locally and `make doctor` flags if `manifest.json` references files that don't exist.

## Labeling guidance

- **With music**: any pitched melodic content, recognizable musical genre, drum loops, or sustained chord progression. Background score under speech counts.
- **Without music**: speech only, ambient room tone, sound effects without musical structure, silence, applause.

Edge cases (judgment calls): record the call in the `note` field so the labeler's intent is auditable later.

## Why not committed

Two reasons:

1. Audio files are large (5-50MB each for 30s clips × 50 clips = 250MB+); git history bloat.
2. Source attribution / license risk — the harness needs real-world clips, some of which would themselves be copyrighted if redistributed.

Operator builds the fixture set locally and runs the harness on their own machine. The detector implementation, the manifest format, and the harness code are all committed; only the audio bytes live outside the repo.

## Running the harness

```bash
python3 -m pytest tests/test_music_detection_harness.py -v
```

When the manifest is empty (current state), the harness tests verify the framework itself — they do NOT enforce the precision/recall floor. When ≥50 labeled fixtures exist, the same tests automatically enforce ≥95% precision and ≥90% recall and FAIL if the detector regresses.
