from __future__ import annotations

import ast
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI

import main
from llm_gateway_core.config.config_store import (
    AtomicConfigFileTransaction,
    AtomicConfigTransactionIntegrityError,
    ConfigFile,
)
from llm_gateway_core.config.loader import ConfigError, ConfigLoader
from llm_gateway_core.services.single_process import (
    SINGLE_APPLICATION_WORKER_COUNT,
)
from tests._async_compat import run_async


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = PROJECT_ROOT / "main.py"
BOOTSTRAP_SOURCE = (
    PROJECT_ROOT
    / "llm_gateway_core"
    / "services"
    / "single_process_bootstrap.py"
)


def test_main_imports_single_process_bootstrap_before_every_other_import() -> None:
    module = ast.parse(MAIN_SOURCE.read_text(encoding="utf-8"))

    first_statement = module.body[0]
    assert isinstance(first_statement, ast.ImportFrom)
    assert first_statement.module == (
        "llm_gateway_core.services.single_process_bootstrap"
    )
    assert {alias.name for alias in first_statement.names} == {
        "SINGLE_APPLICATION_WORKER_COUNT",
        "SingleProcessLease",
        "validate_single_worker_environment",
    }


def test_bootstrap_module_has_no_import_time_side_effects() -> None:
    # single_process_bootstrap no longer loads the environment or validates
    # the worker count at import time -- both used to be top-level calls
    # executed as soon as `import main` reached its very first import
    # statement. Now the module is a pure re-export of single_process, and
    # validate_single_worker_environment is invoked explicitly instead, by
    # main.lifespan (test_lifespan_guard_failure_precedes_startup_and_dependencies
    # below) and main.run_server (test_run_server_validates_before_log_and_uvicorn
    # below).
    module = ast.parse(BOOTSTRAP_SOURCE.read_text(encoding="utf-8"))

    top_level_calls = {
        statement.value.func.id
        for statement in module.body
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
    }
    assert top_level_calls == set()

    import_from_statements = [
        statement
        for statement in module.body
        if isinstance(statement, ast.ImportFrom) and statement.module != "__future__"
    ]
    assert len(import_from_statements) == 1
    only_import = import_from_statements[0]
    assert only_import.level == 1
    assert only_import.module == "single_process"
    assert {alias.name for alias in only_import.names} == {
        "SINGLE_APPLICATION_WORKER_COUNT",
        "SingleProcessInvariantError",
        "SingleProcessLease",
        "validate_single_worker_environment",
    }


def test_invalid_worker_environment_stops_lifespan_before_state_side_effects(
    tmp_path: Path,
) -> None:
    # Because validation is no longer an import-time side effect (see
    # test_bootstrap_module_has_no_import_time_side_effects), `import main`
    # must now succeed even with an invalid GATEWAY_WORKERS value -- the
    # invariant is enforced later, as the very first statement of
    # main.lifespan. Entering the lifespan must still raise before the
    # "Application startup" log line and before the state/database directory
    # is ever created.
    secret = "invalid-worker-secret-sentinel"
    log_dir = tmp_path / "logs"
    state_dir = tmp_path / "state"
    code = (
        "import asyncio\n"
        "import main\n"
        "async def _enter_lifespan():\n"
        "    async with main.lifespan(main.app):\n"
        "        pass\n"
        "try:\n"
        "    asyncio.run(_enter_lifespan())\n"
        "except BaseException as exc:\n"
        "    print(type(exc).__name__)\n"
        "    print(getattr(exc, 'source', 'missing'))\n"
        "    print(getattr(exc, 'reason_code', 'missing'))\n"
        "    raise SystemExit(37)\n"
        "raise SystemExit(0)\n"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "GATEWAY_WORKERS": f"2-{secret}",
            "GATEWAY_DB_DIR": os.fspath(state_dir),
            "LLMGATEWAY_LOG_DIR": os.fspath(log_dir),
            "SECRET_SENTINEL": secret,
        }
    )
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_CMD_ARGS"):
        environment.pop(name, None)

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 37
    assert completed.stdout.splitlines() == [
        "SingleProcessInvariantError",
        "GATEWAY_WORKERS",
        "invalid-worker-count",
    ]
    assert secret not in completed.stdout
    assert "Application startup" not in completed.stderr
    assert not state_dir.exists()


def test_run_server_validates_before_log_and_uvicorn() -> None:
    primary = RuntimeError("worker validation failure")

    with (
        patch("main.validate_single_worker_environment", side_effect=primary) as guard,
        patch.object(main.logger, "info") as startup_log,
        patch("main.uvicorn.run") as uvicorn_run,
    ):
        with pytest.raises(RuntimeError) as raised:
            main.run_server()

    assert raised.value is primary
    guard.assert_called_once_with(os.environ)
    startup_log.assert_not_called()
    uvicorn_run.assert_not_called()


