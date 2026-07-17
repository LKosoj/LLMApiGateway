from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from llm_gateway_core.api.v1 import web as web_api
from llm_gateway_core.api.v1 import web_adapters as web_adapters_owner
from llm_gateway_core.services.accounting import (
    AccountingError,
    AccountingUsage,
    BillingComponent,
    BillingComponentKind,
    CostSource,
    OperationCostCalculator,
)
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from llm_gateway_core.utils.usage_tracking import ModelCostRates
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


def _request(*, services, snapshot, **legacy_aliases):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                services=services,
                **legacy_aliases,
            )
        ),
        state=SimpleNamespace(runtime_snapshot=snapshot),
    )


def test_get_operation_runtime_uses_typed_runtime_over_conflicting_aliases() -> None:
    services = make_app_services()
    snapshot = make_runtime_snapshot(http_client=services.http_client)
    request = _request(
        services=services,
        snapshot=snapshot,
        operation_dispatcher=Mock(name="legacy-dispatcher"),
        http_client=Mock(name="legacy-http-client"),
        config_loader=Mock(name="legacy-config-loader"),
        proxy_http_clients={"legacy": Mock(name="legacy-proxy")},
    )

    dispatcher, http_client, config_loader, proxy_http_clients = web_api._get_operation_runtime(request)

    assert dispatcher is snapshot.operation_dispatcher
    assert http_client is services.http_client
    assert config_loader is snapshot.config_loader
    assert proxy_http_clients is snapshot.proxy_http_clients


def test_get_operation_runtime_fails_when_typed_runtime_wiring_is_missing() -> None:
    services = make_app_services()
    snapshot = make_runtime_snapshot(http_client=services.http_client)

    with pytest.raises(AttributeError):
        web_api._get_operation_runtime(
            SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace()),
                state=SimpleNamespace(runtime_snapshot=snapshot),
            )
        )

    with pytest.raises(AttributeError):
        web_api._get_operation_runtime(
            SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(services=services)),
                state=SimpleNamespace(),
            )
        )


def test_internal_text_model_passes_captured_runtime_dependencies() -> None:
    canonical_routing_state = UpstreamRoutingState()
    services = make_app_services(upstream_routing_state=canonical_routing_state)
    proxy_client = Mock(name="canonical-proxy")
    snapshot = make_runtime_snapshot(
        http_client=services.http_client,
        proxy_http_clients={"provider": proxy_client},
    )
    snapshot.config_loader.providers_config = {
        "provider": SimpleNamespace(baseUrl="https://provider.example", apikey="DIRECT-KEY")
    }
    fallback_rule = {"provider": "provider", "model": "provider-model"}
    snapshot.config_loader.fallback_rules = {"internal-model": {"fallback_models": [fallback_rule]}}
    request = _request(
        services=services,
        snapshot=snapshot,
        proxy_http_clients={"provider": Mock(name="legacy-proxy")},
        upstream_routing_state=UpstreamRoutingState(),
    )
    attempt = AsyncMock(
        return_value=(
            {
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            None,
            2,
        )
    )

    with patch.object(web_adapters_owner, "_attempt_model_fallback_rule", attempt):
        result = run_async(
            web_api._call_internal_text_model(
                request,
                snapshot.config_loader,
                services.http_client,
                model="internal-model",
                messages=[{"role": "user", "content": "question"}],
                temperature=0.0,
                max_tokens=100,
                usage_accumulator=web_api._UsageAccumulator(),
            )
        )

    assert result == "answer"
    call = attempt.await_args
    assert call is not None
    assert call.kwargs["proxy_http_clients"] is snapshot.proxy_http_clients
    assert call.kwargs["upstream_routing_state"] is canonical_routing_state


def test_internal_text_model_observation_uses_final_route_and_frozen_rates() -> None:
    services = make_app_services()
    snapshot = make_runtime_snapshot(
        http_client=services.http_client,
        cost_rate_registry={
            ("provider-b", "model-b"): ModelCostRates(1.0, 2.0),
        },
    )
    snapshot.config_loader.providers_config = {
        "provider-a": SimpleNamespace(),
        "provider-b": SimpleNamespace(),
    }
    snapshot.config_loader.fallback_rules = {
        "internal-model": {
            "fallback_models": [
                {"provider": "provider-a", "model": "model-a"},
                {"provider": "provider-b", "model": "model-b"},
            ]
        }
    }
    request = _request(services=services, snapshot=snapshot)
    attempt = AsyncMock(
        side_effect=[
            (None, "provider-a unavailable", 1),
            (
                {
                    "choices": [{"message": {"content": "answer"}}],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 3,
                        "total_tokens": 5,
                    },
                },
                None,
                2,
            ),
        ]
    )
    accumulator = web_api._UsageAccumulator()

    with patch.object(web_adapters_owner, "_attempt_model_fallback_rule", attempt):
        observed = run_async(
            web_api._call_internal_text_model(
                request,
                snapshot.config_loader,
                services.http_client,
                model="internal-model",
                messages=[{"role": "user", "content": "question"}],
                temperature=0.0,
                max_tokens=100,
                usage_accumulator=accumulator,
                observe=True,
            )
        )

    assert isinstance(observed, web_api._ObservedTextModelResult)
    assert observed.text == "answer"
    assert observed.component.provider == "provider-b"
    assert observed.component.model == "model-b"
    assert observed.component.cost_source is CostSource.TOKEN_REGISTRY
    assert observed.component.usage.cost == pytest.approx(8 / 1_000_000)
    assert accumulator.usage["total_tokens"] == 5
    assert attempt.await_count == 2


