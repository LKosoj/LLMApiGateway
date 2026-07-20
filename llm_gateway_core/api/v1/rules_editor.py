import logging
import json
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Request, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from ...config.loader import (
    FusionModelConfig,
    ModelFallbackConfig,
    ModelsOperationConfig,
    RouterModelConfig,
    SubscriptionQuotaConfig,
)
from ...config.config_store import ConfigFile, ConfigSourceBundle
from ...services.fallback_model_evals import FallbackModelEvalAlreadyRunning
from ...services.openrouter_free_models import OpenRouterFreeModelsNotConfigured
from ...config.paths import PROJECT_ROOT, STATIC_DIR
from ...services.provider_auth import resolve_provider_auth_headers
from ...services.config_updates import (
    ConfigRevision,
    ConfigUpdateCoordinator,
    ConfigUpdateError,
    ConfigUpdateErrorCode,
)
from ...services.runtime_config import AppServices, RuntimeSnapshot
from ...utils.html_cache import get_template
from ...middleware.auth import ROLE_MASTER
from ...middleware.runtime_snapshot import retain_request_runtime
from .config_update_http import (
    ConfigRequestInvalid,
    RawConfigBody,
    capture_config_update_runtime,
    config_error_response,
    config_response_headers,
    parse_config_if_match,
    read_raw_config_body,
)

editor_router = APIRouter()


def _require_master(request: Request) -> None:
    if getattr(request.state, "api_key_role", None) != ROLE_MASTER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint is reserved for the master API key",
        )

HTML_DIR = STATIC_DIR
FREE_TIER_PROVIDERS_DOC_PATH = PROJECT_ROOT / "examples" / "free-tier-providers.md"
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
    model_config = ConfigDict(extra="forbid")

    name: str
    baseUrl: str
    apikey: str | None = None
    type: Literal["openai", "anthropic"] = "openai"
    proxy: str | None = None
    models: Any | None = None
    available_models: list[str] | None = None
    subscription_quota: SubscriptionQuotaConfig | None = None
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


class StructuredRouterPayload(BaseModel):
    rules: list[RouterModelConfig] = Field(default_factory=list)


def _request_body_openapi(
    schema: dict[str, Any],
    *,
    media_type: str,
) -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {media_type: {"schema": schema}},
        }
    }


