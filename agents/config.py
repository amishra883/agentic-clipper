"""YAML config loader + atomic writer. Single source of truth for files in /config/.

Hardening blocks (Day 13 of the revised Phase 2 plan):

- **mtime-aware caching (E-25):**
  Replaces a bare `@lru_cache` with `_load_with_mtime()` so when an
  operator (or Optimizer agent) edits a config file on disk, the next
  `load()` call picks up the change without needing an explicit
  `reload()`. The cache invalidates automatically when the file's
  st_mtime moves forward.

- **Atomic YAML save (E-18):**
  `save()` writes to a same-directory `.tmp` file, fsyncs, and uses
  `os.replace()` for the atomic rename. A reader that opens the
  config mid-write either sees the prior version or the new — never
  a truncated file. This matters because the Optimizer auto-applies
  config changes on a continuous loop; a torn write would crash the
  next reader.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"


class ConfigSaveError(Exception):
    """Atomic save failed (write/fsync/replace). The on-disk file is
    guaranteed unchanged — readers can keep going."""


# (path, mtime_ns) -> parsed dict. Invalidated when mtime_ns changes.
_CACHE: dict[tuple[Path, int], dict] = {}


def _config_path(name: str) -> Path:
    return CONFIG_DIR / f"{name}.yaml"


def _load_with_mtime(path: Path) -> dict:
    """Read+parse the file if its mtime changed since the last cached
    parse. Returns the cached dict on a hit, fresh parse on a miss.

    Codex E-25: prior `@lru_cache(maxsize=None)` cached forever, so an
    operator editing config/budget.yaml had to remember to call
    `reload()` (or restart the process) for changes to take effect.
    The Optimizer agent auto-applies changes within bounds — without
    mtime detection, those changes never propagated.
    """
    if not path.exists():
        raise FileNotFoundError(f"config file missing: {path}")
    mtime_ns = path.stat().st_mtime_ns
    key = (path, mtime_ns)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    # Sweep stale entries for this path (different mtime).
    for k in [k for k in _CACHE if k[0] == path]:
        if k != key:
            _CACHE.pop(k, None)
    with path.open() as fh:
        parsed = yaml.safe_load(fh) or {}
    _CACHE[key] = parsed
    return parsed


def load(name: str) -> dict:
    """Load config/{name}.yaml. Returns the parsed dict. Cache-hit on
    repeated calls when the file hasn't changed; cache-miss + reparse
    when mtime moves forward."""
    return _load_with_mtime(_config_path(name))


def reload(name: str) -> dict:
    """Clear the cache for this name and re-read. Used by tests and by
    operator-driven config changes that need immediate effect (the
    mtime cache picks them up too, but `reload()` is the explicit
    escape hatch for callers that want to be certain)."""
    path = _config_path(name)
    for k in [k for k in _CACHE if k[0] == path]:
        _CACHE.pop(k, None)
    return load(name)


def save(name: str, data: dict, *, allow_create: bool = False) -> None:
    """Atomically write `data` as YAML to config/{name}.yaml.

    Codex E-18: when the Optimizer (or operator) auto-applies a config
    change, the write MUST be atomic — a partial write that a
    concurrent reader picks up corrupts every downstream stage's
    config view. This function:
      1. Renders `data` to YAML in memory (raises ValueError on bad input).
      2. Writes to `{name}.yaml.tmp` in the same directory.
      3. fsync's the temp file so the bytes hit disk.
      4. `os.replace()` swaps temp → final in one inode update.
      5. Best-effort fsync of the parent directory so the rename
         survives a crash.

    `allow_create=False` (default) means we refuse to create a new
    config file — guards against typos that would otherwise silently
    invent `budget_v2.yaml` next to the real one. Pass `allow_create=True`
    on first-time config bootstrap.

    Raises ConfigSaveError if any step fails. The on-disk file is
    guaranteed unchanged on failure.
    """
    path = _config_path(name)
    if not path.exists() and not allow_create:
        raise ConfigSaveError(
            f"refusing to create new config {path.name}; pass allow_create=True "
            f"to bootstrap"
        )
    try:
        rendered = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    except (yaml.YAMLError, TypeError) as exc:
        raise ConfigSaveError(f"cannot render {name} to YAML: {exc}") from exc

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        # Write + fsync the temp file in same directory so os.replace is atomic
        with tmp_path.open("w") as fh:
            fh.write(rendered)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        # Best-effort fsync of parent directory so the rename survives a crash.
        # Not all platforms (notably some macOS volumes) support O_DIRECTORY
        # opens; failure here is non-fatal.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except OSError as exc:
        # Clean up the temp file on any failure so a janitor doesn't
        # mistake a stale .tmp for an in-progress write.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise ConfigSaveError(f"atomic save failed for {path}: {exc}") from exc

    # Invalidate any cached entries for this path so the next load()
    # picks up our new bytes immediately (mtime will also catch this,
    # but the explicit cache-clear protects against same-second writes
    # where mtime resolution is too coarse).
    for k in [k for k in _CACHE if k[0] == path]:
        _CACHE.pop(k, None)


__all__ = ["load", "reload", "save", "ConfigSaveError", "CONFIG_DIR"]
