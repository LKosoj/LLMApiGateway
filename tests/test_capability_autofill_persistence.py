import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.services.capability_autofill import CapabilityAutofillService
from llm_gateway_core.services.config_updates import ConfigUpdateCoordinator
from llm_gateway_core.services.openrouter_free_models import OpenRouterFreeModelsService
from llm_gateway_core.services.runtime_config import RuntimeGenerationManager
from tests._async_compat import run_async
from tests.runtime_test_support import install_test_runtime_snapshot, make_runtime_snapshot


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


FALLBACK_RULES_TEXT = """
// Fallback rules
[
  {
    "gateway_model_name": "gateway-model",
    "fallback_models": [
      {"provider": "devbox", "model": "provider-model"}
    ],
    "rotate_models": false
  }
]
""".strip()


async def _devbox_model_catalog(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [
                {
                    "id": "provider-model",
                    "architecture": {"input_modalities": ["text", "image"]},
                    "supported_parameters": ["tools"],
                    "context_length": 64000,
                }
            ]
        },
        request=request,
    )


class CapabilityAutofillPersistenceTests(unittest.TestCase):
    """Real ConfigLoader + ConfigUpdateCoordinator round trip, confined to a temp dir."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(FALLBACK_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text("{}", encoding="utf-8")
        self.fallback_provider_patcher = patch(
            "llm_gateway_core.config.loader.settings.fallback_provider",
            "openrouter",
        )
        self.fallback_provider_patcher.start()

        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
            fusion_rules_filename=str(Path(self.temp_dir.name) / "models_fusion_rules.json"),
            model_rules_filename=str(Path(self.temp_dir.name) / "models_model_rules.json"),
            router_rules_filename=str(Path(self.temp_dir.name) / "models_router_rules.json"),
        )
        self.config_loader.load_complete()

    def tearDown(self):
        self.fallback_provider_patcher.stop()
        self.temp_dir.cleanup()

    def test_materialize_writes_resolved_capabilities_and_creates_backup(self):
        async def scenario():
            manager = RuntimeGenerationManager()
            shared_client = httpx.AsyncClient(
                transport=httpx.MockTransport(_devbox_model_catalog)
            )
            try:
                snapshot = make_runtime_snapshot(
                    generation=1,
                    config_loader=self.config_loader,
                    http_client=shared_client,
                )
                install_test_runtime_snapshot(manager, snapshot)
                coordinator = ConfigUpdateCoordinator(
                    runtime_manager=manager,
                    shared_http_client=shared_client,
                    initial_snapshot=snapshot,
                )
                try:
                    openrouter_service = OpenRouterFreeModelsService()
                    service = CapabilityAutofillService()
                    await service.materialize(
                        runtime_manager=manager,
                        config_update_coordinator=coordinator,
                        shared_http_client=shared_client,
                        openrouter_free_models_service=openrouter_service,
                    )
                finally:
                    await coordinator.close()
            finally:
                await manager.shutdown()
                await shared_client.aclose()

        run_async(scenario())

        rewritten = json.loads(self.rules_path.read_text(encoding="utf-8"))
        candidate = rewritten[0]["fallback_models"][0]
        self.assertTrue(candidate["supports_vision"])
        self.assertTrue(candidate["supports_tools"])
        self.assertEqual(candidate["context_window"], 64000)
        self.assertEqual(
            sorted(candidate["capabilities_autofilled"]),
            ["context_window", "supports_tools", "supports_vision"],
        )

        backups = list(self.rules_path.parent.glob("models_fallback_rules.json.bak.*"))
        self.assertEqual(len(backups), 1)

        # A reload from disk must see the same materialized marker (proves the write
        # actually reached the file the loader reads, not just an in-memory candidate).
        reloaded = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
            fusion_rules_filename=str(Path(self.temp_dir.name) / "models_fusion_rules.json"),
            model_rules_filename=str(Path(self.temp_dir.name) / "models_model_rules.json"),
            router_rules_filename=str(Path(self.temp_dir.name) / "models_router_rules.json"),
        )
        reloaded.load_complete()
        reloaded_candidate = reloaded._fallback_rules_base["gateway-model"]["fallback_models"][0]
        self.assertEqual(
            sorted(reloaded_candidate["capabilities_autofilled"]),
            ["context_window", "supports_tools", "supports_vision"],
        )


if __name__ == "__main__":
    unittest.main()
