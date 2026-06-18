"""ConfigError replaces sys.exit(1) in loader.

The old loader called ``sys.exit(1)`` on every startup-level failure, making
it impossible for callers (tests, long-running services) to recover or even
assert on the failure. Now failures raise ``ConfigError`` instead — still a
hard failure at startup (FastAPI lifespan propagates the exception), but
catchable and testable.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from llm_gateway_core.config.loader import (
    ConfigError,
    ConfigLoader,
    resolve_provider_config_api_keys,
    resolve_provider_config_auth_headers,
)


VALID_PROVIDERS_SINGLE = """
[
  {
    "openrouter": {
      "baseUrl": "https://openrouter.example",
      "apikey": "DIRECT-KEY"
    }
  }
]
""".strip()


class LoaderConfigErrorTests(unittest.TestCase):
    def test_missing_providers_file_raises_config_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_loader = ConfigLoader(
                providers_filename=str(Path(temp_dir) / "does_not_exist.json"),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )
            with self.assertRaises(ConfigError) as ctx:
                config_loader.load_providers()
        self.assertIn("not found", str(ctx.exception))

    def test_fallback_provider_missing_raises_config_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(VALID_PROVIDERS_SINGLE, encoding="utf-8")

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "does-not-exist"):
                with self.assertRaises(ConfigError) as ctx:
                    config_loader.load_providers()

        self.assertIn("FALLBACK_PROVIDER", str(ctx.exception))
        self.assertIn("does-not-exist", str(ctx.exception))

    def test_invalid_json_raises_config_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text("this is not json", encoding="utf-8")

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with self.assertRaises(ConfigError):
                config_loader.load_providers()

    def test_config_error_is_catchable(self):
        """Regression guard — the whole point of this change is that callers
        can catch failures instead of being forced out via ``sys.exit``.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            config_loader = ConfigLoader(
                providers_filename=str(Path(temp_dir) / "nope.json"),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )
            caught = None
            try:
                config_loader.load_providers()
            except ConfigError as exc:
                caught = exc
            self.assertIsNotNone(caught)

    def test_provider_can_use_structured_upstream_key_pool_without_legacy_apikey(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "openrouter": {
                      "baseUrl": "https://openrouter.example",
                      "routing": {"strategy": "priority", "session_affinity": true},
                      "upstream_key_pools": {
                        "main": {
                          "keys": [
                            {"id": "primary", "apikey": "DIRECT-KEY-1", "priority": 100},
                            {"id": "secondary", "apikey": "DIRECT-KEY-2", "priority": 10}
                          ]
                        }
                      }
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "openrouter"):
                providers = config_loader.load_providers()

        provider = providers["openrouter"]
        self.assertIsNone(provider.apikey)
        self.assertEqual(provider.routing.strategy, "priority")
        self.assertTrue(provider.routing.session_affinity)
        self.assertEqual(provider.upstream_key_pools["main"].keys[0].id, "primary")

    def test_provider_config_api_keys_resolves_enabled_structured_pool_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "openrouter": {
                      "baseUrl": "https://openrouter.example",
                      "upstream_key_pools": {
                        "main": {
                          "keys": [
                            {"id": "primary", "apikey": "DIRECT-KEY-1,DIRECT-KEY-2"},
                            {"id": "disabled", "apikey": "DIRECT-KEY-3", "enabled": false}
                          ]
                        }
                      }
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )
            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "openrouter"):
                providers = config_loader.load_providers()

        self.assertEqual(
            resolve_provider_config_api_keys(providers["openrouter"]),
            ["DIRECT-KEY-1", "DIRECT-KEY-2"],
        )

    def test_provider_can_use_codex_oauth_without_legacy_apikey(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "openrouter": {
                      "baseUrl": "https://openrouter.example",
                      "auth": {"type": "codex_oauth", "token_env": "CODEX_OAUTH_TOKEN"}
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "openrouter"):
                with patch.dict("os.environ", {"CODEX_OAUTH_TOKEN": "codex-access-token"}):
                    providers = config_loader.load_providers()
                    provider = providers["openrouter"]
                    self.assertEqual(resolve_provider_config_api_keys(provider), ["codex-access-token"])
                    self.assertEqual(
                        resolve_provider_config_auth_headers(provider),
                        {"Authorization": "Bearer codex-access-token"},
                    )

    def test_provider_can_use_managed_oauth_credential(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "codex": {
                      "baseUrl": "https://codex.example",
                      "auth": {
                        "type": "codex_oauth",
                        "credential_id": "codex-main",
                        "oauth_client": {
                          "client_id": "client-id",
                          "token_endpoint": "https://issuer.example/oauth/token",
                          "device_authorization_endpoint": "https://issuer.example/oauth/device/code",
                          "scopes": ["openid", "profile"]
                        }
                      }
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "codex"):
                providers = config_loader.load_providers()

        provider = providers["codex"]
        self.assertEqual(provider.auth.credential_id, "codex-main")
        self.assertEqual(provider.auth.oauth_client.client_id, "client-id")

    def test_managed_oauth_rejects_missing_oauth_client(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "codex": {
                      "baseUrl": "https://codex.example",
                      "auth": {"type": "codex_oauth", "credential_id": "codex-main"}
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "codex"):
                with self.assertRaises(ConfigError) as ctx:
                    config_loader.load_providers()

        self.assertIn("oauth_client", str(ctx.exception))

    def test_managed_oauth_rejects_cli_spoof_scope_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "xai": {
                      "baseUrl": "https://api.x.ai/v1",
                      "auth": {
                        "type": "xai_oauth",
                        "credential_id": "xai-main",
                        "oauth_client": {
                          "client_id": "client-id",
                          "token_endpoint": "https://issuer.example/oauth/token",
                          "device_authorization_endpoint": "https://issuer.example/oauth/device/code",
                          "scopes": ["grok-cli:access"]
                        }
                      }
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "xai"):
                with self.assertRaises(ConfigError) as ctx:
                    config_loader.load_providers()

        self.assertIn("spoofing", str(ctx.exception))

    def test_managed_oauth_rejects_plain_http_remote_endpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "codex": {
                      "baseUrl": "https://codex.example",
                      "auth": {
                        "type": "codex_oauth",
                        "credential_id": "codex-main",
                        "oauth_client": {
                          "client_id": "client-id",
                          "token_endpoint": "http://issuer.example/oauth/token"
                        }
                      }
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "codex"):
                with self.assertRaises(ConfigError) as ctx:
                    config_loader.load_providers()

        self.assertIn("must use HTTPS", str(ctx.exception))

    def test_managed_oauth_allows_plain_http_loopback_endpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "codex": {
                      "baseUrl": "https://codex.example",
                      "auth": {
                        "type": "codex_oauth",
                        "credential_id": "codex-main",
                        "oauth_client": {
                          "client_id": "client-id",
                          "token_endpoint": "http://127.0.0.1:8123/oauth/token"
                        }
                      }
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "codex"):
                providers = config_loader.load_providers()

        self.assertEqual(providers["codex"].auth.oauth_client.token_endpoint, "http://127.0.0.1:8123/oauth/token")

    def test_managed_oauth_rejects_plain_http_remote_redirect_uri(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "codex": {
                      "baseUrl": "https://codex.example",
                      "auth": {
                        "type": "codex_oauth",
                        "credential_id": "codex-main",
                        "oauth_client": {
                          "client_id": "client-id",
                          "token_endpoint": "https://issuer.example/oauth/token",
                          "authorization_endpoint": "https://issuer.example/oauth/authorize",
                          "redirect_uri": "http://issuer.example/oauth/callback"
                        }
                      }
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "codex"):
                with self.assertRaises(ConfigError) as ctx:
                    config_loader.load_providers()

        self.assertIn("redirect_uri must use HTTPS", str(ctx.exception))

    def test_managed_oauth_allows_plain_http_loopback_redirect_uri(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "codex": {
                      "baseUrl": "https://codex.example",
                      "auth": {
                        "type": "codex_oauth",
                        "credential_id": "codex-main",
                        "oauth_client": {
                          "client_id": "client-id",
                          "token_endpoint": "https://issuer.example/oauth/token",
                          "authorization_endpoint": "https://issuer.example/oauth/authorize",
                          "redirect_uri": "http://localhost:8123/v1/auth/oauth/callback/codex"
                        }
                      }
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "codex"):
                providers = config_loader.load_providers()

        self.assertEqual(
            providers["codex"].auth.oauth_client.redirect_uri,
            "http://localhost:8123/v1/auth/oauth/callback/codex",
        )

    def test_managed_oauth_rejects_duplicate_credential_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "codex": {
                      "baseUrl": "https://codex.example",
                      "auth": {
                        "type": "codex_oauth",
                        "credential_id": "shared-main",
                        "oauth_client": {
                          "client_id": "client-id",
                          "token_endpoint": "https://issuer.example/oauth/token"
                        }
                      }
                    }
                  },
                  {
                    "xai": {
                      "baseUrl": "https://api.x.ai/v1",
                      "auth": {
                        "type": "xai_oauth",
                        "credential_id": "shared-main",
                        "oauth_client": {
                          "client_id": "client-id",
                          "token_endpoint": "https://issuer.example/oauth/token"
                        }
                      }
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "codex"):
                with self.assertRaises(ConfigError) as ctx:
                    config_loader.load_providers()

        self.assertIn("credential_id 'shared-main' is reused", str(ctx.exception))

    def test_provider_oauth_rejects_legacy_apikey_mix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "openrouter": {
                      "baseUrl": "https://openrouter.example",
                      "apikey": "LEGACY-KEY",
                      "auth": {"type": "codex_oauth", "token_env": "CODEX_OAUTH_TOKEN"}
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "openrouter"):
                with patch.dict("os.environ", {"CODEX_OAUTH_TOKEN": "codex-access-token"}):
                    with self.assertRaises(ConfigError) as ctx:
                        config_loader.load_providers()

        self.assertIn("OAuth 'auth' must not be combined", str(ctx.exception))

    def test_provider_oauth_missing_env_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "openrouter": {
                      "baseUrl": "https://openrouter.example",
                      "auth": {"type": "xai_oauth", "token_env": "XAI_OAUTH_TOKEN"}
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "openrouter"):
                with patch.dict("os.environ", {}, clear=True):
                    with self.assertRaises(ConfigError) as ctx:
                        config_loader.load_providers()

        self.assertIn("XAI_OAUTH_TOKEN", str(ctx.exception))

    def test_structured_upstream_key_pool_env_placeholder_raises_config_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "openrouter": {
                      "baseUrl": "https://openrouter.example",
                      "upstream_key_pools": {
                        "main": {
                          "keys": [{"id": "primary", "apikey": "${OPENROUTER_API_KEY}"}]
                        }
                      }
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "openrouter"):
                with patch.dict("os.environ", {"OPENROUTER_API_KEY": "your-openrouter-api-key"}):
                    with self.assertRaises(ConfigError) as ctx:
                        config_loader.load_providers()

        self.assertIn("OPENROUTER_API_KEY", str(ctx.exception))
        self.assertIn("placeholder", str(ctx.exception))

    def test_pool_only_provider_requires_fallback_rule_upstream_key_pool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "openrouter": {
                      "baseUrl": "https://openrouter.example",
                      "upstream_key_pools": {
                        "main": {
                          "keys": [{"id": "primary", "apikey": "DIRECT-KEY"}]
                        }
                      }
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )
            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "openrouter"):
                providers = config_loader.load_providers()

            with self.assertRaises(ValueError) as ctx:
                config_loader.parse_and_validate_fallback_rules_payload(
                    """
                    [
                      {
                        "gateway_model_name": "gateway-model",
                        "fallback_models": [{"provider": "openrouter", "model": "provider-model"}]
                      }
                    ]
                    """,
                    providers_config=providers,
                )

        self.assertIn("upstream_key_pool", str(ctx.exception))

    def test_unknown_fallback_rule_upstream_key_pool_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "openrouter": {
                      "baseUrl": "https://openrouter.example",
                      "apikey": "DIRECT-KEY",
                      "upstream_key_pools": {
                        "main": {
                          "keys": [{"id": "primary", "apikey": "DIRECT-KEY-2"}]
                        }
                      }
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )
            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "openrouter"):
                providers = config_loader.load_providers()

            with self.assertRaises(ValueError) as ctx:
                config_loader.parse_and_validate_fallback_rules_payload(
                    """
                    [
                      {
                        "gateway_model_name": "gateway-model",
                        "fallback_models": [
                          {
                            "provider": "openrouter",
                            "model": "provider-model",
                            "upstream_key_pool": "missing"
                          }
                        ]
                      }
                    ]
                    """,
                    providers_config=providers,
                )

        self.assertIn("Pool not found", str(ctx.exception))

    def test_fallback_rule_accepts_payload_transforms(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(VALID_PROVIDERS_SINGLE, encoding="utf-8")
            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "openrouter"):
                providers = config_loader.load_providers()

            rules = config_loader.parse_and_validate_fallback_rules_payload(
                """
                [
                  {
                    "gateway_model_name": "gateway-model",
                    "fallback_models": [
                      {
                        "provider": "openrouter",
                        "model": "provider-model",
                        "payload_transforms": {
                          "defaults": {"top_p": 0.9},
                          "overrides": {"parallel_tool_calls": false},
                          "filters": ["seed"]
                        }
                      }
                    ]
                  }
                ]
                """,
                providers_config=providers,
            )

        transforms = rules["gateway-model"]["fallback_models"][0]["payload_transforms"]
        self.assertEqual(transforms["defaults"], {"top_p": 0.9})
        self.assertEqual(transforms["overrides"], {"parallel_tool_calls": False})
        self.assertEqual(transforms["filters"], ["seed"])

    def test_fallback_rule_rejects_reserved_payload_transform_field(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(VALID_PROVIDERS_SINGLE, encoding="utf-8")
            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "openrouter"):
                providers = config_loader.load_providers()

            with self.assertRaises(ValueError) as ctx:
                config_loader.parse_and_validate_fallback_rules_payload(
                    """
                    [
                      {
                        "gateway_model_name": "gateway-model",
                        "fallback_models": [
                          {
                            "provider": "openrouter",
                            "model": "provider-model",
                            "payload_transforms": {
                              "overrides": {"model": "other-model"}
                            }
                          }
                        ]
                      }
                    ]
                    """,
                    providers_config=providers,
                )

        self.assertIn("reserved", str(ctx.exception))

    def test_model_rules_upstream_model_pool_extends_fallback_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            model_rules_path = Path(temp_dir) / "models_model_rules.json"
            providers_path.write_text(VALID_PROVIDERS_SINGLE, encoding="utf-8")
            model_rules_path.write_text(
                """
                {
                  "aliases": {"public-fast": "pool-fast"},
                  "upstream_model_pools": {
                    "pool-fast": {
                      "fallback_models": [
                        {"provider": "openrouter", "model": "provider-fast"},
                        {"provider": "openrouter", "model": "provider-backup"}
                      ],
                      "rotate_models": true
                    }
                  }
                }
                """,
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
                model_rules_filename=str(model_rules_path),
            )

            with patch.object(main.settings, "fallback_provider", "openrouter"):
                config_loader.load_providers()
                config_loader.fallback_rules = {}
                model_rules = config_loader.load_model_rules()

        self.assertEqual(model_rules["aliases"], {"public-fast": "pool-fast"})
        self.assertIn("pool-fast", config_loader.fallback_rules)
        self.assertTrue(config_loader.fallback_rules["pool-fast"]["rotate_models"])
        self.assertEqual(
            config_loader.fallback_rules["pool-fast"]["fallback_models"][0]["model"],
            "provider-fast",
        )

    def test_model_rules_reject_unknown_alias_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(VALID_PROVIDERS_SINGLE, encoding="utf-8")
            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(Path(temp_dir) / "fr.json"),
            )

            with patch.object(main.settings, "fallback_provider", "openrouter"):
                providers = config_loader.load_providers()

            with self.assertRaises(ValueError) as ctx:
                config_loader.parse_and_validate_model_rules_payload(
                    '{"aliases": {"public-fast": "missing-model"}}',
                    providers_config=providers,
                    fallback_rules={},
                )

        self.assertIn("unknown target", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
