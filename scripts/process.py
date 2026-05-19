"""CLI for the end-to-end pipeline orchestrator.

    python3 scripts/process.py --n 5

Picks up to N candidates in 'curated' status and runs each through the
full pipeline (Editor → Writer → Voice → Visuals → Compositor →
Compliance → enqueue). Prints a per-clip outcome table.

Exit codes:
    0 — all picked clips reached a terminal state (ready / quarantined /
        compliance_failed). Operator should look at the summary to see
        what proportion are ready for publish.
    1 — operator error (bad flag, etc.)
    2 — at least one clip hit `error` outcome (unexpected exception)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents import orchestrator  # noqa: E402 — sys.path mutation above is intentional


def _print_summary(summary: orchestrator.RunSummary) -> None:
    print(f"Requested:           {summary.requested}")
    print(f"Processed:           {summary.processed}")
    print(f"  → ready:           {summary.ready}")
    print(f"  → quarantined:     {summary.quarantined}")
    print(f"  → compliance fail: {summary.compliance_failed}")
    print(f"  → lease conflict:  {summary.lease_conflict}")
    print(f"  → errored:         {summary.errored}")
    if summary.processed == 0:
        print()
        print("No curated candidates available. Top up the queue:")
        print("  make scout      # discover new candidates")
        print("  make curator    # promote discovered → curated")
        return
    print()
    print("Per-clip:")
    for r in summary.results:
        line = f"  {r.clip_id:40s}  {r.outcome:18s}"
        if r.error:
            line += f"  ({r.error})"
        print(line)
        if r.outcome in ("error",):
            for s in r.stages:
                flag = "PASS" if s.succeeded else "FAIL"
                print(f"      [{flag}] {s.stage}: {s.detail}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="process", description="Run the pipeline on the next N curated candidates")
    parser.add_argument("--n", type=int, default=5,
                        help="Number of candidates to process (default 5)")
    args = parser.parse_args(argv)

    if args.n <= 0:
        print("ERROR: --n must be > 0", file=sys.stderr)
        return 1

    summary = asyncio.run(orchestrator.run_orchestrator(args.n))
    _print_summary(summary)

    if summary.errored > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
