import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
import tempfile
import json
from collections.abc import Mapping
from typing import Any, Literal
import httpx
from fastapi import APIRouter, Request, HTTPException, Body
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pathlib import Path
from pydantic import BaseModel, Field, ValidationError, field_validator
from threading import Lock

from ...config.loader import ConfigError, FusionModelConfig, ModelFallbackConfig, ModelsOperationConfig, resolve_provider_proxy
from ...services.fallback_model_evals import FallbackModelEvalAlreadyRunning
from ...services.openrouter_free_models import OpenRouterFreeModelsNotConfigured
from ...config.paths import PROJECT_ROOT, STATIC_DIR
from ...services.request_handler import OperationDispatcher, SUPPORTED_OPERATION_TYPES
from ...services.provider_models import ProviderModelsService
from ...services.provider_auth import resolve_provider_auth_headers
from ...utils.html_cache import get_template

editor_router = APIRouter()

# Helper to get paths from ConfigLoader
def _get_config_paths(request: Request):
    config_loader = _get_config_loader(request)
    return config_loader.providers_path, config_loader.fallback_rules_path, config_loader.operation_rules_path


def _get_model_rules_path(request: Request) -> Path:
    config_loader = _get_config_loader(request)
    return config_loader.model_rules_path


def _runtime_model_rules(config_loader) -> Mapping[str, Any]:
    model_rules = getattr(config_loader, "model_rules", None)
    return model_rules if isinstance(model_rules, Mapping) else {}

HTML_DIR = STATIC_DIR
FREE_TIER_PROVIDERS_DOC_PATH = PROJECT_ROOT / "examples" / "free-tier-providers.md"
MAX_COMMENT_BACKUPS = 10
_backup_timestamp_lock = Lock()
_last_backup_timestamp: datetime | None = None
DOCS_CAPABILITY_ORDER = (
    "chat",
    "embeddings",
    "rerank",
    "images",
    "audio_speech",
    "audio_transcription",
    "pdf_conversion",
    "web_search",
    "web_read",
    "web_research",
    "web_deep_research",
)
DOCS_CAPABILITY_SECTIONS = {
    "embeddings": "embeddings",
    "rerank": "rerank",
    "images_generations": "images",
    "images_edits": "images",
    "audio_speech": "audio_speech",
    "audio_transcriptions": "audio_transcription",
    "pdf_conversions": "pdf_conversion",
    "web_search": "web_search",
    "web_read": "web_read",
    "web_research": "web_research",
    "web_deep_research": "web_deep_research",
}
DOCS_IMAGE_OPERATION_SECTIONS = {
    "images_generations": "generation",
    "images_edits": "edit",
}
DOCS_WEB_OPERATION_SECTIONS = {
    "web_search": "search",
    "web_read": "read",
    "web_research": "research",
    "web_deep_research": "deep_research",
}


class StructuredRulesPayload(BaseModel):
    rules: list[ModelFallbackConfig] = Field(default_factory=list)


class StructuredProviderItem(BaseModel):
    name: str
    baseUrl: str
    apikey: str | None = None
    type: Literal["openai", "anthropic"] = "openai"
    proxy: str | None = None
    models: Any | None = None
    available_models: list[str] | None = None
    routing: dict[str, Any] | None = None
    upstream_key_pools: dict[str, Any] | None = None
    auth: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("'name' must not be empty.")
        return normalized_value

    @field_validator("proxy", mode="before")
    @classmethod
    def normalize_blank_proxy(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("apikey", mode="before")
    @classmethod
    def normalize_blank_apikey(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class StructuredProvidersPayload(BaseModel):
    providers: list[StructuredProviderItem] = Field(default_factory=list)


class StructuredFusionPayload(BaseModel):
    rules: list[FusionModelConfig] = Field(default_factory=list)


def _write_text_atomically(file_path: Path, content: str) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=file_path.parent, delete=False) as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)

        os.replace(temp_path, file_path)
    except Exception:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise


def _json5_has_comments(text: str) -> bool:
    """Best-effort detection of JSON5 comments (`//` or `/* */`).

    Deliberately simple: matches only the common cases that users actually write
    in configs. Doesn't attempt to respect strings containing `//` — false
    positives here only trigger a harmless backup, never data loss.
    """
    in_string = False
    escape = False
    prev_char = ""
    for ch in text:
        if escape:
            escape = False
            prev_char = ch
            continue
        if ch == "\\" and in_string:
            escape = True
            prev_char = ch
            continue
        if ch == '"':
            in_string = not in_string
            prev_char = ch
            continue
        if not in_string and prev_char == "/" and ch in ("/", "*"):
            return True
        prev_char = ch
    return False


def _backup_if_has_comments(file_path: Path) -> Path | None:
    """If the on-disk file contains JSON5 comments, copy it to a versioned backup.

    Structured save re-serializes the config via `json.dumps`, which cannot preserve
    JSON5 comments. Rather than silently dropping them, we snapshot the previous
    file so users can recover. Returns the backup path, or None if not created.
    """
    if not file_path.exists():
        return None
    try:
        existing_text = file_path.read_text(encoding="utf-8")
    except OSError:
        logging.exception("Failed to read %s to check for comments before structured save.", file_path)
        return None
    if not _json5_has_comments(existing_text):
        return None
    backup_path = _comment_backup_path(file_path)
    try:
        backup_path.write_text(existing_text, encoding="utf-8")
        _rotate_comment_backups(file_path)
        logging.info(
            "Structured save: detected JSON5 comments in %s; saved a backup to %s before overwrite.",
            file_path.name,
            backup_path.name,
        )
    except OSError:
        logging.exception("Failed to write backup %s before structured save.", backup_path)
        return None
    return backup_path