def test_run_server_always_passes_one_worker() -> None:
    with (
        patch("main.validate_single_worker_environment") as guard,
        patch.object(main.settings, "gateway_host", "127.0.0.9"),
        patch.object(main.settings, "gateway_port", 9109),
        patch.object(main.settings, "debug_mode", True),
        patch.object(main.settings, "log_level", "WARNING"),
        patch.object(main.logger, "info") as startup_log,
        patch("main.uvicorn.run") as uvicorn_run,
    ):
        main.run_server()

    guard.assert_called_once_with(os.environ)
    startup_log.assert_called_once()
    uvicorn_run.assert_called_once_with(
        "main:app",
        host="127.0.0.9",
        port=9109,
        reload=True,
        log_level="warning",
        workers=SINGLE_APPLICATION_WORKER_COUNT,
    )
    assert SINGLE_APPLICATION_WORKER_COUNT == 1


def test_lifespan_guard_failure_precedes_startup_and_dependencies() -> None:
    app = FastAPI()
    primary = RuntimeError("worker validation failure")

    with (
        patch("main.validate_single_worker_environment", side_effect=primary),
        patch.object(main.logger, "info") as startup_log,
        patch("main.ensure_gateway_api_key_configured") as api_key_validation,
        patch("main.resolve_db_dir") as resolve_state_dir,
        patch("main.SingleProcessLease.acquire") as acquire_lease,
        patch("main.preload_templates") as preload,
        patch("main._load_initial_config") as load_config,
        patch("main.create_shared_http_client") as create_http_client,
        patch("main.TokensUsageDB") as create_database,
    ):
        context = main.lifespan(app)
        with pytest.raises(RuntimeError) as raised:
            run_async(context.__aenter__())

    assert raised.value is primary
    startup_log.assert_not_called()
    api_key_validation.assert_not_called()
    resolve_state_dir.assert_not_called()
    acquire_lease.assert_not_called()
    preload.assert_not_called()
    load_config.assert_not_called()
    create_http_client.assert_not_called()
    create_database.assert_not_called()


def test_api_key_failure_precedes_state_directory_creation() -> None:
    app = FastAPI()
    primary = RuntimeError("API key validation failure")

    with (
        patch("main.validate_single_worker_environment"),
        patch("main.ensure_gateway_api_key_configured", side_effect=primary),
        patch("main.resolve_db_dir") as resolve_state_dir,
        patch("main.SingleProcessLease.acquire") as acquire_lease,
    ):
        context = main.lifespan(app)
        with pytest.raises(RuntimeError) as raised:
            run_async(context.__aenter__())

    assert raised.value is primary
    resolve_state_dir.assert_not_called()
    acquire_lease.assert_not_called()


