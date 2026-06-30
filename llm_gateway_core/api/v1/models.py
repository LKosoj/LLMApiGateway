import copy
import logging
import httpx
import time
from fastapi import APIRouter, HTTPException, Request

# Relative imports from the new structure
from ...config.loader import (
    ANTHROPIC_API_VERSION,
    ConfigLoader,
    ProviderDetails,
)
from ...config.settings import settings
from ...services.model_policy import (
    filter_excluded_models,
    is_model_excluded,
    public_alias_entries,
)
from ...services.provider_models import provider_explicit_models
from ...services.provider_auth import resolve_provider_auth_headers

logger = logging.getLogger(__name__)

router = APIRouter()
ANTHROPIC_API_KEY_HEADER_NAME = "x-api-key"
CAPABILITY_ORDER = (
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
MODEL_TYPE_BY_CAPABILITY = {
    "chat": "text",
    "embeddings": "embeddings",
    "rerank": "rerank",
    "images": "image",
    "audio_speech": "audio",
    "audio_transcription": "audio",
    "pdf_conversion": "document",
    "web_search": "web",
    "web_read": "web",
    "web_research": "web",
    "web_deep_research": "web",
}
INPUT_MODALITIES_BY_CAPABILITY = {
    "chat": ("text",),
    "embeddings": ("text",),
    "rerank": ("text",),
    "images": ("text",),
    "audio_speech": ("text",),
    "audio_transcription": ("audio",),
    "pdf_conversion": ("document",),
    "web_search": ("text",),
    "web_read": ("text",),
    "web_research": ("text",),
    "web_deep_research": ("text",),
}
OUTPUT_MODALITIES_BY_CAPABILITY = {
    "chat": ("text",),
    "embeddings": ("embeddings",),
    "rerank": ("text",),
    "images": ("image",),
    "audio_speech": ("audio",),
    "audio_transcription": ("text",),
    "pdf_conversion": ("document",),
    "web_search": ("text",),
    "web_read": ("text",),
    "web_research": ("text",),
    "web_deep_research": ("text",),
}
MODALITY_ORDER = ("text", "image", "audio", "document", "embeddings")
IMAGE_OPERATION_ORDER = ("generation", "edit")
WEB_OPERATION_ORDER = ("search", "read", "research", "deep_research")


async def _build_provider_models_request(
    request: Request,
    provider_name: str,
    provider_config: ProviderDetails,
    *,
    model_id: str | None = None,
) -> tuple[str, dict[str, str]]:
    provider_base_url = provider_config.baseUrl.rstrip("/")
    auth_headers = await resolve_provider_auth_headers(
        request,
        provider_name=provider_name,
        provider_config=provider_config,
    )
    if getattr(provider_config, "type", "openai") == "anthropic":
        target_url = f"{provider_base_url}/v1/models"
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_API_VERSION,
            **auth_headers,
        }
    else:
        target_url = f"{provider_base_url}/models"
        headers = {
            "Content-Type": "application/json",
            **auth_headers,
        }

    if model_id is not None:
        target_url = f"{target_url.rstrip('/')}/{model_id}"

    return target_url, headers


def _ensure_gateway_model_entry(gateway_models: dict, model_name: str) -> dict:
    return gateway_models.setdefault(
        model_name,
        {
            "id": model_name,
            "object": "model",
            "owned_by": "llmgateway",
            "architecture": {
                "input_modalities": [],
                "output_modalities": [],
            },
        },
    )


def _sort_by_known_order(values: set[str], known_order: tuple[str, ...]) -> list[str]:
    ordered_values = [value for value in known_order if value in values]
    remaining_values = sorted(value for value in values if value not in known_order)
    return ordered_values + remaining_values


