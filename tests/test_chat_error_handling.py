import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from tests._async_compat import run_async
from llm_gateway_core.services.request_handler import make_llm_request


class _InvalidJsonResponse:
    def __init__(self, text: str = "not-json", status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def json(self):
        raise ValueError("Expecting value")


class _NullJsonResponse:
    def __init__(self, parsed=None, text: str = "null", status_code: int = 200):
        self._parsed = parsed
        self.text = text
        self.status_code = status_code

    def json(self):
        return self._parsed


class ChatErrorHandlingTests(unittest.TestCase):
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_missing_provider_returns_safe_503_response(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
    ):
        fake_config_loader = Mock()
        fake_config_loader.providers_config = {}
        fake_config_loader.fallback_rules = {
            "gateway-model": {
                "fallback_models": [
                    {
                        "provider": "missing-provider",
                        "model": "provider-model",
                        "use_provider_order_as_fallback": False,
                    }
                ],
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={"model": "gateway-model", "messages": [{"role": "user", "content": "hello"}]},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {
                "detail": "All configured providers failed for model 'gateway-model'. Last error: Configured provider is unavailable for the requested model."
            },
        )
        self.assertNotIn("Traceback", response.text)
        self.assertNotIn("AttributeError", response.text)

    def test_make_llm_request_invalid_json_response_returns_controlled_error(self):
        fake_client = Mock()
        fake_client.post = AsyncMock(return_value=_InvalidJsonResponse())

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
        self.assertIn("Invalid JSON response", error_detail)
        self.assertNotIn("name 'e'", error_detail)

    def test_make_llm_request_null_json_body_returns_controlled_error(self):
        fake_client = Mock()
        fake_client.post = AsyncMock(return_value=_NullJsonResponse(parsed=None, text="null"))

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
        self.assertIn("Non-object JSON body", error_detail)
        self.assertIn("type=NoneType", error_detail)
        self.assertNotIn("NoneType' is not iterable", error_detail)

    def test_make_llm_request_list_json_body_returns_controlled_error(self):
        fake_client = Mock()
        fake_client.post = AsyncMock(return_value=_NullJsonResponse(parsed=[1, 2, 3], text="[1,2,3]"))

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
        self.assertIn("Non-object JSON body", error_detail)
        self.assertIn("type=list", error_detail)

    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_missing_model_returns_validation_400_without_internal_error(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
    ):
        fake_config_loader = Mock()
        fake_config_loader.providers_config = {}
        fake_config_loader.fallback_rules = {}
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hello"}]},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "Missing 'model' in request body"})
        self.assertNotIn("Internal server error", response.text)


if __name__ == "__main__":
    unittest.main()
