"""Tests for performance optimizations (issues #5, #7, #8, #9)."""

import queue
import threading


class TestChunkQueuePutNowait:
    """#5: Verify queue.put_nowait works correctly for stream chunk logging."""

    def test_put_nowait_on_unbounded_queue(self):
        """put_nowait on an unbounded queue (maxsize=0) never raises Full."""
        q = queue.Queue(maxsize=0)
        for i in range(500):
            q.put_nowait(f"chunk_{i}".encode())
        assert q.qsize() == 500

    def test_put_nowait_is_consumed_by_thread(self):
        """Chunks enqueued via put_nowait are consumed by a background thread."""
        q = queue.Queue(maxsize=0)
        results = []

        def consumer():
            while True:
                item = q.get()
                if item is None:
                    break
                results.append(item)
                q.task_done()

        t = threading.Thread(target=consumer)
        t.start()

        for i in range(100):
            q.put_nowait(f"chunk_{i}")

        q.put_nowait(None)  # sentinel
        t.join(timeout=5)

        assert len(results) == 100
        assert results[0] == "chunk_0"
        assert results[-1] == "chunk_99"


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
        from main import create_shared_http_client, HTTP_CLIENT_MAX_CONNECTIONS
        import httpx
        client = create_shared_http_client()
        # Verify limits were passed (httpx stores them on the pool transport)
        transport = client._transport
        assert isinstance(transport, httpx.AsyncHTTPTransport)
        pool = transport._pool
        assert pool._max_connections == HTTP_CLIENT_MAX_CONNECTIONS

    def test_pool_limits_function(self):
        from main import _default_pool_limits, HTTP_CLIENT_MAX_CONNECTIONS, HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS
        limits = _default_pool_limits()
        assert limits.max_connections == HTTP_CLIENT_MAX_CONNECTIONS
        assert limits.max_keepalive_connections == HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS
