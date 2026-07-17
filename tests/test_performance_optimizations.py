"""Tests for performance optimizations (issues #5, #7, #8, #9)."""

class TestTiktokenCache:
    """#7: Verify _resolve_encoding uses lru_cache."""

    def test_resolve_encoding_is_cached(self):
        from llm_gateway_core.utils.usage_tracking import _resolve_encoding

        assert hasattr(_resolve_encoding, "cache_info"), \
            "_resolve_encoding should be decorated with lru_cache"

        _resolve_encoding.cache_clear()

        enc1 = _resolve_encoding("gpt-4")
        enc2 = _resolve_encoding("gpt-4")
        assert enc1 is enc2

        info = _resolve_encoding.cache_info()
        assert info.hits >= 1

    def test_resolve_encoding_fallback_cached(self):
        from llm_gateway_core.utils.usage_tracking import _resolve_encoding

        _resolve_encoding.cache_clear()

        enc = _resolve_encoding("nonexistent-model-xyz-12345")
        assert enc is not None  # falls back to known encoding

        enc2 = _resolve_encoding("nonexistent-model-xyz-12345")
        assert enc is enc2

    def test_resolve_encoding_none_cached(self):
        from llm_gateway_core.utils.usage_tracking import _resolve_encoding

        _resolve_encoding.cache_clear()

        enc1 = _resolve_encoding(None)
        enc2 = _resolve_encoding(None)
        assert enc1 is enc2


class TestChatCountTokensTiktokenCache:
    """Verify Anthropic count-tokens tiktoken resolver is cached."""

    def test_resolve_tiktoken_encoding_is_cached(self):
        from llm_gateway_core.api.v1.chat import _resolve_tiktoken_encoding

        assert hasattr(_resolve_tiktoken_encoding, "cache_info"), \
            "_resolve_tiktoken_encoding should be decorated with lru_cache"

        _resolve_tiktoken_encoding.cache_clear()

        enc1 = _resolve_tiktoken_encoding("gpt-4")
        enc2 = _resolve_tiktoken_encoding("gpt-4")

        assert enc1 is enc2
        assert _resolve_tiktoken_encoding.cache_info().hits >= 1


class TestImagePathPartsCache:
    """Verify image adapter path parsing is cached."""

    def test_iter_path_parts_is_cached(self):
        from llm_gateway_core.api.v1.image_adapters import _iter_path_parts

        assert hasattr(_iter_path_parts, "cache_info"), \
            "_iter_path_parts should be decorated with lru_cache"

        _iter_path_parts.cache_clear()

        parts1 = _iter_path_parts("data.items[0].url")
        parts2 = _iter_path_parts("data.items[0].url")

        assert parts1 == ("data", "items", 0, "url")
        assert parts1 is parts2
        assert _iter_path_parts.cache_info().hits >= 1


class TestWriteLogCleanupInterval:
    """#8: Verify log cleanup runs periodically, not on every write."""

    def test_cleanup_interval_constant_exists(self):
        from llm_gateway_core.middleware.chat_logging import _LOG_CLEANUP_INTERVAL
        assert _LOG_CLEANUP_INTERVAL > 1


class TestHttpxPoolLimits:
    """#9: Verify httpx connection pool limits are explicitly configured."""

    def test_shared_client_has_explicit_limits(self):
        import httpx
        from llm_gateway_core.services.http_client_factory import (
            HTTP_CLIENT_MAX_CONNECTIONS,
            create_shared_http_client,
        )

        client = create_shared_http_client()
        # Verify limits were passed (httpx stores them on the pool transport)
        transport = client._transport
        assert isinstance(transport, httpx.AsyncHTTPTransport)
        pool = transport._pool
        assert pool._max_connections == HTTP_CLIENT_MAX_CONNECTIONS

    def test_pool_limits_function(self):
        from llm_gateway_core.services.http_client_factory import (
            HTTP_CLIENT_MAX_CONNECTIONS,
            HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS,
            _default_pool_limits,
        )

        limits = _default_pool_limits()
        assert limits.max_connections == HTTP_CLIENT_MAX_CONNECTIONS
        assert limits.max_keepalive_connections == HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS
