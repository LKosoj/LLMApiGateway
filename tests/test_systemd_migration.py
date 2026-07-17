from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from docker import systemd_migration
from llm_gateway_core.config.environment import (
    EnvironmentSubsetError,
    parse_environment_subset,
)


@pytest.fixture(autouse=True)
def _require_root_identity() -> None:
    if os.geteuid() != 0:
        pytest.skip("systemd migration ownership tests require root")


def _metadata(path: Path, uid: int, gid: int, mode: int) -> None:
    os.chown(path, uid, gid)
    path.chmod(mode)


def _assert_metadata(path: Path, uid: int, gid: int, mode: int) -> None:
    metadata = path.stat()
    assert (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) == (uid, gid, mode)


def _env_metadata(path: Path, mode: int = 0o640) -> None:
    _metadata(path, systemd_migration.ENV_UID, systemd_migration.SERVICE_GID, mode)


def _service_metadata(path: Path, mode: int) -> None:
    _metadata(path, systemd_migration.SERVICE_UID, systemd_migration.SERVICE_GID, mode)


def _roots(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "legacy"
    target_env = tmp_path / "environment"
    target_state = tmp_path / "state"
    target_cache = tmp_path / "cache"
    source.mkdir()
    target_env.mkdir()
    _env_metadata(target_env, 0o750)
    target_state.mkdir()
    _service_metadata(target_state, 0o750)
    (source / ".env").write_text("BASELINE=1\n", encoding="utf-8")
    for name in systemd_migration.MANDATORY_CONFIG_FILENAMES:
        (source / name).write_bytes(b"{}")
    return source, target_env, target_state, target_cache


def _inventory(
    source: Path,
    target_env: Path,
    target_state: Path,
    target_cache: Path,
    *,
    source_cache: Path | None = None,
) -> dict[str, object]:
    return systemd_migration.inventory(
        source_root=source,
        target_env_dir=target_env,
        target_state_dir=target_state,
        target_cache_dir=target_cache,
        source_cache_dir=source_cache,
    )


def _migrate(
    source: Path,
    target_env: Path,
    target_state: Path,
    target_cache: Path,
    *,
    source_cache: Path | None = None,
    image_migrator: object | None = None,
) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if image_migrator is not None:
        kwargs["image_migrator"] = image_migrator
    return systemd_migration.migrate(
        source_root=source,
        target_env_dir=target_env,
        target_state_dir=target_state,
        target_cache_dir=target_cache,
        source_cache_dir=source_cache,
        **kwargs,
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _make_cache(tmp_path: Path, *, size: int = 16) -> Path:
    cache = tmp_path / "legacy-cache"
    cache.mkdir()
    (cache / "browser.bin").write_bytes(b"x" * size)
    return cache


def _detached_wal(path: Path) -> None:
    live = path.with_name("live.db")
    connection = sqlite3.connect(live)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE events(value TEXT NOT NULL)")
        connection.execute("INSERT INTO events VALUES ('detached-wal')")
        connection.commit()
        shutil.copyfile(live, path)
        shutil.copyfile(live.with_name(f"{live.name}-wal"), path.with_name(f"{path.name}-wal"))
    finally:
        connection.close()
    for suffix in ("", "-wal", "-shm"):
        live.with_name(f"{live.name}{suffix}").unlink(missing_ok=True)


def test_production_ownership_constants_are_fixed_and_not_cli_overridable() -> None:
    module = ast.parse(Path(systemd_migration.__file__).read_text(encoding="utf-8"))
    constants: dict[str, int] = {}
    for statement in module.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id
            in {"ENV_UID", "SERVICE_UID", "SERVICE_GID"}
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, int)
        ):
            constants[statement.targets[0].id] = statement.value.value

    assert constants == {"ENV_UID": 0, "SERVICE_UID": 10001, "SERVICE_GID": 10001}
    assert not hasattr(systemd_migration, "ENV_GID")
    parser = systemd_migration.build_parser()
    option_strings = {option for action in parser._actions for option in action.option_strings}
    assert not {"--uid", "--gid", "--service-uid", "--service-gid"} & option_strings
    modules = Path(systemd_migration.__file__).parent.glob("*systemd_migration*.py")
    assert all(len(path.read_text(encoding="utf-8").splitlines()) < 1000 for path in modules)


