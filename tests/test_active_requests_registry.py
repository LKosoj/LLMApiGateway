import asyncio
import json
import unittest
from types import SimpleNamespace

from starlette.requests import Request

from llm_gateway_core.middleware import chat_logging
from llm_gateway_core.services.active_requests import (
    ActiveRequestsRegistry,
    get_active_requests_registry,
)


class ActiveRequestsRegistryRecordShapeTests(unittest.TestCase):
    def test_start_record_exposes_upstream_key_fingerprint_key(self):
        """Running records must expose the same keys as completed DB rows so the
        usage UI keeps a stable column set while a request is in flight."""
        registry = ActiveRequestsRegistry()
        registry.start(
            request_id="req-1",
            path="/v1/chat/completions",
            api_key_id=None,
        )

        record = registry.list_records()[0]
        self.assertIn("upstream_key_fingerprint", record)
        self.assertIsNone(record["upstream_key_fingerprint"])

    def test_update_applies_prompt_estimate_and_fingerprint(self):
        registry = ActiveRequestsRegistry()
        registry.start(
            request_id="req-1",
            path="/v1/chat/completions",
            api_key_id=None,
        )

        registry.update(
            "req-1",
            prompt_tokens=1234,
            total_tokens=1234,
            is_estimated=True,
            upstream_key_fingerprint="fp-abc",
        )

        record = registry.list_records()[0]
        self.assertEqual(record["prompt_tokens"], 1234)
        self.assertEqual(record["total_tokens"], 1234)
        self.assertTrue(record["is_estimated"])
        self.assertEqual(record["upstream_key_fingerprint"], "fp-abc")

    def test_update_ignores_unset_fields(self):
        registry = ActiveRequestsRegistry()
        registry.start(
            request_id="req-1",
            path="/v1/chat/completions",
            api_key_id=None,
        )
        registry.update("req-1", prompt_tokens=10, total_tokens=10, is_estimated=True)

        registry.update("req-1", provider="openrouter", model="qwen/qwen3")

        record = registry.list_records()[0]
        self.assertEqual(record["prompt_tokens"], 10)
        self.assertTrue(record["is_estimated"])
        self.assertEqual(record["provider"], "openrouter")


class ActiveRequestPromptEstimateTests(unittest.TestCase):
    def _build_request(self, app, request_id: str) -> Request:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [],
                "app": app,
            }
        )
        request.state.llmgateway_active_request_id = request_id
        return request

    def test_schedule_prompt_estimate_updates_running_record(self):
        app = SimpleNamespace(state=SimpleNamespace())
        registry = get_active_requests_registry(app)
        registry.start(
            request_id="req-est",
            path="/v1/chat/completions",
            api_key_id=None,
        )
        request = self._build_request(app, "req-est")
        body = json.dumps(
            {"model": "gw-model", "messages": [{"role": "user", "content": "hello world"}]}
        )

        async def scenario():
            chat_logging._schedule_active_request_prompt_estimate(request, body, "gw-model")
            await asyncio.gather(*chat_logging._ACTIVE_PROMPT_ESTIMATE_TASKS)

        asyncio.run(scenario())

        record = registry.list_records()[0]
        self.assertGreater(record["prompt_tokens"], 0)
        self.assertEqual(record["total_tokens"], record["prompt_tokens"])
        self.assertTrue(record["is_estimated"])

    def test_schedule_prompt_estimate_after_finish_is_noop(self):
        app = SimpleNamespace(state=SimpleNamespace())
        registry = get_active_requests_registry(app)
        registry.start(
            request_id="req-done",
            path="/v1/chat/completions",
            api_key_id=None,
        )
        request = self._build_request(app, "req-done")
        body = json.dumps(
            {"model": "gw-model", "messages": [{"role": "user", "content": "hello"}]}
        )
        registry.finish("req-done")

        async def scenario():
            chat_logging._schedule_active_request_prompt_estimate(request, body, "gw-model")
            await asyncio.gather(*chat_logging._ACTIVE_PROMPT_ESTIMATE_TASKS)

        asyncio.run(scenario())

        self.assertEqual(registry.list_records(), [])


if __name__ == "__main__":
    unittest.main()
