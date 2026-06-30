import json5
import logging
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Literal, Optional, Mapping
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, RootModel, model_validator

# Import settings using relative path within the package
from .settings import settings
from .placeholder_secrets import is_placeholder_secret, placeholder_secret_error
from ..services.payload_transform import (
    validate_payload_transform_filters,
    validate_payload_transform_object,
)
from ..services.model_policy import is_model_excluded
from ..utils.api_keys import select_next_api_key, split_api_keys


class ConfigError(RuntimeError):
    """Raised when configuration loading or validation fails at startup.

    Replaces direct ``sys.exit(1)`` so callers (lifespan, tests) can decide
    how to react — tests can assert on the message, and the process still
    aborts at startup because the exception propagates out of the lifespan.
    """


SECURITY_HEADERS = frozenset({"authorization", "cookie", "x-api-key"})
FORBIDDEN_OPERATION_BODY_PARAMS = frozenset({"stream", "messages", "tool_choice", "tools", "model"})
RERANK_DEFAULT_TARGET_PATH = "/score"
IMAGES_EDITS_DEFAULT_TARGET_PATH = "/images/edits"
AUDIO_SPEECH_DEFAULT_TARGET_PATH = "/audio/speech"
AUDIO_TRANSCRIPTIONS_DEFAULT_TARGET_PATH = "/audio/transcriptions"
PDF_CONVERSIONS_DEFAULT_TARGET_PATH = "/api"
REQUEST_FORMAT_QUERY_PASSAGES = "query_passages"
REQUEST_FORMAT_QUERY_TEXTS = "query_texts"
REQUEST_FORMAT_OPENAI_IMAGES = "openai_images"
REQUEST_FORMAT_OPENAI_IMAGES_MULTIPART = "openai_images_multipart"
REQUEST_FORMAT_NVIDIA_GENAI_JSON = "nvidia_genai_json"
REQUEST_FORMAT_NVIDIA_RIVA_GRPC = "nvidia_riva_grpc"
# Pinned anthropic-version header used for every outbound call to a provider
# of type "anthropic" (/v1/messages and /v1/models).
ANTHROPIC_API_VERSION = "2023-06-01"
RESPONSE_FORMAT_RANKINGS_LOGIT = "rankings_logit"
RESPONSE_FORMAT_SCORES = "scores"
RESPONSE_FORMAT_OPENAI_IMAGES = "openai_images"
RESPONSE_FORMAT_NVIDIA_ARTIFACTS = "nvidia_artifacts"
RESPONSE_OUTPUT_FORMAT_JINA_RESULTS = "jina_results"
SUPPORTED_REQUEST_FORMATS = frozenset(
    {
        REQUEST_FORMAT_QUERY_PASSAGES,
        REQUEST_FORMAT_QUERY_TEXTS,
        REQUEST_FORMAT_OPENAI_IMAGES,
        REQUEST_FORMAT_OPENAI_IMAGES_MULTIPART,
        REQUEST_FORMAT_NVIDIA_GENAI_JSON,
        REQUEST_FORMAT_NVIDIA_RIVA_GRPC,
    }
)
SUPPORTED_RESPONSE_FORMATS = frozenset(
    {
        RESPONSE_FORMAT_RANKINGS_LOGIT,
        RESPONSE_FORMAT_SCORES,
        RESPONSE_FORMAT_OPENAI_IMAGES,
        RESPONSE_FORMAT_NVIDIA_ARTIFACTS,
    }
)
SUPPORTED_RESPONSE_OUTPUT_FORMATS = frozenset({RESPONSE_OUTPUT_FORMAT_JINA_RESULTS})
ENV_REFERENCE_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
ENV_REFERENCE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
KNOWN_PROVIDER_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "COHERE_API_KEY",
        "CLOUDRU_API_KEY",
        "NVIDIA_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "VSEGPT_API_KEY",
    }
)


def resolve_provider_api_key(api_key_reference_or_literal: str | None) -> str | None:
    return select_next_api_key(resolve_provider_api_key_value(api_key_reference_or_literal))


def resolve_provider_api_keys(api_key_reference_or_literal: str | None) -> list[str]:
    return split_api_keys(resolve_provider_api_key_value(api_key_reference_or_literal))


def resolve_provider_config_api_key(provider_config: object) -> str | None:
    api_keys = resolve_provider_config_api_keys(provider_config)
    return select_next_api_key(",".join(api_keys))


def resolve_provider_config_api_keys(provider_config: object) -> list[str]:
    legacy_api_key = getattr(provider_config, "apikey", None)
    if legacy_api_key:
        return resolve_provider_api_keys(legacy_api_key)

    pools = getattr(provider_config, "upstream_key_pools", None)
    if not isinstance(pools, Mapping):
        return []

    api_keys: list[str] = []
    for pool_config in pools.values():
        key_specs = getattr(pool_config, "keys", None)
        if not isinstance(key_specs, list):
            continue
        for key_spec in key_specs:
            if getattr(key_spec, "enabled", True) is False:
                continue
            api_keys.extend(resolve_provider_api_keys(getattr(key_spec, "apikey", None)))
    return api_keys


def resolve_provider_config_auth_headers(provider_config: object, api_key: str | None = None) -> dict[str, str]:
    resolved_api_key = api_key or resolve_provider_config_api_key(provider_config)
    if not resolved_api_key:
        return {}
    if getattr(provider_config, "type", "openai") == "anthropic":
        return {"x-api-key": resolved_api_key}
    return {"Authorization": f"Bearer {resolved_api_key}"}


def resolve_provider_api_key_value(api_key_reference_or_literal: str | None) -> str | None:
    if not api_key_reference_or_literal:
        return None

    env_name = _explicit_env_reference_name(api_key_reference_or_literal, "apikey")
    if env_name is None:
        _warn_unbraced_env_like_literal(api_key_reference_or_literal, "apikey")
        return api_key_reference_or_literal

    return _resolve_explicit_env_reference(env_name, "apikey")


def resolve_provider_proxy(proxy_reference_or_url: str | None) -> str | None:
    """Resolve a proxy URL from an explicit environment reference or a literal URL.

    Environment resolution is opt-in: use ``${PROXY_ENV}``. Without that
    wrapper the value is treated as a literal, even if an environment variable
    with the same name exists.
    """
    if not proxy_reference_or_url:
        return None

    env_name = _explicit_env_reference_name(proxy_reference_or_url, "proxy")
    if env_name is None:
        _warn_unbraced_env_like_literal(proxy_reference_or_url, "proxy")
        return proxy_reference_or_url

    return _resolve_explicit_env_reference(env_name, "proxy")


def _explicit_env_reference_name(value: str, field_name: str) -> str | None:
    match = ENV_REFERENCE_RE.fullmatch(value)
    if match:
        return match.group(1)

    if value.startswith("${") and value.endswith("}"):
        raise ConfigError(
            f"Invalid env reference syntax for provider field '{field_name}'. "
            "Use '${VAR_NAME}' with a non-empty environment variable name."
        )

    return None


def _resolve_explicit_env_reference(env_name: str, field_name: str) -> str:
    resolved = os.getenv(env_name)
    if resolved:
        if field_name == "apikey" and is_placeholder_secret(resolved):
            raise ConfigError(placeholder_secret_error(env_name, resolved))
        return resolved

    raise ConfigError(
        f"env var {env_name} referenced but missing or empty for provider field '{field_name}'"
    )


def _warn_unbraced_env_like_literal(value: str | None, field_name: str) -> None:
    if not _looks_like_env_reference(value):
        return
    if os.getenv(value) is None:
        return

    logging.warning(
        "Provider field '%s' value '%s' looks like an environment variable name, "
        "but '${VAR}' syntax was not used; treating it as a literal.",
        field_name,
        value,
    )


def _looks_like_env_reference(value: str | None) -> bool:
    if not value:
        return False

    normalized_value = value.strip()
    if normalized_value != value or not normalized_value:
        return False
    if any(char.isspace() for char in normalized_value):
        return False
    if normalized_value in KNOWN_PROVIDER_ENV_NAMES:
        return True
    return bool(ENV_REFERENCE_NAME_RE.fullmatch(normalized_value))


def _provider_env_reference_error(provider_name: str, field_name: str, env_name: str) -> str:
    return (
        f"env var {env_name} referenced but missing for provider "
        f"'{provider_name}' field '{field_name}'"
    )


