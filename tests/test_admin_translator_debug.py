"""Tests for the Translator Debugger endpoint and UI page."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from llm_gateway_core.api.v1.admin_translator_debug import translator_debug_router
from llm_gateway_core.config.paths import STATIC_DIR
from llm_gateway_core.middleware.auth import api_key_auth
from llm_gateway_core.db.api_keys_db import ApiKeyRecord


MASTER_KEY = "test-master-key"


class FakeApiKeysDB:
    def __init__(self, record: ApiKeyRecord | None = None):
        self.record = record

    def get_by_key(self, key: str) -> ApiKeyRecord | None:
        return self.record


def _build_app() -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.middleware("http")(api_key_auth)
    app.include_router(translator_debug_router, prefix="/v1")
    return app


def _make_user_record() -> ApiKeyRecord:
    return ApiKeyRecord(
        id=42,
        name="virtual",
        api_key="virtual-key",
        budget_usd=None,
        spent_usd=0.0,
        rpm=None,
        tpm=None,
        disabled=False,
    )


class TranslatorDebugTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway_key_patcher = patch(
            "llm_gateway_core.middleware.auth.settings.gateway_api_key",
            MASTER_KEY,
        )
        self.gateway_key_patcher.start()
        self.app = _build_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.client.close()
        self.gateway_key_patcher.stop()

    def _master_headers(self) -> dict:
        return {"Authorization": f"Bearer {MASTER_KEY}"}

    # ── 1. OpenAI→Anthropic basic chat ────────────────────────────────────────
    def test_openai_to_anthropic_basic_chat(self):
        resp = self.client.post(
            "/v1/admin/translator/debug",
            json={
                "source_format": "openai",
                "target_format": "anthropic",
                "request_body": {
                    "model": "x",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            },
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        steps = {s["step"]: s for s in data["steps"]}
        self.assertEqual(len(steps), 7)

        # Step 4 should be Anthropic format with content blocks
        step4_payload = steps[4]["payload"]
        self.assertIn("messages", step4_payload)
        msgs = step4_payload["messages"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "user")
        content = msgs[0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[0]["text"], "hi")

    # ── 2. Anthropic→OpenAI basic ─────────────────────────────────────────────
    def test_anthropic_to_openai_basic(self):
        resp = self.client.post(
            "/v1/admin/translator/debug",
            json={
                "source_format": "anthropic",
                "target_format": "openai",
                "request_body": {
                    "model": "claude-3-5-sonnet-20241022",
                    "messages": [{"role": "user", "content": "hello"}],
                    "max_tokens": 100,
                },
            },
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        steps = {s["step"]: s for s in data["steps"]}

        # Step 3: intermediate openai request
        step3 = steps[3]["payload"]
        self.assertIn("messages", step3)
        roles = [m["role"] for m in step3["messages"]]
        self.assertIn("user", roles)

    # ── 3. Same-format passthrough (openai→openai) ────────────────────────────
    def test_same_format_passthrough(self):
        body = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "ping"}],
        }
        resp = self.client.post(
            "/v1/admin/translator/debug",
            json={
                "source_format": "openai",
                "target_format": "openai",
                "request_body": body,
            },
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        steps = {s["step"]: s for s in data["steps"]}

        # Steps 3 and 4 must have same messages
        self.assertEqual(steps[3]["payload"]["messages"], steps[4]["payload"]["messages"])
        # Steps 6 and 7 must be equal
        self.assertEqual(steps[6]["payload"], steps[7]["payload"])

        # Step 2 requires_translation must be False
        self.assertFalse(steps[2]["payload"]["requires_translation"])

    # ── 4. Tool-use conversion ────────────────────────────────────────────────
    def test_with_tool_use(self):
        resp = self.client.post(
            "/v1/admin/translator/debug",
            json={
                "source_format": "openai",
                "target_format": "anthropic",
                "request_body": {
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "What is the weather?"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "description": "Get weather",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"city": {"type": "string"}},
                                    "required": ["city"],
                                },
                            },
                        }
                    ],
                },
            },
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        steps = {s["step"]: s for s in data["steps"]}

        # Step 4: Anthropic tools should be present and normalized
        step4 = steps[4]["payload"]
        self.assertIn("tools", step4)
        tools = step4["tools"]
        self.assertEqual(len(tools), 1)
        # Anthropic tools have "input_schema" not "parameters"
        self.assertIn("input_schema", tools[0])

    # ── 5. Thinking block in Anthropic response ───────────────────────────────
    def test_with_thinking_block(self):
        # source=anthropic, target=anthropic: provider returns Anthropic format
        # with thinking blocks; step 6 converts to OpenAI, step 7 back to Anthropic.
        mock_anthropic_response = {
            "id": "msg_thinking_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",
            "content": [
                {"type": "thinking", "thinking": "Let me think..."},
                {"type": "text", "text": "The answer is 42."},
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 20, "output_tokens": 15},
        }
        resp = self.client.post(
            "/v1/admin/translator/debug",
            json={
                "source_format": "anthropic",
                "target_format": "anthropic",
                "request_body": {
                    "model": "claude-3-5-sonnet-20241022",
                    "messages": [{"role": "user", "content": "What is 6×7?"}],
                    "max_tokens": 100,
                },
                "mock_provider_response": mock_anthropic_response,
            },
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        steps = {s["step"]: s for s in data["steps"]}

        # Step 6: intermediate openai response — text block "The answer is 42." present
        step6 = steps[6]["payload"]
        self.assertIn("choices", step6)
        msg_content = step6["choices"][0]["message"]["content"]
        self.assertIn("42", str(msg_content))

    # ── 6. Invalid source_format → 422 ───────────────────────────────────────
    def test_invalid_source_format_422(self):
        resp = self.client.post(
            "/v1/admin/translator/debug",
            json={
                "source_format": "invalid",
                "target_format": "openai",
                "request_body": {"model": "x", "messages": []},
            },
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 422)

    # ── 7. Virtual key → 403 ─────────────────────────────────────────────────
    def test_master_only_403(self):
        resp = self.client.post(
            "/v1/admin/translator/debug",
            json={
                "source_format": "openai",
                "target_format": "anthropic",
                "request_body": {
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            },
            headers={"Authorization": "Bearer virtual-key"},
        )
        self.assertIn(resp.status_code, (403, 401))

    # ── 8. Master → 200 ──────────────────────────────────────────────────────
    def test_master_200(self):
        resp = self.client.post(
            "/v1/admin/translator/debug",
            json={
                "source_format": "openai",
                "target_format": "anthropic",
                "request_body": {
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            },
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 200)

    # ── 9. Default mock when omitted ─────────────────────────────────────────
    def test_default_mock_response_when_omitted(self):
        resp = self.client.post(
            "/v1/admin/translator/debug",
            json={
                "source_format": "openai",
                "target_format": "anthropic",
                "request_body": {
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                # No mock_provider_response
            },
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        steps = {s["step"]: s for s in data["steps"]}

        step5 = steps[5]["payload"]
        # Default anthropic mock should have "content" key
        self.assertIn("content", step5)
        self.assertTrue(
            any(
                "mock" in str(b.get("text", "")).lower()
                for b in step5["content"]
                if isinstance(b, dict)
            ),
            msg="Default mock should mention 'mock' in content text",
        )

    # ── 10. UI page loads ────────────────────────────────────────────────────
    def test_ui_page_loads(self):
        resp = self.client.get(
            "/v1/ui/translator-debug",
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("content-type", ""))
        self.assertIn(
            '<script src="/static/translator-debug.js"',
            resp.text,
        )


if __name__ == "__main__":
    unittest.main()