def test_environment_inventory_is_duplicate_aware_and_never_exposes_values(
    tmp_path: Path,
) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    secret = "migration-secret-sentinel"
    payload = (
        f"API_KEY={secret}\n"
        "SECOND_SECRET=second-secret\n"
        "EMPTY=\n"
        'QUOTED="opaque value # preserved byte-for-byte"\n'
        "# COMMENTED_SECRET=must-not-appear\n"
    ).encode()
    (source / ".env").write_bytes(payload)

    report = _inventory(source, target_env, target_state, target_cache)
    serialized = json.dumps(report, sort_keys=True)

    assert report["environment"] == {
        "status": "ready",
        "assignments": [
            {"name": "API_KEY", "occurrences": 1, "duplicate": False},
            {"name": "EMPTY", "occurrences": 1, "duplicate": False},
            {"name": "QUOTED", "occurrences": 1, "duplicate": False},
            {"name": "SECOND_SECRET", "occurrences": 1, "duplicate": False},
        ],
    }
    assert secret not in serialized
    assert "second-secret" not in serialized
    assert "must-not-appear" not in serialized

    migrated = _migrate(source, target_env, target_state, target_cache)

    assert migrated["environment"]["status"] == "copied"
    target_file = target_env / "gateway.env"
    assert target_file.read_bytes() == payload
    _assert_metadata(target_file, systemd_migration.ENV_UID, systemd_migration.SERVICE_GID, 0o640)


def test_duplicate_environment_keys_block_before_every_mutation(
    tmp_path: Path,
) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    (source / ".env").write_text(
        "DUPLICATE=first-secret\nDUPLICATE=second-secret\n",
        encoding="utf-8",
    )

    with pytest.raises(systemd_migration.MigrationError) as caught:
        _migrate(source, target_env, target_state, target_cache)

    assert caught.value.reason == "duplicate-environment-keys"
    assert caught.value.names == ("DUPLICATE",)
    assert "first-secret" not in str(caught.value)
    assert "second-secret" not in str(caught.value)
    assert list(target_env.iterdir()) == []
    assert list(target_state.iterdir()) == []


def test_existing_environment_target_is_authoritative(tmp_path: Path) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    (source / ".env").write_text("API_KEY=source-secret\n", encoding="utf-8")
    authoritative = b"API_KEY=target-secret\n"
    target_file = target_env / "gateway.env"
    target_file.write_bytes(authoritative)
    _env_metadata(target_file)

    report = _migrate(source, target_env, target_state, target_cache)

    assert report["environment"]["status"] == "existing"
    assert target_file.read_bytes() == authoritative
    assert "source-secret" not in json.dumps(report, sort_keys=True)
    assert "target-secret" not in json.dumps(report, sort_keys=True)


def test_mandatory_environment_and_configs_must_exist_before_mutation(
    tmp_path: Path,
) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    (source / ".env").unlink()

    with pytest.raises(systemd_migration.MigrationError) as env_error:
        _migrate(source, target_env, target_state, target_cache)

    assert env_error.value.reason == "environment-missing"
    assert list(target_env.iterdir()) == []
    assert list(target_state.iterdir()) == []

    (source / ".env").write_text("SAFE=1\n", encoding="utf-8")
    missing = systemd_migration.MANDATORY_CONFIG_FILENAMES[0]
    (source / missing).unlink()
    with pytest.raises(systemd_migration.MigrationError) as config_error:
        _migrate(source, target_env, target_state, target_cache)

    assert config_error.value.reason == "mandatory-config-missing"
    assert config_error.value.names == (missing,)
    assert list(target_env.iterdir()) == []
    assert list(target_state.iterdir()) == []


def test_first_install_creates_only_safe_fhs_roots(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / ".env").write_text("SAFE=1\n", encoding="utf-8")
    for name in systemd_migration.MANDATORY_CONFIG_FILENAMES:
        (source / name).write_bytes(b"{}")
    target_env = tmp_path / "environment"
    target_state = tmp_path / "state"
    target_cache = tmp_path / "cache"

    preflight = _inventory(source, target_env, target_state, target_cache)
    migrated = _migrate(source, target_env, target_state, target_cache)

    assert preflight["migration_required"] is True
    assert migrated["migration_required"] is False
    for path, owner in (
        (target_env, systemd_migration.ENV_UID),
        (target_state, systemd_migration.SERVICE_UID),
    ):
        _assert_metadata(path, owner, systemd_migration.SERVICE_GID, 0o750)
    assert not target_cache.exists()


def test_existing_environment_content_uses_the_same_fail_closed_validation(
    tmp_path: Path,
) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    (source / ".env").unlink()
    existing = target_env / "gateway.env"
    existing.write_text(
        "GATEWAY_WORKERS=reserved-secret\n",
        encoding="utf-8",
    )
    _env_metadata(existing)

    with pytest.raises(systemd_migration.MigrationError) as caught:
        _inventory(source, target_env, target_state, target_cache)

    assert caught.value.reason == "reserved-environment-keys"
    assert caught.value.names == ("GATEWAY_WORKERS",)
    assert "reserved-secret" not in str(caught.value)
    assert list(target_state.iterdir()) == []


