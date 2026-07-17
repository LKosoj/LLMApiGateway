from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from docker import systemd_migration


pytestmark = pytest.mark.skipif(
    os.geteuid() != 0,
    reason="systemd migration ownership tests require root",
)


def _source(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / ".env").write_text("SAFE=1\n", encoding="utf-8")
    for name in systemd_migration.MANDATORY_CONFIG_FILENAMES:
        (source / name).write_bytes(b"{}")
    source_cache = tmp_path / "legacy-cache"
    source_cache.mkdir()
    (source_cache / "browser.bin").write_bytes(b"browser")
    return source, source_cache


def _arguments(tmp_path: Path, source: Path, source_cache: Path) -> dict[str, Path]:
    return {
        "source_root": source,
        "target_env_dir": tmp_path / "environment",
        "target_state_dir": tmp_path / "state",
        "target_cache_dir": tmp_path / "cache",
        "source_cache_dir": source_cache,
    }


def test_real_helper_plans_and_normalizes_safe_fhs_mode_drift(tmp_path: Path) -> None:
    source, source_cache = _source(tmp_path)
    arguments = _arguments(tmp_path, source, source_cache)
    systemd_migration.migrate(**arguments)
    expected_names = ["target-env-dir", "target-state-dir", "target-cache-dir"]
    roots = [arguments["target_env_dir"], arguments["target_state_dir"], arguments["target_cache_dir"]]
    for path, mode in zip(roots, (0o700, 0o710, 0o755), strict=True):
        path.chmod(mode)

    inventory = systemd_migration.inventory(**arguments)
    migrated = systemd_migration.migrate(**arguments)

    assert inventory["migration_required"] is True
    assert inventory["directory_metadata"] == [
        {"name": name, "status": "ready"} for name in expected_names
    ]
    assert migrated["migration_required"] is False
    assert migrated["directory_metadata"] == [
        {"name": name, "status": "normalized"} for name in expected_names
    ]
    assert [stat.S_IMODE(path.stat().st_mode) for path in roots] == [0o750] * 3
    repeated = systemd_migration.inventory(**arguments)
    assert repeated["migration_required"] is False
    assert repeated["directory_metadata"] == []


@pytest.mark.parametrize("unsafe", ["owner", "symlink", "non-directory"])
def test_real_helper_still_rejects_unsafe_fhs_roots(tmp_path: Path, unsafe: str) -> None:
    source, source_cache = _source(tmp_path)
    arguments = _arguments(tmp_path, source, source_cache)
    target_env = arguments["target_env_dir"]
    if unsafe == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        target_env.symlink_to(outside, target_is_directory=True)
    elif unsafe == "non-directory":
        target_env.write_text("unsafe", encoding="utf-8")
    else:
        target_env.mkdir(mode=0o750)
        os.chown(target_env, systemd_migration.SERVICE_UID, systemd_migration.SERVICE_GID)

    with pytest.raises(systemd_migration.MigrationError):
        systemd_migration.inventory(**arguments)

    assert not arguments["target_state_dir"].exists()
    assert not arguments["target_cache_dir"].exists()