def _backup_timestamp_utc() -> str:
    global _last_backup_timestamp
    timestamp = datetime.now(timezone.utc)
    with _backup_timestamp_lock:
        if _last_backup_timestamp is not None and timestamp <= _last_backup_timestamp:
            timestamp = _last_backup_timestamp + timedelta(microseconds=1)
        _last_backup_timestamp = timestamp
    return timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _comment_backup_path(file_path: Path) -> Path:
    return file_path.with_name(f"{file_path.name}.bak.{_backup_timestamp_utc()}")


def _comment_backup_paths(file_path: Path) -> list[Path]:
    backup_prefix = f"{file_path.name}.bak."
    return sorted(path for path in file_path.parent.iterdir() if path.name.startswith(backup_prefix))


def _rotate_comment_backups(file_path: Path, max_backups: int = MAX_COMMENT_BACKUPS) -> None:
    backups = _comment_backup_paths(file_path)
    excess_count = len(backups) - max_backups
    if excess_count <= 0:
        return

    for old_backup in backups[:excess_count]:
        try:
            old_backup.unlink()
        except OSError:
            logging.exception("Failed to remove old JSON5 comments backup: %s", old_backup)


def _get_config_loader(request: Request):
    config_loader = getattr(request.app.state, "config_loader", None)
    if not config_loader:
        logging.error("ConfigLoader not found in application state.")
        raise HTTPException(status_code=500, detail="Internal server error: ConfigLoader not available.")
    return config_loader


def _get_provider_models_service(request: Request) -> ProviderModelsService:
    provider_models_service = getattr(request.app.state, "provider_models_service", None)
    if not provider_models_service:
        logging.error("ProviderModelsService not found in application state.")
        raise HTTPException(status_code=500, detail="Internal server error: ProviderModelsService not available.")
    return provider_models_service


def _get_shared_http_client(request: Request):
    http_client = getattr(request.app.state, "http_client", None)
    if http_client is None:
        logging.error("Shared HTTP client not found in application state.")
        raise HTTPException(status_code=500, detail="Internal server error: Shared HTTP client not available.")
    return http_client


def _serialize_structured_rules(rules: list[ModelFallbackConfig]) -> str:
    payload = [rule.model_dump(exclude_none=True) for rule in rules]
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _serialize_structured_fusion(rules: list[FusionModelConfig]) -> str:
    payload = [rule.model_dump(exclude_none=True) for rule in rules]
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _build_structured_fusion_response(config_loader) -> dict:
    rules: list[dict] = []
    for gateway_model_name, config in config_loader.fusion_rules.items():
        rule_payload = {"gateway_model_name": gateway_model_name, **config}
        rules.append(rule_payload)
    return {
        "rules": rules,
        "providers": list(config_loader.providers_config.keys()),
    }


def _serialize_structured_providers(providers: list[StructuredProviderItem]) -> str:
    payload: list[dict[str, dict[str, Any]]] = []
    for provider in providers:
        provider_payload: dict[str, Any] = {
            "baseUrl": provider.baseUrl,
        }
        if provider.apikey is not None:
            provider_payload["apikey"] = provider.apikey
        if provider.type != "openai":
            provider_payload["type"] = provider.type
        if provider.proxy is not None:
            provider_payload["proxy"] = provider.proxy
        if provider.models is not None:
            provider_payload["models"] = provider.models
        if provider.available_models is not None:
            provider_payload["available_models"] = provider.available_models
        if provider.routing is not None:
            provider_payload["routing"] = provider.routing
        if provider.upstream_key_pools is not None:
            provider_payload["upstream_key_pools"] = provider.upstream_key_pools
        if provider.auth is not None:
            provider_payload["auth"] = provider.auth
        payload.append({provider.name: provider_payload})

    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _build_structured_providers_response(config_loader) -> dict:
    providers: list[dict[str, Any]] = []
    for provider_name, provider_details in config_loader.providers_config.items():
        provider_payload = provider_details.model_dump(exclude_none=True)
        providers.append({
            "name": provider_name,
            **provider_payload,
        })
    return {"providers": providers}


def _build_structured_rules_response(config_loader) -> dict:
    rules: list[dict] = []
    fallback_rules = getattr(config_loader, "_fallback_rules_base", None) or config_loader.fallback_rules
    for gateway_model_name, config in fallback_rules.items():
        rule_payload = {
            "gateway_model_name": gateway_model_name,
            "fallback_models": config.get("fallback_models", []),
            "rotate_models": config.get("rotate_models", False),
            "dynamic_penalty": config.get("dynamic_penalty", False),
            "strip_think_tags": config.get("strip_think_tags", False),
            "compress_tool_results": config.get("compress_tool_results", False),
        }
        max_total_attempts = config.get("max_total_attempts")
        if max_total_attempts is not None:
            rule_payload["max_total_attempts"] = max_total_attempts
        context_overflow_fallback = config.get("context_overflow_fallback")
        if context_overflow_fallback is not None:
            rule_payload["context_overflow_fallback"] = context_overflow_fallback
        rules.append(rule_payload)

    return {
        "rules": rules,
        "providers": list(config_loader.providers_config.keys()),
    }


def _set_fallback_rules_and_reapply_model_rules(config_loader, validated_rules: dict[str, Any]) -> None:
    config_loader._fallback_rules_base = validated_rules
    config_loader.fallback_rules = validated_rules
    if config_loader.model_rules_path.exists():
        config_loader.load_model_rules()


def _validate_existing_model_rules_against_fallback_rules(
    config_loader,
    validated_rules: dict[str, Any],
) -> None:
    model_rules_path = getattr(config_loader, "model_rules_path", None)
    if model_rules_path is None or not model_rules_path.exists():
        return
    model_rules_text = model_rules_path.read_text(encoding="utf-8")
    config_loader.parse_and_validate_model_rules_payload(
        model_rules_text,
        providers_config=config_loader.providers_config,
        fallback_rules=validated_rules,
    )


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _sort_docs_values(values: list[str], known_order: tuple[str, ...]) -> list[str]:
    value_set = set(values)
    ordered_values = [value for value in known_order if value in value_set]
    ordered_values.extend(sorted(value for value in value_set if value not in known_order))
    return ordered_values


