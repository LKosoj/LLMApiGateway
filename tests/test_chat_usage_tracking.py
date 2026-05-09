import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from llm_gateway_core.middleware import chat_logging


class ChatUsageTrackingTests(unittest.TestCase):
    def setUp(self):
        # chat_logging no longer ships with a module-level TokensUsageDB;
        # each test binds its own Mock so insert_usage calls can be observed.
        self._original_db = chat_logging.state.tokens_usage_db
        self._fake_db = Mock()
        chat_logging.set_tokens_usage_db(self._fake_db)

    def tearDown(self):
        chat_logging.set_tokens_usage_db(self._original_db)
    def test_usage_stats_are_recorded_even_when_file_logging_is_disabled(self):
        app = FastAPI()
        app.middleware("http")(chat_logging.log_chat_completions)

        @app.post("/v1/chat/completions")
        async def completions(request: Request):
            request.state.llmgateway_provider = "provider-name"
            request.state.llmgateway_provider_model = "provider-model"
            return JSONResponse(
                content={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "hello",
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 4,
                        "total_tokens": 7,
                        "cost": 0.001,
                    },
                    "model": "provider-model",
                    "request_id": "req-123",
                }
            )

        with patch.object(chat_logging.settings, "log_chat_messages", False):
            with patch.object(chat_logging, "write_log") as write_log_mock:
                with patch.object(self._fake_db, "insert_usage") as insert_usage_mock:
                    with TestClient(app) as client:
                        response = client.post("/v1/chat/completions", json={"model": "demo-model"})

        self.assertEqual(response.status_code, 200)
        write_log_mock.assert_not_called()
        insert_usage_mock.assert_called_once()
        call_args = dict(insert_usage_mock.call_args[0][0])
        self.assertGreaterEqual(call_args.pop("duration_ms"), 0)
        self.assertEqual(
            call_args,
            {
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "total_tokens": 7,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cost": 0.001,
                "gateway_model": "demo-model",
                "operation": "chat",
                "provider": "provider-name",
                "request_id": "req-123",
                "model": "provider-model",
            },
        )

    def test_usage_stats_prefer_actual_gateway_provider_model_over_short_response_model(self):
        app = FastAPI()
        app.middleware("http")(chat_logging.log_chat_completions)

        @app.post("/v1/chat/completions")
        async def completions(request: Request):
            request.state.llmgateway_provider = "devbox"
            request.state.llmgateway_provider_model = "zai.glm-5"
            return JSONResponse(
                content={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "hello",
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 4,
                        "total_tokens": 7,
                        "cost": 0.001,
                    },
                    "model": "glm-5",
                    "request_id": "req-789",
                }
            )

        with patch.object(chat_logging.settings, "log_chat_messages", False):
            with patch.object(chat_logging, "write_log") as write_log_mock:
                with patch.object(self._fake_db, "insert_usage") as insert_usage_mock:
                    with TestClient(app) as client:
                        response = client.post("/v1/chat/completions", json={"model": "demo-model"})

        self.assertEqual(response.status_code, 200)
        write_log_mock.assert_not_called()
        insert_usage_mock.assert_called_once()
        call_args = dict(insert_usage_mock.call_args[0][0])
        self.assertGreaterEqual(call_args.pop("duration_ms"), 0)
        self.assertEqual(
            call_args,
            {
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "total_tokens": 7,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cost": 0.001,
                "gateway_model": "demo-model",
                "operation": "chat",
                "provider": "devbox",
                "request_id": "req-789",
                "model": "zai.glm-5",
            },
        )

    def test_get_token_usage_handles_null_usage_details_without_dropping_fields(self):
        with patch.object(chat_logging.logging, "error") as logging_error_mock:
            tokens_usage = chat_logging.get_token_usage(
                {
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 4,
                        "total_tokens": 7,
                        "cost": 0.001,
                        "prompt_tokens_details": None,
                        "completion_tokens_details": None,
                    },
                    "model": "provider-model",
                    "provider": "provider-name",
                    "request_id": "req-null-details",
                }
            )

        logging_error_mock.assert_not_called()
        self.assertEqual(
            tokens_usage,
            {
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "total_tokens": 7,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cost": 0.001,
                "cost_saved": 0,
                "is_estimated": False,
                "provider": "provider-name",
                "request_id": "req-null-details",
                "model": "provider-model",
            },
        )

    def test_usage_stats_are_recorded_for_anthropic_messages_endpoint(self):
        """Проверка, что статистика собирается для Anthropic /v1/messages эндпоинта."""
        app = FastAPI()
        app.middleware("http")(chat_logging.log_chat_completions)

        @app.post("/v1/messages")
        async def anthropic_messages(request: Request):
            request.state.llmgateway_provider = "anthropic"
            request.state.llmgateway_provider_model = "claude-sonnet-4-20250514"
            return JSONResponse(
                content={
                    "id": "msg_123",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello"}],
                    "model": "claude-sonnet-4-20250514",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                    },
                }
            )

        with patch.object(chat_logging.settings, "log_chat_messages", False):
            with patch.object(chat_logging, "write_log") as write_log_mock:
                with patch.object(self._fake_db, "insert_usage") as insert_usage_mock:
                    with TestClient(app) as client:
                        response = client.post(
                            "/v1/messages",
                            json={
                                "model": "claude-sonnet-4-20250514",
                                "max_tokens": 100,
                                "messages": [{"role": "user", "content": "Hi"}],
                            },
                        )

        self.assertEqual(response.status_code, 200)
        write_log_mock.assert_not_called()
        insert_usage_mock.assert_called_once()
        call_args = insert_usage_mock.call_args[0][0]
        self.assertEqual(call_args["gateway_model"], "claude-sonnet-4-20250514")
        self.assertEqual(call_args["provider"], "anthropic")
        # Для Anthropic /v1/messages должен быть тип операции "messages", а не "chat"
        self.assertEqual(call_args["operation"], "messages")
        # Проверка конвертации Anthropic input_tokens/output_tokens в стандартный формат
        self.assertEqual(call_args["prompt_tokens"], 10)
        self.assertEqual(call_args["completion_tokens"], 5)
        self.assertEqual(call_args["total_tokens"], 15)
        self.assertGreaterEqual(call_args["duration_ms"], 0)

    def test_usage_stats_use_chat_operation_for_openai_completions(self):
        """Проверка, что для OpenAI /v1/chat/completions используется operation='chat'."""
        app = FastAPI()
        app.middleware("http")(chat_logging.log_chat_completions)

        @app.post("/v1/chat/completions")
        async def completions(request: Request):
            request.state.llmgateway_provider = "openai"
            request.state.llmgateway_provider_model = "gpt-4"
            return JSONResponse(
                content={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "hello",
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 4,
                        "total_tokens": 7,
                        "cost": 0.001,
                    },
                    "model": "gpt-4",
                    "request_id": "req-456",
                }
            )

        with patch.object(chat_logging.settings, "log_chat_messages", False):
            with patch.object(chat_logging, "write_log") as write_log_mock:
                with patch.object(self._fake_db, "insert_usage") as insert_usage_mock:
                    with TestClient(app) as client:
                        response = client.post("/v1/chat/completions", json={"model": "demo-model"})

        self.assertEqual(response.status_code, 200)
        write_log_mock.assert_not_called()
        insert_usage_mock.assert_called_once()
        call_args = insert_usage_mock.call_args[0][0]
        self.assertEqual(call_args["operation"], "chat")
        self.assertEqual(call_args["provider"], "openai")
        self.assertEqual(call_args["model"], "gpt-4")
        self.assertGreaterEqual(call_args["duration_ms"], 0)

    def test_usage_stats_are_recorded_for_responses_endpoint(self):
        app = FastAPI()
        app.middleware("http")(chat_logging.log_chat_completions)

        @app.post("/v1/responses")
        async def responses(request: Request):
            request.state.llmgateway_provider = "openai"
            request.state.llmgateway_provider_model = "gpt-4.1"
            request.state.llmgateway_gateway_model = "gateway-model"
            request.state.llmgateway_operation = "chat"
            return JSONResponse(
                content={
                    "id": "resp_123",
                    "object": "response",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Hello",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                }
            )

        with patch.object(chat_logging.settings, "log_chat_messages", False):
            with patch.object(chat_logging, "write_log") as write_log_mock:
                with patch.object(self._fake_db, "insert_usage") as insert_usage_mock:
                    with TestClient(app) as client:
                        response = client.post("/v1/responses", json={"model": "gateway-model", "input": "Hi"})

        self.assertEqual(response.status_code, 200)
        write_log_mock.assert_not_called()
        insert_usage_mock.assert_called_once()
        call_args = dict(insert_usage_mock.call_args[0][0])
        self.assertGreaterEqual(call_args.pop("duration_ms"), 0)
        self.assertEqual(
            call_args,
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cost": 0,
                "gateway_model": "gateway-model",
                "operation": "chat",
                "provider": "openai",
                "model": "gpt-4.1",
            },
        )

    def test_streaming_responses_usage_stats_are_recorded_from_response_completed_event(self):
        app = FastAPI()
        app.middleware("http")(chat_logging.log_chat_completions)

        @app.post("/v1/responses")
        async def responses(request: Request):
            request.state.llmgateway_provider = "openai"
            request.state.llmgateway_provider_model = "gpt-4.1"

            async def body():
                yield b'data: {"type":"response.output_text.delta","response_id":"resp_1","output_index":0,"content_index":0,"delta":"Hello"}\n\n'
                yield b'data: {"type":"response.completed","response":{"id":"resp_1","usage":{"input_tokens":7,"output_tokens":2,"total_tokens":9}}}\n\n'
                yield b'data: [DONE]\n\n'

            return StreamingResponse(body(), media_type="text/event-stream")

        with patch.object(chat_logging.settings, "log_chat_messages", False):
            with patch.object(chat_logging, "write_log") as write_log_mock:
                with patch.object(self._fake_db, "insert_usage") as insert_usage_mock:
                    with TestClient(app) as client:
                        with client.stream(
                            "POST",
                            "/v1/responses",
                            json={"model": "gateway-model", "input": "Hi"},
                        ) as response:
                            _ = response.read()

        self.assertEqual(response.status_code, 200)
        write_log_mock.assert_not_called()
        insert_usage_mock.assert_called_once()
        call_args = dict(insert_usage_mock.call_args[0][0])
        self.assertGreaterEqual(call_args.pop("duration_ms"), 0)
        self.assertEqual(
            call_args,
            {
                "prompt_tokens": 7,
                "completion_tokens": 2,
                "total_tokens": 9,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cost": 0,
                "gateway_model": "gateway-model",
                "operation": "chat",
                "provider": "openai",
                "model": "gpt-4.1",
            },
        )

    def test_messages_count_tokens_requests_are_not_recorded_in_usage_stats(self):
        app = FastAPI()
        app.middleware("http")(chat_logging.log_chat_completions)

        @app.post("/v1/messages/count_tokens")
        async def count_tokens():
            return JSONResponse(content={"input_tokens": 42})

        with patch.object(chat_logging.settings, "log_chat_messages", False):
            with patch.object(chat_logging, "write_log") as write_log_mock:
                with patch.object(self._fake_db, "insert_usage") as insert_usage_mock:
                    with TestClient(app) as client:
                        response = client.post(
                            "/v1/messages/count_tokens",
                            json={
                                "model": "claude-sonnet-4-20250514",
                                "messages": [{"role": "user", "content": "Hi"}],
                            },
                            headers={"anthropic-version": "2023-06-01"},
                        )

        self.assertEqual(response.status_code, 200)
        write_log_mock.assert_not_called()
        insert_usage_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
