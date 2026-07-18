import os
import tempfile
from pathlib import Path

import pytest

from tests.main_lifespan_boundary import find_main_lifespan_boundary_violations
from tests.main_lifespan_import import get_main_import_isolation

# Redirect the file handler to a writable temp directory when the production
# ``logs/gateway.log`` is owned by root (common in shared dev environments).
# configure_logging honors LLMGATEWAY_LOG_DIR when set.
_temp_log_dir = tempfile.mkdtemp(prefix="llmgateway_test_logs_")
os.environ.setdefault("LLMGATEWAY_LOG_DIR", _temp_log_dir)

# Tests that boot the production lifespan in-process start the real supervised
# tasks; keep the free LLM catalog task from fetching https://api.freellmapi.co
# during tests. Tests of the fetch path patch settings.free_llm_catalog_enabled
# or call refresh_once() directly.
os.environ.setdefault("FREE_LLM_CATALOG_ENABLED", "false")


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
def _keep_tests_out_of_the_deployment_state_dir(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
):
    """Give every test its own state directory instead of the deployment's.

    This checkout is also a live deployment: a gateway runs from it and serves
    from its ``db/``. A test that builds a DB without redirecting it resolves to
    that same directory, so a plain ``pytest`` run wrote into the live
    ``tokens_usage.db`` and left orphan test DBs behind. Worse, the directory
    then has to stay group-writable for the suite to pass, and write access to
    it is enough to replace ``session_secret.json`` and forge a master cookie —
    the mode alone cannot protect a file whose directory the suite can write.

    Per test, not per session: ``GATEWAY_DB_DIR`` outranks the
    ``patch.object(module, "__file__", ...)`` redirect that many tests use, so
    one shared directory would defeat their isolation and let their DBs bleed
    into each other. A fresh directory each time preserves it instead.

    A test that sets ``GATEWAY_DB_DIR`` itself still wins: this runs before the
    test body, and ``monkeypatch`` restores the value afterwards either way.
    """
    monkeypatch.setenv(
        "GATEWAY_DB_DIR",
        str(tmp_path_factory.mktemp("gateway-state") / "db"),
    )


@pytest.fixture(autouse=True)
def _main_lifespan_storage_isolation():
    """Verify the session owner without importing ``main`` eagerly."""
    controller = get_main_import_isolation()
    owner = controller.assert_integrity()
    try:
        yield owner
    finally:
        controller.assert_integrity()


def pytest_collection_modifyitems() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    violations = find_main_lifespan_boundary_violations(repo_root)
    if violations:
        formatted = "\n".join(violations)
        raise pytest.UsageError(
            f"Production lifespan test isolation boundary was bypassed:\n{formatted}"
        )
