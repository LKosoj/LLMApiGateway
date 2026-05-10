import asyncio
import logging
import uvicorn
import httpx
from asyncio import create_task as asyncio_create_task
from asyncio import sleep as scheduler_sleep
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from contextlib import asynccontextmanager, suppress
from pathlib import Path, PurePath

# Import components from the new core structure
from llm_gateway_core.config.settings import settings
from llm_gateway_core.config.paths import OUTPUTS_IMAGES_DIR, PROJECT_ROOT, STATIC_DIR
from llm_gateway_core.config.loader import ConfigLoader, resolve_provider_proxy
from llm_gateway_core.services.image_retention import cleanup_old_images
from llm_gateway_core.utils.logging_setup import configure_logging
from llm_gateway_core.middleware.request_logging import log_middleware_functional
from llm_gateway_core.middleware.auth import api_key_auth # Functional middleware
from llm_gateway_core.middleware import chat_logging as _chat_logging_module
from llm_gateway_core.middleware.chat_logging import log_chat_completions # Functional middleware
from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1 import router as api_v1_router
from llm_gateway_core.api.v1.rules_editor import editor_router as api_v1_editor_router # Import the new editor router
from llm_gateway_core.api.v1.stats import stats_router as api_v1_stats_router # Import the new stats router
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB # Import TokensUsageDB
from llm_gateway_core.db.fallback_events_db import FallbackEventsDB
from llm_gateway_core.db.model_rotation_db import ModelRotationDB
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.db.write_batcher import WriteBatcher
from llm_gateway_core.api.v1 import chat as _chat_router_module
from llm_gateway_core.services.provider_models import ProviderModelsService
from llm_gateway_core.services.openrouter_free_models import OpenRouterFreeModelsService
from llm_gateway_core.services.model_availability import run_startup_model_verification
from llm_gateway_core.services.access_control import UsdBudgetLedger
from llm_gateway_core.services.active_requests import ActiveRequestsRegistry
from llm_gateway_core.services.rate_limiter import RateLimiter
from llm_gateway_core.services.request_handler import OperationDispatcher
from llm_gateway_core.utils.html_cache import preload_templates

# --- Application Setup ---

# Configure logging first
configure_logging()
logger = logging.getLogger(__name__)

HTTP_CLIENT_CONNECT_TIMEOUT_SECONDS = 30.0
HTTP_CLIENT_READ_TIMEOUT_SECONDS = 300.0
HTTP_CLIENT_WRITE_TIMEOUT_SECONDS = 60.0
HTTP_CLIENT_POOL_TIMEOUT_SECONDS = 30.0
HTTP_CLIENT_MAX_CONNECTIONS = 200
HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS = 40
USAGE_STATS_RETENTION_DAYS = 90
USAGE_STATS_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
DEEP_RESEARCH_IMAGES_RETENTION_DAYS = 10
DEEP_RESEARCH_IMAGES_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60

# Optional: Lifespan context manager for startup/shutdown events
# (e.g., initializing database connections, closing clients)
def ensure_gateway_api_key_configured() -> None:
    if not settings.gateway_api_key or not settings.gateway_api_key.strip():
        message = "GATEWAY_API_KEY must be set before application startup and remain configured at runtime."
        logger.critical(message)
        raise RuntimeError(message)


def _default_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=HTTP_CLIENT_CONNECT_TIMEOUT_SECONDS,
        read=HTTP_CLIENT_READ_TIMEOUT_SECONDS,
        write=HTTP_CLIENT_WRITE_TIMEOUT_SECONDS,
        pool=HTTP_CLIENT_POOL_TIMEOUT_SECONDS,
    )


def _default_pool_limits() -> httpx.Limits:
    return httpx.Limits(
        max_connections=HTTP_CLIENT_MAX_CONNECTIONS,
        max_keepalive_connections=HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS,
    )


def create_shared_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_default_timeout(), limits=_default_pool_limits())


def create_proxy_http_clients(
    providers_config: dict,
) -> dict[str, httpx.AsyncClient]:
    """Create dedicated httpx clients for providers that have a proxy configured."""
    clients: dict[str, httpx.AsyncClient] = {}
    if not isinstance(providers_config, dict):
        return clients
    for provider_name, details in providers_config.items():
        proxy_url = resolve_provider_proxy(getattr(details, "proxy", None))
        if proxy_url:
            clients[provider_name] = httpx.AsyncClient(
                proxy=proxy_url,
                timeout=_default_timeout(),
                limits=_default_pool_limits(),
            )
            logger.info("Created proxy HTTP client for provider '%s'.", provider_name)
    return clients


def resolve_write_batcher_db_path(tokens_usage_db: object) -> Path:
    fallback_path = PROJECT_ROOT / "db" / "tokens_usage.db"
    db_path = getattr(tokens_usage_db, "db_path", None)

    if isinstance(db_path, PurePath):
        return Path(db_path)
    if isinstance(db_path, str) and db_path:
        return Path(db_path)

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
            await asyncio.to_thread(
                tokens_usage_db.cleanup_old_records,
                retention_days=retention_days,
            )
            await asyncio.to_thread(
                fallback_events_db.cleanup_old_records,
                retention_days=retention_days,
            )
            await scheduler_sleep(interval_seconds)
    except asyncio.CancelledError:
        logger.info("Usage statistics cleanup task stopped.")
        raise


