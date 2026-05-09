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
from llm_gateway_core.config.loader import ConfigError, ConfigLoader


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


if __name__ == "__main__":
    unittest.main()