def _ensure_docs_model(catalog: dict[str, dict], model_name: str) -> dict:
    return catalog.setdefault(model_name, {"id": model_name, "capabilities": []})


def _add_docs_capability(catalog: dict[str, dict], model_name: str, capability: str) -> None:
    model_entry = _ensure_docs_model(catalog, model_name)
    _append_unique(model_entry["capabilities"], capability)


def _build_gateway_docs_catalog(config_loader, allowed_models: set[str] | None = None) -> dict:
    catalog: dict[str, dict] = {}
    operation_rules = config_loader.operation_rules or {}

    for model_name in config_loader.fallback_rules.keys():
        _add_docs_capability(catalog, model_name, "chat")

    for section, capability in DOCS_CAPABILITY_SECTIONS.items():
        for model_name in (operation_rules.get(section) or {}).keys():
            _add_docs_capability(catalog, model_name, capability)

    for section, operation in DOCS_IMAGE_OPERATION_SECTIONS.items():
        for model_name in (operation_rules.get(section) or {}).keys():
            model_entry = _ensure_docs_model(catalog, model_name)
            _append_unique(model_entry.setdefault("image_operations", []), operation)

    for section, operation in DOCS_WEB_OPERATION_SECTIONS.items():
        for model_name in (operation_rules.get(section) or {}).keys():
            model_entry = _ensure_docs_model(catalog, model_name)
            _append_unique(model_entry.setdefault("web_operations", []), operation)

    if allowed_models:
        catalog = {model_name: entry for model_name, entry in catalog.items() if model_name in allowed_models}

    models: list[dict] = []
    groups = {capability: [] for capability in DOCS_CAPABILITY_ORDER}
    for model_entry in catalog.values():
        model_entry["capabilities"] = _sort_docs_values(model_entry["capabilities"], DOCS_CAPABILITY_ORDER)
        if "image_operations" in model_entry:
            model_entry["image_operations"] = _sort_docs_values(model_entry["image_operations"], ("generation", "edit"))
        if "web_operations" in model_entry:
            model_entry["web_operations"] = _sort_docs_values(
                model_entry["web_operations"],
                ("search", "read", "research", "deep_research"),
            )
        models.append(model_entry)
        for capability in model_entry["capabilities"]:
            groups.setdefault(capability, []).append(model_entry["id"])

    for capability, names in groups.items():
        groups[capability] = sorted(names)

    return {"models": sorted(models, key=lambda item: item["id"]), "groups": groups}


def _serialize_structured_operation_rules(
    payload: ModelsOperationConfig,
    *,
    include_audio_speech: bool = False,
    include_audio_transcriptions: bool = False,
    include_pdf_conversions: bool = False,
    include_web_sections: bool = False,
) -> str:
    payload_dump = payload.model_dump(exclude_none=True)
    serialized_payload = {
        "embeddings": payload_dump["embeddings"],
        "rerank": payload_dump["rerank"],
        "images_generations": payload_dump["images_generations"],
        "images_edits": payload_dump["images_edits"],
    }
    if include_audio_speech or payload.audio_speech:
        serialized_payload["audio_speech"] = payload_dump["audio_speech"]
    if include_audio_transcriptions or payload.audio_transcriptions:
        serialized_payload["audio_transcriptions"] = payload_dump["audio_transcriptions"]
    if include_pdf_conversions or payload.pdf_conversions:
        serialized_payload["pdf_conversions"] = payload_dump["pdf_conversions"]
    if include_web_sections or payload.web_search:
        serialized_payload["web_search"] = payload_dump["web_search"]
    if include_web_sections or payload.web_read:
        serialized_payload["web_read"] = payload_dump["web_read"]
    if include_web_sections or payload.web_research:
        serialized_payload["web_research"] = payload_dump["web_research"]
    if include_web_sections or payload.web_deep_research:
        serialized_payload["web_deep_research"] = payload_dump["web_deep_research"]
    return json.dumps(serialized_payload, ensure_ascii=False, indent=2) + "\n"


def _build_structured_operation_rules_response(config_loader) -> dict:
    response_payload = {
        "embeddings": [
            {
                "gateway_model_name": gateway_model_name,
                "routes": config.get("routes", []),
            }
            for gateway_model_name, config in config_loader.operation_rules.get("embeddings", {}).items()
        ],
        "rerank": [
            {
                "gateway_model_name": gateway_model_name,
                "routes": config.get("routes", []),
            }
            for gateway_model_name, config in config_loader.operation_rules.get("rerank", {}).items()
        ],
        "images_generations": [
            {
                "gateway_model_name": gateway_model_name,
                "routes": config.get("routes", []),
            }
            for gateway_model_name, config in config_loader.operation_rules.get("images_generations", {}).items()
        ],
        "images_edits": [
            {
                "gateway_model_name": gateway_model_name,
                "routes": config.get("routes", []),
            }
            for gateway_model_name, config in config_loader.operation_rules.get("images_edits", {}).items()
        ],
    }

    audio_transcriptions = [
        {
            "gateway_model_name": gateway_model_name,
            "routes": config.get("routes", []),
        }
        for gateway_model_name, config in config_loader.operation_rules.get("audio_transcriptions", {}).items()
    ]
    if audio_transcriptions:
        response_payload["audio_transcriptions"] = audio_transcriptions

    for section_name in ("audio_speech", "pdf_conversions"):
        section_rules = [
            {
                "gateway_model_name": gateway_model_name,
                "routes": config.get("routes", []),
            }
            for gateway_model_name, config in config_loader.operation_rules.get(section_name, {}).items()
        ]
        if section_rules:
            response_payload[section_name] = section_rules

    for section_name in ("web_search", "web_read"):
        section_rules = [
            {
                "gateway_model_name": gateway_model_name,
                **{key: value for key, value in config.items() if key != "routes"},
            }
            for gateway_model_name, config in config_loader.operation_rules.get(section_name, {}).items()
        ]
        if section_rules:
            response_payload[section_name] = section_rules

    for section_name in ("web_research", "web_deep_research"):
        section_rules = [
            {
                "gateway_model_name": gateway_model_name,
                **{key: value for key, value in config.items() if key != "routes"},
                "routes": config.get("routes", []),
            }
            for gateway_model_name, config in config_loader.operation_rules.get(section_name, {}).items()
        ]
        if section_rules:
            response_payload[section_name] = section_rules

    return response_payload


