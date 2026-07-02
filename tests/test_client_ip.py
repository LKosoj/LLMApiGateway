from types import SimpleNamespace
from unittest.mock import patch

from llm_gateway_core.utils.client_ip import get_client_ip


def _request(peer: str, headers: dict[str, str] | None = None):
    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers=headers or {},
    )


def test_untrusted_peer_cannot_spoof_x_forwarded_for():
    with patch("llm_gateway_core.utils.client_ip.settings.trusted_proxies", "10.0.0.1"):
        assert get_client_ip(_request("198.51.100.10", {"x-forwarded-for": "203.0.113.5"})) == "198.51.100.10"


def test_trusted_proxy_uses_first_valid_forwarded_for_ip():
    with patch("llm_gateway_core.utils.client_ip.settings.trusted_proxies", "10.0.0.0/8"):
        assert get_client_ip(_request("10.0.0.1", {"x-forwarded-for": "bad, 203.0.113.5, 10.0.0.2"})) == "203.0.113.5"


def test_trusted_proxy_uses_rightmost_untrusted_forwarded_for_ip():
    with patch("llm_gateway_core.utils.client_ip.settings.trusted_proxies", "10.0.0.0/8"):
        assert get_client_ip(_request("10.0.0.1", {"x-forwarded-for": "203.0.113.66, 198.51.100.9"})) == "198.51.100.9"


def test_invalid_trusted_proxy_config_falls_back_to_peer_ip():
    with patch("llm_gateway_core.utils.client_ip.settings.trusted_proxies", "not-a-network"):
        assert get_client_ip(_request("10.0.0.1", {"x-forwarded-for": "203.0.113.5"})) == "10.0.0.1"


def test_missing_peer_returns_none():
    request = SimpleNamespace(client=None, headers={})

    assert get_client_ip(request) is None