def test_unsafe_existing_target_blocks_without_repair_or_other_mutation(
    tmp_path: Path,
) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    (source / ".env").write_text("SAFE=value\n", encoding="utf-8")
    unsafe = target_env / "gateway.env"
    unsafe.write_text("SAFE=authoritative\n", encoding="utf-8")
    _env_metadata(unsafe, 0o644)

    with pytest.raises(systemd_migration.MigrationError) as caught:
        _migrate(source, target_env, target_state, target_cache)

    assert caught.value.reason == "unsafe-existing-target"
    assert caught.value.names == ("gateway.env",)
    assert stat.S_IMODE(unsafe.stat().st_mode) == 0o644
    assert list(target_state.iterdir()) == []
    assert not target_cache.exists()


def test_environment_rejects_reserved_keys_and_non_common_syntax_without_values(
    tmp_path: Path,
) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    reserved_payload = "\n".join(
        f"{name}=reserved-secret-{index}"
        for index, name in enumerate(systemd_migration.RESERVED_ENVIRONMENT_KEYS)
    )
    (source / ".env").write_text(f"{reserved_payload}\n", encoding="utf-8")

    with pytest.raises(systemd_migration.MigrationError) as caught:
        _inventory(source, target_env, target_state, target_cache)

    assert caught.value.reason == "reserved-environment-keys"
    assert caught.value.names == tuple(sorted(systemd_migration.RESERVED_ENVIRONMENT_KEYS))
    assert "reserved-secret" not in str(caught.value)

    unsupported = [
        "export SAFE=secret-value\n",
        "SAFE: secret-value\n",
        "SAFE=${OTHER}\n",
        'SAFE="unterminated\n',
        "SAFE=continued\\\nnext-line\n",
    ]
    for payload in unsupported:
        (source / ".env").write_text(payload, encoding="utf-8")
        with pytest.raises(systemd_migration.MigrationError) as syntax_error:
            _migrate(source, target_env, target_state, target_cache)
        assert syntax_error.value.reason == "unsupported-environment-syntax"
        assert "secret-value" not in str(syntax_error.value)
        assert list(target_env.iterdir()) == []
        assert list(target_state.iterdir()) == []


def test_six_configs_copy_atomically_without_overwrite_and_report_hashes(
    tmp_path: Path,
) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    expected: dict[str, bytes] = {}
    for index, name in enumerate(systemd_migration.CONFIG_FILENAMES):
        payload = json.dumps({"index": index}, separators=(",", ":")).encode()
        expected[name] = payload
        (source / name).write_bytes(payload)

    first = _migrate(source, target_env, target_state, target_cache)

    assert [entry["name"] for entry in first["configs"]] == list(systemd_migration.CONFIG_FILENAMES)
    for entry in first["configs"]:
        payload = expected[entry["name"]]
        assert entry == {"name": entry["name"], "status": "copied", "source_sha256": _sha256(payload),
                         "target_sha256": _sha256(payload)}
        target_file = target_state / "config" / entry["name"]
        assert target_file.read_bytes() == payload
        _assert_metadata(
            target_file, systemd_migration.SERVICE_UID, systemd_migration.SERVICE_GID, 0o660
        )

    changed = b'{"changed":true}'
    first_name = systemd_migration.CONFIG_FILENAMES[0]
    (source / first_name).write_bytes(changed)
    second = _migrate(source, target_env, target_state, target_cache)
    status = {entry["name"]: entry for entry in second["configs"]}[first_name]

    assert status["status"] == "existing"
    assert status["source_sha256"] == _sha256(changed)
    assert status["target_sha256"] == _sha256(expected[first_name])
    assert (target_state / "config" / first_name).read_bytes() == expected[first_name]


