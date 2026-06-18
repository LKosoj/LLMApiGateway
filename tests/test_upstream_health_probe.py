from llm_gateway_core.api.v1.stats import (
    _build_health_probe_request,
    _resolve_health_probe_api_keys,
)
from llm_gateway_core.config.loader import ProviderDetails


def test_anthropic_health_probe_uses_x_api_key_header():
    provider_config = ProviderDetails(
        baseUrl="https://anthropic.example",
        type="anthropic",
        apikey="anthropic-key",
    )

    target_url, headers, payload = _build_health_probe_request(
        provider_config,
        "claude-model",
        "anthropic-key",
    )

    assert target_url == "https://anthropic.example/v1/messages"
    assert headers["x-api-key"] == "anthropic-key"
    assert "Authorization" not in headers
    assert payload["model"] == "claude-model"


def test_health_probe_uses_only_configured_upstream_key_pool():
    provider_config = ProviderDetails(
        baseUrl="https://provider.example",
        upstream_key_pools={
            "main": {
                "keys": [
                    {"id": "main-a", "apikey": "MAIN-A"},
                    {"id": "main-disabled", "apikey": "MAIN-DISABLED", "enabled": False},
                ]
            },
            "other": {
                "keys": [
                    {"id": "other-a", "apikey": "OTHER-A"},
                ]
            },
        },
    )

    assert _resolve_health_probe_api_keys(provider_config, "main") == ["MAIN-A"]
