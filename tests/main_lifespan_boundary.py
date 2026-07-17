from __future__ import annotations

import ast
from pathlib import Path


CanonicalPath = tuple[str, ...]
AliasMap = dict[str, set[CanonicalPath]]

_FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_COMPREHENSION_SCOPES = (
    ast.DictComp,
    ast.GeneratorExp,
    ast.ListComp,
    ast.SetComp,
)
_LEXICAL_SCOPES = (*_FUNCTION_SCOPES, ast.ClassDef, *_COMPREHENSION_SCOPES)

_SENSITIVE_REBINDS = {
    ("main", "lifespan"),
    ("main", "app"),
    ("main", "app", "router"),
    ("main", "app", "router", "lifespan_context"),
}
_SENSITIVE_MAPPINGS = {
    ("main", "__dict__"),
    ("main", "app", "__dict__"),
    ("main", "app", "router", "__dict__"),
}
_SENSITIVE_ATTRIBUTE_OWNERS = {
    path[:-1] for path in _SENSITIVE_REBINDS
}
_PYTEST_MONKEYPATCH_METHODS = {
    "delattr",
    "delitem",
    "setattr",
    "setitem",
}
_BUILTIN_ALIASES = {
    "__import__": {("builtins", "__import__")},
    "delattr": {("builtins", "delattr")},
    "getattr": {("builtins", "getattr")},
    "setattr": {("builtins", "setattr")},
    "vars": {("builtins", "vars")},
}
_ALIASABLE_PATHS = {
    ("main",),
    *_SENSITIVE_REBINDS,
    *_SENSITIVE_MAPPINGS,
    ("sys", "modules"),
    ("importlib", "import_module"),
    ("importlib", "reload"),
    ("unittest", "mock", "patch"),
    ("unittest", "mock", "patch", "dict"),
    ("unittest", "mock", "patch", "multiple"),
    ("unittest", "mock", "patch", "object"),
    ("builtins", "__import__"),
    ("builtins", "delattr"),
    ("builtins", "getattr"),
    ("builtins", "setattr"),
    ("builtins", "vars"),
    ("dict", "__delitem__"),
    ("dict", "__ior__"),
    ("dict", "__setitem__"),
    ("dict", "clear"),
    ("dict", "pop"),
    ("dict", "popitem"),
    ("dict", "setdefault"),
    ("dict", "update"),
    ("object", "__delattr__"),
    ("object", "__setattr__"),
    ("operator",),
    ("operator", "delitem"),
    ("operator", "ior"),
    ("operator", "setitem"),
    ("pytest", "monkeypatch", "delattr"),
    ("pytest", "monkeypatch", "delitem"),
    ("pytest", "monkeypatch", "setattr"),
    ("pytest", "monkeypatch", "setitem"),
    ("runpy",),
    ("runpy", "run_module"),
    ("runpy", "run_path"),
    ("tests", "main_lifespan_import", "_active_controller"),
    ("tests", "main_lifespan_import", "_active_controller", "_close"),
    ("tests", "main_lifespan_import", "MainImportIsolationController"),
    ("tests", "main_lifespan_import", "MainImportIsolationController", "_close"),
    ("tests", "main_lifespan_import", "_close_controller_if_active"),
    ("tests", "main_lifespan_import", "_close_main_import_isolation"),
    ("tests", "main_lifespan_import", "get_main_import_isolation"),
    ("unittest",),
    ("unittest", "mock"),
}
_MAPPING_OPERATIONS = {
    "__delitem__",
    "__ior__",
    "__setitem__",
    "clear",
    "delitem",
    "ior",
    "pop",
    "popitem",
    "setdefault",
    "setitem",
    "update",
}


def _python_test_paths(repo_root: Path) -> tuple[Path, ...]:
    tests_dir = repo_root / "tests"
    paths = set(tests_dir.rglob("*.py"))
    for path in (repo_root / "conftest.py", tests_dir / "conftest.py"):
        if path.is_file():
            paths.add(path)
    return tuple(sorted(paths))


