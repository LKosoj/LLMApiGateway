import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = PROJECT_ROOT / "docker"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _load_readiness(monkeypatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(DOCKER_DIR))
    path = DOCKER_DIR / "systemd_readiness.py"
    spec = importlib.util.spec_from_file_location("llmgateway_systemd_readiness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patch_http_connection(monkeypatch, readiness: ModuleType, factory: Mock) -> None:
    healthcheck_module = sys.modules[readiness.check_health.__module__]
    monkeypatch.setattr(healthcheck_module.http.client, "HTTPConnection", factory)


def test_readiness_retries_503_then_succeeds_with_gateway_port(monkeypatch) -> None:
    readiness = _load_readiness(monkeypatch)
    clock = FakeClock()
    connection = Mock()
    connection.getresponse.side_effect = [Mock(status=503), Mock(status=200)]
    connection_factory = Mock(return_value=connection)
    monkeypatch.setenv("GATEWAY_PORT", "9123")
    _patch_http_connection(monkeypatch, readiness, connection_factory)

    assert readiness.wait_for_readiness(
        timeout_seconds=3.0,
        retry_interval_seconds=1.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert connection_factory.call_args_list == [
        (("localhost", 9123), {"timeout": 5}),
        (("localhost", 9123), {"timeout": 5}),
    ]
    assert connection.request.call_args_list == [(("HEAD", "/health"), {})] * 2
    assert clock.sleeps == [1.0]


def test_readiness_returns_false_for_persistent_503(monkeypatch) -> None:
    readiness = _load_readiness(monkeypatch)
    clock = FakeClock()
    connection = Mock()
    connection.getresponse.return_value.status = 503
    connection_factory = Mock(return_value=connection)
    _patch_http_connection(monkeypatch, readiness, connection_factory)

    assert not readiness.wait_for_readiness(
        timeout_seconds=2.5,
        retry_interval_seconds=1.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert connection_factory.call_count == 3
    assert clock.sleeps == [1.0, 1.0, 0.5]


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("private connection detail"),
        TimeoutError("private timeout detail"),
    ],
    ids=["connection", "timeout"],
)
def test_readiness_returns_false_for_persistent_connection_failure(monkeypatch, capsys, error: OSError) -> None:
    readiness = _load_readiness(monkeypatch)
    clock = FakeClock()
    connection_factory = Mock(side_effect=error)
    _patch_http_connection(monkeypatch, readiness, connection_factory)

    assert not readiness.wait_for_readiness(
        timeout_seconds=1.5,
        retry_interval_seconds=1.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert connection_factory.call_count == 2
    assert clock.sleeps == [1.0, 0.5]
    assert capsys.readouterr() == ("", "")


def test_readiness_does_not_retry_after_attempt_crosses_deadline(monkeypatch) -> None:
    readiness = _load_readiness(monkeypatch)
    clock = FakeClock()
    check = Mock()

    def cross_deadline() -> bool:
        clock.now = 5.0
        return False

    check.side_effect = cross_deadline

    assert not readiness.wait_for_readiness(
        check=check,
        timeout_seconds=2.0,
        retry_interval_seconds=1.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    check.assert_called_once_with()
    assert clock.sleeps == []


def test_main_failure_output_is_deterministic_and_secret_safe(monkeypatch, capsys) -> None:
    readiness = _load_readiness(monkeypatch)
    monkeypatch.setattr(
        readiness,
        "wait_for_readiness",
        Mock(side_effect=ValueError("TOKEN=secret /private/path")),
    )

    assert readiness.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "systemd-readiness: failed reason=check_error\n"
