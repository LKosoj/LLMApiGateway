import importlib
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dotenv
import llm_gateway_core.config.settings as settings_module
from llm_gateway_core.config import paths as project_paths
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
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured_logs = root / "configured logs"
            read_only_cwd = root / "read-only cwd"
            fake_checkout = root / "fake checkout"
            read_only_cwd.mkdir()
            fake_checkout.mkdir()
            read_only_cwd.chmod(0o555)
            marker = f"debug-visible-{root.name}"
            os.chdir(read_only_cwd)
            try:
                with (
                    patch.dict(
                        os.environ,
                        {"LLMGATEWAY_LOG_DIR": os.fspath(configured_logs)},
                        clear=False,
                    ),
                    patch.object(project_paths, "PROJECT_ROOT", fake_checkout),
                ):
                    with patch.object(logging_setup.settings, "log_level", "DEBUG"):
                        logging_setup.configure_logging()
                        root_logger = logging.getLogger()
                        root_logger.debug(marker)
                        for handler in root_logger.handlers:
                            handler.flush()

                    log_content = (configured_logs / "gateway.log").read_text(
                        encoding="utf-8"
                    )
            finally:
                os.chdir(original_cwd)
                read_only_cwd.chmod(0o755)

            self.assertFalse((read_only_cwd / "logs").exists())
            self.assertFalse((fake_checkout / "logs").exists())

        self.assertTrue(logging.getLogger().isEnabledFor(logging.DEBUG))
        self.assertIn(marker, log_content)

    def test_chat_log_filename_contains_uuid(self):
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured_logs = root / "configured logs"
            unrelated_cwd = root / "unrelated cwd"
            unrelated_cwd.mkdir()
            os.chdir(unrelated_cwd)
            try:
                with patch.dict(
                    os.environ,
                    {"LLMGATEWAY_LOG_DIR": os.fspath(configured_logs)},
                    clear=False,
                ):
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

                log_files = list(configured_logs.glob("*.txt"))
            finally:
                os.chdir(original_cwd)

            self.assertFalse((unrelated_cwd / "logs").exists())

        self.assertEqual(len(log_files), 1)
        file_name = log_files[0].name
        uuid_part = file_name.rsplit("_", 1)[-1].removesuffix(".txt")
        self.assertRegex(
            file_name,
            r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.\d{3}_[0-9a-f-]{36}\.txt$",
        )
        self.assertEqual(len(uuid_part), 36)

    def test_chat_log_cleanup_uses_only_configured_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured_logs = root / "configured logs"
            unrelated_cwd = root / "unrelated cwd"
            unrelated_cwd.mkdir()
            original_cwd = os.getcwd()
            os.chdir(unrelated_cwd)
            try:
                with (
                    patch.dict(
                        os.environ,
                        {"LLMGATEWAY_LOG_DIR": os.fspath(configured_logs)},
                        clear=False,
                    ),
                    patch.object(chat_logging.settings, "log_file_limit", 1),
                    patch.object(chat_logging, "_LOG_CLEANUP_INTERVAL", 1),
                    patch.object(chat_logging, "_log_write_counter", 0),
                ):
                    for response in ("first", "second"):
                        chat_logging.write_log({}, "{}", response, {})
            finally:
                os.chdir(original_cwd)

            self.assertEqual(len(list(configured_logs.glob("*.txt"))), 1)
            self.assertFalse((unrelated_cwd / "logs").exists())

    def test_settings_rejects_non_positive_log_file_limit(self):
        with patch.object(dotenv, "load_dotenv", return_value=True):
            with patch.dict(os.environ, {"LOG_FILE_LIMIT": "0"}, clear=False):
                with self.assertRaises(ValueError):
                    importlib.reload(settings_module)

        importlib.reload(settings_module)


if __name__ == "__main__":
    unittest.main()
