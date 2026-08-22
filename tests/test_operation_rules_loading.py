import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable
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

# Поля секций, которые ссылаются на chat-модель из models_fallback_rules.json.
CHAT_MODEL_REFERENCE_FIELDS = {
    "web_search": ("query_model",),
    "web_research": ("analysis_model",),
    "web_deep_research": ("fast_model", "smart_model", "strategic_model"),
}


def _read_json5(path: Path) -> Any:
    return json5.loads(path.read_text(encoding="utf-8"))


def _operation_rules_providers(raw_operation_rules: Any) -> set[str]:
    providers: set[str] = set()
    for section_rules in raw_operation_rules.values():
        if not isinstance(section_rules, list):
            continue
        for rule in section_rules:
            for route in rule.get("routes") or []:
                provider = route.get("provider")
                if isinstance(provider, str):
                    providers.add(provider)
    return providers


def _fallback_rules_providers(raw_fallback_rules: Any) -> set[str]:
    providers: set[str] = set()
    for rule in raw_fallback_rules:
        referenced = list(rule.get("fallback_models") or [])
        # Валидатор проверяет провайдера context_overflow_fallback наравне с
        # обычными моделями цепочки, поэтому синтетический providers.json
        # должен покрывать и его.
        context_overflow_fallback = rule.get("context_overflow_fallback")
        if isinstance(context_overflow_fallback, dict):
            referenced.append(context_overflow_fallback)
        for fallback_model in referenced:
            provider = fallback_model.get("provider")
            if isinstance(provider, str):
                providers.add(provider)
    return providers


def _providers_payload(provider_names: Iterable[str]) -> str:
    """Синтетический providers.json: тесту важны только имена провайдеров, не их адреса и ключи."""
    return json.dumps(
        [
            {name: {"baseUrl": f"https://{index}.provider.example", "apikey": "DIRECT-KEY"}}
            for index, name in enumerate(sorted(provider_names))
        ]
    )


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

    def _load_project_root_operation_rules(self) -> tuple[ConfigLoader, dict]:
        """Загружает боевые models_operation_rules.json + models_fallback_rules.json.

        providers.json синтезируется из имён провайдеров, реально использованных в обоих
        файлах: тест проверяет согласованность конфигов между собой, а не то, какой именно
        провайдер настроен в деплое сегодня.
        """
        self.assertTrue(PROJECT_ROOT_OPERATION_RULES_PATH.exists())
        self.assertTrue(PROJECT_ROOT_FALLBACK_RULES_PATH.exists())
        raw_operation_rules = _read_json5(PROJECT_ROOT_OPERATION_RULES_PATH)
        raw_fallback_rules = _read_json5(PROJECT_ROOT_FALLBACK_RULES_PATH)

        provider_names = {main.settings.fallback_provider}
        provider_names.update(_operation_rules_providers(raw_operation_rules))
        provider_names.update(_fallback_rules_providers(raw_fallback_rules))
        self.providers_path.write_text(_providers_payload(provider_names), encoding="utf-8")
        self.fallback_rules_path.write_text(
            PROJECT_ROOT_FALLBACK_RULES_PATH.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.fallback_rules_path),
            operation_rules_filename=str(PROJECT_ROOT_OPERATION_RULES_PATH),
        )
        config_loader.load_providers()
        config_loader.load_fallback_rules()

        return config_loader, config_loader.load_operation_rules()

    def test_project_root_operation_rules_load_against_project_root_fallback_rules(self):
        _config_loader, operation_rules = self._load_project_root_operation_rules()

        self.assertEqual(set(operation_rules), set(EMPTY_OPERATION_RULES))
        configured_models = [name for section in operation_rules.values() for name in section]
        self.assertTrue(configured_models, "боевые operation rules не должны быть пустыми")

    def test_project_root_operation_rules_reference_project_root_chat_models(self):
        _config_loader, operation_rules = self._load_project_root_operation_rules()
        chat_models = {
            rule["gateway_model_name"]
            for rule in _read_json5(PROJECT_ROOT_FALLBACK_RULES_PATH)
            if isinstance(rule, dict) and isinstance(rule.get("gateway_model_name"), str)
        }

        checked_references = 0
        for section_name, field_names in CHAT_MODEL_REFERENCE_FIELDS.items():
            for gateway_model_name, config in operation_rules[section_name].items():
                for field_name in field_names:
                    referenced_model = config.get(field_name)
                    if referenced_model is None:
                        continue
                    checked_references += 1
                    self.assertIn(
                        referenced_model,
                        chat_models,
                        f"{section_name}.{gateway_model_name}.{field_name}",
                    )

        self.assertTrue(checked_references, "боевые operation rules должны ссылаться на chat-модели")


if __name__ == "__main__":
    unittest.main()
