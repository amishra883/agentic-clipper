"""Generate the manic_reactor avatar reference image via Atlas Cloud Seedream.

One-off. Run once after Atlas Cloud account is funded. The output of this
script (config/avatars/manic_reactor.jpg) becomes the locked reference image
for every subsequent avatar shot in production — change the seed only by
deliberately introducing a new persona, never as a fix.

Why a separate image API: Seedance is a *video* model; we need a static
*image* for the Omni Reference handoff. Atlas Cloud serves ByteDance's
image sibling Seedream at $0.032/image (Lite tier) via the same API key.

Why .jpg and not .png: Atlas Cloud's Seedream endpoint returns JPEG bytes
regardless of the requested format. Earlier versions of this script wrote
those JPEG bytes to a .png filename, which silently lied about the content
type. The script now asserts the magic bytes and writes the matching suffix.

Usage:
    python scripts/generate_avatar.py             # generate + download
    python scripts/generate_avatar.py --dry-run   # show what would happen, no network call
    python scripts/generate_avatar.py --model seedream-v4.5   # try a different model

Reads:
    - $ATLAS_CLOUD_API_KEY (env or .env)
    - Locked seed + prompt from constants below (mirrors config/avatars/README.md)

Writes:
    - config/avatars/manic_reactor.jpg
    - Audit rows in data/main.db (seedance_generations + costs) — best-effort
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
# Make `from agents.db import connect` work when this file is run as
# `python3 scripts/generate_avatar.py`. Python normally only adds the
# script's directory (scripts/) to sys.path, so the agents package wasn't
# importable and the audit-row write silently no-op'd with a misleading
# "DB isn't initialized" message. Inserting at index 0 ensures repo root
# takes precedence over any site-packages collision.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

AVATAR_PATH = REPO_ROOT / "config" / "avatars" / "manic_reactor.jpg"

# Magic-byte prefixes for the two image formats Atlas Cloud might return.
# Seedream v5.0-lite returns JPEG today; PNG is checked defensively so that
# a future provider change is caught rather than silently mislabeling the file.
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

ATLAS_BASE = "https://api.atlascloud.ai/api/v1"
GENERATE_ENDPOINT = f"{ATLAS_BASE}/model/generateImage"
PREDICTION_ENDPOINT = f"{ATLAS_BASE}/model/prediction"

# Locked persona ID. Changing this produces a different character.
# Same value lives in config/avatars/README.md and config/persona.yaml lineage.
LOCKED_SEED = 8376739915435003287

# Cheapest Seedream variant on Atlas Cloud ($0.032/image, native 1-3s generation).
# Atlas's catalog requires the provider prefix in the model id.
DEFAULT_MODEL = "bytedance/seedream-v5.0-lite"

# Atlas Cloud sits behind Cloudflare; urllib's default "Python-urllib/X.Y"
# User-Agent gets blocked with HTTP 403 (CF error 1010). Send a browser-like UA.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

# Mirrors the prompt locked in config/avatars/README.md.
PROMPT = (
    "A stylized animated-feature illustration of an original young-adult "
    "character (early-to-mid 20s, NOT a child, NOT middle-aged) designed as "
    "a podcast/video reactor mascot for short-form gaming and reaction "
    "content. Realistic-leaning humanoid proportions — modern animated-feature "
    "build (head roughly 1/7 of body, NOT chibi, NOT toddler-coded), but still "
    "clearly illustrated and cartoon-styled, NOT photoreal, NOT 3D render, "
    "NOT anime. Spider-Verse / Arcane / Soul-style aesthetic: bold confident "
    "linework, modern flat shading with subtle gradient lighting, on-trend "
    "streaming-mascot look optimized for short-form video virality. "
    "Default expression is warm and approachable — confident, slightly amused. "
    "This is the REST state, NOT a peak reaction. Expressive eyes (cartoon-"
    "proportioned but not oversized), soft neutral or slightly-raised eyebrows. "
    "Mouth in a relaxed closed-mouth half-smile — NOT a wide teeth-bare grin, "
    "NOT a grimace. Front-facing torso, eyes drifting slightly off-camera in "
    "a relaxed gaze. Contemporary casual outfit (modern streetwear or graphic "
    "tee), bright contemporary palette. ONE signature accessory: a chunky "
    "pair of headphones around the neck. Plain neutral background. NOT a real "
    "person, NOT a celebrity, NOT a streamer likeness, NOT based on any "
    "specific human. Original mascot character."
)

# Per-image cost for Seedream v5.0 Lite at the time of writing (2026-05-14).
COST_PER_IMAGE_USD = 0.032


# ---------- HTTP helpers (stdlib only — no requests dependency) ----------

def _request(method: str, url: str, *, api_key: str | None = None, body: dict | None = None, timeout: int = 30) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _get_bytes(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------- API operations ----------

def load_api_key() -> str:
    key = os.environ.get("ATLAS_CLOUD_API_KEY")
    if key:
        return key
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("ATLAS_CLOUD_API_KEY=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()
    sys.exit(
        "ATLAS_CLOUD_API_KEY not set.\n"
        "  Put it in .env (see .env.example), or export ATLAS_CLOUD_API_KEY=sk-... first."
    )


def submit_generation(api_key: str, model: str) -> str:
    body = {
        "model": model,
        "prompt": PROMPT,
        "seed": LOCKED_SEED,
        # Atlas Cloud's docs accept either size or aspect_ratio; we send both
        # so whichever shape they expect is satisfied. Seedream v5.0-lite
        # enforces a minimum of 3,686,400 pixels — 2048x2048 clears that with
        # headroom while keeping the locked 1:1 aspect.
        "size": "2048x2048",
        "aspect_ratio": "1:1",
    }
    status, raw = _request("POST", GENERATE_ENDPOINT, api_key=api_key, body=body)
    if status != 200:
        sys.exit(f"submit failed: HTTP {status}\n  body: {raw.decode('utf-8', 'replace')[:500]}")
    payload = json.loads(raw)
    pred_id = (payload.get("data") or payload).get("id")
    if not pred_id:
        sys.exit(f"submit succeeded but no prediction id in response:\n  {payload}")
    return pred_id


def poll_until_done(api_key: str, pred_id: str, timeout_s: int = 180) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status, raw = _request("GET", f"{PREDICTION_ENDPOINT}/{pred_id}", api_key=api_key, timeout=15)
        if status != 200:
            sys.exit(f"poll failed: HTTP {status}\n  body: {raw.decode('utf-8', 'replace')[:500]}")
        payload = json.loads(raw)
        data = payload.get("data") or payload
        state = data.get("status")
        if state == "completed":
            return data
        if state == "failed":
            err = data.get("error") or "no error string"
            sys.exit(f"generation failed: {err}\n  full: {data}")
        # status is "processing" (or absent); keep polling
        time.sleep(2)
    sys.exit(f"timed out after {timeout_s}s polling prediction {pred_id}")


def download_image(url: str, dest: Path) -> int:
    raw = _get_bytes(url)
    if raw.startswith(JPEG_MAGIC):
        detected = "jpeg"
    elif raw.startswith(PNG_MAGIC):
        detected = "png"
    else:
        sys.exit(
            "downloaded bytes are neither JPEG nor PNG.\n"
            f"  first 16 bytes (hex): {raw[:16].hex()}\n"
            "  Atlas may have returned an error payload or changed format."
        )
    expected_ext = dest.suffix.lstrip(".").lower()
    if expected_ext == "jpg":
        expected_ext = "jpeg"
    if detected != expected_ext:
        sys.exit(
            f"content-type mismatch: downloaded {detected.upper()} but "
            f"AVATAR_PATH suffix is .{dest.suffix.lstrip('.')}. Update "
            "AVATAR_PATH to match what the provider actually returns."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return len(raw)


def record_audit_best_effort(model: str, cost_usd: float, output_path: Path) -> None:
    """Write provenance + cost rows. Silently no-ops if DB isn't initialized
    yet (running this script before `make init-db` is non-fatal)."""
    try:
        from agents.db import connect

        with connect() as conn:
            conn.execute(
                """
                INSERT INTO seedance_generations
                  (clip_id, provider, model_version, tier, prompt, seed,
                   duration_s, cost_usd, status, output_path)
                VALUES (NULL, ?, ?, 'fast', ?, ?, 0, ?, 'succeeded', ?)
                """,
                ("atlas_cloud", model, PROMPT, LOCKED_SEED, cost_usd, str(output_path)),
            )
            conn.execute(
                """
                INSERT INTO costs (category, amount_usd, provider, detail)
                VALUES ('seedance_fast', ?, 'atlas_cloud', ?)
                """,
                (cost_usd, f"avatar reference image: {model} seed={LOCKED_SEED}"),
            )
        print(f"  audit: recorded ${cost_usd:.3f} to data/main.db (seedance_generations + costs)")
    except Exception as exc:
        print(f"  audit: skipped ({exc.__class__.__name__}: {exc})")


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Atlas Cloud model id (default: {DEFAULT_MODEL})")
    parser.add_argument("--dry-run", action="store_true", help="Print payload, do not call the network")
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite the existing avatar reference image. Default behavior "
            "is to refuse-if-exists so a stray re-run does not silently spend "
            "$0.032 and break the locked-image contract."
        ),
    )
    args = parser.parse_args()

    print("=== Generate manic_reactor avatar ===")
    print(f"  model:       {args.model}")
    print(f"  seed:        {LOCKED_SEED}")
    print(f"  output:      {AVATAR_PATH.relative_to(REPO_ROOT)}")
    print(f"  expected $:  ~${COST_PER_IMAGE_USD:.3f} per image")
    print(f"  prompt:      {PROMPT[:120]}...")
    print()

    # --dry-run is always safe (no network call, no filesystem write), so it
    # short-circuits both the idempotency guard and the API-key load.
    if args.dry_run:
        print("(--dry-run) no network call made. Re-run without --dry-run to generate.")
        return 0

    # Idempotency guard: refuse to overwrite a locked reference image without
    # explicit --force. Once committed, this image is the canonical mascot —
    # silent regeneration breaks brand continuity AND costs $0.032 per accident.
    # Per config/avatars/README.md "Changing the avatar later", a deliberate
    # change should go through a /proposals/ review, not a script re-run.
    if AVATAR_PATH.exists() and not args.force:
        existing_size = AVATAR_PATH.stat().st_size
        print(
            f"ERROR: {AVATAR_PATH.relative_to(REPO_ROOT)} already exists "
            f"({existing_size:,} bytes).\n"
            "  Refusing to overwrite a locked reference image without --force.\n"
            "  If you really want to regenerate (and accept the brand-continuity\n"
            "  break documented in config/avatars/README.md):\n"
            "    python3 scripts/generate_avatar.py --force\n"
            "  If you instead want to commit the existing file as the lock:\n"
            "    git add config/avatars/manic_reactor.jpg && git commit"
        )
        return 1

    api_key = load_api_key()

    print("[1/3] submitting generation request...")
    pred_id = submit_generation(api_key, args.model)
    print(f"      prediction id: {pred_id}")

    print("[2/3] polling for completion...")
    data = poll_until_done(api_key, pred_id)
    outputs = data.get("outputs") or []
    if not outputs:
        sys.exit(
            "completed status returned but outputs list is empty.\n"
            "  On Atlas Cloud's Seedance video API this can signal a real-face\n"
            "  filter rejection. The Seedream image endpoint may behave the same.\n"
            "  Check the prompt for any wording that could imply a real person."
        )
    image_url = outputs[0]
    predict_time = data.get("metrics", {}).get("predict_time")
    if predict_time is not None:
        print(f"      generated in {predict_time}s")

    print(f"[3/3] downloading to {AVATAR_PATH.relative_to(REPO_ROOT)}...")
    bytes_written = download_image(image_url, AVATAR_PATH)
    print(f"      wrote {bytes_written:,} bytes")

    record_audit_best_effort(args.model, COST_PER_IMAGE_USD, AVATAR_PATH)

    print()
    print("=== Done. Next steps ===")
    print(f"  1. Open {AVATAR_PATH.relative_to(REPO_ROOT)} and verify:")
    print("       • cartoon-coded (NOT photoreal)")
    print("       • headphones-around-neck signature accessory present")
    print("       • mid-grin expression, front-facing")
    print("       • plain neutral background")
    print("  2. If it looks right, commit it:")
    print(f"       git add {AVATAR_PATH.relative_to(REPO_ROOT)}")
    print('       git commit -m "Lock manic_reactor avatar reference image"')
    print("       git push")
    print("  3. If it does NOT look right:")
    print("       DO NOT change the seed.")
    print("       Tweak PROMPT in this script and re-run.")
    print("       Once happy, commit. The seed stays the same forever after that.")
    print()
    print("  Then re-run `make doctor` — the 'avatar reference image' check should pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