def _assignment_targets(node: ast.AST) -> tuple[ast.AST, ...]:
    if isinstance(node, ast.Assign):
        return tuple(node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        return (node.target,)
    if isinstance(node, ast.Delete):
        return tuple(node.targets)
    return ()


def _assignment_value(node: ast.AST) -> ast.AST | None:
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        return node.value
    return None


def _name_targets(node: ast.AST) -> tuple[str, ...]:
    return tuple(
        child.id
        for target in _assignment_targets(node)
        for child in ast.walk(target)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
    )


def _stored_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
    }


def _pattern_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name:
            names.add(child.name)
        elif isinstance(child, ast.MatchMapping) and child.rest:
            names.add(child.rest)
    return names


def _constant_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _canonical_paths(node: ast.AST, aliases: AliasMap) -> set[CanonicalPath]:
    if isinstance(node, ast.Name):
        return set(aliases.get(node.id, {(node.id,)}))
    if isinstance(node, ast.Attribute):
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == "monkeypatch"
            and node.attr in _PYTEST_MONKEYPATCH_METHODS
        ):
            return {("pytest", "monkeypatch", node.attr)}
        return {(*prefix, node.attr) for prefix in _canonical_paths(node.value, aliases)}
    if isinstance(node, ast.Subscript):
        prefixes = _canonical_paths(node.value, aliases)
        key = _constant_string(node.slice)
        paths: set[CanonicalPath] = set()
        for prefix in prefixes:
            if prefix == ("sys", "modules") and key == "main":
                paths.add(("main",))
            elif prefix[-1:] == ("__dict__",) and key:
                paths.add((*prefix[:-1], key))
        return paths
    if not isinstance(node, ast.Call):
        return set()

    function_paths = _canonical_paths(node.func, aliases)
    if (
        "tests",
        "main_lifespan_import",
        "get_main_import_isolation",
    ) in function_paths:
        return {("tests", "main_lifespan_import", "_active_controller")}
    if node.args and _constant_string(node.args[0]) == "main" and function_paths & {
        ("builtins", "__import__"),
        ("importlib", "import_module"),
    }:
        return {("main",)}
    if node.args and any(path == ("sys", "modules", "get") for path in function_paths):
        if _constant_string(node.args[0]) == "main":
            return {("main",)}
    if len(node.args) >= 2 and function_paths & {
        ("builtins", "getattr"),
        ("object", "__getattribute__"),
    }:
        attribute = _constant_string(node.args[1])
        if attribute:
            return {
                (*target, attribute)
                for target in _canonical_paths(node.args[0], aliases)
            }
    if node.args and ("builtins", "vars") in function_paths:
        return {
            (*target, "__dict__")
            for target in _canonical_paths(node.args[0], aliases)
        }
    if node.args and any(path[-2:] == ("__dict__", "get") for path in function_paths):
        key = _constant_string(node.args[0])
        if key:
            return {
                (*path[:-2], key)
                for path in function_paths
                if path[-2:] == ("__dict__", "get")
            }
    return set()


def _add_alias(aliases: AliasMap, name: str, path: CanonicalPath) -> bool:
    values = aliases.setdefault(name, set())
    if path in values:
        return False
    values.add(path)
    return True


def _is_aliasable_path(path: CanonicalPath) -> bool:
    if path in _ALIASABLE_PATHS:
        return True
    if (
        path[:-1] in {*_SENSITIVE_MAPPINGS, ("sys", "modules")}
        and path[-1] in _MAPPING_OPERATIONS
    ):
        return True
    return False