def _refresh_gateway_model_metadata(model_entry: dict) -> None:
    capabilities = model_entry.get("capabilities", [])
    capability_set = set(capabilities)

    architecture = model_entry.setdefault(
        "architecture",
        {
            "input_modalities": [],
            "output_modalities": [],
        },
    )

    input_modalities = {
        modality
        for capability in capabilities
        for modality in INPUT_MODALITIES_BY_CAPABILITY.get(capability, ())
    }
    output_modalities = {
        modality
        for capability in capabilities
        for modality in OUTPUT_MODALITIES_BY_CAPABILITY.get(capability, ())
    }

    architecture["input_modalities"] = _sort_by_known_order(input_modalities, MODALITY_ORDER)
    architecture["output_modalities"] = _sort_by_known_order(output_modalities, MODALITY_ORDER)

    if len(capability_set) == 1:
        capability = capabilities[0]
        model_entry["type"] = MODEL_TYPE_BY_CAPABILITY[capability]
        return

    model_entry.pop("type", None)


def _add_capability(gateway_models: dict, model_name: str, capability: str) -> None:
    if capability not in CAPABILITY_ORDER:
        raise ValueError(f"Unsupported capability '{capability}'")

    model_entry = _ensure_gateway_model_entry(gateway_models, model_name)
    capabilities = model_entry.setdefault("capabilities", [])
    if capability not in capabilities:
        capabilities.append(capability)
    _refresh_gateway_model_metadata(model_entry)


def _add_input_modalities(gateway_models: dict, model_name: str, modalities: tuple[str, ...]) -> None:
    model_entry = _ensure_gateway_model_entry(gateway_models, model_name)
    architecture = model_entry.setdefault(
        "architecture",
        {
            "input_modalities": [],
            "output_modalities": [],
        },
    )

    input_modalities = set(architecture.get("input_modalities", []))
    input_modalities.update(modalities)
    architecture["input_modalities"] = _sort_by_known_order(input_modalities, MODALITY_ORDER)


def _add_image_operation(gateway_models: dict, model_name: str, operation: str) -> None:
    if operation not in IMAGE_OPERATION_ORDER:
        raise ValueError(f"Unsupported image operation '{operation}'")

    model_entry = _ensure_gateway_model_entry(gateway_models, model_name)
    image_operations = model_entry.setdefault("image_operations", [])
    if operation not in image_operations:
        image_operations.append(operation)
        model_entry["image_operations"] = _sort_by_known_order(set(image_operations), IMAGE_OPERATION_ORDER)


def _add_web_operation(gateway_models: dict, model_name: str, operation: str) -> None:
    if operation not in WEB_OPERATION_ORDER:
        raise ValueError(f"Unsupported web operation '{operation}'")

    model_entry = _ensure_gateway_model_entry(gateway_models, model_name)
    web_operations = model_entry.setdefault("web_operations", [])
    if operation not in web_operations:
        web_operations.append(operation)
        model_entry["web_operations"] = _sort_by_known_order(set(web_operations), WEB_OPERATION_ORDER)


def _add_gateway_operation_models(gateway_models: dict, operation_rules: dict) -> None:
    for model_name in operation_rules.get("embeddings", {}).keys():
        _add_capability(gateway_models, model_name, "embeddings")

    for model_name in operation_rules.get("rerank", {}).keys():
        _add_capability(gateway_models, model_name, "rerank")

    image_model_names = set(operation_rules.get("images_generations", {}).keys())
    image_model_names.update(operation_rules.get("images_edits", {}).keys())
    for model_name in image_model_names:
        _add_capability(gateway_models, model_name, "images")

    for model_name in operation_rules.get("images_generations", {}).keys():
        _add_image_operation(gateway_models, model_name, "generation")

    for model_name in operation_rules.get("images_edits", {}).keys():
        _add_image_operation(gateway_models, model_name, "edit")
        _add_input_modalities(gateway_models, model_name, ("image",))

    for model_name in operation_rules.get("audio_speech", {}).keys():
        _add_capability(gateway_models, model_name, "audio_speech")

    for model_name in operation_rules.get("audio_transcriptions", {}).keys():
        _add_capability(gateway_models, model_name, "audio_transcription")

    for model_name in operation_rules.get("pdf_conversions", {}).keys():
        _add_capability(gateway_models, model_name, "pdf_conversion")

    for model_name in operation_rules.get("web_search", {}).keys():
        _add_capability(gateway_models, model_name, "web_search")
        _add_web_operation(gateway_models, model_name, "search")

    for model_name in operation_rules.get("web_read", {}).keys():
        _add_capability(gateway_models, model_name, "web_read")
        _add_web_operation(gateway_models, model_name, "read")

    for model_name in operation_rules.get("web_research", {}).keys():
        _add_capability(gateway_models, model_name, "web_research")
        _add_web_operation(gateway_models, model_name, "research")

    for model_name in operation_rules.get("web_deep_research", {}).keys():
        _add_capability(gateway_models, model_name, "web_deep_research")
        _add_web_operation(gateway_models, model_name, "deep_research")


