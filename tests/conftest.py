import os
import tempfile

# Redirect the file handler to a writable temp directory when the production
# ``logs/gateway.log`` is owned by root (common in shared dev environments).
# configure_logging honors LLMGATEWAY_LOG_DIR when set.
_temp_log_dir = tempfile.mkdtemp(prefix="llmgateway_test_logs_")
os.environ.setdefault("LLMGATEWAY_LOG_DIR", _temp_log_dir)
