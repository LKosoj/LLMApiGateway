import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main


class _FakeCleanupTask:
    def __init__(self):
        self.cancel_called = False

    def cancel(self):
        self.cancel_called = True

    def __await__(self):
        async def _done():
            return None

        return _done().__await__()


class DispatcherInitializationTests(unittest.TestCase):
    @patch("main.start_usage_stats_cleanup_task")
    @patch("main.TokensUsageDB")
    @patch("main.OperationDispatcher")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_lifespan_initializes_operation_dispatcher_in_app_state(
        self,
        config_loader_ctor,
        create_shared_http_client,
        operation_dispatcher_ctor,
        _tokens_usage_db,
        start_usage_stats_cleanup_task,
    ):
        fake_config_loader = Mock()
        fake_config_loader.providers_config = {
            "openrouter": {
                "baseUrl": "https://openrouter.example",
                "apikey": "DIRECT-KEY",
            }
        }
        fake_config_loader.operation_rules = {
            "embeddings": {
                "gateway/embed-small": {
                    "routes": [
                        {
                            "provider": "openrouter",
                            "model": "text-embedding-3-small",
                            "target_path": "/embeddings",
                        }
                    ]
                }
            },
            "rerank": {},
        }
        config_loader_ctor.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        create_shared_http_client.return_value = fake_http_client

        fake_operation_dispatcher = Mock()
        operation_dispatcher_ctor.return_value = fake_operation_dispatcher

        fake_cleanup_task = _FakeCleanupTask()
        start_usage_stats_cleanup_task.return_value = fake_cleanup_task

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                self.assertIs(client.app.state.http_client, fake_http_client)
                self.assertIs(client.app.state.operation_dispatcher, fake_operation_dispatcher)
                operation_dispatcher_ctor.assert_called_once_with(
                    fake_config_loader.providers_config,
                    fake_config_loader.operation_rules,
                    fake_http_client,
                    model_rules={},
                )

        self.assertTrue(fake_cleanup_task.cancel_called)
        fake_http_client.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