def _apply_model_rules_to_gateway_models(gateway_models: dict, model_rules: dict | None) -> dict:
    for alias, target in public_alias_entries(model_rules).items():
        if alias in gateway_models or target not in gateway_models:
            continue
        alias_entry = copy.deepcopy(gateway_models[target])
        alias_entry["id"] = alias
        alias_entry["aliases_to"] = target
        alias_entry["owned_by"] = "llmgateway"
        gateway_models[alias] = alias_entry
    return filter_excluded_models(gateway_models, model_rules)

def _is_anthropic_request(request: Request) -> bool:
    """Check if request is from Anthropic SDK or Claude Code."""
    # Official SDK uses x-api-key
    if request.headers.get(ANTHROPIC_API_KEY_HEADER_NAME):
        return True
    
    # Claude Code and newer SDKs might use Authorization but also send anthropic-version
    if request.headers.get("anthropic-version"):
        return True
        
    # Some clients might send anthropic-beta or specific User-Agent
    user_agent = request.headers.get("user-agent", "").lower()
    if "anthropic" in user_agent or "claude-cli" in user_agent:
        return True

    return False


def _format_model_for_anthropic(model_entry: dict) -> dict:
    """Convert OpenAI-style model entry to Anthropic format."""
    model_id = model_entry.get("id", "")
    
    # Generate a more user-friendly display name for gateway models
    display_name = model_entry.get("display_name")
    if not display_name:
        if model_id.startswith("llmgateway/"):
            # Convert llmgateway/qwen3.5 to LLMGateway: Qwen3.5
            name_part = model_id[len("llmgateway/"):].replace("-", " ").replace(".", " ")
            display_name = f"LLMGateway: {name_part.title()}"
        elif model_id.startswith("llmgateway."):
            # Convert llmgateway.qwen3.5 to LLMGateway: Qwen3.5
            name_part = model_id[len("llmgateway."):].replace("-", " ").replace(".", " ")
            display_name = f"LLMGateway: {name_part.title()}"
        else:
            display_name = model_id
    
    anthropic_model = {
        "id": model_id,
        "type": "model",
        "display_name": display_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(model_entry.get("created", int(time.time())))),
    }

    capabilities = model_entry.get("capabilities")
    if isinstance(capabilities, list):
        anthropic_model["capabilities"] = list(capabilities)

    architecture = model_entry.get("architecture")
    if isinstance(architecture, dict):
        anthropic_model["architecture"] = {
            "input_modalities": list(architecture.get("input_modalities", [])),
            "output_modalities": list(architecture.get("output_modalities", [])),
        }

    image_operations = model_entry.get("image_operations")
    if isinstance(image_operations, list):
        anthropic_model["image_operations"] = list(image_operations)

    web_operations = model_entry.get("web_operations")
    if isinstance(web_operations, list):
        anthropic_model["web_operations"] = list(web_operations)

    gateway_type = model_entry.get("type")
    if gateway_type and gateway_type != "model":
        anthropic_model["gateway_type"] = gateway_type

    return anthropic_model


