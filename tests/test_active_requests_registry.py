import asyncio
import json
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from fastapi import FastAPI
from starlette.requests import Request

from llm_gateway_core.middleware import chat_logging
from llm_gateway_core.services.active_requests import (
    ActiveRequestsRegistry,
    get_active_requests_registry,
    update_active_request,
)
from llm_gateway_core.services.task_supervisor import TaskSupervisor
from tests._async_compat import run_async
from tests.runtime_test_support import bind_app_services


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


class ActiveRequestsRegistryBindingTests(unittest.TestCase):
    def test_container_wins_over_conflicting_legacy_alias(self):
        app = FastAPI()
        container_registry = ActiveRequestsRegistry()
        legacy_registry = ActiveRequestsRegistry()
        app.state.active_requests_registry = legacy_registry
        bind_app_services(app, active_requests_registry=container_registry)

        self.assertIs(get_active_requests_registry(app), container_registry)
        self.assertIs(app.state.active_requests_registry, legacy_registry)

    def test_missing_services_raises_without_lazy_legacy_write(self):
        app = FastAPI()

        with self.assertRaises(AttributeError):
            get_active_requests_registry(app)

        with self.assertRaises(AttributeError):
            getattr(app.state, "active_requests_registry")

    def test_update_without_request_id_is_noop_without_app_wiring(self):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [],
            }
        )

        update_active_request(request, provider="openai")

    def test_update_with_request_id_propagates_missing_wiring(self):
        missing_app = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [],
            }
        )
        missing_app.state.llmgateway_active_request_id = "req-no-app"
        with self.assertRaises(KeyError):
            update_active_request(missing_app, provider="openai")

        app_without_services = FastAPI()
        missing_services = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [],
                "app": app_without_services,
            }
        )
        missing_services.state.llmgateway_active_request_id = "req-no-services"
        with self.assertRaises(AttributeError):
            update_active_request(missing_services, provider="openai")

        with self.assertRaises(AttributeError):
            getattr(app_without_services.state, "active_requests_registry")


class ActiveRequestPromptEstimateTests(unittest.TestCase):
    def test_schedule_prompt_estimate_updates_running_record(self):
        app = FastAPI()
        registry = ActiveRequestsRegistry()
        services = bind_app_services(app, active_requests_registry=registry)
        registry.start(
            request_id="req-est",
            path="/v1/chat/completions",
            api_key_id=None,
        )
        supervisor = TaskSupervisor()
        body = json.dumps(
            {"model": "gw-model", "messages": [{"role": "user", "content": "hello world"}]}
        )

        async def scenario():
            chat_logging._schedule_active_request_prompt_estimate(
                "req-est",
                registry,
                supervisor,
                body,
                "gw-model",
            )
            replacement_registry = ActiveRequestsRegistry()
            replacement_registry.start(
                request_id="req-est",
                path="/v1/chat/completions",
                api_key_id=None,
            )
            app.state.services = replace(
                services,
                active_requests_registry=replacement_registry,
            )
            while supervisor.task_count:
                await asyncio.sleep(0)
            await supervisor.close()
            return replacement_registry

        replacement_registry = run_async(scenario())

        record = registry.list_records()[0]
        self.assertGreater(record["prompt_tokens"], 0)
        self.assertEqual(record["total_tokens"], record["prompt_tokens"])
        self.assertTrue(record["is_estimated"])
        self.assertEqual(replacement_registry.list_records()[0]["prompt_tokens"], 0)
        self.assertEqual(supervisor.failures, ())

    def test_schedule_prompt_estimate_after_finish_is_noop(self):
        registry = ActiveRequestsRegistry()
        registry.start(
            request_id="req-done",
            path="/v1/chat/completions",
            api_key_id=None,
        )
        supervisor = TaskSupervisor()
        body = json.dumps(
            {"model": "gw-model", "messages": [{"role": "user", "content": "hello"}]}
        )
        registry.finish("req-done")

        async def scenario():
            chat_logging._schedule_active_request_prompt_estimate(
                "req-done",
                registry,
                supervisor,
                body,
                "gw-model",
            )
            while supervisor.task_count:
                await asyncio.sleep(0)
            await supervisor.close()

        run_async(scenario())

        self.assertEqual(registry.list_records(), [])
        self.assertEqual(supervisor.failures, ())

    def test_supervisor_close_waits_for_prompt_worker_without_unretrieved_error(self):
        registry = ActiveRequestsRegistry()
        registry.start(
            request_id="req-close",
            path="/v1/chat/completions",
            api_key_id=None,
        )
        supervisor = TaskSupervisor()
        worker_started = threading.Event()
        release_worker = threading.Event()

        def blocking_estimate(_body, _model):
            worker_started.set()
            release_worker.wait(timeout=2)
            return 3

        async def scenario():
            chat_logging._schedule_active_request_prompt_estimate(
                "req-close",
                registry,
                supervisor,
                "{}",
                "gw-model",
            )
            while not worker_started.is_set():
                await asyncio.sleep(0)
            close_task = asyncio.create_task(supervisor.close())
            await asyncio.sleep(0)
            self.assertFalse(close_task.done())
            release_worker.set()
            await asyncio.wait_for(close_task, timeout=2)

        try:
            with patch.object(
                chat_logging,
                "estimate_prompt_tokens",
                side_effect=blocking_estimate,
            ):
                run_async(scenario())
        finally:
            release_worker.set()

        self.assertEqual(supervisor.task_count, 0)
        self.assertEqual(supervisor.failures, ())


if __name__ == "__main__":
    unittest.main()