def start_usage_stats_cleanup_task(tokens_usage_db: TokensUsageDB, fallback_events_db: FallbackEventsDB) -> asyncio.Task:
    return asyncio_create_task(run_usage_stats_cleanup_loop(tokens_usage_db, fallback_events_db))


async def run_deep_research_images_cleanup_loop(
    images_root: Path,
    *,
    retention_days: int = DEEP_RESEARCH_IMAGES_RETENTION_DAYS,
    interval_seconds: float = DEEP_RESEARCH_IMAGES_CLEANUP_INTERVAL_SECONDS,
) -> None:
    logger.info(
        "Deep research images cleanup task started. Root: %s. Interval: %s seconds. Retention: %s days.",
        images_root,
        interval_seconds,
        retention_days,
    )
    try:
        while True:
            try:
                deleted = await asyncio.to_thread(
                    cleanup_old_images,
                    images_root,
                    retention_days,
                )
                if deleted:
                    logger.info("Deep research images cleanup removed %s stale file(s).", deleted)
            except Exception:
                logger.exception("Deep research images cleanup iteration failed.")
            await scheduler_sleep(interval_seconds)
    except asyncio.CancelledError:
        logger.info("Deep research images cleanup task stopped.")
        raise


def start_deep_research_images_cleanup_task(images_root: Path) -> asyncio.Task:
    return asyncio_create_task(run_deep_research_images_cleanup_loop(images_root))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("LLM Gateway v1.10: Application startup...")
    ensure_gateway_api_key_configured()
    usage_stats_cleanup_task: asyncio.Task | None = None
    deep_research_images_cleanup_task: asyncio.Task | None = None

    OUTPUTS_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    # Preload HTML templates into memory once at startup so UI requests don't
    # hit the disk per-page-view.
    preload_templates([
        STATIC_DIR / "login.html",
        STATIC_DIR / "usage-stats.html",
        STATIC_DIR / "rules-editor.html",
        STATIC_DIR / "web-playground.html",
        STATIC_DIR / "gateway-docs.html",
    ])

    # Initialize ConfigLoader and load configurations
    config_loader = ConfigLoader()
    config_loader.load_providers()
    config_loader.load_fallback_rules()
    config_loader.load_operation_rules()
    config_loader.validate_fallback_operation_consistency()
    app.state.config_loader = config_loader
    app.state.operation_rules = config_loader.operation_rules
    logger.info("Service configurations loaded and ConfigLoader attached to app.state.")

    # Initialize DB instances (schema init is sync, runs before event loop yields)
    tokens_usage_db = TokensUsageDB()
    fallback_events_db = FallbackEventsDB()

    # Create and start the shared WriteBatcher for INSERT operations.
    # Both DBs share the same underlying SQLite file (tokens_usage.db).
    batcher_db_path = resolve_write_batcher_db_path(tokens_usage_db)
    write_batcher = WriteBatcher(batcher_db_path)
    await write_batcher.start()
    for _db in (tokens_usage_db, fallback_events_db):
        if hasattr(_db, "set_batcher"):
            _db.set_batcher(write_batcher)

    app.state.tokens_usage_db = tokens_usage_db
    app.state.fallback_events_db = fallback_events_db
    app.state.write_batcher = write_batcher
    _chat_logging_module.set_tokens_usage_db(tokens_usage_db)

    # ApiKeysDB lives in a separate SQLite file (``api_keys.db``) from the
    # shared WriteBatcher (``tokens_usage.db``); binding the two would route
    # ``UPDATE api_keys …`` into the wrong database and silently drop every
    # spent_usd increment. ApiKeysDB falls back to direct synchronous writes
    # from worker threads when no batcher is bound.
    api_keys_db = ApiKeysDB()
    app.state.api_keys_db = api_keys_db
    _chat_logging_module.set_api_keys_db(api_keys_db)

    usd_budget_ledger = UsdBudgetLedger()
    app.state.usd_budget_ledger = usd_budget_ledger
    _chat_logging_module.set_usd_budget_ledger(usd_budget_ledger)

    app.state.active_requests_registry = ActiveRequestsRegistry()

    rate_limiter = RateLimiter()
    app.state.rate_limiter = rate_limiter
    _chat_logging_module.set_rate_limiter(rate_limiter)

    app.state.chat_model_failure_cooldowns = {}

    model_rotation_db = ModelRotationDB()
    app.state.model_rotation_db = model_rotation_db
    _chat_router_module.set_model_rotation_db(model_rotation_db)
    logger.info("TokensUsageDB, FallbackEventsDB, ModelRotationDB, ApiKeysDB, RateLimiter and WriteBatcher initialized.")

    http_client = create_shared_http_client()
    app.state.http_client = http_client
    logger.info("Shared httpx.AsyncClient initialized and attached to app.state.")

    proxy_http_clients = create_proxy_http_clients(config_loader.providers_config)
    app.state.proxy_http_clients = proxy_http_clients

    operation_dispatcher = OperationDispatcher(
        config_loader.providers_config,
        config_loader.operation_rules,
        http_client,
    )
    app.state.operation_dispatcher = operation_dispatcher
    logger.info("OperationDispatcher initialized and attached to app.state.")

    provider_models_service = ProviderModelsService()
    app.state.provider_models_service = provider_models_service
    logger.info("ProviderModelsService initialized and attached to app.state.")

    openrouter_scoring_http_client = proxy_http_clients.get("openrouter", http_client)
    openrouter_free_models_service = OpenRouterFreeModelsService()
    app.state.openrouter_free_models_service = openrouter_free_models_service
    await openrouter_free_models_service.start(
        providers_config=config_loader.providers_config,
        http_client=openrouter_scoring_http_client,
    )

    await run_startup_model_verification(
        mode=settings.verify_models_on_startup,
        providers_config=config_loader.providers_config,
        fallback_rules=config_loader.fallback_rules,
        operation_rules=config_loader.operation_rules,
        provider_models_service=provider_models_service,
        http_client=http_client,
    )

    usage_stats_cleanup_task = start_usage_stats_cleanup_task(tokens_usage_db, fallback_events_db)
    deep_research_images_cleanup_task = start_deep_research_images_cleanup_task(OUTPUTS_IMAGES_DIR)

    try:
        yield
    finally:
        logger.info("Application shutdown...")
        shutdown_config_error: RuntimeError | None = None
        try:
            ensure_gateway_api_key_configured()
        except RuntimeError as exc:
            shutdown_config_error = exc
        if usage_stats_cleanup_task is not None:
            usage_stats_cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await usage_stats_cleanup_task
        if deep_research_images_cleanup_task is not None:
            deep_research_images_cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await deep_research_images_cleanup_task
        openrouter_free_models_service = getattr(app.state, "openrouter_free_models_service", None)
        if openrouter_free_models_service is not None:
            await openrouter_free_models_service.stop()
        await write_batcher.stop()
        logger.info("WriteBatcher stopped — all pending writes flushed.")
        for name, proxy_client in proxy_http_clients.items():
            await proxy_client.aclose()
            logger.info("Proxy HTTP client for '%s' closed.", name)
        await http_client.aclose()
        logger.info("Shared httpx.AsyncClient closed.")
        if shutdown_config_error is not None:
            raise shutdown_config_error

