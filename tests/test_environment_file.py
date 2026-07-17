from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from llm_gateway_core.config import environment


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_SOURCE = PROJECT_ROOT / "llm_gateway_core" / "config" / "settings.py"
BOOTSTRAP_SOURCE = (
    PROJECT_ROOT
    / "llm_gateway_core"
    / "services"
    / "single_process_bootstrap.py"
)


@pytest.fixture(autouse=True)
def _reset_environment_loader() -> None:
    environment.load_application_environment.cache_clear()
    yield
    environment.load_application_environment.cache_clear()


def test_absent_override_loads_project_dotenv_without_overriding_process_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_dotenv = tmp_path / ".env"
    project_dotenv.write_text(
        "R35_PROJECT_ONLY=project\nR35_PROCESS_WINS=dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(environment, "PROJECT_DOTENV", project_dotenv)
    monkeypatch.delenv(environment.ENVIRONMENT_FILE_KEY, raising=False)
    monkeypatch.delenv("R35_PROJECT_ONLY", raising=False)
    monkeypatch.setenv("R35_PROCESS_WINS", "process")

    selected = environment.load_application_environment()

    assert selected == project_dotenv
    assert os.environ["R35_PROJECT_ONLY"] == "project"
    assert os.environ["R35_PROCESS_WINS"] == "process"


def test_explicit_file_excludes_project_dotenv_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_dotenv = tmp_path / "checkout.env"
    project_dotenv.write_text("R35_CHECKOUT_ONLY=forbidden\n", encoding="utf-8")
    explicit = tmp_path / "runtime.env"
    explicit.write_text("R35_EXPLICIT_ONLY=first\n", encoding="utf-8")
    monkeypatch.setattr(environment, "PROJECT_DOTENV", project_dotenv)
    monkeypatch.setenv(environment.ENVIRONMENT_FILE_KEY, os.fspath(explicit))
    monkeypatch.delenv("R35_CHECKOUT_ONLY", raising=False)
    monkeypatch.delenv("R35_EXPLICIT_ONLY", raising=False)
    monkeypatch.delenv("R35_LATE_VALUE", raising=False)

    first = environment.load_application_environment()
    explicit.write_text("R35_LATE_VALUE=second\n", encoding="utf-8")
    second = environment.load_application_environment()

    assert first == explicit
    assert second == explicit
    assert os.environ["R35_EXPLICIT_ONLY"] == "first"
    assert "R35_CHECKOUT_ONLY" not in os.environ
    assert "R35_LATE_VALUE" not in os.environ


@pytest.mark.parametrize(
    ("raw_value", "reason"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("relative.env", "not-absolute"),
    ],
)
def test_explicit_path_rejects_invalid_text_without_loading_project_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw_value: str,
    reason: str,
) -> None:
    project_dotenv = tmp_path / ".env"
    project_dotenv.write_text("R35_CHECKOUT_SECRET=must-not-load\n", encoding="utf-8")
    monkeypatch.setattr(environment, "PROJECT_DOTENV", project_dotenv)
    monkeypatch.setenv(environment.ENVIRONMENT_FILE_KEY, raw_value)
    monkeypatch.delenv("R35_CHECKOUT_SECRET", raising=False)

    with pytest.raises(environment.EnvironmentFileError) as caught:
        environment.load_application_environment()

    assert caught.value.key == environment.ENVIRONMENT_FILE_KEY
    assert caught.value.reason == reason
    assert "R35_CHECKOUT_SECRET" not in os.environ


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory", "fifo"])
def test_explicit_path_must_be_an_existing_regular_non_symlink_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    explicit = tmp_path / "runtime.env"
    expected_reason = "missing"
    if kind == "symlink":
        target = tmp_path / "target.env"
        target.write_text("R35_FORBIDDEN=symlink\n", encoding="utf-8")
        explicit.symlink_to(target)
        expected_reason = "symlink"
    elif kind == "directory":
        explicit.mkdir()
        expected_reason = "not-regular"
    elif kind == "fifo":
        os.mkfifo(explicit)
        expected_reason = "not-regular"
    monkeypatch.setenv(environment.ENVIRONMENT_FILE_KEY, os.fspath(explicit))

    with pytest.raises(environment.EnvironmentFileError) as caught:
        environment.load_application_environment()

    assert caught.value.reason == expected_reason
    assert caught.value.path == os.fspath(explicit)


