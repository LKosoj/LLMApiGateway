from fastapi.testclient import TestClient

import main


def test_healthz_get_and_head_are_public_and_report_build_headers(monkeypatch):
    monkeypatch.setenv("LLMGATEWAY_BUILD_VERSION", "test-version")
    monkeypatch.setenv("LLMGATEWAY_BUILD_SHA", "abc123")

    client = TestClient(main.app)

    get_response = client.get("/healthz")
    assert get_response.status_code == 200
    assert get_response.json() == {"status": "ok"}
    assert get_response.headers["X-LLMGateway-Build-Version"] == "test-version"
    assert get_response.headers["X-LLMGateway-Build-Sha"] == "abc123"

    head_response = client.head("/healthz")
    assert head_response.status_code == 200
    assert head_response.content == b""
    assert head_response.headers["X-LLMGateway-Build-Version"] == "test-version"


def test_legacy_health_response_body_stays_compatible():
    client = TestClient(main.app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
