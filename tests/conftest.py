import os
import tempfile
from pathlib import Path

import pytest

# Redirect the file handler to a writable temp directory when the production
# ``logs/gateway.log`` is owned by root (common in shared dev environments).
# configure_logging honors LLMGATEWAY_LOG_DIR when set.
_temp_log_dir = tempfile.mkdtemp(prefix="llmgateway_test_logs_")
os.environ.setdefault("LLMGATEWAY_LOG_DIR", _temp_log_dir)


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