def _build_operation_rules_validation_response(message: str, errors: list[dict]) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "detail": {
                "message": message,
                "errors": jsonable_encoder(errors),
            }
        },
    )


def _refresh_operation_runtime_state(request: Request, config_loader) -> None:
    request.app.state.operation_rules = config_loader.operation_rules
    http_client = _get_shared_http_client(request)
    request.app.state.operation_dispatcher = OperationDispatcher(
        config_loader.providers_config,
        config_loader.operation_rules,
        http_client,
        model_rules=_runtime_model_rules(config_loader),
    )


def validate_operation_rules_mapping(providers_config: dict, operation_rules: dict) -> None:
    provider_names = set(providers_config.keys())
    errors: list[str] = []
    section_names = sorted(set(SUPPORTED_OPERATION_TYPES) | set(operation_rules.keys()))

    for section_name in section_names:
        section_routes = operation_rules.get(section_name) or {}
        if not isinstance(section_routes, dict):
            continue

        for gateway_model_name, config in section_routes.items():
            routes = config.get("routes", []) if isinstance(config, dict) else []
            for route_index, route in enumerate(routes):
                if not isinstance(route, dict):
                    continue
                provider_name = route.get("provider")
                if provider_name and provider_name in provider_names:
                    continue
                errors.append(
                    f"Invalid provider '{provider_name}' used in operation route "
                    f"{section_name}.{gateway_model_name}.routes[{route_index}]. "
                    "Provider not found in providers configuration."
                )

    if errors:
        raise ValueError("Invalid operation rules provider references: " + "; ".join(errors))


def _build_proxy_http_clients(providers_config: dict) -> dict[str, httpx.AsyncClient]:
    """Create dedicated httpx clients for providers that declare a proxy.

    Mirrors main.create_proxy_http_clients but is local to avoid circular import.
    """
    from main import _default_timeout, _default_pool_limits  # local import to avoid cycles at import time

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
    return clients


async def _close_proxy_clients(clients: dict[str, httpx.AsyncClient]) -> None:
    for provider_name, client in clients.items():
        try:
            await client.aclose()
        except Exception:
            logging.exception("Failed to close proxy HTTP client for '%s' after reload.", provider_name)


def _refresh_providers_runtime_state(request: Request, config_loader) -> None:
    """After providers_config is reloaded: rebuild proxy clients and OperationDispatcher.

    In-flight requests that captured the old references remain safe; new requests
    will see the refreshed state. Old proxy clients are closed in the background
    so the endpoint response is not blocked.
    """
    old_proxy_clients = dict(getattr(request.app.state, "proxy_http_clients", {}) or {})
    new_proxy_clients = _build_proxy_http_clients(config_loader.providers_config)
    request.app.state.proxy_http_clients = new_proxy_clients

    shared_http_client = _get_shared_http_client(request)
    request.app.state.operation_dispatcher = OperationDispatcher(
        config_loader.providers_config,
        config_loader.operation_rules,
        shared_http_client,
        model_rules=_runtime_model_rules(config_loader),
    )

    if old_proxy_clients:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_close_proxy_clients(old_proxy_clients))
        except RuntimeError:
            logging.warning("No running event loop; skipping async close of old proxy clients.")


async def _validate_provider_models(
    request: Request,
    config_loader,
    fallback_rules: dict,
) -> None:
    provider_models_service = _get_provider_models_service(request)
    http_client = _get_shared_http_client(request)

    models_by_provider: dict[str, set[str]] = {}
    for config in fallback_rules.values():
        for fallback_model in _iter_rule_targets(config):
            provider_name = fallback_model["provider"]
            if provider_name in models_by_provider:
                continue

            provider_config = config_loader.providers_config.get(provider_name)
            if not provider_config:
                raise HTTPException(
                    status_code=400,
                    detail=f"Provider '{provider_name}' is not defined in providers configuration.",
                )

            try:
                auth_headers = await resolve_provider_auth_headers(
                    request,
                    provider_name=provider_name,
                    provider_config=provider_config,
                )
                provider_models = await provider_models_service.get_models(
                    provider_name,
                    provider_config,
                    http_client,
                    auth_headers=auth_headers,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            models_by_provider[provider_name] = set(provider_models)

    for gateway_model_name, config in fallback_rules.items():
        for fallback_model in _iter_rule_targets(config):
            provider_name = fallback_model["provider"]
            model_name = fallback_model["model"]
            if model_name not in models_by_provider.get(provider_name, set()):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Model '{model_name}' is not available for provider '{provider_name}' "
                        f"and cannot be used in fallback rule '{gateway_model_name}'."
                    ),
                )


def _iter_rule_targets(config: dict) -> list[dict]:
    targets = list(config.get("fallback_models", []))
    context_overflow_fallback = config.get("context_overflow_fallback")
    if context_overflow_fallback:
        targets.append(context_overflow_fallback)
    return targets

