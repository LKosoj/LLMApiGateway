import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from llm_gateway_core.config.loader import ConfigLoader
from tests.rules_editor_test_support import transactional_rules_editor_client


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
        self.fusion_rules_path = Path(self.temp_dir.name) / "models_fusion_rules.json"
        self.model_rules_path = Path(self.temp_dir.name) / "models_model_rules.json"
        self.router_rules_path = Path(self.temp_dir.name) / "models_router_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text("{}", encoding="utf-8")
        self.fusion_rules_path.write_text("[]", encoding="utf-8")
        self.model_rules_path.write_text("{}\n", encoding="utf-8")
        self.router_rules_path.write_text("[]", encoding="utf-8")
        self.fallback_provider_patcher = patch(
            "llm_gateway_core.config.loader.settings.fallback_provider",
            "openrouter",
        )
        self.fallback_provider_patcher.start()

        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
            fusion_rules_filename=str(self.fusion_rules_path),
            model_rules_filename=str(self.model_rules_path),
            router_rules_filename=str(self.router_rules_path),
        )
        self.config_loader.load_complete()

    def tearDown(self):
        self.fallback_provider_patcher.stop()
        self.temp_dir.cleanup()

    @contextmanager
    def _client(self, fake_http_client: Mock | None = None):
        fake_http_client = fake_http_client or Mock()
        if not isinstance(fake_http_client.get, AsyncMock):
            fake_http_client.get = AsyncMock(
                return_value=httpx.Response(
                    200,
                    json={"data": [{"id": "provider-model"}]},
                    request=httpx.Request(
                        "GET", "https://devbox.example/models"
                    ),
                )
            )

        async def catalog_handler(request: httpx.Request) -> httpx.Response:
            return await fake_http_client.get(str(request.url))

        transport = httpx.MockTransport(catalog_handler)
        with transactional_rules_editor_client(
            self.config_loader,
            transport=transport,
        ) as (client, runtime):
            yield client, fake_http_client
            generation_response = client.get("/_test/runtime-generation")
            self.assertEqual(generation_response.status_code, 200)
            self.published_snapshot = runtime.observed_snapshot

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
        self.assertFalse(payload["rules"][0]["tool_call_rescue"])
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
        self.assertEqual(
            response.json()["detail"]["code"],
            "config_validation_failed",
        )
        # Preflight names the offending gateway model, fallback model and
        # provider so the editor can point at the row that has to be fixed.
        self.assertIn(
            "Gateway model 'gateway-model': fallback model 'provider-model' "
            "is not available from provider 'devbox'.",
            response.json()["detail"]["errors"][0]["msg"],
        )
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
            self.published_snapshot.config_loader.fallback_rules[
                "gateway-model"
            ]["context_overflow_fallback"]["model"],
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
            self.published_snapshot.config_loader.fallback_rules[
                "gateway-model"
            ]["max_total_attempts"],
            7,
        )
        self.assertTrue(
            self.published_snapshot.config_loader.fallback_rules[
                "gateway-model"
            ]["fallback_models"][0]["use_provider_order_as_fallback"]
        )

        on_disk = self.rules_path.read_text(encoding="utf-8")
        self.assertIn('"max_total_attempts": 7', on_disk)
        self.assertIn('"use_provider_order_as_fallback": true', on_disk)
        self.assertIn('"providers_order"', on_disk)

    def test_structured_save_persists_upstream_key_pool_on_fallback_rows(self):
        fake_http_client = Mock()
        fake_http_client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "provider-model"}]},
                request=httpx.Request("GET", "https://devbox.example/models"),
            )
        )

        with self._client(fake_http_client) as (client, _):
            providers_response = client.post(
                "/v1/config/providers/structured",
                json={
                    "providers": [
                        {"name": "openrouter", "baseUrl": "https://openrouter.example", "apikey": "DIRECT-KEY"},
                        {
                            "name": "devbox",
                            "baseUrl": "https://devbox.example",
                            "apikey": "DIRECT-KEY",
                            "upstream_key_pools": {
                                "main": {
                                    "keys": [{"id": "primary", "apikey": "DIRECT-KEY-POOL"}],
                                },
                            },
                        },
                    ]
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )
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
                                    "upstream_key_pool": "main",
                                },
                            ],
                            "rotate_models": False,
                        }
                    ]
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            get_response = client.get(
                "/v1/config/models-rules/structured",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(providers_response.status_code, 200, providers_response.text)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(get_response.status_code, 200, get_response.text)
        self.assertEqual(
            get_response.json()["rules"][0]["fallback_models"][0]["upstream_key_pool"],
            "main",
        )
        self.assertEqual(
            self.published_snapshot.config_loader.fallback_rules[
                "gateway-model"
            ]["fallback_models"][0]["upstream_key_pool"],
            "main",
        )
        self.assertIn('"upstream_key_pool": "main"', self.rules_path.read_text(encoding="utf-8"))

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
        self.assertTrue(
            self.published_snapshot.config_loader.fallback_rules[
                "gateway-model"
            ]["dynamic_penalty"]
        )
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
        self.assertTrue(
            self.published_snapshot.config_loader.fallback_rules[
                "gateway-model"
            ]["strip_think_tags"]
        )
        self.assertIn('"strip_think_tags": true', self.rules_path.read_text(encoding="utf-8"))

    def test_structured_save_persists_tool_call_rescue(self):
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
                            "tool_call_rescue": True,
                        }
                    ]
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["rules"][0]["tool_call_rescue"])
        self.assertTrue(
            self.published_snapshot.config_loader.fallback_rules[
                "gateway-model"
            ]["tool_call_rescue"]
        )
        self.assertIn('"tool_call_rescue": true', self.rules_path.read_text(encoding="utf-8"))

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
        self.assertTrue(
            self.published_snapshot.config_loader.fallback_rules[
                "gateway-model"
            ]["compress_tool_results"]
        )
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

    def test_structured_save_persists_provider_routing_and_upstream_key_pools(self):
        payload = {
            "providers": [
                {"name": "openrouter", "baseUrl": "https://openrouter.example", "apikey": "DIRECT-KEY"},
                {
                    "name": "devbox",
                    "baseUrl": "https://devbox.example",
                    "apikey": "DIRECT-KEY",
                    "type": "openai",
                    "routing": {
                        "strategy": "priority",
                        "session_affinity": True,
                        "session_affinity_header": "X-Workspace-Session",
                        "session_affinity_ttl_seconds": 900,
                    },
                    "upstream_key_pools": {
                        "main": {
                            "strategy": "priority",
                            "keys": [
                                {"id": "primary", "apikey": "DIRECT-KEY-1", "priority": 100},
                                {"id": "secondary", "apikey": "DIRECT-KEY-2", "priority": 10},
                            ],
                        }
                    },
                },
            ]
        }

        with self._client() as (client, _):
            save_response = client.post(
                "/v1/config/providers/structured",
                json=payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            get_response = client.get(
                "/v1/config/providers/structured",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(save_response.status_code, 200, save_response.text)
        self.assertEqual(get_response.status_code, 200, get_response.text)
        devbox = next(provider for provider in get_response.json()["providers"] if provider["name"] == "devbox")
        self.assertEqual(devbox["routing"]["strategy"], "priority")
        self.assertTrue(devbox["routing"]["session_affinity"])
        self.assertEqual(devbox["upstream_key_pools"]["main"]["keys"][0]["id"], "primary")
        published_loader = self.published_snapshot.config_loader
        self.assertEqual(
            published_loader.providers_config["devbox"].apikey,
            "DIRECT-KEY",
        )
        self.assertEqual(
            published_loader.providers_config["devbox"]
            .upstream_key_pools["main"]
            .keys[0]
            .priority,
            100,
        )
        persisted = self.providers_path.read_text(encoding="utf-8")
        self.assertIn('"upstream_key_pools"', persisted)
        self.assertIn('"session_affinity_header": "X-Workspace-Session"', persisted)

    def test_structured_save_rejects_provider_auth(self):
        payload = {
            "providers": [
                {"name": "openrouter", "baseUrl": "https://openrouter.example", "apikey": "DIRECT-KEY"},
                {
                    "name": "devbox",
                    "baseUrl": "https://devbox.example",
                    "apikey": "DIRECT-KEY",
                    "available_models": ["provider-model", "provider-fast"],
                },
                {
                    "name": "codex",
                    "baseUrl": "https://codex.example",
                    "type": "openai",
                    "auth": {"type": "codex_oauth", "token_env": "CODEX_OAUTH_TOKEN"},
                },
            ]
        }

        with self._client() as (client, _):
            save_response = client.post(
                "/v1/config/providers/structured",
                json=payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(save_response.status_code, 400, save_response.text)
        self.assertEqual(
            save_response.json()["detail"]["code"],
            "config_validation_failed",
        )
        self.assertNotIn("codex_oauth", save_response.text)

    def test_structured_save_persists_payload_transforms_on_fallback_rows(self):
        fake_http_client = Mock()
        fake_http_client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "provider-model"}]},
                request=httpx.Request("GET", "https://devbox.example/models"),
            )
        )
        payload = {
            "rules": [
                {
                    "gateway_model_name": "gateway-model",
                    "fallback_models": [
                        {
                            "provider": "devbox",
                            "model": "provider-model",
                            "payload_transforms": {
                                "defaults": {"top_p": 0.9},
                                "overrides": {"parallel_tool_calls": False},
                                "filters": ["seed"],
                            },
                        }
                    ],
                }
            ]
        }

        with self._client(fake_http_client) as (client, _):
            response = client.post(
                "/v1/config/models-rules/structured",
                json=payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        transforms = self.published_snapshot.config_loader.fallback_rules[
            "gateway-model"
        ]["fallback_models"][0]["payload_transforms"]
        self.assertEqual(transforms["filters"], ["seed"])
        self.assertIn('"payload_transforms"', self.rules_path.read_text(encoding="utf-8"))

    def test_model_rules_raw_endpoint_saves_aliases_and_pools(self):
        payload = """
        {
          "aliases": {"public-fast": "pool-fast"},
          "upstream_model_pools": {
            "pool-fast": {
              "fallback_models": [
                {"provider": "devbox", "model": "provider-fast"}
              ]
            }
          }
        }
        """.strip()

        with self._client() as (client, _):
            response = client.post(
                "/v1/config/model-rules",
                content=payload,
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )
            get_response = client.get(
                "/v1/config/model-rules",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(get_response.status_code, 200, get_response.text)
        self.assertIn('"public-fast"', get_response.text)
        self.assertIn(
            "pool-fast",
            self.published_snapshot.config_loader.fallback_rules,
        )
        self.assertIn('"upstream_model_pools"', self.model_rules_path.read_text(encoding="utf-8"))

    def test_models_rules_raw_save_rejects_existing_alias_that_new_rules_break(self):
        self.model_rules_path.write_text(
            '{"aliases": {"public-model": "gateway-model"}}\n',
            encoding="utf-8",
        )
        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
            fusion_rules_filename=str(self.fusion_rules_path),
            model_rules_filename=str(self.model_rules_path),
            router_rules_filename=str(self.router_rules_path),
        ).load_complete()
        original_rules_text = self.rules_path.read_text(encoding="utf-8")
        payload = """
        [
          {
            "gateway_model_name": "renamed-model",
            "fallback_models": [
              {"provider": "devbox", "model": "provider-model"}
            ],
            "rotate_models": false
          }
        ]
        """.strip()

        with self._client() as (client, _):
            response = client.post(
                "/v1/config/models-rules",
                content=payload,
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "config_validation_failed",
        )
        self.assertEqual(
            response.json()["detail"]["errors"],
            [
                {
                    "type": "rule_validation",
                    "loc": [],
                    "msg": (
                        "model_rules alias 'public-model' references unknown "
                        "target model 'gateway-model'."
                    ),
                }
            ],
        )
        self.assertEqual(self.rules_path.read_text(encoding="utf-8"), original_rules_text)


if __name__ == "__main__":
    unittest.main()
