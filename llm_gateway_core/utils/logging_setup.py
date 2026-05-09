import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pythonjsonlogger.jsonlogger import JsonFormatter

from ..config.settings import settings

# Env override used by the test suite to redirect file logs when the real
# ``logs/gateway.log`` is not writable (e.g. owned by root in shared dev envs).
_LOG_DIR_ENV = "LLMGATEWAY_LOG_DIR"

class CustomJsonFormatter(JsonFormatter):
    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        if 'taskName' in log_record:
            del log_record['taskName']

def configure_logging():
    override = os.environ.get(_LOG_DIR_ENV)
    log_dir = Path(override) if override else Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file_path = log_dir.joinpath("gateway.log").resolve()
    log_level = settings.log_level

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()

    formatter = CustomJsonFormatter("%(asctime)s %(levelname)s %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)

    file_handler = RotatingFileHandler(
        filename=str(log_file_path),
        maxBytes=256000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(log_level)

    root_logger.setLevel(log_level)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    httpcore_logger = logging.getLogger("httpcore")
    httpcore_logger.setLevel("WARNING")
    httpcore_logger.handlers.clear()
    httpcore_logger.addHandler(console_handler)
    httpcore_logger.addHandler(file_handler)
    httpcore_logger.propagate = False

    log_file_path.touch(exist_ok=True)
