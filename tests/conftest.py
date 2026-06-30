import os
import tempfile
from pathlib import Path

import pytest

# Redirect the file handler to a writable temp directory when the production
# ``logs/gateway.log`` is owned by root (common in shared dev environments).
# configure_logging honors LLMGATEWAY_LOG_DIR when set.
_temp_log_dir = tempfile.mkdtemp(prefix="llmgateway_test_logs_")
os.environ.setdefault("LLMGATEWAY_LOG_DIR", _temp_log_dir)

# Keep tests independent from optional config files that may exist in the
# checkout and reference providers absent from a test's temp providers.json.
_temp_config_dir = Path(tempfile.mkdtemp(prefix="llmgateway_test_config_"))
_empty_fusion_rules = _temp_config_dir / "models_fusion_rules.json"
_empty_model_rules = _temp_config_dir / "models_model_rules.json"
_empty_router_rules = _temp_config_dir / "models_router_rules.json"
_empty_fusion_rules.write_text("[]\n", encoding="utf-8")
_empty_model_rules.write_text("{}\n", encoding="utf-8")
_empty_router_rules.write_text("[]\n", encoding="utf-8")
os.environ["FUSION_RULES_FILENAME"] = str(_empty_fusion_rules)
os.environ["MODEL_RULES_FILENAME"] = str(_empty_model_rules)
os.environ["ROUTER_RULES_FILENAME"] = str(_empty_router_rules)


@pytest.fixture(autouse=True)
def _cleanup_orphan_sqlite_journals():
    """Remove orphan SQLite WAL/SHM journals left in ``db/`` by test DBs.

    Some tests delete only the main ``.db``/``.sqlite`` file in their teardown
    but forget the ``-wal``/``-shm`` siblings. Running on a session-wide
    autouse hook keeps the project-root ``db/`` directory clean even when an
    individual test omits the cleanup.
    """
    yield
    db_dir = Path(__file__).resolve().parent.parent / "db"
    if not db_dir.is_dir():
        return
    for entry in db_dir.iterdir():
        name = entry.name
        if not name.startswith("test_"):
            continue
        if name.endswith("-wal") or name.endswith("-shm"):
            base = name[: -len("-wal")] if name.endswith("-wal") else name[: -len("-shm")]
            if not (db_dir / base).exists():
                entry.unlink(missing_ok=True)