def test_explicit_decode_error_does_not_expose_file_contents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "r35-env-secret-sentinel"
    explicit = tmp_path / "runtime.env"
    explicit.write_bytes(f"SECRET={secret}\n".encode() + b"\xff")
    monkeypatch.setenv(environment.ENVIRONMENT_FILE_KEY, os.fspath(explicit))

    with pytest.raises(environment.EnvironmentFileError) as caught:
        environment.load_application_environment()

    assert caught.value.reason == "decode-failed"
    assert caught.value.key == environment.ENVIRONMENT_FILE_KEY
    assert caught.value.path == os.fspath(explicit)
    assert secret not in str(caught.value)
    assert set(vars(caught.value)) == {"key", "reason", "path"}


def test_explicit_path_value_is_not_rendered_in_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_path = tmp_path / "r35-path-secret-sentinel" / "missing.env"
    raw_path = os.fspath(secret_path)
    monkeypatch.setenv(environment.ENVIRONMENT_FILE_KEY, raw_path)

    with pytest.raises(environment.EnvironmentFileError) as caught:
        environment.load_application_environment()

    assert caught.value.path == raw_path
    assert "r35-path-secret-sentinel" not in str(caught.value)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("R35_EARLY=first\n'broken\n", "parse-failed"),
        ("R35_EARLY=first\nR35_BAD=secret\x00value\n", "value-invalid"),
        ("# secret\x00comment\nR35_EARLY=first\n", "value-invalid"),
        ("R35_DUPLICATE=first\nR35_DUPLICATE=second\n", "duplicate-key"),
    ],
)
def test_explicit_parse_and_value_failures_leave_environment_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: str,
    reason: str,
) -> None:
    explicit = tmp_path / "runtime.env"
    explicit.write_text(payload, encoding="utf-8")
    monkeypatch.setenv(environment.ENVIRONMENT_FILE_KEY, os.fspath(explicit))
    for key in ("R35_EARLY", "R35_BAD", "R35_DUPLICATE"):
        monkeypatch.delenv(key, raising=False)
    before = dict(os.environ)

    with pytest.raises(environment.EnvironmentFileError) as caught:
        environment.load_application_environment()

    assert caught.value.reason == reason
    assert dict(os.environ) == before
    assert "secret" not in str(caught.value)


def test_explicit_application_failure_rolls_back_all_applied_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "runtime.env"
    explicit.write_text("R35_FIRST=first\nR35_SECOND=second\n", encoding="utf-8")

    class FailingEnvironment(dict[str, str]):
        def __setitem__(self, key: str, value: str) -> None:
            if key == "R35_SECOND":
                raise OSError("application-secret-sentinel")
            super().__setitem__(key, value)

    fake_environment = FailingEnvironment(os.environ)
    fake_environment[environment.ENVIRONMENT_FILE_KEY] = os.fspath(explicit)
    fake_environment.pop("R35_FIRST", None)
    fake_environment.pop("R35_SECOND", None)
    before = dict(fake_environment)
    monkeypatch.setattr(environment.os, "environ", fake_environment)

    with pytest.raises(environment.EnvironmentFileError) as caught:
        environment.load_application_environment()

    assert caught.value.reason == "application-failed"
    assert fake_environment == before
    assert "application-secret-sentinel" not in str(caught.value)


def test_settings_is_the_only_import_time_environment_loader() -> None:
    # single_process_bootstrap no longer imports or calls
    # load_application_environment at all (it is now a plain re-export of
    # single_process); settings.py is the sole remaining import-time caller
    # of the startup environment loader.
    bootstrap = ast.parse(BOOTSTRAP_SOURCE.read_text(encoding="utf-8"))
    settings = ast.parse(SETTINGS_SOURCE.read_text(encoding="utf-8"))

    environment_helper_modules = {"llm_gateway_core.config.environment", "environment"}

    bootstrap_imports = [
        statement
        for statement in bootstrap.body
        if isinstance(statement, ast.ImportFrom)
        and statement.module in environment_helper_modules
    ]
    assert bootstrap_imports == []

    settings_imports = [
        statement
        for statement in settings.body
        if isinstance(statement, ast.ImportFrom)
        and statement.module in environment_helper_modules
    ]
    assert len(settings_imports) == 1
    assert {alias.name for alias in settings_imports[0].names} == {
        "load_application_environment"
    }
    assert any(
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "load_application_environment"
        for statement in settings.body
    )