@router.get("") # Route relative to the prefix defined in v1/__init__.py
async def get_models(request: Request):
    """
    Returns a combined list of models available through the gateway's
    routing rules and the configured fallback provider.
    
    Returns response in Anthropic format if request uses X-Api-Key header,
    otherwise returns OpenAI-compatible format.
    """
    is_anthropic = _is_anthropic_request(request)
    
    http_client: object = getattr(request.app.state, "http_client", None)
    if http_client is None:
        logger.error("Shared httpx.AsyncClient not found in application state within get_models.")
        raise HTTPException(status_code=500, detail="Internal server error: Shared HTTP client not available.")
    config_loader_instance: ConfigLoader = request.app.state.config_loader
    if not config_loader_instance:
        logger.error("ConfigLoader not found in application state within get_models.")
        raise HTTPException(status_code=500, detail="Internal server error: Core configuration not available.")

    providers_config = config_loader_instance.providers_config
    fallback_rules = config_loader_instance.fallback_rules
    operation_rules = getattr(
        request.app.state,
        "operation_rules",
        {
            "embeddings": {},
            "rerank": {},
            "images_generations": {},
            "images_edits": {},
            "audio_speech": {},
            "audio_transcriptions": {},
            "pdf_conversions": {},
            "web_search": {},
            "web_read": {},
            "web_research": {},
            "web_deep_research": {},
        },
    )

    gateway_models = {} # Use dict to avoid duplicates easily
    # 1. Add models defined in the gateway's fallback rules
    for model_name in fallback_rules.keys():
        _add_capability(gateway_models, model_name, "chat")
    # Fusion ensemble models are callable as regular chat models.
    fusion_rules = getattr(config_loader_instance, "fusion_rules", None)
    if isinstance(fusion_rules, dict):
        for model_name in fusion_rules.keys():
            _add_capability(gateway_models, model_name, "chat")
    # Router models are callable as regular chat models.
    router_rules = getattr(config_loader_instance, "router_rules", None)
    if isinstance(router_rules, dict):
        for model_name in router_rules.keys():
            _add_capability(gateway_models, model_name, "chat")
    _add_gateway_operation_models(gateway_models, operation_rules)

    # 2. Fetch and add models from the fallback provider
    fallback_provider_name = settings.fallback_provider
    if not fallback_provider_name:
        logger.warning("No fallback_provider configured in settings. Skipping fallback provider models list.")
    else:
        fallback_provider_config = providers_config.get(fallback_provider_name)
        if not fallback_provider_config:
            logger.error(f"Configuration error: Fallback provider '{fallback_provider_name}' not found in providers config.")
            # Proceed without fallback models, or raise an internal error? For now, proceed.
        else:
            fallback_provider_base_url = fallback_provider_config.baseUrl

            if not fallback_provider_base_url:
                logger.error(f"Configuration error: 'baseUrl' missing for fallback provider '{fallback_provider_name}'.")
            else:
                explicit_models = provider_explicit_models(fallback_provider_config)
                if explicit_models is not None:
                    # Provider pins its model list in config; skip the live /models call.
                    for model_id in explicit_models:
                        if model_id not in gateway_models:  # Don't override gateway rules
                            gateway_models[model_id] = {
                                "id": model_id,
                                "object": "model",
                                "owned_by": fallback_provider_name,
                                "source_provider": fallback_provider_name,
                            }
                    logger.info(
                        f"Using {len(explicit_models)} explicitly configured models "
                        f"for fallback provider '{fallback_provider_name}'."
                    )
                else:
                    target_url, headers = await _build_provider_models_request(
                        request,
                        fallback_provider_name,
                        fallback_provider_config,
                    )

                    try:
                        logger.info(f"Fetching models list from fallback provider '{fallback_provider_name}' at {target_url}")
                        response_fallback = await http_client.get(target_url, headers=headers)

                        # Check for downstream errors
                        if response_fallback.status_code >= 400:
                            error_text = response_fallback.text
                            logger.warning(f"Downstream error {response_fallback.status_code} fetching models from {target_url}: {error_text[:500]}...")
                            # Don't raise immediately, allow gateway models to still be returned
                        else:
                            # Attempt to parse JSON and merge models
                            try:
                                fallback_models_data = response_fallback.json()
                                if isinstance(fallback_models_data.get("data"), list):
                                    for model_info in fallback_models_data["data"]:
                                        model_id = model_info.get("id")
                                        if model_id and model_id not in gateway_models: # Add only if not already defined by gateway rules
                                            # Add provider info for clarity
                                            model_info["owned_by"] = model_info.get("owned_by", fallback_provider_name)
                                            model_info["source_provider"] = fallback_provider_name
                                            gateway_models[model_id] = model_info
                                    logger.info(f"Successfully merged models from fallback provider '{fallback_provider_name}'.")
                                else:
                                    logger.warning(f"Unexpected format in response from {target_url}. 'data' field missing or not a list.")

                            except ValueError:
                                logger.error(f"Invalid JSON response fetching models from {target_url}: {response_fallback.text[:500]}...", exc_info=True)

                    except httpx.RequestError as e:
                        logger.error(f"RequestError fetching models from fallback provider {target_url}: {e}", exc_info=True)
                    except Exception as e:
                        logger.error(f"Unexpected error fetching models from fallback provider {target_url}: {e}", exc_info=True)


    gateway_models = _apply_model_rules_to_gateway_models(
        gateway_models,
        getattr(config_loader_instance, "model_rules", None),
    )

    # 3. Restrict to models allowed for the caller's virtual key, if any.
    # Master/legacy callers have ``api_key_record is None`` and see the full list.
    api_key_record = getattr(request.state, "api_key_record", None)
    if api_key_record is not None and api_key_record.allowed_models:
        allowed = set(api_key_record.allowed_models)
        gateway_models = {mid: m for mid, m in gateway_models.items() if mid in allowed}

    # 4. Format final response
    response_list = sorted(gateway_models.values(), key=lambda x: x['id']) # Sort by ID

    if is_anthropic:
        # Return in Anthropic format for Anthropic SDK
        anthropic_models = [_format_model_for_anthropic(m) for m in response_list]
        return {
            "data": anthropic_models,
            "has_more": False,
            "first_id": anthropic_models[0]["id"] if anthropic_models else "",
            "last_id": anthropic_models[-1]["id"] if anthropic_models else "",
        }
    else:
        # Return in OpenAI format for OpenAI SDK
        return {
            "object": "list",
            "data": response_list
        }


