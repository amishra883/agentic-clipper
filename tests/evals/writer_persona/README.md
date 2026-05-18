# Writer persona eval suite

Day 1 BLOCKER (revised Phase 2 plan): before the Writer's LLM call ships, this directory must hold **20 golden outputs** from the locked persona prompt, and any new prompt must match **≥18 of them** within the similarity threshold.

## What this catches

Phase 2 Writer (`agents/writer.py:_llm_generate_script`) sends a persona prompt + scene context + sanitized trending refs to Claude and gets back a `Script`. The persona prompt is in `config/persona.yaml` and will be tuned over time. Without an eval suite, every prompt change is an uncontrolled blast radius — the operator can't tell "this prompt change shifted the persona toward boring" until weeks of underperforming clips ship.

The eval suite is the regression net: it asserts that the structural and stylistic properties the locked persona promises (substance tags, trending refs, punch density, do-not avoidance) hold across a fixed input set. A new prompt either passes ≥18/20 or doesn't ship.

## Format

Each golden output is a JSON file in this directory: `golden_NNN_short_label.json`.

```json
{
  "id": "001-streamer-fail",
  "label": "Streamer fails at clutch moment in Valorant",
  "input": {
    "creator": "Sketch",
    "source_excerpt": "<30s transcript snippet>",
    "punch_beats_s": [4.5, 12.0, 22.0],
    "trending_refs": [
      {"kind": "meme", "value": "skill-issue", "freshness": "hot", "source": "kym"}
    ]
  },
  "expected": {
    "substance_tags_min": 1,
    "trending_refs_used_min": 1,
    "punch_density_min": 0.10,
    "must_contain_any": ["fail", "skill", "moment"],
    "must_not_contain_any": ["impersonate", "as Sketch says"],
    "do_not_violations": []
  },
  "_golden_output_text": "<the locked persona's actual generated script — gets compared by similarity>",
  "_locked_at": "2026-05-18",
  "_persona_version": "P-01 v1"
}
```

## How the runner works

`tests/test_writer_persona_evals.py` loads each golden file, calls Writer with the `input`, and asserts:

1. The returned `Script` satisfies the structural `expected` fields (substance, trending, punch_density, must_contain/must_not_contain, do_not_violations).
2. The returned `text` is sufficiently similar to `_golden_output_text` (Phase 1: character-overlap stub; Phase 2: sentence-embedding similarity).

A change is a "match" if all structural checks pass AND similarity ≥ threshold. The threshold for Phase 1 is loose (0.4 character overlap); the operator tightens it once real fixtures land.

## Current state

The directory is empty (placeholder note below). The runner xfails until ≥20 golden files exist. Pattern matches the music-detector harness: framework testable today, floor enforced when real data lands.

## Why "similarity" not "equality"

LLM output is non-deterministic even at temperature 0 (sampling, tokenizer drift, model patches). Exact-equality testing would fail constantly. The eval cares about persona STABILITY — the new prompt produces the same SHAPE of output as the locked one — not exact reproduction.