def test_internal_text_model_observation_rejects_missing_usage() -> None:
    usage: dict[str, object] = {}
    services = make_app_services()
    snapshot = make_runtime_snapshot(
        http_client=services.http_client,
        cost_rate_registry={
            ("provider", "provider-model"): ModelCostRates(1.0, 1.0),
        },
    )
    snapshot.config_loader.providers_config = {"provider": SimpleNamespace()}
    snapshot.config_loader.fallback_rules = {
        "internal-model": {
            "fallback_models": [
                {"provider": "provider", "model": "provider-model"},
            ]
        }
    }
    request = _request(services=services, snapshot=snapshot)
    attempt = AsyncMock(
        return_value=(
            {
                "choices": [{"message": {"content": "answer"}}],
                "usage": usage,
            },
            None,
            1,
        )
    )

    with (
        patch.object(web_adapters_owner, "_attempt_model_fallback_rule", attempt),
        pytest.raises(AccountingError),
    ):
        run_async(
            web_api._call_internal_text_model(
                request,
                snapshot.config_loader,
                services.http_client,
                model="internal-model",
                messages=[{"role": "user", "content": "question"}],
                temperature=0.0,
                max_tokens=100,
                usage_accumulator=web_api._UsageAccumulator(),
                observe=True,
            )
        )


def test_internal_text_model_observation_accepts_mismatched_total_with_derived_value() -> None:
    """A ``total_tokens`` that disagrees with prompt+completion is tolerated:
    the response is accepted and the derived total (prompt+completion) is used
    instead of rejecting an already-successful upstream call."""
    services = make_app_services()
    snapshot = make_runtime_snapshot(
        http_client=services.http_client,
        cost_rate_registry={
            ("provider", "provider-model"): ModelCostRates(1.0, 1.0),
        },
    )
    snapshot.config_loader.providers_config = {"provider": SimpleNamespace()}
    snapshot.config_loader.fallback_rules = {
        "internal-model": {
            "fallback_models": [
                {"provider": "provider", "model": "provider-model"},
            ]
        }
    }
    request = _request(services=services, snapshot=snapshot)
    attempt = AsyncMock(
        return_value=(
            {
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3},
            },
            None,
            1,
        )
    )

    with patch.object(web_adapters_owner, "_attempt_model_fallback_rule", attempt):
        observed = run_async(
            web_api._call_internal_text_model(
                request,
                snapshot.config_loader,
                services.http_client,
                model="internal-model",
                messages=[{"role": "user", "content": "question"}],
                temperature=0.0,
                max_tokens=100,
                usage_accumulator=web_api._UsageAccumulator(),
                observe=True,
            )
        )

    assert isinstance(observed, web_api._ObservedTextModelResult)
    assert observed.component.usage.prompt_tokens == 1
    assert observed.component.usage.completion_tokens == 1
    assert observed.component.usage.total_tokens == 2
    assert observed.component.usage.cost == pytest.approx(2 / 1_000_000)


@pytest.mark.parametrize(
    ("calculator", "expected_cost", "expected_source"),
    [
        (
            OperationCostCalculator(unit="operation", rate_usd=0.23),
            0.23,
            CostSource.OPERATION_CONFIGURED,
        ),
        (None, 0.1, CostSource.OPERATION_DEFAULT),
    ],
)
def test_observed_search_returns_one_operation_before_internal_components(
    calculator: OperationCostCalculator | None,
    expected_cost: float,
    expected_source: CostSource,
) -> None:
    services = make_app_services()
    service_model = "llmgateway/web-search"
    calculators = {("web_search", service_model): calculator} if calculator is not None else {}
    snapshot = make_runtime_snapshot(
        http_client=services.http_client,
        operation_cost_calculator_registry=calculators,
    )
    request = _request(services=services, snapshot=snapshot)
    model_component = BillingComponent(
        provider="provider",
        model="query-model",
        usage=AccountingUsage(
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            cost=0.01,
        ),
        cost_source=CostSource.UPSTREAM,
    )
    results = [{"url": "https://example.test", "title": "Title", "snippet": "Snippet"}]

    async def search(*_args, component_accumulator, **_kwargs):
        component_accumulator.append(model_component)
        return results

    with patch.object(web_adapters_owner, "_search_with_model", side_effect=search):
        observed = run_async(
            web_api._search_with_model_observed(
                request,
                search_model=service_model,
                query="topic",
                max_results=5,
                num_queries=1,
                language="en",
                usage_accumulator=web_api._UsageAccumulator(),
                expand_query=True,
            )
        )

    assert isinstance(observed, web_api._ObservedWebToolResult)
    assert observed.result is results
    assert isinstance(observed.components, tuple)
    assert len(observed.components) == 2
    operation_component = observed.components[0]
    assert operation_component.component_kind is BillingComponentKind.OPERATION
    assert operation_component.operation == "web_search"
    assert operation_component.gateway_model == service_model
    assert operation_component.usage.cost == expected_cost
    assert operation_component.cost_source is expected_source
    assert observed.components[1] is model_component
