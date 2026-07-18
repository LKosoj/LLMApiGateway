from llm_gateway_core.services.single_process_bootstrap import (
    SINGLE_APPLICATION_WORKER_COUNT,
    SingleProcessLease,
    validate_single_worker_environment,
)

import asyncio
import logging
import os
import uvicorn
import httpx
from asyncio import sleep as scheduler_sleep
from collections.abc import Awaitable, Callable
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePath
from threading import Lock
from typing import TypeVar

# Import components from the new core structure
from llm_gateway_core.config.settings import settings
from llm_gateway_core.config.paths import (
    OUTPUTS_IMAGES_DIR,
    PROJECT_ROOT,
    STATIC_DIR,
    resolve_db_dir,
)
from llm_gateway_core.config.config_store import AtomicConfigFileTransaction
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.config.placeholder_secrets import is_placeholder_secret
from llm_gateway_core.version import __version__
from llm_gateway_core.services.image_retention import ImageRetentionService
from llm_gateway_core.services.image_storage import (
    GeneratedImageStorage,
    prepare_default_images_root,
)
from llm_gateway_core.services.http_client_factory import (
    close_http_clients,
    create_shared_http_client,
)
from llm_gateway_core.utils.logging_setup import configure_logging
from llm_gateway_core.middleware.request_logging import (
    RequestLoggingASGIMiddleware,
    log_middleware_functional as log_middleware_functional,
)
from llm_gateway_core.middleware.auth import (
    ApiKeyAuthMiddleware,
    api_key_auth as api_key_auth,
)
from llm_gateway_core.middleware.chat_logging import prepare_chat_response_observation
from llm_gateway_core.middleware.content_size import (
    ContentLengthLimitMiddleware,
    ContentSizeLimitMiddleware,
)
from llm_gateway_core.middleware.runtime_snapshot import (
    RuntimeAvailabilityMiddleware,
    RuntimeSnapshotMiddleware,
)
from llm_gateway_core.middleware.response_observation import (
    ResponseObservationMiddleware,
)
from llm_gateway_core.api.error_envelope import StructuredHTTPException, code_from_detail, error_response
from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.health import health_router
from llm_gateway_core.api.v1 import router as api_v1_router
from llm_gateway_core.api.v1.rules_editor import editor_router as api_v1_editor_router # Import the new editor router
from llm_gateway_core.api.v1.stats import stats_router as api_v1_stats_router # Import the new stats router
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB # Import TokensUsageDB
from llm_gateway_core.db.fallback_events_db import FallbackEventsDB
from llm_gateway_core.db.rejections_db import RejectionsDB
from llm_gateway_core.db.model_rotation_db import ModelRotationDB
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.db.write_batcher import WriteBatcher
from llm_gateway_core.services import session_secret
from llm_gateway_core.services.openrouter_free_models import (
    REFRESH_INTERVAL_SECONDS as CAPABILITY_AUTOFILL_REFRESH_INTERVAL_SECONDS,
    OpenRouterFreeModelsService,
)
from llm_gateway_core.services.fallback_model_evals import FallbackModelEvalService
from llm_gateway_core.services.capability_autofill import CapabilityAutofillService
from llm_gateway_core.services.health import HealthService
from llm_gateway_core.services.model_availability import run_startup_model_verification
from llm_gateway_core.services.access_control import UsdBudgetLedger
from llm_gateway_core.services.accounting_service import AccountingService
from llm_gateway_core.services.active_requests import ActiveRequestsRegistry
from llm_gateway_core.services.config_updates import ConfigUpdateCoordinator
from llm_gateway_core.services.deep_research_process import DeepResearchProcessRunner
from llm_gateway_core.services.rate_limiter import RateLimiter
from llm_gateway_core.services.ip_blocklist import IpBlockGuard
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState, fingerprint_api_key
from llm_gateway_core.services.upstream_subscription_quota import UpstreamSubscriptionQuotaService
from llm_gateway_core.services.runtime_candidate import build_runtime_candidate
from llm_gateway_core.services.runtime_config import (
    AppServices,
    RuntimeGenerationManager,
    RuntimeSnapshot,
)
from llm_gateway_core.services.task_supervisor import TaskSupervisor
from llm_gateway_core.services.stream_observation import StreamObservationCapacity
from llm_gateway_core.utils.html_cache import preload_templates
from llm_gateway_core.utils.log_redaction import safe_exception_type_name
from llm_gateway_core.services.upload_admission import UploadAdmission

# --- Application Setup ---

# Configure logging first
configure_logging()
logger = logging.getLogger(__name__)

