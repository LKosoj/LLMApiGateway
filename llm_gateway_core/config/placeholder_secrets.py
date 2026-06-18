PLACEHOLDER_SECRET_VALUES = frozenset(
    {
        "<thisgatewayapikey>",
        "thisgatewayapikey",
        "your-anthropic-api-key",
        "your-cloudru-api-key",
        "your-cohere-api-key",
        "your-google-api-key",
        "your-jina-api-key",
        "your-klusterai-api-key",
        "your-nebius-api-key",
        "your-nvidia-api-key",
        "your-openai-api-key",
        "your-openrouter-api-key",
        "your-requesty-api-key",
        "your-secure-api-key",
        "your-tavily-api-key",
        "your-together-api-key",
        "your-vsegpt-api-key",
        "your-xai-api-key",
        "your-zai-api-key",
    }
)


def is_placeholder_secret(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in PLACEHOLDER_SECRET_VALUES


def placeholder_secret_error(setting_name: str, value: str) -> str:
    return f"{setting_name} uses placeholder secret value {value!r}; replace it with a real secret."
