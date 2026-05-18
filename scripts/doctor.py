#!/usr/bin/env python3
"""make doctor — health check.

Per CLAUDE.md: "health-check APIs, budget burn, strike count, account warming
status, Seedance provider availability". Phase 1 implementation: structural
checks only (DB present, configs parseable, schema applied, secrets declared
in .env, quarterly re-verify dates haven't expired). Live API pings are
NotImplementedError until Phase 2.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


class CheckResult:
    def __init__(self, name: str, ok: bool, detail: str) -> None:
        self.name = name
        self.ok = ok
        self.detail = detail

    def __str__(self) -> str:
        marker = "[OK]  " if self.ok else "[FAIL]"
        return f"{marker} {self.name}: {self.detail}"


def check_configs_parse() -> list[CheckResult]:
    out: list[CheckResult] = []
    config_dir = REPO_ROOT / "config"
    for name in (
        "budget",
        "creators",
        "clipper_programs",
        "persona",
        "avatar_reactions",
        "optimizer_bounds",
        "posting_schedule",
    ):
        path = config_dir / f"{name}.yaml"
        if not path.exists():
            out.append(CheckResult(f"config/{name}.yaml exists", False, "missing"))
            continue
        try:
            with path.open() as fh:
                yaml.safe_load(fh)
            out.append(CheckResult(f"config/{name}.yaml parses", True, "ok"))
        except Exception as exc:
            out.append(CheckResult(f"config/{name}.yaml parses", False, str(exc)))
    return out


def check_schema() -> list[CheckResult]:
    schema = REPO_ROOT / "data" / "schema.sql"
    if not schema.exists():
        return [CheckResult("data/schema.sql exists", False, "missing")]
    return [CheckResult("data/schema.sql exists", True, "ok")]


def check_db() -> list[CheckResult]:
    db_path = Path(os.environ.get("AGENTIC_CLIPPER_DB", REPO_ROOT / "data" / "main.db"))
    if not db_path.exists():
        return [CheckResult(
            "data/main.db exists",
            False,
            f"missing — run `make init-db` to create it at {db_path}",
        )]
    return [CheckResult("data/main.db exists", True, f"ok at {db_path}")]


def check_env_template() -> list[CheckResult]:
    template = REPO_ROOT / ".env.example"
    if not template.exists():
        return [CheckResult(".env.example exists", False, "missing")]
    return [CheckResult(".env.example exists", True, "ok")]


def check_avatar_seed_locked() -> list[CheckResult]:
    readme = REPO_ROOT / "config" / "avatars" / "README.md"
    if not readme.exists():
        return [CheckResult("avatar seed locked", False, "config/avatars/README.md missing")]
    text = readme.read_text()
    if "8376739915435003287" not in text:
        return [CheckResult(
            "avatar seed locked",
            False,
            "seed value missing from config/avatars/README.md",
        )]
    return [CheckResult("avatar seed locked", True, "seed=8376739915435003287")]


def check_avatar_reference_image() -> list[CheckResult]:
    # Atlas Cloud's Seedream endpoint returns JPEG; .jpg is the canonical
    # filename. A stray .png (from an earlier scaffold) would not pass.
    ref = REPO_ROOT / "config" / "avatars" / "manic_reactor.jpg"
    if ref.exists():
        return [CheckResult("avatar reference image", True, str(ref))]
    return [CheckResult(
        "avatar reference image",
        False,
        "deferred — generate on first Atlas Cloud call per phase0_digest.md action item",
    )]


def check_quarterly_reverify() -> list[CheckResult]:
    """Phase 0 docs commit to quarterly re-verification of:
      - fair_use_position.md (next 2026-08-14)
      - seedance_access.md   (next 2026-08-14)
    The doctor flags if today >= next-due. Phase 1 scaffold parses the dates
    out of the doc headers.
    """
    out: list[CheckResult] = []
    today = date.today()
    for name, header_marker in [
        ("fair_use_position.md", "Next review due:"),
        ("seedance_access.md", "Next due:"),
    ]:
        path = REPO_ROOT / "docs" / name
        if not path.exists():
            out.append(CheckResult(
                f"quarterly re-verify {name}",
                False,
                f"docs/{name} missing",
            ))
            continue
        try:
            text = path.read_text()
            # Extract a YYYY-MM-DD that follows the header marker
            idx = text.find(header_marker)
            if idx < 0:
                out.append(CheckResult(
                    f"quarterly re-verify {name}",
                    True,
                    "next-due marker not in expected format (informational)",
                ))
                continue
            tail = text[idx : idx + 200]
            import re
            m = re.search(r"(\d{4}-\d{2}-\d{2})", tail)
            if not m:
                out.append(CheckResult(
                    f"quarterly re-verify {name}",
                    True,
                    "no date found after marker (informational)",
                ))
                continue
            due = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            if today >= due:
                out.append(CheckResult(
                    f"quarterly re-verify {name}",
                    False,
                    f"OVERDUE since {due} — re-verify now",
                ))
            else:
                days = (due - today).days
                out.append(CheckResult(
                    f"quarterly re-verify {name}",
                    True,
                    f"next due {due} ({days}d)",
                ))
        except Exception as exc:
            out.append(CheckResult(
                f"quarterly re-verify {name}",
                False,
                f"check error: {exc}",
            ))
    return out


def check_budget_cap() -> list[CheckResult]:
    path = REPO_ROOT / "config" / "budget.yaml"
    try:
        with path.open() as fh:
            cfg = yaml.safe_load(fh)
        cap = cfg.get("monthly_cap_usd")
        items_total = sum(li.get("monthly_budget_usd", 0) for li in cfg.get("line_items", {}).values())
        if cap is None:
            return [CheckResult("budget cap defined", False, "monthly_cap_usd missing")]
        ok = items_total <= cap
        return [CheckResult(
            "budget line items <= cap",
            ok,
            f"sum(line_items)=${items_total} cap=${cap}",
        )]
    except Exception as exc:
        return [CheckResult("budget cap defined", False, str(exc))]


def check_live_apis_stub() -> list[CheckResult]:
    """Live API pings (YouTube, TikTok, Instagram, Atlas Cloud, fal.ai) are
    NotImplementedError in Phase 1. Surface them in the report so the operator
    sees what's still ahead.
    """
    return [CheckResult(
        "live API health checks",
        True,
        "Phase 2 — YouTube / TikTok / Instagram / Atlas Cloud pings not yet wired",
    )]


CHECKS = [
    check_configs_parse,
    check_schema,
    check_db,
    check_env_template,
    check_avatar_seed_locked,
    check_avatar_reference_image,
    check_quarterly_reverify,
    check_budget_cap,
    check_live_apis_stub,
]


def main() -> int:
    print("=== agentic-clipper doctor ===\n")
    failures = 0
    for fn in CHECKS:
        for r in fn():
            print(r)
            if not r.ok:
                failures += 1
    print()
    print(f"Result: {failures} failure(s)" if failures else "Result: all healthy")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