# The router itself will be included with a prefix like /v1 or /admin in main.py
@editor_router.get("/ui/rules-editor", response_class=HTMLResponse, tags=["Config Editor UI"])
async def get_editor_page(request: Request):
    """Serves the HTML page for the configuration editor."""
    editor_html_path = HTML_DIR / "rules-editor.html"
    if not editor_html_path.exists():
        logging.error(f"Editor HTML file not found at {editor_html_path}")
        raise HTTPException(status_code=404, detail="Editor page not found.")
    try:
        return HTMLResponse(content=await get_template(editor_html_path))
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error reading editor HTML file: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load editor page.")


async def _serve_playground_page() -> HTMLResponse:
    playground_html_path = HTML_DIR / "web-playground.html"
    if not playground_html_path.exists():
        logging.error("Playground HTML file not found at %s", playground_html_path)
        raise HTTPException(status_code=404, detail="Playground page not found.")
    try:
        return HTMLResponse(content=await get_template(playground_html_path))
    except HTTPException:
        raise
    except Exception as e:
        logging.error("Error reading playground HTML file: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load playground page.")


@editor_router.get("/ui/playground", response_class=HTMLResponse, tags=["Config Editor UI"])
async def get_playground_page(request: Request):
    """Serves the admin playground page for testing operation endpoints."""
    return await _serve_playground_page()


@editor_router.get("/ui/web-playground", response_class=HTMLResponse, tags=["Config Editor UI"])
async def get_web_playground_page(request: Request):
    """Serves the legacy playground URL."""
    return await _serve_playground_page()


@editor_router.get("/ui/docs", response_class=HTMLResponse, tags=["Config Editor UI"])
async def get_gateway_docs_page(request: Request):
    """Serves the gateway integration documentation page."""
    docs_html_path = HTML_DIR / "gateway-docs.html"
    if not docs_html_path.exists():
        logging.error("Gateway docs HTML file not found at %s", docs_html_path)
        raise HTTPException(status_code=404, detail="Gateway docs page not found.")
    try:
        return HTMLResponse(content=await get_template(docs_html_path))
    except HTTPException:
        raise
    except Exception as e:
        logging.error("Error reading gateway docs HTML file: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load gateway docs page.")


@editor_router.get("/ui/docs/models", tags=["Config Editor API"])
async def get_gateway_docs_models(request: Request):
    """Returns gateway model names grouped by configured capabilities for the docs page."""
    config_loader = _get_config_loader(request)
    api_key_record = getattr(request.state, "api_key_record", None)
    allowed_models = set(api_key_record.allowed_models) if api_key_record and api_key_record.allowed_models else None
    return JSONResponse(content=_build_gateway_docs_catalog(config_loader, allowed_models))


@editor_router.get("/ui/docs/free-tier-providers.md", response_class=PlainTextResponse, tags=["Config Editor API"])
async def get_free_tier_providers_doc():
    """Returns the free-tier provider catalog Markdown for client-side rendering."""
    if not FREE_TIER_PROVIDERS_DOC_PATH.exists():
        logging.error("Free-tier provider catalog not found at %s", FREE_TIER_PROVIDERS_DOC_PATH)
        raise HTTPException(status_code=404, detail="Free-tier provider catalog not found.")
    try:
        return PlainTextResponse(
            FREE_TIER_PROVIDERS_DOC_PATH.read_text(encoding="utf-8"),
            media_type="text/plain; charset=utf-8",
        )
    except OSError as exc:
        logging.error("Error reading free-tier provider catalog: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load free-tier provider catalog.")


def _build_playground_models(config_loader) -> dict[str, list[str]]:
    rules = config_loader.operation_rules or {}
    fallback_rules = config_loader.fallback_rules or {}
    fusion_rules = getattr(config_loader, "fusion_rules", None) or {}

    def _names(section: str) -> list[str]:
        return sorted((rules.get(section) or {}).keys())

    # Fusion ensemble models are callable as regular chat models, so they belong
    # in the chat selector. ``fusion`` is also returned separately so the UI can
    # mark them and enable Fusion-specific controls.
    return {
        "chat": sorted(set(fallback_rules.keys()) | set(fusion_rules.keys())),
        "fusion": sorted(fusion_rules.keys()),
        "web_search": _names("web_search"),
        "web_read": _names("web_read"),
        "web_research": _names("web_research"),
        "web_deep_research": _names("web_deep_research"),
        "audio_speech": _names("audio_speech"),
        "audio_transcriptions": _names("audio_transcriptions"),
        "images_generations": _names("images_generations"),
        "images_edits": _names("images_edits"),
        "pdf_conversions": _names("pdf_conversions"),
    }


@editor_router.get("/ui/playground/models", tags=["Config Editor API"])
async def get_playground_models(request: Request):
    """Returns lists of gateway models configured for playground operations.

    Lets the playground UI populate model selectors without duplicating the
    operation-config schema on the frontend.
    """
    config_loader = _get_config_loader(request)
    return JSONResponse(content=_build_playground_models(config_loader))


@editor_router.get("/ui/web-playground/models", tags=["Config Editor API"])
async def get_web_playground_models(request: Request):
    """Returns playground model lists for the legacy web-playground URL."""
    config_loader = _get_config_loader(request)
    return JSONResponse(content=_build_playground_models(config_loader))


@editor_router.get("/config/models-rules", response_class=PlainTextResponse, tags=["Config Editor API"])
async def get_models_rules_text(request: Request):
    """Fetches the current raw text content of models_fallback_rules.json."""
    _, fallback_rules_path, _ = _get_config_paths(request)
    if not fallback_rules_path.exists():
        logging.error(f"Configuration file {fallback_rules_path.name} not found.")
        raise HTTPException(status_code=404, detail=f"{fallback_rules_path.name} not found.")
    try:
        with open(fallback_rules_path, "r", encoding="utf-8") as f:
            content = f.read()
        return PlainTextResponse(content=content)
    except Exception as e:
        logging.error(f"Error reading {fallback_rules_path.name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not read {fallback_rules_path.name}.")

