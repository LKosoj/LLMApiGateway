from __future__ import annotations

import ast
import inspect
from pathlib import Path

from llm_gateway_core.api.v1 import web
from llm_gateway_core.api.v1 import web_adapters
from llm_gateway_core.api.v1 import web_extraction
from llm_gateway_core.api.v1 import web_research_orchestration
from llm_gateway_core.api.v1 import web_safe_fetch


OWNER_MODULES = {
    "web_safe_fetch": web_safe_fetch,
    "web_extraction": web_extraction,
    "web_adapters": web_adapters,
    "web_research_orchestration": web_research_orchestration,
}


def _module_tree(module) -> ast.Module:
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def test_web_facade_owns_expected_endpoints() -> None:
    expected = {
        "/web/search": "web_search",
        "/web/read": "web_read",
        "/tavily/search": "tavily_search",
        "/tavily/extract": "tavily_extract",
        "/jina/extract": "jina_extract",
        "/jina/search": "jina_search",
        "/web/research": "web_research",
        "/web/deep-research": "web_deep_research",
    }

    expected_routes = {("POST", path, name) for path, name in expected.items()}
    expected_routes.update({
        ("GET", "/jina/extract", "jina_extract"),
        ("GET", "/jina/extract/{url:path}", "jina_extract"),
        ("GET", "/jina/search", "jina_search"),
        ("GET", "/jina/search/{query:path}", "jina_search"),
    })
    assert {
        (method, route.path, route.name) for route in web.router.routes for method in route.methods
    } == expected_routes
    for route in web.router.routes:
        assert inspect.unwrap(route.endpoint).__module__ == web.__name__


def test_fusion_private_imports_remain_facade_reexports() -> None:
    assert web._UsageAccumulator is web_adapters._UsageAccumulator
    assert web._read_with_model is web_adapters._read_with_model
    assert web._read_with_model_observed is web_adapters._read_with_model_observed
    assert web._search_with_model is web_adapters._search_with_model
    assert web._search_with_model_observed is web_adapters._search_with_model_observed


def test_web_owner_modules_form_one_way_dependency_graph() -> None:
    ranks = {
        "web_safe_fetch": 0,
        "web_extraction": 1,
        "web_adapters": 2,
        "web_research_orchestration": 3,
    }

    for owner_name, module in OWNER_MODULES.items():
        for node in _module_tree(module).body:
            if not isinstance(node, ast.ImportFrom) or node.level != 1:
                continue
            dependency = node.module
            assert dependency != "web"
            if dependency in ranks:
                assert ranks[dependency] < ranks[owner_name]


def test_web_facade_does_not_own_network_or_browser_transports() -> None:
    tree = _module_tree(web)
    imported_modules = {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
    defined_names = {
        node.name for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "socket" not in imported_modules
    assert "httpcore" not in imported_modules
    assert (
        not {
            "_PinnedHostNetworkBackend",
            "_PinnedHostAsyncHTTPTransport",
            "_direct_http_fetch",
            "_cloakbrowser_render_sync",
            "_search_zai",
            "_read_zai",
        }
        & defined_names
    )

    assert web_safe_fetch._PinnedHostNetworkBackend.__module__ == web_safe_fetch.__name__
    assert web_extraction._cloakbrowser_render_sync.__module__ == web_extraction.__name__
    assert web_adapters._search_zai.__module__ == web_adapters.__name__