def _execution_scope(
    node: ast.AST,
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
) -> ast.AST:
    current = node
    while current in parents:
        parent = parents[current]
        if isinstance(parent, _FUNCTION_SCOPES):
            body = parent.body if not isinstance(parent, ast.Lambda) else parent.body
            if isinstance(parent, ast.Lambda) or current in body:
                return parent
            current = parent
            continue
        if isinstance(parent, _COMPREHENSION_SCOPES):
            return parent
        if isinstance(parent, ast.ClassDef):
            if current in parent.body:
                return parent
            current = parent
            continue
        current = parent
    return tree


def _scope_chain(
    scope: ast.AST,
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
) -> tuple[ast.AST, ...]:
    scopes: list[ast.AST] = []
    current: ast.AST | None = scope
    inside_function = False
    while current is not None and current is not tree:
        if isinstance(current, (*_FUNCTION_SCOPES, *_COMPREHENSION_SCOPES)):
            scopes.append(current)
            inside_function = True
        elif isinstance(current, ast.ClassDef) and not inside_function:
            scopes.append(current)
        current = parents.get(current)
    scopes.append(tree)
    return tuple(scopes)


def _local_shadow_path(scope: ast.AST, name: str) -> CanonicalPath:
    return (
        "<local-shadow>",
        type(scope).__name__,
        str(getattr(scope, "lineno", 0)),
        str(getattr(scope, "col_offset", 0)),
        name,
    )


def _argument_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> set[str]:
    arguments = function.args
    names = {
        *(argument.arg for argument in arguments.posonlyargs),
        *(argument.arg for argument in arguments.args),
        *(argument.arg for argument in arguments.kwonlyargs),
    }
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return names


def _import_bindings(node: ast.Import | ast.ImportFrom) -> tuple[tuple[str, CanonicalPath], ...]:
    bindings: list[tuple[str, CanonicalPath]] = []
    if isinstance(node, ast.Import):
        for imported in node.names:
            if imported.name in {"importlib", "main", "operator", "runpy", "sys"}:
                bindings.append(
                    (
                        imported.asname or imported.name,
                        (imported.name,),
                    )
                )
            elif imported.name == "tests.main_lifespan_import":
                bindings.append(
                    (
                        imported.asname or "tests",
                        (
                            ("tests", "main_lifespan_import")
                            if imported.asname
                            else ("tests",)
                        ),
                    )
                )
            elif imported.name == "unittest.mock":
                bindings.append(
                    (
                        imported.asname or "unittest",
                        (
                            ("unittest", "mock")
                            if imported.asname
                            else ("unittest",)
                        ),
                    )
                )
        return tuple(bindings)

    for imported in node.names:
        name = imported.asname or imported.name
        if node.module == "main":
            bindings.append((name, ("main", imported.name)))
        elif node.module == "importlib" and imported.name in {
            "import_module",
            "reload",
        }:
            bindings.append((name, ("importlib", imported.name)))
        elif node.module == "sys" and imported.name == "modules":
            bindings.append((name, ("sys", "modules")))
        elif node.module == "builtins" and imported.name in _BUILTIN_ALIASES:
            bindings.append((name, ("builtins", imported.name)))
        elif node.module == "operator" and imported.name in {
            "delitem",
            "ior",
            "setitem",
        }:
            bindings.append((name, ("operator", imported.name)))
        elif node.module == "unittest" and imported.name == "mock":
            bindings.append((name, ("unittest", "mock")))
        elif node.module == "runpy" and imported.name in {
            "run_module",
            "run_path",
        }:
            bindings.append((name, ("runpy", imported.name)))
        elif node.module == "tests" and imported.name == "main_lifespan_import":
            bindings.append(
                (name, ("tests", "main_lifespan_import"))
            )
        elif node.module == "tests.main_lifespan_import" and imported.name in {
            "_ACTIVE_CONTROLLER",
            "_close_controller_if_active",
            "_close_main_import_isolation",
            "MainImportIsolationController",
            "get_main_import_isolation",
        }:
            canonical_name = (
                "_active_controller"
                if imported.name == "_ACTIVE_CONTROLLER"
                else imported.name
            )
            bindings.append(
                (name, ("tests", "main_lifespan_import", canonical_name))
            )
        elif node.module == "unittest.mock" and imported.name == "patch":
            bindings.append((name, ("unittest", "mock", "patch")))
    return tuple(bindings)


