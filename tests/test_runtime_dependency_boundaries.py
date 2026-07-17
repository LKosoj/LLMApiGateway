from __future__ import annotations

import ast
import subprocess
import textwrap
from pathlib import Path

import pytest

import tests.runtime_boundary_subprocess as subprocess_support
import tests.runtime_dependency_boundary as boundary
from tests.runtime_boundary_subprocess import assert_fresh_import_order
from tests.runtime_dependency_boundary import (
    BoundaryViolation,
    analyze_runtime_dependency_source,
    find_runtime_dependency_boundary_violations,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _violations(source: str) -> tuple[BoundaryViolation, ...]:
    return analyze_runtime_dependency_source(
        textwrap.dedent(source).lstrip("\n"),
        filename="mutation.py",
    )


@pytest.mark.parametrize(
    "root, field",
    [
        pytest.param("app", "active_requests_registry", id="app-active-requests"),
        pytest.param("app", "rejections_db", id="app-rejections"),
        pytest.param(
            "request.app",
            "active_requests_registry",
            id="request-app-active-requests",
        ),
        pytest.param("request.app", "rejections_db", id="request-app-rejections"),
    ],
)
def test_direct_denied_state_reads_are_reported(root: str, field: str) -> None:
    assert _violations(f"value = {root}.state.{field}\n") == (BoundaryViolation("mutation.py", 1, 8, field),)


@pytest.mark.parametrize(
    "statement, expected_column",
    [
        pytest.param("app.state.rejections_db = value", 0, id="write"),
        pytest.param("del request.app.state.active_requests_registry", 4, id="delete"),
    ],
)
def test_denied_state_writes_and_deletes_are_reported(
    statement: str,
    expected_column: int,
) -> None:
    violation = _violations(statement + "\n")
    assert len(violation) == 1
    assert violation[0].column == expected_column
    assert violation[0].field in boundary.DENIED_STATE_FIELDS


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("services = app.state.services", id="app-services"),
        pytest.param("services = request.app.state.services", id="request-app-services"),
        pytest.param("request.state.request_id = 'request-1'", id="request-metadata-write"),
        pytest.param("value = request.state.route_name", id="request-metadata-read"),
    ],
)
def test_canonical_services_and_request_metadata_are_allowed(source: str) -> None:
    assert _violations(source + "\n") == ()


def test_results_are_stable_sorted_and_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "second = app.state.rejections_db\nfirst = app.state.active_requests_registry\n"
    original_walk = ast.walk

    def duplicate_walk(tree: ast.AST) -> list[ast.AST]:
        nodes = list(original_walk(tree))
        return nodes + nodes

    monkeypatch.setattr(boundary.ast, "walk", duplicate_walk)

    assert _violations(source) == (
        BoundaryViolation("mutation.py", 1, 9, "rejections_db"),
        BoundaryViolation("mutation.py", 2, 8, "active_requests_registry"),
    )


def test_current_foundational_targets_have_zero_violations() -> None:
    assert find_runtime_dependency_boundary_violations(REPO_ROOT) == ()


@pytest.mark.parametrize(
    "first, second",
    [
        (
            "llm_gateway_core.services.runtime_config",
            "llm_gateway_core.services.active_requests",
        ),
        (
            "llm_gateway_core.services.active_requests",
            "llm_gateway_core.services.runtime_config",
        ),
        (
            "llm_gateway_core.services.runtime_config",
            "llm_gateway_core.db.rejections_db",
        ),
        (
            "llm_gateway_core.db.rejections_db",
            "llm_gateway_core.services.runtime_config",
        ),
    ],
)
def test_foundational_import_orders_are_fresh_and_hermetic(
    first: str,
    second: str,
) -> None:
    assert_fresh_import_order(REPO_ROOT, first, second)


def test_import_probe_is_isolated_and_rejects_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = subprocess.CompletedProcess(
        args=["python"],
        returncode=0,
        stdout="",
        stderr="Exception ignored in: unraisable\n",
    )
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return completed

    monkeypatch.setattr(subprocess_support.subprocess, "run", fake_run)

    with pytest.raises(AssertionError, match="unraisable"):
        assert_fresh_import_order(REPO_ROOT, "first", "second", timeout=7)

    command = calls[0][0][0]
    assert isinstance(command, list)
    assert "-I" in command
    assert calls[0][1]["env"] == {}
    assert calls[0][1]["timeout"] == 7
