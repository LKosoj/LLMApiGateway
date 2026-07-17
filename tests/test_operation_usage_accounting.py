"""Regression guard for removal of the legacy operation accounting path."""

from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPERATION_PROXY = PROJECT_ROOT / "llm_gateway_core/api/v1/operation_proxy.py"


def test_operation_proxy_has_no_legacy_accounting_entrypoint() -> None:
    source = OPERATION_PROXY.read_text(encoding="utf-8")
    assert "def record_operation_usage(" not in source
    assert "usd_budget_reserved" not in source


def test_operation_proxy_does_not_mutate_accounting_stores_directly() -> None:
    tree = ast.parse(OPERATION_PROXY.read_text(encoding="utf-8"))
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint(
        {"insert_usage", "record_spent", "commit_reserved", "add_tokens"}
    )
