import os
import math
from pydantic_settings import BaseSettings, SettingsConfigDict

from .environment import load_application_environment


load_application_environment()

IMAGE_UPLOAD_MAX_PARTS = 5
IMAGE_DATA_URL_OVERHEAD_BYTES = 64


def _max_image_materialization_peak_bytes(
    *,
    max_file_bytes: int,
    max_total_bytes: int,
) -> int:
    raw_total = min(max_total_bytes, IMAGE_UPLOAD_MAX_PARTS * max_file_bytes)
    part_count = min(IMAGE_UPLOAD_MAX_PARTS, raw_total)
    largest_part = min(max_file_bytes, raw_total - (part_count - 1))
    encoded_total = (
        4 * ((raw_total + (2 * part_count)) // 3)
        + (IMAGE_DATA_URL_OVERHEAD_BYTES * part_count)
    )
    largest_encoded = (
        4 * ((largest_part + 2) // 3)
        + IMAGE_DATA_URL_OVERHEAD_BYTES
    )
    return encoded_total + largest_part + largest_encoded


def _get_positive_int_env(var_name: str, default: int) -> int:
    raw_value = os.getenv(var_name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(
            f"key={var_name} reason=positive_integer_required"
        ) from None

    if value <= 0:
        raise ValueError(
            f"key={var_name} reason=positive_integer_required"
        ) from None

    return value


def _get_positive_float_env(var_name: str, default: float) -> float:
    raw_value = os.getenv(var_name)
    if raw_value is None:
        return default

    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        raise ValueError(
            f"key={var_name} reason=positive_finite_number_required"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"key={var_name} reason=positive_finite_number_required"
        ) from None
    return value


def _get_non_empty_str_env(var_name: str, default: str) -> str:
    raw_value = os.getenv(var_name)
    if raw_value is None:
        return default

    value = raw_value.strip()
    return value or default


def _get_bool_env(var_name: str, default: bool = False) -> bool:
    raw_value = os.getenv(var_name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}

class Settings(BaseSettings):
    model_config = SettingsConfigDict(hide_input_in_errors=True)

    # It's generally better practice to load .env once at the start
    # and access variables directly via os.getenv within the class definition
    # or use Pydantic's built-in .env file handling.
    # Sticking closer to original for now, but consider refactoring this.

    fallback_provider: str = _get_non_empty_str_env("FALLBACK_PROVIDER", "openrouter")
    gateway_api_key: str | None = os.getenv("GATEWAY_API_KEY")
    log_file_limit: int = _get_positive_int_env("LOG_FILE_LIMIT", 15) # Provide default directly
    gateway_port: int = _get_positive_int_env("GATEWAY_PORT", 9000)
    provider_injection_enabled: bool = os.getenv("PROVIDER_INJECTION_ENABLED", "true").lower() == "true"
    log_chat_messages: bool = os.getenv("LOG_CHAT_ENABLED", "false").lower() == "true"
    log_fallback_full_messages: bool = os.getenv("LOG_FALLBACK_FULL_MESSAGES", "false").lower() == "true"
    routing_diagnostic_headers: bool = _get_bool_env("ROUTING_DIAGNOSTIC_HEADERS", False)
    max_request_body_bytes: int = _get_positive_int_env("MAX_REQUEST_BODY_BYTES", 100 * 1024 * 1024)
    write_batcher_queue_maxsize: int = _get_positive_int_env(
        "WRITE_BATCHER_QUEUE_MAXSIZE",
        4096,
    )
    deep_research_max_children: int = _get_positive_int_env(
        "DEEP_RESEARCH_MAX_CHILDREN",
        1024,
    )
    stream_observation_buffer_max_bytes: int = _get_positive_int_env(
        "STREAM_OBSERVATION_BUFFER_MAX_BYTES",
        32 * 1024 * 1024,
    )
    stream_chunk_queue_maxsize: int = _get_positive_int_env(
        "STREAM_CHUNK_QUEUE_MAXSIZE",
        32,
    )
    stream_event_max_bytes: int = _get_positive_int_env(
        "STREAM_EVENT_MAX_BYTES",
        1024 * 1024,
    )
    json_response_max_bytes: int = _get_positive_int_env(
        "JSON_RESPONSE_MAX_BYTES",
        8 * 1024 * 1024,
    )
    json_response_inflight_max_bytes: int = _get_positive_int_env(
        "JSON_RESPONSE_INFLIGHT_MAX_BYTES",
        32 * 1024 * 1024,
    )
    audio_upload_max_bytes: int = _get_positive_int_env(
        "AUDIO_UPLOAD_MAX_BYTES",
        32 * 1024 * 1024,
    )
    image_upload_max_file_bytes: int = _get_positive_int_env(
        "IMAGE_UPLOAD_MAX_FILE_BYTES",
        16 * 1024 * 1024,
    )
    image_upload_max_total_bytes: int = _get_positive_int_env(
        "IMAGE_UPLOAD_MAX_TOTAL_BYTES",
        64 * 1024 * 1024,
    )
    pdf_upload_max_bytes: int = _get_positive_int_env(
        "PDF_UPLOAD_MAX_BYTES",
        80 * 1024 * 1024,
    )
    upload_inflight_max_bytes: int = _get_positive_int_env(
        "UPLOAD_INFLIGHT_MAX_BYTES",
        256 * 1024 * 1024,
    )
    upload_admission_timeout_seconds: float = _get_positive_float_env(
        "UPLOAD_ADMISSION_TIMEOUT_SECONDS",
        5.0,
    )
    deep_research_process_capacity: int = _get_positive_int_env(
        "DEEP_RESEARCH_PROCESS_CAPACITY",
        1,
    )
    deep_research_admission_timeout_seconds: float = _get_positive_float_env(
        "DEEP_RESEARCH_ADMISSION_TIMEOUT_SECONDS",
        5.0,
    )
    audio_decoded_max_bytes: int = _get_positive_int_env(
        "AUDIO_DECODED_MAX_BYTES",
        96 * 1024 * 1024,
    )

    def model_post_init(self, __context: object) -> None:
        if self.stream_event_max_bytes > self.stream_observation_buffer_max_bytes:
            raise ValueError(
                "STREAM_EVENT_MAX_BYTES must not exceed "
                "STREAM_OBSERVATION_BUFFER_MAX_BYTES"
            )
        if self.json_response_max_bytes > self.json_response_inflight_max_bytes:
            raise ValueError(
                "JSON_RESPONSE_MAX_BYTES must not exceed "
                "JSON_RESPONSE_INFLIGHT_MAX_BYTES"
            )
        upload_limits = {
            "AUDIO_UPLOAD_MAX_BYTES": self.audio_upload_max_bytes,
            "IMAGE_UPLOAD_MAX_TOTAL_BYTES": self.image_upload_max_total_bytes,
            "PDF_UPLOAD_MAX_BYTES": self.pdf_upload_max_bytes,
        }
        for name, value in upload_limits.items():
            if value > self.max_request_body_bytes:
                raise ValueError(f"{name} must not exceed MAX_REQUEST_BODY_BYTES")
        if self.image_upload_max_file_bytes > self.image_upload_max_total_bytes:
            raise ValueError(
                "IMAGE_UPLOAD_MAX_FILE_BYTES must not exceed "
                "IMAGE_UPLOAD_MAX_TOTAL_BYTES"
            )
        image_materialization_peak = _max_image_materialization_peak_bytes(
            max_file_bytes=self.image_upload_max_file_bytes,
            max_total_bytes=self.image_upload_max_total_bytes,
        )
        minimum_upload_capacity = max(
            self.image_upload_max_total_bytes,
            image_materialization_peak,
            self.pdf_upload_max_bytes,
            self.audio_upload_max_bytes + (2 * self.audio_decoded_max_bytes),
        )
        if self.upload_inflight_max_bytes < minimum_upload_capacity:
            raise ValueError(
                "UPLOAD_INFLIGHT_MAX_BYTES must cover the largest configured "
                "upload working set"
            )

    # Brute-force protection: block a client IP after repeated failed
    # authentications (key guessing). Counts consecutive auth_invalid rejections
    # per IP; a successful auth resets the counter.
    ip_block_enabled: bool = _get_bool_env("IP_BLOCK_ENABLED", True)
    ip_block_max_failures: int = _get_positive_int_env("IP_BLOCK_MAX_FAILURES", 5)
    ip_block_duration_minutes: int = _get_positive_int_env("IP_BLOCK_DURATION_MINUTES", 20)
    trusted_proxies: str = os.getenv("TRUSTED_PROXIES", "")
    session_cookie_secure: bool = _get_bool_env("SESSION_COOKIE_SECURE", True)

    # Add CORS settings
    cors_allow_origins_str: str | None = os.getenv("CORS_ALLOW_ORIGINS") # Load as string

    @property
    def cors_allow_origins(self) -> list[str] | None:
        """Parses the comma-separated CORS origins string into a list."""
        if self.cors_allow_origins_str:
            return [origin.strip() for origin in self.cors_allow_origins_str.split(",") if origin.strip()]
        return None # Return None if env var is not set or empty

    # Add debug mode setting
    debug_mode: bool = os.getenv("DEBUG_MODE", "false").lower() == "true"
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    gateway_host: str = os.getenv("GATEWAY_HOST", "0.0.0.0")

    # Startup verification of configured models against each provider's
    # /models endpoint. "off" skips the check; "warn" logs warnings for
    # missing models; "strict" aborts startup if any configured model is
    # missing (or a provider fails to answer).
    verify_models_on_startup: str = _get_non_empty_str_env(
        "VERIFY_MODELS_ON_STARTUP", "warn"
    ).lower()

    # Keys for built-in web_search/web_read adapters (Proxy → Tavily → Jina → Z.AI).
    # Tavily/Jina/Z.AI accept one key or comma-separated keys; order is fixed.
    proxy_url: str | None = os.getenv("PROXY_URL") or None
    tavily_api_key: str | None = os.getenv("TAVILY_API_KEY") or None
    jina_api_key: str | None = os.getenv("JINA_API_KEY") or None
    zai_api_key: str | None = os.getenv("ZAI_API_KEY") or None
    web_read_cloakbrowser_enabled: bool = _get_bool_env("WEB_READ_CLOAKBROWSER_ENABLED", False)
    web_read_cloakbrowser_no_sandbox: bool = _get_bool_env("WEB_READ_CLOAKBROWSER_NO_SANDBOX", False)

    # Free-tier LLM catalog (freellmapi.co): minimum monthly token budget floor
    # a model must clear to be shown on the /ui/free-models page, and a kill
    # switch for the periodic fetch (test environments disable the real HTTP call).
    free_llm_catalog_min_monthly_tokens: int = _get_positive_int_env(
        "FREE_LLM_CATALOG_MIN_MONTHLY_TOKENS", 30_000_000
    )
    free_llm_catalog_enabled: bool = _get_bool_env("FREE_LLM_CATALOG_ENABLED", True)

    # Kill switch for the periodic capability-autofill loop that fetches
    # provider /models catalogs to fill unset supports_vision/supports_tools/
    # context_window fields in fallback rules (test environments disable the
    # real HTTP call). Editor-save triggered autofill is not affected.
    capability_autofill_enabled: bool = _get_bool_env("CAPABILITY_AUTOFILL_ENABLED", True)

    # Example of Pydantic's .env handling (alternative approach)
    # class Config:
    #     env_file = '.env' # Relative to where the script is run
    #     env_file_encoding = 'utf-8'

# Create a single instance for the application to import
settings = Settings()
