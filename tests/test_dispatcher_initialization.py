import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main


class DispatcherInitializationTests(unittest.TestCase):
    @patch("main.start_usage_stats_cleanup_task")
    @patch("llm_gateway_core.services.runtime_candidate.OperationDispatcher")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_lifespan_initializes_operation_dispatcher_in_app_state(
        self,
        config_loader_ctor,
        create_shared_http_client,
        operation_dispatcher_ctor,
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
        fake_config_loader.load_complete.return_value = fake_config_loader
        config_loader_ctor.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        create_shared_http_client.return_value = fake_http_client

        fake_operation_dispatcher = Mock()
        operation_dispatcher_ctor.return_value = fake_operation_dispatcher
        fake_config_update_coordinator = Mock()
        fake_config_update_coordinator.close = AsyncMock()

        with (
            patch.object(main.settings, "gateway_api_key", "test-gateway-key"),
            patch.object(main.AtomicConfigFileTransaction, "recover_pending"),
            patch.object(
                main,
                "ConfigUpdateCoordinator",
                return_value=fake_config_update_coordinator,
            ),
        ):
            with TestClient(main.app) as client:
                services = client.app.state.services
                self.assertIs(services.http_client, fake_http_client)
                lease = client.portal.call(services.runtime_manager.acquire_current)
                try:
                    self.assertIs(
                        lease.snapshot.operation_dispatcher,
                        fake_operation_dispatcher,
                    )
                finally:
                    client.portal.call(lease.release)
                operation_dispatcher_ctor.assert_called_once_with(
                    fake_config_loader.providers_config,
                    fake_config_loader.operation_rules,
                    fake_http_client,
                    model_rules={},
                )
                start_usage_stats_cleanup_task.assert_called_once_with(
                    services.tokens_usage_db,
                    services.fallback_events_db,
                    services.rejections_db,
                    supervisor=services.task_supervisor,
                )

        self.assertTrue(services.task_supervisor.closed)
        fake_http_client.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
