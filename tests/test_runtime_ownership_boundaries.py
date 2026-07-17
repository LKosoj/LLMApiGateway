from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = PROJECT_ROOT / "main.py"
LOADER_PATH = PROJECT_ROOT / "llm_gateway_core/config/loader.py"
CHAT_LOGGING_PATH = PROJECT_ROOT / "llm_gateway_core/middleware/chat_logging.py"
RUNTIME_CONFIG_PATH = PROJECT_ROOT / "llm_gateway_core/services/runtime_config.py"
RUNTIME_TEST_SUPPORT_PATH = PROJECT_ROOT / "tests/runtime_test_support.py"

LEGACY_STATE_ALIASES = frozenset(
    {
        "config_loader",
        "operation_rules",
        "tokens_usage_db",
        "fallback_events_db",
        "rejections_db",
        "write_batcher",
        "api_keys_db",
        "usd_budget_ledger",
        "active_requests_registry",
        "rate_limiter",
        "ip_block_guard",
        "upstream_routing_state",
        "model_rotation_db",
        "http_client",
        "upstream_subscription_quota_service",
        "proxy_http_clients",
        "operation_dispatcher",
        "fusion_service",
        "router_model_service",
        "provider_models_service",
        "cost_rate_registry",
        "openrouter_free_models_service",
        "fallback_model_eval_service",
    }
)
LEGACY_RELOAD_METHODS = frozenset(
    {
        "reload_fallback_rules",
        "reload_fusion_rules",
        "reload_router_rules",
        "reload_providers_config",
        "reload_operation_rules",
    }
)
LEGACY_CHAT_DEFINITIONS = frozenset(
    {
        "ChatLoggingState",
        "set_tokens_usage_db",
        "set_api_keys_db",
        "set_rate_limiter",
        "set_usd_budget_ledger",
        "_dependency_from_request",
        "_tokens_usage_db_from_request",
        "_api_keys_db_from_request",
        "_rate_limiter_from_request",
        "_usd_budget_ledger_from_request",
        "_require_tokens_usage_db",
    }
)
PRIVATE_MANAGER_METHODS = frozenset(
    {"_install_initial_for_testing", "_publish_for_testing"}
)
PUBLIC_RAW_MANAGER_METHODS = frozenset({"install_initial", "publish"})


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def _method_names(class_node: ast.ClassDef) -> set[str]:
    return {
        node.name
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_lifespan_publishes_only_the_typed_services_container() -> None:
    tree = _parse(MAIN_PATH)
    binding = _class(tree, "_TemporaryStateBinding")
    bind = next(
        node
        for node in binding.body
        if isinstance(node, ast.FunctionDef) and node.name == "bind"
    )
    assert [argument.arg for argument in bind.args.args] == ["self", "services"]

    bound_names = [
        call.args[0].value
        for call in ast.walk(bind)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "_set"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    ]
    assert bound_names == ["services"]

    lifespan = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    lifespan_literals = {
        node.value
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert lifespan_literals.isdisjoint(LEGACY_STATE_ALIASES)

    bind_calls = [
        call
        for call in ast.walk(lifespan)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "state_binding"
        and call.func.attr == "bind"
    ]
    assert len(bind_calls) == 1
    assert len(bind_calls[0].args) == 1
    assert isinstance(bind_calls[0].args[0], ast.Name)
    assert bind_calls[0].args[0].id == "services"
    assert bind_calls[0].keywords == []


def test_loader_and_chat_logging_have_no_legacy_mutation_bridges() -> None:
    loader = _class(_parse(LOADER_PATH), "ConfigLoader")
    assert _method_names(loader).isdisjoint(LEGACY_RELOAD_METHODS)

    chat_tree = _parse(CHAT_LOGGING_PATH)
    definitions = {
        node.name
        for node in chat_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert definitions.isdisjoint(LEGACY_CHAT_DEFINITIONS)
    top_level_assignments = {
        target.id
        for node in chat_tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Name)
    }
    assert "state" not in top_level_assignments


def _manager_receiver(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Attribute):
        return False
    receiver = ast.unparse(call.func.value)
    return "manager" in receiver or receiver == "super()"


def test_runtime_manager_raw_mutation_is_private_and_test_support_owned() -> None:
    runtime_tree = _parse(RUNTIME_CONFIG_PATH)
    manager = _class(runtime_tree, "RuntimeGenerationManager")
    methods = _method_names(manager)
    assert methods.isdisjoint(PUBLIC_RAW_MANAGER_METHODS)
    assert PRIVATE_MANAGER_METHODS <= methods

    private_calls: list[tuple[str, str]] = []
    public_manager_calls: list[tuple[str, int, str]] = []
    for path in PROJECT_ROOT.rglob("*.py"):
        if any(part in {".git", ".venv", ".cli-proxy"} for part in path.parts):
            continue
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr in PRIVATE_MANAGER_METHODS:
                private_calls.append((relative, node.func.attr))
            if (
                node.func.attr in PUBLIC_RAW_MANAGER_METHODS
                and _manager_receiver(node)
            ):
                public_manager_calls.append(
                    (relative, node.lineno, ast.unparse(node.func))
                )

    assert sorted(private_calls) == [
        ("tests/runtime_test_support.py", "_install_initial_for_testing"),
        ("tests/runtime_test_support.py", "_publish_for_testing"),
    ]
    assert public_manager_calls == []

    support_functions = {
        node.name
        for node in _parse(RUNTIME_TEST_SUPPORT_PATH).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {
        "install_test_runtime_snapshot",
        "publish_test_runtime_snapshot",
    } <= support_functions