def test_sqlite_backup_includes_committed_wal_and_existing_target_wins(
    tmp_path: Path,
) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    source_db_dir = source / "db"
    source_db_dir.mkdir()
    source_db = source_db_dir / "tokens_usage.db"
    connection = sqlite3.connect(source_db)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE events(value TEXT NOT NULL)")
        connection.execute("INSERT INTO events VALUES ('from-wal')")
        connection.commit()
        assert source_db.with_name(f"{source_db.name}-wal").exists()

        report = _migrate(source, target_env, target_state, target_cache)

        target_db = target_state / source_db.name
        with sqlite3.connect(target_db) as copied:
            assert copied.execute("SELECT value FROM events").fetchall() == [("from-wal",)]
        state = {entry["name"]: entry for entry in report["state"]}
        assert state["tokens_usage.db"]["status"] == "copied"
        assert state["tokens_usage.db-wal"]["status"] == "included-by-sqlite-backup"
        assert state["tokens_usage.db-shm"]["status"] == "included-by-sqlite-backup"
        _assert_metadata(target_db, systemd_migration.SERVICE_UID, systemd_migration.SERVICE_GID, 0o660)

        connection.execute("INSERT INTO events VALUES ('later-source-row')")
        connection.commit()
        repeated = _migrate(source, target_env, target_state, target_cache)
        target_state = {entry["name"]: entry for entry in repeated["state"]}
        assert target_state["tokens_usage.db"]["status"] == "existing"
        with sqlite3.connect(target_db) as copied:
            assert copied.execute("SELECT value FROM events").fetchall() == [("from-wal",)]
    finally:
        connection.close()


def test_unknown_state_blocks_every_mutation_and_lists_only_basenames(
    tmp_path: Path,
) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    (source / ".env").write_text("SECRET=must-not-leak\n", encoding="utf-8")
    state_dir = source / "db"
    state_dir.mkdir()
    unknown_db = state_dir / "unknown-secret-state.db"
    unknown_file = state_dir / "unclassified-state"
    unknown_db.write_bytes(b"private database bytes")
    unknown_file.write_text("private state", encoding="utf-8")

    with pytest.raises(systemd_migration.MigrationError) as caught:
        _migrate(source, target_env, target_state, target_cache)

    assert caught.value.reason == "unknown-state"
    assert caught.value.names == (
        "unclassified-state",
        "unknown-secret-state.db",
    )
    assert "private database bytes" not in str(caught.value)
    assert "private state" not in str(caught.value)
    assert list(target_env.iterdir()) == []
    assert list(target_state.iterdir()) == []
    assert not target_cache.exists()
    assert unknown_db.read_bytes() == b"private database bytes"
    assert unknown_file.read_text(encoding="utf-8") == "private state"


def test_lock_temp_dead_letter_and_sqlite_sidecars_are_classified_explicitly(
    tmp_path: Path,
) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    state_dir = source / "db"
    state_dir.mkdir()
    lock = state_dir / ".llmgateway-single-process.lock"
    temporary = state_dir / ".migration.tmp"
    dead_letter = state_dir / "tokens_usage.db.dead-letter.jsonl"
    lock.write_text("pid", encoding="utf-8")
    temporary.write_text("partial", encoding="utf-8")
    durable = b'{"reason":"diagnostic"}\n'
    dead_letter.write_bytes(durable)

    report = _inventory(source, target_env, target_state, target_cache)
    state = {entry["name"]: entry for entry in report["state"]}

    assert state[lock.name]["status"] == "ephemeral-lock-skip"
    assert state[temporary.name]["status"] == "temporary-skip"
    assert state[dead_letter.name]["status"] == "ready"

    migrated = _migrate(source, target_env, target_state, target_cache)
    migrated_state = {entry["name"]: entry for entry in migrated["state"]}

    assert migrated_state[dead_letter.name]["status"] == "copied"
    assert (target_state / dead_letter.name).read_bytes() == durable
    assert not (target_state / lock.name).exists()
    assert not (target_state / temporary.name).exists()
    assert lock.exists()
    assert temporary.exists()


def test_outputs_migration_delegates_to_verified_image_storage_api(
    tmp_path: Path,
) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    source_images = source / "outputs" / "images"
    source_images.mkdir(parents=True)
    (source_images / "generated.png").write_bytes(b"not-copied-by-helper")
    calls: list[tuple[Path, Path, Path]] = []

    def image_migrator(
        source_path: Path,
        outputs_path: Path,
        manifest_path: Path,
    ) -> SimpleNamespace:
        calls.append((source_path, outputs_path, manifest_path))
        return SimpleNamespace(count=1, tree_sha256="verified-image-tree")

    report = _migrate(source, target_env, target_state, target_cache, image_migrator=image_migrator)

    assert calls == [
        (source_images, target_state / "outputs", target_state / ".migration" / "outputs-images.manifest.json")
    ]
    assert report["outputs"] == {"status": "migrated-via-image-storage-cli", "count": 1,
                                 "tree_sha256": "verified-image-tree"}
    assert not (target_state / "outputs" / "images" / "generated.png").exists()

    outputs = target_state / "outputs"
    target_images = outputs / "images"
    target_images.mkdir(parents=True)
    for directory in (outputs, target_images):
        _service_metadata(directory, 0o770)
    existing_image = target_images / "existing.png"
    existing_image.write_bytes(b"existing")
    _service_metadata(existing_image, 0o660)

    repeated = _migrate(source, target_env, target_state, target_cache, image_migrator=image_migrator)

    assert repeated["outputs"]["status"] == "existing"
    assert len(calls) == 1
    assert existing_image.read_bytes() == b"existing"


def test_default_outputs_delegate_initializes_then_calls_r3_4_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from llm_gateway_core.services import image_storage_cli

    calls: list[tuple[str, tuple[Path, ...]]] = []
    expected = SimpleNamespace(count=1, tree_sha256="tree")

    def initialize(outputs: Path) -> None:
        calls.append(("initialize", (outputs,)))

    def migrate_images(
        source: Path,
        outputs: Path,
        manifest: Path,
    ) -> SimpleNamespace:
        calls.append(("migrate", (source, outputs, manifest)))
        return expected

    monkeypatch.setattr(image_storage_cli, "initialize_volume", initialize)
    monkeypatch.setattr(image_storage_cli, "migrate_images", migrate_images)
    source = tmp_path / "source"
    outputs = tmp_path / "outputs"
    manifest = tmp_path / "manifest.json"

    result = systemd_migration._default_image_migrator(source, outputs, manifest)

    assert result is expected
    assert calls == [("initialize", (outputs,)), ("migrate", (source, outputs, manifest))]


def test_cloakbrowser_cache_is_staged_with_manifest_and_source_is_preserved(
    tmp_path: Path,
) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    source_cache = tmp_path / "legacy-cloakbrowser-cache"
    browser_dir = source_cache / "chromium-test"
    browser_dir.mkdir(parents=True)
    browser = browser_dir / "chrome"
    marker = source_cache / "latest_version"
    browser.write_bytes(b"browser-binary")
    browser.chmod(0o755)
    marker.write_text("test-version", encoding="utf-8")
    fixed_mtime = 1_700_000_000_123_456_789
    os.utime(browser, ns=(fixed_mtime, fixed_mtime))
    os.utime(marker, ns=(fixed_mtime + 1, fixed_mtime + 1))

    report = _migrate(source, target_env, target_state, target_cache, source_cache=source_cache)

    cache_report = report["cloakbrowser_cache"]
    assert cache_report["status"] == "copied"
    assert cache_report["manifest"]["count"] == 2
    manifest_path = target_cache / ".migration-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == cache_report["manifest"]
    assert manifest["files"] == [
        {
            "mode": 0o770,
            "mtime_ns": fixed_mtime,
            "path": "chromium-test/chrome",
            "sha256": _sha256(b"browser-binary"),
            "size": len(b"browser-binary"),
        },
        {
            "mode": 0o660,
            "mtime_ns": fixed_mtime + 1,
            "path": "latest_version",
            "sha256": _sha256(b"test-version"),
            "size": len(b"test-version"),
        },
    ]
    assert browser.read_bytes() == b"browser-binary"
    for destination in (
        target_cache,
        target_cache / "chromium-test",
        target_cache / "chromium-test" / "chrome",
        target_cache / "latest_version",
        manifest_path,
    ):
        expected_mode = 0o660
        if destination == target_cache:
            expected_mode = 0o750
        elif destination.is_dir() or destination.name == "chrome":
            expected_mode = 0o770
        _assert_metadata(
            destination, systemd_migration.SERVICE_UID, systemd_migration.SERVICE_GID, expected_mode
        )

    browser.write_bytes(b"new-source-binary")
    repeated = _migrate(source, target_env, target_state, target_cache, source_cache=source_cache)

    assert repeated["cloakbrowser_cache"]["status"] == "existing"
    assert (target_cache / "chromium-test" / "chrome").read_bytes() == b"browser-binary"


def test_legacy_logs_are_report_only(tmp_path: Path) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    logs = source / "logs"
    logs.mkdir()
    (logs / "gateway.log").write_text("secret log line", encoding="utf-8")

    inventory = _inventory(source, target_env, target_state, target_cache)
    migrated = _migrate(source, target_env, target_state, target_cache)

    assert inventory["legacy_logs"] == {"status": "report-only", "entry_count": 1}
    assert migrated["legacy_logs"] == inventory["legacy_logs"]
    assert not (target_state / "logs").exists()
    assert (logs / "gateway.log").exists()
    assert "secret log line" not in json.dumps(migrated, sort_keys=True)


