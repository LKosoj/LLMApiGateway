from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

import main
from llm_gateway_core.config.paths import STATIC_DIR


def _make_static_client() -> TestClient:
    app = FastAPI()
    app.mount("/static", main.CachedStaticFiles(directory=str(STATIC_DIR)), name="static")
    return TestClient(app)


def test_cached_static_files_sets_immutable_cache_header_for_fonts() -> None:
    client = _make_static_client()
    response = client.get("/static/vendor/fonts/manrope-latin-wght-normal.woff2")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "public, max-age=31536000, immutable"


def test_cached_static_files_sets_short_cache_header_for_js_and_json() -> None:
    client = _make_static_client()

    js_response = client.get("/static/usage-stats.js")
    assert js_response.status_code == 200
    assert js_response.headers.get("cache-control") == "public, max-age=300"

    json_response = client.get("/static/locales/ru/activity.json")
    assert json_response.status_code == 200
    assert json_response.headers.get("cache-control") == "public, max-age=300"


def test_cached_static_files_does_not_set_cache_header_on_404() -> None:
    client = _make_static_client()
    response = client.get("/static/nonexistent-file-for-cache-test.js")
    assert response.status_code == 404
    assert "cache-control" not in response.headers


def test_app_mounts_static_with_cached_static_files_and_keeps_outputs_images_uncached() -> None:
    mounts = {route.path: route.app for route in main.app.routes if hasattr(route, "app")}
    assert isinstance(mounts["/static"], main.CachedStaticFiles)
    assert type(mounts["/outputs/images"]) is StaticFiles
