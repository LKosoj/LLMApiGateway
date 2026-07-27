"""Hermetic public-route inventory for transactional config updates."""

import ast
from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = PROJECT_ROOT / "llm_gateway_core/api"
MAIN_SOURCE = PROJECT_ROOT / "main.py"
V1_INIT_SOURCE = API_ROOT / "v1/__init__.py"
MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

EXPECTED_CONFIG_UPDATE_ROUTES = {
    (
        "POST",
        "/v1/config/fusion-rules/structured",
    ): ("llm_gateway_core/api/v1/rules_editor.py", "save_fusion_rules_structured"),
    (
        "POST",
        "/v1/config/model-operations/structured",
    ): ("llm_gateway_core/api/v1/rules_editor.py", "save_operation_rules_structured"),
    (
        "POST",
        "/v1/config/model-rules",
    ): ("llm_gateway_core/api/v1/rules_editor.py", "save_model_rules"),
    (
        "POST",
        "/v1/config/models-rules",
    ): ("llm_gateway_core/api/v1/rules_editor.py", "save_models_rules"),
    (
        "POST",
        "/v1/config/models-rules/structured",
    ): ("llm_gateway_core/api/v1/rules_editor.py", "save_models_rules_structured"),
    (
        "POST",
        "/v1/config/providers",
    ): ("llm_gateway_core/api/v1/rules_editor.py", "save_providers_config"),
    (
        "POST",
        "/v1/config/resync",
    ): ("llm_gateway_core/api/v1/rules_editor.py", "resync_config_from_disk"),
    (
        "POST",
        "/v1/config/providers/structured",
    ): ("llm_gateway_core/api/v1/rules_editor.py", "save_providers_structured"),
    (
        "POST",
        "/v1/config/router-rules/structured",
    ): ("llm_gateway_core/api/v1/rules_editor.py", "save_router_rules_structured"),
    (
        "POST",
        "/v1/ui/providers-config",
    ): ("llm_gateway_core/api/v1/rules_editor.py", "save_providers_config"),
    (
        "PUT",
        "/v1/admin/pricing",
    ): ("llm_gateway_core/api/v1/admin_pricing.py", "update_pricing"),
}
EXPECTED_PRICING_MUTATION_ROUTES = {
    (
        "POST",
        "/admin/pricing/calculate",
    ): ("llm_gateway_core/api/v1/admin_pricing.py", "calculate_cost"),
    (
        "PUT",
        "/admin/pricing",
    ): ("llm_gateway_core/api/v1/admin_pricing.py", "update_pricing"),
}
EXPECTED_MAIN_ROUTER_IMPORTS = (
    ("llm_gateway_core.api.v1", "router", "api_v1_router"),
    (
        "llm_gateway_core.api.v1.rules_editor",
        "editor_router",
        "api_v1_editor_router",
    ),
)
EXPECTED_V1_ROUTER_IMPORTS = (
    (
        "llm_gateway_core.api.v1.admin_pricing",
        "admin_pricing_router",
        "admin_pricing_router",
    ),
)


@dataclass(frozen=True, slots=True)
class RouteDefinition:
    method: str
    local_path: str
    source: str
    lineno: int
    function_name: str
    function_node: ast.FunctionDef | ast.AsyncFunctionDef = field(
        compare=False,
        repr=False,
    )

    @property
    def public_path(self) -> str:
        return f"/v1{self.local_path}"


@dataclass(frozen=True, slots=True)
class ImportBinding:
    module: str
    symbol: str
    local_name: str
    lineno: int


def _source_filename(source_path: Path) -> str:
    try:
        return source_path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return source_path.as_posix()


def _mutation_route_definitions_from_source(source_path: Path) -> tuple[RouteDefinition, ...]:
    definitions: list[RouteDefinition] = []
    source = _source_filename(source_path)
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if not isinstance(decorator.func, ast.Attribute):
                continue
            method = decorator.func.attr.upper()
            if method not in MUTATION_METHODS:
                continue
            if not decorator.args:
                raise AssertionError(f"mutation route without a positional path at {source}:{decorator.lineno}")
            path_node = decorator.args[0]
            if not isinstance(path_node, ast.Constant) or not isinstance(
                path_node.value,
                str,
            ):
                raise AssertionError(f"non-literal mutation path at {source}:{decorator.lineno}")
            definitions.append(
                RouteDefinition(
                    method=method,
                    local_path=path_node.value,
                    source=source,
                    lineno=decorator.lineno,
                    function_name=node.name,
                    function_node=node,
                )
            )
    return tuple(definitions)


