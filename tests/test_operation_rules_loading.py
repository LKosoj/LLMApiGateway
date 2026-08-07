import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import json5

import main
from llm_gateway_core.config.loader import ConfigError, ConfigLoader


VALID_PROVIDERS_TEXT = """
[
  {
    "openrouter": {
      "baseUrl": "https://openrouter.example",
      "apikey": "DIRECT-KEY"
    }
  },
  {
    "openai_codex": {
      "baseUrl": "https://openai-codex.example",
      "apikey": "DIRECT-KEY"
    }
  },
  {
    "cohere": {
      "baseUrl": "https://cohere.example",
      "apikey": "DIRECT-KEY"
    }
  },
  {
    "nvidia": {
      "baseUrl": "https://integrate.api.nvidia.com/v1",
      "apikey": "DIRECT-KEY"
    }
  },
  {
    "cloudru": {
      "baseUrl": "https://foundation-models.api.cloud.ru",
      "apikey": "DIRECT-KEY"
    }
  },
  {
    "vsegpt": {
      "baseUrl": "https://api.vsegpt.ru:7090/v1",
      "apikey": "DIRECT-KEY"
    }
  },
  {
    "aitunnel": {
      "baseUrl": "https://api.aitunnel.ru/v1/",
      "apikey": "DIRECT-KEY"
    }
  },
  {
    "anymodel": {
      "baseUrl": "https://anymodel.org/v1",
      "apikey": "DIRECT-KEY"
    }
  },
  {
    "llm_ai": {
      "baseUrl": "http://94.143.43.118:18080/v1/",
      "apikey": "DIRECT-KEY"
    }
  },
  {
    "z.ai": {
      "baseUrl": "https://api.z.ai/api/coding/paas/v4",
      "apikey": "DIRECT-KEY"
    }
  }
]
""".strip()


PROJECT_ROOT_OPERATION_RULES_PATH = Path(__file__).resolve().parent.parent / "models_operation_rules.json"
PROJECT_ROOT_FALLBACK_RULES_PATH = Path(__file__).resolve().parent.parent / "models_fallback_rules.json"


INVALID_OPERATION_RULES_TEXT = """
{
  "embeddings": [
    {
      "gateway_model_name": "embed-model",
      "routes": [
        {
          "provider": "missing-provider",
          "model": "text-embedding-3-small",
          "target_path": "/embeddings"
        }
      ]
    }
  ],
  "rerank": [],
  "images_generations": [],
  "images_edits": []
}
""".strip()

WEB_RESEARCH_RULES_TEXT = """
{
  "web_search": [
    {
      "gateway_model_name": "llmgateway/web-search",
      "query_model": "gateway-chat"
    }
  ],
  "web_read": [
    {
      "gateway_model_name": "llmgateway/web-read"
    }
  ],
  "rerank": [
    {
      "gateway_model_name": "llmgateway/rerank",
      "routes": [
        {
          "provider": "cohere",
          "model": "rerank-model",
          "target_path": "/rerank"
        }
      ]
    }
  ],
  "web_research": [
    {
      "gateway_model_name": "llmgateway/web-research",
      "search_model": "llmgateway/web-search",
      "read_model": "llmgateway/web-read",
      "rerank_model": "llmgateway/rerank",
      "analysis_model": "gateway-chat"
    }
  ]
}
""".strip()

EMPTY_OPERATION_RULES = {
    "embeddings": {},
    "rerank": {},
    "images_generations": {},
    "images_edits": {},
    "audio_speech": {},
    "audio_transcriptions": {},
    "web_search": {},
    "web_read": {},
    "web_research": {},
    "web_deep_research": {},
    "pdf_conversions": {},
}


class OperationRulesLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.fallback_rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.fallback_rules_path.write_text("[]", encoding="utf-8")
        self.fallback_provider_patcher = patch.object(main.settings, "fallback_provider", "openrouter")
        self.fallback_provider_patcher.start()

    def tearDown(self):
        self.fallback_provider_patcher.stop()
        self.temp_dir.cleanup()

    def _build_loader(self) -> ConfigLoader:
        return ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.fallback_rules_path),
            operation_rules_filename=str(self.operation_rules_path),
        )

    def test_config_loader_init_accepts_operation_rules_filename(self):
        config_loader = self._build_loader()

        self.assertEqual(config_loader.operation_rules_path, self.operation_rules_path)
        self.assertEqual(config_loader.operation_rules, EMPTY_OPERATION_RULES)

    def test_load_operation_rules_accepts_empty_file(self):
        self.operation_rules_path.write_text("", encoding="utf-8")
        config_loader = self._build_loader()
        config_loader.load_providers()

        operation_rules = config_loader.load_operation_rules()

        self.assertEqual(operation_rules, EMPTY_OPERATION_RULES)
        self.assertEqual(config_loader.operation_rules, EMPTY_OPERATION_RULES)

    def test_load_operation_rules_allows_missing_file_with_warning(self):
        config_loader = self._build_loader()
        config_loader.load_providers()

        with self.assertLogs(level="WARNING") as captured_logs:
            operation_rules = config_loader.load_operation_rules()

        self.assertEqual(operation_rules, EMPTY_OPERATION_RULES)
        self.assertIn("Model operation rules file not found", "\n".join(captured_logs.output))

    def test_load_operation_rules_exits_on_invalid_provider(self):
        self.operation_rules_path.write_text(INVALID_OPERATION_RULES_TEXT, encoding="utf-8")
        config_loader = self._build_loader()
        config_loader.load_providers()

        with self.assertLogs(level="ERROR") as captured_logs:
            with self.assertRaises(ConfigError):
                config_loader.load_operation_rules()

        self.assertIn("Invalid provider 'missing-provider'", "\n".join(captured_logs.output))

    def test_project_root_models_operation_rules_example_loads_successfully(self):
        self.assertTrue(PROJECT_ROOT_OPERATION_RULES_PATH.exists())
        self.assertTrue(PROJECT_ROOT_FALLBACK_RULES_PATH.exists())
        self.fallback_rules_path.write_text(
            """
[
  {
    "gateway_model_name": "llmgateway/light_model",
    "fallback_models": [{"provider": "openrouter", "model": "gpt-4o-mini"}],
    "rotate_models": false
  },
  {
    "gateway_model_name": "llmgateway/high",
    "fallback_models": [{"provider": "openrouter", "model": "gpt-4o"}],
    "rotate_models": false
  }
]
""".strip(),
            encoding="utf-8",
        )

        config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.fallback_rules_path),
            operation_rules_filename=str(PROJECT_ROOT_OPERATION_RULES_PATH),
        )
        config_loader.load_providers()
        config_loader.load_fallback_rules()

        operation_rules = config_loader.load_operation_rules()
        project_fallback_rules = json5.loads(
            PROJECT_ROOT_FALLBACK_RULES_PATH.read_text(encoding="utf-8")
        )
        project_gateway_models = {
            rule["gateway_model_name"]
            for rule in project_fallback_rules
            if isinstance(rule, dict) and isinstance(rule.get("gateway_model_name"), str)
        }

        self.assertIn("llmgateway/embedding", operation_rules["embeddings"])
        self.assertIn("llmgateway/rerank", operation_rules["rerank"])
        self.assertIn("llmgateway/flux.image-generation", operation_rules["images_generations"])
        self.assertIn("llmgateway/flux.image-edit", operation_rules["images_edits"])
        self.assertIn("llmgateway/silero-tts", operation_rules["audio_speech"])
        self.assertIn("llmgateway/silero-en-tts", operation_rules["audio_speech"])
        self.assertIn("llmgateway/cosyvoice-tts", operation_rules["audio_speech"])
        self.assertIn("llmgateway/pdf-converter", operation_rules["pdf_conversions"])
        self.assertEqual(
            operation_rules["embeddings"]["llmgateway/embedding"]["routes"][0]["provider"],
            "cloudru",
        )
        self.assertEqual(
            operation_rules["embeddings"]["llmgateway/embedding"]["routes"][0]["model"],
            "Qwen/Qwen3-Embedding-0.6B",
        )
        self.assertEqual(
            operation_rules["embeddings"]["llmgateway/embedding"]["routes"][0]["custom_body_params"],
            {},
        )
        self.assertEqual(
            operation_rules["rerank"]["llmgateway/rerank"]["routes"][0]["provider"],
            "cloudru",
        )
        self.assertEqual(
            operation_rules["rerank"]["llmgateway/rerank"]["routes"][0]["target_path"],
            "https://foundation-models.api.cloud.ru/score",
        )
        self.assertEqual(
            operation_rules["rerank"]["llmgateway/rerank"]["routes"][1]["provider"],
            "llm_ai",
        )
        self.assertEqual(
            operation_rules["rerank"]["llmgateway/rerank"]["routes"][1]["target_path"],
            "http://94.143.43.118:18080/score",
        )
        self.assertNotIn("request_format", operation_rules["rerank"]["llmgateway/rerank"]["routes"][0])
        self.assertNotIn("response_format", operation_rules["rerank"]["llmgateway/rerank"]["routes"][0])
        self.assertEqual(
            operation_rules["embeddings"]["llmgateway/embedding"]["routes"][1]["retry_count"],
            3,
        )
        self.assertEqual(
            operation_rules["rerank"]["llmgateway/rerank"]["routes"][1]["retry_count"],
            3,
        )
        self.assertEqual(
            operation_rules["images_generations"]["llmgateway/flux.image-generation"]["routes"][0]["provider"],
            "aitunnel",
        )
        self.assertEqual(
            operation_rules["images_generations"]["llmgateway/flux.image-generation"]["routes"][0]["model"],
            "gpt-image-2",
        )
        flux_edit_route = operation_rules["images_edits"]["llmgateway/flux.image-edit"]["routes"][0]
        self.assertIn(flux_edit_route["provider"], config_loader.providers_config)
        self.assertEqual(flux_edit_route["model"], "gpt-image-2")
        self.assertEqual(flux_edit_route["target_path"], "/images/edits")
        self.assertEqual(
            operation_rules["audio_speech"]["llmgateway/silero-tts"]["routes"][0]["target_path"],
            "/silero/audio/speech",
        )
        self.assertEqual(
            operation_rules["audio_speech"]["llmgateway/silero-tts"]["routes"][0]["voices_target_path"],
            "/voices",
        )
        self.assertEqual(
            operation_rules["audio_speech"]["llmgateway/silero-en-tts"]["routes"][0]["model"],
            "silero-v3_en",
        )
        self.assertEqual(
            operation_rules["audio_speech"]["llmgateway/silero-en-tts"]["routes"][0]["voices_target_path"],
            "/voices",
        )
        self.assertEqual(
            operation_rules["audio_speech"]["llmgateway/cosyvoice-tts"]["routes"][0]["model"],
            "cosyvoice",
        )
        self.assertEqual(
            operation_rules["audio_speech"]["llmgateway/cosyvoice-tts"]["routes"][0]["voices_target_path"],
            "/voices",
        )
        self.assertEqual(
            operation_rules["pdf_conversions"]["llmgateway/pdf-converter"]["routes"][0]["target_path"],
            "http://94.143.43.118:18080/pdf/api",
        )
        klein_generation_mapping = operation_rules["images_generations"]["llmgateway/flux.2-klein-4b-generation"][
            "routes"
        ][0]["request_mapping"]
        self.assertNotIn("allowed_client_fields", klein_generation_mapping)
        self.assertEqual(klein_generation_mapping.get("fields", {}).get(""), "n")
        self.assertEqual(klein_generation_mapping.get("constants", {}), {})
        self.assertEqual(
            operation_rules["images_edits"]["llmgateway/ai-klein-edit"]["routes"][0]
            .get("request_mapping", {})
            .get("constants", {}),
            {},
        )
        self.assertIn(
            operation_rules["web_search"]["llmgateway/web-search"]["query_model"],
            project_gateway_models,
        )
        self.assertEqual(
            operation_rules["web_research"]["llmgateway/web-research"]["search_model"],
            "llmgateway/web-search",
        )
        self.assertEqual(
            operation_rules["web_research"]["llmgateway/web-research"]["read_model"],
            "llmgateway/web-read",
        )
        self.assertEqual(
            operation_rules["web_research"]["llmgateway/web-research"]["rerank_model"],
            "llmgateway/rerank",
        )
        self.assertEqual(
            operation_rules["web_deep_research"]["llmgateway/web-deep-research"]["search_model"],
            "llmgateway/web-search",
        )
        self.assertEqual(
            operation_rules["web_deep_research"]["llmgateway/web-deep-research"]["read_model"],
            "llmgateway/web-read",
        )
        self.assertEqual(
            operation_rules["web_deep_research"]["llmgateway/web-deep-research"]["fast_model"],
            "llmgateway/light_model",
        )
        self.assertEqual(
            operation_rules["web_deep_research"]["llmgateway/web-deep-research"]["embedding_model"],
            "llmgateway/embedding",
        )
        self.assertEqual(
            operation_rules["web_deep_research"]["llmgateway/web-deep-research"]["image_generation_model"],
            "llmgateway/gpt-image-2",
        )
        self.assertEqual(
            operation_rules["web_deep_research"]["llmgateway/web-deep-research"]["image_generation_size"],
            "1024x1024",
        )


if __name__ == "__main__":
    unittest.main()