# Create FastAPI app instance
STATIC_FILES_DIR = STATIC_DIR
STATIC_FILES_DIR.mkdir(parents=True, exist_ok=True) # Ensure static directory exists

app = FastAPI(
    title="LLMGateway",
    description="A gateway for routing LLM requests with fallback and rotation.",
    version="1.10",
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


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception during {request.method} {request.url.path}: {exc}", exc_info=True)
    return Response(
        content=f"Internal server error: {str(exc)}",
        status_code=500
    )

# 1. CORS Middleware (usually first)
configure_cors(app, settings.cors_allow_origins)

# 2. Request Logging Middleware
app.middleware("http")(log_middleware_functional)

# 3. Authentication Middleware
app.middleware("http")(api_key_auth)

# 3.5 Anthropic Version Header Middleware
@app.middleware("http")
async def add_anthropic_version_header(request: Request, call_next):
    response = await call_next(request)
    if "/v1/messages" in request.url.path or "/v1/models" in request.url.path or "anthropic-version" in request.headers:
        if "anthropic-version" not in response.headers:
            response.headers["anthropic-version"] = "2023-06-01"
    return response

# 4. Chat Completion Observability Middleware
logger.info("Chat usage tracking is ENABLED.")
if settings.log_chat_messages:
    logger.info("Chat message file logging is ENABLED.")
else:
    logger.info("Chat message file logging is DISABLED.")
app.middleware("http")(log_chat_completions)

# 5. GZip Middleware - added via add_middleware to be the outermost layer for responses
app.add_middleware(GZipMiddleware, minimum_size=1000)


# --- API Routers ---

# Include auth/login and root redirect routes
app.include_router(auth_router)
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
    StaticFiles(directory=str(OUTPUTS_IMAGES_DIR)),
    name="outputs-images",
)
logger.info(f"Deep research images mounted from {OUTPUTS_IMAGES_DIR}")

# --- Basic Health Check Endpoint ---
@app.get("/health", tags=["Health"])
async def health_check():
    """Basic health check endpoint."""
    return {"status": "ok"}

# --- Main Execution Block (for direct running) ---
if __name__ == "__main__":
    logger.info(f"Starting Uvicorn server on host {settings.gateway_host} port {settings.gateway_port}")
    uvicorn.run(
        "main:app", # Point to the app instance in this file
        host=settings.gateway_host,
        port=settings.gateway_port,
        reload=settings.debug_mode, # Enable reload only in debug mode
        log_level=settings.log_level.lower() # Pass the lowercase string name directly
    )