def _scoped_aliases(
    tree: ast.Module,
) -> tuple[
    dict[ast.AST, ast.AST],
    dict[ast.AST, AliasMap],
    dict[ast.AST, AliasMap],
]:
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    scopes = {
        tree,
        *(
            node
            for node in ast.walk(tree)
            if isinstance(node, _LEXICAL_SCOPES)
        ),
    }
    declarations = {scope: set() for scope in scopes}
    aliases_by_scope: dict[ast.AST, AliasMap] = {scope: {} for scope in scopes}
    assignment_records: list[tuple[ast.AST, ast.AST]] = []

    for node in ast.walk(tree):
        if isinstance(node, _FUNCTION_SCOPES):
            declarations[node].update(_argument_names(node))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                declarations[_execution_scope(node, tree, parents)].add(node.name)
        elif isinstance(node, ast.ClassDef):
            declarations[_execution_scope(node, tree, parents)].add(node.name)
        elif isinstance(node, _COMPREHENSION_SCOPES):
            for generator in node.generators:
                declarations[node].update(_stored_names(generator.target))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            declarations[_execution_scope(node, tree, parents)].update(
                _stored_names(node.target)
            )
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            scope = _execution_scope(node, tree, parents)
            for item in node.items:
                if item.optional_vars is not None:
                    declarations[scope].update(_stored_names(item.optional_vars))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            declarations[_execution_scope(node, tree, parents)].add(node.name)
        elif isinstance(node, ast.Match):
            scope = _execution_scope(node, tree, parents)
            for case in node.cases:
                declarations[scope].update(_pattern_names(case.pattern))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            scope = _execution_scope(node, tree, parents)
            for name, path in _import_bindings(node):
                declarations[scope].add(name)
                if not isinstance(scope, ast.ClassDef):
                    _add_alias(aliases_by_scope[scope], name, path)
        elif isinstance(
            node,
            (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr),
        ):
            scope = _execution_scope(node, tree, parents)
            declarations[scope].update(_name_targets(node))
            value = _assignment_value(node)
            if value is not None and not isinstance(scope, ast.ClassDef):
                assignment_records.append((scope, node))

    def aliases_for_chain(chain: tuple[ast.AST, ...]) -> AliasMap:
        aliases: AliasMap = {
            name: set(paths) for name, paths in _BUILTIN_ALIASES.items()
        }
        for current in reversed(chain):
            for name in declarations[current]:
                values = aliases_by_scope[current].get(name)
                aliases[name] = (
                    set(values)
                    if values
                    else {_local_shadow_path(current, name)}
                )
        return aliases

    def effective_aliases(scope: ast.AST) -> AliasMap:
        return aliases_for_chain(_scope_chain(scope, tree, parents))

    changed = True
    while changed:
        changed = False
        for scope, node in assignment_records:
            value = _assignment_value(node)
            if value is None:
                continue
            paths = {
                path
                for path in _canonical_paths(value, effective_aliases(scope))
                if _is_aliasable_path(path)
            }
            for name in _name_targets(node):
                for path in paths:
                    # Same-scope alias facts are deliberately monotonic: a later
                    # reassignment does not erase an earlier sensitive origin.
                    changed = (
                        _add_alias(aliases_by_scope[scope], name, path) or changed
                    )

    aliases_by_node: dict[ast.AST, AliasMap] = {}
    for class_scope in (
        scope for scope in scopes if isinstance(scope, ast.ClassDef)
    ):
        outer_aliases = aliases_for_chain(
            _scope_chain(class_scope, tree, parents)[1:]
        )
        current_aliases: AliasMap = {}
        bound_names: set[str] = set()
        for statement in class_scope.body:
            snapshot = {
                name: set(paths) for name, paths in outer_aliases.items()
            }
            for name in bound_names:
                values = current_aliases.get(name)
                snapshot[name] = (
                    set(values)
                    if values
                    else {_local_shadow_path(class_scope, name)}
                )
            for child in ast.walk(statement):
                if _execution_scope(child, tree, parents) is class_scope:
                    aliases_by_node[child] = snapshot

            events = sorted(
                (
                    child
                    for child in ast.walk(statement)
                    if _execution_scope(child, tree, parents) is class_scope
                    and isinstance(
                        child,
                        (
                            ast.Assign,
                            ast.AnnAssign,
                            ast.AugAssign,
                            ast.NamedExpr,
                            ast.Import,
                            ast.ImportFrom,
                            ast.FunctionDef,
                            ast.AsyncFunctionDef,
                            ast.ClassDef,
                        ),
                    )
                ),
                key=lambda child: (
                    getattr(child, "lineno", 0),
                    getattr(child, "col_offset", 0),
                ),
            )
            working = {name: set(paths) for name, paths in snapshot.items()}
            for event in events:
                if isinstance(event, (ast.Import, ast.ImportFrom)):
                    for name, path in _import_bindings(event):
                        bound_names.add(name)
                        _add_alias(current_aliases, name, path)
                        working[name] = set(current_aliases[name])
                elif isinstance(
                    event,
                    (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr),
                ):
                    value = _assignment_value(event)
                    paths = (
                        {
                            path
                            for path in _canonical_paths(value, working)
                            if _is_aliasable_path(path)
                        }
                        if value is not None
                        else set()
                    )
                    for name in _name_targets(event):
                        bound_names.add(name)
                        if paths:
                            for path in paths:
                                _add_alias(current_aliases, name, path)
                            working[name] = set(current_aliases[name])
                        else:
                            current_aliases.pop(name, None)
                            working[name] = {_local_shadow_path(class_scope, name)}
                elif isinstance(
                    event,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                ):
                    bound_names.add(event.name)
                    current_aliases.pop(event.name, None)
                    working[event.name] = {
                        _local_shadow_path(class_scope, event.name)
                    }
        aliases_by_scope[class_scope] = current_aliases

    return (
        parents,
        {scope: effective_aliases(scope) for scope in scopes},
        aliases_by_node,
    )