# If router is included with prefix /v1, this becomes /v1/config/models-rules
@editor_router.post("/config/models-rules", tags=["Config Editor API"])
async def save_models_rules(request: Request, payload_text: str = Body(..., media_type="text/plain")):
    """
    Validates and saves the updated models_fallback_rules.json.
    Triggers a configuration reload on success.
    """
    config_loader = _get_config_loader(request)
    _, fallback_rules_path, _ = _get_config_paths(request)

    try:
        validated_rules = config_loader.parse_and_validate_fallback_rules_payload(
            payload_text,
            providers_config=config_loader.providers_config,
        )
        _validate_existing_model_rules_against_fallback_rules(config_loader, validated_rules)
    except ValidationError as ve:
        logging.error(f"Validation error saving {fallback_rules_path.name}: {ve.errors()}", exc_info=False)
        return JSONResponse(status_code=400, content={"detail": "Validation Error", "errors": ve.errors()})
    except ValueError as e:
        logging.error(f"Validation error saving {fallback_rules_path.name}: {e}", exc_info=False)
        raise HTTPException(status_code=400, detail=str(e))

    try:
        _write_text_atomically(fallback_rules_path, payload_text)
        _set_fallback_rules_and_reapply_model_rules(config_loader, validated_rules)
        logging.info(f"Successfully saved validated configuration to {fallback_rules_path.name}.")
        return {"message": f"{fallback_rules_path.name} updated successfully."}
    except Exception as e:
        logging.error(f"Error saving {fallback_rules_path.name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not save {fallback_rules_path.name}.")


