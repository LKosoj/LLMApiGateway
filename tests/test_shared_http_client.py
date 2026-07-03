import asyncio
import copy
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import main
from tests._async_compat import run_async
from llm_gateway_core.agents.deep_research import DeepResearchManager
from llm_gateway_core.services.request_handler import make_llm_request


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes], status_code: int = 200):
        self._chunks = chunks
        self.status_code = status_code

    async def aread(self):
        return b""

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeStreamContextManager:
    def __init__(self, response: _FakeStreamResponse):
        self._response = response
        self.exited = False

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return False


async def _collect_streaming_response_body(response: StreamingResponse) -> bytes:
    body = []
    async for chunk in response.body_iterator:
        body.append(chunk)
    return b"".join(body)


async def _make_stream_request_and_collect(
    fake_client: Mock,
    request_payload: dict,
) -> tuple[StreamingResponse | None, str | None, bytes | None]:
    response_data, error_detail = await make_llm_request(
        fake_client,
        "https://example.com/chat/completions",
        {"Content-Type": "application/json"},
        request_payload,
        True,
    )

    collected = None
    if isinstance(response_data, StreamingResponse):
        collected = await _collect_streaming_response_body(response_data)

    return response_data, error_detail, collected


class SharedHttpClientTests(unittest.TestCase):
    def test_run_usage_stats_cleanup_loop_deletes_records_older_than_90_days(self):
        fake_tokens_usage_db = Mock()
        fake_fallback_events_db = Mock()
        fake_rejections_db = Mock()

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        async def scenario():
            with patch("asyncio.to_thread", side_effect=fake_to_thread):
                cleanup_task = asyncio.create_task(
                    main.run_usage_stats_cleanup_loop(
                        fake_tokens_usage_db,
                        fake_fallback_events_db,
                        fake_rejections_db,
                        retention_days=90,
                        interval_seconds=3600,
                    )
                )
                await asyncio.sleep(0)
                fake_tokens_usage_db.cleanup_old_records.assert_called_once_with(retention_days=90)
                fake_fallback_events_db.cleanup_old_records.assert_called_once_with(retention_days=90)
                fake_rejections_db.cleanup_old_records.assert_called_once_with(retention_days=90)
                cleanup_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await cleanup_task

        run_async(scenario())

    def test_run_budget_reset_loop_resets_due_keys_and_syncs_ledger(self):
        reset_record = Mock(id=42, budget_usd=10.0, spent_usd=0.0)
        fake_api_keys_db = Mock()
        fake_api_keys_db.reset_due_budgets.return_value = [reset_record]
        fake_ledger = Mock()

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        async def scenario():
            with patch("asyncio.to_thread", side_effect=fake_to_thread):
                reset_task = asyncio.create_task(
                    main.run_budget_reset_loop(
                        fake_api_keys_db,
                        fake_ledger,
                        interval_seconds=3600,
                    )
                )
                await asyncio.sleep(0)
                fake_api_keys_db.reset_due_budgets.assert_called_once_with()
                fake_ledger.reset_record.assert_called_once_with(
                    42, budget_usd=10.0, spent_usd=0.0
                )
                reset_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await reset_task

        run_async(scenario())

    def test_create_shared_http_client_configures_finite_timeouts(self):
        http_client = main.create_shared_http_client()
        self.assertEqual(http_client.timeout.connect, main.HTTP_CLIENT_CONNECT_TIMEOUT_SECONDS)
        self.assertEqual(main.HTTP_CLIENT_READ_TIMEOUT_SECONDS, 500.0)
        self.assertEqual(http_client.timeout.read, main.HTTP_CLIENT_READ_TIMEOUT_SECONDS)
        self.assertEqual(http_client.timeout.write, main.HTTP_CLIENT_WRITE_TIMEOUT_SECONDS)
        self.assertEqual(http_client.timeout.pool, main.HTTP_CLIENT_POOL_TIMEOUT_SECONDS)
        run_async(http_client.aclose())

    def test_resolve_write_batcher_db_path_ignores_mock_db_path(self):
        fake_tokens_usage_db = Mock()

        resolved = main.resolve_write_batcher_db_path(fake_tokens_usage_db)

        self.assertEqual(resolved, main.PROJECT_ROOT / "db" / "tokens_usage.db")

    def test_resolve_write_batcher_db_path_accepts_real_path(self):
        fake_tokens_usage_db = Mock()
        fake_tokens_usage_db.db_path = Path("/tmp/custom-tokens-usage.db")

        resolved = main.resolve_write_batcher_db_path(fake_tokens_usage_db)

        self.assertEqual(resolved, Path("/tmp/custom-tokens-usage.db"))

    @patch.object(main.ConfigLoader, "load_providers")
    @patch.object(main.ConfigLoader, "load_fallback_rules")
    @patch.object(main.ConfigLoader, "load_model_rules")
    @patch.object(main.ConfigLoader, "load_fusion_rules")
    @patch.object(main.ConfigLoader, "load_router_rules")
    @patch.object(main.ConfigLoader, "load_operation_rules")
    @patch("main.start_usage_stats_cleanup_task")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    def test_lifespan_creates_and_closes_shared_http_client_once(
        self,
        async_client_ctor,
        _tokens_usage_db,
        start_usage_stats_cleanup_task,
        _load_operation_rules,
        _load_router_rules,
        _load_fusion_rules,
        _load_model_rules,
        _load_fallback_rules,
        _load_providers,
    ):
        class _FakeCleanupTask:
            def __init__(self):
                self.cancel_called = False

            def cancel(self):
                self.cancel_called = True

            def __await__(self):
                async def _done():
                    return None

                return _done().__await__()

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        fake_cleanup_task = _FakeCleanupTask()
        start_usage_stats_cleanup_task.return_value = fake_cleanup_task

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                self.assertIs(client.app.state.http_client, fake_http_client)
                self.assertEqual(
                    client.app.state.operation_rules,
                    {
                        "embeddings": {},
                        "rerank": {},
                        "images_generations": {},
                        "images_edits": {},
                        "audio_speech": {},
                        "audio_transcriptions": {},
                        "pdf_conversions": {},
                        "web_search": {},
                        "web_read": {},
                        "web_research": {},
                        "web_deep_research": {},
                    },
                )
                self.assertEqual(
                    client.app.state.write_batcher._db_path,
                    main.PROJECT_ROOT / "db" / "tokens_usage.db",
                )
                async_client_ctor.assert_called_once()
                # start_usage_stats_cleanup_task is called with tokens_usage_db and fallback_events_db
                start_usage_stats_cleanup_task.assert_called_once()
                call_args = start_usage_stats_cleanup_task.call_args
                self.assertIs(call_args[0][0], _tokens_usage_db.return_value)
                _load_operation_rules.assert_called_once()
                _load_fusion_rules.assert_called_once()
                _load_router_rules.assert_called_once()
                _load_model_rules.assert_called_once()

        self.assertTrue(fake_cleanup_task.cancel_called)
        fake_http_client.aclose.assert_awaited_once()

    def test_make_llm_request_uses_injected_client(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        fake_client.post = AsyncMock(return_value=_FakeResponse({"id": "response-id"}))
        request_payload = {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]}
        original_payload = copy.deepcopy(request_payload)

        with patch("llm_gateway_core.services.request_handler.httpx.AsyncClient", side_effect=AssertionError("AsyncClient constructor must not be used")):
            response_data, error_detail = run_async(
                make_llm_request(
                    fake_client,
                    "https://example.com/chat/completions",
                    {"Content-Type": "application/json"},
                    request_payload,
                    False,
                )
            )

        self.assertEqual(response_data, {"id": "response-id"})
        self.assertIsNone(error_detail)
        self.assertEqual(request_payload, original_payload)
        fake_client.post.assert_awaited_once()
        self.assertEqual(fake_client.post.await_args.args[0], "https://example.com/chat/completions")
        self.assertEqual(fake_client.post.await_args.kwargs["headers"], {"Content-Type": "application/json"})
        self.assertEqual(fake_client.post.await_args.kwargs["json"], request_payload)
        self.assertNotIn("content", fake_client.post.await_args.kwargs)
        self.assertNotIn("timeout", fake_client.post.await_args.kwargs)

    def test_make_llm_request_stream_uses_client_defaults_without_timeout_override(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse(
            [b'data: {"id":"chunk-1","choices":[{"delta":{"content":"hello"}}]}\n\n']
        )
        fake_client.stream = Mock(return_value=_FakeStreamContextManager(stream_response))
        request_payload = {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]}
        original_payload = copy.deepcopy(request_payload)

        response_data, error_detail = run_async(
            make_llm_request(
                fake_client,
                "https://example.com/chat/completions",
                {"Content-Type": "application/json"},
                request_payload,
                True,
            )
        )

        self.assertIsInstance(response_data, StreamingResponse)
        self.assertIsNone(error_detail)
        self.assertEqual(request_payload, original_payload)
        fake_client.stream.assert_called_once()
        self.assertNotIn("timeout", fake_client.stream.call_args.kwargs)

    def test_make_llm_request_stream_without_usage_finishes_cleanly(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse(
            [
                b'data: {"id":"chunk-1","choices":[{"delta":{"content":"hello"}}]}\n\n',
                b"data: [DONE]\n\n",
            ]
        )
        fake_client.stream = Mock(return_value=_FakeStreamContextManager(stream_response))

        response_data, error_detail, collected = run_async(
            _make_stream_request_and_collect(
                fake_client,
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
            )
        )

        self.assertIsInstance(response_data, StreamingResponse)
        self.assertIsNone(error_detail)
        self.assertEqual(
            collected,
            b'data: {"id":"chunk-1","choices":[{"delta":{"content":"hello"}}]}\n\n'
            b"data: [DONE]\n\n",
        )

    def test_make_llm_request_stream_with_only_done_returns_error(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse([b"data: [DONE]\n\n"])
        stream_context = _FakeStreamContextManager(stream_response)
        fake_client.stream = Mock(return_value=stream_context)

        response_data, error_detail, _ = run_async(
            _make_stream_request_and_collect(
                fake_client,
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(error_detail, "Stream ended before any content chunks were received.")
        self.assertTrue(stream_context.exited)

    def test_make_llm_request_stream_with_only_role_chunk_returns_error(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse(
            [
                b'data: {"id":"chunk-1","choices":[{"delta":{"role":"assistant"}}]}\n\n',
                b"data: [DONE]\n\n",
            ]
        )
        stream_context = _FakeStreamContextManager(stream_response)
        fake_client.stream = Mock(return_value=stream_context)

        response_data, error_detail, _ = run_async(
            _make_stream_request_and_collect(
                fake_client,
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(error_detail, "Stream ended before any content chunks were received.")
        self.assertTrue(stream_context.exited)

    def test_make_llm_request_closes_stream_context_on_downstream_stream_error_status(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse([], status_code=400)
        stream_response.aread = AsyncMock(return_value=b"boom")
        stream_context = _FakeStreamContextManager(stream_response)
        fake_client.stream = Mock(return_value=stream_context)

        response_data, error_detail = run_async(
            make_llm_request(
                fake_client,
                "https://example.com/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
                True,
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(error_detail, "boom")
        self.assertTrue(stream_context.exited)

    def test_make_llm_request_stream_preserves_keepalive_before_first_data(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse(
            [
                b": ping\n\n",
                b'data: {"id":"chunk-1","choices":[{"delta":{"content":"hello"}}]}\n\n',
                b"data: [DONE]\n\n",
            ]
        )
        fake_client.stream = Mock(return_value=_FakeStreamContextManager(stream_response))

        response_data, error_detail, collected = run_async(
            _make_stream_request_and_collect(
                fake_client,
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
            )
        )

        self.assertIsInstance(response_data, StreamingResponse)
        self.assertIsNone(error_detail)
        self.assertTrue(collected.startswith(b": ping\n\n"))
        self.assertIn(b'"content":"hello"', collected)

    def test_make_llm_request_stream_with_only_keepalive_returns_error(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse([b": ping\n\n"])
        stream_context = _FakeStreamContextManager(stream_response)
        fake_client.stream = Mock(return_value=stream_context)

        response_data, error_detail, _ = run_async(
            _make_stream_request_and_collect(
                fake_client,
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(error_detail, "Stream ended before any content chunks were received.")
        self.assertTrue(stream_context.exited)

    def test_make_llm_request_stream_stops_after_error_chunk_without_code(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse(
            [
                b'data: {"id":"chunk-1","choices":[{"delta":{"content":"hello"}}]}\n\n',
                b'data: {"error":{"message":"boom"}}\n\n',
                b'data: {"id":"chunk-2","choices":[{"delta":{"content":"after-error"}}]}\n\n',
            ]
        )
        fake_client.stream = Mock(return_value=_FakeStreamContextManager(stream_response))

        response_data, error_detail, collected = run_async(
            _make_stream_request_and_collect(
                fake_client,
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
            )
        )

        self.assertIsInstance(response_data, StreamingResponse)
        self.assertIsNone(error_detail)
        self.assertIn(b'"content":"hello"', collected)
        self.assertIn(b'"error":{"message":"boom"}', collected)
        self.assertNotIn(b"after-error", collected)

    def test_make_llm_request_returns_error_on_provider_timeout(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        fake_client.post = AsyncMock(
            side_effect=httpx.ReadTimeout(
                "provider timed out",
                request=httpx.Request("POST", "https://example.com/chat/completions"),
            )
        )

        response_data, error_detail = run_async(
            make_llm_request(
                fake_client,
                "https://example.com/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
                False,
            )
        )

        self.assertIsNone(response_data)
        self.assertIsNotNone(error_detail)
        self.assertIn("ReadTimeout connecting to https://example.com/chat/completions", error_detail)
        self.assertIn("provider timed out", error_detail)

    def test_make_llm_request_preserves_timeout_type_when_exception_message_is_empty(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        fake_client.post = AsyncMock(
            side_effect=httpx.ReadTimeout(
                "",
                request=httpx.Request("POST", "https://example.com/chat/completions"),
            )
        )

        response_data, error_detail = run_async(
            make_llm_request(
                fake_client,
                "https://example.com/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
                False,
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(
            error_detail,
            "ReadTimeout connecting to https://example.com/chat/completions",
        )


class DeepResearchWorkerHttpClientTests(unittest.TestCase):
    def test_conduct_deep_research_passes_http_client_to_install_gateway_image_generator(self):
        """The call site in conduct_deep_research must always pass http_client to
        _install_gateway_image_generator when image generation is enabled.
        This ensures GatewayImageGenerator reuses the per-worker client instead of
        creating a new one per image generation call."""
        install_calls: list[dict] = []

        def fake_install(researcher, model, *, http_client=None):
            install_calls.append({"http_client": http_client})

        class _FakeResearcher:
            env_snapshot = {}
            image_provider = None
            context = ["context"]
            available_images = []
            sources = []
            source_urls = []
            costs = None

            def __init__(self, *, query, report_type, verbose):
                self.image_generator = _FakeImageGenerator()

            async def conduct_research(self):
                return "result"

            async def write_report(self):
                return "report"

            def _generate_research_id(self):
                return "rid"

        class _FakeImageGenerator:
            image_provider = None

            def is_enabled(self):
                return True

            async def plan_and_generate_images(self, **kwargs):
                return [{"url": "/outputs/images/rid/img.png", "prompt": "p", "alt_text": "a"}]

        class _FakeManager(DeepResearchManager):
            def _get_researcher_factory(self):
                return _FakeResearcher

        with patch("llm_gateway_core.agents.deep_research._install_gateway_image_generator", side_effect=fake_install):
            run_async(
                _FakeManager().conduct_deep_research(
                    query="test",
                    fast_model="model",
                    smart_model="model",
                    strategic_model="model",
                    image_generation_enabled=True,
                    image_generation_model="img-model",
                    image_generation_size="512x512",
                )
            )

        self.assertEqual(len(install_calls), 1, "Expected exactly one call to _install_gateway_image_generator")
        self.assertIsInstance(
            install_calls[0]["http_client"],
            httpx.AsyncClient,
            "_install_gateway_image_generator must receive a live httpx.AsyncClient, not None",
        )


if __name__ == "__main__":
    unittest.main()