def test_cli_inventory_is_deterministic_secret_safe_and_requires_absolute_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    secret = "cli-secret-sentinel"
    (source / ".env").write_text(f"TOKEN={secret}\n", encoding="utf-8")

    arguments = [
        "inventory",
        "--source-root",
        os.fspath(source),
        "--target-env-dir",
        os.fspath(target_env),
        "--target-state-dir",
        os.fspath(target_state),
        "--target-cache-dir",
        os.fspath(target_cache),
    ]
    assert systemd_migration.main(arguments) == 0
    first = capsys.readouterr()
    assert systemd_migration.main(arguments) == 0
    second = capsys.readouterr()

    assert first.err == second.err == ""
    assert first.out == second.out
    assert secret not in first.out
    assert json.loads(first.out)["environment"]["assignments"] == [
        {"duplicate": False, "name": "TOKEN", "occurrences": 1}]

    with pytest.raises(systemd_migration.MigrationError) as caught:
        systemd_migration.inventory(
            source_root=Path("relative"),
            target_env_dir=target_env,
            target_state_dir=target_state,
            target_cache_dir=target_cache,
        )
    assert caught.value.reason == "path-not-absolute"


def test_all_caller_supplied_roots_must_be_safe_and_disjoint(tmp_path: Path) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)

    with pytest.raises(systemd_migration.MigrationError) as caught:
        systemd_migration.inventory(
            source_root=source,
            target_env_dir=target_env,
            target_state_dir=target_state,
            target_cache_dir=target_state / "nested-cache",
        )

    assert caught.value.reason == "path-overlap"
    assert list(target_env.iterdir()) == []
    assert list(target_state.iterdir()) == []
    assert not target_cache.exists()


@pytest.mark.parametrize("crash_state", ["empty", "incomplete-empty", "incomplete-complete"])
def test_real_r3_4_output_crash_states_resume_through_initialize_and_migrate(
    tmp_path: Path,
    crash_state: str,
) -> None:
    from llm_gateway_core.services.image_storage_cli import INCOMPLETE_MARKER, initialize_volume

    source, target_env, target_state, target_cache = _roots(tmp_path)
    source_image = source / "outputs" / "images" / "generated.png"
    source_image.parent.mkdir(parents=True)
    source_image.write_bytes(b"recovered-image")
    fixed_mtime = 1_700_000_000_000_000_123
    os.utime(source_image, ns=(fixed_mtime, fixed_mtime))
    outputs = target_state / "outputs"
    initialize_volume(outputs)
    if crash_state == "incomplete-complete":
        target_image = outputs / "images" / source_image.name
        shutil.copyfile(source_image, target_image)
        os.utime(target_image, ns=(fixed_mtime, fixed_mtime))
        _service_metadata(target_image, 0o660)
    if crash_state.startswith("incomplete"):
        marker = outputs / INCOMPLETE_MARKER
        marker.write_text("incomplete\n", encoding="utf-8")
        _metadata(marker, 0, 0, 0o600)

    assert _inventory(source, target_env, target_state, target_cache)["outputs"] == {"status": "ready"}
    report = _migrate(source, target_env, target_state, target_cache)

    assert report["outputs"]["status"] == "migrated-via-image-storage-cli"
    assert (outputs / "images" / source_image.name).read_bytes() == b"recovered-image"
    assert not (outputs / INCOMPLETE_MARKER).exists()


def test_sqlite_detached_wal_snapshot_does_not_touch_source_or_create_shm(tmp_path: Path) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    source_db_dir = source / "db"
    source_db_dir.mkdir()
    database = source_db_dir / "tokens_usage.db"
    _detached_wal(database)
    watched = (database, database.with_name(f"{database.name}-wal"))
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in watched}

    _migrate(source, target_env, target_state, target_cache)

    assert not database.with_name(f"{database.name}-shm").exists()
    assert before == {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in watched}
    with sqlite3.connect(target_state / database.name) as copied:
        assert copied.execute("SELECT value FROM events").fetchall() == [("detached-wal",)]


def test_all_three_known_databases_use_verified_backup(tmp_path: Path) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    source_db_dir = source / "db"
    source_db_dir.mkdir()
    for index, name in enumerate(systemd_migration.KNOWN_DB_FILENAMES):
        with sqlite3.connect(source_db_dir / name) as connection:
            connection.execute("CREATE TABLE marker(value INTEGER NOT NULL)")
            connection.execute("INSERT INTO marker VALUES (?)", (index,))
    _migrate(source, target_env, target_state, target_cache)
    for index, name in enumerate(systemd_migration.KNOWN_DB_FILENAMES):
        with sqlite3.connect(target_state / name) as copied:
            assert copied.execute("SELECT value FROM marker").fetchone() == (index,)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("PLAIN=value\n", {"PLAIN": "value"}),
        ("EMPTY=\n", {"EMPTY": ""}),
        ("SINGLE='literal $ value # kept'\n", {"SINGLE": "literal $ value # kept"}),
        ('DOUBLE="opaque value # kept"\n', {"DOUBLE": "opaque value # kept"}),
        ("export BAD=value\n", None),
        ("BAD=${OTHER}\n", None),
        ("BAD=value # comment\n", None),
        ("BAD=continued\\\nnext=value\n", None),
    ],
)
def test_common_environment_subset_parity_table(payload: str, expected: dict[str, str] | None) -> None:
    if expected is None:
        with pytest.raises(EnvironmentSubsetError):
            parse_environment_subset(payload)
    else:
        assert parse_environment_subset(payload) == expected


