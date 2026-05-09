import importlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOTENV_PATH = PROJECT_ROOT / ".env"
SETTINGS_MODULE_NAME = "llm_gateway_core.config.settings"


class SettingsEnvPriorityTests(unittest.TestCase):
    def test_process_env_overrides_dotenv_values_after_settings_reload(self):
        original_exists = DOTENV_PATH.exists()
        original_content = DOTENV_PATH.read_text(encoding="utf-8") if original_exists else None
        settings_module = importlib.import_module(SETTINGS_MODULE_NAME)

        try:
            DOTENV_PATH.write_text("GATEWAY_API_KEY=dotenv-value\n", encoding="utf-8")

            with patch.dict(os.environ, {"GATEWAY_API_KEY": "process-value"}, clear=False):
                reloaded_module = importlib.reload(settings_module)
                self.assertEqual(reloaded_module.settings.gateway_api_key, "process-value")
        finally:
            if original_exists and original_content is not None:
                DOTENV_PATH.write_text(original_content, encoding="utf-8")
            elif DOTENV_PATH.exists():
                DOTENV_PATH.unlink()

            importlib.reload(settings_module)


if __name__ == "__main__":
    unittest.main()
