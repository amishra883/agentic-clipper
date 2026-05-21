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


def _ping_https(host: str, *, path: str = "/", timeout: float = 5.0,
                accept_status: tuple[int, ...] = (200, 204, 301, 302, 400, 401, 403, 404)) -> tuple[bool, str]:
    """HEAD `https://<host><path>` and report reachability without auth.

    A 4xx response from an authenticated endpoint still means the host and
    TLS are healthy — the request reached the API and got rejected for
    missing credentials or a malformed HEAD (some APIs don't support HEAD
    on the root path). 400/401/403/404 all count as reachability passes;
    only 5xx / network errors are real failures.
    """
    import socket
    import ssl
    import urllib.error
    import urllib.request
    url = f"https://{host}{path}"
    req = urllib.request.Request(
        url, method="HEAD",
        # Atlas Cloud sits behind Cloudflare and blocks the default
        # Python-urllib UA with HTTP 403/CF 1010. Match what
        # scripts/generate_avatar.py uses.
        headers={"User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (socket.gaierror, socket.timeout, ConnectionError, ssl.SSLError,
            urllib.error.URLError, TimeoutError) as exc:
        return False, f"network error: {exc.__class__.__name__}: {exc}"
    if code in accept_status:
        return True, f"HEAD {url} -> HTTP {code} (host reachable)"
    return False, f"HEAD {url} -> HTTP {code} (unexpected)"


def check_live_apis() -> list[CheckResult]:
    """Liveness pings to each external provider. No credentials needed —
    we only verify DNS + TLS + edge-layer reachability. A 401/403 counts
    as PASS (request got to the API; auth missing).

    TikTok is intentionally manual per operator decision (2026-05-14); the
    drop-path writer is wired in publisher.py, so we don't ping the upload
    API at all.
    """
    out: list[CheckResult] = []

    # YouTube Data API v3
    ok, detail = _ping_https("www.googleapis.com", path="/youtube/v3/")
    out.append(CheckResult("YouTube Data API v3 reachable", ok, detail))

    # Instagram Graph API (graph.facebook.com hosts both IG and FB graph)
    ok, detail = _ping_https("graph.facebook.com", path="/v18.0/")
    out.append(CheckResult("Instagram Graph API reachable", ok, detail))

    # Atlas Cloud (Seedance video + Seedream image)
    ok, detail = _ping_https("api.atlascloud.ai", path="/v1/models")
    out.append(CheckResult("Atlas Cloud API reachable", ok, detail))

    # fal.ai failover provider
    ok, detail = _ping_https("fal.run", path="/")
    out.append(CheckResult("fal.ai API reachable", ok, detail))

    # TikTok: intentionally manual mode; surface as informational pass.
    out.append(CheckResult(
        "TikTok manual-mode drop path",
        True,
        "manual_drop_directory writer wired; live API intentionally not used (operator-decided 2026-05-14)",
    ))
    return out


def check_schema_version() -> list[CheckResult]:
    """Compare `schema_version` table to the highest-numbered migration file.

    If they diverge, the operator pulled new code without running `make migrate`.
    That state is dangerous — the running pipeline may write rows that violate
    constraints added in the unapplied migration, or read columns that don't
    exist. Fail loudly with the exact command to fix.
    """
    db_path = Path(os.environ.get("AGENTIC_CLIPPER_DB", REPO_ROOT / "data" / "main.db"))
    migrations_dir = REPO_ROOT / "migrations"

    # Find the highest migration version on disk (parse `-- VERSION: N` header).
    expected = 1  # baseline version from data/schema.sql
    if migrations_dir.exists():
        import re
        version_re = re.compile(r"^--\s*VERSION:\s*(\d+)\s*$", re.MULTILINE)
        for path in migrations_dir.glob("*.sql"):
            m = version_re.search(path.read_text())
            if m:
                expected = max(expected, int(m.group(1)))

    if not db_path.exists():
        return [CheckResult(
            "schema version",
            False,
            f"data/main.db missing — run `make init-db && make migrate` (expected v{expected})",
        )]

    import sqlite3
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        actual = int(row[0]) if row and row[0] is not None else 0
    except sqlite3.OperationalError as exc:
        return [CheckResult(
            "schema version",
            False,
            f"schema_version table unreadable ({exc}) — run `make init-db`",
        )]
    except Exception as exc:  # pragma: no cover — defensive
        return [CheckResult("schema version", False, f"DB query failed: {exc}")]

    if actual < expected:
        return [CheckResult(
            "schema version",
            False,
            f"DB is at v{actual} but migrations/ has files through v{expected} — run `make migrate`",
        )]
    if actual > expected:
        return [CheckResult(
            "schema version",
            False,
            f"DB is at v{actual} but migrations/ only goes to v{expected} — likely on an older code commit; "
            "check `git status` and `git log` against the running DB",
        )]
    return [CheckResult("schema version", True, f"DB v{actual} matches latest migration v{expected}")]


def check_monthly_budget_burn() -> list[CheckResult]:
    """Compare month-to-date external spend (from the costs table) against
    monthly_cap_usd and hard_kill_switch_usd in config/budget.yaml.

    Codex P1 finding: per-clip caps don't prevent monthly burn. This check
    enforces the aggregate.
    """
    try:
        with (REPO_ROOT / "config" / "budget.yaml").open() as fh:
            cfg = yaml.safe_load(fh)
        cap = float(cfg.get("monthly_cap_usd", 0))
        kill = float(cfg.get("hard_kill_switch_usd", cap))
    except Exception as exc:
        return [CheckResult("monthly budget burn", False, f"config/budget.yaml unreadable: {exc}")]

    db_path = Path(os.environ.get("AGENTIC_CLIPPER_DB", REPO_ROOT / "data" / "main.db"))
    if not db_path.exists():
        return [CheckResult(
            "monthly budget burn",
            False,
            "data/main.db missing — run `make init-db`; no spend ledger available",
        )]

    import sqlite3
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_usd), 0) AS spend FROM costs "
                "WHERE strftime('%Y-%m', ts) = strftime('%Y-%m', 'now')"
            ).fetchone()
        spend = float(row[0])
    except Exception as exc:
        return [CheckResult("monthly budget burn", False, f"DB query failed: {exc}")]

    pct = (spend / cap * 100) if cap > 0 else 0
    if spend >= kill:
        return [CheckResult(
            "monthly budget burn",
            False,
            f"OVER hard_kill_switch — ${spend:.2f} >= ${kill:.2f}; pipeline MUST be paused",
        )]
    if spend >= cap:
        return [CheckResult(
            "monthly budget burn",
            False,
            f"OVER cap — ${spend:.2f} / ${cap:.2f} ({pct:.0f}%); Optimizer should be auto-pausing line items",
        )]
    if pct >= 85:
        return [CheckResult(
            "monthly budget burn",
            False,
            f"approaching cap — ${spend:.2f} / ${cap:.2f} ({pct:.0f}%); review burn rate",
        )]
    return [CheckResult(
        "monthly budget burn",
        True,
        f"${spend:.2f} / ${cap:.2f} ({pct:.0f}%); headroom OK",
    )]


