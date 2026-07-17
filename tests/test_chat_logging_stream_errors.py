import asyncio
import gzip
import json
import unittest
from contextlib import ExitStack, asynccontextmanager
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from llm_gateway_core.middleware import chat_logging
from llm_gateway_core.middleware.response_observation import (
    ResponseObservationMiddleware,
)
from llm_gateway_core.middleware.runtime_snapshot import RuntimeSnapshotMiddleware
from llm_gateway_core.services.stream_observation import StreamObservationCapacity
from tests._async_compat import run_async
from tests.chat_accounting_test_support import install_legacy_chat_logging_passthrough
from tests.runtime_test_support import installed_runtime, make_app_services


_CONFIG_LOADER = SimpleNamespace(fallback_rules={})
_COST_RATE_REGISTRY = MappingProxyType({})


def _install_chat_logging(app: FastAPI) -> None:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with installed_runtime(_app):
            yield

    app.router.lifespan_context = lifespan
    app.add_middleware(
        ResponseObservationMiddleware,
        request_preparer=chat_logging.prepare_chat_response_observation,
    )
    app.add_middleware(RuntimeSnapshotMiddleware)


class ChatLoggingStreamErrorsTests(unittest.TestCase):
    def setUp(self):
        self._accounting_stack = ExitStack()
        self.addCleanup(self._accounting_stack.close)
        install_legacy_chat_logging_passthrough(self._accounting_stack)

    def test_stream_captures_runtime_pricing_dependencies_before_response(self):
        app = FastAPI()
        _install_chat_logging(app)
        captured_snapshots = []

        class CapturingChunkProcessor(chat_logging.ChunkProcessor):
            instance = None

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                type(self).instance = self

        @app.post("/v1/chat/completions")
        async def stream_response(request: Request):
            captured_snapshots.append(request.state.runtime_snapshot)
            await request.body()

            async def body():
                yield b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3,"cost":0}}\n\n'
                yield b"data: [DONE]\n\n"

            return StreamingResponse(body(), media_type="text/event-stream")

        with patch.object(chat_logging, "ChunkProcessor", CapturingChunkProcessor):
            with patch.object(chat_logging, "record_chat_observability"):
                with TestClient(app) as client:
                    with client.stream(
                        "POST",
                        "/v1/chat/completions",
                        json={"model": "gateway-model", "stream": True},
                    ) as response:
                        body = b"".join(response.iter_bytes())

        processor = CapturingChunkProcessor.instance

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured_snapshots), 1)
        self.assertIsNotNone(processor)
        self.assertIs(processor.config_loader, captured_snapshots[0].config_loader)
        self.assertIs(
            processor.cost_rate_registry,
            captured_snapshots[0].cost_rate_registry,
        )
        self.assertNotIn("_usd_budget_reserved", processor.tokens_usage)
        self.assertNotIn("_usd_budget_reserved_estimate", processor.tokens_usage)
        self.assertIn(b'"total_tokens":3', body)

    def test_error_and_non_stream_payloads_exclude_legacy_budget_markers(self):
        for status_code in (200, 503):
            with self.subTest(status_code=status_code):
                app = FastAPI()
                _install_chat_logging(app)

                @app.post("/v1/chat/completions")
                async def response(request: Request):
                    await request.body()
                    if status_code >= 400:
                        request.state.usage_tracker = {"prompt_tokens": 1}
                    return JSONResponse(
                        {
                            "model": "provider-model",
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "total_tokens": 2,
                                "cost": 1.0,
                            },
                        },
                        status_code=status_code,
                    )

                with patch.object(
                    chat_logging,
                    "_schedule_active_request_prompt_estimate",
                ):
                    with patch.object(
                        chat_logging,
                        "_record_chat_observability_with_rates",
                    ) as record_mock:
                        with TestClient(app) as client:
                            response = client.post(
                                "/v1/chat/completions",
                                json={"model": "gateway-model", "messages": []},
                            )

                self.assertEqual(response.status_code, status_code)
                record_mock.assert_called_once()
                tokens_usage = record_mock.call_args.args[3]
                self.assertNotIn("_usd_budget_reserved", tokens_usage)
                self.assertNotIn("_usd_budget_reserved_estimate", tokens_usage)

    def test_non_stream_error_response_is_observability_only(self):
        app = FastAPI()
        _install_chat_logging(app)

        @app.post("/v1/chat/completions")
        async def failed_response(request: Request):
            await request.body()
            return JSONResponse({"detail": "provider failed"}, status_code=503)

        with patch.object(
            chat_logging,
            "record_chat_observability",
            wraps=chat_logging.record_chat_observability,
        ) as record_chat_observability_mock:
            with TestClient(app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={"model": "demo-model", "messages": [{"role": "user", "content": "hi"}]},
                )

        self.assertEqual(response.status_code, 503)
        record_chat_observability_mock.assert_called_once()
        tokens_usage = record_chat_observability_mock.call_args.args[3]
        self.assertTrue(tokens_usage["_skip_rate_based_cost"])
        services = record_chat_observability_mock.call_args.kwargs["services"]
        services.tokens_usage_db.insert_usage.assert_not_called()

    def test_streaming_response_stops_processor_task_without_timeout_wait(self):
        app = FastAPI()
        _install_chat_logging(app)

        class InspectableChunkProcessor(chat_logging.ChunkProcessor):
            last_instance = None

            def __init__(
                self,
                req_headers,
                req_body_str,
                is_real_streaming,
                *,
                services,
                config_loader,
                cost_rate_registry,
                provider_name=None,
                provider_model=None,
                gateway_model=None,
                operation="chat",
                api_key_id=None,
                request=None,
            ):
                super().__init__(
                    req_headers,
                    req_body_str,
                    is_real_streaming,
                    services=services,
                    config_loader=config_loader,
                    cost_rate_registry=cost_rate_registry,
                    provider_name=provider_name,
                    provider_model=provider_model,
                    gateway_model=gateway_model,
                    operation=operation,
                    api_key_id=api_key_id,
                    request=request,
                )
                type(self).last_instance = self
                self.finish_called = False
                self.wait_called = False

            async def finish(self):
                self.finish_called = True
                await super().finish()

            async def wait(self, timeout):
                self.wait_called = True
                return await super().wait(timeout)

        @app.post("/v1/chat/completions")
        async def stream_response(request: Request):
            await request.body()

            async def body():
                yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                yield b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3,"cost":0}}' \
                    b'\n\n'
                yield b"data: [DONE]\n\n"

            return StreamingResponse(body(), media_type="text/event-stream")

        with patch.object(chat_logging, "ChunkProcessor", InspectableChunkProcessor):
            with patch.object(chat_logging, "record_chat_observability") as record_chat_observability_mock:
                with TestClient(app) as client:
                    with client.stream(
                        "POST",
                        "/v1/chat/completions",
                        json={"model": "demo-model", "stream": True},
                    ) as response:
                        self.assertEqual(response.status_code, 200)
                        body = b"".join(response.iter_bytes())

        self.assertEqual(
            body,
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3,"cost":0}}\n\n'
            b"data: [DONE]\n\n",
        )

        processor = InspectableChunkProcessor.last_instance
        self.assertIsNotNone(processor)
        self.assertTrue(processor.finish_called)
        self.assertTrue(processor.wait_called)
        self.assertTrue(processor.done())
        record_chat_observability_mock.assert_called_once()

    def test_failed_stream_writes_single_log_record(self):
        async def scenario():
            services = make_app_services()
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=services,
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            with patch.object(chat_logging, "record_chat_observability") as record_chat_observability_mock:
                processor.start()
                await processor.enqueue_chunk(
                    b'data: {"error":{"message":"provider failed"}}\n\n'
                )
                await processor.finish()
                await processor.wait(timeout=0.5)
                return processor, record_chat_observability_mock, services

        processor, mock, services = run_async(scenario())

        self.assertTrue(processor.done())
        mock.assert_called_once_with(
            {},
            "{}",
            "[Upstream error event]",
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cost": 0,
                "operation": "chat",
            },
            _COST_RATE_REGISTRY,
            request=None,
            services=services,
        )

    def test_anthropic_tool_use_stream_is_included_in_log_output(self):
        async def scenario():
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=make_app_services(),
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            with patch.object(chat_logging, "record_chat_observability") as record_chat_observability_mock:
                processor.start()
                await processor.enqueue_chunk(
                    b'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
                    b'"content_block":{"type":"tool_use","id":"toolu_weather_1","name":"get_weather","input":{}}}\n\n'
                )
                await processor.enqueue_chunk(
                    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                    b'"delta":{"type":"input_json_delta","partial_json":"{\\"city\\":\\"Mosc"}}\n\n'
                )
                await processor.enqueue_chunk(
                    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                    b'"delta":{"type":"input_json_delta","partial_json":"ow\\"}"}}\n\n'
                )
                await processor.finish()
                await processor.wait(timeout=0.5)
                return processor, record_chat_observability_mock

        processor, mock = run_async(scenario())
        self.assertTrue(processor.done())
        mock.assert_called_once()
        log_output = mock.call_args[0][2]
        self.assertIn("[Tool Use: get_weather]", log_output)
        self.assertIn('{"city":"Moscow"}', log_output)

    def test_tool_state_count_is_bounded_and_cleared_on_overflow(self):
        async def scenario():
            services = make_app_services()
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=services,
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            for index in range(chat_logging.STREAM_TOOL_STATE_MAX_ITEMS + 1):
                processor._append_anthropic_tool_input(
                    index,
                    {},
                    tool_name="tool",
                )
            return processor, services.stream_observation_capacity.snapshot

        processor, snapshot = run_async(scenario())

        self.assertTrue(processor._observation_failed)
        self.assertEqual(processor._anthropic_tool_blocks, {})
        self.assertEqual(
            snapshot.last_reason_code,
            "tool_state_capacity_exhausted",
        )

    def test_stream_logging_truncates_large_accumulated_response_and_uses_bounded_queue(self):
        async def scenario():
            with patch.object(chat_logging, "STREAM_LOG_CAPTURE_MAX_CHARS", 24):
                processor = chat_logging.ChunkProcessor(
                    {},
                    "{}",
                    True,
                    services=make_app_services(),
                    config_loader=_CONFIG_LOADER,
                    cost_rate_registry=_COST_RATE_REGISTRY,
                )
                self.assertEqual(processor.queue.maxsize, chat_logging.STREAM_CHUNK_QUEUE_MAXSIZE)

                with patch.object(chat_logging, "record_chat_observability") as record_chat_observability_mock:
                    processor.start()
                    await processor.enqueue_chunk(
                        b'data: {"choices":[{"delta":{"content":"abcdefghijklmnopqrstuvwxyz"}}]}\n\n'
                    )
                    await processor.finish()
                    await processor.wait(timeout=0.5)
                    return processor, record_chat_observability_mock

        processor, mock = run_async(scenario())
        self.assertTrue(processor.done())
        mock.assert_called_once()
        log_output = mock.call_args[0][2]
        self.assertIn(chat_logging.STREAM_LOG_TRUNCATION_MARKER.strip(), log_output)
        self.assertLessEqual(
            len(log_output),
            24 + len(chat_logging.STREAM_LOG_TRUNCATION_MARKER),
        )

    def test_process_capacity_applies_backpressure_and_releases_every_lease(self):
        async def scenario():
            capacity = StreamObservationCapacity(max_items=1, max_bytes=1024)
            services = make_app_services(stream_observation_capacity=capacity)
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=services,
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            first = b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'
            second = b'data: {"choices":[{"delta":{"content":"two"}}]}\n\n'
            release_consumer = asyncio.Event()
            original_run = processor._run

            async def delayed_run():
                await release_consumer.wait()
                await original_run()

            with (
                patch.object(processor, "_run", delayed_run),
                patch.object(chat_logging, "record_chat_observability"),
            ):
                processor.start()
                await processor.enqueue_chunk(first)
                blocked_enqueue = asyncio.create_task(
                    processor.enqueue_chunk(second)
                )
                await asyncio.sleep(0)
                self.assertFalse(blocked_enqueue.done())
                self.assertEqual(capacity.snapshot.active_items, 1)
                release_consumer.set()
                await blocked_enqueue
                await processor.finish()
                self.assertTrue(await processor.wait(timeout=0.5))
            await asyncio.sleep(0)
            return capacity.snapshot, services.task_supervisor.task_count

        snapshot, task_count = run_async(scenario())

        self.assertEqual(snapshot.active_items, 0)
        self.assertEqual(snapshot.active_bytes, 0)
        self.assertEqual(snapshot.waiters, 0)
        self.assertEqual(task_count, 0)

    def test_fragmented_event_releases_item_slots_before_event_delimiter(self):
        async def scenario():
            capacity = StreamObservationCapacity(max_items=2, max_bytes=4096)
            services = make_app_services(stream_observation_capacity=capacity)
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=services,
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            payload = (
                b'data: {"choices":[{"delta":{"content":"fragmented"}}]}\n\n'
            )
            with patch.object(chat_logging, "record_chat_observability"):
                processor.start()
                for fragment in payload:
                    await processor.enqueue_chunk(bytes((fragment,)))
                await processor.finish()
                self.assertTrue(await processor.wait(timeout=0.5))
            return processor, capacity.snapshot

        processor, snapshot = run_async(asyncio.wait_for(scenario(), timeout=1.0))

        self.assertIn("fragmented", processor.llm_response_accum)
        self.assertEqual(snapshot.active_items, 0)
        self.assertEqual(snapshot.active_bytes, 0)
        self.assertEqual(snapshot.waiters, 0)

    def test_enqueue_granted_after_processor_exit_releases_lease(self):
        async def scenario():
            capacity = StreamObservationCapacity(max_items=1, max_bytes=1024)
            blocker = await capacity.acquire(1)
            services = make_app_services(stream_observation_capacity=capacity)
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=services,
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            with patch.object(chat_logging, "record_chat_observability"):
                processor.start()
                pending_enqueue = asyncio.create_task(
                    processor.enqueue_chunk(b"data: late\n\n")
                )
                for _attempt in range(20):
                    if capacity.snapshot.waiters == 1:
                        break
                    await asyncio.sleep(0)
                self.assertEqual(capacity.snapshot.waiters, 1)

                await processor.finish()
                self.assertTrue(await processor.wait(timeout=0.5))
                blocker.release_all()
                await pending_enqueue
            return capacity.snapshot

        snapshot = run_async(scenario())

        self.assertEqual(snapshot.active_items, 0)
        self.assertEqual(snapshot.active_bytes, 0)
        self.assertEqual(snapshot.waiters, 0)

    def test_cancelled_finish_caller_does_not_cancel_shared_sentinel_publish(self):
        async def scenario():
            capacity = StreamObservationCapacity(max_items=32, max_bytes=4096)
            services = make_app_services(stream_observation_capacity=capacity)
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=services,
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            for index in range(32):
                await processor.enqueue_chunk(
                    f'data: {{"choices":[{{"delta":{{"content":"{index}"}}}}]}}\n\n'.encode()
                )

            cancelled_caller = asyncio.create_task(processor.finish())
            for _attempt in range(20):
                if processor._finish_task is not None:
                    break
                await asyncio.sleep(0)
            cancelled_caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled_caller

            with patch.object(chat_logging, "record_chat_observability"):
                processor.start()
                await processor.finish()
                self.assertTrue(await processor.wait(timeout=0.5))
            await asyncio.sleep(0)
            return capacity.snapshot, services.task_supervisor.task_count

        snapshot, task_count = run_async(scenario())

        self.assertEqual(snapshot.active_items, 0)
        self.assertEqual(snapshot.active_bytes, 0)
        self.assertEqual(snapshot.waiters, 0)
        self.assertEqual(task_count, 0)

    def test_gzip_observation_is_bounded_and_diagnostics_do_not_expose_payload(self):
        async def run_case(chunk: bytes, *, event_limit: int):
            capacity = StreamObservationCapacity(max_items=2, max_bytes=2048)
            services = make_app_services(
                stream_observation_capacity=capacity,
                stream_event_max_bytes=event_limit,
            )
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=services,
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            with (
                patch.object(
                    chat_logging.settings,
                    "stream_event_max_bytes",
                    event_limit + 1,
                ),
                patch.object(chat_logging, "record_chat_observability") as record_mock,
            ):
                processor.start()
                await processor.enqueue_chunk(chunk)
                await processor.finish()
                await processor.wait(timeout=0.5)
            return capacity.snapshot, processor, record_mock

        valid_payload = json.dumps(
            {"choices": [{"delta": {"content": "compressed"}}]},
            separators=(",", ":"),
        ).encode()
        valid_chunk = gzip.compress(b"data: " + valid_payload + b"\n\n")
        valid_snapshot, valid_processor, valid_record = run_async(
            run_case(valid_chunk, event_limit=256)
        )
        self.assertIn("compressed", valid_processor.llm_response_accum)
        valid_record.assert_called_once()
        self.assertEqual(valid_snapshot.active_bytes, 0)

        secret = b"SUPER-SECRET-GZIP-PAYLOAD"
        with self.assertLogs(chat_logging.logger, level="WARNING") as logs:
            invalid_snapshot, _, _ = run_async(
                run_case(b"\x1f\x8b" + secret, event_limit=64)
            )
        joined_logs = "\n".join(logs.output)
        self.assertNotIn(secret.decode(), joined_logs)
        self.assertIn("reason=gzip_invalid", joined_logs)
        self.assertEqual(invalid_snapshot.active_items, 0)
        self.assertEqual(invalid_snapshot.active_bytes, 0)

        expanded = gzip.compress(b"data: " + (b"x" * 128) + b"\n\n")
        with self.assertLogs(chat_logging.logger, level="WARNING") as logs:
            expanded_snapshot, _, _ = run_async(
                run_case(expanded, event_limit=32)
            )
        self.assertIn("reason=event_too_large", "\n".join(logs.output))
        self.assertEqual(expanded_snapshot.active_items, 0)
        self.assertEqual(expanded_snapshot.active_bytes, 0)

    def test_gzip_observation_uses_one_incremental_decoder_across_chunks(self):
        async def scenario():
            capacity = StreamObservationCapacity(max_items=2, max_bytes=2048)
            services = make_app_services(
                stream_observation_capacity=capacity,
                stream_event_max_bytes=256,
            )
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=services,
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            payload = gzip.compress(
                b'data: {"choices":[{"delta":{"content":"split-gzip"}}]}\n\n'
            )
            chunks = (payload[:1], payload[1:5], payload[5:13], payload[13:])
            with patch.object(chat_logging, "record_chat_observability"):
                processor.start()
                for chunk in chunks:
                    await processor.enqueue_chunk(chunk)
                await processor.finish()
                self.assertTrue(await processor.wait(timeout=0.5))
            return processor, capacity.snapshot

        processor, snapshot = run_async(scenario())

        self.assertIn("split-gzip", processor.llm_response_accum)
        self.assertEqual(snapshot.active_items, 0)
        self.assertEqual(snapshot.active_bytes, 0)
        self.assertEqual(snapshot.waiters, 0)

    def test_malformed_complete_sse_data_enters_explicit_failure_without_payload_log(self):
        async def scenario(payload: bytes):
            capacity = StreamObservationCapacity(max_items=4, max_bytes=1024)
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=make_app_services(
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=256,
                ),
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            with patch.object(chat_logging, "record_chat_observability"):
                processor.start()
                await processor.enqueue_chunk(payload)
                await processor.finish()
                self.assertTrue(await processor.wait(timeout=0.5))
            return processor, capacity.snapshot

        for payload in (
            b"data: not-json-SECRET\n\n",
            b"data: {SECRET\n\n",
        ):
            with self.subTest(payload=payload):
                with self.assertLogs(chat_logging.logger, level="WARNING") as logs:
                    processor, snapshot = run_async(scenario(payload))

                joined_logs = "\n".join(logs.output)
                self.assertTrue(processor._observation_failed)
                self.assertEqual(snapshot.last_reason_code, "event_payload_invalid")
                self.assertNotIn("SECRET", joined_logs)
                self.assertIn("reason=event_payload_invalid", joined_logs)
                self.assertEqual(snapshot.active_items, 0)
                self.assertEqual(snapshot.active_bytes, 0)

    def test_event_at_capacity_limit_fails_observation_without_deadlock(self):
        async def scenario():
            event_limit = 32
            capacity = StreamObservationCapacity(
                max_items=2,
                max_bytes=event_limit,
            )
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=make_app_services(
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=event_limit,
                ),
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            partial_event = b"data: " + (b"x" * (event_limit - len(b"data: ")))
            with (
                patch.object(chat_logging, "record_chat_observability"),
                self.assertLogs(chat_logging.logger, level="WARNING") as logs,
            ):
                processor.start()
                await processor.enqueue_chunk(partial_event)
                await processor.enqueue_chunk(b"\n\n")
                await processor.finish()
                self.assertTrue(await processor.wait(timeout=0.5))
            return capacity.snapshot, logs.output

        snapshot, logs = run_async(asyncio.wait_for(scenario(), timeout=1.0))

        self.assertIn("reason=observation_progress_exhausted", "\n".join(logs))
        self.assertEqual(snapshot.active_items, 0)
        self.assertEqual(snapshot.active_bytes, 0)
        self.assertEqual(snapshot.waiters, 0)

    def test_prestart_event_at_capacity_limit_aborts_without_waiting(self):
        async def scenario():
            event_limit = 32
            capacity = StreamObservationCapacity(max_items=2, max_bytes=event_limit)
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=make_app_services(
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=event_limit,
                ),
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            partial_event = b"data: " + (b"x" * (event_limit - len(b"data: ")))
            with patch.object(chat_logging, "record_chat_observability"):
                await processor.enqueue_chunk(partial_event)
                await processor.enqueue_chunk(b"\n\n")
                prestart_snapshot = capacity.snapshot
                processor.start()
                await processor.finish()
                self.assertTrue(await processor.wait(timeout=0.5))
            return prestart_snapshot, capacity.snapshot

        prestart_snapshot, terminal_snapshot = run_async(
            asyncio.wait_for(scenario(), timeout=1.0)
        )

        self.assertEqual(prestart_snapshot.active_items, 0)
        self.assertEqual(prestart_snapshot.active_bytes, 0)
        self.assertEqual(prestart_snapshot.waiters, 0)
        self.assertEqual(terminal_snapshot.active_items, 0)
        self.assertEqual(terminal_snapshot.active_bytes, 0)

    def test_prestart_item_capacity_aborts_and_drains_without_waiting(self):
        async def scenario():
            capacity = StreamObservationCapacity(max_items=2, max_bytes=1024)
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=make_app_services(
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=512,
                ),
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )

            await processor.enqueue_chunk(b"first")
            await processor.enqueue_chunk(b"second")
            await asyncio.wait_for(
                processor.enqueue_chunk(b"third"),
                timeout=0.2,
            )
            return processor, capacity.snapshot

        processor, snapshot = run_async(scenario())

        self.assertTrue(processor._observation_failed)
        self.assertEqual(snapshot.active_items, 0)
        self.assertEqual(snapshot.active_bytes, 0)
        self.assertEqual(snapshot.waiters, 0)

    def test_multiple_partial_streams_cannot_deadlock_shared_byte_capacity(self):
        async def scenario():
            capacity = StreamObservationCapacity(max_items=4, max_bytes=40)
            services = make_app_services(stream_observation_capacity=capacity)
            processors = [
                chat_logging.ChunkProcessor(
                    {},
                    "{}",
                    True,
                    services=services,
                    config_loader=_CONFIG_LOADER,
                    cost_rate_registry=_COST_RATE_REGISTRY,
                )
                for _index in range(2)
            ]
            with patch.object(chat_logging, "record_chat_observability"):
                for processor in processors:
                    processor.start()
                    await processor.enqueue_chunk(b"data: " + (b"x" * 14))
                await asyncio.gather(
                    *(processor.enqueue_chunk(b"\n\n") for processor in processors)
                )
                await asyncio.gather(*(processor.finish() for processor in processors))
                waits = await asyncio.gather(
                    *(processor.wait(timeout=0.5) for processor in processors)
                )
            return waits, capacity.snapshot

        waits, snapshot = run_async(asyncio.wait_for(scenario(), timeout=1.0))

        self.assertEqual(waits, [True, True])
        self.assertEqual(snapshot.active_items, 0)
        self.assertEqual(snapshot.active_bytes, 0)
        self.assertEqual(snapshot.waiters, 0)

    def test_one_gzip_chunk_can_contain_multiple_bounded_sse_events(self):
        async def scenario():
            capacity = StreamObservationCapacity(max_items=4, max_bytes=2048)
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=make_app_services(
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=64,
                ),
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            events = (
                b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"two"}}]}\n\n'
            )
            self.assertGreater(len(events), 64)
            with patch.object(chat_logging, "record_chat_observability"):
                processor.start()
                await processor.enqueue_chunk(gzip.compress(events))
                await processor.finish()
                self.assertTrue(await processor.wait(timeout=0.5))
            return processor, capacity.snapshot

        processor, snapshot = run_async(scenario())

        self.assertIn("onetwo", processor.llm_response_accum)
        self.assertEqual(snapshot.active_items, 0)
        self.assertEqual(snapshot.active_bytes, 0)

    def test_gzip_probe_and_trailing_data_are_accounted_and_released(self):
        async def probe_scenario():
            capacity = StreamObservationCapacity(max_items=2, max_bytes=256)
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=make_app_services(
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=256,
                ),
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            await processor.enqueue_chunk(b"\x1f")
            probe_snapshot = capacity.snapshot
            with patch.object(chat_logging, "record_chat_observability"):
                processor.start()
                await processor.finish()
                self.assertTrue(await processor.wait(timeout=0.5))
            return probe_snapshot, capacity.snapshot

        probe_snapshot, released_snapshot = run_async(probe_scenario())
        self.assertEqual(probe_snapshot.active_items, 1)
        self.assertEqual(probe_snapshot.active_bytes, 1)
        self.assertEqual(released_snapshot.active_items, 0)
        self.assertEqual(released_snapshot.active_bytes, 0)

        async def trailing_scenario():
            capacity = StreamObservationCapacity(max_items=2, max_bytes=1024)
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=make_app_services(
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=256,
                ),
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            valid = gzip.compress(b"data: {}\n\n")
            with patch.object(chat_logging, "record_chat_observability"):
                processor.start()
                await processor.enqueue_chunk(valid + (b"S" * 100_000))
                await processor.finish()
                self.assertTrue(await processor.wait(timeout=0.5))
            return processor, capacity.snapshot

        with self.assertLogs(chat_logging.logger, level="WARNING") as logs:
            processor, trailing_snapshot = run_async(trailing_scenario())
        self.assertIn("reason=gzip_trailing_data", "\n".join(logs.output))
        self.assertIsNone(processor._gzip_decompressor)
        self.assertEqual(trailing_snapshot.active_items, 0)
        self.assertEqual(trailing_snapshot.active_bytes, 0)

    def test_gzip_terminal_exceptions_release_local_and_probe_leases(self):
        async def constructor_failure_scenario():
            capacity = StreamObservationCapacity(max_items=2, max_bytes=256)
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=make_app_services(stream_observation_capacity=capacity),
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            await processor.enqueue_chunk(b"\x1f")
            with (
                patch.object(
                    chat_logging.zlib,
                    "decompressobj",
                    side_effect=MemoryError("secret constructor failure"),
                ),
                self.assertRaises(MemoryError),
            ):
                await processor.enqueue_chunk(b"\x8brest")
            return capacity.snapshot

        constructor_snapshot = run_async(constructor_failure_scenario())
        self.assertEqual(constructor_snapshot.active_items, 0)
        self.assertEqual(constructor_snapshot.active_bytes, 0)

        class FatalDecoder:
            def decompress(self, _payload, _limit):
                raise MemoryError("secret decompression failure")

        async def decompression_failure_scenario():
            capacity = StreamObservationCapacity(max_items=2, max_bytes=256)
            processor = chat_logging.ChunkProcessor(
                {},
                "{}",
                True,
                services=make_app_services(
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=10,
                ),
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
            )
            with (
                patch.object(
                    chat_logging.zlib,
                    "decompressobj",
                    return_value=FatalDecoder(),
                ),
                self.assertRaises(MemoryError),
            ):
                await processor.enqueue_chunk(b"\x1f\x8bpayload")
            return capacity.snapshot

        decompression_snapshot = run_async(decompression_failure_scenario())
        self.assertEqual(decompression_snapshot.active_items, 0)
        self.assertEqual(decompression_snapshot.active_bytes, 0)


if __name__ == "__main__":
    unittest.main()