USAGE_STATS_RETENTION_DAYS = 90
USAGE_STATS_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
# Periodic budget reset is checked frequently so daily/monthly boundaries fire
# within a minute of the scheduled UTC instant rather than once per day.
BUDGET_RESET_CHECK_INTERVAL_SECONDS = 60
DEEP_RESEARCH_IMAGES_RETENTION_DAYS = 10
DEEP_RESEARCH_IMAGES_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
# Capability autofill and the OpenRouter catalog refresh are independent 8h
# timers; on a cold start the catalog is usually still empty when the first
# materialize() runs. Sleeping the full 8h interval in that case would leave
# the OpenRouter fallback source unavailable for up to 8h, so the loop polls
# this much more often until the catalog gets populated.
CAPABILITY_AUTOFILL_COLD_START_INTERVAL_SECONDS = 300.0
_T = TypeVar("_T")


def _prepare_source_outputs_images_dir() -> None:
    if "GATEWAY_OUTPUTS_DIR" in os.environ:
        return
    prepare_default_images_root(PROJECT_ROOT, OUTPUTS_IMAGES_DIR)

# Optional: Lifespan context manager for startup/shutdown events
# (e.g., initializing database connections, closing clients)
def ensure_gateway_api_key_configured() -> None:
    if not settings.gateway_api_key or not settings.gateway_api_key.strip():
        message = "GATEWAY_API_KEY must be set before application startup and remain configured at runtime."
        logger.critical(message)
        raise RuntimeError(message)
    if is_placeholder_secret(settings.gateway_api_key):
        message = "GATEWAY_API_KEY uses a placeholder value; replace it with a real secret before startup."
        logger.critical(message)
        raise RuntimeError(message)


def resolve_write_batcher_db_path(tokens_usage_db: object) -> Path:
    db_path = getattr(tokens_usage_db, "db_path", None)

    if isinstance(db_path, PurePath):
        return Path(db_path)
    if isinstance(db_path, str) and db_path:
        return Path(db_path)

    # Resolved here rather than up front: resolve_db_dir creates the directory,
    # and the paths above need no such directory made for them.
    fallback_path = resolve_db_dir() / "tokens_usage.db"
    if db_path is not None:
        logger.warning(
            "Ignoring unsupported tokens_usage_db.db_path value of type %s; using %s instead.",
            type(db_path).__name__,
            fallback_path,
        )
    return fallback_path


async def run_usage_stats_cleanup_loop(
    tokens_usage_db: TokensUsageDB,
    fallback_events_db: FallbackEventsDB,
    rejections_db: RejectionsDB,
    *,
    retention_days: int = USAGE_STATS_RETENTION_DAYS,
    interval_seconds: float = USAGE_STATS_CLEANUP_INTERVAL_SECONDS,
) -> None:
    logger.info(
        "Usage statistics cleanup task started. Interval: %s seconds. Retention: %s days.",
        interval_seconds,
        retention_days,
    )
    try:
        while True:
            try:
                await run_cancellation_resistant_thread_worker(
                    tokens_usage_db.cleanup_old_records,
                    retention_days=retention_days,
                )
                await run_cancellation_resistant_thread_worker(
                    fallback_events_db.cleanup_old_records,
                    retention_days=retention_days,
                )
                await run_cancellation_resistant_thread_worker(
                    rejections_db.cleanup_old_records,
                    retention_days=retention_days,
                )
            except Exception:
                logger.exception("Usage statistics cleanup iteration failed.")
            await scheduler_sleep(interval_seconds)
    except asyncio.CancelledError:
        logger.info("Usage statistics cleanup task stopped.")
        raise


def start_usage_stats_cleanup_task(
    tokens_usage_db: TokensUsageDB,
    fallback_events_db: FallbackEventsDB,
    rejections_db: RejectionsDB,
    *,
    supervisor: TaskSupervisor,
) -> asyncio.Task:
    return supervisor.create_task(
        run_usage_stats_cleanup_loop(tokens_usage_db, fallback_events_db, rejections_db),
        name="usage-stats-cleanup",
    )


async def run_deep_research_images_cleanup_loop(
    image_retention_service: ImageRetentionService,
    *,
    interval_seconds: float = DEEP_RESEARCH_IMAGES_CLEANUP_INTERVAL_SECONDS,
) -> None:
    logger.info(
        "Deep research images cleanup task started. Interval: %s seconds.",
        interval_seconds,
    )
    try:
        while True:
            await scheduler_sleep(interval_seconds)
            try:
                snapshot = await run_cancellation_resistant_thread_worker(
                    image_retention_service.run,
                )
                deleted = (
                    snapshot.deleted_final
                    + snapshot.deleted_temp
                    + snapshot.deleted_dirs
                )
                if deleted:
                    logger.info(
                        "Deep research images cleanup removed %s stale entry(s).",
                        deleted,
                    )
                if snapshot.failures:
                    logger.warning(
                        "Deep research images cleanup completed with %s failure(s).",
                        snapshot.failures,
                    )
            except Exception:
                logger.exception("Deep research images cleanup iteration failed.")
    except asyncio.CancelledError:
        logger.info("Deep research images cleanup task stopped.")
        raise