def _is_sensitive_rebind_target(node: ast.AST, aliases: AliasMap) -> bool:
    if isinstance(node, ast.Name):
        return False
    paths = _canonical_paths(node, aliases)
    if paths & _SENSITIVE_REBINDS or paths & _SENSITIVE_MAPPINGS:
        return True
    if isinstance(node, ast.Subscript):
        return (
            ("sys", "modules") in _canonical_paths(node.value, aliases)
            and _constant_string(node.slice) == "main"
        )
    return ("sys", "modules") in paths


def _candidate_attribute_paths(
    target_node: ast.AST,
    attribute_node: ast.AST,
    aliases: AliasMap,
) -> set[CanonicalPath]:
    attribute = _constant_string(attribute_node)
    if not attribute:
        return set()
    return {
        (*target, attribute)
        for target in _canonical_paths(target_node, aliases)
    }


def _literal_mapping_keys(node: ast.AST) -> set[str] | None:
    if not isinstance(node, ast.Dict):
        return None
    keys: set[str] = set()
    for key_node in node.keys:
        key = _constant_string(key_node)
        if key is None:
            return None
        keys.add(key)
    return keys


def _sensitive_mapping_keys(path: CanonicalPath) -> set[str]:
    if path == ("sys", "modules"):
        return {"main"}
    if path == ("main", "__dict__"):
        return {"app", "lifespan"}
    if path == ("main", "app", "__dict__"):
        return {"router"}
    if path == ("main", "app", "router", "__dict__"):
        return {"lifespan_context"}
    return set()


