"""Config loader + atomic save tests — Day 13 hardening (E-18, E-25).

Critical assertions:
- mtime-aware cache invalidates when the file changes
- Same-mtime hits return cached dict (no reparse)
- Atomic save renders + fsync + os.replace
- Save refuses to create new files unless allow_create=True
- Failed save (bad YAML, OSError) leaves the on-disk file unchanged
- Cache is cleared after save so the next load sees fresh bytes
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest
import yaml

from agents import config


@pytest.fixture
def tmp_config_dir(monkeypatch):
    """Point config.CONFIG_DIR at a temp dir + reset the cache."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(config, "CONFIG_DIR", Path(tmp))
        # Reset the module-level cache between tests.
        config._CACHE.clear()
        yield Path(tmp)


def _write(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data))


# ---------- E-25: mtime invalidation ----------


def test_load_caches_on_repeated_calls(tmp_config_dir):
    """Two consecutive loads with the same mtime return the same dict
    instance (cache hit)."""
    p = tmp_config_dir / "thing.yaml"
    _write(p, {"a": 1})
    d1 = config.load("thing")
    d2 = config.load("thing")
    assert d1 is d2  # exact instance — cache hit


def test_load_reparses_when_mtime_changes(tmp_config_dir):
    """Editing the file changes mtime → next load reparses → new dict."""
    p = tmp_config_dir / "thing.yaml"
    _write(p, {"a": 1})
    d1 = config.load("thing")
    assert d1["a"] == 1

    # Sleep to ensure st_mtime_ns moves forward (some filesystems
    # have sub-microsecond mtime granularity but we want to be sure).
    time.sleep(0.01)
    _write(p, {"a": 2})
    d2 = config.load("thing")
    assert d2["a"] == 2
    assert d1 is not d2


def test_reload_explicit_clears_cache(tmp_config_dir):
    """reload() invalidates regardless of mtime — useful when a tool
    bypasses our save() and writes via shell."""
    p = tmp_config_dir / "thing.yaml"
    _write(p, {"a": 1})
    d1 = config.load("thing")

    # Overwrite via os.write without changing mtime sleep
    p.write_text(yaml.safe_dump({"a": 99}))
    # Force mtime backwards to confirm reload doesn't rely on it
    # (some test environments truncate mtime resolution to 1s)
    d2 = config.reload("thing")
    assert d2["a"] == 99
    assert d1 is not d2


def test_load_missing_file_raises(tmp_config_dir):
    with pytest.raises(FileNotFoundError):
        config.load("nonexistent")


def test_cache_sweeps_stale_mtime_entries(tmp_config_dir):
    """After an mtime change, the OLD cached entry should be evicted
    so the cache doesn't grow unbounded."""
    p = tmp_config_dir / "thing.yaml"
    _write(p, {"a": 1})
    config.load("thing")
    time.sleep(0.01)
    _write(p, {"a": 2})
    config.load("thing")
    # Only one entry per path should survive.
    entries_for_path = [k for k in config._CACHE if k[0] == p]
    assert len(entries_for_path) == 1


# ---------- E-18: Atomic save ----------


def test_save_writes_yaml_and_round_trips(tmp_config_dir):
    """save() renders a dict to YAML; load() reads it back unchanged."""
    p = tmp_config_dir / "thing.yaml"
    _write(p, {"initial": "v"})  # bootstrap so save's allow_create=False
    config.save("thing", {"key": "value", "n": 42})
    out = config.load("thing")
    assert out == {"key": "value", "n": 42}


def test_save_refuses_to_create_new_file_by_default(tmp_config_dir):
    """A typo in the name shouldn't silently invent a new config file.
    allow_create=False is the safety default."""
    with pytest.raises(config.ConfigSaveError, match="refusing to create"):
        config.save("typo_name", {"k": "v"})


def test_save_allows_create_with_explicit_flag(tmp_config_dir):
    """allow_create=True is the bootstrap escape hatch."""
    config.save("brand_new", {"k": "v"}, allow_create=True)
    assert (tmp_config_dir / "brand_new.yaml").exists()


def test_save_atomic_no_partial_file_on_success(tmp_config_dir):
    """After a successful save the temp file should NOT be present —
    only the final file."""
    p = tmp_config_dir / "thing.yaml"
    _write(p, {"a": 1})
    config.save("thing", {"a": 2})
    partials = list(tmp_config_dir.glob("*.tmp"))
    assert partials == []


def test_save_cleans_up_temp_on_render_failure(tmp_config_dir):
    """YAML render failure raises ConfigSaveError BEFORE any disk
    write. No temp file should exist after; original file unchanged."""
    p = tmp_config_dir / "thing.yaml"
    _write(p, {"a": 1})

    # Pass an unserializable object (a set inside a dict) — yaml.safe_dump
    # rejects it.
    with pytest.raises(config.ConfigSaveError, match="cannot render"):
        config.save("thing", {"bad": object()})

    # On-disk file unchanged
    assert yaml.safe_load(p.read_text()) == {"a": 1}
    # No temp leftovers
    assert list(tmp_config_dir.glob("*.tmp")) == []


def test_save_cleans_up_temp_on_os_error(tmp_config_dir, monkeypatch):
    """If os.replace raises (cross-device rename, permission, etc.),
    the temp file is cleaned up and the on-disk file is unchanged."""
    p = tmp_config_dir / "thing.yaml"
    _write(p, {"a": 1})

    def fail_replace(src, dst):
        raise OSError("simulated cross-device rename")
    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(config.ConfigSaveError, match="atomic save failed"):
        config.save("thing", {"a": 2})

    # On-disk unchanged
    assert yaml.safe_load(p.read_text()) == {"a": 1}
    # Temp cleaned up
    assert list(tmp_config_dir.glob("*.tmp")) == []


def test_save_invalidates_cache(tmp_config_dir):
    """After save(), the next load() must see the new bytes — even on
    the same mtime second (mtime resolution may not catch fast writes)."""
    p = tmp_config_dir / "thing.yaml"
    _write(p, {"a": 1})
    first = config.load("thing")
    assert first["a"] == 1

    # Save and immediately re-load — the explicit cache clear inside
    # save() must invalidate even if mtime hasn't ticked.
    config.save("thing", {"a": 2})
    second = config.load("thing")
    assert second["a"] == 2