def start_deep_research_images_cleanup_task(
    image_retention_service: ImageRetentionService,
    *,
    supervisor: TaskSupervisor,
    interval_seconds: float = DEEP_RESEARCH_IMAGES_CLEANUP_INTERVAL_SECONDS,
) -> asyncio.Task:
    return supervisor.create_task(
        run_deep_research_images_cleanup_loop(
            image_retention_service,
            interval_seconds=interval_seconds,
        ),
        name="deep-research-images-cleanup",
    )


async def run_budget_reset_loop(
    accounting_service: AccountingService,
    *,
    interval_seconds: float = BUDGET_RESET_CHECK_INTERVAL_SECONDS,
) -> None:
    logger.info(
        "Periodic budget reset task started. Interval: %s seconds.",
        interval_seconds,
    )
    try:
        while True:
            try:
                await accounting_service.reset_due_budgets(
                    now=datetime.now(timezone.utc),
                )
            except Exception:
                logger.exception("Periodic budget reset iteration failed.")
            await scheduler_sleep(interval_seconds)
    except asyncio.CancelledError:
        logger.info("Periodic budget reset task stopped.")
        raise


def start_budget_reset_task(
    accounting_service: AccountingService,
    *,
    supervisor: TaskSupervisor,
) -> asyncio.Task:
    return supervisor.create_task(
        run_budget_reset_loop(accounting_service),
        name="budget-reset",
    )


def _select_capability_autofill_interval(
    *,
    openrouter_configured: bool,
    capability_index_empty: bool,
    default_interval_seconds: float,
) -> float:
    """Pick the next capability-autofill sleep interval.

    Short-circuits to a brief cold-start poll only while the official
    OpenRouter provider is configured but its capability index has not been
    populated yet by the (independent) OpenRouter refresh loop -- otherwise
    the OpenRouter fallback source would stay unavailable for up to a full
    default interval after every cold start.
    """
    if openrouter_configured and capability_index_empty:
        return CAPABILITY_AUTOFILL_COLD_START_INTERVAL_SECONDS
    return default_interval_seconds


async def run_capability_autofill_loop(
    capability_autofill_service: CapabilityAutofillService,
    *,
    runtime_manager: RuntimeGenerationManager,
    config_update_coordinator: ConfigUpdateCoordinator,
    shared_http_client: httpx.AsyncClient,
    openrouter_free_models_service: OpenRouterFreeModelsService,
    interval_seconds: float = CAPABILITY_AUTOFILL_REFRESH_INTERVAL_SECONDS,
) -> None:
    logger.info(
        "Capability autofill task started. Interval: %s seconds.",
        interval_seconds,
    )
    try:
        while True:
            # A failed iteration must not kill the supervised 8h loop for the
            # rest of the process lifetime -- log it and retry on the default
            # cadence instead.
            sleep_seconds = interval_seconds
            try:
                openrouter_status = await openrouter_free_models_service.get_status()
                capability_index_empty = not await openrouter_free_models_service.get_capability_index()
                # materialize() never raises: failures are logged and absorbed internally.
                await capability_autofill_service.materialize(
                    runtime_manager=runtime_manager,
                    config_update_coordinator=config_update_coordinator,
                    shared_http_client=shared_http_client,
                    openrouter_free_models_service=openrouter_free_models_service,
                )
                sleep_seconds = _select_capability_autofill_interval(
                    openrouter_configured=bool(openrouter_status.get("configured")),
                    capability_index_empty=capability_index_empty,
                    default_interval_seconds=interval_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Capability autofill iteration failed; retrying in %s seconds.",
                    sleep_seconds,
                )
            await scheduler_sleep(sleep_seconds)
    except asyncio.CancelledError:
        logger.info("Capability autofill task stopped.")
        raise


def start_capability_autofill_task(
    capability_autofill_service: CapabilityAutofillService,
    *,
    runtime_manager: RuntimeGenerationManager,
    config_update_coordinator: ConfigUpdateCoordinator,
    shared_http_client: httpx.AsyncClient,
    openrouter_free_models_service: OpenRouterFreeModelsService,
    supervisor: TaskSupervisor,
) -> asyncio.Task:
    return supervisor.create_task(
        run_capability_autofill_loop(
            capability_autofill_service,
            runtime_manager=runtime_manager,
            config_update_coordinator=config_update_coordinator,
            shared_http_client=shared_http_client,
            openrouter_free_models_service=openrouter_free_models_service,
        ),
        name="capability-autofill",
    )