def _mapping_update_touches_sensitive_keys(
    target: CanonicalPath,
    payload: ast.AST | None,
    keywords: list[ast.keyword],
) -> bool:
    sensitive_keys = _sensitive_mapping_keys(target)
    if not sensitive_keys:
        return False
    if payload is not None:
        keys = _literal_mapping_keys(payload)
        if keys is None or keys & sensitive_keys:
            return True
    for keyword in keywords:
        if keyword.arg is None or keyword.arg in sensitive_keys:
            return True
    return False


def _keyword_is_true(node: ast.Call, name: str) -> bool:
    return any(
        keyword.arg == name
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in node.keywords
    )


def _call_argument(
    node: ast.Call,
    index: int,
    *keyword_names: str,
) -> ast.AST | None:
    if len(node.args) > index:
        return node.args[index]
    for keyword in node.keywords:
        if keyword.arg in keyword_names:
            return keyword.value
    return None


def _call_reloads_main(node: ast.Call, aliases: AliasMap) -> bool:
    if ("importlib", "reload") not in _canonical_paths(node.func, aliases):
        return False
    module_node = _call_argument(node, 0, "module")
    return bool(
        module_node is not None
        and ("main",) in _canonical_paths(module_node, aliases)
    )


def _literal_target_paths(node: ast.AST | None) -> set[CanonicalPath]:
    target = _constant_string(node)
    if not target:
        return set()
    return {tuple(target.split("."))}


def _target_paths(node: ast.AST | None, aliases: AliasMap) -> set[CanonicalPath]:
    if node is None:
        return set()
    return _canonical_paths(node, aliases) | _literal_target_paths(node)


def _call_rebinds_attribute(node: ast.Call, aliases: AliasMap) -> bool:
    function_paths = _canonical_paths(node.func, aliases)
    monkeypatch_paths = {
        ("pytest", "monkeypatch", "delattr"),
        ("pytest", "monkeypatch", "setattr"),
    }
    unbound_paths = {
        ("builtins", "delattr"),
        ("builtins", "setattr"),
        ("object", "__delattr__"),
        ("object", "__setattr__"),
        *monkeypatch_paths,
    }

    if function_paths & unbound_paths:
        candidates: set[CanonicalPath] = set()
        target_node = _call_argument(node, 0, "target")
        attribute_node = _call_argument(node, 1, "attribute", "name")
        if function_paths & monkeypatch_paths:
            dotted_target = _constant_string(target_node)
            if (
                dotted_target
                and tuple(dotted_target.split(".")) in _SENSITIVE_REBINDS
            ):
                return True
        if target_node is not None and attribute_node is not None:
            candidates.update(
                _candidate_attribute_paths(target_node, attribute_node, aliases)
            )
        if candidates & _SENSITIVE_REBINDS:
            return True

    for function_path in function_paths:
        if (
            function_path[-1:] not in {("__setattr__",), ("__delattr__",)}
            or function_path[:-1] not in _SENSITIVE_ATTRIBUTE_OWNERS
        ):
            continue
        attribute_node = _call_argument(node, 0, "attribute", "name")
        attribute = _constant_string(attribute_node)
        if attribute and (*function_path[:-1], attribute) in _SENSITIVE_REBINDS:
            return True

    if ("unittest", "mock", "patch") in function_paths:
        target_node = _call_argument(node, 0, "target")
        target = _constant_string(target_node)
        if target and tuple(target.split(".")) in _SENSITIVE_REBINDS:
            return True

    if ("unittest", "mock", "patch", "object") in function_paths:
        target_node = _call_argument(node, 0, "target")
        attribute_node = _call_argument(node, 1, "attribute")
        if target_node is not None and attribute_node is not None:
            return bool(
                _candidate_attribute_paths(target_node, attribute_node, aliases)
                & _SENSITIVE_REBINDS
            )

    if ("unittest", "mock", "patch", "multiple") in function_paths:
        target_node = _call_argument(node, 0, "target")
        target_paths = _target_paths(target_node, aliases)
        return any(
            keyword.arg and (*target, keyword.arg) in _SENSITIVE_REBINDS
            for target in target_paths
            for keyword in node.keywords
            if keyword.arg != "target"
        )
    return False