def test_lease_failure_precedes_output_config_http_db_and_services(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    raw_state_dir = tmp_path / "nested" / "state"
    primary = RuntimeError("lease contention")

    with (
        patch("main.validate_single_worker_environment"),
        patch("main.ensure_gateway_api_key_configured"),
        patch("main.resolve_db_dir", return_value=raw_state_dir),
        patch("main.SingleProcessLease.acquire", side_effect=primary) as acquire_lease,
        patch("main.session_secret.get_session_secret") as issue_session_secret,
        patch("main.preload_templates") as preload,
        patch("main._load_initial_config") as load_config,
        patch("main.create_shared_http_client") as create_http_client,
        patch("main.TokensUsageDB") as create_database,
        patch("main.OpenRouterFreeModelsService") as create_service,
    ):
        context = main.lifespan(app)
        with pytest.raises(RuntimeError) as raised:
            run_async(context.__aenter__())

    assert raised.value is primary
    canonical_state_dir = raw_state_dir.resolve(strict=True)
    assert canonical_state_dir.is_absolute()
    acquire_lease.assert_called_once_with(canonical_state_dir)
    # The cookie-signing secret is issued only under the lease: two processes
    # starting at once on a fresh state dir must not mint rival secrets.
    issue_session_secret.assert_not_called()
    preload.assert_not_called()
    load_config.assert_not_called()
    create_http_client.assert_not_called()
    create_database.assert_not_called()
    create_service.assert_not_called()


def test_lifespan_acquires_lease_before_recovery_and_config_load(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    events: list[str] = []
    failure = ConfigError("stop after ordered config load")
    lease = Mock(name="single-process-lease")
    lease.close = AsyncMock(side_effect=lambda: events.append("lease-close"))
    loader = Mock(spec=ConfigLoader)
    loader.configured_paths = {
        config_file: tmp_path / f"{config_file.value}.json"
        for config_file in ConfigFile
    }

    def acquire_lease(_state_dir: Path) -> Mock:
        events.append("lease")
        return lease

    def recover(paths) -> int:
        assert paths is loader.configured_paths
        events.append("recover")
        return 0

    def load_complete() -> ConfigLoader:
        events.append("load")
        raise failure

    loader.load_complete.side_effect = load_complete
    create_http_client = Mock()

    with (
        patch("main.validate_single_worker_environment"),
        patch("main.ensure_gateway_api_key_configured"),
        patch("main.resolve_db_dir", return_value=tmp_path / "state"),
        patch("main.SingleProcessLease.acquire", side_effect=acquire_lease),
        patch("main.OUTPUTS_IMAGES_DIR", tmp_path / "outputs" / "images"),
        patch("main.preload_templates"),
        patch("main.ConfigLoader", return_value=loader),
        patch.object(
            AtomicConfigFileTransaction,
            "recover_pending",
            side_effect=recover,
        ),
        patch("main.create_shared_http_client", create_http_client),
    ):
        context = main.lifespan(app)
        with pytest.raises(ConfigError) as raised:
            run_async(context.__aenter__())

    assert raised.value is failure
    assert events == ["lease", "recover", "load", "lease-close"]
    create_http_client.assert_not_called()


def test_recovery_error_releases_lease_without_load_cleanup_or_resources(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    failure = AtomicConfigTransactionIntegrityError(
        ConfigFile.MODEL_RULES,
        "startup journal state is ambiguous",
    )
    lease = Mock(name="single-process-lease")
    lease.close = AsyncMock()
    loader = Mock(spec=ConfigLoader)
    loader.configured_paths = {
        config_file: tmp_path / f"{config_file.value}.json"
        for config_file in ConfigFile
    }
    create_http_client = Mock()
    build_snapshot = Mock()

    with (
        patch("main.validate_single_worker_environment"),
        patch("main.ensure_gateway_api_key_configured"),
        patch("main.resolve_db_dir", return_value=tmp_path / "state"),
        patch("main.SingleProcessLease.acquire", return_value=lease),
        patch("main.OUTPUTS_IMAGES_DIR", tmp_path / "outputs" / "images"),
        patch("main.preload_templates"),
        patch("main.ConfigLoader", return_value=loader),
        patch.object(
            AtomicConfigFileTransaction,
            "recover_pending",
            side_effect=failure,
        ),
        patch.object(
            AtomicConfigFileTransaction,
            "cleanup_orphans",
        ) as cleanup_orphans,
        patch("main.create_shared_http_client", create_http_client),
        patch("main._build_initial_snapshot", build_snapshot),
    ):
        context = main.lifespan(app)
        with pytest.raises(AtomicConfigTransactionIntegrityError) as raised:
            run_async(context.__aenter__())

    assert raised.value is failure
    assert "secret" not in str(raised.value)
    loader.load_complete.assert_not_called()
    cleanup_orphans.assert_not_called()
    create_http_client.assert_not_called()
    build_snapshot.assert_not_called()
    lease.close.assert_awaited_once_with()


@pytest.mark.parametrize("failure_stage", ("mkdir", "resolve"))
def test_state_directory_preparation_failure_precedes_lease_and_dependencies(
    failure_stage: str,
) -> None:
    app = FastAPI()
    primary = RuntimeError(f"state directory {failure_stage} failure")
    raw_state_dir = Mock(name="raw-state-directory")
    if failure_stage == "mkdir":
        raw_state_dir.mkdir.side_effect = primary
    else:
        raw_state_dir.resolve.side_effect = primary

    with (
        patch("main.validate_single_worker_environment"),
        patch("main.ensure_gateway_api_key_configured"),
        patch("main.resolve_db_dir", return_value=raw_state_dir),
        patch("main.SingleProcessLease.acquire") as acquire_lease,
        patch("main.preload_templates") as preload,
        patch("main._load_initial_config") as load_config,
        patch("main.create_shared_http_client") as create_http_client,
        patch("main.TokensUsageDB") as create_database,
    ):
        context = main.lifespan(app)
        with pytest.raises(RuntimeError) as raised:
            run_async(context.__aenter__())

    assert raised.value is primary
    raw_state_dir.mkdir.assert_called_once_with(parents=True, exist_ok=True)
    if failure_stage == "mkdir":
        raw_state_dir.resolve.assert_not_called()
    else:
        raw_state_dir.resolve.assert_called_once_with(strict=True)
    acquire_lease.assert_not_called()
    preload.assert_not_called()
    load_config.assert_not_called()
    create_http_client.assert_not_called()
    create_database.assert_not_called()


@pytest.mark.parametrize(
    "primary",
    (
        RuntimeError("config startup failure"),
        asyncio.CancelledError("config startup cancellation"),
    ),
)
def test_post_acquire_startup_failure_preserves_identity_and_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary: BaseException,
) -> None:
    app = FastAPI()
    monkeypatch.chdir(tmp_path)
    lease = Mock(name="single-process-lease")
    lease.close = AsyncMock()

    with (
        patch("main.validate_single_worker_environment"),
        patch("main.ensure_gateway_api_key_configured"),
        patch("main.resolve_db_dir", return_value=Path("state")),
        patch("main.SingleProcessLease.acquire", return_value=lease) as acquire_lease,
        patch("main.preload_templates"),
        patch("main._load_initial_config", side_effect=primary),
        patch("main.create_shared_http_client") as create_http_client,
    ):
        context = main.lifespan(app)
        with pytest.raises(type(primary)) as raised:
            run_async(context.__aenter__())

    assert raised.value is primary
    acquire_lease.assert_called_once_with((tmp_path / "state").resolve(strict=True))
    lease.close.assert_awaited_once_with()
    create_http_client.assert_not_called()
