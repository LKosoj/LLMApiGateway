import math
import re
from typing import Any, Dict, List, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

from .placeholder_secrets import is_placeholder_secret, placeholder_secret_error
from .schema_validation import (
    AUDIO_SPEECH_DEFAULT_TARGET_PATH,
    AUDIO_TRANSCRIPTIONS_DEFAULT_TARGET_PATH,
    IMAGES_EDITS_DEFAULT_TARGET_PATH,
    PDF_CONVERSIONS_DEFAULT_TARGET_PATH,
    RERANK_DEFAULT_TARGET_PATH,
    SUPPORTED_REQUEST_FORMATS,
    SUPPORTED_RESPONSE_FORMATS,
    SUPPORTED_RESPONSE_OUTPUT_FORMATS,
    validate_custom_body_params as _validate_custom_body_params,
    validate_custom_headers as _validate_custom_headers,
    validate_header_name as _validate_header_name,
    validate_json_object as _validate_json_object,
    validate_non_empty_string as _validate_non_empty_string,
    validate_non_negative_number as _validate_non_negative_number,
    validate_provider_name_list as _validate_provider_name_list,
    validate_route_format as _validate_route_format,
    validate_target_path as _validate_target_path,
)
from ..services.payload_transform import (
    validate_payload_transform_filters,
    validate_payload_transform_object,
)

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
    # Capability metadata used by the chat dispatch capability guard to
    # prefilter fallback-chain candidates. ``None``/absent means "no data" and
    # never filters a candidate out.
    supports_vision: Optional[bool] = None
    supports_tools: Optional[bool] = None
    context_window: Optional[int] = None
    # Names of the capability fields above that F-auto (capability_autofill
    # service) currently owns. Only fields listed here (or unset fields) may
    # be overwritten by the resolver; a manual edit removes the field's name
    # from this list, making it permanently hands-off for autofill.
    capabilities_autofilled: Optional[
        List[Literal["supports_vision", "supports_tools", "context_window"]]
    ] = None

    @field_validator("custom_headers")
    @classmethod
    def validate_custom_headers(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        # Same denylist as OperationRoute: never let a rule inject auth/cookie/x-api-key
        # headers that could exfiltrate quota or hijack provider sessions.
        return _validate_custom_headers(value)

    @field_validator("context_window")
    @classmethod
    def validate_context_window(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        if int(value) <= 0:
            raise ValueError("'context_window' must be greater than 0.")
        return int(value)

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
    tool_call_rescue: bool = False

    @field_validator(
        'rotate_models', 'dynamic_penalty', 'strip_think_tags', 'compress_tool_results', 'tool_call_rescue',
        mode='before',
    )
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
    tool_call_rescue: bool = False
    max_total_attempts: Optional[int] = None
    context_overflow_fallback: Optional[FallbackModelRule] = None

    @field_validator("fallback_models")
    @classmethod
    def validate_fallback_models(cls, value: List[FallbackModelRule]) -> List[FallbackModelRule]:
        if not value:
            raise ValueError("'fallback_models' must not be empty.")
        return value

    @field_validator(
        "rotate_models", "dynamic_penalty", "strip_think_tags", "compress_tool_results", "tool_call_rescue",
        mode="before",
    )
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
    # Optional standby panel members used to refill a failed panel slot.
    reserve: List[FusionMember] = Field(default_factory=list)

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

    @field_validator("reserve")
    @classmethod
    def _validate_reserve(cls, value: List[FusionMember]) -> List[FusionMember]:
        if len(value) > FUSION_PANEL_MAX:
            raise ValueError(
                f"A Fusion reserve can have at most {FUSION_PANEL_MAX} models."
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


class OperationCostCalculatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit: Literal["operation"]
    rate_usd: float

    @field_validator("rate_usd", mode="before")
    @classmethod
    def validate_rate_usd(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("'rate_usd' must be a finite non-negative number.")
        try:
            normalized_value = float(value)
        except (OverflowError, TypeError, ValueError):
            raise ValueError("'rate_usd' must be a finite non-negative number.") from None
        if not math.isfinite(normalized_value) or normalized_value < 0:
            raise ValueError("'rate_usd' must be a finite non-negative number.")
        return normalized_value


class _TokenPricedOperationModelConfig(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def reject_operation_cost_calculator(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "cost_calculator" in data:
            raise ValueError("Token-priced operation models do not support 'cost_calculator'.")
        return data


class EmbeddingsModelConfig(_TokenPricedOperationModelConfig):
    gateway_model_name: str
    routes: List[OperationRoute]


class RerankModelConfig(_TokenPricedOperationModelConfig):
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
    cost_calculator: OperationCostCalculatorConfig | None = None
    routes: List[OperationRoute]


class ImagesEditModelConfig(BaseModel):
    gateway_model_name: str
    cost_calculator: OperationCostCalculatorConfig | None = None
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
    cost_calculator: OperationCostCalculatorConfig | None = None
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
    cost_calculator: OperationCostCalculatorConfig | None = None
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
    cost_calculator: OperationCostCalculatorConfig | None = None
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
    cost_calculator: OperationCostCalculatorConfig | None = None
    query_model: str | None = None


class WebReadModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gateway_model_name: str
    cost_calculator: OperationCostCalculatorConfig | None = None


class WebResearchModelConfig(BaseModel):
    gateway_model_name: str
    cost_calculator: OperationCostCalculatorConfig | None = None
    search_model: str
    read_model: str
    rerank_model: str
    analysis_model: str
    routes: List[OperationRoute] = Field(default_factory=list)


class WebDeepResearchModelConfig(BaseModel):
    gateway_model_name: str
    cost_calculator: OperationCostCalculatorConfig | None = None
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
