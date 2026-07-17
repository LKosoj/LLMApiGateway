"""Small regression gate for forbidden individual ``app.state`` fields."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


DENIED_STATE_FIELDS = frozenset({"active_requests_registry", "rejections_db"})
RUNTIME_DEPENDENCY_TARGETS = (
    Path("llm_gateway_core/services/active_requests.py"),
    Path("llm_gateway_core/db/rejections_db.py"),
)


@dataclass(frozen=True, order=True)
class BoundaryViolation:
    path: str
    line: int
    column: int
    field: str


def analyze_runtime_dependency_source(
    source: str,
    *,
    filename: str,
) -> tuple[BoundaryViolation, ...]:
    """Find direct reads, writes, or deletes of denied ``*.state`` fields."""
    tree = ast.parse(source, filename=filename)
    violations = {
        BoundaryViolation(
            path=filename,
            line=node.lineno,
            column=node.col_offset,
            field=node.attr,
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in DENIED_STATE_FIELDS
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "state"
    }
    return tuple(sorted(violations))


def find_runtime_dependency_boundary_violations(
    repo_root: Path,
) -> tuple[BoundaryViolation, ...]:
    """Analyze the two foundational modules guarded by this regression test."""
    violations: set[BoundaryViolation] = set()
    for relative_path in RUNTIME_DEPENDENCY_TARGETS:
        source = (repo_root / relative_path).read_text(encoding="utf-8")
        violations.update(analyze_runtime_dependency_source(source, filename=relative_path.as_posix()))
    return tuple(sorted(violations))
