"""Integration tests for RTK Token Compression in the chat dispatch pipeline."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import main

from llm_gateway_core.services.token_compression import compress_messages


def _make_long_text(n: int = 600) -> str:
    return ("data line\n" * n)


# ---------------------------------------------------------------------------
# Unit-level integration: compress_messages called with enabled=True / False
# ---------------------------------------------------------------------------

def test_integration_enabled_compresses():
    """compress_tool_results=True causes tool result to be compressed."""
    text = _make_long_text()
    body = {"messages": [{"role": "tool", "content": text}]}
    stats = compress_messages(body, enabled=True)
    assert stats is not None
    assert stats.hits > 0
    assert body["messages"][0]["content"] != text


def test_integration_disabled_noop():
    """compress_tool_results=False leaves messages untouched."""
    text = _make_long_text()
    original_text = text
    body = {"messages": [{"role": "tool", "content": text}]}
    stats = compress_messages(body, enabled=False)
    assert stats is None
    assert body["messages"][0]["content"] == original_text


def test_integration_safe_by_design_exception():
    """If a filter raises an exception, original text is returned."""
    from llm_gateway_core.services.token_compression.apply_filter import safe_apply

    def broken_filter(text):
        raise RuntimeError("unexpected error")

    original = "x" * 200
    result = safe_apply(broken_filter, original)
    assert result == original


def test_integration_safe_by_design_size_growth():
    """If compressed result is larger, original text is returned."""
    from llm_gateway_core.services.token_compression.apply_filter import safe_apply

    def expanding_filter(text):
        return text + " " * 1000

    original = "hello world" * 20
    result = safe_apply(expanding_filter, original)
    assert result == original


def test_integration_compression_stats_properties():
    text = _make_long_text()
    body = {"messages": [{"role": "tool", "content": text}]}
    stats = compress_messages(body, enabled=True)
    assert stats is not None
    assert stats.saved_bytes > 0
    assert 0 < stats.saved_pct <= 100


def test_integration_multiple_tool_messages():
    """All tool messages in the list are compressed."""
    text = _make_long_text()
    body = {
        "messages": [
            {"role": "tool", "content": text},
            {"role": "tool", "content": text},
        ]
    }
    stats = compress_messages(body, enabled=True)
    assert stats is not None
    assert stats.hits >= 2


def test_integration_non_tool_untouched():
    """User and assistant messages are not modified."""
    tool_text = _make_long_text()
    user_text = "hello!"
    body = {
        "messages": [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": "response"},
            {"role": "tool", "content": tool_text},
        ]
    }
    compress_messages(body, enabled=True)
    assert body["messages"][0]["content"] == user_text
    assert body["messages"][1]["content"] == "response"


# ---------------------------------------------------------------------------
# Full path integration: ConfigLoader → fallback_rules → _dispatch_chat_request
#                        → compress_messages actually called
# ---------------------------------------------------------------------------

class DispatchCompressIntegrationTests(unittest.TestCase):
    """Verify that compress_messages is invoked when compress_tool_results is True
    in the fallback rule, using a TestClient that goes through _dispatch_chat_request."""

    def _make_fake_config_loader(self, compress: bool):
        fake = Mock()
        fake.providers_config = {
            "test-provider": SimpleNamespace(
                baseUrl="https://test.example",
                apikey="DIRECT-KEY",
            ),
        }
        fake.fallback_rules = {
            "compress-model": {
                "fallback_models": [
                    {"provider": "test-provider", "model": "real-model"},
                ],
                "rotate_models": False,
                "dynamic_penalty": False,
                "strip_think_tags": False,
                "compress_tool_results": compress,
            }
        }
        fake.load_providers.return_value = fake.providers_config
        fake.load_fallback_rules.return_value = fake.fallback_rules
        return fake

    @patch("llm_gateway_core.services.token_compression.compress_messages", wraps=compress_messages)
    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_compress_tool_results_true_calls_compress_messages(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
        compress_messages_spy,
    ):
        """When compress_tool_results=True, compress_messages must be called by _dispatch_chat_request."""
        config_loader_cls.return_value = self._make_fake_config_loader(compress=True)

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = ({"id": "ok", "choices": []}, None)

        tool_content = _make_long_text(700)
        payload = {
            "model": "compress-model",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "tool", "content": tool_content},
            ],
        }

        with patch.object(main.settings, "gateway_api_key", "test-key"):
            from fastapi.testclient import TestClient
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=payload,
                    headers={"Authorization": "Bearer test-key"},
                )

        self.assertEqual(response.status_code, 200)
        compress_messages_spy.assert_called_once()
        _args, _kwargs = compress_messages_spy.call_args
        # enabled=True must have been passed
        self.assertTrue(_kwargs.get("enabled", _args[1] if len(_args) > 1 else None))

    @patch("llm_gateway_core.services.token_compression.compress_messages", wraps=compress_messages)
    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_compress_tool_results_false_skips_compress_messages(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
        compress_messages_spy,
    ):
        """When compress_tool_results=False, compress_messages must NOT be called."""
        config_loader_cls.return_value = self._make_fake_config_loader(compress=False)

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = ({"id": "ok", "choices": []}, None)

        payload = {
            "model": "compress-model",
            "messages": [
                {"role": "user", "content": "hello"},
            ],
        }

        with patch.object(main.settings, "gateway_api_key", "test-key"):
            from fastapi.testclient import TestClient
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=payload,
                    headers={"Authorization": "Bearer test-key"},
                )

        self.assertEqual(response.status_code, 200)
        compress_messages_spy.assert_not_called()
