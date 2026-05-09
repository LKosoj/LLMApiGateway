import os
from dotenv import load_dotenv
from pydantic_settings import BaseSettings

# Load .env file from the project root (assuming run.py is in the root)
# Adjust the path if the execution context changes
dotenv_path = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
load_dotenv(dotenv_path=dotenv_path, override=False)


def _get_positive_int_env(var_name: str, default: int) -> int:
    raw_value = os.getenv(var_name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"{var_name} must be a positive integer, got: {raw_value!r}")

    if value <= 0:
        raise ValueError(f"{var_name} must be greater than 0, got: {value}")

    return value


def _get_non_empty_str_env(var_name: str, default: str) -> str:
    raw_value = os.getenv(var_name)
    if raw_value is None:
        return default

    value = raw_value.strip()
    return value or default

class Settings(BaseSettings):
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


    # Example of Pydantic's .env handling (alternative approach)
    # class Config:
    #     env_file = '.env' # Relative to where the script is run
    #     env_file_encoding = 'utf-8'

# Create a single instance for the application to import
settings = Settings()
