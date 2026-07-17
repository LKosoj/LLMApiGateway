import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent
project_root_str = str(PROJECT_ROOT)

if project_root_str not in sys.path:
    sys.path.insert(0, project_root_str)

from tests.main_lifespan_import import (  # noqa: E402
    install_main_import_isolation,
)


def enforce_main_lifespan_boundary(repo_root: Path = PROJECT_ROOT) -> None:
    from tests.main_lifespan_boundary import find_main_lifespan_boundary_violations

    violations = find_main_lifespan_boundary_violations(repo_root)
    if violations:
        formatted = "\n".join(violations)
        raise pytest.UsageError(
            f"Production lifespan test isolation boundary was bypassed:\n{formatted}"
        )


def _bootstrap_main_import_isolation():
    controller, cleanup = install_main_import_isolation(PROJECT_ROOT)
    configured_with = None
    try:
        enforce_main_lifespan_boundary()
    except BaseException:
        cleanup()
        raise

    @pytest.hookimpl(tryfirst=True)
    def configure(config: pytest.Config) -> None:
        """Register cleanup after the early lazy-import hook was installed."""
        nonlocal configured_with

        if configured_with is not None:
            raise pytest.UsageError(
                "Main import isolation pytest hook was already configured."
            )
        configured_with = config
        try:
            config.add_cleanup(cleanup)
            controller.assert_integrity()
            enforce_main_lifespan_boundary()
        except BaseException:
            cleanup()
            raise

    return configure


pytest_configure = _bootstrap_main_import_isolation()


@pytest.fixture(autouse=True, scope="session")
def keep_tests_off_the_real_session_secret(tmp_path_factory: pytest.TempPathFactory):
    """Stop the suite from reissuing the cookie-signing secret of a real gateway.

    Tests sign in with throwaway ``GATEWAY_API_KEY`` values, and the secret is
    reissued whenever the configured key stops matching the stored fingerprint.
    Left alone, a test run would therefore rewrite ``db/session_secret.json`` of
    a gateway deployed from this checkout and log every one of its users out at
    the next restart, without anyone having rotated the key.
    """
    from unittest.mock import patch

    from llm_gateway_core.services import session_secret

    state_dir = tmp_path_factory.mktemp("session-secret-state")
    with patch.object(session_secret, "resolve_db_dir", lambda: state_dir):
        yield