@router.get("/{model_id:path}") # Route for getting a specific model (allow slashes in model_id)
async def get_model(model_id: str, request: Request):
    """
    Get information about a specific model by ID.
    
    Returns response in Anthropic format if request uses X-Api-Key header,
    otherwise returns OpenAI-compatible format.
    """
    is_anthropic = _is_anthropic_request(request)
    
    http_client: object = getattr(request.app.state, "http_client", None)
    if http_client is None:
        logger.error("Shared httpx.AsyncClient not found in application state within get_model.")
        raise HTTPException(status_code=500, detail="Internal server error: Shared HTTP client not available.")
    config_loader_instance: ConfigLoader = request.app.state.config_loader
    if not config_loader_instance:
        logger.error("ConfigLoader not found in application state within get_model.")
        raise HTTPException(status_code=500, detail="Internal server error: Core configuration not available.")

    providers_config = config_loader_instance.providers_config
    fallback_rules = config_loader_instance.fallback_rules
    operation_rules = getattr(
        request.app.state,
        "operation_rules",
        {
            "embeddings": {},
            "rerank": {},
            "images_generations": {},
            "images_edits": {},
            "audio_speech": {},
            "audio_transcriptions": {},
            "pdf_conversions": {},
            "web_search": {},
            "web_read": {},
            "web_research": {},
            "web_deep_research": {},
        },
    )

    # Reject the request early if the caller's virtual key forbids this model.
    # Master/legacy callers have ``api_key_record is None`` and pass through.
    api_key_record = getattr(request.state, "api_key_record", None)
    if api_key_record is not None and not api_key_record.model_allowed(model_id):
        raise HTTPException(
            status_code=403,
            detail=f"Model '{model_id}' is not allowed for this API key",
        )

    gateway_models = {}

    # 1. Check gateway models first
    for model_name in fallback_rules.keys():
        _add_capability(gateway_models, model_name, "chat")
    fusion_rules = getattr(config_loader_instance, "fusion_rules", None)
    if isinstance(fusion_rules, dict):
        for model_name in fusion_rules.keys():
            _add_capability(gateway_models, model_name, "chat")
    router_rules = getattr(config_loader_instance, "router_rules", None)
    if isinstance(router_rules, dict):
        for model_name in router_rules.keys():
            _add_capability(gateway_models, model_name, "chat")
    _add_gateway_operation_models(gateway_models, operation_rules)
    gateway_models = _apply_model_rules_to_gateway_models(
        gateway_models,
        getattr(config_loader_instance, "model_rules", None),
    )

    # 2. Check if requested model is in gateway models
    if model_id in gateway_models:
        model_entry = gateway_models[model_id]
        if is_anthropic:
            return _format_model_for_anthropic(model_entry)
        else:
            return model_entry

    if is_model_excluded(model_id, getattr(config_loader_instance, "model_rules", None)):
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' is excluded by model_rules.")

    # 3. If not found in gateway models, try to fetch from fallback provider
    fallback_provider_name = settings.fallback_provider
    if fallback_provider_name:
        fallback_provider_config = providers_config.get(fallback_provider_name)
        explicit_models = (
            provider_explicit_models(fallback_provider_config)
            if fallback_provider_config
            else None
        )
        if explicit_models is not None:
            if model_id in explicit_models:
                model_entry = {
                    "id": model_id,
                    "object": "model",
                    "owned_by": fallback_provider_name,
                    "source_provider": fallback_provider_name,
                }
                if is_anthropic:
                    return _format_model_for_anthropic(model_entry)
                return model_entry
            # Pinned model list without this id → fall through to 404, no live lookup.
        elif fallback_provider_config and fallback_provider_config.baseUrl:
            target_url, headers = await _build_provider_models_request(
                request,
                fallback_provider_name,
                fallback_provider_config,
                model_id=model_id,
            )
            
            try:
                logger.info(f"Fetching model '{model_id}' from fallback provider '{fallback_provider_name}' at {target_url}")
                response_fallback = await http_client.get(target_url, headers=headers)
                
                if response_fallback.status_code == 200:
                    try:
                        model_data = response_fallback.json()
                        if is_anthropic:
                            # Convert to Anthropic format
                            return _format_model_for_anthropic(model_data)
                        else:
                            return model_data
                    except ValueError:
                        logger.error(f"Invalid JSON response fetching model {model_id} from {target_url}")
                elif response_fallback.status_code == 404:
                    logger.info(f"Model '{model_id}' not found in fallback provider '{fallback_provider_name}'")
                else:
                    logger.warning(f"Downstream error {response_fallback.status_code} fetching model {model_id} from {target_url}")
                    
            except httpx.RequestError as e:
                logger.error(f"RequestError fetching model {model_id} from fallback provider {target_url}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error fetching model {model_id} from fallback provider {target_url}: {e}")

    # Model not found
    raise HTTPException(
        status_code=404,
        detail=f"Model '{model_id}' not found. Available models: {list(gateway_models.keys())}"
    )
