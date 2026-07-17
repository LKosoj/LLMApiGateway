import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_healthcheck() -> ModuleType:
    path = PROJECT_ROOT / "docker" / "healthcheck.py"
    spec = importlib.util.spec_from_file_location("llmgateway_docker_healthcheck", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_healthcheck_makes_one_head_request_to_the_single_health_path(monkeypatch):
    healthcheck = _load_healthcheck()
    connection = Mock()
    connection.getresponse.return_value.status = 200
    connection_factory = Mock(return_value=connection)
    monkeypatch.setenv("GATEWAY_PORT", "9123")
    monkeypatch.setattr(healthcheck.http.client, "HTTPConnection", connection_factory)

    assert healthcheck.check_health() is True

    connection_factory.assert_called_once_with("localhost", 9123, timeout=5)
    connection.request.assert_called_once_with("HEAD", "/health")
    connection.getresponse.assert_called_once_with()
    connection.close.assert_called_once_with()


def test_healthcheck_does_not_retry_a_connection_failure(monkeypatch):
    healthcheck = _load_healthcheck()
    connection_factory = Mock(side_effect=OSError("unavailable"))
    monkeypatch.setattr(healthcheck.http.client, "HTTPConnection", connection_factory)

    assert healthcheck.check_health() is False

    connection_factory.assert_called_once_with("localhost", 9000, timeout=5)


def test_healthcheck_closes_connection_after_unhealthy_response(monkeypatch):
    healthcheck = _load_healthcheck()
    connection = Mock()
    connection.getresponse.return_value.status = 503
    connection_factory = Mock(return_value=connection)
    monkeypatch.setattr(healthcheck.http.client, "HTTPConnection", connection_factory)

    assert healthcheck.check_health() is False

    connection.request.assert_called_once_with("HEAD", "/health")
    connection.getresponse.assert_called_once_with()
    connection.close.assert_called_once_with()


def test_healthcheck_closes_connection_after_http_error(monkeypatch):
    healthcheck = _load_healthcheck()
    connection = Mock()
    connection.getresponse.side_effect = healthcheck.http.client.RemoteDisconnected(
        "closed"
    )
    connection_factory = Mock(return_value=connection)
    monkeypatch.setattr(healthcheck.http.client, "HTTPConnection", connection_factory)

    assert healthcheck.check_health() is False

    connection.request.assert_called_once_with("HEAD", "/health")
    connection.getresponse.assert_called_once_with()
    connection.close.assert_called_once_with()