def test_sparse_2_1_gib_inventory_and_large_cache_copy_stay_bounded(tmp_path: Path) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    source_cache = _make_cache(tmp_path)
    large = source_cache / "browser.bin"
    large.write_bytes(b"")
    os.truncate(large, 2_1 * 1024**3)
    script = Path(systemd_migration.__file__)
    arguments = [
        os.fspath(script), "inventory", "--source-root", os.fspath(source),
        "--target-env-dir", os.fspath(target_env), "--target-state-dir", os.fspath(target_state),
        "--target-cache-dir", os.fspath(target_cache), "--source-cache-dir", os.fspath(source_cache),
    ]
    limit = 192 * 1024**2
    runner = (
        "import resource,runpy,sys;"
        f"resource.setrlimit(resource.RLIMIT_AS,({limit},{limit}));"
        "sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')"
    )
    command = [sys.executable, "-c", runner, *arguments]
    first = subprocess.run(command, cwd="/", capture_output=True, text=True, timeout=30)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["cloakbrowser_cache"] == {"status": "ready"}
    os.truncate(large, 64 * 1024**2)
    arguments[1] = "migrate"
    command[-len(arguments):] = arguments
    second = subprocess.run(command, cwd="/", capture_output=True, text=True, timeout=30)
    assert second.returncode == 0, second.stderr
    assert (target_cache / large.name).stat().st_size == 64 * 1024**2


