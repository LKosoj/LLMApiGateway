from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import create_autospec

from fastapi.testclient import TestClient

import main
from llm_gateway_core.services.health import (
    HealthCheck,
    HealthPhase,
    HealthReport,
    HealthService,
)
from llm_gateway_core.version import __version__
from tests.runtime_test_support import make_app_services


_MISSING = object()


@contextmanager
def _app_services(services: object = _MISSING):
    previous = getattr(main.app.state, "services", _MISSING)
    if services is _MISSING:
        if previous is not _MISSING:
            delattr(main.app.state, "services")
    else:
        main.app.state.services = services
    try:
        yield
    finally:
        if previous is _MISSING:
            if hasattr(main.app.state, "services"):
                delattr(main.app.state, "services")
        else:
            main.app.state.services = previous


def _ready_report() -> HealthReport:
    return HealthReport(
        phase=HealthPhase.READY,
        checks=(HealthCheck("lifecycle", True, "ready"),),
    )


def test_health_get_and_head_report_starting_before_app_services(monkeypatch):
    monkeypatch.setenv("LLMGATEWAY_BUILD_VERSION", "must-be-ignored")
    monkeypatch.setenv("LLMGATEWAY_BUILD_SHA", "abc123")

    with _app_services():
        client = TestClient(main.app, headers={"Accept-Encoding": "identity"})
        get_response = client.get("/health")
        head_response = client.head("/health")

    assert get_response.status_code == 503
    assert get_response.json()["status"] == "error"
    assert get_response.json()["phase"] == "starting"
    assert get_response.headers["X-LLMGateway-Build-Version"] == __version__
    assert get_response.headers["X-LLMGateway-Build-Sha"] == "abc123"
    assert get_response.headers["Cache-Control"] == "no-store"
    assert head_response.status_code == 503
    assert head_response.content == b""
    assert dict(head_response.headers) == dict(get_response.headers)
    assert head_response.headers["Content-Type"] == "application/json"
    assert head_response.headers["Content-Length"] == get_response.headers["Content-Length"]
    assert head_response.headers["X-LLMGateway-Build-Version"] == __version__
    assert head_response.headers["Cache-Control"] == "no-store"


def test_health_get_and_head_share_ready_report_and_headers():
    health_service = create_autospec(HealthService, instance=True, spec_set=True)
    health_service.report.return_value = _ready_report()
    services = make_app_services(health_service=health_service)

    with _app_services(services):
        client = TestClient(main.app, headers={"Accept-Encoding": "identity"})
        get_response = client.get("/health")
        head_response = client.head("/health")

    assert get_response.status_code == 200
    assert get_response.json() == _ready_report().as_dict()
    assert head_response.status_code == 200
    assert head_response.content == b""
    assert dict(head_response.headers) == dict(get_response.headers)
    assert head_response.headers["Content-Type"] == "application/json"
    assert head_response.headers["Content-Length"] == get_response.headers["Content-Length"]
    assert get_response.headers["X-LLMGateway-Build-Version"] == __version__
    assert head_response.headers["X-LLMGateway-Build-Version"] == __version__
    assert get_response.headers["Cache-Control"] == "no-store"
    assert head_response.headers["Cache-Control"] == "no-store"
    assert health_service.report.await_count == 2


def test_health_is_public_before_runtime_startup(monkeypatch):
    monkeypatch.setattr(main.settings, "gateway_api_key", "health-valid-token")

    with _app_services():
        client = TestClient(main.app)
        for headers in (
            {},
            {"Authorization": "Bearer wrong-token"},
            {"Authorization": "Bearer health-valid-token"},
        ):
            health = client.get("/health", headers=headers)
            health_head = client.head("/health", headers=headers)

            assert health.status_code == 503
            assert health_head.status_code == 503


def test_openapi_exposes_only_get_and_head_on_health_and_healthz_paths():
    # /healthz is the liveness endpoint added alongside /health (readiness):
    # both must expose exactly GET and HEAD, nothing else.
    main.app.openapi_schema = None
    schema = main.app.openapi()

    assert set(schema["paths"]["/health"]) == {"get", "head"}
    assert set(schema["paths"]["/healthz"]) == {"get", "head"}
