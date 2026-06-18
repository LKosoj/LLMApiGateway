from unittest.mock import patch

from llm_gateway_core.api.v1.stats import (
    _build_health_probe_request,
    _resolve_health_probe_api_keys,
)
from llm_gateway_core.config.loader import ProviderDetails


def test_anthropic_oauth_health_probe_uses_bearer_header():
    provider_config = ProviderDetails(
        baseUrl="https://anthropic.example",
        type="anthropic",
        auth={"type": "claude_oauth", "token_env": "CLAUDE_OAUTH_TOKEN"},
    )

    with patch.dict("os.environ", {"CLAUDE_OAUTH_TOKEN": "oauth-token"}):
        target_url, headers, payload = _build_health_probe_request(
            provider_config,
            "claude-model",
            "oauth-token",
        )

    assert target_url == "https://anthropic.example/v1/messages"
    assert headers["Authorization"] == "Bearer oauth-token"
    assert "x-api-key" not in headers
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