def _inline_local_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    definitions = schema.get("$defs", {})
    if not isinstance(definitions, dict):
        raise ValueError("OpenAPI schema '$defs' must be an object.")

    def expand(value: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(value, list):
            return [expand(item, stack) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" not in value:
            if "$defs" in value:
                raise ValueError("Nested OpenAPI '$defs' are not supported.")
            return {key: expand(item, stack) for key, item in value.items()}
        if set(value) != {"$ref"}:
            raise ValueError("OpenAPI '$ref' with sibling keys is not supported.")

        reference = value["$ref"]
        prefix = "#/$defs/"
        if not isinstance(reference, str) or not reference.startswith(prefix):
            raise ValueError(f"Unsupported OpenAPI reference: {reference!r}.")
        definition_name = reference[len(prefix) :]
        if definition_name not in definitions:
            raise ValueError(f"Missing OpenAPI definition: {definition_name!r}.")
        if definition_name in stack:
            raise ValueError(f"Cyclic OpenAPI definition: {definition_name!r}.")
        return expand(definitions[definition_name], (*stack, definition_name))

    root_schema = {key: value for key, value in schema.items() if key != "$defs"}
    return expand(root_schema)


RAW_CONFIG_OPENAPI = _request_body_openapi(
    {"title": "Payload Text", "type": "string"},
    media_type="text/plain",
)
STRUCTURED_RULES_OPENAPI = _request_body_openapi(
    _inline_local_schema_refs(StructuredRulesPayload.model_json_schema()),
    media_type="application/json",
)
STRUCTURED_FUSION_OPENAPI = _request_body_openapi(
    _inline_local_schema_refs(StructuredFusionPayload.model_json_schema()),
    media_type="application/json",
)
STRUCTURED_ROUTER_OPENAPI = _request_body_openapi(
    _inline_local_schema_refs(StructuredRouterPayload.model_json_schema()),
    media_type="application/json",
)
STRUCTURED_PROVIDERS_OPENAPI = _request_body_openapi(
    _inline_local_schema_refs(StructuredProvidersPayload.model_json_schema()),
    media_type="application/json",
)
STRUCTURED_OPERATION_OPENAPI = _request_body_openapi(
    {"type": "object", "additionalProperties": True},
    media_type="application/json",
)


def _capture_control_runtime(
    request: Request,
) -> tuple[AppServices, RuntimeSnapshot]:
    services = getattr(request.app.state, "services", None)
    runtime_snapshot = getattr(request.state, "runtime_snapshot", None)
    if not isinstance(services, AppServices) or not isinstance(
        runtime_snapshot,
        RuntimeSnapshot,
    ):
        logging.error("Typed control runtime dependencies are not available.")
        raise HTTPException(
            status_code=500,
            detail="Internal server error: Runtime dependencies not available.",
        )
    return services, runtime_snapshot


def _serialize_structured_rules(rules: list[ModelFallbackConfig]) -> str:
    payload = [rule.model_dump(exclude_none=True) for rule in rules]
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _serialize_structured_fusion(rules: list[FusionModelConfig]) -> str:
    payload = [rule.model_dump(exclude_none=True) for rule in rules]
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _serialize_structured_router(rules: list[RouterModelConfig]) -> str:
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


def _build_structured_router_response(config_loader) -> dict:
    fallback_rules = config_loader.fallback_rules or {}
    rules: list[dict] = []
    for gateway_model_name, config in (getattr(config_loader, "router_rules", None) or {}).items():
        rule_payload = {"gateway_model_name": gateway_model_name, **config}
        rules.append(rule_payload)

    fallback_chains: dict[str, list[dict[str, Any]]] = {}
    for gateway_model_name, config in fallback_rules.items():
        chain: list[dict[str, Any]] = []
        for index, fallback_model in enumerate(config.get("fallback_models", []) or []):
            chain.append(
                {
                    "index": index,
                    "provider": fallback_model.get("provider"),
                    "model": fallback_model.get("model"),
                }
            )
        fallback_chains[gateway_model_name] = chain

    return {
        "rules": rules,
        "chat_models": sorted(fallback_rules.keys()),
        "fallback_chains": fallback_chains,
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
        if provider.subscription_quota is not None:
            provider_payload["subscription_quota"] = (
                provider.subscription_quota.model_dump()
            )
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
    fallback_rules = getattr(config_loader, "_fallback_rules_base", None)
    if fallback_rules is None:
        fallback_rules = config_loader.fallback_rules
    for gateway_model_name, config in fallback_rules.items():
        rule_payload = {
            "gateway_model_name": gateway_model_name,
            "fallback_models": config.get("fallback_models", []),
            "rotate_models": config.get("rotate_models", False),
            "dynamic_penalty": config.get("dynamic_penalty", False),
            "strip_think_tags": config.get("strip_think_tags", False),
            "compress_tool_results": config.get("compress_tool_results", False),
            "tool_call_rescue": config.get("tool_call_rescue", False),
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
    for model_name in (getattr(config_loader, "fusion_rules", None) or {}).keys():
        _add_docs_capability(catalog, model_name, "chat")
    for model_name in (getattr(config_loader, "router_rules", None) or {}).keys():
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
    def serialize_model(
        gateway_model_name: str,
        config: dict[str, Any],
        *,
        include_routes: bool,
    ) -> dict[str, Any]:
        model = {
            "gateway_model_name": gateway_model_name,
            **{key: value for key, value in config.items() if key != "routes"},
        }
        if include_routes:
            model["routes"] = config.get("routes", [])
        return model

    response_payload = {
        "embeddings": [
            serialize_model(gateway_model_name, config, include_routes=True)
            for gateway_model_name, config in config_loader.operation_rules.get("embeddings", {}).items()
        ],
        "rerank": [
            serialize_model(gateway_model_name, config, include_routes=True)
            for gateway_model_name, config in config_loader.operation_rules.get("rerank", {}).items()
        ],
        "images_generations": [
            serialize_model(gateway_model_name, config, include_routes=True)
            for gateway_model_name, config in config_loader.operation_rules.get("images_generations", {}).items()
        ],
        "images_edits": [
            serialize_model(gateway_model_name, config, include_routes=True)
            for gateway_model_name, config in config_loader.operation_rules.get("images_edits", {}).items()
        ],
    }

    audio_transcriptions = [
        serialize_model(gateway_model_name, config, include_routes=True)
        for gateway_model_name, config in config_loader.operation_rules.get("audio_transcriptions", {}).items()
    ]
    if audio_transcriptions:
        response_payload["audio_transcriptions"] = audio_transcriptions

    for section_name in ("audio_speech", "pdf_conversions"):
        section_rules = [
            serialize_model(gateway_model_name, config, include_routes=True)
            for gateway_model_name, config in config_loader.operation_rules.get(section_name, {}).items()
        ]
        if section_rules:
            response_payload[section_name] = section_rules

    for section_name in ("web_search", "web_read"):
        section_rules = [
            serialize_model(gateway_model_name, config, include_routes=False)
            for gateway_model_name, config in config_loader.operation_rules.get(section_name, {}).items()
        ]
        if section_rules:
            response_payload[section_name] = section_rules

    for section_name in ("web_research", "web_deep_research"):
        section_rules = [
            serialize_model(gateway_model_name, config, include_routes=True)
            for gateway_model_name, config in config_loader.operation_rules.get(section_name, {}).items()
        ]
        if section_rules:
            response_payload[section_name] = section_rules

    return response_payload


def _snapshot_source_bundle(snapshot: RuntimeSnapshot) -> ConfigSourceBundle:
    bundle = snapshot.config_loader.source_bundle
    if not isinstance(bundle, ConfigSourceBundle):
        raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)
    return bundle


def _config_filename(snapshot: RuntimeSnapshot, config_file: ConfigFile) -> str:
    return _snapshot_source_bundle(snapshot)[config_file].path.name


def _config_json_response(
    snapshot: RuntimeSnapshot,
    config_file: ConfigFile,
    content: dict[str, Any],
) -> JSONResponse:
    return JSONResponse(
        content=content,
        headers=config_response_headers(snapshot, config_file),
    )


def _config_raw_response(
    snapshot: RuntimeSnapshot,
    config_file: ConfigFile,
    *,
    missing_content: bytes | None = None,
) -> PlainTextResponse:
    document = _snapshot_source_bundle(snapshot)[config_file]
    if document.exists:
        if document.content is None:
            raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)
        content = document.content
    elif missing_content is not None:
        content = missing_content
    else:
        raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)
    return PlainTextResponse(
        content=content,
        headers=config_response_headers(snapshot, config_file),
    )


async def _read_config_update_request(
    request: Request,
    config_file: ConfigFile,
) -> tuple[ConfigRevision | None, RawConfigBody]:
    expected_revision = parse_config_if_match(request, config_file)
    raw_body = await read_raw_config_body(request)
    return expected_revision, raw_body


def _decode_structured_envelope(raw_body: RawConfigBody) -> Any:
    try:
        return json.loads(raw_body.validation_text)
    except (json.JSONDecodeError, RecursionError):
        raise ConfigRequestInvalid from None


def _capture_config_update_base(
    request: Request,
    config_file: ConfigFile,
    expected_revision: ConfigRevision | None,
) -> tuple[AppServices, RuntimeSnapshot, ConfigUpdateCoordinator]:
    services, snapshot = capture_config_update_runtime(request)
    coordinator = services.config_update_coordinator
    coordinator.check_base(
        base_snapshot=snapshot,
        config_file=config_file,
        expected_revision=expected_revision,
    )
    return services, snapshot, coordinator


def _trigger_capability_autofill(services: AppServices) -> None:
    """Fire-and-forget F-auto materialize after a fallback-rules save.

    The save response does not wait for this; failures are logged and
    absorbed inside ``materialize()`` itself.
    """
    services.task_supervisor.create_task(
        services.capability_autofill_service.materialize(
            runtime_manager=services.runtime_manager,
            config_update_coordinator=services.config_update_coordinator,
            shared_http_client=services.http_client,
            openrouter_free_models_service=services.openrouter_free_models_service,
        ),
        name="capability-autofill-editor-save",
    )


def _safe_validation_errors(
    exc: ValidationError,
) -> tuple[dict[str, object], ...]:
    """Credential-safe validation error detail (field path, no raw input)."""
    return tuple(
        {
            "type": error["type"],
            "loc": list(error["loc"]),
            "msg": error["msg"],
        }
        for error in exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
    )


def _validation_failed(exc: ValidationError | None = None) -> ConfigUpdateError:
    errors = _safe_validation_errors(exc) if exc is not None else None
    return ConfigUpdateError(ConfigUpdateErrorCode.VALIDATION_FAILED, errors=errors)


async def _validate_candidate_provider_models(
    request: Request,
    candidate_snapshot: RuntimeSnapshot,
    shared_http_client: httpx.AsyncClient,
) -> None:
    config_loader = candidate_snapshot.config_loader
    fallback_rules = config_loader._fallback_rules_base
    provider_models_service = candidate_snapshot.provider_models_service

    models_by_provider: dict[str, set[str]] = {}
    for config in fallback_rules.values():
        for fallback_model in _iter_rule_targets(config):
            provider_name = fallback_model["provider"]
            if provider_name in models_by_provider:
                continue

            provider_config = config_loader.providers_config.get(provider_name)
            if provider_config is None:
                raise ValueError("Fallback provider is not configured.")
            http_client = (
                candidate_snapshot.proxy_http_clients[provider_name]
                if provider_name in candidate_snapshot.proxy_http_clients
                else shared_http_client
            )
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
            models_by_provider[provider_name] = set(provider_models)

    for config in fallback_rules.values():
        for fallback_model in _iter_rule_targets(config):
            provider_name = fallback_model["provider"]
            model_name = fallback_model["model"]
            if model_name not in models_by_provider.get(provider_name, set()):
                raise ValueError("Fallback model is not available from provider.")


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
    _services, runtime_snapshot = _capture_control_runtime(request)
    config_loader = runtime_snapshot.config_loader
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
    router_rules = getattr(config_loader, "router_rules", None) or {}

    def _names(section: str) -> list[str]:
        return sorted((rules.get(section) or {}).keys())

    # Fusion and Router models are callable as regular chat models, so they
    # belong in the chat selector. ``fusion`` is also returned separately so the
    # UI can mark them and enable Fusion-specific controls.
    return {
        "chat": sorted(set(fallback_rules.keys()) | set(fusion_rules.keys()) | set(router_rules.keys())),
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
    _services, runtime_snapshot = _capture_control_runtime(request)
    config_loader = runtime_snapshot.config_loader
    return JSONResponse(content=_build_playground_models(config_loader))


@editor_router.get("/ui/web-playground/models", tags=["Config Editor API"])
async def get_web_playground_models(request: Request):
    """Returns playground model lists for the legacy web-playground URL."""
    _services, runtime_snapshot = _capture_control_runtime(request)
    config_loader = runtime_snapshot.config_loader
    return JSONResponse(content=_build_playground_models(config_loader))


@editor_router.get("/config/models-rules", response_class=PlainTextResponse, tags=["Config Editor API"])
async def get_models_rules_text(request: Request):
    """Fetches the current raw text content of models_fallback_rules.json."""
    try:
        _services, snapshot = capture_config_update_runtime(request)
        return _config_raw_response(snapshot, ConfigFile.FALLBACK_RULES)
    except ConfigUpdateError as exc:
        return config_error_response(exc)

# If router is included with prefix /v1, this becomes /v1/config/models-rules
@editor_router.post(
    "/config/models-rules",
    tags=["Config Editor API"],
    openapi_extra=RAW_CONFIG_OPENAPI,
)
async def save_models_rules(request: Request):
    """
    Validates and saves the updated models_fallback_rules.json.
    Triggers a configuration reload on success.
    """
    _require_master(request)
    try:
        expected_revision, raw_body = await _read_config_update_request(
            request,
            ConfigFile.FALLBACK_RULES,
        )
        _services, snapshot, coordinator = _capture_config_update_base(
            request,
            ConfigFile.FALLBACK_RULES,
            expected_revision,
        )
        result = await coordinator.update(
            base_snapshot=snapshot,
            config_file=ConfigFile.FALLBACK_RULES,
            candidate_bytes=raw_body.original_bytes,
            expected_revision=expected_revision,
            comments_backup=False,
        )
        _trigger_capability_autofill(_services)
        filename = _config_filename(
            result.snapshot,
            ConfigFile.FALLBACK_RULES,
        )
        return _config_json_response(
            result.snapshot,
            ConfigFile.FALLBACK_RULES,
            {"message": f"{filename} updated successfully."},
        )
    except (ConfigRequestInvalid, ConfigUpdateError) as exc:
        return config_error_response(exc)


@editor_router.get("/config/model-rules", response_class=PlainTextResponse, tags=["Config Editor API"])
async def get_model_rules_text(request: Request):
    try:
        _services, snapshot = capture_config_update_runtime(request)
        return _config_raw_response(
            snapshot,
            ConfigFile.MODEL_RULES,
            missing_content=b"{\n}\n",
        )
    except ConfigUpdateError as exc:
        return config_error_response(exc)


@editor_router.post(
    "/config/model-rules",
    tags=["Config Editor API"],
    openapi_extra=RAW_CONFIG_OPENAPI,
)
async def save_model_rules(request: Request):
    _require_master(request)
    try:
        expected_revision, raw_body = await _read_config_update_request(
            request,
            ConfigFile.MODEL_RULES,
        )
        _services, snapshot, coordinator = _capture_config_update_base(
            request,
            ConfigFile.MODEL_RULES,
            expected_revision,
        )
        result = await coordinator.update(
            base_snapshot=snapshot,
            config_file=ConfigFile.MODEL_RULES,
            candidate_bytes=raw_body.original_bytes,
            expected_revision=expected_revision,
            comments_backup=True,
        )
        filename = _config_filename(result.snapshot, ConfigFile.MODEL_RULES)
        response_body = {"message": f"{filename} updated successfully."}
        if result.comments_backup is not None:
            response_body["comments_backup"] = result.comments_backup
        return _config_json_response(
            result.snapshot,
            ConfigFile.MODEL_RULES,
            response_body,
        )
    except (ConfigRequestInvalid, ConfigUpdateError) as exc:
        return config_error_response(exc)


@editor_router.get("/config/models-rules/structured", tags=["Config Editor API"])
async def get_models_rules_structured(request: Request):
    try:
        _services, snapshot = capture_config_update_runtime(request)
        return _config_json_response(
            snapshot,
            ConfigFile.FALLBACK_RULES,
            _build_structured_rules_response(snapshot.config_loader),
        )
    except ConfigUpdateError as exc:
        return config_error_response(exc)


@editor_router.post(
    "/config/models-rules/structured",
    tags=["Config Editor API"],
    openapi_extra=STRUCTURED_RULES_OPENAPI,
)
async def save_models_rules_structured(request: Request):
    _require_master(request)
    try:
        expected_revision, raw_body = await _read_config_update_request(
            request,
            ConfigFile.FALLBACK_RULES,
        )
        envelope = _decode_structured_envelope(raw_body)
        services, snapshot, coordinator = _capture_config_update_base(
            request,
            ConfigFile.FALLBACK_RULES,
            expected_revision,
        )
        try:
            payload = StructuredRulesPayload.model_validate(envelope)
        except ValidationError as exc:
            raise _validation_failed(exc) from None
        candidate_bytes = _serialize_structured_rules(payload.rules).encode(
            "utf-8"
        )

        async def preflight(candidate_snapshot: RuntimeSnapshot) -> None:
            await _validate_candidate_provider_models(
                request,
                candidate_snapshot,
                services.http_client,
            )

        result = await coordinator.update(
            base_snapshot=snapshot,
            config_file=ConfigFile.FALLBACK_RULES,
            candidate_bytes=candidate_bytes,
            expected_revision=expected_revision,
            comments_backup=True,
            preflight=preflight,
        )
        _trigger_capability_autofill(services)
        filename = _config_filename(
            result.snapshot,
            ConfigFile.FALLBACK_RULES,
        )
        response_body = {
            "message": f"{filename} updated successfully.",
            "rules": _build_structured_rules_response(
                result.snapshot.config_loader
            )["rules"],
        }
        if result.comments_backup is not None:
            response_body["comments_backup"] = result.comments_backup
        return _config_json_response(
            result.snapshot,
            ConfigFile.FALLBACK_RULES,
            response_body,
        )
    except (ConfigRequestInvalid, ConfigUpdateError) as exc:
        return config_error_response(exc)


@editor_router.get("/config/fusion-rules/structured", tags=["Config Editor API"])
async def get_fusion_rules_structured(request: Request):
    try:
        _services, snapshot = capture_config_update_runtime(request)
        return _config_json_response(
            snapshot,
            ConfigFile.FUSION_RULES,
            _build_structured_fusion_response(snapshot.config_loader),
        )
    except ConfigUpdateError as exc:
        return config_error_response(exc)


@editor_router.post(
    "/config/fusion-rules/structured",
    tags=["Config Editor API"],
    openapi_extra=STRUCTURED_FUSION_OPENAPI,
)
async def save_fusion_rules_structured(request: Request):
    _require_master(request)
    try:
        expected_revision, raw_body = await _read_config_update_request(
            request,
            ConfigFile.FUSION_RULES,
        )
        envelope = _decode_structured_envelope(raw_body)
        _services, snapshot, coordinator = _capture_config_update_base(
            request,
            ConfigFile.FUSION_RULES,
            expected_revision,
        )
        try:
            payload = StructuredFusionPayload.model_validate(envelope)
        except ValidationError as exc:
            raise _validation_failed(exc) from None
        result = await coordinator.update(
            base_snapshot=snapshot,
            config_file=ConfigFile.FUSION_RULES,
            candidate_bytes=_serialize_structured_fusion(payload.rules).encode(
                "utf-8"
            ),
            expected_revision=expected_revision,
            comments_backup=True,
        )
        filename = _config_filename(result.snapshot, ConfigFile.FUSION_RULES)
        response_body = {
            "message": f"{filename} updated successfully.",
            "rules": _build_structured_fusion_response(
                result.snapshot.config_loader
            )["rules"],
        }
        if result.comments_backup is not None:
            response_body["comments_backup"] = result.comments_backup
        return _config_json_response(
            result.snapshot,
            ConfigFile.FUSION_RULES,
            response_body,
        )
    except (ConfigRequestInvalid, ConfigUpdateError) as exc:
        return config_error_response(exc)


@editor_router.get("/config/router-rules/structured", tags=["Config Editor API"])
async def get_router_rules_structured(request: Request):
    try:
        _services, snapshot = capture_config_update_runtime(request)
        return _config_json_response(
            snapshot,
            ConfigFile.ROUTER_RULES,
            _build_structured_router_response(snapshot.config_loader),
        )
    except ConfigUpdateError as exc:
        return config_error_response(exc)


@editor_router.post(
    "/config/router-rules/structured",
    tags=["Config Editor API"],
    openapi_extra=STRUCTURED_ROUTER_OPENAPI,
)
async def save_router_rules_structured(request: Request):
    _require_master(request)
    try:
        expected_revision, raw_body = await _read_config_update_request(
            request,
            ConfigFile.ROUTER_RULES,
        )
        envelope = _decode_structured_envelope(raw_body)
        _services, snapshot, coordinator = _capture_config_update_base(
            request,
            ConfigFile.ROUTER_RULES,
            expected_revision,
        )
        try:
            payload = StructuredRouterPayload.model_validate(envelope)
        except ValidationError as exc:
            raise _validation_failed(exc) from None
        result = await coordinator.update(
            base_snapshot=snapshot,
            config_file=ConfigFile.ROUTER_RULES,
            candidate_bytes=_serialize_structured_router(payload.rules).encode(
                "utf-8"
            ),
            expected_revision=expected_revision,
            comments_backup=True,
        )
        filename = _config_filename(result.snapshot, ConfigFile.ROUTER_RULES)
        response_body = {
            "message": f"{filename} updated successfully.",
            **_build_structured_router_response(result.snapshot.config_loader),
        }
        if result.comments_backup is not None:
            response_body["comments_backup"] = result.comments_backup
        return _config_json_response(
            result.snapshot,
            ConfigFile.ROUTER_RULES,
            response_body,
        )
    except (ConfigRequestInvalid, ConfigUpdateError) as exc:
        return config_error_response(exc)


@editor_router.get("/config/model-operations/structured", tags=["Config Editor API"])
async def get_operation_rules_structured(request: Request):
    try:
        _services, snapshot = capture_config_update_runtime(request)
        return _config_json_response(
            snapshot,
            ConfigFile.OPERATION_RULES,
            _build_structured_operation_rules_response(snapshot.config_loader),
        )
    except ConfigUpdateError as exc:
        return config_error_response(exc)


@editor_router.post(
    "/config/model-operations/structured",
    tags=["Config Editor API"],
    openapi_extra=STRUCTURED_OPERATION_OPENAPI,
)
async def save_operation_rules_structured(request: Request):
    _require_master(request)
    try:
        expected_revision, raw_body = await _read_config_update_request(
            request,
            ConfigFile.OPERATION_RULES,
        )
        envelope = _decode_structured_envelope(raw_body)
        _services, snapshot, coordinator = _capture_config_update_base(
            request,
            ConfigFile.OPERATION_RULES,
            expected_revision,
        )
        if not isinstance(envelope, dict):
            raise _validation_failed()
        try:
            structured_payload = ModelsOperationConfig.model_validate(envelope)
        except ValidationError as exc:
            raise _validation_failed(exc) from None
        payload_text = _serialize_structured_operation_rules(
            structured_payload,
            include_audio_speech=(
                "audio_speech" in envelope
                or bool(structured_payload.audio_speech)
            ),
            include_audio_transcriptions=(
                "audio_transcriptions" in envelope
                or bool(structured_payload.audio_transcriptions)
            ),
            include_pdf_conversions=(
                "pdf_conversions" in envelope
                or bool(structured_payload.pdf_conversions)
            ),
            include_web_sections=(
                "web_search" in envelope
                or "web_read" in envelope
                or "web_research" in envelope
                or "web_deep_research" in envelope
            ),
        )
        result = await coordinator.update(
            base_snapshot=snapshot,
            config_file=ConfigFile.OPERATION_RULES,
            candidate_bytes=payload_text.encode("utf-8"),
            expected_revision=expected_revision,
            comments_backup=True,
        )
        filename = _config_filename(
            result.snapshot,
            ConfigFile.OPERATION_RULES,
        )
        response_body = {
            "message": f"{filename} updated successfully.",
            **_build_structured_operation_rules_response(
                result.snapshot.config_loader
            ),
        }
        if result.comments_backup is not None:
            response_body["comments_backup"] = result.comments_backup
        return _config_json_response(
            result.snapshot,
            ConfigFile.OPERATION_RULES,
            response_body,
        )
    except (ConfigRequestInvalid, ConfigUpdateError) as exc:
        return config_error_response(exc)


# --- Endpoints for providers.json ---

@editor_router.get("/config/providers", response_class=PlainTextResponse, tags=["Config Editor API"])
async def get_providers_text(request: Request):
    """Fetches the current raw text content of providers.json."""
    try:
        _services, snapshot = capture_config_update_runtime(request)
        return _config_raw_response(snapshot, ConfigFile.PROVIDERS)
    except ConfigUpdateError as exc:
        return config_error_response(exc)


@editor_router.get("/config/providers/structured", tags=["Config Editor API"])
async def get_providers_structured(request: Request):
    try:
        _services, snapshot = capture_config_update_runtime(request)
        return _config_json_response(
            snapshot,
            ConfigFile.PROVIDERS,
            _build_structured_providers_response(snapshot.config_loader),
        )
    except ConfigUpdateError as exc:
        return config_error_response(exc)


@editor_router.post(
    "/config/providers/structured",
    tags=["Config Editor API"],
    openapi_extra=STRUCTURED_PROVIDERS_OPENAPI,
)
async def save_providers_structured(request: Request):
    _require_master(request)
    try:
        expected_revision, raw_body = await _read_config_update_request(
            request,
            ConfigFile.PROVIDERS,
        )
        envelope = _decode_structured_envelope(raw_body)
        _services, snapshot, coordinator = _capture_config_update_base(
            request,
            ConfigFile.PROVIDERS,
            expected_revision,
        )
        try:
            payload = StructuredProvidersPayload.model_validate(envelope)
        except ValidationError as exc:
            raise _validation_failed(exc) from None
        result = await coordinator.update(
            base_snapshot=snapshot,
            config_file=ConfigFile.PROVIDERS,
            candidate_bytes=_serialize_structured_providers(
                payload.providers
            ).encode("utf-8"),
            expected_revision=expected_revision,
            comments_backup=True,
        )
        filename = _config_filename(result.snapshot, ConfigFile.PROVIDERS)
        response_body = {
            "message": f"{filename} updated successfully.",
            **_build_structured_providers_response(
                result.snapshot.config_loader
            ),
        }
        if result.comments_backup is not None:
            response_body["comments_backup"] = result.comments_backup
        return _config_json_response(
            result.snapshot,
            ConfigFile.PROVIDERS,
            response_body,
        )
    except (ConfigRequestInvalid, ConfigUpdateError) as exc:
        return config_error_response(exc)


@editor_router.post(
    "/config/providers",
    tags=["Config Editor API"],
    openapi_extra=RAW_CONFIG_OPENAPI,
)
@editor_router.post(
    "/ui/providers-config",
    tags=["Config Editor API"],
    openapi_extra=RAW_CONFIG_OPENAPI,
)
async def save_providers_config(request: Request):
    """
    Validates and saves the updated providers.json.
    Triggers a providers configuration reload on success.
    """
    _require_master(request)
    try:
        expected_revision, raw_body = await _read_config_update_request(
            request,
            ConfigFile.PROVIDERS,
        )
        _services, snapshot, coordinator = _capture_config_update_base(
            request,
            ConfigFile.PROVIDERS,
            expected_revision,
        )
        result = await coordinator.update(
            base_snapshot=snapshot,
            config_file=ConfigFile.PROVIDERS,
            candidate_bytes=raw_body.original_bytes,
            expected_revision=expected_revision,
            comments_backup=False,
        )
        filename = _config_filename(result.snapshot, ConfigFile.PROVIDERS)
        return _config_json_response(
            result.snapshot,
            ConfigFile.PROVIDERS,
            {"message": f"{filename} updated successfully."},
        )
    except (ConfigRequestInvalid, ConfigUpdateError) as exc:
        return config_error_response(exc)


@editor_router.get("/config/providers/{provider_name}/models", tags=["Config Editor API"])
async def get_provider_models(request: Request, provider_name: str):
    services, runtime_snapshot = _capture_control_runtime(request)
    config_loader = runtime_snapshot.config_loader
    provider_config = config_loader.providers_config.get(provider_name)
    if not provider_config:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found.")

    provider_models_service = runtime_snapshot.provider_models_service
    http_client = runtime_snapshot.proxy_http_clients.get(
        provider_name,
        services.http_client,
    )
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
    services, _runtime_snapshot = _capture_control_runtime(request)
    return await services.openrouter_free_models_service.get_status()


@editor_router.get("/capability-autofill", tags=["Config Editor API"])
async def get_capability_autofill_status(request: Request):
    _require_master(request)
    services, _runtime_snapshot = _capture_control_runtime(request)
    return await services.capability_autofill_service.get_status()


@editor_router.post("/openrouter/free-models/run", tags=["Config Editor API"])
async def start_openrouter_free_models_refresh(request: Request):
    _require_master(request)
    services, _runtime_snapshot = _capture_control_runtime(request)
    service = services.openrouter_free_models_service
    try:
        started = await service.start_manual_full_refresh()
    except OpenRouterFreeModelsNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not started:
        raise HTTPException(status_code=409, detail="OpenRouter free model refresh is already running.")
    return await service.get_status()


@editor_router.get("/fallback-model-evals", tags=["Config Editor API"])
async def get_fallback_model_eval_status(request: Request):
    _require_master(request)
    services, _runtime_snapshot = _capture_control_runtime(request)
    return await services.fallback_model_eval_service.get_status()


@editor_router.post("/fallback-model-evals/run", tags=["Config Editor API"])
async def start_fallback_model_eval(request: Request):
    _require_master(request)
    services, _runtime_snapshot = _capture_control_runtime(request)
    runtime_lease = retain_request_runtime(request)
    try:
        await services.fallback_model_eval_service.start_eval_with_runtime(
            runtime_lease=runtime_lease,
            http_client=services.http_client,
        )
    except FallbackModelEvalAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await services.fallback_model_eval_service.get_status()
