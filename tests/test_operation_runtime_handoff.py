from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from llm_gateway_core.api.v1 import audio, embeddings, images, pdf
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


OPERATION_MODULES = (
    pytest.param(audio, id="audio"),
    pytest.param(embeddings, id="embeddings"),
    pytest.param(images, id="images"),
    pytest.param(pdf, id="pdf"),
)


def _legacy_aliases() -> dict[str, object]:
    return {
        "operation_dispatcher": Mock(name="legacy-dispatcher"),
        "http_client": Mock(name="legacy-http-client"),
        "config_loader": Mock(name="legacy-config-loader"),
        "proxy_http_clients": {"legacy": Mock(name="legacy-proxy")},
    }


def _request(*, services: object | None, snapshot: object | None) -> SimpleNamespace:
    app_state = SimpleNamespace(**_legacy_aliases())
    if services is not None:
        app_state.services = services

    request_state = SimpleNamespace()
    if snapshot is not None:
        request_state.runtime_snapshot = snapshot

    return SimpleNamespace(
        app=SimpleNamespace(state=app_state),
        state=request_state,
    )


@pytest.mark.parametrize("module", OPERATION_MODULES)
def test_operation_runtime_uses_typed_dependencies_over_conflicting_aliases(module: object) -> None:
    services = make_app_services()
    snapshot = make_runtime_snapshot(
        http_client=services.http_client,
        proxy_http_clients={"provider": Mock(name="canonical-proxy")},
    )

    dispatcher, http_client, config_loader, proxy_http_clients = module._get_operation_runtime(
        _request(services=services, snapshot=snapshot)
    )

    assert dispatcher is snapshot.operation_dispatcher
    assert http_client is services.http_client
    assert config_loader is snapshot.config_loader
    assert proxy_http_clients is snapshot.proxy_http_clients


@pytest.mark.parametrize("module", OPERATION_MODULES)
@pytest.mark.parametrize(
    ("include_services", "include_snapshot"),
    (
        pytest.param(False, True, id="missing-services"),
        pytest.param(True, False, id="missing-snapshot"),
    ),
)
def test_legacy_aliases_do_not_replace_missing_typed_operation_runtime(
    module: object,
    include_services: bool,
    include_snapshot: bool,
) -> None:
    services = make_app_services()
    snapshot = make_runtime_snapshot(http_client=services.http_client)

    with pytest.raises(AttributeError):
        module._get_operation_runtime(
            _request(
                services=services if include_services else None,
                snapshot=snapshot if include_snapshot else None,
            )
        )