def check_strike_monitor() -> list[CheckResult]:
    """Per CLAUDE.md "Zero copyright strikes tolerated" — any unresolved strike
    is a FAIL regardless of count.
    """
    db_path = Path(os.environ.get("AGENTIC_CLIPPER_DB", REPO_ROOT / "data" / "main.db"))
    if not db_path.exists():
        return [CheckResult(
            "strike monitor",
            False,
            "data/main.db missing — run `make init-db`; cannot verify zero-strike posture",
        )]
    import sqlite3
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM strikes WHERE resolved = 0"
            ).fetchone()
        n = int(row[0])
    except Exception as exc:
        return [CheckResult("strike monitor", False, f"DB query failed: {exc}")]
    if n > 0:
        return [CheckResult(
            "strike monitor",
            False,
            f"{n} unresolved strike(s) — review accounts and dispute / failover before next publish",
        )]
    return [CheckResult("strike monitor", True, "0 unresolved strikes")]


def check_account_warming() -> list[CheckResult]:
    """Per spec: maintain 2 warm backup accounts per platform aged >=30d.
    Failing the check means failover is unsafe."""
    db_path = Path(os.environ.get("AGENTIC_CLIPPER_DB", REPO_ROOT / "data" / "main.db"))
    if not db_path.exists():
        return [CheckResult(
            "account warming",
            False,
            "data/main.db missing — run `make init-db`; cannot verify warm-backup count",
        )]
    import sqlite3
    out: list[CheckResult] = []
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT platform,
                       SUM(CASE WHEN role='primary' AND active=1 THEN 1 ELSE 0 END) AS primaries,
                       SUM(CASE WHEN role='backup_warm' AND warm_eligible=1 THEN 1 ELSE 0 END) AS warm_backups
                  FROM accounts
                 GROUP BY platform
                """
            ).fetchall()
    except Exception as exc:
        return [CheckResult("account warming", False, f"DB query failed: {exc}")]

    if not rows:
        return [CheckResult(
            "account warming",
            False,
            "no rows in accounts table — register accounts before any publish",
        )]
    for platform, primaries, warm_backups in rows:
        ok = (primaries or 0) >= 1 and (warm_backups or 0) >= 2
        out.append(CheckResult(
            f"account warming ({platform})",
            ok,
            f"primary={primaries or 0}, warm_backups={warm_backups or 0} (need primary>=1, warm_backups>=2)",
        ))
    return out


def check_atlas_cloud_credentials() -> list[CheckResult]:
    """Confirm ATLAS_CLOUD_API_KEY is set somewhere the pipeline can find
    it. Doesn't ping the API — that's check_live_apis's job — just
    verifies the key exists so Visuals doesn't silently drop to scaffold
    mode mid-pilot."""
    key = os.environ.get("ATLAS_CLOUD_API_KEY")
    if key:
        return [CheckResult(
            name="atlas_cloud: api key", ok=True,
            detail=f"present in env ({len(key)} chars)",
        )]
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("ATLAS_CLOUD_API_KEY=") and not stripped.startswith("#"):
                value = stripped.split("=", 1)[1].strip()
                if value:
                    return [CheckResult(
                        name="atlas_cloud: api key", ok=True,
                        detail=f"present in .env ({len(value)} chars)",
                    )]
    return [CheckResult(
        name="atlas_cloud: api key", ok=False,
        detail=(
            "ATLAS_CLOUD_API_KEY not set. Visuals stays in scaffold mode "
            "→ every clip fails Compliance (no AI-content to disclose). "
            "See docs/runbook.md §4."
        ),
    )]


def check_pipeline_dependencies() -> list[CheckResult]:
    """Verify the local tools the orchestrator needs are on PATH /
    importable. Each missing piece downgrades the relevant stage to
    scaffold mode rather than blocking — but the operator should see
    what's missing in one place.

    PATH binaries:
      - ffmpeg, ffprobe (Compositor)
      - yt-dlp (Editor download)

    Python packages:
      - faster_whisper (Editor transcribe)
      - TTS (Coqui XTTS-v2; Voice)
    """
    import shutil
    import importlib.util

    out: list[CheckResult] = []
    for binary in ("ffmpeg", "ffprobe", "yt-dlp"):
        path = shutil.which(binary)
        if path:
            out.append(CheckResult(
                name=f"binary: {binary}", ok=True, detail=path,
            ))
        else:
            out.append(CheckResult(
                name=f"binary: {binary}", ok=False,
                detail=(
                    "not on PATH; install (`brew install ffmpeg`, "
                    "`pip install yt-dlp`). Stage falls back to "
                    "scaffold mode until installed."
                ),
            ))

    for pkg, hint in (
        ("faster_whisper", "pip install faster-whisper"),
        ("piper", "pip install piper-tts  # Py 3.14-compatible local TTS"),
        ("TTS", "pip install TTS  # Coqui XTTS-v2 — requires Python <3.12"),
    ):
        if importlib.util.find_spec(pkg) is not None:
            out.append(CheckResult(name=f"python: {pkg}", ok=True, detail="importable"))
        else:
            # TTS is documented as Python-<3.12-only; downgrade to a softer
            # hint rather than a hard FAIL on 3.12+ environments where the
            # operator chose Piper. Voice has a fallback chain (Coqui →
            # Piper → scaffold) so missing one engine isn't blocking.
            soft = pkg == "TTS" and sys.version_info >= (3, 12)
            out.append(CheckResult(
                name=f"python: {pkg}",
                ok=soft,  # OK if it's the documented-incompatible engine
                detail=(
                    f"not installed (expected on Python {sys.version_info[0]}."
                    f"{sys.version_info[1]} — fall back to Piper)"
                    if soft
                    else f"not installed; {hint}. Stage falls back to scaffold mode."
                ),
            ))

    # Piper voice model file. Piper's CLI ships with the runtime but voice
    # models are downloaded separately. Without a model file, _synthesize_piper
    # raises NotImplementedError and the orchestrator drops to scaffold mode.
    piper_voice_path = (
        REPO_ROOT / "config" / "voice_models" / "piper" / "en_US-amy-medium.onnx"
    )
    if piper_voice_path.exists():
        out.append(CheckResult(
            name="piper: voice model", ok=True,
            detail=f"present at {piper_voice_path}",
        ))
    else:
        out.append(CheckResult(
            name="piper: voice model", ok=False,
            detail=(
                f"missing at {piper_voice_path}. Download with: "
                f"mkdir -p config/voice_models/piper && "
                f"cd config/voice_models/piper && "
                f"curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx && "
                f"curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json"
            ),
        ))
    return out


CHECKS = [
    check_configs_parse,
    check_schema,
    check_db,
    check_schema_version,
    check_env_template,
    check_avatar_seed_locked,
    check_avatar_reference_image,
    check_quarterly_reverify,
    check_budget_cap,
    check_monthly_budget_burn,
    check_strike_monitor,
    check_account_warming,
    check_pipeline_dependencies,
    check_atlas_cloud_credentials,
    check_live_apis,
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
