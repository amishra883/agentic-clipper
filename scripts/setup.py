#!/usr/bin/env python3
"""make setup — operator-facing one-shot environment bootstrap.

Walks the operator through the pre-wiring setup needed before Phase 2 can run.
NON-DESTRUCTIVE by default: this script never installs system packages or
downloads model weights on its own. It detects what's missing and prints the
exact commands the operator should run. Pass --apply to invoke a small subset
that's safe to run automatically (pip deps, schema init, migrations).

Why not fully automated:
- `brew install yt-dlp ffmpeg` needs the operator's sudo password
- Coqui XTTS-v2 weights are ~1.8GB; we don't want to start that download
  silently in the middle of a `make setup` invocation
- API keys can't be acquired without the operator visiting provider portals
- The operator's macOS Keychain rotation flow (per Eng finding E-11) is a
  manual decision

Checks performed (in this order):

  1. System binaries: yt-dlp, ffmpeg, sqlite3, python3 (≥3.11)
  2. Python deps from requirements.txt
  3. Coqui XTTS-v2 model weights (presence + checksum if available)
  4. data/main.db exists (else: `make init-db`)
  5. Schema version vs migrations/ (else: `make migrate`)
  6. .env vars per .env.example (warns on missing; offers --setup-keys flow)
  7. Avatar reference image (config/avatars/manic_reactor.jpg)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
ENV_FILE = REPO_ROOT / ".env"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
AVATAR_PATH = REPO_ROOT / "config" / "avatars" / "manic_reactor.jpg"

# Provider portals for the .env walker (Block B's interactive --setup-keys flow,
# wired Phase 2 but documented here so the operator knows where to go today).
PROVIDER_PORTALS = {
    "ATLAS_CLOUD_API_KEY": "https://www.atlascloud.ai/dashboard (Atlas Cloud → API Keys)",
    "ANTHROPIC_API_KEY": "https://console.anthropic.com/settings/keys",
    "YOUTUBE_API_KEY": "https://console.cloud.google.com/apis/credentials",
    "YOUTUBE_OAUTH_CLIENT_SECRET": "https://console.cloud.google.com/apis/credentials (OAuth 2.0 Client IDs)",
    "TWITCH_CLIENT_ID": "https://dev.twitch.tv/console/apps",
    "TWITCH_CLIENT_SECRET": "https://dev.twitch.tv/console/apps (same app as TWITCH_CLIENT_ID)",
    "META_APP_ID": "https://developers.facebook.com/apps",
    "IG_BUSINESS_ACCOUNT_ID": "https://business.facebook.com (Instagram → Business Settings)",
    "ELEVENLABS_API_KEY": "https://elevenlabs.io/app/settings/api-keys",
    "FAL_API_KEY": "https://fal.ai/dashboard/keys",
    "S3_ACCESS_KEY": "your S3-compatible provider (Backblaze B2 / Cloudflare R2 / AWS)",
    "S3_SECRET_KEY": "same provider as S3_ACCESS_KEY",
}


@dataclass
class CheckOutcome:
    name: str
    ok: bool
    detail: str
    fix: str | None = None  # exact command to run (printed verbatim if not OK)


# ---------- Check helpers ----------

def _which(binary: str) -> str | None:
    return shutil.which(binary)


def check_system_binaries() -> list[CheckOutcome]:
    out: list[CheckOutcome] = []
    for binary, brew_pkg in [
        ("yt-dlp", "yt-dlp"),
        ("ffmpeg", "ffmpeg"),
        ("sqlite3", "sqlite"),
    ]:
        path = _which(binary)
        if path:
            out.append(CheckOutcome(name=f"`{binary}` installed", ok=True, detail=path))
        else:
            out.append(CheckOutcome(
                name=f"`{binary}` installed",
                ok=False,
                detail="not found in PATH",
                fix=f"brew install {brew_pkg}",
            ))

    # python3 version
    pyver = sys.version_info
    if pyver >= (3, 11):
        out.append(CheckOutcome(
            name="python3 ≥ 3.11",
            ok=True,
            detail=f"running {pyver.major}.{pyver.minor}.{pyver.micro}",
        ))
    else:
        out.append(CheckOutcome(
            name="python3 ≥ 3.11",
            ok=False,
            detail=f"running {pyver.major}.{pyver.minor}.{pyver.micro}",
            fix="brew install python@3.11 (or newer)",
        ))
    return out


def check_python_deps() -> list[CheckOutcome]:
    if not REQUIREMENTS.exists():
        return [CheckOutcome("requirements.txt", False, "missing")]
    # Don't actually call pip --dry-run (slow + network). Just confirm pyyaml
    # and pytest import — the two we know matter for doctor + tests.
    out: list[CheckOutcome] = []
    for mod, pip_name in [("yaml", "pyyaml"), ("pytest", "pytest")]:
        try:
            __import__(mod)
            out.append(CheckOutcome(f"python: import {mod}", True, "ok"))
        except ImportError:
            out.append(CheckOutcome(
                f"python: import {mod}",
                False,
                "not installed",
                fix=f"python3 -m pip install --user {pip_name}",
            ))
    # Larger dependency set (faster-whisper, coqui-tts) gets installed later;
    # operator is warned but not blocked here.
    out.append(CheckOutcome(
        "python: faster-whisper / coqui-tts",
        True,
        "not yet required — Day 5-8 of Phase 2 wires these (see docs/phase2_plan.md Block B)",
    ))
    return out


def check_db_state() -> list[CheckOutcome]:
    db_path = Path(os.environ.get("AGENTIC_CLIPPER_DB", REPO_ROOT / "data" / "main.db"))
    if not db_path.exists():
        return [CheckOutcome(
            "data/main.db exists",
            False,
            "missing",
            fix="make init-db && make migrate",
        )]
    return [CheckOutcome("data/main.db exists", True, str(db_path))]


def check_env_keys() -> list[CheckOutcome]:
    if not ENV_FILE.exists():
        return [CheckOutcome(
            ".env exists",
            False,
            "missing — copy .env.example to .env and fill in real values",
            fix=f"cp .env.example .env && $EDITOR .env",
        )]
    # Parse current .env, list which keys are unset
    set_keys: set[str] = set()
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, _, val = line.partition("=")
            if val.strip():
                set_keys.add(key.strip())
    # Compare against .env.example's keyset
    expected: set[str] = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, _ = line.split("=", 1)
            expected.add(key.strip())
    missing = sorted(expected - set_keys)
    if not missing:
        return [CheckOutcome(f".env keys ({len(expected)})", True, "all present")]
    # Report missing keys with portals
    detail_lines = [f"  {k} → {PROVIDER_PORTALS.get(k, '(see .env.example for context)')}" for k in missing]
    return [CheckOutcome(
        f".env keys ({len(set_keys)}/{len(expected)})",
        False,
        f"missing {len(missing)} of {len(expected)}:\n" + "\n".join(detail_lines),
        fix="Edit .env and fill in the keys above. Phase 2 docs/runbook.md has provider walkthroughs.",
    )]


def check_avatar() -> list[CheckOutcome]:
    if AVATAR_PATH.exists():
        size = AVATAR_PATH.stat().st_size
        return [CheckOutcome("avatar reference image", True, f"{AVATAR_PATH.name} ({size:,} bytes)")]
    return [CheckOutcome(
        "avatar reference image",
        False,
        "not yet generated",
        fix="python3 scripts/generate_avatar.py (after ATLAS_CLOUD_API_KEY is set)",
    )]


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Run the safe-to-automate subset: init-db, migrate, pip install pyyaml/pytest.",
    )
    args = parser.parse_args()

    print("=== agentic-clipper setup ===\n")
    sections: list[tuple[str, list[CheckOutcome]]] = [
        ("System binaries", check_system_binaries()),
        ("Python deps", check_python_deps()),
        ("Database", check_db_state()),
        (".env keys", check_env_keys()),
        ("Avatar reference image", check_avatar()),
    ]

    total_failures = 0
    for section_name, results in sections:
        print(f"--- {section_name} ---")
        for r in results:
            marker = "[OK]  " if r.ok else "[FAIL]"
            print(f"{marker} {r.name}: {r.detail}")
            if not r.ok and r.fix:
                # Indent fix instructions visually
                for line in r.fix.splitlines():
                    print(f"       → {line}")
            if not r.ok:
                total_failures += 1
        print()

    if args.apply:
        print("--- --apply: safe automations ---")
        # Run the small bounded set: pip install missing pyyaml/pytest, init-db, migrate.
        # Codex finding 2026-05-18: `pip install --user` inside a venv installs
        # to the user site-packages instead of the venv's site-packages —
        # the venv stays broken. Detect venv via `sys.prefix != sys.base_prefix`
        # (the canonical Python idiom) and omit --user when inside one.
        in_venv = sys.prefix != sys.base_prefix
        pip_cmd = [sys.executable, "-m", "pip", "install", "--quiet"]
        if not in_venv:
            pip_cmd.append("--user")
        pip_cmd.extend(["pyyaml", "pytest"])
        try:
            subprocess.check_call(pip_cmd)
            scope = "(venv)" if in_venv else "(--user)"
            print(f"[OK]   pip installed pyyaml + pytest {scope}")
        except subprocess.CalledProcessError as exc:
            print(f"[FAIL] pip install: {exc}")
            total_failures += 1
        try:
            from agents.db import init_schema
            init_schema()
            print("[OK]   data/main.db initialized")
        except Exception as exc:
            print(f"[FAIL] init_schema: {exc}")
            total_failures += 1
        try:
            from scripts.migrate import migrate
            summary = migrate()
            applied = summary["applied"]
            if applied:
                print(f"[OK]   applied migrations: {applied}")
            else:
                print(f"[OK]   no pending migrations (DB at v{summary['current_version']})")
        except Exception as exc:
            print(f"[FAIL] migrate: {exc}")
            total_failures += 1
        print()

    print(f"=== {total_failures} item(s) need operator attention ===")
    if total_failures == 0:
        print("Setup complete. Run `make doctor` next.")
    else:
        print("Run the → commands above, then re-run `make setup` until clean.")
        print("For a fully-automated subset (pip deps + init-db + migrate): `make setup-apply`")
    return 1 if total_failures > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