def _mapping_mutation_is_sensitive(node: ast.Call, aliases: AliasMap) -> bool:
    function_paths = _canonical_paths(node.func, aliases)
    for function_path in function_paths:
        if not function_path or function_path[-1] not in _MAPPING_OPERATIONS:
            continue
        operation = function_path[-1]
        if function_path in {
            ("pytest", "monkeypatch", "delitem"),
            ("pytest", "monkeypatch", "setitem"),
        }:
            target_node = _call_argument(node, 0, "dic")
            key_node = _call_argument(node, 1, "name")
            if target_node is None or key_node is None:
                continue
            key = _constant_string(key_node)
            if any(
                key in _sensitive_mapping_keys(target)
                for target in _canonical_paths(target_node, aliases)
                if target in {*_SENSITIVE_MAPPINGS, ("sys", "modules")}
            ):
                return True
            continue
        bound_target = function_path[:-1]
        if bound_target == ("sys", "modules") or bound_target in _SENSITIVE_MAPPINGS:
            if operation in {"clear", "popitem"}:
                return True
            if operation in {"__ior__", "ior", "update"}:
                return _mapping_update_touches_sensitive_keys(
                    bound_target,
                    node.args[0] if node.args else None,
                    node.keywords,
                )
            if node.args:
                key = _constant_string(node.args[0])
                if key in _sensitive_mapping_keys(bound_target):
                    return True

        unbound_paths = {
            ("dict", "__delitem__"),
            ("dict", "__ior__"),
            ("dict", "__setitem__"),
            ("dict", "clear"),
            ("dict", "pop"),
            ("dict", "popitem"),
            ("dict", "setdefault"),
            ("dict", "update"),
            ("operator", "delitem"),
            ("operator", "ior"),
            ("operator", "setitem"),
        }
        if function_path not in unbound_paths or not node.args:
            continue
        target_paths = _canonical_paths(node.args[0], aliases)
        for target in target_paths & {
            *_SENSITIVE_MAPPINGS,
            ("sys", "modules"),
        }:
            if operation in {"clear", "popitem"}:
                return True
            if operation in {"__ior__", "ior", "update"}:
                if _mapping_update_touches_sensitive_keys(
                    target,
                    node.args[1] if len(node.args) >= 2 else None,
                    node.keywords,
                ):
                    return True
                continue
            if len(node.args) >= 2:
                key = _constant_string(node.args[1])
                if key in _sensitive_mapping_keys(target):
                    return True
    return False


def _patch_dict_is_sensitive(node: ast.Call, aliases: AliasMap) -> bool:
    function_paths = _canonical_paths(node.func, aliases)
    if ("unittest", "mock", "patch", "dict") not in function_paths:
        return False
    target_node = _call_argument(node, 0, "in_dict", "target")
    if target_node is None:
        return False
    target_paths = _target_paths(target_node, aliases)
    values_node = _call_argument(node, 1, "values")
    entry_keywords = [
        keyword
        for keyword in node.keywords
        if keyword.arg not in {"clear", "in_dict", "target", "values"}
    ]
    for target in target_paths & {
        *_SENSITIVE_MAPPINGS,
        ("sys", "modules"),
    }:
        if _keyword_is_true(node, "clear"):
            return True
        if _mapping_update_touches_sensitive_keys(
            target,
            values_node,
            entry_keywords,
        ):
            return True
    return False


