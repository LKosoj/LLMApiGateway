import json5
import logging
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, RootModel, model_validator

# Import settings using relative path within the package
from .settings import settings
from ..utils.api_keys import select_next_api_key


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
class ProviderDetails(BaseModel):
    baseUrl: str
    apikey: str
    # API dialect this provider speaks. ``openai`` — OpenAI-compatible
    # /chat/completions with Bearer auth. ``anthropic`` — native Anthropic
    # /v1/messages with ``x-api-key`` and ``anthropic-version`` headers.
    type: Literal["openai", "anthropic"] = "openai"
    proxy: str | None = None
    models: Any | None = None

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
        if not isinstance(value, str) or not value.strip():
            raise ValueError("'apikey' must be a non-empty string.")

        return value

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

class FallbackModelRule(BaseModel):
    provider: str
    model: str
    use_provider_order_as_fallback: bool = False
    providers_order: Optional[List[str]] = None
    retry_delay: Optional[int] = None
    retry_count: Optional[int] = None
    custom_body_params: Dict[str, Any] = Field(default_factory=dict)
    custom_headers: Dict[str, Any] = Field(default_factory=dict)

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

class ModelFallbackConfig(BaseModel):
    gateway_model_name: str
    fallback_models: List[FallbackModelRule]
    context_overflow_fallback: Optional[FallbackModelRule] = None
    rotate_models: bool = False
    strip_think_tags: bool = False
    # Global attempt budget for the entire fallback chain. When set, the retry
    # counter is not reset when switching to a fallback model: once this many
    # attempts have been made across all providers in the chain, dispatch stops
    # and returns the last error. ``None`` preserves the legacy behavior where
    # each model burns its own per-model ``retry_count`` independently.
    max_total_attempts: Optional[int] = None

    @field_validator('rotate_models', 'strip_think_tags', mode='before')
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


class OperationRoute(BaseModel):
    provider: str
    model: str
    target_path: str
    voices_target_path: str | None = None
    custom_headers: Dict[str, Any] = Field(default_factory=dict)
    custom_body_params: Dict[str, Any] = Field(default_factory=dict)
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
    ):
        from .paths import PROJECT_ROOT
        self.project_root = PROJECT_ROOT
        
        # Use provided filename, or environment variable, or default
        p_file = providers_filename or os.getenv("PROVIDERS_FILENAME", "providers.json")
        f_file = fallback_rules_filename or os.getenv("FALLBACK_RULES_FILENAME", "models_fallback_rules.json")
        o_file = operation_rules_filename or os.getenv("OPERATION_RULES_FILENAME", "models_operation_rules.json")

        self.providers_path = Path(p_file) if os.path.isabs(p_file) else self.project_root / p_file
        self.fallback_rules_path = Path(f_file) if os.path.isabs(f_file) else self.project_root / f_file
        self.operation_rules_path = Path(o_file) if os.path.isabs(o_file) else self.project_root / o_file

        self.providers_config: Dict[str, ProviderDetails] = {}
        self.fallback_rules: Dict[str, Dict[str, Any]] = {} # Store validated rules as dicts
        self.operation_rules: Dict[str, Dict[str, Any]] = _empty_operation_rules()

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
                "strip_think_tags": rule.strip_think_tags,
            }
            if rule.max_total_attempts is not None:
                rule_config["max_total_attempts"] = rule.max_total_attempts
            if rule.context_overflow_fallback is not None:
                rule_config["context_overflow_fallback"] = rule.context_overflow_fallback.model_dump(exclude_none=True)
            fallback_rules_temp[rule.gateway_model_name] = rule_config

        return fallback_rules_temp

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

        _validate_provider_name_list(
            fallback_model_rule.get("providers_order"),
            "providers_order",
        )

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

                if os.getenv(env_name):
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
            logging.info(f"Successfully reloaded and validated model fallback rules from {self.fallback_rules_path}")
            logging.info(f"Reloaded model rules for: {list(self.fallback_rules.keys())}")
            return True

        except ValidationError as ve:
            logging.error(f"Validation error during reload of '{self.fallback_rules_path.name}': {ve.errors()}", exc_info=False) # No need for full stack for validation
            return False
        except Exception as e:
            logging.error(f"Failed to reload or validate '{self.fallback_rules_path.name}': {str(e)}", exc_info=True)
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