@pytest.mark.parametrize("race", ["symlink", "fifo", "metadata"])
def test_cache_publish_race_never_clobbers_and_is_revalidated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    race: str,
) -> None:
    from docker import _systemd_migration_apply as apply

    source, target_env, target_state, target_cache = _roots(tmp_path)
    source_cache = _make_cache(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = apply.rename_noreplace

    def inject(parent: int, staging: str, target: str) -> None:
        if race == "symlink":
            target_cache.symlink_to(outside, target_is_directory=True)
        elif race == "fifo":
            os.mkfifo(target_cache)
        else:
            original(parent, staging, target)
            target_cache.chmod(0o777)
            return
        original(parent, staging, target)

    monkeypatch.setattr(apply, "rename_noreplace", inject)
    with pytest.raises(systemd_migration.MigrationError):
        _migrate(source, target_env, target_state, target_cache, source_cache=source_cache)
    assert list(outside.iterdir()) == []
    assert not list(tmp_path.glob(".cache.migration-*"))


def test_diagnostics_and_argparse_are_bounded_hashed_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    state_dir = source / "db"
    state_dir.mkdir()
    for index in range(24):
        (state_dir / f"unsafe-secret-{index}-\n-🔥").write_bytes(b"secret")
    with pytest.raises(systemd_migration.MigrationError) as caught:
        _inventory(source, target_env, target_state, target_cache)
    assert len(caught.value.names) == 17
    assert "unsafe-secret" not in str(caught.value)
    assert systemd_migration.main(["inventory", "--unsafe-secret=do-not-print"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"names": [], "reason": "invalid-arguments", "status": "error"}
    assert "do-not-print" not in captured.err


def test_inventory_is_shallow_for_active_database_and_output_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from docker import _systemd_migration_plan as plan

    source, target_env, target_state, target_cache = _roots(tmp_path)
    (source / "db").mkdir()
    (source / "db" / "tokens_usage.db").write_bytes(b"actively-changing-not-sqlite")
    images = source / "outputs" / "images"
    images.mkdir(parents=True)
    (images / "active.png").write_bytes(b"actively-changing-image")
    monkeypatch.setattr(plan, "_image_inventory", lambda path: pytest.fail(f"deep output read: {path}"))
    report = _inventory(source, target_env, target_state, target_cache)
    assert report["outputs"] == {"status": "ready"}
    assert {item["name"]: item["status"] for item in report["state"]}["tokens_usage.db"] == "ready"


def test_directory_metadata_unlink_and_nested_fsync_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from docker import _systemd_migration_fs as migration_fs

    calls: list[Path] = []
    monkeypatch.setattr(migration_fs, "fsync_directory", lambda path: calls.append(path))
    root = tmp_path / "durable"
    migration_fs.ensure_directory(root, uid=0, gid=10001, mode=0o750)
    assert calls == [root, tmp_path]
    child = root / "child"
    leaf = child / "leaf"
    leaf.mkdir(parents=True)
    (leaf / "payload").write_bytes(b"x")
    calls.clear()
    migration_fs.sync_directories_bottom_up(root)
    assert calls == [leaf, child, root]
    removed = root / "removed"
    removed.write_bytes(b"x")
    migration_fs.unlink_and_sync(removed)
    assert calls[-1] == root


def test_context_manager_body_errors_keep_their_operation_classification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from docker import _systemd_migration_fs as migration_fs

    source = tmp_path / "source"
    source.write_bytes(b"payload")
    snapshot = migration_fs.regular_snapshot(source, name=source.name)
    assert snapshot is not None

    def fail_write(_descriptor: int, _payload: object) -> int:
        raise OSError("destination-write")

    with monkeypatch.context() as scoped:
        scoped.setattr(migration_fs.os, "write", fail_write)
        with pytest.raises(systemd_migration.MigrationError) as write_error:
            migration_fs.copy_snapshot_to_new(snapshot, tmp_path / "target", uid=0, gid=0, mode=0o600)
    assert write_error.value.reason == "target-write-failed"
    original_fstat = migration_fs.os.fstat
    calls = 0

    def fail_post_yield(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("source-stat")
        return original_fstat(descriptor)

    with monkeypatch.context() as scoped:
        scoped.setattr(migration_fs.os, "fstat", fail_post_yield)
        with pytest.raises(systemd_migration.MigrationError) as source_error:
            with migration_fs.open_snapshot(snapshot, name=source.name):
                pass
    assert source_error.value.reason == "source-changed"
    with pytest.raises(OSError, match="directory-body"):
        with migration_fs.open_directory(tmp_path):
            raise OSError("directory-body")


@pytest.mark.parametrize("unsafe", ["mode", "owner", "symlink"])
def test_target_sidecar_is_validated_when_source_has_the_same_sidecar(tmp_path: Path, unsafe: str) -> None:
    source, target_env, target_state, target_cache = _roots(tmp_path)
    source_db = source / "db"
    source_db.mkdir()
    database = "tokens_usage.db"
    (source_db / database).write_bytes(b"source")
    source_sidecar = source_db / f"{database}-wal"
    source_sidecar.write_bytes(b"source-wal")
    target_database = target_state / database
    target_database.write_bytes(b"target")
    _service_metadata(target_database, 0o660)
    target_sidecar = target_state / source_sidecar.name
    if unsafe == "symlink":
        target_sidecar.symlink_to(source_sidecar)
    else:
        target_sidecar.write_bytes(b"target-wal")
        uid = 0 if unsafe == "owner" else systemd_migration.SERVICE_UID
        _metadata(target_sidecar, uid, systemd_migration.SERVICE_GID, 0o644 if unsafe == "mode" else 0o660)

    with pytest.raises(systemd_migration.MigrationError) as caught:
        _inventory(source, target_env, target_state, target_cache)
    assert caught.value.reason == "unsafe-existing-target"
    assert caught.value.names == (target_sidecar.name,)


def test_sqlite_cleanup_attempts_every_path_before_deterministic_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from docker import _systemd_migration_fs as migration_fs

    temporary = tmp_path / ".tokens_usage.db.migration-test"
    staging = tmp_path / ".tokens_usage.db.snapshot-test"
    calls: list[str] = []

    def fail_first_unlink(path: Path, *, missing_ok: bool) -> None:
        assert missing_ok
        calls.append(path.name)
        if path == temporary:
            raise OSError("unlink")

    def fail_staging(path: Path) -> None:
        calls.append(path.name)
        raise systemd_migration.MigrationError("temporary-cleanup-failed", ("staging",))

    monkeypatch.setattr(migration_fs, "unlink_and_sync", fail_first_unlink)
    monkeypatch.setattr(migration_fs, "remove_tree_and_sync", fail_staging)
    with pytest.raises(systemd_migration.MigrationError) as caught:
        migration_fs._cleanup_sqlite_temporary(temporary, staging, "tokens_usage.db")

    assert caught.value.reason == "temporary-cleanup-failed"
    assert caught.value.names == ("tokens_usage.db",)
    expected = [temporary.name, *(f"{temporary.name}{suffix}" for suffix in migration_fs.SQLITE_SIDECAR_SUFFIXES), staging.name]
    assert calls == expected
