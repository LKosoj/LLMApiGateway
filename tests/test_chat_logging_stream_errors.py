import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from llm_gateway_core.middleware import chat_logging
from tests._async_compat import run_async


class ChatLoggingStreamErrorsTests(unittest.TestCase):
    def test_non_stream_error_response_is_not_recorded_as_usage(self):
        app = FastAPI()
        app.middleware("http")(chat_logging.log_chat_completions)

        @app.post("/v1/chat/completions")
        async def failed_response():
            return JSONResponse({"detail": "provider failed"}, status_code=503)

        with patch.object(chat_logging, "record_chat_observability") as record_chat_observability_mock:
            with TestClient(app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={"model": "demo-model", "messages": [{"role": "user", "content": "hi"}]},
                )

        self.assertEqual(response.status_code, 503)
        record_chat_observability_mock.assert_not_called()

    def test_streaming_response_stops_processor_task_without_timeout_wait(self):
        app = FastAPI()
        app.middleware("http")(chat_logging.log_chat_completions)

        class InspectableChunkProcessor(chat_logging.ChunkProcessor):
            last_instance = None

            def __init__(
                self,
                req_headers,
                req_body_str,
                is_real_streaming,
                provider_name=None,
                provider_model=None,
                gateway_model=None,
                operation="chat",
                api_key_id=None,
                usd_budget_reserved=False,
                key_tpm_limit=None,
            ):
                super().__init__(
                    req_headers,
                    req_body_str,
                    is_real_streaming,
                    provider_name=provider_name,
                    provider_model=provider_model,
                    gateway_model=gateway_model,
                    operation=operation,
                    api_key_id=api_key_id,
                    usd_budget_reserved=usd_budget_reserved,
                    key_tpm_limit=key_tpm_limit,
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
        async def stream_response():
            async def body():
                yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                yield b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3,"cost":0}}' \
                    b'\n\n'

            return StreamingResponse(body(), media_type="text/event-stream")

        with patch.object(chat_logging, "ChunkProcessor", InspectableChunkProcessor):
            with patch.object(chat_logging, "record_chat_observability") as record_chat_observability_mock:
                with TestClient(app) as client:
                    with client.stream("POST", "/v1/chat/completions", json={"model": "demo-model"}) as response:
                        self.assertEqual(response.status_code, 200)
                        body = b"".join(response.iter_bytes())

        self.assertEqual(
            body,
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3,"cost":0}}\n\n',
        )

        processor = InspectableChunkProcessor.last_instance
        self.assertIsNotNone(processor)
        self.assertTrue(processor.finish_called)
        self.assertTrue(processor.wait_called)
        self.assertTrue(processor.done())
        record_chat_observability_mock.assert_called_once()

    def test_failed_stream_writes_single_log_record(self):
        async def scenario():
            processor = chat_logging.ChunkProcessor({}, "{}", True)
            with patch.object(chat_logging, "record_chat_observability") as record_chat_observability_mock:
                processor.start()
                processor.enqueue_chunk(b'data: {"error":{"message":"provider failed"}}\n\n')
                await processor.finish()
                await processor.wait(timeout=0.5)
                return processor, record_chat_observability_mock

        processor, mock = run_async(scenario())

        self.assertTrue(processor.done())
        mock.assert_called_once_with(
            {},
            "{}",
            '{"error":{"message":"provider failed"}}',
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cost": 0,
                "operation": "chat",
            },
            request=None,
        )

    def test_anthropic_tool_use_stream_is_included_in_log_output(self):
        async def scenario():
            processor = chat_logging.ChunkProcessor({}, "{}", True)
            with patch.object(chat_logging, "record_chat_observability") as record_chat_observability_mock:
                processor.start()
                processor.enqueue_chunk(
                    b'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
                    b'"content_block":{"type":"tool_use","id":"toolu_weather_1","name":"get_weather","input":{}}}\n\n'
                )
                processor.enqueue_chunk(
                    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                    b'"delta":{"type":"input_json_delta","partial_json":"{\\"city\\":\\"Mosc"}}\n\n'
                )
                processor.enqueue_chunk(
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

    def test_stream_logging_truncates_large_accumulated_response_and_uses_bounded_queue(self):
        async def scenario():
            with patch.object(chat_logging, "STREAM_LOG_CAPTURE_MAX_CHARS", 24):
                processor = chat_logging.ChunkProcessor({}, "{}", True)
                self.assertEqual(processor.queue.maxsize, chat_logging.STREAM_CHUNK_QUEUE_MAXSIZE)

                with patch.object(chat_logging, "record_chat_observability") as record_chat_observability_mock:
                    processor.start()
                    processor.enqueue_chunk(
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


if __name__ == "__main__":
    unittest.main()
