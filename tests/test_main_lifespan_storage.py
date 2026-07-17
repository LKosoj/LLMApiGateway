from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec, patch
from urllib.parse import unquote, urlsplit

import pytest
import aiosqlite
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from llm_gateway_core.services.accounting_service import AccountingService
from llm_gateway_core.services.config_updates import (
    ConfigUpdateState,
    ConfigUpdateStatusSnapshot,
)
from llm_gateway_core.services.image_storage import ImageStorageError
from llm_gateway_core.services.runtime_config import RuntimeSnapshot
from tests._async_compat import run_async
from tests.main_lifespan_boundary import find_main_lifespan_boundary_violations
from tests.main_lifespan_storage import (
    MainLifespanStorageIsolation,
    installed_main_lifespan_storage_isolation,
)
from tests.runtime_test_support import install_test_runtime_snapshot


def _fake_main_module(project_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        PROJECT_ROOT=project_root,
        OUTPUTS_IMAGES_DIR=project_root / "outputs" / "images",
    )


def test_default_source_launch_prepares_missing_outputs_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "fresh-checkout"
    project_root.mkdir()
    images_root = project_root / "outputs" / "images"
    monkeypatch.delenv("GATEWAY_OUTPUTS_DIR", raising=False)

    with (
        patch.object(main, "PROJECT_ROOT", project_root),
        patch.object(main, "OUTPUTS_IMAGES_DIR", images_root),
    ):
        main._prepare_source_outputs_images_dir()
        main.GeneratedImageStorage(images_root).probe()

    assert images_root.is_dir()


def test_default_source_prepare_rejects_symlink_without_touching_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "checkout"
    outside = tmp_path / "outside"
    project_root.mkdir()
    outside.mkdir()
    (project_root / "outputs").symlink_to(outside, target_is_directory=True)
    images_root = project_root / "outputs" / "images"
    monkeypatch.delenv("GATEWAY_OUTPUTS_DIR", raising=False)

    with (
        patch.object(main, "PROJECT_ROOT", project_root),
        patch.object(main, "OUTPUTS_IMAGES_DIR", images_root),
        pytest.raises(ImageStorageError, match="default-root-prepare-failed"),
    ):
        main._prepare_source_outputs_images_dir()

    assert not (outside / "images").exists()


def test_explicit_outputs_root_is_not_created_by_source_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit_root = tmp_path / "managed-volume"
    images_root = explicit_root / "images"
    monkeypatch.setenv("GATEWAY_OUTPUTS_DIR", str(explicit_root))

    with patch.object(main, "OUTPUTS_IMAGES_DIR", images_root):
        main._prepare_source_outputs_images_dir()
        with pytest.raises(ImageStorageError) as raised:
            main.GeneratedImageStorage(images_root).probe()

    assert raised.value.reason == "outputs-root-missing"
    assert not explicit_root.exists()


@contextmanager
def _guard_checkout_sqlite_connections(
    checkout_db: Path,
) -> Iterator[list[Path]]:
    checkout_db = checkout_db.resolve(strict=False)
    observed: list[Path] = []
    original_sqlite_connect = sqlite3.connect
    original_aiosqlite_connect = aiosqlite.connect

    def checked_path(database: object, *, uri: bool = False) -> None:
        raw_path = os.fsdecode(os.fspath(database))
        if raw_path == ":memory:":
            return
        if uri and raw_path.startswith("file:"):
            raw_path = unquote(urlsplit(raw_path).path)
        resolved = Path(raw_path).resolve(strict=False)
        if resolved.is_relative_to(checkout_db):
            raise AssertionError(f"test process attempted checkout SQLite path: {resolved}")
        observed.append(resolved)

    def guarded_sqlite_connect(database, *args, **kwargs):
        checked_path(database, uri=bool(kwargs.get("uri")))
        return original_sqlite_connect(database, *args, **kwargs)

    def guarded_aiosqlite_connect(database, *args, **kwargs):
        checked_path(database)
        return original_aiosqlite_connect(database, *args, **kwargs)

    with (
        patch.object(sqlite3, "connect", guarded_sqlite_connect),
        patch.object(aiosqlite, "connect", guarded_aiosqlite_connect),
    ):
        yield observed


def _assert_services_use_temp_storage(services: object) -> Path:
    db_dir = Path(os.environ["GATEWAY_DB_DIR"])
    assert main.PROJECT_ROOT == db_dir.parent
    assert main.OUTPUTS_IMAGES_DIR == db_dir.parent / "outputs" / "images"
    assert main.OUTPUTS_IMAGES_DIR.is_dir()
    assert services.image_storage.images_root == main.OUTPUTS_IMAGES_DIR.resolve()
    for database_name in (
        "tokens_usage_db",
        "fallback_events_db",
        "rejections_db",
        "api_keys_db",
        "model_rotation_db",
    ):
        database = getattr(services, database_name)
        assert Path(database.db_path).parent == db_dir
    assert Path(services.write_batcher.db_path).parent == db_dir
    return db_dir.parent


@contextmanager
def _isolated_production_dependencies() -> Iterator[None]:
    config_loader = SimpleNamespace(
        providers_config={},
        fallback_rules={},
        operation_rules={},
        fusion_rules={},
        model_rules={},
        router_rules={},
    )
    shared_client = Mock(name="shared-client")
    shared_client.aclose = AsyncMock()
    openrouter_service = Mock(name="openrouter-service")
    openrouter_service.start_runtime = AsyncMock()
    openrouter_service.stop = AsyncMock()
    fallback_eval_service = Mock(name="fallback-eval-service")
    fallback_eval_service.stop = AsyncMock()

    class RecordingCoordinator:
        def __init__(
            self,
            *,
            runtime_manager: object,
            shared_http_client: object,
            initial_snapshot: RuntimeSnapshot,
        ) -> None:
            assert shared_http_client is shared_client
            assert initial_snapshot.config_loader is config_loader
            self.runtime_manager = runtime_manager
            self.closed = False

        @property
        def status_snapshot(self) -> ConfigUpdateStatusSnapshot:
            return ConfigUpdateStatusSnapshot(
                state=ConfigUpdateState.RUNNING,
                accepting=not self.closed,
                active_updates=0,
                pending_cleanup=0,
                last_failure=None,
            )

        async def close(self) -> None:
            self.closed = True

    async def build_initial_snapshot(manager, candidate_loader, http_client):
        snapshot = RuntimeSnapshot(
            generation=1,
            config_loader=candidate_loader,
            operation_dispatcher=Mock(name="operation-dispatcher"),
            fusion_service=Mock(name="fusion-service"),
            router_model_service=Mock(name="router-model-service"),
            provider_models_service=Mock(name="provider-models-service"),
            proxy_http_clients={},
            cost_rate_registry={},
        )
        install_test_runtime_snapshot(manager, snapshot)
        return snapshot

    with ExitStack() as stack:
        stack.enter_context(patch.object(main.settings, "gateway_api_key", "storage-test-key"))
        stack.enter_context(patch.object(main.settings, "ip_block_enabled", False))
        stack.enter_context(patch("main.preload_templates"))
        stack.enter_context(patch("main._load_initial_config", return_value=config_loader))
        stack.enter_context(patch("main.create_shared_http_client", return_value=shared_client))
        stack.enter_context(
            patch("main._build_initial_snapshot", side_effect=build_initial_snapshot)
        )
        stack.enter_context(
            patch("main.OpenRouterFreeModelsService", return_value=openrouter_service)
        )
        stack.enter_context(
            patch("main.FallbackModelEvalService", return_value=fallback_eval_service)
        )
        stack.enter_context(
            patch("main.ConfigUpdateCoordinator", RecordingCoordinator)
        )
        stack.enter_context(patch("main.run_startup_model_verification", AsyncMock()))
        yield


def test_owner_uses_unique_roots_and_restores_exact_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original_root = tmp_path / "original-project"
    original_images = tmp_path / "original-images"
    fake_main = _fake_main_module(original_root)
    fake_main.OUTPUTS_IMAGES_DIR = original_images
    monkeypatch.setenv("GATEWAY_DB_DIR", "/preexisting/db")
    observations: list[tuple[Path, Path, Path, str]] = []

    @asynccontextmanager
    async def production_lifespan(_app: object) -> AsyncIterator[Path]:
        root = Path(fake_main.PROJECT_ROOT)
        observations.append(
            (
                root,
                Path(fake_main.OUTPUTS_IMAGES_DIR),
                Path(os.environ["GATEWAY_DB_DIR"]),
                "startup",
            )
        )
        try:
            yield root
        finally:
            observations.append(
                (
                    Path(fake_main.PROJECT_ROOT),
                    Path(fake_main.OUTPUTS_IMAGES_DIR),
                    Path(os.environ["GATEWAY_DB_DIR"]),
                    "shutdown",
                )
            )

    owner = MainLifespanStorageIsolation(fake_main, base_dir=tmp_path)
    wrapped = owner.wrap(production_lifespan)

    async def scenario() -> tuple[Path, Path]:
        async with wrapped(object()) as first_root:
            assert first_root.is_dir()
        async with wrapped(object()) as second_root:
            assert second_root.is_dir()
        return first_root, second_root

    first_root, second_root = run_async(scenario())
    assert first_root != second_root
    assert not first_root.exists()
    assert not second_root.exists()
    assert fake_main.PROJECT_ROOT == original_root
    assert fake_main.OUTPUTS_IMAGES_DIR == original_images
    assert os.environ["GATEWAY_DB_DIR"] == "/preexisting/db"
    assert [stage for *_paths, stage in observations] == [
        "startup",
        "shutdown",
        "startup",
        "shutdown",
    ]
    for root, images_dir, db_dir, _stage in observations:
        assert images_dir == root / "outputs" / "images"
        assert db_dir == root / "db"


