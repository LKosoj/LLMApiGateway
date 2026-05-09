import importlib
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dotenv
import llm_gateway_core.config.settings as settings_module
from llm_gateway_core.middleware import chat_logging
from llm_gateway_core.utils import logging_setup


class LoggingSetupTests(unittest.TestCase):
    def tearDown(self):
        logging.shutdown()
        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)

    def test_configure_logging_applies_debug_level(self):
        original_cwd = os.getcwd()
        # Isolate from the conftest-wide LLMGATEWAY_LOG_DIR override so this test
        # exercises the default cwd-relative "logs" directory branch.
        env_without_override = {k: v for k, v in os.environ.items() if k != "LLMGATEWAY_LOG_DIR"}
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                with patch.dict(os.environ, env_without_override, clear=True):
                    with patch.object(logging_setup.settings, "log_level", "DEBUG"):
                        logging_setup.configure_logging()
                        root_logger = logging.getLogger()
                        root_logger.debug("debug-visible")
                        for handler in root_logger.handlers:
                            handler.flush()

                    log_content = Path("logs/gateway.log").read_text(encoding="utf-8")
            finally:
                os.chdir(original_cwd)

        self.assertTrue(logging.getLogger().isEnabledFor(logging.DEBUG))
        self.assertIn("debug-visible", log_content)

    def test_chat_log_filename_contains_uuid(self):
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                with patch.object(chat_logging.settings, "log_file_limit", 5):
                    chat_logging.write_log(
                        {"Authorization": "Bearer secret"},
                        '{"messages":[{"role":"user","content":"hello"}]}',
                        "response",
                        {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                            "reasoning_tokens": 0,
                            "cached_tokens": 0,
                            "cost": 0.0,
                        },
                    )

                log_files = list(Path("logs").glob("*.txt"))
            finally:
                os.chdir(original_cwd)

        self.assertEqual(len(log_files), 1)
        file_name = log_files[0].name
        uuid_part = file_name.rsplit("_", 1)[-1].removesuffix(".txt")
        self.assertRegex(
            file_name,
            r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.\d{3}_[0-9a-f-]{36}\.txt$",
        )
        self.assertEqual(len(uuid_part), 36)

    def test_settings_rejects_non_positive_log_file_limit(self):
        with patch.object(dotenv, "load_dotenv", return_value=True):
            with patch.dict(os.environ, {"LOG_FILE_LIMIT": "0"}, clear=False):
                with self.assertRaises(ValueError):
                    importlib.reload(settings_module)

        importlib.reload(settings_module)


if __name__ == "__main__":
    unittest.main()
