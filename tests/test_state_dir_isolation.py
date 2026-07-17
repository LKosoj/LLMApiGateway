"""Guards that keep the suite out of the deployment's state directory.

This checkout doubles as a live deployment: a gateway runs from it and serves
from its ``db/``. Both ways a test can reach that directory are covered here —
letting the production resolver fall through to it, and hardcoding a relative
path that resolves against the working directory.
"""

from __future__ import annotations

import ast
from pathlib import Path

from llm_gateway_core.config.paths import PROJECT_ROOT, resolve_db_dir


def test_the_state_dir_a_test_resolves_is_never_the_deployment_one():
    """The autouse fixture must outrank every resolution path, not just some.

    ``resolve_db_dir`` answers differently depending on whether a caller passes
    its ``__file__``, and production callers do pass it. Checking only the
    argument-less form would leave the form the DB classes actually use unproven.
    """
    deployment_db_dir = PROJECT_ROOT / "db"

    for module_file in (None, str(PROJECT_ROOT / "llm_gateway_core" / "db" / "x.py")):
        resolved = resolve_db_dir(module_file)
        assert resolved != deployment_db_dir
        assert deployment_db_dir not in resolved.parents


def test_no_test_hardcodes_a_relative_path_into_the_state_dir():
    """A relative ``db/...`` literal resolves against the working directory.

    Tests run from the repository root, so such a literal names the deployment's
    own file no matter what the fixture redirects — the test writes to one place
    while the code it exercises reads another. Ask ``resolve_db_dir()`` instead.
    """
    # Built by concatenation on purpose: spelled out, the prefix would be a
    # literal in this file and this guard would report itself.
    relative_prefix = "db" + "/"
    tests_dir = Path(__file__).resolve().parent

    offenders = []
    for source_file in sorted(tests_dir.rglob("*.py")):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith(relative_prefix)
            ):
                location = f"{source_file.relative_to(PROJECT_ROOT)}:{node.lineno}"
                offenders.append(f"{location}: {node.value!r}")

    assert offenders == []