@editor_router.get("/config/model-rules", response_class=PlainTextResponse, tags=["Config Editor API"])
async def get_model_rules_text(request: Request):
    model_rules_path = _get_model_rules_path(request)
    if not model_rules_path.exists():
        return PlainTextResponse(content="{\n}\n")
    try:
        return PlainTextResponse(content=model_rules_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.error(f"Error reading {model_rules_path.name}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not read {model_rules_path.name}.")


@editor_router.post("/config/model-rules", tags=["Config Editor API"])
async def save_model_rules(request: Request, payload_text: str = Body(..., media_type="text/plain")):
    config_loader = _get_config_loader(request)
    model_rules_path = _get_model_rules_path(request)
    base_fallback_rules = getattr(config_loader, "_fallback_rules_base", None) or config_loader.fallback_rules

    try:
        config_loader.parse_and_validate_model_rules_payload(
            payload_text,
            providers_config=config_loader.providers_config,
            fallback_rules=base_fallback_rules,
        )
    except ValidationError as ve:
        logging.error(f"Validation error saving {model_rules_path.name}: {ve.errors()}", exc_info=False)
        return JSONResponse(status_code=400, content={"detail": "Validation Error", "errors": ve.errors()})
    except ValueError as exc:
        logging.error(f"Validation error saving {model_rules_path.name}: {exc}", exc_info=False)
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        model_rules_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = _backup_if_has_comments(model_rules_path)
        _write_text_atomically(model_rules_path, payload_text)
        config_loader.load_model_rules()
        shared_http_client = _get_shared_http_client(request)
        request.app.state.operation_dispatcher = OperationDispatcher(
            config_loader.providers_config,
            config_loader.operation_rules,
            shared_http_client,
            model_rules=_runtime_model_rules(config_loader),
        )
        response_body = {"message": f"{model_rules_path.name} updated successfully."}
        if backup_path is not None:
            response_body["comments_backup"] = backup_path.name
        return response_body
    except Exception as exc:
        logging.error(f"Error saving {model_rules_path.name}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not save {model_rules_path.name}.")


@editor_router.get("/config/models-rules/structured", tags=["Config Editor API"])
async def get_models_rules_structured(request: Request):
    config_loader = _get_config_loader(request)
    return _build_structured_rules_response(config_loader)


@editor_router.post("/config/models-rules/structured", tags=["Config Editor API"])
async def save_models_rules_structured(request: Request, payload: StructuredRulesPayload):
    config_loader = _get_config_loader(request)
    _, fallback_rules_path, _ = _get_config_paths(request)
    payload_text = _serialize_structured_rules(payload.rules)

    try:
        validated_rules = config_loader.parse_and_validate_fallback_rules_payload(
            payload_text,
            providers_config=config_loader.providers_config,
        )
        await _validate_provider_models(request, config_loader, validated_rules)
        _validate_existing_model_rules_against_fallback_rules(config_loader, validated_rules)
    except ValidationError as ve:
        logging.error(f"Validation error saving {fallback_rules_path.name}: {ve.errors()}", exc_info=False)
        return JSONResponse(status_code=400, content={"detail": "Validation Error", "errors": ve.errors()})
    except HTTPException:
        raise
    except ValueError as exc:
        logging.error(f"Validation error saving {fallback_rules_path.name}: {exc}", exc_info=False)
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        backup_path = _backup_if_has_comments(fallback_rules_path)
        _write_text_atomically(fallback_rules_path, payload_text)
        _set_fallback_rules_and_reapply_model_rules(config_loader, validated_rules)
        logging.info(f"Successfully saved structured fallback rules to {fallback_rules_path.name}.")
        response_body = {
            "message": f"{fallback_rules_path.name} updated successfully.",
            "rules": _build_structured_rules_response(config_loader)["rules"],
        }
        if backup_path is not None:
            response_body["comments_backup"] = backup_path.name
        return response_body
    except Exception as exc:
        logging.error(f"Error saving {fallback_rules_path.name}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not save {fallback_rules_path.name}.")


@editor_router.get("/config/fusion-rules/structured", tags=["Config Editor API"])
async def get_fusion_rules_structured(request: Request):
    config_loader = _get_config_loader(request)
    return _build_structured_fusion_response(config_loader)


@editor_router.post("/config/fusion-rules/structured", tags=["Config Editor API"])
async def save_fusion_rules_structured(request: Request, payload: StructuredFusionPayload):
    config_loader = _get_config_loader(request)
    fusion_rules_path = config_loader.fusion_rules_path
    payload_text = _serialize_structured_fusion(payload.rules)

    try:
        validated_rules = config_loader.parse_and_validate_fusion_rules_payload(
            payload_text,
            providers_config=config_loader.providers_config,
        )
    except ValidationError as ve:
        logging.error(f"Validation error saving {fusion_rules_path.name}: {ve.errors()}", exc_info=False)
        return JSONResponse(status_code=400, content={"detail": "Validation Error", "errors": ve.errors()})
    except HTTPException:
        raise
    except ValueError as exc:
        logging.error(f"Validation error saving {fusion_rules_path.name}: {exc}", exc_info=False)
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        backup_path = _backup_if_has_comments(fusion_rules_path)
        _write_text_atomically(fusion_rules_path, payload_text)
        config_loader.fusion_rules = validated_rules
        logging.info(f"Successfully saved structured fusion rules to {fusion_rules_path.name}.")
        response_body = {
            "message": f"{fusion_rules_path.name} updated successfully.",
            "rules": _build_structured_fusion_response(config_loader)["rules"],
        }
        if backup_path is not None:
            response_body["comments_backup"] = backup_path.name
        return response_body
    except Exception as exc:
        logging.error(f"Error saving {fusion_rules_path.name}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not save {fusion_rules_path.name}.")


@editor_router.get("/config/model-operations/structured", tags=["Config Editor API"])
async def get_operation_rules_structured(request: Request):
    config_loader = _get_config_loader(request)
    return _build_structured_operation_rules_response(config_loader)


@editor_router.post("/config/model-operations/structured", tags=["Config Editor API"])
async def save_operation_rules_structured(request: Request, payload: dict = Body(...)):
    config_loader = _get_config_loader(request)
    _, _, operation_rules_path = _get_config_paths(request)

    try:
        structured_payload = ModelsOperationConfig.model_validate(payload)
        payload_text = _serialize_structured_operation_rules(
            structured_payload,
            include_audio_speech=(
                "audio_speech" in payload or bool(structured_payload.audio_speech)
            ),
            include_audio_transcriptions=(
                "audio_transcriptions" in payload or bool(structured_payload.audio_transcriptions)
            ),
            include_pdf_conversions=(
                "pdf_conversions" in payload or bool(structured_payload.pdf_conversions)
            ),
            include_web_sections=(
                "web_search" in payload
                or "web_read" in payload
                or "web_research" in payload
                or "web_deep_research" in payload
            ),
        )
        config_loader.parse_and_validate_operation_routes_payload(
            payload_text,
            providers_config=config_loader.providers_config,
            fallback_rules=config_loader.fallback_rules,
        )
    except ValidationError as exc:
        logging.error(
            "Validation error saving %s: %s",
            operation_rules_path.name,
            exc.errors(),
            exc_info=False,
        )
        return _build_operation_rules_validation_response("Validation Error", exc.errors())
    except ValueError as exc:
        logging.error(f"Validation error saving {operation_rules_path.name}: {exc}", exc_info=False)
        return _build_operation_rules_validation_response(str(exc), [])

    try:
        backup_path = _backup_if_has_comments(operation_rules_path)
        _write_text_atomically(operation_rules_path, payload_text)
        if not config_loader.reload_operation_rules():
            raise HTTPException(status_code=500, detail=f"Could not reload {operation_rules_path.name}.")

        _refresh_operation_runtime_state(request, config_loader)
        logging.info(f"Successfully saved structured operation rules to {operation_rules_path.name}.")
        response_body = {
            "message": f"{operation_rules_path.name} updated successfully.",
            **_build_structured_operation_rules_response(config_loader),
        }
        if backup_path is not None:
            response_body["comments_backup"] = backup_path.name
        return response_body
    except HTTPException:
        raise
    except Exception as exc:
        logging.error(f"Error saving {operation_rules_path.name}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not save {operation_rules_path.name}.")


# --- Endpoints for providers.json ---

@editor_router.get("/config/providers", response_class=PlainTextResponse, tags=["Config Editor API"])
async def get_providers_text(request: Request):
    """Fetches the current raw text content of providers.json."""
    providers_path, _, _ = _get_config_paths(request)
    if not providers_path.exists():
        logging.error(f"Configuration file {providers_path.name} not found.")
        raise HTTPException(status_code=404, detail=f"{providers_path.name} not found.")
    try:
        with open(providers_path, "r", encoding="utf-8") as f:
            content = f.read()
        return PlainTextResponse(content=content)
    except Exception as e:
        logging.error(f"Error reading {providers_path.name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not read {providers_path.name}.")


@editor_router.get("/config/providers/structured", tags=["Config Editor API"])
async def get_providers_structured(request: Request):
    config_loader = _get_config_loader(request)
    return _build_structured_providers_response(config_loader)


@editor_router.post("/config/providers/structured", tags=["Config Editor API"])
async def save_providers_structured(request: Request, payload: StructuredProvidersPayload):
    config_loader = _get_config_loader(request)
    providers_path, _, _ = _get_config_paths(request)
    payload_text = _serialize_structured_providers(payload.providers)

    try:
        validated_providers = config_loader.parse_and_validate_providers_payload(
            payload_text,
            strict_env=True,
        )
        config_loader.validate_fallback_rules_mapping(
            config_loader.fallback_rules,
            providers_config=validated_providers,
        )
        validate_operation_rules_mapping(
            validated_providers,
            config_loader.operation_rules,
        )
    except ValidationError as ve:
        errors = ve.errors(include_context=False)
        logging.error(f"Validation error saving {providers_path.name}: {errors}", exc_info=False)
        return JSONResponse(status_code=400, content={"detail": "Validation Error", "errors": errors})
    except ValueError as e:
        logging.error(f"Validation error saving {providers_path.name}: {e}", exc_info=False)
        raise HTTPException(status_code=400, detail=str(e))
    except ConfigError as e:
        logging.error(f"Semantic validation error saving {providers_path.name}: {e}", exc_info=False)
        raise HTTPException(status_code=400, detail=str(e)) from e

    try:
        backup_path = _backup_if_has_comments(providers_path)
        _write_text_atomically(providers_path, payload_text)
        config_loader.providers_config = validated_providers
        provider_models_service = getattr(request.app.state, "provider_models_service", None)
        if provider_models_service:
            provider_models_service.clear()
        _refresh_providers_runtime_state(request, config_loader)
        logging.info(f"Successfully saved structured providers configuration to {providers_path.name}.")
        response_body = {
            "message": f"{providers_path.name} updated successfully.",
            **_build_structured_providers_response(config_loader),
        }
        if backup_path is not None:
            response_body["comments_backup"] = backup_path.name
        return response_body
    except Exception as e:
        logging.error(f"Error saving {providers_path.name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not save {providers_path.name}.")


@editor_router.post("/config/providers", tags=["Config Editor API"])
@editor_router.post("/ui/providers-config", tags=["Config Editor API"])
async def save_providers_config(request: Request, payload_text: str = Body(..., media_type="text/plain")):
    """
    Validates and saves the updated providers.json.
    Triggers a providers configuration reload on success.
    """
    config_loader = _get_config_loader(request)
    providers_path, _, _ = _get_config_paths(request)

    try:
        validated_providers = config_loader.parse_and_validate_providers_payload(
            payload_text,
            strict_env=True,
        )
        config_loader.validate_fallback_rules_mapping(
            config_loader.fallback_rules,
            providers_config=validated_providers,
        )
        validate_operation_rules_mapping(
            validated_providers,
            config_loader.operation_rules,
        )
    except ValidationError as ve:
        logging.error(f"Validation error saving {providers_path.name}: {ve.errors()}", exc_info=False)
        return JSONResponse(status_code=400, content={"detail": "Validation Error", "errors": ve.errors()})
    except ValueError as e:
        logging.error(f"Validation error saving {providers_path.name}: {e}", exc_info=False)
        raise HTTPException(status_code=400, detail=str(e))
    except ConfigError as e:
        logging.error(f"Semantic validation error saving {providers_path.name}: {e}", exc_info=False)
        raise HTTPException(status_code=400, detail=str(e)) from e

    try:
        _write_text_atomically(providers_path, payload_text)
        config_loader.providers_config = validated_providers
        provider_models_service = getattr(request.app.state, "provider_models_service", None)
        if provider_models_service:
            provider_models_service.clear()
        _refresh_providers_runtime_state(request, config_loader)
        logging.info(f"Successfully saved validated providers configuration to {providers_path.name}.")
        return {"message": f"{providers_path.name} updated successfully."}
    except Exception as e:
        logging.error(f"Error saving {providers_path.name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not save {providers_path.name}.")


@editor_router.get("/config/providers/{provider_name}/models", tags=["Config Editor API"])
async def get_provider_models(request: Request, provider_name: str):
    config_loader = _get_config_loader(request)
    provider_config = config_loader.providers_config.get(provider_name)
    if not provider_config:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found.")

    provider_models_service = _get_provider_models_service(request)
    proxy_http_clients = getattr(request.app.state, "proxy_http_clients", {})
    http_client = proxy_http_clients.get(provider_name, _get_shared_http_client(request))
    try:
        auth_headers = await resolve_provider_auth_headers(
            request,
            provider_name=provider_name,
            provider_config=provider_config,
        )
        models = await provider_models_service.get_models(
            provider_name,
            provider_config,
            http_client,
            auth_headers=auth_headers,
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "provider": provider_name,
        "models": [{"id": model_id} for model_id in models],
        "cache_ttl_seconds": provider_models_service.ttl_seconds,
    }


@editor_router.get("/openrouter/free-models", tags=["Config Editor API"])
async def get_openrouter_free_models_status(request: Request):
    service = getattr(request.app.state, "openrouter_free_models_service", None)
    if service is None:
        return {
            "configured": False,
            "running": False,
            "provider": "openrouter",
            "intervalSeconds": 8 * 60 * 60,
            "lastCheckedAt": None,
            "nextRefreshAt": None,
            "lastError": None,
            "manualRefreshRunning": False,
            "snapshot": None,
        }
    return await service.get_status()


@editor_router.post("/openrouter/free-models/run", tags=["Config Editor API"])
async def start_openrouter_free_models_refresh(request: Request):
    service = getattr(request.app.state, "openrouter_free_models_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="OpenRouter free model scoring service is not initialized.")
    try:
        started = await service.start_manual_full_refresh()
    except OpenRouterFreeModelsNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not started:
        raise HTTPException(status_code=409, detail="OpenRouter free model refresh is already running.")
    return await service.get_status()


@editor_router.get("/fallback-model-evals", tags=["Config Editor API"])
async def get_fallback_model_eval_status(request: Request):
    service = getattr(request.app.state, "fallback_model_eval_service", None)
    if service is None:
        return {
            "configured": False,
            "running": False,
            "lastCheckedAt": None,
            "lastError": "Fallback model eval service is not initialized.",
            "snapshot": None,
        }
    return await service.get_status()


@editor_router.post("/fallback-model-evals/run", tags=["Config Editor API"])
async def start_fallback_model_eval(request: Request):
    service = getattr(request.app.state, "fallback_model_eval_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Fallback model eval service is not initialized.")

    config_loader = _get_config_loader(request)
    try:
        await service.start_eval(
            providers_config=config_loader.providers_config,
            fallback_rules=config_loader.fallback_rules,
            http_client=_get_shared_http_client(request),
            proxy_http_clients=getattr(request.app.state, "proxy_http_clients", {}),
        )
    except FallbackModelEvalAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await service.get_status()