def _mutation_route_definitions() -> tuple[RouteDefinition, ...]:
    definitions: list[RouteDefinition] = []
    for source_path in sorted(API_ROOT.rglob("*.py")):
        definitions.extend(_mutation_route_definitions_from_source(source_path))
    return tuple(definitions)


def _is_config_writer(definition: RouteDefinition) -> bool:
    if definition.local_path.startswith("/config/"):
        return True
    if definition.local_path == "/ui/providers-config":
        return True
    return (definition.method, definition.local_path) == ("PUT", "/admin/pricing")


def _config_writer_definitions() -> tuple[RouteDefinition, ...]:
    return tuple(definition for definition in _mutation_route_definitions() if _is_config_writer(definition))


def _import_from_bindings(
    source_path: Path,
    *,
    package: str | None = None,
) -> tuple[ImportBinding, ...]:
    tree = ast.parse(
        source_path.read_text(encoding="utf-8"),
        filename=_source_filename(source_path),
    )
    bindings: list[ImportBinding] = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            module = node.module or ""
        else:
            if package is None:
                raise AssertionError(f"relative import without package at {source_path.name}:{node.lineno}")
            package_parts = package.split(".")
            parent_hops = node.level - 1
            if parent_hops >= len(package_parts):
                raise AssertionError(f"relative import above package at {source_path.name}:{node.lineno}")
            module_parts = package_parts[: len(package_parts) - parent_hops]
            if node.module:
                module_parts.extend(node.module.split("."))
            module = ".".join(module_parts)
        bindings.extend(
            ImportBinding(
                module=module,
                symbol=alias.name,
                local_name=alias.asname or alias.name,
                lineno=node.lineno,
            )
            for alias in node.names
        )
    return tuple(bindings)


def _include_router_mounts(source_path: Path, owner: str) -> tuple[tuple[str, str], ...]:
    tree = ast.parse(
        source_path.read_text(encoding="utf-8"),
        filename=_source_filename(source_path),
    )
    mounts: list[tuple[str, str]] = []
    for statement in tree.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        node = statement.value
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "include_router" or not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id != owner or not node.args:
            continue
        router_node = node.args[0]
        if not isinstance(router_node, ast.Name):
            continue
        prefix = ""
        for keyword in node.keywords:
            if keyword.arg != "prefix":
                continue
            if not isinstance(keyword.value, ast.Constant) or not isinstance(
                keyword.value.value,
                str,
            ):
                raise AssertionError(f"non-literal router prefix at {source_path.name}:{node.lineno}")
            prefix = keyword.value.value
        mounts.append((router_node.id, prefix))
    return tuple(mounts)


def _mounts_for_router(
    mounts: tuple[tuple[str, str], ...],
    router_name: str,
) -> tuple[tuple[str, str], ...]:
    return tuple(mount for mount in mounts if mount[0] == router_name)


def _relevant_import_identities(
    bindings: tuple[ImportBinding, ...],
    expected: tuple[tuple[str, str, str], ...],
) -> tuple[tuple[str, str, str], ...]:
    expected_origins = {(module, symbol) for module, symbol, _ in expected}
    expected_names = {local_name for _, _, local_name in expected}
    return tuple(
        sorted(
            (binding.module, binding.symbol, binding.local_name)
            for binding in bindings
            if (binding.module, binding.symbol) in expected_origins or binding.local_name in expected_names
        )
    )


def test_config_update_route_inventory_is_stable() -> None:
    definitions = _config_writer_definitions()
    actual = sorted(
        (
            (definition.method, definition.public_path),
            (definition.source, definition.function_name),
        )
        for definition in definitions
    )

    assert len(definitions) == len(EXPECTED_CONFIG_UPDATE_ROUTES)
    assert actual == sorted(EXPECTED_CONFIG_UPDATE_ROUTES.items())
    assert {definition.function_name for definition in definitions} == {
        function_name for _, function_name in EXPECTED_CONFIG_UPDATE_ROUTES.values()
    }


