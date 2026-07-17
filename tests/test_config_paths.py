"""Unified application-root and configuration-path resolution.

Before, each module that needed the repo root recomputed
``Path(__file__).parent.parent.parent`` (or .parent.parent.parent.parent!),
which silently broke whenever a module moved. Everything now imports
from one module, which these tests pin.
"""
import os
import unittest
from pathlib import Path

import pytest

from llm_gateway_core.config import paths as project_paths
from llm_gateway_core.config.loader import ConfigError, ConfigLoader


FILENAME_ENVIRONMENT = (
    "PROVIDERS_FILENAME",
    "FALLBACK_RULES_FILENAME",
    "OPERATION_RULES_FILENAME",
    "FUSION_RULES_FILENAME",
    "MODEL_RULES_FILENAME",
    "ROUTER_RULES_FILENAME",
)


class ProjectPathsTests(unittest.TestCase):
    def test_project_root_points_at_repository_root(self):
        # main.py is the entrypoint — by definition, it lives in the project root.
        self.assertTrue((project_paths.PROJECT_ROOT / "main.py").is_file())

    def test_project_root_is_absolute(self):
        self.assertTrue(project_paths.PROJECT_ROOT.is_absolute())

    def test_static_dir_is_under_project_root(self):
        self.assertEqual(
            project_paths.STATIC_DIR,
            project_paths.PROJECT_ROOT / "static",
        )

    def test_loader_uses_same_project_root(self):
        loader = ConfigLoader()
        self.assertEqual(Path(loader.project_root), project_paths.PROJECT_ROOT)


def test_created_db_dir_is_owner_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_dir = tmp_path / "db"
    monkeypatch.setenv("GATEWAY_DB_DIR", os.fspath(db_dir))

    resolved = project_paths.resolve_db_dir()

    assert resolved == db_dir
    assert db_dir.stat().st_mode & 0o777 == 0o700


def test_existing_db_dir_keeps_the_mode_the_operator_chose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolving a path must not silently revert a deliberate chmod.

    This runs on every call, so re-locking the directory here would undo an
    operator's widening at the next restart and leave only a log line behind.
    """
    db_dir = tmp_path / "db"
    db_dir.mkdir(mode=0o750)
    monkeypatch.setenv("GATEWAY_DB_DIR", os.fspath(db_dir))

    project_paths.resolve_db_dir()

    assert db_dir.stat().st_mode & 0o777 == 0o750


def test_loader_resolves_relative_defaults_from_absolute_app_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_DIR", os.fspath(tmp_path))
    for name in FILENAME_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)

    loader = ConfigLoader()

    assert loader.project_root == tmp_path
    assert loader.providers_path == tmp_path / "providers.json"
    assert loader.fallback_rules_path == tmp_path / "models_fallback_rules.json"


def test_loader_keeps_absolute_filename_and_roots_relative_filename_at_app_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_dir = tmp_path / "application root"
    absolute_fallback = tmp_path / "shared config" / "fallback.json"
    monkeypatch.setenv("APP_DIR", os.fspath(app_dir))

    loader = ConfigLoader(
        providers_filename="config/providers.json",
        fallback_rules_filename=os.fspath(absolute_fallback),
    )

    assert loader.providers_path == app_dir / "config" / "providers.json"
    assert loader.fallback_rules_path == absolute_fallback


@pytest.mark.parametrize("value", ["", "relative/application"])
def test_loader_rejects_invalid_app_dir(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_DIR", value)

    with pytest.raises(ValueError, match="APP_DIR"):
        ConfigLoader()


@pytest.mark.parametrize("environment_name", FILENAME_ENVIRONMENT)
def test_loader_rejects_explicitly_empty_filename_environment(
    environment_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_DIR", os.fspath(tmp_path))
    monkeypatch.setenv(environment_name, "")

    with pytest.raises(ValueError, match=environment_name):
        ConfigLoader()


def test_loader_rejects_explicitly_empty_filename_argument(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="PROVIDERS_FILENAME"):
        ConfigLoader(providers_filename="")


def test_loader_missing_fallback_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "providers.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("APP_DIR", os.fspath(tmp_path))
    for name in FILENAME_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ConfigError, match="models_fallback_rules.json.*not found"):
        ConfigLoader().load_fallback_rules()


def test_outputs_dir_defaults_to_absolute_project_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GATEWAY_OUTPUTS_DIR", raising=False)

    assert project_paths.resolve_outputs_dir() == project_paths.PROJECT_ROOT / "outputs"
    assert project_paths.resolve_outputs_dir().is_absolute()


def test_outputs_dir_uses_explicit_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "persistent outputs"
    monkeypatch.setenv("GATEWAY_OUTPUTS_DIR", os.fspath(configured))

    assert project_paths.resolve_outputs_dir() == configured


@pytest.mark.parametrize("value", ["", "   ", "relative/outputs"])
def test_outputs_dir_rejects_invalid_explicit_value(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_OUTPUTS_DIR", value)

    with pytest.raises(ValueError, match="GATEWAY_OUTPUTS_DIR"):
        project_paths.resolve_outputs_dir()


@pytest.mark.parametrize("value", ["/", "//", "/tmp/..", "/tmp/"])
def test_outputs_dir_rejects_unsafe_absolute_value(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_OUTPUTS_DIR", value)

    with pytest.raises(ValueError, match="GATEWAY_OUTPUTS_DIR"):
        project_paths.resolve_outputs_dir()


def test_outputs_images_dir_is_derived_from_outputs_dir() -> None:
    assert project_paths.OUTPUTS_IMAGES_DIR == project_paths.OUTPUTS_DIR / "images"


def test_log_dir_defaults_to_absolute_project_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLMGATEWAY_LOG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    assert project_paths.resolve_log_dir() == project_paths.PROJECT_ROOT / "logs"
    assert project_paths.resolve_log_dir().is_absolute()


def test_log_dir_uses_explicit_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "persistent logs"
    monkeypatch.setenv("LLMGATEWAY_LOG_DIR", os.fspath(configured))

    assert project_paths.resolve_log_dir() == configured


@pytest.mark.parametrize("value", ["", "   ", "relative/logs"])
def test_log_dir_rejects_invalid_explicit_value(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLMGATEWAY_LOG_DIR", value)

    with pytest.raises(ValueError, match="LLMGATEWAY_LOG_DIR"):
        project_paths.resolve_log_dir()


@pytest.mark.parametrize("value", ["/", "//", "/tmp/..", "/tmp/"])
def test_log_dir_rejects_unsafe_absolute_value(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLMGATEWAY_LOG_DIR", value)

    with pytest.raises(ValueError, match="LLMGATEWAY_LOG_DIR"):
        project_paths.resolve_log_dir()


if __name__ == "__main__":
    unittest.main()
