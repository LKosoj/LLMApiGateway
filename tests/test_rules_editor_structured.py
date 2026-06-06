import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient

import main
from llm_gateway_core.config.loader import ConfigLoader


VALID_PROVIDERS_TEXT = """
[
  {
    "openrouter": {
      "baseUrl": "https://openrouter.example",
      "apikey": "DIRECT-KEY"
    }
  },
  {
    "devbox": {
      "baseUrl": "https://devbox.example",
      "apikey": "DIRECT-KEY"
    }
  }
]
""".strip()

VALID_RULES_TEXT = """
[
  {
    "gateway_model_name": "gateway-model",
    "fallback_models": [
      {
        "provider": "devbox",
        "model": "provider-model",
        "retry_count": 2
      }
    ],
    "rotate_models": false
  }
]
""".strip()


class RulesEditorStructuredTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text("{}", encoding="utf-8")
        self.fallback_provider_patcher = patch.object(main.settings, "fallback_provider", "openrouter")
        self.fallback_provider_patcher.start()

        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
        )
        self.config_loader.load_providers()
        self.config_loader.load_fallback_rules()

    def tearDown(self):
        self.fallback_provider_patcher.stop()
        self.temp_dir.cleanup()

    @contextmanager
    def _client(self, fake_http_client: Mock | None = None):
        fake_http_client = fake_http_client or Mock()
        fake_http_client.aclose = AsyncMock()
        if not hasattr(fake_http_client, "get"):
            fake_http_client.get = AsyncMock()

        with ExitStack() as stack:
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))

            with TestClient(main.app) as client:
                yield client, fake_http_client

    def test_get_models_rules_structured_returns_rules_and_providers(self):
        with self._client() as (client, _):
            response = client.get(
                "/v1/config/models-rules/structured",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["providers"], ["openrouter", "devbox"])
        self.assertEqual(payload["rules"][0]["gateway_model_name"], "gateway-model")
        self.assertFalse(payload["rules"][0]["strip_think_tags"])
        self.assertEqual(payload["rules"][0]["fallback_models"][0]["provider"], "devbox")
        self.assertEqual(payload["rules"][0]["fallback_models"][0]["model"], "provider-model")

    def test_structured_save_returns_400_when_provider_model_is_missing(self):
        original_file_content = self.rules_path.read_text(encoding="utf-8")
        original_runtime_model = self.config_loader.fallback_rules["gateway-model"]["fallback_models"][0]["model"]
        fake_http_client = Mock()
        fake_http_client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "different-model"}]},
                request=httpx.Request("GET", "https://devbox.example/models"),
            )
        )

        with self._client(fake_http_client) as (client, _):
            response = client.post(
                "/v1/config/models-rules/structured",
                json={
                    "rules": [
                        {
                            "gateway_model_name": "gateway-model",
                            "fallback_models": [
                                {
                                    "provider": "devbox",
                                    "model": "provider-model",
                                }
                            ],
                            "rotate_models": False,
                        }
                    ]
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("provider-model", response.json()["detail"])
        self.assertEqual(self.rules_path.read_text(encoding="utf-8"), original_file_content)
        self.assertEqual(
            self.config_loader.fallback_rules["gateway-model"]["fallback_models"][0]["model"],
            original_runtime_model,
        )

    def test_provider_models_endpoint_uses_cache_and_cache_is_cleared_after_providers_save(self):
        fake_http_client = Mock()
        fake_http_client.get = AsyncMock(
            side_effect=[
                httpx.Response(
                    200,
                    json={"data": [{"id": "devbox-model-a"}]},
                    request=httpx.Request("GET", "https://devbox.example/models"),
                ),
                httpx.Response(
                    200,
                    json={"data": [{"id": "devbox-model-b"}]},
                    request=httpx.Request("GET", "https://devbox-updated.example/models"),
                ),
            ]
        )
        updated_providers_text = """
        [
          {
            "openrouter": {
              "baseUrl": "https://openrouter.example",
              "apikey": "DIRECT-KEY"
            }
          },
          {
            "devbox": {
              "baseUrl": "https://devbox-updated.example",
              "apikey": "DIRECT-KEY"
            }
          }
        ]
        """.strip()

        with self._client(fake_http_client) as (client, _):
            first_models_response = client.get(
                "/v1/config/providers/devbox/models",
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            second_models_response = client.get(
                "/v1/config/providers/devbox/models",
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            save_response = client.post(
                "/v1/config/providers",
                content=updated_providers_text,
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )
            third_models_response = client.get(
                "/v1/config/providers/devbox/models",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(first_models_response.status_code, 200)
        self.assertEqual(first_models_response.json()["models"], [{"id": "devbox-model-a"}])
        self.assertEqual(second_models_response.status_code, 200)
        self.assertEqual(second_models_response.json()["models"], [{"id": "devbox-model-a"}])
        self.assertEqual(save_response.status_code, 200)
        self.assertEqual(third_models_response.status_code, 200)
        self.assertEqual(third_models_response.json()["models"], [{"id": "devbox-model-b"}])
        self.assertEqual(fake_http_client.get.await_count, 2)
        self.assertEqual(fake_http_client.get.await_args_list[0].args[0], "https://devbox.example/models")
        self.assertEqual(fake_http_client.get.await_args_list[1].args[0], "https://devbox-updated.example/models")

    def test_structured_save_persists_context_overflow_fallback(self):
        original_runtime_model = self.config_loader.fallback_rules["gateway-model"]["fallback_models"][0]["model"]
        fake_http_client = Mock()
        fake_http_client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "provider-model"}, {"id": "large-context-model"}]},
                request=httpx.Request("GET", "https://devbox.example/models"),
            )
        )

        with self._client(fake_http_client) as (client, _):
            response = client.post(
                "/v1/config/models-rules/structured",
                json={
                    "rules": [
                        {
                            "gateway_model_name": "gateway-model",
                            "fallback_models": [
                                {
                                    "provider": "devbox",
                                    "model": "provider-model",
                                }
                            ],
                            "context_overflow_fallback": {
                                "provider": "devbox",
                                "model": "large-context-model",
                            },
                            "rotate_models": False,
                        }
                    ]
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["rules"][0]["context_overflow_fallback"],
            {
                "provider": "devbox",
                "model": "large-context-model",
                "use_provider_order_as_fallback": False,
                "custom_body_params": {},
                "custom_headers": {},
            },
        )
        self.assertEqual(
            self.config_loader.fallback_rules["gateway-model"]["context_overflow_fallback"]["model"],
            "large-context-model",
        )
        self.assertEqual(
            self.config_loader.fallback_rules["gateway-model"]["fallback_models"][0]["model"],
            original_runtime_model,
        )
        self.assertIn('"context_overflow_fallback"', self.rules_path.read_text(encoding="utf-8"))

    def test_structured_save_persists_max_total_attempts_and_use_provider_order(self):
        """GET → POST → GET round-trip must preserve max_total_attempts and
        use_provider_order_as_fallback (per-fallback). Previously
        _build_structured_rules_response dropped max_total_attempts entirely so
        a UI round-trip silently lost the chain budget on disk."""
        fake_http_client = Mock()
        fake_http_client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "provider-model"}, {"id": "large-context-model"}]},
                request=httpx.Request("GET", "https://devbox.example/models"),
            )
        )

        with self._client(fake_http_client) as (client, _):
            save_response = client.post(
                "/v1/config/models-rules/structured",
                json={
                    "rules": [
                        {
                            "gateway_model_name": "gateway-model",
                            "fallback_models": [
                                {
                                    "provider": "devbox",
                                    "model": "provider-model",
                                    "use_provider_order_as_fallback": True,
                                    "providers_order": ["devbox", "openrouter"],
                                },
                                {
                                    "provider": "devbox",
                                    "model": "large-context-model",
                                    "use_provider_order_as_fallback": False,
                                },
                            ],
                            "rotate_models": False,
                            "max_total_attempts": 7,
                        }
                    ]
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            self.assertEqual(save_response.status_code, 200, save_response.text)

            saved_rules = save_response.json()["rules"]
            self.assertEqual(saved_rules[0]["max_total_attempts"], 7)
            self.assertTrue(saved_rules[0]["fallback_models"][0]["use_provider_order_as_fallback"])
            self.assertFalse(saved_rules[0]["fallback_models"][1]["use_provider_order_as_fallback"])

            get_response = client.get(
                "/v1/config/models-rules/structured",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(get_response.status_code, 200, get_response.text)
        round_tripped = get_response.json()["rules"][0]
        self.assertEqual(round_tripped["max_total_attempts"], 7)
        self.assertEqual(
            round_tripped["fallback_models"][0]["use_provider_order_as_fallback"],
            True,
        )
        self.assertEqual(
            round_tripped["fallback_models"][0]["providers_order"],
            ["devbox", "openrouter"],
        )
        self.assertEqual(
            round_tripped["fallback_models"][1]["use_provider_order_as_fallback"],
            False,
        )

        self.assertEqual(
            self.config_loader.fallback_rules["gateway-model"]["max_total_attempts"],
            7,
        )
        self.assertTrue(
            self.config_loader.fallback_rules["gateway-model"]["fallback_models"][0][
                "use_provider_order_as_fallback"
            ]
        )

        on_disk = self.rules_path.read_text(encoding="utf-8")
        self.assertIn('"max_total_attempts": 7', on_disk)
        self.assertIn('"use_provider_order_as_fallback": true', on_disk)
        self.assertIn('"providers_order"', on_disk)

    def test_structured_get_omits_max_total_attempts_when_unset(self):
        """When the rule does not configure max_total_attempts, the response
        must omit the key entirely (preserves 'unlimited' semantics — Optional
        chain budget). Otherwise UI cannot distinguish 'unlimited' from 0."""
        with self._client() as (client, _):
            response = client.get(
                "/v1/config/models-rules/structured",
                headers={"Authorization": "Bearer test-gateway-key"},
            )
        self.assertEqual(response.status_code, 200)
        rule = response.json()["rules"][0]
        self.assertNotIn("max_total_attempts", rule)

    def test_structured_save_persists_dynamic_penalty(self):
        """Regression: _build_fallback_rules_config previously dropped
        dynamic_penalty from the in-memory rule_config, so the GET endpoint
        re-served False even when the file on disk had true. chat.py also
        reads from the in-memory state, so the feature never activated via
        the UI round-trip."""
        fake_http_client = Mock()
        fake_http_client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "provider-model"}]},
                request=httpx.Request("GET", "https://devbox.example/models"),
            )
        )

        with self._client(fake_http_client) as (client, _):
            response = client.post(
                "/v1/config/models-rules/structured",
                json={
                    "rules": [
                        {
                            "gateway_model_name": "gateway-model",
                            "fallback_models": [
                                {
                                    "provider": "devbox",
                                    "model": "provider-model",
                                }
                            ],
                            "rotate_models": False,
                            "dynamic_penalty": True,
                        }
                    ]
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["rules"][0]["dynamic_penalty"])
        self.assertTrue(self.config_loader.fallback_rules["gateway-model"]["dynamic_penalty"])
        self.assertIn('"dynamic_penalty": true', self.rules_path.read_text(encoding="utf-8"))

    def test_structured_save_persists_strip_think_tags(self):
        fake_http_client = Mock()
        fake_http_client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "provider-model"}]},
                request=httpx.Request("GET", "https://devbox.example/models"),
            )
        )

        with self._client(fake_http_client) as (client, _):
            response = client.post(
                "/v1/config/models-rules/structured",
                json={
                    "rules": [
                        {
                            "gateway_model_name": "gateway-model",
                            "fallback_models": [
                                {
                                    "provider": "devbox",
                                    "model": "provider-model",
                                }
                            ],
                            "rotate_models": False,
                            "strip_think_tags": True,
                        }
                    ]
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["rules"][0]["strip_think_tags"])
        self.assertTrue(self.config_loader.fallback_rules["gateway-model"]["strip_think_tags"])
        self.assertIn('"strip_think_tags": true', self.rules_path.read_text(encoding="utf-8"))

    def test_structured_save_persists_compress_tool_results(self):
        fake_http_client = Mock()
        fake_http_client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "provider-model"}]},
                request=httpx.Request("GET", "https://devbox.example/models"),
            )
        )

        with self._client(fake_http_client) as (client, _):
            response = client.post(
                "/v1/config/models-rules/structured",
                json={
                    "rules": [
                        {
                            "gateway_model_name": "gateway-model",
                            "fallback_models": [
                                {
                                    "provider": "devbox",
                                    "model": "provider-model",
                                }
                            ],
                            "rotate_models": False,
                            "compress_tool_results": True,
                        }
                    ]
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

            get_response = client.get(
                "/v1/config/models-rules/structured",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["rules"][0]["compress_tool_results"])
        self.assertEqual(get_response.status_code, 200)
        self.assertTrue(get_response.json()["rules"][0]["compress_tool_results"])
        self.assertTrue(self.config_loader.fallback_rules["gateway-model"]["compress_tool_results"])
        self.assertIn('"compress_tool_results": true', self.rules_path.read_text(encoding="utf-8"))

    def test_structured_save_persists_available_models_and_short_circuits_models_endpoint(self):
        fake_http_client = Mock()
        fake_http_client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "from-upstream"}]},
                request=httpx.Request("GET", "https://proxy.example/models"),
            )
        )
        payload = {
            "providers": [
                {"name": "openrouter", "baseUrl": "https://openrouter.example", "apikey": "DIRECT-KEY"},
                {"name": "devbox", "baseUrl": "https://devbox.example", "apikey": "DIRECT-KEY"},
                {
                    "name": "pinned",
                    "baseUrl": "https://proxy.example/v1",
                    "apikey": "DIRECT-KEY",
                    "available_models": ["alpha", "beta"],
                },
            ]
        }

        with self._client(fake_http_client) as (client, _):
            save_response = client.post(
                "/v1/config/providers/structured",
                json=payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            models_response = client.get(
                "/v1/config/providers/pinned/models",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(save_response.status_code, 200)
        persisted = self.providers_path.read_text(encoding="utf-8")
        self.assertIn("available_models", persisted)
        self.assertIn("alpha", persisted)
        self.assertEqual(models_response.status_code, 200)
        # The explicit list short-circuits the upstream /models call.
        self.assertEqual(models_response.json()["models"], [{"id": "alpha"}, {"id": "beta"}])


if __name__ == "__main__":
    unittest.main()