@pytest.mark.parametrize("failure_stage", ("startup", "body", "shutdown"))
@pytest.mark.parametrize(
    "failure_type",
    (RuntimeError, asyncio.CancelledError, SystemExit, KeyboardInterrupt),
)
def test_owner_restores_absent_env_and_globals_after_every_failure_stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_stage: str,
    failure_type: type[BaseException],
) -> None:
    original_root = tmp_path / "original-project"
    original_images = tmp_path / "original-images"
    fake_main = _fake_main_module(original_root)
    fake_main.OUTPUTS_IMAGES_DIR = original_images
    monkeypatch.delenv("GATEWAY_DB_DIR", raising=False)
    shutdown_observation: list[Path] = []
    failure = failure_type(f"{failure_stage} failed")

    @asynccontextmanager
    async def production_lifespan(_app: object) -> AsyncIterator[None]:
        if failure_stage == "startup":
            raise failure
        try:
            yield
        finally:
            shutdown_observation.append(Path(os.environ["GATEWAY_DB_DIR"]))
            if failure_stage == "shutdown":
                raise failure

    owner = MainLifespanStorageIsolation(fake_main, base_dir=tmp_path)
    wrapped = owner.wrap(production_lifespan)

    async def scenario() -> None:
        async with wrapped(object()):
            if failure_stage == "body":
                raise failure

    with pytest.raises(failure_type) as raised:
        run_async(scenario())
    assert raised.value is failure

    assert "GATEWAY_DB_DIR" not in os.environ
    assert fake_main.PROJECT_ROOT == original_root
    assert fake_main.OUTPUTS_IMAGES_DIR == original_images
    if failure_stage != "startup":
        assert shutdown_observation[0].parent.name.startswith(
            "llmgateway_main_lifespan_"
        )


def test_owner_rejects_overlap_before_changing_active_storage(tmp_path: Path) -> None:
    fake_main = _fake_main_module(tmp_path / "original-project")
    started = asyncio.Event()
    release = asyncio.Event()

    @asynccontextmanager
    async def production_lifespan(_app: object) -> AsyncIterator[None]:
        yield

    owner = MainLifespanStorageIsolation(fake_main, base_dir=tmp_path)
    wrapped = owner.wrap(production_lifespan)

    async def hold_first_lifespan() -> None:
        async with wrapped(object()):
            started.set()
            await release.wait()

    async def scenario() -> None:
        first = asyncio.create_task(hold_first_lifespan())
        await started.wait()
        active_root = fake_main.PROJECT_ROOT
        active_db_dir = os.environ["GATEWAY_DB_DIR"]
        with pytest.raises(RuntimeError, match="Overlapping production test lifespans"):
            async with wrapped(object()):
                pytest.fail("overlap must fail before entering the original lifespan")
        assert fake_main.PROJECT_ROOT == active_root
        assert os.environ["GATEWAY_DB_DIR"] == active_db_dir
        release.set()
        await first

    run_async(scenario())


def test_process_lock_rejects_overlap_across_owner_instances(tmp_path: Path) -> None:
    first_main = _fake_main_module(tmp_path / "first-original")
    second_main = _fake_main_module(tmp_path / "second-original")
    entered = threading.Event()
    release = threading.Event()
    outcomes: list[str] = []

    @asynccontextmanager
    async def production_lifespan(_app: object) -> AsyncIterator[None]:
        yield

    first = MainLifespanStorageIsolation(first_main, base_dir=tmp_path).wrap(
        production_lifespan
    )
    second = MainLifespanStorageIsolation(second_main, base_dir=tmp_path).wrap(
        production_lifespan
    )

    def first_worker() -> None:
        async def scenario() -> None:
            async with first(object()):
                entered.set()
                release.wait(timeout=5)

        try:
            run_async(scenario())
        except BaseException as exc:
            outcomes.append(f"first:{type(exc).__name__}")

    def second_worker() -> None:
        async def scenario() -> None:
            try:
                async with second(object()):
                    outcomes.append("second:entered")
            except RuntimeError as exc:
                if "Overlapping production test lifespans" in str(exc):
                    outcomes.append("second:rejected")
                else:
                    outcomes.append(f"second:{type(exc).__name__}")

        run_async(scenario())

    first_thread = threading.Thread(target=first_worker)
    first_thread.start()
    assert entered.wait(timeout=5)
    second_root = second_main.PROJECT_ROOT
    second_images = second_main.OUTPUTS_IMAGES_DIR
    active_db_dir = os.environ["GATEWAY_DB_DIR"]
    second_thread = threading.Thread(target=second_worker)
    second_thread.start()
    second_thread.join(timeout=5)
    try:
        assert not second_thread.is_alive()
        assert outcomes == ["second:rejected"]
        assert second_main.PROJECT_ROOT == second_root
        assert second_main.OUTPUTS_IMAGES_DIR == second_images
        assert os.environ["GATEWAY_DB_DIR"] == active_db_dir
    finally:
        release.set()
        first_thread.join(timeout=5)
    assert not first_thread.is_alive()
    assert outcomes == ["second:rejected"]

    async def sequential() -> None:
        async with second(object()):
            assert second_main.PROJECT_ROOT != second_root

    run_async(sequential())


def test_installation_wraps_both_entries_and_restores_exact_callables(
    tmp_path: Path,
) -> None:
    @asynccontextmanager
    async def public_lifespan(_app: object) -> AsyncIterator[None]:
        yield

    @asynccontextmanager
    async def router_lifespan(_app: object) -> AsyncIterator[None]:
        yield

    router = SimpleNamespace(lifespan_context=router_lifespan)
    fake_main = _fake_main_module(tmp_path / "original-project")
    fake_main.app = SimpleNamespace(router=router)
    fake_main.lifespan = public_lifespan

    with installed_main_lifespan_storage_isolation(
        fake_main,
        base_dir=tmp_path,
    ) as owner:
        assert owner.owns(fake_main.lifespan)
        assert owner.owns(router.lifespan_context)

    assert fake_main.lifespan is public_lifespan
    assert router.lifespan_context is router_lifespan


def test_fresh_app_with_its_own_lifespan_is_not_mutated(tmp_path: Path) -> None:
    fake_main = _fake_main_module(tmp_path / "original-project")
    observed: list[tuple[Path, str | None]] = []

    @asynccontextmanager
    async def fresh_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        observed.append((fake_main.PROJECT_ROOT, os.environ.get("GATEWAY_DB_DIR")))
        yield

    app = FastAPI(lifespan=fresh_lifespan)
    with TestClient(app):
        pass

    assert observed == [(tmp_path / "original-project", os.environ.get("GATEWAY_DB_DIR"))]


def test_direct_exported_lifespan_with_fresh_app_uses_temp_storage(
    _main_lifespan_storage_isolation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _main_lifespan_storage_isolation is not None
    original_root = main.PROJECT_ROOT
    original_images = main.OUTPUTS_IMAGES_DIR
    monkeypatch.setenv("GATEWAY_DB_DIR", "/preexisting/direct-db")
    isolated_root: Path | None = None

    async def scenario() -> Path:
        fresh_app = FastAPI()
        async with main.lifespan(fresh_app):
            return _assert_services_use_temp_storage(fresh_app.state.services)

    with _isolated_production_dependencies():
        isolated_root = run_async(scenario())

    assert isolated_root is not None
    assert not isolated_root.exists()
    assert os.environ["GATEWAY_DB_DIR"] == "/preexisting/direct-db"
    assert main.PROJECT_ROOT == original_root
    assert main.OUTPUTS_IMAGES_DIR == original_images


def test_mocked_tokens_db_write_batcher_fallback_uses_isolated_root(
    _main_lifespan_storage_isolation,
) -> None:
    assert _main_lifespan_storage_isolation is not None
    tokens_db = Mock(name="tokens-usage-db-without-path")
    tokens_db.db_path = None
    captured_paths: list[Path] = []
    lifecycle_events: list[str] = []
    accounting_service = create_autospec(
        AccountingService,
        instance=True,
        spec_set=True,
    )
    accounting_service.start.side_effect = lambda: lifecycle_events.append(
        "accounting.start"
    )
    accounting_service.stop.side_effect = lambda: lifecycle_events.append(
        "accounting.stop"
    )
    accounting_service.reset_due_budgets.return_value = ()

    def write_batcher_factory(db_path: Path, *, queue_maxsize: int) -> Mock:
        assert queue_maxsize == main.settings.write_batcher_queue_maxsize
        resolved = Path(db_path).resolve(strict=False)
        captured_paths.append(resolved)
        batcher = Mock(name="write-batcher")
        batcher.db_path = resolved
        batcher.start = AsyncMock(
            side_effect=lambda: lifecycle_events.append("write-batcher.start")
        )
        batcher.stop = AsyncMock(
            side_effect=lambda: lifecycle_events.append("write-batcher.stop")
        )
        return batcher

    async def scenario() -> Path:
        app = FastAPI()
        async with main.lifespan(app):
            isolated_root = main.PROJECT_ROOT
            assert captured_paths == [isolated_root / "db" / "tokens_usage.db"]
            assert app.state.services.write_batcher.db_path == captured_paths[0]
            return isolated_root

    with (
        _isolated_production_dependencies(),
        patch("main.TokensUsageDB", return_value=tokens_db),
        patch("main.WriteBatcher", side_effect=write_batcher_factory),
        patch("main.AccountingService", return_value=accounting_service),
    ):
        isolated_root = run_async(scenario())

    assert captured_paths == [isolated_root / "db" / "tokens_usage.db"]
    assert lifecycle_events.index("write-batcher.start") < lifecycle_events.index(
        "accounting.start"
    )
    assert lifecycle_events.index("accounting.stop") < lifecycle_events.index(
        "write-batcher.stop"
    )
    assert not isolated_root.exists()


def test_actual_main_app_lifespan_uses_only_guarded_temp_storage(
    _main_lifespan_storage_isolation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _main_lifespan_storage_isolation is not None
    repo_root = Path(__file__).resolve().parents[1]
    checkout_db = repo_root / "db"
    original_root = main.PROJECT_ROOT
    original_images = main.OUTPUTS_IMAGES_DIR
    research_id = "runtime-isolation-e2e"
    filename = "image_deadbeef_424242.png"
    checkout_image = repo_root / "outputs" / "images" / research_id / filename
    assert not checkout_image.exists()
    outputs_mount = next(
        route
        for route in main.app.routes
        if getattr(route, "name", None) == "outputs-images"
    )
    original_outputs_mount_app = outputs_mount.app
    monkeypatch.setenv("GATEWAY_DB_DIR", "/preexisting/main-app-db")
    isolated_root: Path | None = None
    image_cleanup_roots: list[Path] = []
    original_retention_run = main.ImageRetentionService.run

    def tracking_retention_run(service):
        image_cleanup_roots.append(main.OUTPUTS_IMAGES_DIR.resolve(strict=False))
        return original_retention_run(service)

    with (
        _guard_checkout_sqlite_connections(checkout_db) as observed_connections,
        _isolated_production_dependencies(),
        patch.object(main.ImageRetentionService, "run", tracking_retention_run),
    ):
        with TestClient(main.app) as client:
            services = client.app.state.services
            isolated_root = _assert_services_use_temp_storage(services)
            assert outputs_mount.app is not original_outputs_mount_app
            assert Path(outputs_mount.app.directory) == main.OUTPUTS_IMAGES_DIR
            expected_image = b"\x89PNG\r\n\x1a\nserved-from-isolated-storage"
            published = services.image_storage.publish_png(
                expected_image,
                filename,
                research_id,
            )
            image_response = client.get(
                published.url,
                headers={"Authorization": "Bearer storage-test-key"},
            )
            response = client.get("/health")
            assert response.status_code == 200
            assert image_response.status_code == 200
            assert image_response.content == expected_image
            assert published.path.is_relative_to(isolated_root)

    assert isolated_root is not None
    isolated_db = isolated_root / "db"
    assert observed_connections
    assert all(path.is_relative_to(isolated_db) for path in observed_connections)
    assert image_cleanup_roots == [isolated_root / "outputs" / "images"]
    assert not checkout_image.exists()
    assert outputs_mount.app is original_outputs_mount_app
    assert not isolated_root.exists()
    assert os.environ["GATEWAY_DB_DIR"] == "/preexisting/main-app-db"
    assert main.PROJECT_ROOT == original_root
    assert main.OUTPUTS_IMAGES_DIR == original_images


def test_recursive_source_boundary_has_no_bypass_or_rebind() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    assert find_main_lifespan_boundary_violations(repo_root) == ()