def _validate_non_empty_string(value: str, field_name: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(f"'{field_name}' must not be empty.")
    return normalized_value


def _validate_target_path(value: str) -> str:
    normalized_value = value.strip()
    if normalized_value.startswith(("http://", "https://")):
        return normalized_value
    if not normalized_value.startswith("/"):
        raise ValueError("'target_path' must start with '/' or be an absolute http(s) URL.")
    return normalized_value


def _validate_route_format(value: str | None, field_name: str, allowed_values: frozenset[str]) -> str | None:
    if value is None:
        return None

    normalized_value = value.strip()
    if not normalized_value:
        return None

    if normalized_value not in allowed_values:
        allowed_values_text = ", ".join(sorted(allowed_values))
        raise ValueError(f"'{field_name}' must be one of: {allowed_values_text}.")

    return normalized_value


def _validate_non_negative_number(value: int | float | None, field_name: str) -> int | float | None:
    if value is None:
        return None

    if value < 0:
        raise ValueError(f"'{field_name}' must be greater than or equal to 0.")

    return value


def _validate_custom_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    blocked_headers = sorted(header for header in headers if header.lower() in SECURITY_HEADERS)
    if blocked_headers:
        raise ValueError(
            "custom_headers must not contain protected headers: Authorization, Cookie, X-Api-Key."
        )
    return headers


def _validate_custom_body_params(body_params: Dict[str, Any]) -> Dict[str, Any]:
    blocked_keys = sorted(param for param in body_params if param.lower() in FORBIDDEN_OPERATION_BODY_PARAMS)
    if blocked_keys:
        raise ValueError(
            "custom_body_params must not contain reserved keys: stream, messages, tool_choice, tools, model."
        )
    return body_params


def _validate_json_object(value: Dict[str, Any] | None, field_name: str) -> Dict[str, Any] | None:
    if value is None:
        return None

    if not isinstance(value, dict):
        raise ValueError(f"'{field_name}' must be a JSON object.")

    return value


def _validate_provider_name_list(value: Any, field_name: str) -> List[str] | None:
    if value is None:
        return None

    if not isinstance(value, list):
        raise ValueError(f"'{field_name}' must be a list of provider names.")

    if not value:
        raise ValueError(f"'{field_name}' must not be empty when provided.")

    normalized_values: list[str] = []
    seen_values: set[str] = set()
    for provider_name in value:
        if not isinstance(provider_name, str):
            raise ValueError(f"'{field_name}' must contain only provider name strings.")
        normalized_provider_name = provider_name.strip()
        if not normalized_provider_name:
            raise ValueError(f"'{field_name}' must not contain empty provider names.")
        if normalized_provider_name in seen_values:
            raise ValueError(f"'{field_name}' must not contain duplicate provider names.")
        seen_values.add(normalized_provider_name)
        normalized_values.append(normalized_provider_name)

    return normalized_values


def _validate_header_name(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    normalized_value = _validate_non_empty_string(value, field_name)
    if not re.fullmatch(r"[A-Za-z0-9-]+", normalized_value):
        raise ValueError(f"'{field_name}' must contain only letters, digits, and hyphens.")
    return normalized_value


def _empty_operation_rules() -> Dict[str, Dict[str, Any]]:
    return {
        "embeddings": {},
        "rerank": {},
        "images_generations": {},
        "images_edits": {},
        "audio_speech": {},
        "audio_transcriptions": {},
        "web_search": {},
        "web_read": {},
        "web_research": {},
        "web_deep_research": {},
        "pdf_conversions": {},
    }


# Note: Pydantic models defined here. Consider moving them to llm_gateway_core/models/config.py
# or similar for better separation if the models directory grows.
class SubscriptionQuotaConfig(BaseModel):
    kind: Literal["github_copilot", "gemini_cli", "antigravity"]
    token_env: str


class UpstreamRoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["round-robin", "fill-first", "priority"] = "round-robin"
    session_affinity: bool = False
    session_affinity_header: str = "X-Session-Id"
    session_affinity_ttl_seconds: int = 3600

    @field_validator("session_affinity_header", mode="before")
    @classmethod
    def validate_session_affinity_header(cls, value: Any) -> str:
        return _validate_header_name(value, "session_affinity_header") or "X-Session-Id"

    @field_validator("session_affinity_ttl_seconds")
    @classmethod
    def validate_session_affinity_ttl_seconds(cls, value: int) -> int:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("'session_affinity_ttl_seconds' must be a positive integer.")
        return parsed


class UpstreamKeySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    apikey: str
    priority: int = 0
    enabled: bool = True

    @field_validator("id", mode="before")
    @classmethod
    def validate_id(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized_value = _validate_non_empty_string(value, "id")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", normalized_value):
            raise ValueError("'id' must contain only letters, digits, dots, underscores, colons, and hyphens.")
        return normalized_value

    @field_validator("apikey", mode="before")
    @classmethod
    def validate_apikey(cls, value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("'apikey' must be a non-empty string.")
        if is_placeholder_secret(value):
            raise ValueError(placeholder_secret_error("upstream key apikey", value))
        return value

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, value: int) -> int:
        return int(value)


class UpstreamKeyPoolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["round-robin", "fill-first", "priority"] | None = None
    session_affinity: bool | None = None
    session_affinity_header: str | None = None
    session_affinity_ttl_seconds: int | None = None
    keys: list[UpstreamKeySpec]

    @field_validator("session_affinity_header", mode="before")
    @classmethod
    def validate_session_affinity_header(cls, value: Any) -> str | None:
        return _validate_header_name(value, "session_affinity_header")

    @field_validator("session_affinity_ttl_seconds")
    @classmethod
    def validate_session_affinity_ttl_seconds(cls, value: int | None) -> int | None:
        if value is None:
            return None
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("'session_affinity_ttl_seconds' must be a positive integer.")
        return parsed

    @field_validator("keys")
    @classmethod
    def validate_keys(cls, value: list[UpstreamKeySpec]) -> list[UpstreamKeySpec]:
        if not value:
            raise ValueError("'keys' must not be empty.")
        seen_ids: set[str] = set()
        for key_spec in value:
            if key_spec.id is None:
                continue
            if key_spec.id in seen_ids:
                raise ValueError("'keys' must not contain duplicate ids.")
            seen_ids.add(key_spec.id)
        return value


class ProviderDetails(BaseModel):
    baseUrl: str
    apikey: str | None = None
    # API dialect this provider speaks. ``openai`` — OpenAI-compatible
    # /chat/completions with Bearer auth. ``anthropic`` — native Anthropic
    # /v1/messages with ``x-api-key`` and ``anthropic-version`` headers.
    type: Literal["openai", "anthropic"] = "openai"
    proxy: str | None = None
    models: Any | None = None
    # Optional explicit list of model ids this provider serves. When set, the
    # gateway uses it instead of querying the upstream ``/models`` endpoint.
    # ``models`` stays reserved for per-model metadata (``upstream_limits`` etc.).
    available_models: list[str] | None = None
    subscription_quota: Optional[SubscriptionQuotaConfig] = None
    routing: UpstreamRoutingConfig = Field(default_factory=UpstreamRoutingConfig)
    upstream_key_pools: Dict[str, UpstreamKeyPoolConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def reject_provider_auth_config(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "auth" in value:
            auth_value = value.get("auth")
            auth_type = auth_value.get("type") if isinstance(auth_value, Mapping) else None
            suffix = f" (type: {auth_type})" if auth_type else ""
            raise ValueError(
                f"provider 'auth' config is no longer supported{suffix}; "
                "use 'apikey' or 'upstream_key_pools'."
            )
        return value

    @field_validator("baseUrl", mode="before")
    @classmethod
    def validate_base_url(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("'baseUrl' must be a non-empty string.")

        normalized_value = value.strip()
        if not normalized_value.startswith(("http://", "https://")):
            raise ValueError("'baseUrl' must start with 'http://' or 'https://'.")

        return normalized_value

    @field_validator("apikey", mode="before")
    @classmethod
    def validate_apikey(cls, value: Any) -> str:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("'apikey' must be a non-empty string.")
        if is_placeholder_secret(value):
            raise ValueError(placeholder_secret_error("provider apikey", value))

        return value

    @field_validator("upstream_key_pools")
    @classmethod
    def validate_upstream_key_pools(
        cls,
        value: Dict[str, UpstreamKeyPoolConfig],
    ) -> Dict[str, UpstreamKeyPoolConfig]:
        for pool_name in value:
            _validate_non_empty_string(pool_name, "upstream_key_pools key")
        return value

    @model_validator(mode="after")
    def validate_auth_material(self) -> "ProviderDetails":
        if self.apikey or self.upstream_key_pools:
            return self
        raise ValueError("provider must define either 'apikey' or 'upstream_key_pools'.")

class ProviderConfig(RootModel[Dict[str, ProviderDetails]]):
    """
    Represents a single entry in the providers.json list, 
    which is a dictionary with one key (provider name) and ProviderDetails as value.
    e.g., {"openai": {"baseUrl": "...", "apikey": "..."}}
    """
    @model_validator(mode='before')
    @classmethod
    def check_single_key_and_structure(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise ValueError("Provider entry must be a dictionary.")
        if len(data) != 1:
            raise ValueError("Provider entry dictionary must contain exactly one key (the provider name).")
        
        # Further validation of inner structure can be implicitly handled by Pydantic
        # when it tries to match Dict[str, ProviderDetails]
        # For example, the value associated with the key must match ProviderDetails structure.
        return data


class PayloadTransformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaults: Dict[str, Any] = Field(default_factory=dict)
    overrides: Dict[str, Any] = Field(default_factory=dict)
    filters: list[str] = Field(default_factory=list)

    @field_validator("defaults")
    @classmethod
    def validate_defaults(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return validate_payload_transform_object(value, "payload_transforms.defaults")

    @field_validator("overrides")
    @classmethod
    def validate_overrides(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return validate_payload_transform_object(value, "payload_transforms.overrides")

    @field_validator("filters")
    @classmethod
    def validate_filters(cls, value: list[str]) -> list[str]:
        return validate_payload_transform_filters(value, "payload_transforms.filters")


class FallbackModelRule(BaseModel):
    provider: str
    model: str
    upstream_key_pool: str | None = None
    use_provider_order_as_fallback: bool = False
    providers_order: Optional[List[str]] = None
    retry_delay: Optional[int] = None
    retry_count: Optional[int] = None
    custom_body_params: Dict[str, Any] = Field(default_factory=dict)
    custom_headers: Dict[str, Any] = Field(default_factory=dict)
    payload_transforms: PayloadTransformConfig | None = None

    @field_validator("custom_headers")
    @classmethod
    def validate_custom_headers(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        # Same denylist as OperationRoute: never let a rule inject auth/cookie/x-api-key
        # headers that could exfiltrate quota or hijack provider sessions.
        return _validate_custom_headers(value)

    @field_validator("custom_body_params")
    @classmethod
    def validate_custom_body_params(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return _validate_custom_body_params(value)

    @field_validator("providers_order")
    @classmethod
    def validate_providers_order(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return _validate_provider_name_list(value, "providers_order")

    @field_validator("upstream_key_pool", mode="before")
    @classmethod
    def validate_upstream_key_pool(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _validate_non_empty_string(value, "upstream_key_pool")

class ModelFallbackConfig(BaseModel):
    gateway_model_name: str
    fallback_models: List[FallbackModelRule]
    context_overflow_fallback: Optional[FallbackModelRule] = None
    rotate_models: bool = False
    dynamic_penalty: bool = False
    strip_think_tags: bool = False
    # Global attempt budget for the entire fallback chain. When set, the retry
    # counter is not reset when switching to a fallback model: once this many
    # attempts have been made across all providers in the chain, dispatch stops
    # and returns the last error. ``None`` preserves the legacy behavior where
    # each model burns its own per-model ``retry_count`` independently.
    max_total_attempts: Optional[int] = None

    compress_tool_results: bool = False

    @field_validator('rotate_models', 'dynamic_penalty', 'strip_think_tags', 'compress_tool_results', mode='before')
    def validate_rotate_models(cls, v):
        if isinstance(v, str):
            return v.lower() == 'true'
        return v

    @field_validator("max_total_attempts")
    @classmethod
    def validate_max_total_attempts(cls, value: int | None) -> int | None:
        validated_value = _validate_non_negative_number(value, "max_total_attempts")
        if validated_value is None:
            return None
        return int(validated_value)


class ModelPrefixRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefix: str
    target: str | None = None
    target_prefix: str | None = None

    @field_validator("prefix", mode="before")
    @classmethod
    def validate_prefix(cls, value: Any) -> str:
        return _validate_non_empty_string(value, "prefix")

    @field_validator("target", "target_prefix", mode="before")
    @classmethod
    def validate_targets(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _validate_non_empty_string(value, "target")

    @model_validator(mode="after")
    def validate_target_shape(self) -> "ModelPrefixRule":
        if bool(self.target) == bool(self.target_prefix):
            raise ValueError("Exactly one of 'target' or 'target_prefix' must be set for a prefix rule.")
        return self


class UpstreamModelPoolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fallback_models: List[FallbackModelRule]
    rotate_models: bool = False
    dynamic_penalty: bool = False
    strip_think_tags: bool = False
    compress_tool_results: bool = False
    max_total_attempts: Optional[int] = None
    context_overflow_fallback: Optional[FallbackModelRule] = None

    @field_validator("fallback_models")
    @classmethod
    def validate_fallback_models(cls, value: List[FallbackModelRule]) -> List[FallbackModelRule]:
        if not value:
            raise ValueError("'fallback_models' must not be empty.")
        return value

    @field_validator("rotate_models", "dynamic_penalty", "strip_think_tags", "compress_tool_results", mode="before")
    def validate_bool_fields(cls, value):
        if isinstance(value, str):
            return value.lower() == "true"
        return value

    @field_validator("max_total_attempts")
    @classmethod
    def validate_max_total_attempts(cls, value: int | None) -> int | None:
        validated_value = _validate_non_negative_number(value, "max_total_attempts")
        if validated_value is None:
            return None
        return int(validated_value)


class ModelRulesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aliases: Dict[str, str] = Field(default_factory=dict)
    prefixes: List[ModelPrefixRule] = Field(default_factory=list)
    excluded_models: List[str] = Field(default_factory=list)
    upstream_model_pools: Dict[str, UpstreamModelPoolConfig] = Field(default_factory=dict)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: Dict[str, str]) -> Dict[str, str]:
        normalized_aliases: dict[str, str] = {}
        for alias, target in value.items():
            normalized_aliases[
                _validate_non_empty_string(alias, "aliases key")
            ] = _validate_non_empty_string(target, "aliases target")
        return normalized_aliases

    @field_validator("excluded_models")
    @classmethod
    def validate_excluded_models(cls, value: List[str]) -> List[str]:
        normalized_values: list[str] = []
        seen_values: set[str] = set()
        for model_name in value:
            normalized_model_name = _validate_non_empty_string(model_name, "excluded_models")
            if normalized_model_name in seen_values:
                raise ValueError("'excluded_models' must not contain duplicate values.")
            seen_values.add(normalized_model_name)
            normalized_values.append(normalized_model_name)
        return normalized_values

    @field_validator("upstream_model_pools")
    @classmethod
    def validate_upstream_model_pool_names(
        cls,
        value: Dict[str, UpstreamModelPoolConfig],
    ) -> Dict[str, UpstreamModelPoolConfig]:
        for model_name in value:
            _validate_non_empty_string(model_name, "upstream_model_pools key")
        return value


FUSION_PANEL_MAX = 8


class FusionMember(BaseModel):
    """A single model taking part in a Fusion ensemble.

    References a configured provider plus its model id — the same shape as a
    fallback target — so the gateway itself can be used as a provider (self
    loopback) to reach gateway models, or any upstream provider can be called
    directly.
    """

    provider: str
    model: str
    temperature: float | None = None
    reasoning: Dict[str, Any] | None = None
    max_completion_tokens: int | None = None

    @field_validator("provider", "model", mode="before")
    @classmethod
    def _validate_non_empty(cls, value: Any, info) -> Any:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"'{info.field_name}' must be a non-empty string.")
        return value.strip()

    @field_validator("temperature")
    @classmethod
    def _validate_temperature(cls, value: float | None) -> float | None:
        if value is None:
            return None
        numeric = float(value)
        if not (0.0 <= numeric <= 2.0):
            raise ValueError("'temperature' must be between 0 and 2.")
        return numeric

    @field_validator("max_completion_tokens")
    @classmethod
    def _validate_max_completion_tokens(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if int(value) <= 0:
            raise ValueError("'max_completion_tokens' must be a positive integer.")
        return int(value)


class FusionWebToolsConfig(BaseModel):
    """Optional agentic web tools granted to Fusion *panel* members.

    When present, every panel member is run in a tool loop: it may call a
    ``web_search`` tool (and, when ``read_model`` is set, a ``web_fetch`` tool)
    before writing its answer. ``search_model`` / ``read_model`` reference
    gateway web-search / web-read operation models (``models_operation_rules``).
    The main and judge members never receive tools.
    """

    search_model: str
    read_model: Optional[str] = None
    max_tool_calls: int = 6
    max_iterations: int = 4
    max_results: int = 5

    @field_validator("search_model", mode="before")
    @classmethod
    def _validate_search_model(cls, value: Any) -> Any:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("'search_model' must be a non-empty string.")
        return value.strip()

    @field_validator("read_model", mode="before")
    @classmethod
    def _validate_read_model(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("'read_model' must be a non-empty string when provided.")
        return value.strip()

    @field_validator("max_tool_calls", "max_iterations", "max_results")
    @classmethod
    def _validate_positive(cls, value: int, info) -> int:
        if int(value) <= 0:
            raise ValueError(f"'{info.field_name}' must be a positive integer.")
        return int(value)


class FusionModelConfig(BaseModel):
    """A Fusion gateway model: a panel of models answers in parallel, a judge
    produces a structured analysis, and a main model writes the final answer."""

    gateway_model_name: str
    panel: List[FusionMember]
    main_model: FusionMember
    # Judge defaults to ``main_model`` when omitted.
    judge_model: Optional[FusionMember] = None
    include_details_default: bool = True
    # Optional agentic web tools for the panel members.
    web_tools: Optional[FusionWebToolsConfig] = None

    @field_validator("gateway_model_name", mode="before")
    @classmethod
    def _validate_gateway_model_name(cls, value: Any) -> Any:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("'gateway_model_name' must be a non-empty string.")
        return value.strip()

    @field_validator("panel")
    @classmethod
    def _validate_panel(cls, value: List[FusionMember]) -> List[FusionMember]:
        if not value:
            raise ValueError("Fusion 'panel' must contain at least one model.")
        if len(value) > FUSION_PANEL_MAX:
            raise ValueError(
                f"Fusion 'panel' must contain at most {FUSION_PANEL_MAX} models."
            )
        return value


class RouterTargetConfig(BaseModel):
    """One explicit destination a Router gateway model is allowed to choose."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["gateway_model", "fallback_entry"]
    model: str | None = None
    gateway_model: str | None = None
    index: int | None = None

    @field_validator("model", "gateway_model", mode="before")
    @classmethod
    def _validate_optional_non_empty(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("target model fields must be non-empty strings when provided.")
        return value.strip()

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: int | None) -> int | None:
        if value is None:
            return None
        parsed = int(value)
        if parsed < 0:
            raise ValueError("'index' must be greater than or equal to 0.")
        return parsed

    @model_validator(mode="after")
    def _validate_shape(self) -> "RouterTargetConfig":
        if self.type == "gateway_model":
            if not self.model:
                raise ValueError("gateway_model target requires 'model'.")
            if self.gateway_model is not None or self.index is not None:
                raise ValueError("gateway_model target must not set 'gateway_model' or 'index'.")
            return self

        if not self.gateway_model:
            raise ValueError("fallback_entry target requires 'gateway_model'.")
        if self.index is None:
            raise ValueError("fallback_entry target requires 'index'.")
        if self.model is not None:
            raise ValueError("fallback_entry target must not set 'model'.")
        return self


class RouterModelConfig(BaseModel):
    """A Router gateway model that asks a selector model to choose a target."""

    model_config = ConfigDict(extra="forbid")

    gateway_model_name: str
    selector_model: str
    targets: List[RouterTargetConfig]

    @field_validator("gateway_model_name", "selector_model", mode="before")
    @classmethod
    def _validate_non_empty(cls, value: Any, info) -> Any:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"'{info.field_name}' must be a non-empty string.")
        return value.strip()

    @field_validator("targets")
    @classmethod
    def _validate_targets(cls, value: List[RouterTargetConfig]) -> List[RouterTargetConfig]:
        if not value:
            raise ValueError("Router 'targets' must contain at least one target.")
        return value


class OperationRoute(BaseModel):
    provider: str
    model: str
    target_path: str
    voices_target_path: str | None = None
    custom_headers: Dict[str, Any] = Field(default_factory=dict)
    custom_body_params: Dict[str, Any] = Field(default_factory=dict)
    payload_transforms: PayloadTransformConfig | None = None
    request_format: str | None = None
    response_format: str | None = None
    response_output_format: str | None = None
    request_mapping: Dict[str, Any] | None = None
    response_mapping: Dict[str, Any] | None = None
    retry_delay: float | None = None
    retry_count: int | None = None

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        return _validate_non_empty_string(value, "model")

    @field_validator("target_path")
    @classmethod
    def validate_target_path(cls, value: str) -> str:
        return _validate_target_path(value)

    @field_validator("voices_target_path")
    @classmethod
    def validate_voices_target_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_target_path(value)

    @field_validator("request_format")
    @classmethod
    def validate_request_format(cls, value: str | None) -> str | None:
        return _validate_route_format(value, "request_format", SUPPORTED_REQUEST_FORMATS)

    @field_validator("response_format")
    @classmethod
    def validate_response_format(cls, value: str | None) -> str | None:
        return _validate_route_format(value, "response_format", SUPPORTED_RESPONSE_FORMATS)

    @field_validator("response_output_format")
    @classmethod
    def validate_response_output_format(cls, value: str | None) -> str | None:
        return _validate_route_format(
            value,
            "response_output_format",
            SUPPORTED_RESPONSE_OUTPUT_FORMATS,
        )

    @field_validator("retry_delay")
    @classmethod
    def validate_retry_delay(cls, value: float | None) -> float | None:
        validated_value = _validate_non_negative_number(value, "retry_delay")
        if validated_value is None:
            return None
        return float(validated_value)

    @field_validator("retry_count")
    @classmethod
    def validate_retry_count(cls, value: int | None) -> int | None:
        validated_value = _validate_non_negative_number(value, "retry_count")
        if validated_value is None:
            return None
        return int(validated_value)

    @field_validator("custom_headers")
    @classmethod
    def validate_custom_headers(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return _validate_custom_headers(value)

    @field_validator("custom_body_params")
    @classmethod
    def validate_custom_body_params(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return _validate_custom_body_params(value)

    @field_validator("request_mapping")
    @classmethod
    def validate_request_mapping(cls, value: Dict[str, Any] | None) -> Dict[str, Any] | None:
        return _validate_json_object(value, "request_mapping")

    @field_validator("response_mapping")
    @classmethod
    def validate_response_mapping(cls, value: Dict[str, Any] | None) -> Dict[str, Any] | None:
        return _validate_json_object(value, "response_mapping")


class EmbeddingsModelConfig(BaseModel):
    gateway_model_name: str
    routes: List[OperationRoute]


class RerankModelConfig(BaseModel):
    gateway_model_name: str
    routes: List[OperationRoute]

    @model_validator(mode="before")
    @classmethod
    def apply_default_target_path(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        routes = data.get("routes")
        if not isinstance(routes, list):
            return data

        updated_data = dict(data)
        updated_data["routes"] = [
            (
                {**route, "target_path": RERANK_DEFAULT_TARGET_PATH}
                if isinstance(route, dict) and "target_path" not in route
                else route
            )
            for route in routes
        ]
        return updated_data


class ImagesGenerationModelConfig(BaseModel):
    gateway_model_name: str
    routes: List[OperationRoute]


class ImagesEditModelConfig(BaseModel):
    gateway_model_name: str
    routes: List[OperationRoute]

    @model_validator(mode="before")
    @classmethod
    def apply_default_target_path(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        routes = data.get("routes")
        if not isinstance(routes, list):
            return data

        updated_data = dict(data)
        updated_data["routes"] = [
            (
                {**route, "target_path": IMAGES_EDITS_DEFAULT_TARGET_PATH}
                if isinstance(route, dict) and "target_path" not in route
                else route
            )
            for route in routes
        ]
        return updated_data


class AudioSpeechModelConfig(BaseModel):
    gateway_model_name: str
    routes: List[OperationRoute]

    @model_validator(mode="before")
    @classmethod
    def apply_default_target_path(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        routes = data.get("routes")
        if not isinstance(routes, list):
            return data

        updated_data = dict(data)
        updated_data["routes"] = [
            (
                {**route, "target_path": AUDIO_SPEECH_DEFAULT_TARGET_PATH}
                if isinstance(route, dict) and "target_path" not in route
                else route
            )
            for route in routes
        ]
        return updated_data


class AudioTranscriptionModelConfig(BaseModel):
    gateway_model_name: str
    routes: List[OperationRoute]

    @model_validator(mode="before")
    @classmethod
    def apply_default_target_path(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        routes = data.get("routes")
        if not isinstance(routes, list):
            return data

        updated_data = dict(data)
        updated_data["routes"] = [
            (
                {**route, "target_path": AUDIO_TRANSCRIPTIONS_DEFAULT_TARGET_PATH}
                if isinstance(route, dict) and "target_path" not in route
                else route
            )
            for route in routes
        ]
        return updated_data


class PdfConversionModelConfig(BaseModel):
    gateway_model_name: str
    routes: List[OperationRoute]

    @model_validator(mode="before")
    @classmethod
    def apply_default_target_path(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        routes = data.get("routes")
        if not isinstance(routes, list):
            return data

        updated_data = dict(data)
        updated_data["routes"] = [
            (
                {**route, "target_path": PDF_CONVERSIONS_DEFAULT_TARGET_PATH}
                if isinstance(route, dict) and "target_path" not in route
                else route
            )
            for route in routes
        ]
        return updated_data


class WebSearchModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gateway_model_name: str
    query_model: str | None = None


class WebReadModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gateway_model_name: str


class WebResearchModelConfig(BaseModel):
    gateway_model_name: str
    search_model: str
    read_model: str
    rerank_model: str
    analysis_model: str
    routes: List[OperationRoute] = Field(default_factory=list)


class WebDeepResearchModelConfig(BaseModel):
    gateway_model_name: str
    search_model: str
    read_model: str
    fast_model: str
    smart_model: str
    strategic_model: str
    embedding_model: str | None = None
    image_generation_model: str | None = None
    image_generation_size: str | None = None
    routes: List[OperationRoute] = Field(default_factory=list)


class _ModelsOperationConfigSections(BaseModel):
    model_config = ConfigDict(extra="forbid")

    embeddings: List[EmbeddingsModelConfig] = Field(default_factory=list)
    rerank: List[RerankModelConfig] = Field(default_factory=list)
    images_generations: List[ImagesGenerationModelConfig] = Field(default_factory=list)
    images_edits: List[ImagesEditModelConfig] = Field(default_factory=list)
    audio_speech: List[AudioSpeechModelConfig] = Field(default_factory=list)
    audio_transcriptions: List[AudioTranscriptionModelConfig] = Field(default_factory=list)
    web_search: List[WebSearchModelConfig] = Field(default_factory=list)
    web_read: List[WebReadModelConfig] = Field(default_factory=list)
    web_research: List[WebResearchModelConfig] = Field(default_factory=list)
    web_deep_research: List[WebDeepResearchModelConfig] = Field(default_factory=list)
    pdf_conversions: List[PdfConversionModelConfig] = Field(default_factory=list)


class ModelsOperationConfig(RootModel[_ModelsOperationConfigSections]):
    @property
    def embeddings(self) -> List[EmbeddingsModelConfig]:
        return self.root.embeddings

    @property
    def rerank(self) -> List[RerankModelConfig]:
        return self.root.rerank

    @property
    def images_generations(self) -> List[ImagesGenerationModelConfig]:
        return self.root.images_generations

    @property
    def images_edits(self) -> List[ImagesEditModelConfig]:
        return self.root.images_edits

    @property
    def audio_speech(self) -> List[AudioSpeechModelConfig]:
        return self.root.audio_speech

    @property
    def audio_transcriptions(self) -> List[AudioTranscriptionModelConfig]:
        return self.root.audio_transcriptions

    @property
    def web_search(self) -> List[WebSearchModelConfig]:
        return self.root.web_search

    @property
    def web_read(self) -> List[WebReadModelConfig]:
        return self.root.web_read

    @property
    def web_research(self) -> List[WebResearchModelConfig]:
        return self.root.web_research

    @property
    def web_deep_research(self) -> List[WebDeepResearchModelConfig]:
        return self.root.web_deep_research

    @property
    def pdf_conversions(self) -> List[PdfConversionModelConfig]:
        return self.root.pdf_conversions


class ConfigLoader:
    def __init__(
        self,
        providers_filename: str | None = None,
        fallback_rules_filename: str | None = None,
        operation_rules_filename: str | None = None,
        fusion_rules_filename: str | None = None,
        model_rules_filename: str | None = None,
        router_rules_filename: str | None = None,
    ):
        from .paths import PROJECT_ROOT
        self.project_root = PROJECT_ROOT

        # Use provided filename, or environment variable, or default
        p_file = providers_filename or os.getenv("PROVIDERS_FILENAME", "providers.json")
        f_file = fallback_rules_filename or os.getenv("FALLBACK_RULES_FILENAME", "models_fallback_rules.json")
        o_file = operation_rules_filename or os.getenv("OPERATION_RULES_FILENAME", "models_operation_rules.json")
        fusion_file = fusion_rules_filename or os.getenv("FUSION_RULES_FILENAME", "models_fusion_rules.json")
        model_file = model_rules_filename or os.getenv("MODEL_RULES_FILENAME", "models_model_rules.json")
        router_file = router_rules_filename or os.getenv("ROUTER_RULES_FILENAME", "models_router_rules.json")

        self.providers_path = Path(p_file) if os.path.isabs(p_file) else self.project_root / p_file
        self.fallback_rules_path = Path(f_file) if os.path.isabs(f_file) else self.project_root / f_file
        self.operation_rules_path = Path(o_file) if os.path.isabs(o_file) else self.project_root / o_file
        self.fusion_rules_path = Path(fusion_file) if os.path.isabs(fusion_file) else self.project_root / fusion_file
        self.model_rules_path = Path(model_file) if os.path.isabs(model_file) else self.project_root / model_file
        self.router_rules_path = Path(router_file) if os.path.isabs(router_file) else self.project_root / router_file

        self.providers_config: Dict[str, ProviderDetails] = {}
        self.fallback_rules: Dict[str, Dict[str, Any]] = {} # Store validated rules as dicts
        self._fallback_rules_base: Dict[str, Dict[str, Any]] = {}
        self.operation_rules: Dict[str, Dict[str, Any]] = _empty_operation_rules()
        self.fusion_rules: Dict[str, Dict[str, Any]] = {} # Store validated fusion configs as dicts
        self.model_rules: Dict[str, Any] = {}
        self.router_rules: Dict[str, Dict[str, Any]] = {} # Store validated router configs as dicts

    def _build_providers_config(self, raw_provider_list: Any) -> Dict[str, ProviderDetails]:
        if not isinstance(raw_provider_list, list):
            raise ValueError("Invalid format: Expected a list of provider objects.")

        providers_config_temp: Dict[str, ProviderDetails] = {}
        seen_provider_names: set[str] = set()
        for item_dict in raw_provider_list:
            validated_entry = ProviderConfig.model_validate(item_dict)
            provider_name = list(validated_entry.root.keys())[0]
            if provider_name in seen_provider_names:
                raise ValueError(
                    f"Duplicate provider '{provider_name}' found in providers.json. Provider names must be unique."
                )
            provider_details = validated_entry.root[provider_name]
            seen_provider_names.add(provider_name)
            providers_config_temp[provider_name] = provider_details

        return providers_config_temp

    def _build_fallback_rules_config(self, raw_rules: Any) -> Dict[str, Dict[str, Any]]:
        if not isinstance(raw_rules, list):
            raise ValueError("Invalid format: Expected a list of rule objects.")

        fallback_rules_temp: Dict[str, Dict[str, Any]] = {}
        seen_gateway_model_names: set[str] = set()
        for item in raw_rules:
            rule = ModelFallbackConfig(**item)
            if rule.gateway_model_name in seen_gateway_model_names:
                raise ValueError(
                    "Duplicate gateway_model_name "
                    f"'{rule.gateway_model_name}' found in models_fallback_rules.json. Gateway model names must be unique."
                )
            seen_gateway_model_names.add(rule.gateway_model_name)
            rule_config = {
                "fallback_models": [fm.model_dump(exclude_none=True) for fm in rule.fallback_models],
                "rotate_models": rule.rotate_models,
                "dynamic_penalty": rule.dynamic_penalty,
                "strip_think_tags": rule.strip_think_tags,
                "compress_tool_results": rule.compress_tool_results,
            }
            if rule.max_total_attempts is not None:
                rule_config["max_total_attempts"] = rule.max_total_attempts
            if rule.context_overflow_fallback is not None:
                rule_config["context_overflow_fallback"] = rule.context_overflow_fallback.model_dump(exclude_none=True)
            fallback_rules_temp[rule.gateway_model_name] = rule_config

        return fallback_rules_temp

    def _build_model_rules_config(self, raw_rules: Any) -> Dict[str, Any]:
        if raw_rules in (None, ""):
            return {}
        config = ModelRulesConfig.model_validate(raw_rules)
        model_rules = config.model_dump(exclude_none=True)
        return model_rules

    def _model_pool_fallback_rules(
        self,
        model_rules: Dict[str, Any],
        base_fallback_rules: Dict[str, Dict[str, Any]] | None = None,
    ) -> Dict[str, Dict[str, Any]]:
        pool_rules: dict[str, dict[str, Any]] = {}
        upstream_model_pools = model_rules.get("upstream_model_pools", {})
        if not isinstance(upstream_model_pools, dict):
            return pool_rules

        effective_base_rules = self.fallback_rules if base_fallback_rules is None else base_fallback_rules
        for gateway_model_name, pool_config in upstream_model_pools.items():
            if gateway_model_name in effective_base_rules:
                raise ValueError(
                    f"upstream_model_pools entry '{gateway_model_name}' conflicts with an existing fallback rule."
                )
            fallback_models = pool_config.get("fallback_models", [])
            pool_rule = {
                "fallback_models": fallback_models,
                "rotate_models": bool(pool_config.get("rotate_models", False)),
                "dynamic_penalty": bool(pool_config.get("dynamic_penalty", False)),
                "strip_think_tags": bool(pool_config.get("strip_think_tags", False)),
                "compress_tool_results": bool(pool_config.get("compress_tool_results", False)),
            }
            if pool_config.get("max_total_attempts") is not None:
                pool_rule["max_total_attempts"] = pool_config["max_total_attempts"]
            if pool_config.get("context_overflow_fallback") is not None:
                pool_rule["context_overflow_fallback"] = pool_config["context_overflow_fallback"]
            pool_rules[gateway_model_name] = pool_rule
        return pool_rules

    def _validate_model_rules_mapping(
        self,
        model_rules: Dict[str, Any],
        combined_fallback_rules: Dict[str, Dict[str, Any]],
    ) -> None:
        aliases = model_rules.get("aliases", {})
        if isinstance(aliases, dict):
            for alias, target in aliases.items():
                if alias in combined_fallback_rules:
                    raise ValueError(
                        f"model_rules alias '{alias}' conflicts with an existing fallback rule."
                    )
                if target not in combined_fallback_rules:
                    raise ValueError(
                        f"model_rules alias '{alias}' references unknown target model '{target}'."
                    )
                if is_model_excluded(alias, model_rules) or is_model_excluded(target, model_rules):
                    raise ValueError(
                        f"model_rules alias '{alias}' must not reference an excluded model."
                    )

        prefixes = model_rules.get("prefixes", [])
        if isinstance(prefixes, list):
            seen_prefixes: set[str] = set()
            for prefix_rule in prefixes:
                prefix = prefix_rule.get("prefix") if isinstance(prefix_rule, dict) else None
                if not isinstance(prefix, str):
                    continue
                if prefix in seen_prefixes:
                    raise ValueError(f"Duplicate model_rules prefix '{prefix}'.")
                seen_prefixes.add(prefix)
                target = prefix_rule.get("target")
                if target and target not in combined_fallback_rules:
                    raise ValueError(
                        f"model_rules prefix '{prefix}' references unknown target model '{target}'."
                    )

    def _build_fusion_rules_config(self, raw_rules: Any) -> Dict[str, Dict[str, Any]]:
        if not isinstance(raw_rules, list):
            raise ValueError("Invalid format: Expected a list of fusion model objects.")

        fusion_rules_temp: Dict[str, Dict[str, Any]] = {}
        seen_gateway_model_names: set[str] = set()
        for item in raw_rules:
            config = FusionModelConfig(**item)
            if config.gateway_model_name in seen_gateway_model_names:
                raise ValueError(
                    "Duplicate gateway_model_name "
                    f"'{config.gateway_model_name}' found in models_fusion_rules.json. "
                    "Gateway model names must be unique."
                )
            seen_gateway_model_names.add(config.gateway_model_name)
            data = config.model_dump(exclude_none=True)
            data.pop("gateway_model_name", None)
            fusion_rules_temp[config.gateway_model_name] = data

        return fusion_rules_temp

    def _build_router_rules_config(self, raw_rules: Any) -> Dict[str, Dict[str, Any]]:
        if raw_rules in (None, ""):
            raw_rules = []
        if not isinstance(raw_rules, list):
            raise ValueError("Invalid format: Expected a list of router model objects.")

        router_rules_temp: Dict[str, Dict[str, Any]] = {}
        seen_gateway_model_names: set[str] = set()
        for item in raw_rules:
            config = RouterModelConfig(**item)
            if config.gateway_model_name in seen_gateway_model_names:
                raise ValueError(
                    "Duplicate gateway_model_name "
                    f"'{config.gateway_model_name}' found in models_router_rules.json. "
                    "Gateway model names must be unique."
                )
            seen_gateway_model_names.add(config.gateway_model_name)
            data = config.model_dump(exclude_none=True)
            data.pop("gateway_model_name", None)
            router_rules_temp[config.gateway_model_name] = data

        return router_rules_temp

    def _build_operation_config(self, raw_rules: Any) -> Dict[str, Dict[str, Dict[str, Any]]]:
        operation_rules = ModelsOperationConfig.model_validate(raw_rules)
        operation_config_temp = _empty_operation_rules()

        for section_name, section_rules in (
            ("embeddings", operation_rules.embeddings),
            ("rerank", operation_rules.rerank),
            ("images_generations", operation_rules.images_generations),
            ("images_edits", operation_rules.images_edits),
            ("audio_speech", operation_rules.audio_speech),
            ("audio_transcriptions", operation_rules.audio_transcriptions),
            ("web_search", operation_rules.web_search),
            ("web_read", operation_rules.web_read),
            ("web_research", operation_rules.web_research),
            ("web_deep_research", operation_rules.web_deep_research),
            ("pdf_conversions", operation_rules.pdf_conversions),
        ):
            if section_name not in operation_config_temp:
                if not section_rules:
                    continue
                operation_config_temp[section_name] = {}

            section_config = operation_config_temp[section_name]
            seen_gateway_model_names: set[str] = set()
            for item in section_rules:
                if item.gateway_model_name in seen_gateway_model_names:
                    raise ValueError(
                        "Duplicate gateway_model_name "
                        f"'{item.gateway_model_name}' found in {section_name} operation routes. "
                        "Gateway model names must be unique within a section."
                    )

                seen_gateway_model_names.add(item.gateway_model_name)
                item_config = item.model_dump(exclude_none=True)
                routes_attr = getattr(item, "routes", None)
                if routes_attr is not None:
                    item_config["routes"] = [route.model_dump(exclude_none=True) for route in routes_attr]
                item_config.pop("gateway_model_name", None)
                section_config[item.gateway_model_name] = item_config

        return operation_config_temp

    def validate_fallback_rules_mapping(
        self,
        fallback_rules_to_validate: Dict[str, Dict[str, Any]],
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
    ) -> None:
        effective_providers = providers_config or self.providers_config
        if not effective_providers:
            raise ValueError("Providers must be loaded before validating fallback rules.")

        for gateway_model_name, config in fallback_rules_to_validate.items():
            fallback_models = config.get("fallback_models", [])
            if not fallback_models:
                raise ValueError(f"Gateway model '{gateway_model_name}' must have at least one fallback model defined.")

            for fallback_model_rule in fallback_models:
                self._validate_single_fallback_rule(
                    gateway_model_name,
                    fallback_model_rule,
                    effective_providers,
                )

            context_overflow_fallback = config.get("context_overflow_fallback")
            if context_overflow_fallback:
                self._validate_single_fallback_rule(
                    gateway_model_name,
                    context_overflow_fallback,
                    effective_providers,
                    rule_label="context_overflow_fallback",
                )

    def _validate_single_fallback_rule(
        self,
        gateway_model_name: str,
        fallback_model_rule: Dict[str, Any],
        effective_providers: Dict[str, ProviderDetails],
        rule_label: str = "fallback rule",
    ) -> None:
        provider = fallback_model_rule.get("provider")
        model = fallback_model_rule.get("model")

        if not provider:
            raise ValueError(f"'provider' is missing for a {rule_label} under '{gateway_model_name}'.")
        if not model:
            raise ValueError(
                f"'model' is missing for a {rule_label} under '{gateway_model_name}' (provider: {provider})."
            )
        if provider not in effective_providers:
            raise ValueError(
                f"Invalid provider '{provider}' used in {rule_label} for '{gateway_model_name}'. "
                "Provider not found in configuration."
            )
        provider_config = effective_providers[provider]
        upstream_key_pool = fallback_model_rule.get("upstream_key_pool")
        if upstream_key_pool:
            if upstream_key_pool not in provider_config.upstream_key_pools:
                raise ValueError(
                    f"Invalid upstream_key_pool '{upstream_key_pool}' used in {rule_label} "
                    f"for '{gateway_model_name}' (provider: {provider}). Pool not found in provider configuration."
                )
        elif provider_config.apikey is None and provider_config.upstream_key_pools:
            raise ValueError(
                f"'upstream_key_pool' is required for {rule_label} under '{gateway_model_name}' "
                f"because provider '{provider}' is configured with upstream_key_pools but no legacy apikey."
            )

        _validate_provider_name_list(
            fallback_model_rule.get("providers_order"),
            "providers_order",
        )

    def validate_fusion_rules_mapping(
        self,
        fusion_rules_to_validate: Dict[str, Dict[str, Any]],
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
    ) -> None:
        if not fusion_rules_to_validate:
            # No fusion models configured — nothing references providers, so skip validation.
            return

        effective_providers = providers_config or self.providers_config
        if not effective_providers:
            raise ValueError("Providers must be loaded before validating fusion rules.")

        for gateway_model_name, config in fusion_rules_to_validate.items():
            panel = config.get("panel", [])
            if not panel:
                raise ValueError(
                    f"Fusion model '{gateway_model_name}' must have at least one panel member."
                )
            if len(panel) > FUSION_PANEL_MAX:
                raise ValueError(
                    f"Fusion model '{gateway_model_name}' has {len(panel)} panel members; "
                    f"the maximum is {FUSION_PANEL_MAX}."
                )

            members: List[tuple[str, Dict[str, Any]]] = [("panel member", member) for member in panel]
            members.append(("main_model", config.get("main_model") or {}))
            judge_model = config.get("judge_model")
            if judge_model:
                members.append(("judge_model", judge_model))

            for role_label, member in members:
                provider = member.get("provider")
                if not provider:
                    raise ValueError(
                        f"'provider' is missing for a {role_label} under fusion model '{gateway_model_name}'."
                    )
                if not member.get("model"):
                    raise ValueError(
                        f"'model' is missing for a {role_label} under fusion model '{gateway_model_name}'."
                    )
                if provider not in effective_providers:
                    raise ValueError(
                        f"Invalid provider '{provider}' used in {role_label} for fusion model "
                        f"'{gateway_model_name}'. Provider not found in configuration."
                    )

    def validate_router_rules_mapping(
        self,
        router_rules_to_validate: Dict[str, Dict[str, Any]],
        fallback_rules: Optional[Dict[str, Dict[str, Any]]] = None,
        fusion_rules: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        if not router_rules_to_validate:
            return

        effective_fallback_rules = self.fallback_rules if fallback_rules is None else fallback_rules
        effective_fusion_rules = self.fusion_rules if fusion_rules is None else fusion_rules
        if not effective_fallback_rules:
            raise ValueError("Fallback rules must be loaded before validating router rules.")

        for router_model_name, config in router_rules_to_validate.items():
            if router_model_name in effective_fallback_rules:
                raise ValueError(
                    f"Router model '{router_model_name}' conflicts with an existing fallback rule."
                )
            if router_model_name in effective_fusion_rules:
                raise ValueError(
                    f"Router model '{router_model_name}' conflicts with an existing Fusion rule."
                )

            selector_model = config.get("selector_model")
            if selector_model not in effective_fallback_rules:
                raise ValueError(
                    f"Router model '{router_model_name}' references unknown selector_model "
                    f"'{selector_model}' (must be a gateway chat model in fallback rules)."
                )

            seen_target_ids: set[str] = set()
            for target in config.get("targets", []):
                target_type = target.get("type")
                if target_type == "gateway_model":
                    target_model = target.get("model")
                    target_id = f"gateway:{target_model}"
                    if target_model not in effective_fallback_rules:
                        raise ValueError(
                            f"Router model '{router_model_name}' references unknown target model "
                            f"'{target_model}' (must be a gateway chat model in fallback rules)."
                        )
                elif target_type == "fallback_entry":
                    target_gateway_model = target.get("gateway_model")
                    target_index = target.get("index")
                    target_id = f"fallback_entry:{target_gateway_model}:{target_index}"
                    target_rule = effective_fallback_rules.get(target_gateway_model)
                    if target_rule is None:
                        raise ValueError(
                            f"Router model '{router_model_name}' references unknown fallback_entry "
                            f"gateway_model '{target_gateway_model}'."
                        )
                    fallback_models = target_rule.get("fallback_models") or []
                    if not isinstance(target_index, int) or target_index >= len(fallback_models):
                        raise ValueError(
                            f"Router model '{router_model_name}' references fallback_entry "
                            f"'{target_gateway_model}' index {target_index}, but the fallback chain "
                            f"has {len(fallback_models)} entries."
                        )
                else:
                    raise ValueError(
                        f"Router model '{router_model_name}' has unsupported target type '{target_type}'."
                    )

                if target_id in seen_target_ids:
                    raise ValueError(
                        f"Router model '{router_model_name}' contains duplicate target '{target_id}'."
                    )
                seen_target_ids.add(target_id)

    def validate_operation_routes(
        self,
        operation_routes_to_validate: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
        fallback_rules: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        effective_operation_routes = (
            self.operation_rules if operation_routes_to_validate is None else operation_routes_to_validate
        )
        effective_providers = providers_config or self.providers_config
        if not effective_providers:
            raise ValueError("Providers must be loaded before validating operation routes.")
        effective_fallback_rules = (
            self.fallback_rules if fallback_rules is None else fallback_rules
        )

        for section_name, section_routes in effective_operation_routes.items():
            for gateway_model_name, config in section_routes.items():
                routes = config.get("routes", [])
                if not routes:
                    if section_name in {
                        "web_search",
                        "web_read",
                        "web_research",
                        "web_deep_research",
                    }:
                        continue
                    raise ValueError(
                        f"Gateway model '{gateway_model_name}' in '{section_name}' must have at least one route defined."
                    )

                for route in routes:
                    provider = route.get("provider")
                    if not provider:
                        raise ValueError(
                            f"'provider' is missing for an operation route under '{gateway_model_name}' in "
                            f"'{section_name}'."
                        )
                    if provider not in effective_providers:
                        raise ValueError(
                            f"Invalid provider '{provider}' used in operation route for '{gateway_model_name}' "
                            f"in '{section_name}'. Provider not found in configuration."
                        )

        web_search_models = set(effective_operation_routes.get("web_search", {}))
        web_read_models = set(effective_operation_routes.get("web_read", {}))
        rerank_models = set(effective_operation_routes.get("rerank", {}))
        embedding_models = set(effective_operation_routes.get("embeddings", {}))
        image_generation_models = set(effective_operation_routes.get("images_generations", {}))
        chat_models = set(effective_fallback_rules or {})
        for gateway_model_name, config in effective_operation_routes.get("web_research", {}).items():
            search_model = config.get("search_model") if isinstance(config, dict) else None
            read_model = config.get("read_model") if isinstance(config, dict) else None
            rerank_model = config.get("rerank_model") if isinstance(config, dict) else None
            analysis_model = config.get("analysis_model") if isinstance(config, dict) else None
            if search_model not in web_search_models:
                raise ValueError(
                    f"Web research model '{gateway_model_name}' references unknown search_model '{search_model}'."
                )
            if read_model not in web_read_models:
                raise ValueError(
                    f"Web research model '{gateway_model_name}' references unknown read_model '{read_model}'."
                )
            if rerank_model is not None and rerank_model not in rerank_models:
                raise ValueError(
                    f"Web research model '{gateway_model_name}' references unknown rerank_model "
                    f"'{rerank_model}' (must be a gateway rerank model)."
                )
            if analysis_model not in chat_models:
                raise ValueError(
                    f"Web research model '{gateway_model_name}' references unknown analysis_model "
                    f"'{analysis_model}' (must be a gateway chat model in fallback rules)."
                )
        for gateway_model_name, config in effective_operation_routes.get("web_deep_research", {}).items():
            search_model = config.get("search_model") if isinstance(config, dict) else None
            read_model = config.get("read_model") if isinstance(config, dict) else None
            fast_model = config.get("fast_model") if isinstance(config, dict) else None
            smart_model = config.get("smart_model") if isinstance(config, dict) else None
            strategic_model = config.get("strategic_model") if isinstance(config, dict) else None
            embedding_model = config.get("embedding_model") if isinstance(config, dict) else None
            image_generation_model = config.get("image_generation_model") if isinstance(config, dict) else None
            if search_model is not None and search_model not in web_search_models:
                raise ValueError(
                    f"Web deep research model '{gateway_model_name}' references unknown search_model '{search_model}'."
                )
            if read_model is not None and read_model not in web_read_models:
                raise ValueError(
                    f"Web deep research model '{gateway_model_name}' references unknown read_model '{read_model}'."
                )
            for field_name, value in (
                ("fast_model", fast_model),
                ("smart_model", smart_model),
                ("strategic_model", strategic_model),
            ):
                if value not in chat_models:
                    raise ValueError(
                        f"Web deep research model '{gateway_model_name}' references unknown "
                        f"{field_name} '{value}' (must be a gateway chat model in fallback rules)."
                    )
            if embedding_model is not None and embedding_model not in embedding_models:
                raise ValueError(
                    f"Web deep research model '{gateway_model_name}' references unknown embedding_model "
                    f"'{embedding_model}' (must be a gateway embeddings model)."
                )
            if image_generation_model is not None and image_generation_model not in image_generation_models:
                raise ValueError(
                    f"Web deep research model '{gateway_model_name}' references unknown image_generation_model "
                    f"'{image_generation_model}' (must be a gateway images_generations model)."
                )

    def validate_fallback_operation_consistency(
        self,
        fallback_rules: Optional[Dict[str, Dict[str, Any]]] = None,
        operation_rules: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    ) -> None:
        """Cross-validate that chat models from fallback rules are consistent
        with operation rules.

        Catches configuration bugs where a model referenced by an operation
        rule (e.g. web_research.analysis_model) is not defined as a chat model
        in the fallback rules, or where a chat model exists but has no fallback
        chain and is only used by operations — which could leave it unreachable
        for direct chat routing.
        """
        effective_fallback = fallback_rules or self.fallback_rules
        effective_operation = operation_rules or self.operation_rules

        chat_models = set(effective_fallback.keys())

        # All *analysis_model*, *fast_model*, *smart_model*, *strategic_model*
        # in operation rules must be defined in fallback (chat) rules.
        for section_name in ("web_research", "web_deep_research"):
            section = effective_operation.get(section_name, {})
            for gw_model, config in section.items():
                if not isinstance(config, dict):
                    continue
                for field in ("analysis_model", "fast_model", "smart_model", "strategic_model"):
                    value = config.get(field)
                    if value and value not in chat_models:
                        raise ValueError(
                            f"Operation rule '{section_name}.{gw_model}' references "
                            f"{field}='{value}', which is not defined in fallback (chat) rules."
                        )

    def parse_and_validate_providers_payload(
        self,
        payload_text: str,
        *,
        strict_env: bool = False,
    ) -> Dict[str, ProviderDetails]:
        raw_provider_list = json5.loads(payload_text)
        providers_config = self._build_providers_config(raw_provider_list)
        if not self._perform_provider_semantic_validation(
            providers_config,
            raise_on_error=strict_env,
            strict_env=strict_env,
        ):
            raise ValueError("Semantic validation failed for providers configuration.")
        return providers_config

    def parse_and_validate_fallback_rules_payload(
        self,
        payload_text: str,
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        raw_rules = json5.loads(payload_text)
        fallback_rules = self._build_fallback_rules_config(raw_rules)
        self.validate_fallback_rules_mapping(fallback_rules, providers_config=providers_config)
        return fallback_rules

    def parse_and_validate_model_rules_payload(
        self,
        payload_text: str,
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
        fallback_rules: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        raw_rules = json5.loads(payload_text)
        model_rules = self._build_model_rules_config(raw_rules)
        original_fallback_rules = self.fallback_rules
        try:
            if fallback_rules is not None:
                self.fallback_rules = fallback_rules
            base_fallback_rules = self.fallback_rules
            pool_rules = self._model_pool_fallback_rules(model_rules, base_fallback_rules)
            combined_fallback_rules = {**base_fallback_rules, **pool_rules}
            self.validate_fallback_rules_mapping(
                combined_fallback_rules,
                providers_config=providers_config,
            )
            self._validate_model_rules_mapping(model_rules, combined_fallback_rules)
            return model_rules
        finally:
            self.fallback_rules = original_fallback_rules

    def parse_and_validate_operation_routes_payload(
        self,
        payload_text: str,
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
        fallback_rules: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        raw_rules = json5.loads(payload_text)
        operation_routes = self._build_operation_config(raw_rules)
        self.validate_operation_routes(
            operation_routes,
            providers_config=providers_config,
            fallback_rules=fallback_rules,
        )
        return operation_routes

    def parse_and_validate_router_rules_payload(
        self,
        payload_text: str,
        fallback_rules: Optional[Dict[str, Dict[str, Any]]] = None,
        fusion_rules: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        raw_rules = json5.loads(payload_text)
        router_rules = self._build_router_rules_config(raw_rules)
        self.validate_router_rules_mapping(
            router_rules,
            fallback_rules=fallback_rules,
            fusion_rules=fusion_rules,
        )
        return router_rules

    def _resolve_operation_rules_path(self, filename: str = "models_operation_rules.json") -> Path:
        if filename == "models_operation_rules.json":
            return self.operation_rules_path
        return self.project_root / filename

    def _read_operation_rules_payload(self, operation_rules_path: Path) -> Any:
        with open(operation_rules_path, 'r', encoding='utf-8') as f:
            payload_text = f.read()

        if not payload_text.strip():
            return {}

        return json5.loads(payload_text)

    def load_providers(self) -> Dict[str, ProviderDetails]:
        """Loads and validates provider configurations from the JSON file."""

        if not self.providers_path.exists():
            message = f"Provider configuration file not found at {self.providers_path}"
            logging.error(message)
            raise ConfigError(message)

        try:
            with open(self.providers_path, 'r', encoding='utf-8') as f:
                raw_mapping = json5.load(f)

            providers_config_temp = self._build_providers_config(raw_mapping)

            self.providers_config = providers_config_temp
            if not self._perform_provider_semantic_validation(self.providers_config, raise_on_error=True):
                logging.critical("Provider semantic validation unexpectedly returned False during initial load.")
                raise ConfigError("Provider semantic validation failed during initial load.")

            logging.info(f"Successfully loaded and validated providers from {self.providers_path}")
            logging.info(f"Loaded providers: {list(self.providers_config.keys())}")
            return self.providers_config

        except ConfigError:
            raise
        except Exception as e:
            logging.error(f"Failed to load or validate '{self.providers_path.name}': {str(e)}", exc_info=True)
            raise ConfigError(f"Failed to load or validate '{self.providers_path.name}': {e}") from e

    def _perform_provider_semantic_validation(
        self,
        providers_to_validate: Dict[str, ProviderDetails],
        raise_on_error: bool = False,
        strict_env: bool = False,
    ) -> bool:
        """
        Performs semantic validation on a dictionary of provider configurations.
        Checks for fallback provider existence and API key environment variables.
        Returns True if all critical checks pass, False otherwise.
        If raise_on_error is True, raises ConfigError on critical failure.
        """
        all_valid = True
        fallback_provider_name = settings.fallback_provider
        if fallback_provider_name not in providers_to_validate:
            available_providers = ", ".join(sorted(providers_to_validate.keys())) or "<none>"
            message = (
                f"FALLBACK_PROVIDER '{fallback_provider_name}' is not defined in providers.json. "
                f"Available providers: {available_providers}."
            )
            logging.error(message)
            if raise_on_error:
                raise ConfigError(message)
            all_valid = False # Mark as invalid but continue checking other things if not raising

        env_errors: list[str] = []
        for provider_name, config in providers_to_validate.items():
            for field_name, field_value in (("apikey", config.apikey), ("proxy", config.proxy)):
                if not field_value:
                    continue

                try:
                    env_name = _explicit_env_reference_name(field_value, field_name)
                except ConfigError as exc:
                    message = str(exc)
                    logging.error(message)
                    env_errors.append(message)
                    continue

                if env_name is None:
                    if _looks_like_env_reference(field_value):
                        logging.warning(
                            "Provider '%s' field '%s' value '%s' looks like an environment variable name, "
                            "but '${VAR}' syntax was not used; treating it as a literal.",
                            provider_name,
                            field_name,
                            field_value,
                        )
                    continue

                resolved_env_value = os.getenv(env_name)
                if resolved_env_value:
                    if field_name == "apikey" and is_placeholder_secret(resolved_env_value):
                        message = placeholder_secret_error(env_name, resolved_env_value)
                        logging.error(message)
                        env_errors.append(message)
                    continue

                message = _provider_env_reference_error(provider_name, field_name, env_name)
                logging.error(message)
                env_errors.append(message)

            for pool_name, pool_config in config.upstream_key_pools.items():
                for key_index, key_spec in enumerate(pool_config.keys):
                    field_name = f"upstream_key_pools.{pool_name}.keys[{key_index}].apikey"
                    try:
                        env_name = _explicit_env_reference_name(key_spec.apikey, field_name)
                    except ConfigError as exc:
                        message = str(exc)
                        logging.error(message)
                        env_errors.append(message)
                        continue

                    if env_name is None:
                        if _looks_like_env_reference(key_spec.apikey):
                            logging.warning(
                                "Provider '%s' field '%s' value '%s' looks like an environment variable name, "
                                "but '${VAR}' syntax was not used; treating it as a literal.",
                                provider_name,
                                field_name,
                                key_spec.apikey,
                            )
                        continue

                    resolved_env_value = os.getenv(env_name)
                    if resolved_env_value:
                        if is_placeholder_secret(resolved_env_value):
                            message = placeholder_secret_error(env_name, resolved_env_value)
                            logging.error(message)
                            env_errors.append(message)
                        continue

                    message = _provider_env_reference_error(provider_name, field_name, env_name)
                    logging.error(message)
                    env_errors.append(message)

        if env_errors:
            if raise_on_error:
                raise ConfigError("; ".join(env_errors))
            all_valid = False

        return all_valid

    def _validate_providers(self):
        """Legacy wrapper for initial load validation. Raises ConfigError on failure."""
        if not self._perform_provider_semantic_validation(self.providers_config, raise_on_error=True):
            # This path should not normally be reachable (raise_on_error=True raises before returning False),
            # but keep a safeguard so a silent failure can't leak through.
            logging.critical("Provider semantic validation failed during initial load.")
            raise ConfigError("Provider semantic validation failed during initial load.")


    def load_model_rules(self) -> Dict[str, Any]:
        """Loads optional model alias/prefix/exclusion/upstream-pool rules."""
        if not self.model_rules_path.exists():
            self.model_rules = {}
            return {}

        try:
            with open(self.model_rules_path, 'r', encoding='utf-8') as f:
                raw_rules = json5.load(f)

            model_rules_temp = self._build_model_rules_config(raw_rules)
            base_fallback_rules = self._fallback_rules_base or self.fallback_rules
            pool_rules = self._model_pool_fallback_rules(model_rules_temp, base_fallback_rules)
            combined_fallback_rules = {**base_fallback_rules, **pool_rules}
            self.validate_fallback_rules_mapping(
                combined_fallback_rules,
                providers_config=self.providers_config,
            )
            self._validate_model_rules_mapping(model_rules_temp, combined_fallback_rules)
            self.fallback_rules = combined_fallback_rules
            self.model_rules = model_rules_temp
            logging.info(f"Successfully loaded model rules from {self.model_rules_path}")
            return self.model_rules

        except ConfigError:
            raise
        except Exception as e:
            logging.error(f"Failed to load or validate '{self.model_rules_path.name}': {str(e)}", exc_info=True)
            raise ConfigError(f"Failed to load or validate '{self.model_rules_path.name}': {e}") from e


    def load_fallback_rules(self) -> Dict[str, Dict[str, Any]]:
        """Loads and validates model fallback rules from the JSON file."""
        if not self.fallback_rules_path.exists():
            logging.warning(f"Model fallback rules file not found at {self.fallback_rules_path}. Proceeding without fallback rules.")
            return {}

        try:
            with open(self.fallback_rules_path, 'r', encoding='utf-8') as f:
                raw_rules = json5.load(f)

            fallback_rules_temp = self._build_fallback_rules_config(raw_rules)

            self.fallback_rules = fallback_rules_temp
            self._fallback_rules_base = fallback_rules_temp
            self._validate_fallback_rules() # Perform post-load validation
            logging.info(f"Successfully loaded and validated model fallback rules from {self.fallback_rules_path}")
            logging.info(f"Loaded model rules for: {list(self.fallback_rules.keys())}")
            return self.fallback_rules

        except ConfigError:
            raise
        except Exception as e:
            logging.error(f"Failed to load or validate '{self.fallback_rules_path.name}': {str(e)}", exc_info=True)
            raise ConfigError(f"Failed to load or validate '{self.fallback_rules_path.name}': {e}") from e

    def reload_fallback_rules(self) -> bool:
        """Reloads and validates model fallback rules from the JSON file.
        Returns True on success, False on failure."""
        if not self.fallback_rules_path.exists():
            logging.error(f"Model fallback rules file not found at {self.fallback_rules_path} during reload.")
            return False

        try:
            with open(self.fallback_rules_path, 'r', encoding='utf-8') as f:
                raw_rules = json5.load(f)

            fallback_rules_temp = self._build_fallback_rules_config(raw_rules)
            
            if not self.providers_config:
                 logging.warning("Providers not loaded. Loading providers before validating fallback rules reload.")
                 self.load_providers()

            self.validate_fallback_rules_mapping(fallback_rules_temp, providers_config=self.providers_config)

            # If all validations pass, update the actual instance rules
            self.fallback_rules = fallback_rules_temp
            self._fallback_rules_base = fallback_rules_temp
            if self.model_rules_path.exists():
                self.load_model_rules()
            logging.info(f"Successfully reloaded and validated model fallback rules from {self.fallback_rules_path}")
            logging.info(f"Reloaded model rules for: {list(self.fallback_rules.keys())}")
            return True

        except ValidationError as ve:
            logging.error(f"Validation error during reload of '{self.fallback_rules_path.name}': {ve.errors()}", exc_info=False) # No need for full stack for validation
            return False
        except Exception as e:
            logging.error(f"Failed to reload or validate '{self.fallback_rules_path.name}': {str(e)}", exc_info=True)
            return False

    def load_fusion_rules(self) -> Dict[str, Dict[str, Any]]:
        """Loads and validates Fusion model configs from the JSON file."""
        if not self.fusion_rules_path.exists():
            logging.info(
                f"Fusion rules file not found at {self.fusion_rules_path}. Proceeding without fusion models."
            )
            self.fusion_rules = {}
            return {}

        try:
            with open(self.fusion_rules_path, 'r', encoding='utf-8') as f:
                raw_rules = json5.load(f)

            fusion_rules_temp = self._build_fusion_rules_config(raw_rules)

            if not self.providers_config:
                logging.warning("Providers not loaded. Loading providers before validating fusion rules.")
                self.load_providers()

            self.validate_fusion_rules_mapping(fusion_rules_temp, providers_config=self.providers_config)
            self.fusion_rules = fusion_rules_temp
            logging.info(f"Successfully loaded and validated fusion rules from {self.fusion_rules_path}")
            logging.info(f"Loaded fusion models for: {list(self.fusion_rules.keys())}")
            return self.fusion_rules

        except ConfigError:
            raise
        except Exception as e:
            logging.error(f"Failed to load or validate '{self.fusion_rules_path.name}': {str(e)}", exc_info=True)
            raise ConfigError(f"Failed to load or validate '{self.fusion_rules_path.name}': {e}") from e

    def reload_fusion_rules(self) -> bool:
        """Reloads and validates Fusion model configs from the JSON file.
        Returns True on success, False on failure."""
        if not self.fusion_rules_path.exists():
            # A missing file simply means "no fusion models"; treat as an empty, valid config.
            self.fusion_rules = {}
            return True

        try:
            with open(self.fusion_rules_path, 'r', encoding='utf-8') as f:
                raw_rules = json5.load(f)

            fusion_rules_temp = self._build_fusion_rules_config(raw_rules)

            if not self.providers_config:
                logging.warning("Providers not loaded. Loading providers before validating fusion rules reload.")
                self.load_providers()

            self.validate_fusion_rules_mapping(fusion_rules_temp, providers_config=self.providers_config)
            self.fusion_rules = fusion_rules_temp
            logging.info(f"Successfully reloaded and validated fusion rules from {self.fusion_rules_path}")
            logging.info(f"Reloaded fusion models for: {list(self.fusion_rules.keys())}")
            return True

        except ValidationError as ve:
            logging.error(f"Validation error during reload of '{self.fusion_rules_path.name}': {ve.errors()}", exc_info=False)
            return False
        except Exception as e:
            logging.error(f"Failed to reload or validate '{self.fusion_rules_path.name}': {str(e)}", exc_info=True)
            return False

    def parse_and_validate_fusion_rules_payload(
        self,
        payload_text: str,
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        raw_rules = json5.loads(payload_text)
        fusion_rules = self._build_fusion_rules_config(raw_rules)
        self.validate_fusion_rules_mapping(fusion_rules, providers_config=providers_config)
        return fusion_rules

    def load_router_rules(self) -> Dict[str, Dict[str, Any]]:
        """Loads and validates Router model configs from the JSON file."""
        if not self.router_rules_path.exists():
            logging.info(
                f"Router rules file not found at {self.router_rules_path}. Proceeding without router models."
            )
            self.router_rules = {}
            return {}

        try:
            with open(self.router_rules_path, 'r', encoding='utf-8') as f:
                payload_text = f.read()

            if not payload_text.strip():
                payload_text = "[]"

            router_rules_temp = self.parse_and_validate_router_rules_payload(
                payload_text,
                fallback_rules=self.fallback_rules,
                fusion_rules=self.fusion_rules,
            )
            self.router_rules = router_rules_temp
            logging.info(f"Successfully loaded and validated router rules from {self.router_rules_path}")
            logging.info(f"Loaded router models for: {list(self.router_rules.keys())}")
            return self.router_rules

        except ConfigError:
            raise
        except Exception as e:
            logging.error(f"Failed to load or validate '{self.router_rules_path.name}': {str(e)}", exc_info=True)
            raise ConfigError(f"Failed to load or validate '{self.router_rules_path.name}': {e}") from e

    def reload_router_rules(self) -> bool:
        """Reloads and validates Router model configs from the JSON file.
        Returns True on success, False on failure."""
        if not self.router_rules_path.exists():
            self.router_rules = {}
            return True

        try:
            with open(self.router_rules_path, 'r', encoding='utf-8') as f:
                payload_text = f.read()

            if not payload_text.strip():
                payload_text = "[]"

            router_rules_temp = self.parse_and_validate_router_rules_payload(
                payload_text,
                fallback_rules=self.fallback_rules,
                fusion_rules=self.fusion_rules,
            )
            self.router_rules = router_rules_temp
            logging.info(f"Successfully reloaded and validated router rules from {self.router_rules_path}")
            logging.info(f"Reloaded router models for: {list(self.router_rules.keys())}")
            return True

        except ValidationError as ve:
            logging.error(f"Validation error during reload of '{self.router_rules_path.name}': {ve.errors()}", exc_info=False)
            return False
        except Exception as e:
            logging.error(f"Failed to reload or validate '{self.router_rules_path.name}': {str(e)}", exc_info=True)
            return False

    def reload_providers_config(self) -> bool:
        """
        Reloads and validates provider configurations from the providers.json file.
        Updates self.providers_config on success.
        Returns True on success, False on failure.
        """
        if not self.providers_path.exists():
            logging.error(f"Provider configuration file not found at {self.providers_path} during reload.")
            return False

        try:
            with open(self.providers_path, 'r', encoding='utf-8') as f:
                raw_provider_list = json5.load(f)

            potential_new_providers_config = self._build_providers_config(raw_provider_list)
            
            # Perform semantic validation on the successfully parsed and structurally validated providers
            if not self._perform_provider_semantic_validation(potential_new_providers_config, raise_on_error=False):
                logging.error(f"Semantic validation failed during reload of {self.providers_path.name}.")
                return False # Semantic validation failed (e.g., fallback provider missing)

            # If all validations pass, update the actual instance config
            self.providers_config = potential_new_providers_config
            logging.info(f"Successfully reloaded and validated providers from {self.providers_path}")
            logging.info(f"Reloaded providers: {list(self.providers_config.keys())}")
            return True

        except ValidationError as ve:
            logging.error(f"Validation error during reload of '{self.providers_path.name}': {ve.errors()}", exc_info=False)
            return False
        except Exception as e:
            logging.error(f"Failed to reload or validate '{self.providers_path.name}': {str(e)}", exc_info=True)
            return False

    def load_operation_rules(self, filename: str = "models_operation_rules.json") -> Dict[str, Dict[str, Any]]:
        """Loads and validates operation routing rules from the JSON file."""
        operation_rules_path = self._resolve_operation_rules_path(filename)
        self.operation_rules_path = operation_rules_path
        if not operation_rules_path.exists():
            logging.warning(
                "Model operation rules file not found at %s. Proceeding without operation rules.",
                operation_rules_path,
            )
            self.operation_rules = _empty_operation_rules()
            return self.operation_rules

        try:
            raw_rules = self._read_operation_rules_payload(operation_rules_path)
            operation_rules_temp = self._build_operation_config(raw_rules)

            self.operation_rules = operation_rules_temp
            self._validate_operation_rules()
            logging.info("Successfully loaded and validated model operation rules from %s", operation_rules_path)
            logging.info(
                "Loaded operation rules for embeddings: %s; rerank: %s; images_generations: %s; "
                "images_edits: %s; audio_speech: %s; audio_transcriptions: %s; web_search: %s; "
                "web_read: %s; web_research: %s; web_deep_research: %s; pdf_conversions: %s",
                list(self.operation_rules["embeddings"].keys()),
                list(self.operation_rules["rerank"].keys()),
                list(self.operation_rules["images_generations"].keys()),
                list(self.operation_rules["images_edits"].keys()),
                list(self.operation_rules.get("audio_speech", {}).keys()),
                list(self.operation_rules.get("audio_transcriptions", {}).keys()),
                list(self.operation_rules.get("web_search", {}).keys()),
                list(self.operation_rules.get("web_read", {}).keys()),
                list(self.operation_rules.get("web_research", {}).keys()),
                list(self.operation_rules.get("web_deep_research", {}).keys()),
                list(self.operation_rules.get("pdf_conversions", {}).keys()),
            )
            return self.operation_rules

        except ConfigError:
            raise
        except Exception as e:
            logging.error(f"Failed to load or validate '{operation_rules_path.name}': {str(e)}", exc_info=True)
            raise ConfigError(f"Failed to load or validate '{operation_rules_path.name}': {e}") from e

    def reload_operation_rules(self) -> bool:
        """Reloads and validates operation routing rules from the JSON file."""
        if not self.operation_rules_path.exists():
            logging.error(
                "Model operation rules file not found at %s during reload.",
                self.operation_rules_path,
            )
            return False

        try:
            raw_rules = self._read_operation_rules_payload(self.operation_rules_path)
            operation_rules_temp = self._build_operation_config(raw_rules)

            if not self.providers_config:
                logging.warning("Providers not loaded. Loading providers before validating operation rules reload.")
                self.load_providers()

            self.validate_operation_routes(operation_rules_temp, providers_config=self.providers_config)

            self.operation_rules = operation_rules_temp
            logging.info(
                "Successfully reloaded and validated model operation rules from %s",
                self.operation_rules_path,
            )
            logging.info(
                "Reloaded operation rules for embeddings: %s; rerank: %s; images_generations: %s; "
                "images_edits: %s; audio_speech: %s; audio_transcriptions: %s; web_search: %s; "
                "web_read: %s; web_research: %s; web_deep_research: %s; pdf_conversions: %s",
                list(self.operation_rules["embeddings"].keys()),
                list(self.operation_rules["rerank"].keys()),
                list(self.operation_rules["images_generations"].keys()),
                list(self.operation_rules["images_edits"].keys()),
                list(self.operation_rules.get("audio_speech", {}).keys()),
                list(self.operation_rules.get("audio_transcriptions", {}).keys()),
                list(self.operation_rules.get("web_search", {}).keys()),
                list(self.operation_rules.get("web_read", {}).keys()),
                list(self.operation_rules.get("web_research", {}).keys()),
                list(self.operation_rules.get("web_deep_research", {}).keys()),
                list(self.operation_rules.get("pdf_conversions", {}).keys()),
            )
            return True

        except ValidationError as ve:
            logging.error(
                "Validation error during reload of '%s': %s",
                self.operation_rules_path.name,
                ve.errors(),
                exc_info=False,
            )
            return False
        except Exception as e:
            logging.error(
                "Failed to reload or validate '%s': %s",
                self.operation_rules_path.name,
                str(e),
                exc_info=True,
            )
            return False

    def _validate_fallback_rules(self):
        """Performs post-load validation on fallback rules."""
        if not self.providers_config:
             # Ensure providers are loaded first if validation depends on them
             logging.warning("Providers not loaded yet. Loading providers before validating fallback rules.")
             self.load_providers()
             if not self.providers_config:
                 message = "Failed to load providers, cannot validate fallback rules."
                 logging.error(message)
                 raise ConfigError(message)
        try:
            self.validate_fallback_rules_mapping(self.fallback_rules, providers_config=self.providers_config)
        except ValueError as e:
            logging.error(str(e))
            raise ConfigError(str(e)) from e

    def _validate_operation_rules(self):
        """Performs post-load validation on operation rules."""
        if not self.providers_config:
            logging.warning("Providers not loaded yet. Loading providers before validating operation rules.")
            self.load_providers()
            if not self.providers_config:
                message = "Failed to load providers, cannot validate operation rules."
                logging.error(message)
                raise ConfigError(message)
        try:
            self.validate_operation_routes(providers_config=self.providers_config)
        except ValueError as e:
            logging.error(str(e))
            raise ConfigError(str(e)) from e