def _call_executes_unwrapped_main(
    node: ast.Call,
    aliases: AliasMap,
    repo_root: Path,
) -> bool:
    function_paths = _canonical_paths(node.func, aliases)
    if ("runpy", "run_module") in function_paths:
        module_node = _call_argument(node, 0, "mod_name")
        return _constant_string(module_node) == "main"
    if ("runpy", "run_path") not in function_paths:
        return False
    path_node = _call_argument(node, 0, "path_name")
    raw_path = _constant_string(path_node)
    if raw_path is None:
        return False
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    try:
        return candidate.resolve() == (repo_root / "main.py").resolve()
    except (OSError, RuntimeError):
        return False


def _call_closes_main_isolation(
    node: ast.Call,
    aliases: AliasMap,
    source_path: Path,
) -> bool:
    function_paths = _canonical_paths(node.func, aliases)
    direct_cleanup_paths = {
        ("tests", "main_lifespan_import", "_close_controller_if_active"),
        ("tests", "main_lifespan_import", "_close_main_import_isolation"),
    }
    active_close = (
        "tests",
        "main_lifespan_import",
        "_active_controller",
        "_close",
    )
    controller_close = (
        "tests",
        "main_lifespan_import",
        "MainImportIsolationController",
        "_close",
    )
    closes_isolation = bool(function_paths & direct_cleanup_paths) or (
        active_close in function_paths or controller_close in function_paths
    )
    if not closes_isolation:
        return False
    if source_path == Path("tests/main_lifespan_import.py"):
        return False
    if source_path == Path("conftest.py"):
        return not bool(function_paths & direct_cleanup_paths)
    return True


def _call_mutates_isolation(
    node: ast.Call,
    aliases: AliasMap,
    repo_root: Path,
    source_path: Path,
) -> bool:
    return bool(
        _call_reloads_main(node, aliases)
        or _call_rebinds_attribute(node, aliases)
        or _mapping_mutation_is_sensitive(node, aliases)
        or _patch_dict_is_sensitive(node, aliases)
        or _call_executes_unwrapped_main(node, aliases, repo_root)
        or _call_closes_main_isolation(node, aliases, source_path)
    )


def _augmented_assignment_mutates_isolation(
    node: ast.AugAssign,
    aliases: AliasMap,
) -> bool:
    target_paths = _canonical_paths(node.target, aliases)
    mapping_targets = target_paths & {
        *_SENSITIVE_MAPPINGS,
        ("sys", "modules"),
    }
    if isinstance(node.op, ast.BitOr) and mapping_targets:
        return any(
            _mapping_update_touches_sensitive_keys(target, node.value, [])
            for target in mapping_targets
        )
    return _is_sensitive_rebind_target(node.target, aliases)


def find_main_lifespan_boundary_violations(repo_root: Path) -> tuple[str, ...]:
    """Find literal mutations that can remove the lazy import storage owner."""

    violations: set[str] = set()
    for path in _python_test_paths(repo_root):
        source_path = path.relative_to(repo_root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents, aliases_by_scope, aliases_by_node = _scoped_aliases(tree)
        for node in ast.walk(tree):
            scope = _execution_scope(node, tree, parents)
            aliases = aliases_by_node.get(node, aliases_by_scope[scope])
            forbidden = False
            if isinstance(
                node,
                (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr, ast.Delete),
            ):
                if isinstance(node, ast.AugAssign):
                    forbidden = _augmented_assignment_mutates_isolation(
                        node,
                        aliases,
                    )
                else:
                    forbidden = any(
                        _is_sensitive_rebind_target(target, aliases)
                        for target in _assignment_targets(node)
                    )
            elif isinstance(node, ast.Call):
                forbidden = _call_mutates_isolation(
                    node,
                    aliases,
                    repo_root,
                    source_path,
                )
            if forbidden:
                violations.add(f"{source_path}:{node.lineno}")
    return tuple(sorted(violations))