def test_invalid_explicit_file_fails_before_settings_paths_logging_and_db(
    tmp_path: Path,
) -> None:
    # settings.py is now the sole import-time caller of
    # load_application_environment (see
    # test_settings_is_the_only_import_time_environment_loader above):
    # single_process_bootstrap no longer loads the environment or validates
    # the worker count at import time, so llm_gateway_core.services.single_process
    # is *not* forbidden here anymore -- main.py imports it, via bootstrap,
    # unconditionally before settings.py ever runs. What must still hold is
    # that a broken explicit env file stops `import main` before
    # llm_gateway_core.config.settings/paths finish loading and before any
    # log/state directory is created.
    secret = "r35-invalid-env-secret"
    target = tmp_path / "target.env"
    target.write_text(f"SECRET={secret}\n", encoding="utf-8")
    explicit = tmp_path / "runtime.env"
    explicit.symlink_to(target)
    log_dir = tmp_path / "logs"
    state_dir = tmp_path / "state"
    code = (
        "import sys\n"
        "try:\n"
        "    import main\n"
        "except BaseException as exc:\n"
        "    print(type(exc).__name__)\n"
        "    print(getattr(exc, 'key', 'missing'))\n"
        "    print(getattr(exc, 'reason', 'missing'))\n"
        "    print(getattr(exc, 'path', 'missing'))\n"
        "    still_forbidden = ('llm_gateway_core.config.settings', "
        "'llm_gateway_core.config.paths')\n"
        "    print(','.join(name for name in still_forbidden if name in sys.modules))\n"
        "    print('llm_gateway_core.services.single_process' in sys.modules)\n"
        "    raise SystemExit(37)\n"
        "raise SystemExit(0)\n"
    )
    process_environment = os.environ.copy()
    process_environment.update(
        {
            environment.ENVIRONMENT_FILE_KEY: os.fspath(explicit),
            "GATEWAY_WORKERS": "2",
            "GATEWAY_DB_DIR": os.fspath(state_dir),
            "LLMGATEWAY_LOG_DIR": os.fspath(log_dir),
        }
    )
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_CMD_ARGS"):
        process_environment.pop(name, None)

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=process_environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 37
    assert completed.stdout.splitlines() == [
        "EnvironmentFileError",
        environment.ENVIRONMENT_FILE_KEY,
        "symlink",
        os.fspath(explicit),
        "",
        "True",
    ]
    assert completed.stderr == ""
    assert secret not in completed.stdout
    assert not log_dir.exists()
    assert not state_dir.exists()


@pytest.mark.parametrize(
    ("payload", "expected_diagnostic"),
    [
        (
            "LOG_FILE_LIMIT=r35-settings-secret\n",
            "key=LOG_FILE_LIMIT reason=positive_integer_required",
        ),
        (
            "PROVIDER_INJECTION_ENABLED=r35-settings-secret\n",
            "bool_parsing",
        ),
        (
            "GATEWAY_API_KEY=r35-settings-secret\n"
            "MAX_REQUEST_BODY_BYTES=8\n"
            "PDF_UPLOAD_MAX_BYTES=9\n"
            "UPLOAD_INFLIGHT_MAX_BYTES=1000000000\n",
            "MAX_REQUEST_BODY_BYTES",
        ),
    ],
)
def test_settings_failures_do_not_expose_explicit_environment_values(
    tmp_path: Path,
    payload: str,
    expected_diagnostic: str,
) -> None:
    explicit = tmp_path / "runtime.env"
    explicit.write_text(payload, encoding="utf-8")
    process_environment = os.environ.copy()
    for key in (
        "GATEWAY_API_KEY",
        "LOG_FILE_LIMIT",
        "MAX_REQUEST_BODY_BYTES",
        "PDF_UPLOAD_MAX_BYTES",
        "PROVIDER_INJECTION_ENABLED",
        "UPLOAD_INFLIGHT_MAX_BYTES",
    ):
        process_environment.pop(key, None)
    process_environment.update(
        {
            environment.ENVIRONMENT_FILE_KEY: os.fspath(explicit),
            "GATEWAY_WORKERS": "1",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", "import llm_gateway_core.config.settings"],
        cwd=PROJECT_ROOT,
        env=process_environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    combined_output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert expected_diagnostic in combined_output
    assert "r35-settings-secret" not in combined_output
    assert "input_value=" not in combined_output
    assert "invalid literal for int()" not in combined_output
