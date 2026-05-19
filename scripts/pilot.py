"""CLI for the Day 14 validation pilot gate.

Operator entry points (also wired via the Makefile):

    python3 scripts/pilot.py start [--platform instagram_reels] [--clips 30]
    python3 scripts/pilot.py status
    python3 scripts/pilot.py record-revenue --amount 1.23 --source ad_rev [--detail "..."]
    python3 scripts/pilot.py record-time --minutes 32 [--note "..."]
    python3 scripts/pilot.py record-claim [--clip-id ...] [--detail ...]
    python3 scripts/pilot.py verdict
    python3 scripts/pilot.py finalize --pass [--notes "..."]
    python3 scripts/pilot.py finalize --fail [--notes "..."]
    python3 scripts/pilot.py finalize --abandon [--notes "..."]

Exit codes:
    0 — operation succeeded; pilot in expected state
    1 — operator error (missing flag, invalid value, no active pilot)
    2 — gate verdict is FAIL (so `verdict` can be chained in CI/cron)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

# Allow `python3 scripts/pilot.py ...` to find the `agents` package without
# requiring `cd $REPO_ROOT && python3 -m scripts.pilot`. The Makefile
# invokes the script by path, so this bootstrap is load-bearing.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents import pilot  # noqa: E402 — sys.path mutation above is intentional


def _cmd_start(args: argparse.Namespace) -> int:
    try:
        run = pilot.start_pilot(
            target_platform=args.platform,
            target_clip_count=args.clips,
            claim_threshold_pct=args.claim_threshold,
            rpv_threshold_usd=args.rpv_threshold,
            operator_minutes_threshold=args.minutes_threshold,
            notes=args.notes,
        )
    except pilot.PilotAlreadyActive as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Pilot #{run.id} started.")
    print(f"  Target:    {run.target_clip_count} clips to {run.target_platform}")
    print(f"  Gates:     claims < {run.claim_threshold_pct}%, "
          f"RPV > ${run.rpv_threshold_usd}, "
          f"time < {run.operator_minutes_threshold}min/day")
    print(f"  Started:   {run.started_at}")
    print()
    print("Daily operator loop:")
    print("  - run the pipeline, post clips to the target platform")
    print("  - `make pilot-record-time MINUTES=<actual>` at end of day")
    print("  - `make pilot-record-revenue AMOUNT=<usd> SOURCE=ad_rev` "
          "when dashboard updates")
    print("  - `make pilot-record-claim CLIP_ID=<id>` if a Content ID claim shows up")
    print("  - `make pilot-status` to check progress")
    return 0


def _print_progress(progress: pilot.PilotProgress, pilot_run: pilot.PilotRun) -> None:
    print(f"Pilot #{progress.pilot_run_id} — {progress.target_platform}")
    print(f"  Status:    {pilot_run.status}  (started {pilot_run.started_at})")
    print(f"  Clips:     {progress.clips_posted}/{progress.target_clip_count} posted")
    print(f"  Elapsed:   {progress.days_elapsed:.1f} day(s)")
    print(f"  Claims:    {progress.claim_count}  "
          f"({progress.claim_rate_pct:.2f}% of posted; "
          f"gate <{pilot_run.claim_threshold_pct}%)")
    print(f"  Revenue:   ${progress.revenue_usd:.4f}")
    print(f"  Views:     {progress.total_views:,}")
    print(f"  RPV:       ${progress.rpv_usd:.6f}/view  "
          f"(gate >${pilot_run.rpv_threshold_usd})")
    print(f"  Op-time:   {progress.operator_minutes_total}min total = "
          f"{progress.operator_minutes_per_day:.1f}min/day  "
          f"(gate <{pilot_run.operator_minutes_threshold}min/day)")


def _cmd_status(args: argparse.Namespace) -> int:
    run = pilot.current_pilot()
    if run is None:
        print("No active pilot. Run `make pilot-start` to begin one.")
        return 1
    progress = pilot.pilot_progress(run)
    _print_progress(progress, run)
    return 0


def _cmd_record_revenue(args: argparse.Namespace) -> int:
    try:
        row_id = pilot.record_revenue(
            amount_usd=args.amount, source=args.source, detail=args.detail,
        )
    except pilot.NoActivePilot as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Recorded revenue row #{row_id}: ${args.amount:.4f} from {args.source}")
    return 0


def _cmd_record_time(args: argparse.Namespace) -> int:
    try:
        pilot.record_operator_time(minutes=args.minutes, note=args.note)
    except pilot.NoActivePilot as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Recorded {args.minutes} minutes of operator time.")
    return 0


def _cmd_record_claim(args: argparse.Namespace) -> int:
    try:
        pilot.record_claim(clip_id=args.clip_id, detail=args.detail)
    except pilot.NoActivePilot as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Recorded Content ID claim (clip_id={args.clip_id or '<none>'}).")
    return 0


def _cmd_verdict(args: argparse.Namespace) -> int:
    run = pilot.current_pilot()
    if run is None:
        print("No active pilot.", file=sys.stderr)
        return 1
    progress = pilot.pilot_progress(run)
    _print_progress(progress, run)
    gate = pilot.evaluate_gate(run)
    print()
    print(f"VERDICT: {gate.verdict.upper()}")
    print(f"  {gate.rationale}")
    print()
    for c in gate.criteria:
        flag = "PASS" if c.passed else "FAIL"
        print(f"  [{flag}] {c.name}: {c.detail}")
    if gate.verdict == "pass":
        return 0
    if gate.verdict == "fail":
        return 2
    # inconclusive — operator action is "keep posting", not a build-break
    return 0


def _cmd_finalize(args: argparse.Namespace) -> int:
    # Exactly one of --pass / --fail / --abandon must be set.
    selected = [k for k in ("pass_", "fail", "abandon") if getattr(args, k)]
    if len(selected) != 1:
        print("ERROR: pick exactly one of --pass / --fail / --abandon",
              file=sys.stderr)
        return 1
    verdict_map = {"pass_": "pass", "fail": "fail", "abandon": "abandon"}
    verdict = verdict_map[selected[0]]
    try:
        run = pilot.finalize_pilot(verdict=verdict, notes=args.notes)
    except pilot.NoActivePilot as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Pilot #{run.id} finalized: {run.status}")
    if run.failed_reasons_json:
        print(f"  Failed gates: {run.failed_reasons_json}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pilot", description="Day 14 validation pilot gate")
    subs = p.add_subparsers(dest="cmd", required=True)

    sp = subs.add_parser("start", help="open a new pilot run")
    sp.add_argument("--platform", default=pilot.DEFAULT_TARGET_PLATFORM,
                    choices=["instagram_reels", "youtube_shorts", "tiktok"])
    sp.add_argument("--clips", type=int, default=pilot.DEFAULT_TARGET_CLIP_COUNT)
    sp.add_argument("--claim-threshold", type=float,
                    default=pilot.DEFAULT_CLAIM_THRESHOLD_PCT,
                    help="Max Content ID claim rate, in percent (default %(default)s)")
    sp.add_argument("--rpv-threshold", type=float,
                    default=pilot.DEFAULT_RPV_THRESHOLD_USD,
                    help="Min revenue per view, in USD (default %(default)s)")
    sp.add_argument("--minutes-threshold", type=int,
                    default=pilot.DEFAULT_OPERATOR_MINUTES_THRESHOLD,
                    help="Max operator minutes per day (default %(default)s)")
    sp.add_argument("--notes", default=None)
    sp.set_defaults(func=_cmd_start)

    sp = subs.add_parser("status", help="show pilot progress")
    sp.set_defaults(func=_cmd_status)

    sp = subs.add_parser("record-revenue", help="add a revenue line item")
    sp.add_argument("--amount", type=float, required=True)
    sp.add_argument("--source", required=True,
                    choices=["ad_rev", "affiliate", "creator_fund", "other"])
    sp.add_argument("--detail", default=None)
    sp.set_defaults(func=_cmd_record_revenue)

    sp = subs.add_parser("record-time", help="add end-of-day operator minutes")
    sp.add_argument("--minutes", type=int, required=True)
    sp.add_argument("--note", default=None)
    sp.set_defaults(func=_cmd_record_time)

    sp = subs.add_parser("record-claim", help="record a Content ID claim")
    sp.add_argument("--clip-id", default=None)
    sp.add_argument("--detail", default=None)
    sp.set_defaults(func=_cmd_record_claim)

    sp = subs.add_parser("verdict", help="evaluate the gate")
    sp.set_defaults(func=_cmd_verdict)

    sp = subs.add_parser("finalize", help="close the active pilot")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--pass", dest="pass_", action="store_true")
    g.add_argument("--fail", action="store_true")
    g.add_argument("--abandon", action="store_true")
    sp.add_argument("--notes", default=None)
    sp.set_defaults(func=_cmd_finalize)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