async def run_cancellation_resistant_thread_worker(
    function: Callable[..., _T],
    *args: object,
    **kwargs: object,
) -> _T:
    """Finish one blocking worker before propagating parent cancellation."""
    worker_coroutine = asyncio.to_thread(function, *args, **kwargs)
    try:
        worker_task = asyncio.get_running_loop().create_task(
            worker_coroutine,
            name="periodic-blocking-worker",
        )
    except BaseException:
        worker_coroutine.close()
        raise
    cancellation: asyncio.CancelledError | None = None
    while not worker_task.done():
        try:
            await asyncio.shield(worker_task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
        except BaseException:
            if worker_task.done():
                # The exception is the worker's own outcome (it just
                # finished); .result() below will safely reproduce it.
                break
            # Something other than our own cancellation interrupted this
            # await while the worker is still running (for example
            # GeneratorExit from the coroutine being closed). Do not call
            # .result() on an unfinished task (raises InvalidStateError,
            # masking the real exception) and do not swallow the original
            # exception: hand the worker's eventual outcome to a
            # done-callback so it is still retrieved and logged, then
            # re-raise the original exception immediately.
            worker_task.add_done_callback(_discard_worker_task_outcome)
            raise

    try:
        result = worker_task.result()
    except BaseException as worker_exc:
        if cancellation is None:
            raise
        _log_cleanup_failure("periodic-blocking-worker", worker_exc)
        raise cancellation
    if cancellation is not None:
        raise cancellation
    return result


def _discard_worker_task_outcome(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        _log_cleanup_failure("periodic-blocking-worker", exception)


def _load_initial_config() -> ConfigLoader:
    loader = ConfigLoader()
    AtomicConfigFileTransaction.recover_pending(loader.configured_paths)
    return loader.load_complete()


def _log_cleanup_failure(resource: str, exception: BaseException) -> None:
    logger.error(
        "Resource cleanup failed: resource=%s exception_type=%s.",
        resource,
        safe_exception_type_name(exception),
    )


def _push_safe_cleanup(
    stack: AsyncExitStack,
    resource: str,
    callback: Callable[[], Awaitable[object]],
) -> None:
    async def cleanup() -> None:
        try:
            await callback()
        except BaseException as exc:
            _log_cleanup_failure(resource, exc)

    stack.push_async_callback(cleanup)


async def _close_stream_observation_capacity(
    capacity: StreamObservationCapacity,
) -> None:
    capacity.close(reason="lifespan_shutdown")


async def _verify_stream_observation_capacity_drained(
    capacity: StreamObservationCapacity,
) -> None:
    snapshot = capacity.snapshot
    if snapshot.active_items or snapshot.active_bytes or snapshot.waiters:
        raise RuntimeError("Stream observation capacity did not drain during shutdown.")


async def _close_upload_admission(admission: UploadAdmission) -> None:
    admission.close()


async def _verify_upload_admission_drained(admission: UploadAdmission) -> None:
    snapshot = admission.snapshot
    if snapshot.active_bytes or snapshot.active_leases or snapshot.waiters:
        raise RuntimeError("Upload admission did not drain during shutdown.")


async def _drain_resource_stack(stack: AsyncExitStack) -> None:
    drain_task = asyncio.get_running_loop().create_task(
        stack.aclose(),
        name="lifespan-resource-drain",
    )
    cancellation: asyncio.CancelledError | None = None
    while not drain_task.done():
        try:
            await asyncio.shield(drain_task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
    drain_task.result()
    if cancellation is not None:
        raise cancellation


_MISSING_STATE_VALUE = object()


class _TemporaryStateBinding:
    def __init__(self, state: object) -> None:
        self._state = state
        self._previous_services: object = _MISSING_STATE_VALUE
        self._owned_services: object = _MISSING_STATE_VALUE

    def bind(self, services: AppServices) -> None:
        self._previous_services = getattr(
            self._state,
            "services",
            _MISSING_STATE_VALUE,
        )
        self._owned_services = services
        self._set("services", services)

    def _set(self, name: str, value: object) -> None:
        setattr(self._state, name, value)

    async def rollback(self) -> None:
        owned_services = self._owned_services
        previous_services = self._previous_services
        if owned_services is _MISSING_STATE_VALUE:
            return
        try:
            current_services = getattr(
                self._state,
                "services",
                _MISSING_STATE_VALUE,
            )
            if current_services is not owned_services:
                return
            if previous_services is _MISSING_STATE_VALUE:
                delattr(self._state, "services")
            else:
                setattr(self._state, "services", previous_services)
        except BaseException as exc:
            _log_cleanup_failure("app_state.services", exc)
        finally:
            self._previous_services = _MISSING_STATE_VALUE
            self._owned_services = _MISSING_STATE_VALUE


async def _build_initial_snapshot(
    manager: RuntimeGenerationManager,
    config_loader: ConfigLoader,
    http_client: httpx.AsyncClient,
) -> RuntimeSnapshot:
    candidate = await build_runtime_candidate(
        manager=manager,
        generation=1,
        config_loader=config_loader,
        shared_http_client=http_client,
    )
    try:
        return candidate.install_initial()
    except BaseException:
        try:
            await candidate.close_unpublished()
        except BaseException as cleanup_exc:
            _log_cleanup_failure("initial-runtime-candidate", cleanup_exc)
        raise


_LIFESPAN_OWNER_ATTRIBUTE = "_llm_gateway_lifespan_owner"
_LIFESPAN_OWNER_LOCK = Lock()
_MISSING_LIFESPAN_OWNER = object()


def _claim_lifespan_owner(state: object) -> tuple[object, bool, object]:
    owner = object()
    with _LIFESPAN_OWNER_LOCK:
        previous = getattr(
            state,
            _LIFESPAN_OWNER_ATTRIBUTE,
            _MISSING_LIFESPAN_OWNER,
        )
        if previous is not _MISSING_LIFESPAN_OWNER and previous is not None:
            raise RuntimeError("Application lifespan is already active.")
        marker_existed = previous is not _MISSING_LIFESPAN_OWNER
        setattr(state, _LIFESPAN_OWNER_ATTRIBUTE, owner)
    return owner, marker_existed, previous


def _release_lifespan_owner(
    state: object,
    owner: object,
    marker_existed: bool,
    marker_previous: object,
) -> None:
    with _LIFESPAN_OWNER_LOCK:
        current = getattr(
            state,
            _LIFESPAN_OWNER_ATTRIBUTE,
            _MISSING_LIFESPAN_OWNER,
        )
        if current is not owner:
            return
        if marker_existed:
            setattr(state, _LIFESPAN_OWNER_ATTRIBUTE, marker_previous)
        else:
            delattr(state, _LIFESPAN_OWNER_ATTRIBUTE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_single_worker_environment(os.environ)
    logger.info("LLM Gateway v%s: Application startup...", __version__)
    state = app.state
    owner, marker_existed, marker_previous = _claim_lifespan_owner(state)
    resources = AsyncExitStack()
    health_service: HealthService | None = None
    try:
        ensure_gateway_api_key_configured()
        raw_state_dir = resolve_db_dir()
        raw_state_dir.mkdir(parents=True, exist_ok=True)
        state_dir = raw_state_dir.resolve(strict=True)
        single_process_lease = SingleProcessLease.acquire(state_dir)
        _push_safe_cleanup(
            resources,
            "single-process-lease",
            single_process_lease.close,
        )
        # Issue or load the cookie-signing secret while the lease is held, so a
        # concurrent first start cannot have two processes mint rival secrets.
        # Doing it at boot also means a state directory that is read-only or
        # owned by another user fails the startup rather than looking healthy
        # until the first person tries to log in.
        session_secret.get_session_secret(settings.gateway_api_key)
        config_loader = _load_initial_config()
        _prepare_source_outputs_images_dir()
        image_storage = GeneratedImageStorage(OUTPUTS_IMAGES_DIR)
        image_storage.probe()
        image_retention_service = ImageRetentionService(
            image_storage,
            DEEP_RESEARCH_IMAGES_RETENTION_DAYS,
        )
        await run_cancellation_resistant_thread_worker(
            image_retention_service.run,
        )
        preload_templates([
            STATIC_DIR / "login.html",
            STATIC_DIR / "usage-stats.html",
            STATIC_DIR / "rules-editor.html",
            STATIC_DIR / "web-playground.html",
            STATIC_DIR / "gateway-docs.html",
            STATIC_DIR / "translator-debug.html",
            STATIC_DIR / "pricing.html",
        ])
        http_client = create_shared_http_client()
        _push_safe_cleanup(
            resources,
            "shared-http-client",
            lambda: close_http_clients({"shared": http_client}),
        )

        tokens_usage_db = TokensUsageDB()
        fallback_events_db = FallbackEventsDB()
        rejections_db = RejectionsDB()
        api_keys_db = ApiKeysDB()
        usd_budget_ledger = UsdBudgetLedger()
        active_requests_registry = ActiveRequestsRegistry()
        rate_limiter = RateLimiter()
        ip_block_guard = (
            IpBlockGuard(
                max_failures=settings.ip_block_max_failures,
                block_seconds=settings.ip_block_duration_minutes * 60,
            )
            if settings.ip_block_enabled
            else None
        )
        upstream_routing_state = UpstreamRoutingState()

        write_batcher = WriteBatcher(
            resolve_write_batcher_db_path(tokens_usage_db),
            queue_maxsize=settings.write_batcher_queue_maxsize,
        )
        _push_safe_cleanup(resources, "write-batcher", write_batcher.stop)
        await write_batcher.start()
        for database in (tokens_usage_db, fallback_events_db, rejections_db):
            if hasattr(database, "set_batcher"):
                database.set_batcher(write_batcher)

        accounting_service = AccountingService(
            tokens_usage_db,
            api_keys_db,
            budget_ledger=usd_budget_ledger,
            rate_limiter=rate_limiter,
        )
        # Deliberately not wrapped in _push_safe_cleanup: stop() raises when a
        # charge stayed unsettled (e.g. a projection the sink kept rejecting),
        # and that is money spent upstream but never recorded in the ledger.
        # Swallowing it here would turn a real accounting debt into a silent
        # shutdown.
        resources.push_async_callback(accounting_service.stop)
        await accounting_service.start()

        def resolve_legacy_rotation_scope(token: str) -> str | None:
            if settings.gateway_api_key and token == settings.gateway_api_key:
                return f"master:{fingerprint_api_key(token)}"
            get_by_key = getattr(api_keys_db, "get_by_key", None)
            if get_by_key is None:
                return None
            record = get_by_key(token)
            if record is not None:
                return f"user:{record.id}"
            return None

        model_rotation_db = ModelRotationDB(
            legacy_scope_resolver=resolve_legacy_rotation_scope
        )
        upstream_subscription_quota_service = UpstreamSubscriptionQuotaService(
            http_client=http_client
        )

        runtime_manager = RuntimeGenerationManager()
        _push_safe_cleanup(resources, "runtime-generation-manager", runtime_manager.shutdown)
        initial_snapshot = await _build_initial_snapshot(
            runtime_manager,
            config_loader,
            http_client,
        )

        openrouter_free_models_service = OpenRouterFreeModelsService()
        _push_safe_cleanup(
            resources,
            "openrouter-free-models-service",
            openrouter_free_models_service.stop,
        )
        await openrouter_free_models_service.start_runtime(
            runtime_manager=runtime_manager,
            shared_http_client=http_client,
        )

        fallback_model_eval_service = FallbackModelEvalService()
        _push_safe_cleanup(
            resources,
            "fallback-model-eval-service",
            fallback_model_eval_service.stop,
        )

        capability_autofill_service = CapabilityAutofillService()

        task_supervisor = TaskSupervisor()
        stream_observation_capacity = StreamObservationCapacity(
            max_items=settings.stream_chunk_queue_maxsize,
            max_bytes=settings.stream_observation_buffer_max_bytes,
        )
        json_response_capacity = StreamObservationCapacity(
            max_items=settings.stream_chunk_queue_maxsize,
            max_bytes=settings.json_response_inflight_max_bytes,
        )
        upload_admission = UploadAdmission(
            max_bytes=settings.upload_inflight_max_bytes,
        )
        _push_safe_cleanup(
            resources,
            "stream-observation-capacity-drain",
            lambda: _verify_stream_observation_capacity_drained(
                stream_observation_capacity
            ),
        )
        _push_safe_cleanup(
            resources,
            "json-response-capacity-drain",
            lambda: _verify_stream_observation_capacity_drained(
                json_response_capacity
            ),
        )
        _push_safe_cleanup(
            resources,
            "upload-admission-drain",
            lambda: _verify_upload_admission_drained(upload_admission),
        )
        _push_safe_cleanup(resources, "task-supervisor", task_supervisor.close)
        _push_safe_cleanup(
            resources,
            "stream-observation-capacity",
            lambda: _close_stream_observation_capacity(
                stream_observation_capacity
            ),
        )
        _push_safe_cleanup(
            resources,
            "json-response-capacity",
            lambda: _close_stream_observation_capacity(json_response_capacity),
        )
        _push_safe_cleanup(
            resources,
            "upload-admission",
            lambda: _close_upload_admission(upload_admission),
        )

        config_update_coordinator = ConfigUpdateCoordinator(
            runtime_manager=runtime_manager,
            shared_http_client=http_client,
            initial_snapshot=initial_snapshot,
        )
        _push_safe_cleanup(
            resources,
            "config-update-coordinator",
            config_update_coordinator.close,
        )
        health_service = HealthService(
            runtime_manager=runtime_manager,
            config_update_coordinator=config_update_coordinator,
            accounting_service=accounting_service,
            write_batcher=write_batcher,
            image_retention_service=image_retention_service,
            database_paths=(
                tokens_usage_db.db_path,
                fallback_events_db.db_path,
                rejections_db.db_path,
                api_keys_db.db_path,
                model_rotation_db.db_path,
                write_batcher.db_path,
            ),
            task_supervisor=task_supervisor,
        )

        await run_startup_model_verification(
            mode=settings.verify_models_on_startup,
            providers_config=initial_snapshot.config_loader.providers_config,
            fallback_rules=initial_snapshot.config_loader.fallback_rules,
            operation_rules=initial_snapshot.config_loader.operation_rules,
            provider_models_service=initial_snapshot.provider_models_service,
            http_client=http_client,
        )

        start_usage_stats_cleanup_task(
            tokens_usage_db,
            fallback_events_db,
            rejections_db,
            supervisor=task_supervisor,
        )
        start_deep_research_images_cleanup_task(
            image_retention_service,
            supervisor=task_supervisor,
        )
        start_budget_reset_task(
            accounting_service,
            supervisor=task_supervisor,
        )
        start_capability_autofill_task(
            capability_autofill_service,
            runtime_manager=runtime_manager,
            config_update_coordinator=config_update_coordinator,
            shared_http_client=http_client,
            openrouter_free_models_service=openrouter_free_models_service,
            supervisor=task_supervisor,
        )

        deep_research_process_runner = DeepResearchProcessRunner(
            capacity=settings.deep_research_process_capacity,
            admission_timeout_seconds=(
                settings.deep_research_admission_timeout_seconds
            ),
        )
        await deep_research_process_runner.start()

        state_binding: _TemporaryStateBinding | None = None
        try:
            services = AppServices(
                runtime_manager=runtime_manager,
                config_update_coordinator=config_update_coordinator,
                http_client=http_client,
                tokens_usage_db=tokens_usage_db,
                fallback_events_db=fallback_events_db,
                rejections_db=rejections_db,
                api_keys_db=api_keys_db,
                model_rotation_db=model_rotation_db,
                write_batcher=write_batcher,
                accounting_service=accounting_service,
                health_service=health_service,
                image_storage=image_storage,
                image_retention_service=image_retention_service,
                usd_budget_ledger=usd_budget_ledger,
                active_requests_registry=active_requests_registry,
                rate_limiter=rate_limiter,
                ip_block_guard=ip_block_guard,
                upstream_routing_state=upstream_routing_state,
                upstream_subscription_quota_service=upstream_subscription_quota_service,
                openrouter_free_models_service=openrouter_free_models_service,
                fallback_model_eval_service=fallback_model_eval_service,
                capability_autofill_service=capability_autofill_service,
                deep_research_process_runner=deep_research_process_runner,
                upload_admission=upload_admission,
                upload_admission_timeout_seconds=(
                    settings.upload_admission_timeout_seconds
                ),
                stream_observation_capacity=stream_observation_capacity,
                json_response_capacity=json_response_capacity,
                stream_event_max_bytes=settings.stream_event_max_bytes,
                json_response_max_bytes=settings.json_response_max_bytes,
                task_supervisor=task_supervisor,
            )
            state_binding = _TemporaryStateBinding(state)
            state_binding.bind(services)
        except BaseException:
            try:
                await deep_research_process_runner.aclose()
            except BaseException as cleanup_exc:
                _log_cleanup_failure("deep-research-process-runner", cleanup_exc)
            if state_binding is not None:
                await state_binding.rollback()
            raise
        assert state_binding is not None
        _push_safe_cleanup(resources, "application-state", state_binding.rollback)
        _push_safe_cleanup(
            resources,
            "deep-research-process-runner",
            deep_research_process_runner.aclose,
        )
        health_service.mark_ready()
        logger.info("Application startup completed with runtime generation 1.")
        yield
    except BaseException:
        if health_service is not None:
            health_service.mark_failed()
        try:
            await _drain_resource_stack(resources)
        except BaseException as cleanup_exc:
            _log_cleanup_failure("lifespan-resource-stack", cleanup_exc)
        raise
    else:
        logger.info("Application shutdown...")
        if health_service is not None:
            health_service.mark_stopping()
        try:
            await _drain_resource_stack(resources)
        except BaseException as cleanup_exc:
            _log_cleanup_failure("lifespan-resource-stack", cleanup_exc)
            raise
    finally:
        try:
            _release_lifespan_owner(
                state,
                owner,
                marker_existed,
                marker_previous,
            )
        except BaseException as marker_exc:
            _log_cleanup_failure("lifespan-owner-marker", marker_exc)

# Create FastAPI app instance
STATIC_FILES_DIR = STATIC_DIR
STATIC_FILES_DIR.mkdir(parents=True, exist_ok=True) # Ensure static directory exists

app = FastAPI(
    title="LLMGateway",
    description="A gateway for routing LLM requests with fallback and rotation.",
    version=__version__,
    lifespan=lifespan,
)

# --- Middleware Configuration ---

def configure_cors(app: FastAPI, cors_allow_origins: list[str] | None = None) -> None:
    if not cors_allow_origins:
        logger.info("CORS middleware is DISABLED because CORS_ALLOW_ORIGINS is not set.")
        return

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


async def add_anthropic_version_header(request: Request, call_next):
    response = await call_next(request)
    if "/v1/messages" in request.url.path or "/v1/models" in request.url.path or "anthropic-version" in request.headers:
        if "anthropic-version" not in response.headers:
            response.headers["anthropic-version"] = "2023-06-01"
    return response


async def add_routing_diagnostic_headers(request: Request, call_next):
    response = await call_next(request)
    if not settings.routing_diagnostic_headers:
        return response

    provider = getattr(request.state, "llmgateway_provider", None)
    model = getattr(request.state, "llmgateway_provider_model", None)
    key_fingerprint = getattr(request.state, "llmgateway_upstream_key_fingerprint", None)
    if provider and model:
        routed_via = f"{provider}/{model}"
        if key_fingerprint:
            routed_via = f"{routed_via};key={key_fingerprint}"
        response.headers["X-Routed-Via"] = routed_via

    attempts = getattr(request.state, "llmgateway_fallback_attempts", None)
    if isinstance(attempts, list) and attempts:
        response.headers["X-Fallback-Attempts"] = str(len(attempts))
    return response


def configure_gateway_middleware(
    app: FastAPI,
    *,
    cors_allow_origins: list[str] | None = None,
    max_request_body_bytes: int | None = None,
) -> None:
    # Starlette wraps middleware in reverse registration order. The layers
    # below are registered inside-out so inbound requests hit CORS, request
    # logging, the Content-Length guard, runtime availability, auth, aggregate
    # body admission, and only then the runtime lease and endpoint layers.
    logger.info("Chat usage tracking is ENABLED.")
    if settings.log_chat_messages:
        logger.info("Chat message file logging is ENABLED.")
    else:
        logger.info("Chat message file logging is DISABLED.")

    app.middleware("http")(add_anthropic_version_header)
    app.middleware("http")(add_routing_diagnostic_headers)
    app.add_middleware(
        ResponseObservationMiddleware,
        request_preparer=prepare_chat_response_observation,
    )
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(RuntimeSnapshotMiddleware)
    app.add_middleware(
        ContentSizeLimitMiddleware,
        max_body_size=max_request_body_bytes or settings.max_request_body_bytes,
    )
    app.add_middleware(ApiKeyAuthMiddleware)
    app.add_middleware(RuntimeAvailabilityMiddleware)
    app.add_middleware(
        ContentLengthLimitMiddleware,
        max_body_size=max_request_body_bytes or settings.max_request_body_bytes,
    )
    app.add_middleware(RequestLoggingASGIMiddleware)
    configure_cors(app, cors_allow_origins)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception during {request.method} {request.url.path}: {exc}", exc_info=True)
    return error_response(
        request,
        status_code=500,
        detail="Internal server error",
        error_type="internal_error",
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    extra = exc.extra_payload if isinstance(exc, StructuredHTTPException) else None
    return error_response(
        request,
        status_code=exc.status_code,
        detail=exc.detail,
        code=code_from_detail(exc.detail),
        headers=exc.headers,
        extra=extra,
    )

configure_gateway_middleware(
    app,
    cors_allow_origins=settings.cors_allow_origins,
    max_request_body_bytes=settings.max_request_body_bytes,
)


# --- API Routers ---

# Include auth/login and root redirect routes
app.include_router(auth_router)
app.include_router(health_router)
# Include the main v1 API router
app.include_router(api_v1_router, prefix="/v1")
# Include the v1 editor API router, also under /v1 prefix
app.include_router(api_v1_editor_router, prefix="/v1", tags=["Config Editor"])
# Include the v1 stats API router, also under /v1 prefix
app.include_router(api_v1_stats_router, prefix="/v1", tags=["Usage Stats"])

# --- Static Files ---
app.mount("/static", StaticFiles(directory=str(STATIC_FILES_DIR)), name="static")
logger.info(f"Static files mounted from {STATIC_FILES_DIR}")

# Deep research generated illustrations. StaticFiles resolves paths via
# os.path.realpath, so path-traversal (../) is blocked. The directory is
# protected by api_key_auth — only authenticated sessions or API keys can read
# files (the middleware inspects the mount as a registered route).
app.mount(
    "/outputs/images",
    StaticFiles(directory=str(OUTPUTS_IMAGES_DIR), check_dir=False),
    name="outputs-images",
)
logger.info(f"Deep research images mounted from {OUTPUTS_IMAGES_DIR}")

def run_server() -> None:
    validate_single_worker_environment(os.environ)
    logger.info(
        "Starting Uvicorn server on host %s port %s",
        settings.gateway_host,
        settings.gateway_port,
    )
    uvicorn.run(
        "main:app",
        host=settings.gateway_host,
        port=settings.gateway_port,
        reload=settings.debug_mode,
        log_level=settings.log_level.lower(),
        workers=SINGLE_APPLICATION_WORKER_COUNT,
    )


# --- Main Execution Block (for direct running) ---
if __name__ == "__main__":
    run_server()