def test_config_update_routes_have_no_duplicate_definitions() -> None:
    definitions_by_route: dict[tuple[str, str], list[RouteDefinition]] = {}
    for definition in _config_writer_definitions():
        definitions_by_route.setdefault(
            (definition.method, definition.public_path),
            [],
        ).append(definition)

    duplicates = {
        route: [f"{definition.source}:{definition.lineno}:{definition.function_name}" for definition in definitions]
        for route, definitions in definitions_by_route.items()
        if len(definitions) != 1
    }
    assert duplicates == {}


def test_pricing_mutation_route_allowlist_is_exact() -> None:
    actual = sorted(
        (
            (definition.method, definition.local_path),
            (definition.source, definition.function_name),
        )
        for definition in _mutation_route_definitions()
        if definition.local_path == "/admin/pricing" or definition.local_path.startswith("/admin/pricing/")
    )
    assert actual == sorted(EXPECTED_PRICING_MUTATION_ROUTES.items())


def test_legacy_provider_aliases_share_one_ast_function_node() -> None:
    definitions = _config_writer_definitions()

    provider_definition = next(
        definition
        for definition in definitions
        if (definition.method, definition.local_path) == ("POST", "/config/providers")
    )
    legacy_definition = next(
        definition
        for definition in definitions
        if (definition.method, definition.local_path) == ("POST", "/ui/providers-config")
    )

    assert provider_definition.function_node is legacy_definition.function_node


def test_public_v1_import_and_mount_chain_is_stable() -> None:
    main_imports = _import_from_bindings(MAIN_SOURCE)
    v1_imports = _import_from_bindings(
        V1_INIT_SOURCE,
        package="llm_gateway_core.api.v1",
    )
    main_mounts = _include_router_mounts(MAIN_SOURCE, "app")
    v1_mounts = _include_router_mounts(V1_INIT_SOURCE, "router")

    assert _relevant_import_identities(main_imports, EXPECTED_MAIN_ROUTER_IMPORTS) == tuple(
        sorted(EXPECTED_MAIN_ROUTER_IMPORTS)
    )
    assert _relevant_import_identities(v1_imports, EXPECTED_V1_ROUTER_IMPORTS) == tuple(
        sorted(EXPECTED_V1_ROUTER_IMPORTS)
    )
    assert _mounts_for_router(main_mounts, "api_v1_editor_router") == (("api_v1_editor_router", "/v1"),)
    assert _mounts_for_router(main_mounts, "api_v1_router") == (("api_v1_router", "/v1"),)
    assert _mounts_for_router(v1_mounts, "admin_pricing_router") == (("admin_pricing_router", ""),)


def test_router_ast_helpers_ignore_nested_statements_and_expose_extra_mount(tmp_path: Path) -> None:
    source_path = tmp_path / "synthetic_routes.py"
    source_path.write_text(
        """
from package.top import router as watched_router
app.include_router(watched_router, prefix="/v1")
app.include_router(watched_router, prefix="/extra")

if enabled:
    from package.conditional import router as watched_router
    app.include_router(watched_router, prefix="/conditional")

def register():
    from package.nested import router as watched_router
    app.include_router(watched_router, prefix="/nested")

try:
    from package.guarded import router as watched_router
    app.include_router(watched_router, prefix="/guarded")
except ImportError:
    pass

@router.post("/config/direct")
async def direct_writer():
    pass

if enabled:
    @router.post("/config/conditional")
    async def conditional_writer():
        pass

def register_writer():
    @router.post("/config/nested")
    async def nested_writer():
        pass

class WriterContainer:
    @router.post("/config/class")
    async def class_writer(self):
        pass

try:
    @router.post("/config/guarded")
    async def guarded_writer():
        pass
except RuntimeError:
    pass
""".lstrip(),
        encoding="utf-8",
    )

    imports = _import_from_bindings(source_path)
    mounts = _include_router_mounts(source_path, "app")
    route_definitions = _mutation_route_definitions_from_source(source_path)

    assert [
        (binding.module, binding.symbol, binding.local_name)
        for binding in imports
        if binding.local_name == "watched_router"
    ] == [("package.top", "router", "watched_router")]
    assert _mounts_for_router(mounts, "watched_router") == (
        ("watched_router", "/v1"),
        ("watched_router", "/extra"),
    )
    assert [
        (definition.method, definition.local_path, definition.function_name) for definition in route_definitions
    ] == [("POST", "/config/direct", "direct_writer")]
